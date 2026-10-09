import pytest
from fastapi.testclient import TestClient
from server.main import app
from server.database import SessionLocal
from server.models import LegalDocumentTemplate

client = TestClient(app)

def test_legal_document_template_model_and_tags():
    db = SessionLocal()
    try:
        tmpl = db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.tipo == "impugnacao").first()
        assert tmpl is not None
        assert tmpl.id is not None
        assert tmpl.tipo == "impugnacao"
        assert tmpl.titulo is not None
        assert "14.133" in tmpl.fundamentacao_legal
        assert "{{orgao}}" in tmpl.corpo
        assert "{{numero_edital}}" in tmpl.corpo
        assert "{{objeto}}" in tmpl.corpo
        assert tmpl.is_nova_lei is True
    finally:
        db.close()

def test_legal_document_templates_seeded_types():
    db = SessionLocal()
    try:
        types = [t.tipo for t in db.query(LegalDocumentTemplate).all()]
        assert "impugnacao" in types
        assert "esclarecimento" in types
        assert "intencao_recurso" in types
        assert "recurso" in types
        assert "contrarbenzoes" in types

        nova_lei_templates = db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.is_nova_lei == True).all()
        assert len(nova_lei_templates) >= 5
    finally:
        db.close()

def test_page_juridico_html_rendering_and_filters():
    resp = client.get("/juridico")
    assert resp.status_code == 200
    html = resp.text

    # Título e cabeçalho
    assert "Peças Jurídicas" in html

    # Categorias de filtro
    assert "Impugnação" in html
    assert "Esclarecimento" in html
    assert "Recurso" in html
    assert "Contrarrazões" in html

    # Chave seletora
    assert "Somente nova lei" in html
    assert "14.133/2021" in html

    # Botões de ação
    assert "Visualizar" in html
    assert "Copiar" in html or "copyTemplateText" in html
    assert "downloadTemplateFile" in html

def test_api_list_legal_templates_with_filters():
    # 1. Lista completa
    resp = client.get("/api/juridico/templates")
    assert resp.status_code == 200
    data = resp.json()
    assert "total" in data
    assert data["total"] >= 5
    assert "items" in data

    # 2. Filtro por tipo
    resp_imp = client.get("/api/juridico/templates?tipo=impugnacao")
    assert resp_imp.status_code == 200
    for item in resp_imp.json()["items"]:
        assert item["tipo"] == "impugnacao"

    # 3. Filtro somente nova lei
    resp_nl = client.get("/api/juridico/templates?nova_lei_only=true")
    assert resp_nl.status_code == 200
    for item in resp_nl.json()["items"]:
        assert item["is_nova_lei"] is True

    # 4. Filtro por busca textual
    resp_q = client.get("/api/juridico/templates?q=Esclarecimento")
    assert resp_q.status_code == 200
    assert len(resp_q.json()["items"]) >= 1

def test_api_render_legal_template_variables_replacement():
    db = SessionLocal()
    try:
        tmpl = db.query(LegalDocumentTemplate).first()
        tmpl_id = tmpl.id
    finally:
        db.close()

    resp = client.post(
        f"/api/juridico/templates/{tmpl_id}/render",
        json={
            "orgao": "Prefeitura de Teste",
            "numero_edital": "PE 99/2026",
            "objeto": "Fornecimento de TI"
        }
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "rendered_corpo" in data
    assert "Prefeitura de Teste" in data["rendered_corpo"]
    assert "PE 99/2026" in data["rendered_corpo"]
    assert "Fornecimento de TI" in data["rendered_corpo"]
    assert "{{orgao}}" not in data["rendered_corpo"]
    assert "{{numero_edital}}" not in data["rendered_corpo"]
    assert "{{objeto}}" not in data["rendered_corpo"]
