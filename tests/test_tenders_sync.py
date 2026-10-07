import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from server.main import app
from server.database import SessionLocal
from server.models import Tender, Source
from server.integrations.opportunity_collector import (
    detect_operational_source,
    detect_executor_portal,
    generate_comprasnet_link,
    generate_pncp_link,
    sync_procurement_opportunities
)

client = TestClient(app)


def test_detect_operational_source_compras_gov():
    # 1. Caso com UASG e órgão Federal
    rec1 = {
        "orgaoEntidade": {"esferaId": "F", "razaoSocial": "Ministério da Educação"},
        "unidadeOrgao": {"codigoUnidade": "153037", "nomeUnidade": "UFAL"},
        "linkSistemaOrigem": "https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra=15303705000332026",
        "sistemaOrigem": "Compras.gov.br"
    }
    source_sys, uasg, sicaf = detect_operational_source(rec1)
    assert source_sys == "compras_gov"
    assert uasg == "153037"
    assert sicaf is True

    # 2. Caso com link direto do Comprasnet
    rec2 = {
        "linkSistemaOrigem": "https://www.comprasnet.gov.br/edital/160016",
        "administrative_sphere": "FEDERAL"
    }
    source_sys, uasg, sicaf = detect_operational_source(rec2)
    assert source_sys == "compras_gov"
    assert sicaf is True


def test_detect_operational_source_pncp_fallback():
    # Caso estadual/municipal sem Comprasnet
    rec = {
        "orgaoEntidade": {"esferaId": "E", "razaoSocial": "Governo do Estado de São Paulo"},
        "unidadeOrgao": {"ufSigla": "SP", "municipioNome": "São Paulo"},
        "administrative_sphere": "ESTADUAL",
        "linkSistemaOrigem": "https://www.bec.sp.gov.br",
        "sistemaOrigem": "BEC-SP"
    }
    source_sys, uasg, sicaf = detect_operational_source(rec)
    assert source_sys == "pncp"
    assert sicaf is False


def test_generate_comprasnet_link():
    link1 = generate_comprasnet_link(uasg_code="160016", notice_number="410/2026")
    assert "cnetmobile" in link1
    assert "160016" in link1

    # Link existente preservado
    existing = "https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra=123"
    link2 = generate_comprasnet_link(uasg_code="999999", original_link=existing)
    assert link2 == existing


def test_generate_pncp_link():
    pncp_link = generate_pncp_link(numero_controle_pncp="00394452000103-1-000410/2026")
    assert pncp_link == "https://pncp.gov.br/app/editais/00394452000103/2026/410"


def test_sync_procurement_opportunities_executes_and_deduplicates():
    db = SessionLocal()
    try:
        # Primeira execução: insere ou atualiza
        res1 = sync_procurement_opportunities(db=db, days_back=1)
        assert res1["status"] == "success"
        assert res1["total_processados"] > 0
        assert res1["compras_gov_count"] > 0

        # Segunda execução: não deve duplicar registros no banco
        count_before = db.query(Tender).count()
        res2 = sync_procurement_opportunities(db=db, days_back=1)
        count_after = db.query(Tender).count()

        assert res2["status"] == "success"
        # Contagem não deve duplicar registros com mesmo external_id
        assert count_after == count_before
        assert res2["atualizados"] == res2["total_processados"]
    finally:
        db.close()


def test_api_sync_tenders_endpoint():
    resp = client.post("/api/tenders/sync?days_back=2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert "total_processados" in data
    assert "compras_gov_count" in data
    assert "pncp_count" in data
    assert data["total_processados"] >= data["compras_gov_count"]


def test_api_search_filter_by_source_system():
    # Filtro por Compras.gov.br
    resp_compras = client.get("/api/tenders?source_system=compras_gov")
    assert resp_compras.status_code == 200
    data_compras = resp_compras.json()
    for item in data_compras["results"]:
        assert item["source_system"] == "compras_gov"
        assert item["sicaf_ready"] is True
        assert item["link_comprasnet"] is not None

    # Filtro por PNCP
    resp_pncp = client.get("/api/tenders?source_system=pncp")
    assert resp_pncp.status_code == 200
    data_pncp = resp_pncp.json()
    for item in data_pncp["results"]:
        assert item["source_system"] == "pncp"


def test_search_page_displays_operational_badges():
    resp = client.get("/search")
    assert resp.status_code == 200
    html = resp.text
    # Deve conter menção visual ao botão e às etiquetas Compras.gov.br e PNCP
    assert "Atualizar Oportunidades" in html
    assert "Compras.gov.br (SICAF)" in html
    assert "PNCP" in html
    assert "triggerSyncTenders" in html


def test_sync_collector_fetches_at_least_50_opportunities():
    db = SessionLocal()
    try:
        res = sync_procurement_opportunities(db=db, days_back=2)
        assert res["status"] == "success"
        # Deve processar no mínimo 50 oportunidades
        assert res["total_processados"] >= 50
        assert res["compras_gov_count"] > 0
        assert res["pncp_count"] > 0
    finally:
        db.close()


def test_api_tenders_pagination_and_limits():
    # 1. Padrão deve ser 20 itens por página
    resp_default = client.get("/api/tenders")
    assert resp_default.status_code == 200
    data_default = resp_default.json()
    assert data_default["page_size"] == 20
    assert data_default["total"] >= 1
    assert len(data_default["results"]) <= 20

    # 2. Suporte a parâmetro 'limit'
    resp_limit = client.get("/api/tenders?limit=10")
    assert resp_limit.status_code == 200
    data_limit = resp_limit.json()
    assert data_limit["page_size"] == 10
    assert len(data_limit["results"]) <= 10

    # 3. Suporte a parâmetro 'page_size' com 25
    resp_ps = client.get("/api/tenders?page=1&page_size=25")
    assert resp_ps.status_code == 200
    data_ps = resp_ps.json()
    assert data_ps["page_size"] == 25
    assert data_ps["page"] == 1


def test_search_page_displays_total_synced_and_pagination_controls():
    resp = client.get("/search")
    assert resp.status_code == 200
    html = resp.text
    # Total de oportunidades sincronizadas no banco
    assert "sincronizadas no banco" in html
    assert "Exibindo" in html
    # Seletor de quantidade por página
    assert "Por página:" in html
    assert "page-size-select" in html
    assert "changePageSize" in html
    # Controles de paginação
    assert "Anterior" in html
    assert "Próxima" in html


def test_detect_executor_portal_mappings():
    # Teste de identificação de portais executores
    assert detect_executor_portal({"linkSistemaOrigem": "https://www.bec.sp.gov.br"}) == "BEC/SP"
    assert detect_executor_portal({"sistemaOrigem": "BLL Compras"}) == "BLL Compras"
    assert detect_executor_portal({"link_origem": "https://www.licitacoes-e.com.br"}) == "Licitações-e"
    assert detect_executor_portal({"linkSistemaOrigem": "https://pregaobanrisul.com.br"}) == "Compras RS"
    assert detect_executor_portal({"link_origem": "https://www.compras.mg.gov.br"}) == "Compras MG"
    assert detect_executor_portal({"link_origem": "https://www.portaldecompraspublicas.com.br"}) == "Portal de Compras Públicas"
    assert detect_executor_portal({"sistemaOrigem": "Compras.gov.br", "esferaId": "F"}) == "Compras.gov.br"
    assert detect_executor_portal({"sistemaOrigem": "Desconhecido"}) == "PNCP Geral"


def test_api_sync_tenders_large_scale_and_consolidated_stats():
    # Endpoint aceita parâmetros de profundidade e retorna estatísticas consolidadas
    resp = client.post("/api/tenders/sync?pages=2&limit_per_page=50")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert "total_coletados" in data
    assert "novos_inseridos" in data
    assert "atualizados" in data
    assert "total_geral_banco" in data
    assert data["total_coletados"] >= 50
    assert data["total_geral_banco"] >= data["total_coletados"]


def test_api_search_filter_by_portal():
    # Filtro específico de portal na busca
    resp = client.get("/api/tenders?portal=Compras.gov.br")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    for r in data["results"]:
        assert r["source_system"] == "compras_gov" or "Compras.gov" in (r.get("executor_portal") or "")


def test_dispensas_eletronicas_multi_modality_collection_and_filters():
    # 1. Coleta e sincronização multi-modalidade incluindo Dispensas
    db = SessionLocal()
    try:
        sync_res = sync_procurement_opportunities(db=db, days_back=7, pages=2, limit_per_page=50)
        assert sync_res["status"] == "success"
        assert sync_res.get("dispensas_count", 0) > 0
    finally:
        db.close()

    # 2. Busca por "Dispensa Eletrônica" e por "Dispensa de Licitação" unificadas
    resp_eletronica = client.get("/api/tenders?modality=Dispensa Eletrônica")
    assert resp_eletronica.status_code == 200
    data_eletronica = resp_eletronica.json()
    assert data_eletronica["total"] > 0
    for r in data_eletronica["results"]:
        assert "dispensa" in r["modality"].lower()

    resp_licitacao = client.get("/api/tenders?modality=Dispensa de Licitação")
    assert resp_licitacao.status_code == 200
    data_licitacao = resp_licitacao.json()
    assert data_licitacao["total"] == data_eletronica["total"]

    # 3. Presença de certame militar e de Alagoas (ex: Marinha / UASG 783310 / AL)
    resp_marinha = client.get("/api/tenders?modality=Dispensa&state=AL&uasg_code=783310")
    assert resp_marinha.status_code == 200
    data_marinha = resp_marinha.json()
    assert data_marinha["total"] >= 1
    assert any("783310" in (r.get("uasg_code") or "") for r in data_marinha["results"])
    assert any("Marinha" in (r.get("contracting_entity") or "") for r in data_marinha["results"])

    # 4. Rota HTML /search com filtro de modalidade
    resp_html = client.get("/search?modality=Dispensa Eletrônica")
    assert resp_html.status_code == 200
    assert "Dispensa" in resp_html.text


