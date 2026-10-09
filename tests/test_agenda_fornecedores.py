import os
import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from server.main import app
from server.database import SessionLocal
from server.models import Tender, Deal, SupplierProspect

client = TestClient(app)

def test_deal_deadline_properties():
    db = SessionLocal()
    try:
        tender = db.query(Tender).first()
        assert tender is not None

        deal = Deal(
            tender_id=tender.id,
            stage="disputa",
            notes="Teste de propriedades de prazo"
        )
        db.add(deal)
        db.commit()
        db.refresh(deal)

        assert deal.deadline_urgency in ["critico", "alerta", "normal", "vencido", "sem_data"]
        if deal.target_date:
            assert isinstance(deal.days_remaining, int)

        db.delete(deal)
        db.commit()
    finally:
        db.close()

def test_supplier_prospect_model():
    db = SessionLocal()
    try:
        supp = SupplierProspect(
            name="Empresa Beta Tecnologia Ltda",
            cnpj="12.345.678/0001-90",
            state="DF",
            city="Brasília",
            segment="Tecnologia da Informação",
            total_tenders=10,
            total_value=1500000.0,
            ai_summary="Fornecedor frequente em pregões federais"
        )
        db.add(supp)
        db.commit()
        db.refresh(supp)

        assert supp.id is not None
        assert supp.name == "Empresa Beta Tecnologia Ltda"
        assert supp.state == "DF"

        db.delete(supp)
        db.commit()
    finally:
        db.close()

def test_page_agenda_html_rendering_and_badges():
    resp = client.get("/agenda")
    assert resp.status_code == 200
    html = resp.text

    # 1. Bloco de avisos no topo com badges coloridas para prazos críticos
    assert "Vence hoje / Últimas 24h" in html
    assert "Faltam X dias" in html

    # 2. Calendário interativo
    assert "calendar-days-grid" in html
    assert "calendar-month-year" in html
    assert "btn-view-month" in html
    assert "btn-view-week" in html

    # 3. Coluna lateral com Próximos Prazos e atalhos para o Kanban
    assert "Próximos Prazos" in html
    assert "Atalhos Rápidos" in html
    assert "Funil & Quadros Kanban" in html
    assert "/negocios?view=quadros" in html

def test_api_agenda_events_endpoint():
    resp = client.get("/api/agenda/events")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert "events" in data
    assert isinstance(data["events"], list)
    assert "total" in data

def test_fornecedores_fallback_without_gemini_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("AI_API_KEY", raising=False)

    resp = client.get("/fornecedores")
    assert resp.status_code == 200
    html = resp.text

    # Mensagem amigável de fallback orientando a inserção da chave
    assert "Configuração de IA: Chave do Google Gemini não detectada" in html
    assert "GEMINI_API_KEY=sua_chave_aqui" in html
    assert "Modo Base Ativo" in html
    assert "companies-table" in html

def test_empresas_alias_route(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("AI_API_KEY", raising=False)

    resp = client.get("/empresas")
    assert resp.status_code == 200
    html = resp.text
    assert "Mapeamento &" in html
    assert "Prospecção de Empresas" in html

def test_fornecedores_with_gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-valid-gemini-key-12345")

    resp = client.get("/fornecedores")
    assert resp.status_code == 200
    html = resp.text

    assert "IA Gemini Ativa" in html
    assert "gemini-3.5-flash-lite" in html

def test_api_fornecedores_and_empresas_endpoints():
    for endpoint in ["/api/fornecedores", "/api/empresas"]:
        resp = client.get(endpoint)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert "gemini_configured" in data
        assert "companies" in data
        assert isinstance(data["companies"], list)
        assert "total_companies" in data
