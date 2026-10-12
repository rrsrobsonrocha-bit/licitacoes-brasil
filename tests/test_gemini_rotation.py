"""Rotação de chaves do Gemini (GEMINI_API_KEYS): round-robin, troca de chave em erro de cota e fallback."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai.errors import ClientError

from server.database import SessionLocal
from server.models import Tender
from server.services import gemini_chaves
from server.services.ai_service import analyze_tender_summary


def erro_429(retry="37s"):
    """Erro de cota no formato devolvido pela API do Gemini (google-genai)."""
    return ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                       "message": "You exceeded your current quota.",
                                       "details": [{"retryDelay": retry}]}})


@pytest.fixture(autouse=True)
def rotacao_limpa():
    gemini_chaves.estado.reiniciar()
    yield
    gemini_chaves.estado.reiniciar()


def _rotativo(chaves, falhas, chamadas):
    """Rotação com clientes google-genai falsos: cada chave registra o uso e falha conforme ``falhas``."""
    def fabrica(chave):
        def responder(model, contents=None, config=None):
            chamadas.append(chave)
            if falhas.get(chave) is not None:
                raise falhas[chave]
            return SimpleNamespace(text=json.dumps({"chave": chave, "modelo": model}))

        async def responder_async(**kw):
            return responder(**kw)
        return SimpleNamespace(models=SimpleNamespace(generate_content=responder),
                               aio=SimpleNamespace(models=SimpleNamespace(generate_content=responder_async)))
    return gemini_chaves.ClienteRotativo(chaves, fabrica=fabrica)


# ------------------------------------------------------------------ leitura das chaves
def test_le_gemini_api_keys_com_fallback_para_a_chave_unica(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS", " chave-a , chave-b,,chave-a, chave-c ")
    monkeypatch.setenv("GEMINI_API_KEY", "antiga")
    assert gemini_chaves.chaves_configuradas() == ["chave-a", "chave-b", "chave-c"]

    monkeypatch.setenv("GEMINI_API_KEYS", "")
    assert gemini_chaves.chaves_configuradas() == ["antiga"]
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("AI_API_KEY", "apelido")
    assert gemini_chaves.chaves_configuradas() == ["apelido"]
    monkeypatch.setenv("AI_API_KEY", "")
    assert gemini_chaves.chaves_configuradas() == [] and gemini_chaves.cliente_rotativo() is None


def test_detecta_erros_de_cota():
    assert gemini_chaves.e_erro_de_cota(erro_429())
    assert gemini_chaves.e_erro_de_cota(RuntimeError("429 Too Many Requests"))
    assert gemini_chaves.e_erro_de_cota(type("ResourceExhausted", (Exception,), {})("RESOURCE_EXHAUSTED"))
    assert not gemini_chaves.e_erro_de_cota(ClientError(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "model"}}))
    assert not gemini_chaves.e_erro_de_cota(RuntimeError("503 UNAVAILABLE"))


# ------------------------------------------------------------------ troca de chave
def test_429_na_primeira_chave_refaz_o_pedido_com_a_segunda():
    chamadas = []
    cliente = _rotativo(["k1", "k2"], {"k1": erro_429()}, chamadas)
    resposta = asyncio.run(cliente.aio.models.generate_content(model="gemini-flash", contents="resuma"))
    assert json.loads(resposta.text)["chave"] == "k2"
    assert chamadas == ["k1", "k2"]

    # k1 fica em espera (retryDelay de 37 s): o próximo pedido vai direto para k2
    chamadas.clear()
    asyncio.run(cliente.aio.models.generate_content(model="gemini-flash", contents="outro"))
    assert chamadas == ["k2"]


def test_round_robin_distribui_os_pedidos_entre_as_chaves():
    chamadas = []
    cliente = _rotativo(["k1", "k2", "k3"], {}, chamadas)
    for _ in range(4):
        cliente.models.generate_content(model="m", contents="x")
    assert chamadas == ["k1", "k2", "k3", "k1"]


def test_so_devolve_erro_quando_todas_as_chaves_esgotam_no_mesmo_ciclo():
    chamadas = []
    cliente = _rotativo(["k1", "k2"], {"k1": erro_429(), "k2": erro_429()}, chamadas)
    with pytest.raises(gemini_chaves.CotaGeminiEsgotada) as exc:
        asyncio.run(cliente.aio.models.generate_content(model="m", contents="x"))
    assert chamadas == ["k1", "k2"] and "429 RESOURCE_EXHAUSTED" in str(exc.value) and "2 chave(s)" in str(exc.value)


def test_erro_que_nao_e_de_cota_nao_troca_de_chave():
    chamadas = []
    cliente = _rotativo(["k1", "k2"], {"k1": RuntimeError("503 UNAVAILABLE")}, chamadas)
    with pytest.raises(RuntimeError):
        cliente.models.generate_content(model="m", contents="x")
    assert chamadas == ["k1"]


def test_chave_em_espera_volta_ao_ciclo_depois_do_prazo(monkeypatch):
    relogio = [100.0]
    monkeypatch.setattr(gemini_chaves.estado, "relogio", lambda: relogio[0])
    chamadas = []
    falhas = {"k1": erro_429(retry="10s")}
    cliente = _rotativo(["k1", "k2"], falhas, chamadas)
    cliente.models.generate_content(model="m", contents="x")      # k1 429 -> k2
    falhas.clear()
    chamadas.clear()
    cliente.models.generate_content(model="m", contents="x")      # k1 em espera: só k2
    assert chamadas == ["k2"]
    relogio[0] += 11
    chamadas.clear()
    for _ in range(2):
        cliente.models.generate_content(model="m", contents="x")
    assert sorted(chamadas) == ["k1", "k2"]


# ------------------------------------------------------------------ ponta a ponta no resumo executivo
@pytest.mark.asyncio
async def test_resumo_do_edital_usa_a_segunda_chave_quando_a_primeira_esgota(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS", "chave-esgotada,chave-livre")
    db = SessionLocal()
    tender_id = db.query(Tender.id).first()[0]
    db.close()
    resumo = {"objeto_resumido": "Aquisição de notebooks", "habilitacao_tecnica": ["Atestado"],
              "prazos_entrega_execucao": "30 dias", "condicoes_pagamento_sancoes": "30 dias",
              "pontos_atencao": ["Amostra"]}
    usadas = []

    def cliente_por_chave(api_key):
        cliente = MagicMock()
        if api_key == "chave-esgotada":
            cliente.aio.models.generate_content = AsyncMock(side_effect=erro_429())
        else:
            resposta = MagicMock()
            resposta.text = json.dumps(resumo)
            cliente.aio.models.generate_content = AsyncMock(return_value=resposta)
        usadas.append(api_key)
        return cliente

    with patch("google.genai.Client", side_effect=cliente_por_chave):
        result = await analyze_tender_summary(tender_id=tender_id, force_refresh=True)

    assert result["success"] is True and result["provider"] == "GEMINI"
    assert result["summary"]["objeto_resumido"] == "Aquisição de notebooks"
    assert usadas == ["chave-esgotada", "chave-livre"]
