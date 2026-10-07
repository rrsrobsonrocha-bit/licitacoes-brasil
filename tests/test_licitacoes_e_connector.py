import os
import pytest
from server.connectors.licitacoes_e import LicitacoesEConnector
from server.connectors.registry import get_connector, list_all_sources

def test_licitacoes_e_connector_active_and_properties():
    connector = LicitacoesEConnector()
    assert connector.source_id == "licitacoes-e-04"
    assert connector.official_domain == "licitacoes-e.com.br"
    
    # Com .env carregado (LICITACOES_E_ACTIVE="true", LICITACOES_E_MOCK_MODE="true")
    assert connector.active is True
    assert connector.mock_mode is True
    assert connector.integration_type == "API_MOCK"
    assert "mock" in connector.authorization_notes.lower()

def test_licitacoes_e_connector_catalog_registration():
    connector = get_connector("licitacoes-e-04")
    assert connector is not None
    assert connector.active is True
    
    sources = list_all_sources()
    bb_source = next((s for s in sources if s["id"] == "licitacoes-e-04"), None)
    assert bb_source is not None
    assert bb_source["active"] is True
    assert bb_source["integration_type"] == "API_MOCK"

def test_licitacoes_e_normalization_to_canonical_model():
    connector = LicitacoesEConnector()
    raw_sample = {
        "numeroLicitacao": "1049988",
        "ano": 2026,
        "cnpjOrgao": "00000000000191",
        "titulo": "Solução de Gerenciamento de Identidades e Acessos Corporativos",
        "objeto": "Contratação de subscrição de software para governança de identidades e auditoria contínua.",
        "orgao": "Banco do Brasil S.A. - DITEC",
        "unidadeCompradora": "Centro de Tecnologia e Operações",
        "uf": "DF",
        "municipio": "Brasília",
        "regiao": "Centro-Oeste",
        "esfera": "FEDERAL",
        "modalidade": "Pregão Eletrônico",
        "categoria": "Tecnologia",
        "situacao": "Publicada / Em Disputa",
        "valorTotalEstimado": 12500000.0
    }
    
    tender = connector.normalize_tender(raw_sample)
    
    # 1. Campos de identificação canônica
    assert tender["external_id"] == "00000000000191-1-1049988/2026"
    assert tender["contracting_entity"] == "Banco do Brasil S.A. - DITEC"
    assert tender["state"] == "DF"
    assert tender["modality"] == "Pregão Eletrônico"
    assert tender["estimated_total_value"] == 12500000.0
    
    # 2. URLs canônicas PNCP e sala de disputa
    assert tender["url_pncp"].startswith("https://pncp.gov.br/app/editais/00000000000191/2026/1049988")
    assert "licitacoes-e.com.br" in tender["url_disputa"]
    assert "1049988" in tender["url_disputa"]
    assert tender["executor_portal"] == "Licitações-e"
    assert tender["sistema_origem"] == "Licitações-e (Banco do Brasil)"

def test_licitacoes_e_fetch_tenders_mock():
    connector = LicitacoesEConnector()
    tenders = connector.fetch_tenders({"limit": 5})
    assert len(tenders) > 0
    for t in tenders:
        assert "external_id" in t
        assert t["url_pncp"].startswith("https://pncp.gov.br/app/editais/")
        assert t["executor_portal"] == "Licitações-e"
        assert t["estimated_total_value"] > 0
