import pytest
from datetime import datetime
from fastapi.testclient import TestClient
from server.database import SessionLocal
from server.models import SavedSearch, Alert, AlertDelivery, Tender, User, generate_uuid
from server.services.alert_service import (
    match_tender_with_saved_search,
    check_and_deliver_tender_alerts,
    process_new_tenders_alerts
)
from server.main import app

def test_match_tender_with_saved_search_unit():
    """Valida a lógica pura de matching com diferentes combinações de UF e palavras-chave."""
    saved = SavedSearch(
        id=generate_uuid(),
        name="Radar Tomografia MG",
        keywords="tomógrafo computadorizado",
        ufs="MG, SP",
        min_value=50000.0,
        active=True
    )

    # 1. Correspondência perfeita (UF e palavras-chave)
    tender_match = Tender(
        id=generate_uuid(),
        title="Registro de preços para aquisição de tomógrafo computadorizado 64 canais",
        object_description="Equipamento de diagnóstico por imagem de alta resolução",
        contracting_entity="Secretaria de Estado de Saúde de MG",
        state="MG",
        estimated_total_value=450000.0,
        modality="Pregão Eletrônico"
    )
    assert match_tender_with_saved_search(tender_match, saved) is True

    # 2. Não corresponde por UF divergente
    tender_wrong_uf = Tender(
        id=generate_uuid(),
        title="Aquisição de tomógrafo computadorizado",
        object_description="Diagnóstico hospitalar",
        contracting_entity="SESAU/AL",
        state="AL",
        estimated_total_value=300000.0,
        modality="Pregão Eletrônico"
    )
    assert match_tender_with_saved_search(tender_wrong_uf, saved) is False

    # 3. Não corresponde por palavras-chave ausentes
    tender_wrong_kw = Tender(
        id=generate_uuid(),
        title="Contratação de serviços de limpeza e conservação predial",
        object_description="Mão de obra terceirizada",
        contracting_entity="Hospital Regional MG",
        state="MG",
        estimated_total_value=120000.0,
        modality="Pregão Eletrônico"
    )
    assert match_tender_with_saved_search(tender_wrong_kw, saved) is False

    # 4. Não corresponde por valor abaixo do mínimo
    tender_low_val = Tender(
        id=generate_uuid(),
        title="Manutenção preventiva de tomógrafo computadorizado",
        object_description="Serviço técnico",
        contracting_entity="Prefeitura Municipal MG",
        state="MG",
        estimated_total_value=15000.0,
        modality="Dispensa Eletrônica"
    )
    assert match_tender_with_saved_search(tender_low_val, saved) is False


def test_automatic_alert_matching_and_delivery_creation():
    """Simula a inserção de novo certame e criação de AlertDelivery com deduplicação."""
    db = SessionLocal()
    try:
        user = db.query(User).first()
        uid = user.id if user else "usr-user-01"
        unique_kw = f"cardiodesfibrilador-{generate_uuid()[:8]}"

        saved_alert = SavedSearch(
            id=generate_uuid(),
            user_id=uid,
            name="Alerta Cardíaco RS",
            keywords=unique_kw,
            ufs="RS",
            min_value=10000.0,
            active=True,
            created_at=datetime.utcnow()
        )
        db.add(saved_alert)
        db.commit()

        # Cria certame que atende exatamente ao alerta
        new_tender = Tender(
            id=generate_uuid(),
            source_id="pncp-source-01",
            external_id=f"ext-{generate_uuid()}",
            title=f"Aquisição de {unique_kw} hospitalar bifásico",
            object_description="Equipamento médico para emergência e pronto atendimento",
            contracting_entity="Hospital de Clínicas de Porto Alegre",
            state="RS",
            city="Porto Alegre",
            modality="Pregão Eletrônico",
            estimated_total_value=68000.0,
            status="Divulgada no PNCP",
            source_url="https://pncp.gov.br/app/editais/123"
        )
        db.add(new_tender)
        db.commit()

        # Executa motor de matching de alertas
        deliveries = check_and_deliver_tender_alerts(db, new_tender)
        assert len(deliveries) >= 1

        created_deliv = next((d for d in deliveries if d.alert_id == saved_alert.id), None)
        assert created_deliv is not None
        assert created_deliv.tender_id == new_tender.id
        assert created_deliv.channel == "IN_APP"
        assert created_deliv.status == "SENT"

        # Deduplicação: nova chamada não deve duplicar a entrega
        duplicate_check = check_and_deliver_tender_alerts(db, new_tender)
        duplicate_matching = [d for d in duplicate_check if d.alert_id == saved_alert.id]
        assert len(duplicate_matching) == 0

    finally:
        db.close()


def test_api_alerts_feed_endpoint():
    """Valida o endpoint GET /api/alerts/feed retornando as notificações entregues."""
    client = TestClient(app)
    db = SessionLocal()
    try:
        user = db.query(User).first()
        uid = user.id if user else "usr-user-01"

        # Garante ao menos uma entrega registrada no banco
        feed_kw = f"ultrassom-{generate_uuid()[:8]}"
        saved = SavedSearch(
            id=generate_uuid(),
            user_id=uid,
            name="Feed Alerta Ultrassom",
            keywords=feed_kw,
            ufs="PR",
            min_value=20000.0,
            active=True,
            created_at=datetime.utcnow()
        )
        db.add(saved)

        tender = Tender(
            id=generate_uuid(),
            source_id="pncp-source-01",
            external_id=f"ext-feed-{generate_uuid()}",
            title=f"Fornecimento de sistema de {feed_kw} com doppler colorido",
            object_description="Aparelho portátil para diagnóstico por ultrassom",
            contracting_entity="Secretaria de Saúde de Curitiba",
            state="PR",
            city="Curitiba",
            modality="Pregão Eletrônico",
            estimated_total_value=85000.0,
            status="Divulgada no PNCP",
            source_url="https://pncp.gov.br/app/editais/feed-test"
        )
        db.add(tender)
        db.commit()

        check_and_deliver_tender_alerts(db, tender)

        # Consome o endpoint de feed
        response = client.get("/api/alerts/feed?limit=25")
        assert response.status_code == 200

        payload = response.json()
        assert "total" in payload
        assert "feed" in payload
        assert payload["total"] >= 1

        feed_item = next((item for item in payload["feed"] if item["tender_id"] == tender.id), None)
        assert feed_item is not None
        assert feed_kw in feed_item["title"]
        assert feed_item["state"] == "PR"
        assert feed_item["channel"] == "IN_APP"

    finally:
        db.close()
