import pytest
from server.database import SessionLocal
from server.models import Alert, Tender, AlertDelivery
from server.alerts_engine import match_tender_with_alert, run_alert_processing_job

def test_alert_matching_rules():
    alert = Alert(
        name="Teste Alerta Saúde AL",
        keywords="medicamentos hospitalares",
        filters={"state": "AL", "category": "Medicamentos"},
        frequency="DAILY",
        channels="IN_APP"
    )

    matching_tender = Tender(
        state="AL",
        category="Medicamentos",
        title="Aquisição de medicamentos e materiais hospitalares",
        object_description="Fornecimento de itens de farmácia",
        contracting_entity="SESAU/AL",
        estimated_total_value=100000.0
    )

    non_matching_tender = Tender(
        state="SP",
        category="Tecnologia",
        title="Serviços de Computação em Nuvem",
        object_description="Licenças de software",
        contracting_entity="PRODESP",
        estimated_total_value=500000.0
    )

    assert match_tender_with_alert(matching_tender, alert) is True
    assert match_tender_with_alert(non_matching_tender, alert) is False

def test_alert_job_deduplication():
    db = SessionLocal()
    try:
        # Executar primeira rodada de processamento
        res1 = run_alert_processing_job(db)
        assert res1["active_alerts_evaluated"] >= 1

        # Executar imediatamente segunda rodada
        # Não deve gerar notificações duplicadas para as mesmas licitações já notificadas
        res2 = run_alert_processing_job(db)
        assert res2["total_deliveries_created"] == 0
    finally:
        db.close()

def test_create_and_list_alerts_api(usuario_auth):
    from fastapi.testclient import TestClient
    from server.main import app
    from server.models import SavedSearch

    client = TestClient(app)

    # 1. Criação do alerta de pesquisa via POST /api/alerts
    payload = {
        "keywords": "medicamentos hospitalares",
        "ufs": "AL",
        "min_value": 75000.0,
        "name": "Alerta Saúde AL"
    }
    assert client.post("/api/alerts", json=payload).status_code == 401  # alerta é privado: exige login
    res_post = client.post("/api/alerts", json=payload, headers=usuario_auth.headers)
    assert res_post.status_code in (200, 201)
    data_post = res_post.json()
    assert "id" in data_post
    assert data_post["keywords"] == "medicamentos hospitalares"
    assert data_post["ufs"] == "AL"
    assert data_post["min_value"] == 75000.0
    assert data_post["active"] is True

    # 2. Listagem de alertas ativos via GET /api/alerts
    res_get = client.get("/api/alerts", headers=usuario_auth.headers)
    assert res_get.status_code == 200
    alerts = res_get.json()
    assert isinstance(alerts, list)
    assert len(alerts) >= 1

    target = next((a for a in alerts if a["id"] == data_post["id"]), None)
    assert target is not None
    assert target["keywords"] == "medicamentos hospitalares"
    assert target["ufs"] == "AL"
    assert target["min_value"] == 75000.0
    assert target["active"] is True

    # 3. Validação de persistência na base SQLite via modelo SavedSearch
    db = SessionLocal()
    try:
        saved = db.query(SavedSearch).filter(SavedSearch.id == data_post["id"]).first()
        assert saved is not None
        assert saved.keywords == "medicamentos hospitalares"
        assert saved.ufs == "AL"
        assert saved.min_value == 75000.0
        assert saved.active is True
        assert saved.created_at is not None
    finally:
        db.close()
