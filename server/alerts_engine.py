import logging
from datetime import datetime
from typing import List, Dict, Any
from sqlalchemy.orm import Session
from server.models import Alert, AlertDelivery, Tender, TenderItem
from server.search_engine import parse_search_query, normalize_text

logger = logging.getLogger(__name__)

def match_tender_with_alert(tender: Tender, alert: Alert) -> bool:
    """Verifica se uma licitação cumpre todos os critérios configurados no alerta"""
    filters: Dict[str, Any] = alert.filters or {}

    # 1. Filtro de UF
    if filters.get("state"):
        if tender.state.upper() != str(filters["state"]).upper():
            return False

    # 2. Filtro de Município
    if filters.get("city") and tender.city:
        if normalize_text(filters["city"]) not in normalize_text(tender.city):
            return False

    # 3. Filtro de Modalidade
    if filters.get("modality"):
        if normalize_text(filters["modality"]) not in normalize_text(tender.modality):
            return False

    # 4. Filtro de Categoria
    if filters.get("category") and tender.category:
        if normalize_text(filters["category"]) not in normalize_text(tender.category):
            return False

    # 5. Faixa de valor
    val_min = filters.get("value_min")
    val_max = filters.get("value_max")
    t_val = tender.estimated_total_value or 0.0

    if val_min is not None and t_val < float(val_min):
        return False
    if val_max is not None and t_val > float(val_max):
        return False

    # 6. Palavras-chave do alerta
    keywords = alert.keywords or filters.get("keywords")
    if keywords:
        exacts, terms = parse_search_query(keywords)
        searchable_text = f"{tender.title} {tender.object_description} {tender.contracting_entity}"
        norm_searchable = normalize_text(searchable_text)

        # Deve satisfazer ao menos as frases exatas e termos
        for phrase in exacts:
            if phrase not in norm_searchable:
                return False

        for term in terms:
            if term not in norm_searchable:
                return False

    return True

def run_alert_processing_job(db: Session) -> Dict[str, Any]:
    """
    Executa a verificação periódica de alertas ativos.
    Identifica novas oportunidades e dispara entregas garantindo que
    cada licitação seja entregue apenas uma vez por alerta (Deduplicação).
    """
    active_alerts = db.query(Alert).filter(Alert.active == True).all()
    recent_tenders = db.query(Tender).all()

    total_deliveries_created = 0
    matched_tenders = []

    for alert in active_alerts:
        # Obter IDs de licitações já notificadas para este alerta
        already_delivered_ids = set(
            row[0] for row in db.query(AlertDelivery.tender_id).filter(
                AlertDelivery.alert_id == alert.id
            ).all()
        )

        for tender in recent_tenders:
            if tender.id in already_delivered_ids:
                continue

            if match_tender_with_alert(tender, alert):
                # Criar registro de entrega para in-app e e-mail
                channels = [c.strip() for c in (alert.channels or "IN_APP").split(",")]
                for ch in channels:
                    delivery = AlertDelivery(
                        alert_id=alert.id,
                        tender_id=tender.id,
                        channel=ch,
                        status="SENT",
                        delivered_at=datetime.utcnow()
                    )
                    db.add(delivery)
                    total_deliveries_created += 1

                matched_tenders.append({
                    "alert_name": alert.name,
                    "tender_title": tender.title,
                    "tender_id": tender.id
                })

        alert.last_run_at = datetime.utcnow()

    db.commit()
    logger.info(f"[Alert Engine] Processamento concluído. {total_deliveries_created} novas notificações geradas.")
    return {
        "active_alerts_evaluated": len(active_alerts),
        "total_deliveries_created": total_deliveries_created,
        "matches": matched_tenders
    }
