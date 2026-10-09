import pytest
from fastapi.testclient import TestClient
from server.main import app

client = TestClient(app)

def test_api_dashboard_summary_structure_and_metrics():
    resp = client.get("/api/user/dashboard-summary")
    assert resp.status_code == 200
    data = resp.json()

    # 1. Total de negócios ativos (deals no funil)
    assert "active_deals_count" in data
    assert isinstance(data["active_deals_count"], int)

    # 2. Empresas identificadas
    assert "companies_identified_count" in data
    assert isinstance(data["companies_identified_count"], int)

    # 3. Licitações publicadas hoje e itens
    assert "tenders_today_count" in data
    assert isinstance(data["tenders_today_count"], int)
    assert "total_items_count" in data
    assert isinstance(data["total_items_count"], int)

    # 4. Distribuição por categoria (Serviços, Materiais e Não Classificado)
    assert "categories_distribution" in data
    cat = data["categories_distribution"]
    assert "servicos" in cat
    assert "materiais" in cat
    assert "nao_classificado" in cat
    assert isinstance(cat["servicos"], int)
    assert isinstance(cat["materiais"], int)
    assert isinstance(cat["nao_classificado"], int)

    # 5. Distribuição por faixa de valor (Até R$ 50 mil, R$ 50 mil a R$ 250 mil, Acima de R$ 250 mil)
    assert "value_ranges" in data
    val = data["value_ranges"]
    assert "ate_50k" in val
    assert "de_50k_a_250k" in val
    assert "acima_250k" in val
    assert isinstance(val["ate_50k"], int)
    assert isinstance(val["de_50k_a_250k"], int)
    assert isinstance(val["acima_250k"], int)

    # 6. Próximos eventos da agenda vinculados aos negócios em andamento
    assert "agenda_events" in data
    assert isinstance(data["agenda_events"], list)
    if data["agenda_events"]:
        event = data["agenda_events"][0]
        assert "title" in event
        assert "date" in event

    # 7. Barra de status do funil com contadores por etapa
    assert "funnel_stages" in data
    fs = data["funnel_stages"]
    for stage in ["oportunidades", "qualificacao", "disputa", "classificacao", "contrato"]:
        assert stage in fs
        assert isinstance(fs[stage], int)

    # 8. Compatibilidade com campos legados
    assert "favorites_count" in data
    assert "alerts_count" in data
    assert "deliveries_count" in data
    assert "upcoming_tenders" in data
    assert "recent_deliveries" in data

def test_page_dashboard_html_rendering_executive_cards():
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    html = resp.text

    # Validar os 4 cards de topo
    assert "Negócios ativos" in html
    assert "Empresas identificadas" in html
    assert "Licitações publicadas hoje" in html
    assert "Eventos na agenda" in html

    # Validar caixas de contagem por categoria
    assert "Serviços" in html
    assert "Materiais" in html
    assert "Não Classificado" in html

    # Validar caixas de contagem por faixas de valor
    assert "Até R$ 50 mil" in html
    assert "R$ 50 mil a R$ 250 mil" in html
    assert "Acima de R$ 250 mil" in html

    # Validar barra de status do funil
    assert "Status do Funil de Negócios" in html
    assert "Funil de Licitações" in html
    assert "funnel-count-oportunidades" in html
    assert "funnel-count-qualificacao" in html
    assert "funnel-count-disputa" in html
    assert "funnel-count-classificacao" in html
    assert "funnel-count-contrato" in html
    assert "funnel-bar-oportunidades" in html
    assert "funnel-bar-contrato" in html

    # Validar seção de Agenda da semana
    assert "Agenda da semana" in html

