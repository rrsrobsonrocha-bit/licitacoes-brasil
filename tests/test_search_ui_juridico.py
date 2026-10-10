import html
import re
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from server.main import app
from server.database import SessionLocal
from server.models import LegalDocumentTemplate

client = TestClient(app)


def _hrefs_chips_uf(pagina: str):
    barra = re.search(r'<div id="uf-chips".*?</div>', pagina, re.S).group(0)
    return [html.unescape(h) for h in re.findall(r'href="([^"]*)"', barra)]


# ----------------- Barra horizontal de UFs -----------------

def test_chips_uf_listam_27_estados_e_preservam_filtros():
    resp = client.get("/search", params={"q": "medicamentos", "modality": "Pregão Eletrônico", "status": "Aberta", "uf": "SP", "page": 3})
    assert resp.status_code == 200
    hrefs = _hrefs_chips_uf(resp.text)
    assert len(hrefs) == 28  # "Todos" + 27 UFs

    ufs = []
    for href in hrefs:
        qs = parse_qs(urlparse(href).query)
        assert qs["q"] == ["medicamentos"]
        assert qs["modality"] == ["Pregão Eletrônico"]
        assert qs["status"] == ["Aberta"]
        assert "page" not in qs  # trocar de UF volta para a primeira página
        ufs.append(qs.get("uf", [None])[0])
    assert ufs[0] is None  # "Todos" remove o filtro de UF
    assert len(set(ufs[1:])) == 27


def test_chip_uf_ativo_marcado_com_aria_current():
    pagina = client.get("/search", params={"uf": "ba"}).text
    barra = re.search(r'<div id="uf-chips".*?</div>', pagina, re.S).group(0)
    ativos = re.findall(r'<a [^>]*aria-current="true"[^>]*>([^<]+)</a>', barra)
    assert ativos == ["BA"]


def test_paginacao_preserva_filtro_de_status():
    pagina = client.get("/search", params={"status": "Aberta", "page_size": 1}).text
    links = [html.unescape(h) for h in re.findall(r'href="(\?[^"]*page=\d+[^"]*)"', pagina)]
    if links:  # só há links quando existem mais páginas
        assert all(parse_qs(urlparse(l).query).get("status") == ["Aberta"] for l in links)


# ----------------- Rotas jurídicas e PDF -----------------

def test_gerar_pdf_corpo_nulo_retorna_json_amigavel():
    resp = client.post("/api/juridico/gerar-pdf", content="null", headers={"Content-Type": "application/json"})
    assert resp.status_code == 422
    corpo = resp.json()
    assert isinstance(corpo["detail"], str) and corpo["detail"]


def test_gerar_pdf_campo_invalido_retorna_detail_em_texto():
    resp = client.post("/api/juridico/gerar-pdf", json={"corpo": "x", "titulo": "a" * 300})
    assert resp.status_code == 422
    assert "titulo" in resp.json()["detail"]


def test_gerar_pdf_falha_inesperada_retorna_json_500():
    with patch("server.main.gerar_peticao_pdf", side_effect=RuntimeError("reportlab quebrou")):
        resp = client.post("/api/juridico/gerar-pdf", json={"corpo": "Texto da petição"})
    assert resp.status_code == 500
    assert resp.headers["content-type"].startswith("application/json")
    assert "PDF" in resp.json()["detail"]


def test_validacao_fora_do_juridico_mantem_padrao_fastapi():
    resp = client.get("/api/tenders/qualquer/itens", params={"atualizar": "talvez"})
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], list)


def test_render_template_com_corpo_json_nulo_e_campos_vazios():
    db = SessionLocal()
    try:
        tmpl = LegalDocumentTemplate(tipo="", titulo="Modelo sem corpo", corpo="", is_nova_lei=True)
        db.add(tmpl)
        db.commit()
        tmpl_id = tmpl.id
    finally:
        db.close()

    try:
        resp = client.post(f"/api/juridico/templates/{tmpl_id}/render", content="null", headers={"Content-Type": "application/json"})
        assert resp.status_code == 200
        assert resp.json()["rendered_corpo"] == ""
        assert resp.json()["variables_used"]["orgao"] == "[ÓRGÃO COMPRADOR]"

        detalhe = client.get(f"/api/juridico/templates/{tmpl_id}")
        assert detalhe.status_code == 200
        assert detalhe.json()["tipo_label"] == "Peça jurídica"
    finally:
        db = SessionLocal()
        try:
            db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.id == tmpl_id).delete()
            db.commit()
        finally:
            db.close()
