import logging
from datetime import datetime
from typing import List, Dict, Any, Optional, Union
from sqlalchemy.orm import Session
from server.models import SavedSearch, Alert, AlertDelivery, Tender, generate_uuid
from server.search_engine import parse_search_query, normalize_text

logger = logging.getLogger(__name__)

def match_tender_with_saved_search(tender: Tender, saved_search: SavedSearch) -> bool:
    """
    Compara um certame contra os critérios de um SavedSearch ativo (UFs e palavras-chave).
    Retorna True se todos os critérios configurados forem satisfeitos.
    """
    if not saved_search.active:
        return False

    # 1. Filtro de UF(s)
    if saved_search.ufs:
        tender_uf = (tender.state or "").strip().upper()
        allowed_ufs = {
            uf.strip().upper()
            for uf in saved_search.ufs.replace(";", ",").replace("/", ",").split(",")
            if uf.strip()
        }
        if tender_uf not in allowed_ufs:
            return False

    # 2. Filtro de Palavras-chave / Expressões
    if saved_search.keywords:
        searchable_text = f"{tender.title or ''} {tender.object_description or ''} {tender.contracting_entity or ''}"
        norm_searchable = normalize_text(searchable_text)
        exacts, terms = parse_search_query(saved_search.keywords)

        for phrase in exacts:
            if phrase not in norm_searchable:
                return False

        for term in terms:
            if term not in norm_searchable:
                return False

    # 3. Filtro de Valor Mínimo
    if saved_search.min_value is not None:
        t_val = tender.estimated_total_value or 0.0
        if t_val < float(saved_search.min_value):
            return False

    # 4. Filtros complementares em formato JSON (modalidade, etc.)
    if saved_search.filters and isinstance(saved_search.filters, dict):
        filt_mod = saved_search.filters.get("modality")
        if filt_mod and tender.modality:
            if normalize_text(filt_mod) not in normalize_text(tender.modality):
                return False

    return True


def check_and_deliver_tender_alerts(db: Session, tender: Tender) -> List[AlertDelivery]:
    """
    Compara novo certame contra SavedSearch ativos e registra AlertDelivery
    quando houver correspondência, assegurando deduplicação e integridade relacional.
    """
    active_searches = db.query(SavedSearch).filter(SavedSearch.active == True).all()
    created_deliveries: List[AlertDelivery] = []

    for saved in active_searches:
        if match_tender_with_saved_search(tender, saved):
            # Deduplicação: verifica se já existe despacho para este certame e alerta
            already_delivered = db.query(AlertDelivery).filter(
                AlertDelivery.alert_id == saved.id,
                AlertDelivery.tender_id == tender.id
            ).first()

            if already_delivered:
                continue

            # Assegura que o registro correspondente em Alert exista para satisfazer FK
            alert_rec = db.query(Alert).filter(Alert.id == saved.id).first()
            if not alert_rec:
                # Alertas são privados: busca salva sem dono não gera entrega para nenhuma conta
                target_uid = saved.user_id
                if not target_uid:
                    continue
                alert_rec = Alert(
                    id=saved.id,
                    user_id=target_uid,
                    name=saved.name or "Alerta",
                    keywords=saved.keywords,
                    filters=saved.filters or {},
                    frequency="DAILY",
                    channels="IN_APP",
                    active=saved.active,
                    created_at=saved.created_at
                )
                db.add(alert_rec)
                db.flush()

            delivery = AlertDelivery(
                id=generate_uuid(),
                alert_id=saved.id,
                tender_id=tender.id,
                channel="IN_APP",
                status="SENT",
                delivered_at=datetime.utcnow()
            )
            db.add(delivery)
            created_deliveries.append(delivery)

    if created_deliveries:
        db.commit()
        logger.info(f"[Alert Matching] {len(created_deliveries)} novas entregas geradas para o certame {tender.id}.")

    return created_deliveries


def process_new_tenders_alerts(db: Session, tenders: Union[Tender, List[Tender]]) -> List[AlertDelivery]:
    """Processa lote de novos certames contra os critérios de alertas ativos."""
    if isinstance(tenders, Tender):
        tenders = [tenders]
    all_deliveries: List[AlertDelivery] = []
    for t in tenders:
        all_deliveries.extend(check_and_deliver_tender_alerts(db, t))
    return all_deliveries
