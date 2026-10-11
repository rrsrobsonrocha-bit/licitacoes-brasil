"""Catálogo global permanente, auto-warmup dos últimos 15 dias do PNCP, painel com volume de vigentes e busca."""
import asyncio
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.config import settings
from server.connectors.pncp import PNCPConnector
from server.database import SessionLocal
from server.main import app
from server.models import Tender, hoje_sao_paulo
from server.search_engine import agora_sao_paulo_naive
from server.services import auto_sync

client = TestClient(app)


class PNCPPorDia:
    """API do PNCP simulada que registra as consultas e devolve certames do dia pedido."""

    def __init__(self, prefixo: str, por_modalidade=None):
        self.prefixo = prefixo
        self.por_modalidade = por_modalidade or {6: 2, 8: 1}
        self.chamadas = []

    def __call__(self, data_inicial, data_final, codigo_modalidade=None, uf=None, pagina=1, tamanho_pagina=50):
        self.chamadas.append((data_inicial, data_final, codigo_modalidade, pagina))
        if pagina > 1:
            return {"success": True, "data": {"data": []}}
        dia = datetime.strptime(data_inicial, "%Y%m%d")
        prazo = (agora_sao_paulo_naive() + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S")
        registros = [
            {
                "numeroControlePNCP": f"{self.prefixo}-{data_inicial}-{codigo_modalidade}-{n}",
                "numeroCompra": f"{n}/2026",
                "objetoCompra": f"Aquisição real simulada {self.prefixo} {data_inicial} {n}",
                "modalidadeNome": "Pregão Eletrônico" if codigo_modalidade == 6 else "Dispensa Eletrônica",
                "situacaoCompraNome": "Divulgada no PNCP",
                "valorTotalEstimado": 1000.0 * (n + 1),
                "dataPublicacaoPncp": dia.replace(hour=10).strftime("%Y-%m-%dT%H:%M:%S"),
                "dataAberturaProposta": prazo,
                "dataEncerramentoProposta": prazo,
                "orgaoEntidade": {"cnpj": "00394452000103", "razaoSocial": f"Órgão {self.prefixo}", "esferaId": "F"},
                "unidadeOrgao": {"ufSigla": "DF", "municipioNome": "Brasília", "nomeUnidade": "Unidade"},
            }
            for n in range(self.por_modalidade.get(codigo_modalidade, 0))
        ]
        return {"success": True, "data": {"data": registros}}


def _apagar_por_prefixo(prefixo: str) -> None:
    db = SessionLocal()
    try:
        db.query(Tender).filter(Tender.external_id.like(f"{prefixo}%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _contar(prefixo: str = None) -> int:
    db = SessionLocal()
    try:
        q = db.query(Tender)
        if prefixo:
            q = q.filter(Tender.external_id.like(f"{prefixo}%"))
        return q.count()
    finally:
        db.close()


# ---------------- 1. Auto-warmup dos últimos 15 dias ----------------
def test_warmup_coleta_15_dias_do_mais_recente_ao_mais_antigo_e_deduplica(monkeypatch):
    prefixo = f"WARMUP-{uuid.uuid4().hex[:8]}"
    api = PNCPPorDia(prefixo)
    monkeypatch.setattr(settings, "AUTO_SYNC_DIAS", 15)
    alertas = MagicMock(return_value=[])
    try:
        with patch.object(PNCPConnector, "fetch_tenders_live", api), \
             patch("server.services.alert_service.check_and_deliver_tender_alerts", alertas), \
             patch("server.integrations.opportunity_collector.time.sleep"):
            resumo = auto_sync.executar_warmup()

            hoje = hoje_sao_paulo()
            esperados = [(hoje - timedelta(days=i)).strftime("%Y%m%d") for i in range(15)]
            dias_consultados = []
            for ini, fim, modalidade, _pagina in api.chamadas:
                assert ini == fim  # um dia por vez
                assert modalidade in (6, 8)  # só modalidades com disputa
                if ini not in dias_consultados:
                    dias_consultados.append(ini)
            assert dias_consultados == esperados  # do mais recente para o mais antigo

            assert resumo["novos"] == 15 * 3 and not resumo["falhas"]
            assert _contar(prefixo) == 45
            alertas.assert_not_called()  # acervo retroativo não dispara alertas em massa

            # Segunda execução: upsert por numeroControlePNCP, sem duplicar
            resumo2 = auto_sync.executar_warmup()
            assert resumo2["novos"] == 0 and resumo2["atualizados"] == 45
            assert _contar(prefixo) == 45
    finally:
        _apagar_por_prefixo(prefixo)


def test_atualizacao_periodica_cobre_hoje_e_ontem_e_entrega_alertas():
    prefixo = f"ATUALIZA-{uuid.uuid4().hex[:8]}"
    api = PNCPPorDia(prefixo, {6: 1, 8: 0})
    alertas = MagicMock(return_value=[])
    try:
        with patch.object(PNCPConnector, "fetch_tenders_live", api), \
             patch("server.services.alert_service.check_and_deliver_tender_alerts", alertas):
            resumo = auto_sync.executar_atualizacao()
        hoje = hoje_sao_paulo()
        assert {c[0] for c in api.chamadas} == {hoje.strftime("%Y%m%d"), (hoje - timedelta(days=1)).strftime("%Y%m%d")}
        assert resumo["novos"] == 2
        assert alertas.call_count == 2
    finally:
        _apagar_por_prefixo(prefixo)


def test_api_indisponivel_nao_insere_nem_apaga_nada():
    antes = _contar()

    def _falha(self, *args, **kwargs):
        return {"success": False, "error": "offline"}

    with patch.object(PNCPConnector, "fetch_tenders_live", _falha):
        resumo = auto_sync.executar_warmup(dias=3)
    assert resumo["novos"] == 0
    assert _contar() == antes  # catálogo permanente: nada é removido


def test_decisao_de_warmup(monkeypatch):
    monkeypatch.setattr(auto_sync, "em_producao", lambda: False)
    monkeypatch.setattr(settings, "AUTO_SYNC_MIN_VIGENTES", 10 ** 9)
    assert auto_sync.precisa_warmup(no_arranque=False) is True  # poucos vigentes

    monkeypatch.setattr(settings, "AUTO_SYNC_MIN_VIGENTES", 0)
    assert auto_sync.precisa_warmup(no_arranque=True) is False  # dev com acervo suficiente

    monkeypatch.setattr(auto_sync, "em_producao", lambda: True)
    assert auto_sync.precisa_warmup(no_arranque=True) is True  # todo arranque em produção
    assert auto_sync.precisa_warmup(no_arranque=False) is False


def test_loop_dispara_warmup_no_arranque_sem_acao_do_usuario(monkeypatch):
    warmup = MagicMock(return_value={"tipo": "warmup"})
    atualizacao = MagicMock(return_value={"tipo": "atualizacao"})
    monkeypatch.setattr(auto_sync, "executar_warmup", warmup)
    monkeypatch.setattr(auto_sync, "executar_atualizacao", atualizacao)
    monkeypatch.setattr(auto_sync, "precisa_warmup", lambda no_arranque: no_arranque)

    async def _rodar():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(auto_sync.loop_sincronizacao_automatica(atraso_inicial=0), timeout=1)

    asyncio.run(_rodar())
    warmup.assert_called_once()
    atualizacao.assert_not_called()
    assert auto_sync.estado["proxima_execucao"]


def test_status_da_sincronizacao_e_publico():
    dados = client.get("/api/sync/status").json()
    for chave in ("ativo", "intervalo_minutos", "dias_warmup", "total_editais", "editais_vigentes", "ultima_execucao"):
        assert chave in dados
    assert dados["editais_vigentes"] <= dados["total_editais"]


# ---------------- 2. Catálogo global com acesso imediato ----------------
def test_conta_nova_ve_todo_o_catalogo_imediatamente(criar_usuario):
    conta = criar_usuario("Conta Recém-Criada")
    anonimo = client.get("/api/tenders?vigentes=true&page_size=5").json()
    logado = client.get("/api/tenders?vigentes=true&page_size=5", headers=conta.headers).json()
    assert logado["total"] == anonimo["total"] == _contar_vigentes()


def _contar_vigentes() -> int:
    return auto_sync.contar_vigentes()


# ---------------- 3. Painel e busca ----------------
def _tender(db, marcador: str, orgao: str, prazo_em_dias: int) -> str:
    agora = agora_sao_paulo_naive()
    t = Tender(
        source_id="pncp-source-01",
        external_id=f"{marcador}-{uuid.uuid4().hex[:10]}",
        title=f"Edital {marcador} {uuid.uuid4().hex[:6]}",
        object_description=f"Objeto {marcador}",
        contracting_entity=orgao,
        state="SP",
        modality="Pregão Eletrônico",
        status="Divulgada no PNCP",
        publication_date=agora - timedelta(days=3),
        proposal_deadline=agora + timedelta(days=prazo_em_dias),
        opening_date=agora + timedelta(days=prazo_em_dias),
        source_url="https://pncp.gov.br/app/editais/teste",
    )
    db.add(t)
    db.commit()
    return t.id


def test_painel_destaca_vigentes_orgaos_ativos_e_funil_pronto():
    marcador = f"PAINEL{uuid.uuid4().hex[:6]}"
    antes = client.get("/api/user/dashboard-summary").json()["companies_identified_count"]
    db = SessionLocal()
    try:
        _tender(db, marcador, f"Órgão Ativo A {marcador}", 10)
        _tender(db, marcador, f"Órgão Ativo B {marcador}", 10)
        _tender(db, marcador, f"Órgão Encerrado {marcador}", -5)
    finally:
        db.close()
    try:
        resumo = client.get("/api/user/dashboard-summary").json()
        assert resumo["companies_identified_count"] == antes + 2  # só órgãos com editais vigentes

        html = client.get("/dashboard").text
        card = html.split('id="card-vigentes"', 1)[1].split('id="card-companies"', 1)[0]
        assert "Editais vigentes para disputa" in card and 'id="card-tenders-today"' in card
        assert "publicadas hoje" in card
        assert html.index('id="card-vigentes"') < html.index('id="card-companies"') < html.index('id="card-active-deals"')
        assert "Órgãos mapeados" in html
        assert 'id="funil-vazio-cta"' in html and "Seu funil está pronto." in html
        assert "syncFromDashboard" not in html and 'id="auto-sync-status"' in html
    finally:
        _apagar_por_prefixo(marcador)


def test_funil_vazio_em_negocios_convida_a_explorar_editais(criar_usuario):
    conta = criar_usuario("Funil Vazio")
    html = client.get("/negocios", cookies=conta.cookies).text
    assert 'id="funil-vazio-cta"' in html and "/search?vigentes=true" in html


def test_busca_abre_com_vigentes_e_tem_atalhos_de_periodo():
    marcador = f"BUSCA{uuid.uuid4().hex[:6]}"
    db = SessionLocal()
    try:
        aberto = _tender(db, marcador, "Órgão Busca", 10)
        encerrado = _tender(db, marcador, "Órgão Busca", -5)
    finally:
        db.close()
    try:
        html = client.get(f"/search?q={marcador}").text
        assert aberto in html and encerrado not in html  # padrão: somente vigentes
        assert "Somente vigentes" in html

        todos = client.get(f"/search?q={marcador}&vigentes=false").text
        assert aberto in todos and encerrado in todos

        hoje = hoje_sao_paulo()
        pagina = client.get("/search").text
        ordem = [pagina.index(f'id="{i}"') for i in (
            "atalho-todas-vigentes", "atalho-ultimos-15-dias", "atalho-ultimos-7-dias", "atalho-publicadas-hoje")]
        assert ordem == sorted(ordem)
        assert f"date_from={(hoje - timedelta(days=14)).isoformat()}" in pagina
        assert '<input type="hidden" name="vigentes" value="false" />' in pagina

        # Período explícito não força o filtro de vigentes (publicadas no intervalo, abertas ou não)
        periodo = client.get(f"/search?q={marcador}&date_from={(hoje - timedelta(days=14)).isoformat()}&date_to={hoje.isoformat()}").text
        assert aberto in periodo and encerrado in periodo
    finally:
        _apagar_por_prefixo(marcador)
