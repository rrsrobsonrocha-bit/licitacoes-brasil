"""Drawer de detalhes resiliente: identificador longo, PNCP fora do ar e falhas de banco nunca derrubam a tela."""
import time
import uuid
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from server.database import SessionLocal
from server.main import app
from server.models import Tender
from server.search_engine import agora_sao_paulo_naive
from server.services import pncp_compras

client = TestClient(app)
CNPJ = "12265468000197"


@pytest.fixture
def certame_longo():
    """Certame com external_id longo (> 36 caracteres) e URL oficial do PNCP."""
    marcador = f"LONGO-{uuid.uuid4().hex}"
    external_id = f"{CNPJ}-1-000777-2026-{marcador}-{'X' * 20}"
    assert len(external_id) > 64
    agora = agora_sao_paulo_naive()
    db = SessionLocal()
    t = Tender(
        source_id="pncp-source-01", external_id=external_id, title="Aquisição de notebooks",
        object_description="Registro de preços para notebooks", contracting_entity="Prefeitura de Exemplo",
        state="SP", city="Campinas", status="Divulgada no PNCP", modality="Pregão Eletrônico",
        estimated_total_value=185000.0, url_pncp=f"https://pncp.gov.br/app/editais/{CNPJ}/2026/777",
        source_url="https://pncp.gov.br/app/editais", publication_date=agora - timedelta(days=2),
        proposal_deadline=agora + timedelta(days=8),
    )
    db.add(t)
    db.commit()
    dados = {"id": t.id, "external_id": external_id}
    db.close()
    yield dados
    pncp_compras._transport_override = None
    db = SessionLocal()
    db.query(Tender).filter(Tender.id == dados["id"]).delete(synchronize_session=False)
    db.commit()
    db.close()


def _pncp_fora_do_ar(chamadas):
    def responder(request):
        chamadas.append(str(request.url))
        raise httpx.ReadTimeout("PNCP sem resposta", request=request)
    return httpx.MockTransport(responder)


def test_drawer_com_identificador_longo_e_pncp_fora_do_ar_entrega_dados_basicos(certame_longo):
    chamadas = []
    pncp_compras._transport_override = _pncp_fora_do_ar(chamadas)
    for ident in (certame_longo["id"], certame_longo["external_id"]):
        resp = client.get(f"/api/tenders/{ident}/drawer")
        assert resp.status_code == 200, resp.text
        dados = resp.json()
        t = dados["tender"]
        assert t["id"] == certame_longo["id"] and t["external_id"] == certame_longo["external_id"]
        assert t["orgao"] == "Prefeitura de Exemplo" and t["objeto"] == "Registro de preços para notebooks"
        assert t["valor_estimado"] == 185000.0 and t["data_limite_propostas"]
        assert dados["pncp_indisponivel"] is True
        assert dados["itens"]["lista"] == [] and dados["itens"]["status"] == "indisponivel"
        assert dados["arquivos"]["lista"] == [] and dados["arquivos"]["status"] == "indisponivel"
    # Após o primeiro timeout o disjuntor evita novas esperas: só a 1ª abertura consultou o PNCP
    assert len(chamadas) == 1


def test_drawer_sem_sincronizar_responde_sem_consultar_o_pncp(certame_longo):
    chamadas = []
    pncp_compras._transport_override = _pncp_fora_do_ar(chamadas)
    dados = client.get(f"/api/tenders/{certame_longo['id']}/drawer", params={"sincronizar": "false"}).json()
    assert chamadas == []
    assert dados["sincronizado"] is False and dados["pncp_indisponivel"] is False
    assert dados["itens"]["status"] == "pendente" and dados["tender"]["orgao"] == "Prefeitura de Exemplo"


def test_atualizar_do_pncp_ignora_o_disjuntor(certame_longo):
    pncp_compras._marcar_pncp_fora()
    chamadas = []

    def responder(request):
        chamadas.append(str(request.url))
        return httpx.Response(200, json=[])
    pncp_compras._transport_override = httpx.MockTransport(responder)

    pausado = client.get(f"/api/tenders/{certame_longo['id']}/drawer").json()
    assert chamadas == [] and pausado["pncp_indisponivel"] is True
    assert "não está respondendo" in pausado["itens"]["mensagem"]

    forcado = client.get(f"/api/tenders/{certame_longo['id']}/drawer", params={"atualizar": "true"}).json()
    assert chamadas and forcado["pncp_indisponivel"] is False
    assert forcado["itens"]["status"] == "vazio" and not pncp_compras.pncp_fora_do_ar()


def test_timeout_interativo_e_curto():
    assert pncp_compras.TIMEOUT_INTERATIVO.read <= 10 and pncp_compras.TIMEOUT_INTERATIVO.connect <= 5


def test_falha_inesperada_na_sincronizacao_nao_derruba_o_drawer(certame_longo, monkeypatch):
    async def quebra(*_args, **_kwargs):
        raise RuntimeError("relacionamento inconsistente")

    monkeypatch.setattr(pncp_compras, "sincronizar_itens", quebra)
    monkeypatch.setattr(pncp_compras, "sincronizar_arquivos", quebra)
    resp = client.get(f"/api/tenders/{certame_longo['id']}/drawer")
    assert resp.status_code == 200
    dados = resp.json()
    assert dados["itens"]["status"] == "erro" and dados["arquivos"]["status"] == "erro"
    assert dados["pncp_indisponivel"] is True and dados["tender"]["titulo"] == "Aquisição de notebooks"


def test_espera_pelo_pncp_nao_segura_conexao_do_banco(certame_longo):
    """Durante a consulta ao PNCP a sessão não mantém transação aberta (conexão volta ao pool)."""
    estado = {}

    def responder(request):
        db = SessionLocal()
        try:
            estado["consulta_paralela"] = db.query(Tender.id).filter(Tender.id == certame_longo["id"]).scalar()
        finally:
            db.close()
        return httpx.Response(200, json=[])
    pncp_compras._transport_override = httpx.MockTransport(responder)
    inicio = time.time()
    assert client.get(f"/api/tenders/{certame_longo['id']}/drawer").status_code == 200
    assert estado["consulta_paralela"] == certame_longo["id"] and time.time() - inicio < 10


def test_drawer_frontend_nao_troca_a_tela_por_erro_quando_o_pncp_falha():
    html = client.get("/search").text
    assert "/drawer?sincronizar=false`" in html  # dados básicos primeiro, sem esperar o PNCP
    assert "Itens ainda não sincronizados." in html and "Atualizar do PNCP" in html
    assert "carregarPncpDoDrawer" in html
