import pytest
from server.connectors.pncp import build_canonical_pncp_url
from server.integrations.opportunity_collector import generate_pncp_link
from server.main import app
from fastapi.testclient import TestClient

client = TestClient(app)

def test_build_canonical_pncp_url_with_explicit_params():
    # CNPJ com pontuação, ano numérico, sequencial com zeros à esquerda
    url = build_canonical_pncp_url(
        cnpj="00.394.452/0001-03",
        ano="2026",
        sequencial="000177"
    )
    assert url == "https://pncp.gov.br/app/editais/00394452000103/2026/177"

def test_build_canonical_pncp_url_from_numero_controle_pncp():
    # Padrão oficial numeroControlePNCP: {cnpj}-1-{sequencial}/{ano}
    ctrl = "12200135000180-1-000042/2025"
    url = build_canonical_pncp_url(numero_controle_pncp=ctrl)
    assert url == "https://pncp.gov.br/app/editais/12200135000180/2025/42"

    ctrl2 = "00394452000103-1-000410/2026"
    url2 = build_canonical_pncp_url(numero_controle_pncp=ctrl2)
    assert url2 == "https://pncp.gov.br/app/editais/00394452000103/2026/410"

def test_build_canonical_pncp_url_from_malformed_url():
    # URL antiga quebrada contendo numeroControle ou invertida
    bad_url = "https://pncp.gov.br/app/editais/12200135000180-1-000042/2025"
    canon = build_canonical_pncp_url(numero_controle_pncp=bad_url)
    assert canon == "https://pncp.gov.br/app/editais/12200135000180/2025/42"

    inv_url = "https://pncp.gov.br/app/editais/12200135000180/42/2025"
    canon_inv = build_canonical_pncp_url(numero_controle_pncp=inv_url)
    assert canon_inv == "https://pncp.gov.br/app/editais/12200135000180/2025/42"

def test_build_canonical_pncp_url_from_raw_api_record():
    # Objeto retornado diretamente da API do PNCP
    raw_api = {
        "orgaoEntidade": {
            "cnpj": "46.379.400/0001-50",
            "razaoSocial": "SEDUC SP"
        },
        "anoCompra": 2026,
        "sequencialCompra": 210,
        "numeroCompra": "210/2026"
    }
    url = build_canonical_pncp_url(raw_record=raw_api)
    assert url == "https://pncp.gov.br/app/editais/46379400000150/2026/210"

def test_generate_pncp_link_helper():
    url = generate_pncp_link(
        numero_controle_pncp="00394502000144-1-000177/2026"
    )
    assert url == "https://pncp.gov.br/app/editais/00394502000144/2026/177"

def test_api_search_returns_canonical_and_dispute_links():
    response = client.get("/api/tenders?page=1&page_size=5")
    assert response.status_code == 200
    data = response.json()
    assert "results" in data
    assert len(data["results"]) > 0

    for tender in data["results"]:
        assert "url_pncp" in tender
        assert "url_disputa" in tender
        assert tender["url_pncp"] is not None
        assert "https://pncp.gov.br/app/editais/" in tender["url_pncp"]
        # Verifica se obedece o formato canônico /{cnpj}/{ano}/{sequencial}
        parts = tender["url_pncp"].replace("https://pncp.gov.br/app/editais/", "").split("/")
        assert len(parts) == 3
        cnpj, ano, seq = parts
        assert len(cnpj) == 14 and cnpj.isdigit()
        assert len(ano) == 4 and ano.isdigit()
        assert seq.isdigit()

        # O link de disputa deve existir
        assert tender["url_disputa"] is not None
        assert tender["url_disputa"].startswith("http")


def test_resolve_document_download_url_direct_binary_prioritized():
    from server.main import resolve_document_download_url
    # URL binária direta deve ser mantida com prioridade sobre o redirecionamento ao certame
    direct_pdf = "https://comprasnet.gov.br/editais/anexos/12200135000180/termo_referencia.pdf"
    assert resolve_document_download_url(direct_pdf) == direct_pdf

    direct_download = "https://pncp.gov.br/api/pncp/v1/orgaos/12200135000180/compras/2025/42/arquivos/1/download"
    assert resolve_document_download_url(direct_download) == direct_download


def test_resolve_document_download_url_legacy_converted_to_strict_3_segments():
    from server.main import resolve_document_download_url
    # URL em formato legado com hífen eliminada para estrita 3 segmentos /{cnpj}/{ano}/{sequencial}
    legacy_url = "https://pncp.gov.br/app/editais/12200135000180-1-000042/2025"
    canon = resolve_document_download_url(legacy_url)
    assert canon == "https://pncp.gov.br/app/editais/12200135000180/2025/42"


def test_api_tender_detail_document_canonical_download():
    # Valida no endpoint de detalhe da API se os documentos possuem URLs canônicas
    resp = client.get("/api/tenders?page=1&page_size=1")
    assert resp.status_code == 200
    first_tender = resp.json()["results"][0]
    tender_id = first_tender["id"]

    det_resp = client.get(f"/api/tenders/{tender_id}")
    assert det_resp.status_code == 200
    det_data = det_resp.json()
    assert "documents" in det_data
    assert len(det_data["documents"]) > 0
    for doc in det_data["documents"]:
        assert "source_url" in doc
        assert doc["source_url"] is not None
        # Não deve conter formato legado com hífen composto
        assert "-1-" not in doc["source_url"]
