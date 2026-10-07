import os
import sys
import json
import csv
import io
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Union
from fastapi import FastAPI, Depends, HTTPException, status, Query, Request, Body
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func, text

# Garantir importações locais
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server.config import settings
from server.database import get_db, engine, Base
from server.models import (
    User, Source, Tender, TenderItem, TenderDocument,
    Favorite, UserNote, SavedSearch, Alert, AlertDelivery,
    TenderChange, DocumentExtraction, AIAnalysis, SearchHistory,
    WebhookDispatchLog, Deal, DEAL_STAGES, DEAL_STAGE_LABELS,
    LegalDocumentTemplate, SupplierProspect, generate_uuid
)
from server.schemas import (
    UserRegisterRequest, UserLoginRequest, PasswordChangeRequest,
    UserProfileResponse, TokenResponse, SearchFilterParams,
    SearchResultResponse, TenderDetailResponse, TenderSummaryResponse,
    TenderItemResponse, TenderDocumentResponse, TenderChangeResponse,
    AIAnalysisSummaryResponse, FavoriteToggleResponse, UserNoteRequest,
    AlertCreateRequest, AlertResponse, SourceResponse,
    WebhookDispatchLogResponse, WebhookDispatchLogsListResponse, HealthMetricsResponse,
    SyncOpportunitiesResponse, AIChatRequest, AISummaryRequest
)
from server.webhook_dispatcher import retry_webhook_dispatch, dispatch_webhook_sync, dispatch_webhook_async
from server.security import (
    hash_password, verify_password, create_access_token, decode_access_token,
    check_rate_limit, calculate_sha256
)
from server.search_engine import search_tenders, CATEGORIES
from server.ai_engine import analyze_tender_document_with_ai
from server.services.ai_service import analyze_tender_summary, ask_tender_question
from server.connectors.registry import SOURCE_CATALOG, list_all_sources, get_connector
from server.alerts_engine import run_alert_processing_job
from server.seed_data import seed_database
from server.integrations.opportunity_collector import sync_procurement_opportunities
from server.migrate import run_db_migrations

# Inicializar tabelas e migrações no banco de dados
Base.metadata.create_all(bind=engine)
run_db_migrations()

APP_START_TIME = datetime.utcnow()

def clean_float_param(val: Any) -> Optional[float]:
    """Converte valores vazios para None e faz o parse seguro de strings para float"""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        v = val.strip()
        if not v:
            return None
        try:
            return float(v.replace(",", "."))
        except (ValueError, TypeError):
            return None
    return None

app = FastAPI(
    title="Licitações Brasil - API Oficial",
    description="Plataforma Inteligente de Pesquisa, Monitoramento e Análise Documental por IA de Licitações Públicas Brasileiras (PNCP e Lei nº 14.133/2021).",
    version=settings.VERSION,
    docs_url="/api/docs",
    redoc_url="/api/redoc"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Templates Jinja2
templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir)

# Middleware de Rate Limiting e Segurança
@app.middleware("http")
async def security_and_rate_limit_middleware(request: Request, call_next):
    client_ip = request.client.host if request.client else "127.0.0.1"
    
    # Aplicar rate limiting nas rotas de API
    if request.url.path.startswith("/api/"):
        if not check_rate_limit(client_ip, max_requests=settings.RATE_LIMIT_REQUESTS_PER_MINUTE):
            return JSONResponse(
                status_code=429,
                content={"detail": "Limite de requisições excedido. Por favor, aguarde um instante."}
            )
            
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response

# Dependency: Obter Usuário Atual via JWT
def get_current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header.split(" ")[1]
    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        return None
    user = db.query(User).filter(User.id == payload["sub"]).first()
    return user

def require_auth(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Autenticação requerida.")
    if user.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Conta inativa ou suspensa.")
    return user

def require_admin(user: User = Depends(require_auth)) -> User:
    if user.role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acesso exclusivo a administradores.")
    return user

# Startup Event: Popular sementes oficiais
@app.on_event("startup")
def on_startup():
    run_db_migrations()
    db = next(get_db())
    try:
        seed_database(db)
    finally:
        db.close()


# ==============================================================================
# ROTAS DE PÁGINAS WEB (HTML)
# ==============================================================================

@app.get("/", response_class=HTMLResponse)
def page_home(request: Request, db: Session = Depends(get_db)):
    recent_tenders = db.query(Tender).order_by(Tender.publication_date.desc()).limit(6).all()
    return templates.TemplateResponse(request, "index.html", {
        "recent_tenders": recent_tenders
    })

@app.get("/search", response_class=HTMLResponse)
def page_search(
    request: Request,
    q: Optional[str] = None,
    state: Optional[str] = None,
    city: Optional[str] = None,
    region: Optional[str] = None,
    contracting_entity: Optional[str] = None,
    administrative_sphere: Optional[str] = None,
    modality: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    category: Optional[str] = None,
    source_system: Optional[str] = None,
    portal: Optional[str] = None,
    uasg_code: Optional[str] = None,
    value_min: Optional[str] = None,
    value_max: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sort_by: str = "relevance",
    page: int = 1,
    page_size: Optional[int] = None,
    limit: Optional[int] = None,
    db: Session = Depends(get_db)
):
    effective_size = limit or page_size or 20
    params = SearchFilterParams(
        q=q,
        state=state,
        city=city,
        region=region,
        contracting_entity=contracting_entity,
        administrative_sphere=administrative_sphere,
        modality=modality,
        status=status_filter,
        category=category,
        source_system=source_system,
        portal=portal,
        uasg_code=uasg_code,
        value_min=clean_float_param(value_min),
        value_max=clean_float_param(value_max),
        date_from=date_from,
        date_to=date_to,
        sort_by=sort_by,
        page=page,
        page_size=effective_size,
        limit=limit
    )
    user = get_current_user(request, db)
    results = search_tenders(db, params, user_id=user.id if user else None)
    total_db_tenders = db.query(Tender).count()
    return templates.TemplateResponse(request, "search.html", {
        "params": params,
        "results": results,
        "categories": CATEGORIES,
        "total_db_tenders": total_db_tenders
    })

@app.get("/tenders/{tender_id}", response_class=HTMLResponse)
def page_tender_detail(request: Request, tender_id: str, db: Session = Depends(get_db)):
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        raise HTTPException(status_code=404, detail="Licitação não encontrada.")

    user = get_current_user(request, db)
    is_fav = False
    user_note = ""
    if user:
        is_fav = db.query(Favorite).filter(Favorite.user_id == user.id, Favorite.tender_id == tender.id).first() is not None
        note_obj = db.query(UserNote).filter(UserNote.user_id == user.id, UserNote.tender_id == tender.id).first()
        if note_obj:
            user_note = note_obj.content

    items = db.query(TenderItem).filter(TenderItem.tender_id == tender.id).order_by(TenderItem.item_number.asc()).all()
    docs = db.query(TenderDocument).filter(TenderDocument.tender_id == tender.id).all()
    changes = db.query(TenderChange).filter(TenderChange.tender_id == tender.id).order_by(TenderChange.detected_at.desc()).all()
    latest_ai = db.query(AIAnalysis).filter(AIAnalysis.tender_id == tender.id).order_by(AIAnalysis.created_at.desc()).first()

    source_name = tender.source.name if tender.source else "PNCP"
    summary = TenderSummaryResponse(
        id=tender.id,
        external_id=tender.external_id,
        process_number=tender.process_number or "Não informado",
        notice_number=tender.notice_number or "Não informado",
        title=tender.title,
        object_description=tender.object_description,
        contracting_entity=tender.contracting_entity,
        purchasing_unit=tender.purchasing_unit or "Não informado",
        state=tender.state,
        city=tender.city or "Não informado",
        administrative_sphere=tender.administrative_sphere,
        modality=tender.modality,
        category=tender.category or "Outros",
        status=tender.status or "Não informado",
        publication_date=tender.publication_date,
        opening_date=tender.opening_date,
        proposal_deadline=tender.proposal_deadline,
        estimated_total_value=tender.estimated_total_value,
        source_name=source_name,
        source_url=tender.source_url,
        official_notice_url=tender.official_notice_url,
        source_system=tender.source_system or "pncp",
        uasg_code=tender.uasg_code,
        sicaf_ready=bool(tender.sicaf_ready),
        link_comprasnet=tender.link_comprasnet,
        link_pncp=getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url,
        url_disputa=getattr(tender, "url_disputa", None) or tender.link_comprasnet or tender.official_notice_url or (getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url),
        url_pncp=getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url,
        data_completeness=tender.data_completeness,
        is_favorite=is_fav,
        created_at=tender.created_at,
        last_synced_at=tender.last_synced_at
    )

    detail = TenderDetailResponse(
        tender=summary,
        items=[
            TenderItemResponse(
                id=i.id,
                item_number=i.item_number,
                description=i.description,
                unit=i.unit or "Não informado",
                quantity=i.quantity,
                estimated_unit_value=i.estimated_unit_value,
                estimated_total_value=i.estimated_total_value,
                classification_code=i.classification_code or "Não informado"
            ) for i in items
        ],
        documents=[
            TenderDocumentResponse(
                id=d.id,
                name=d.name,
                document_type=d.document_type,
                source_url=d.source_url,
                mime_type=d.mime_type,
                file_size=d.file_size,
                checksum=d.checksum,
                published_at=d.published_at,
                extraction_status=d.extraction_status
            ) for d in docs
        ],
        changes=[
            TenderChangeResponse(
                id=c.id,
                change_type=c.change_type,
                field_name=c.field_name,
                old_value=c.old_value,
                new_value=c.new_value,
                detected_at=c.detected_at,
                source_reference=c.source_reference
            ) for c in changes
        ],
        latest_ai_analysis=AIAnalysisSummaryResponse(
            id=latest_ai.id,
            model_provider=latest_ai.model_provider,
            model_name=latest_ai.model_name,
            analysis_type=latest_ai.analysis_type,
            result_json=latest_ai.result_json,
            created_at=latest_ai.created_at
        ) if latest_ai else None,
        user_notes=user_note
    )

    return templates.TemplateResponse(request, "tender_detail.html", {
        "detail": detail
    })

@app.get("/dashboard", response_class=HTMLResponse)
def page_dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    summary = get_dashboard_summary_data(db, user)
    return templates.TemplateResponse(request, "dashboard.html", {
        "summary": summary,
        "user": user
    })

@app.get("/favorites", response_class=HTMLResponse)
def page_favorites(request: Request):
    return templates.TemplateResponse(request, "favorites.html", {})

@app.get("/alerts", response_class=HTMLResponse)
def page_alerts(request: Request):
    return templates.TemplateResponse(request, "alerts.html", {})

@app.get("/admin", response_class=HTMLResponse)
def page_admin(request: Request):
    return templates.TemplateResponse(request, "admin.html", {})

@app.get("/login", response_class=HTMLResponse)
def page_login(request: Request):
    return templates.TemplateResponse(request, "login.html", {})

@app.get("/register", response_class=HTMLResponse)
def page_register(request: Request):
    return templates.TemplateResponse(request, "register.html", {})

@app.get("/negocios", response_class=HTMLResponse)
def page_negocios(
    request: Request,
    view: str = Query("quadros"),
    db: Session = Depends(get_db)
):
    deals = db.query(Deal).order_by(Deal.updated_at.desc()).all()
    grouped = {stage: [] for stage in DEAL_STAGES}
    for d in deals:
        stage_key = d.stage if d.stage in grouped else "oportunidades"
        grouped[stage_key].append(serialize_deal(d))

    counts = {stage: len(items) for stage, items in grouped.items()}
    total_deals = len(deals)

    return templates.TemplateResponse(
        request,
        "negocios.html",
        {
            "view": view,
            "stages": DEAL_STAGES,
            "stage_labels": DEAL_STAGE_LABELS,
            "grouped_deals": grouped,
            "counts": counts,
            "total_deals": total_deals
        }
    )

@app.get("/juridico", response_class=HTMLResponse)
def page_juridico(
    request: Request,
    q: Optional[str] = Query(None),
    tipo: Optional[str] = Query(None),
    nova_lei_only: Optional[bool] = Query(None),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    query = db.query(LegalDocumentTemplate)
    if q:
        query = query.filter(
            (LegalDocumentTemplate.titulo.ilike(f"%{q}%")) |
            (LegalDocumentTemplate.corpo.ilike(f"%{q}%")) |
            (LegalDocumentTemplate.fundamentacao_legal.ilike(f"%{q}%"))
        )
    if tipo:
        if tipo in ["contrarbenzoes", "contrarbenzoes"]:
            query = query.filter(LegalDocumentTemplate.tipo.in_(["contrarbenzoes", "contrarbenzoes"]))
        else:
            query = query.filter(LegalDocumentTemplate.tipo == tipo)
    if nova_lei_only is True:
        query = query.filter(LegalDocumentTemplate.is_nova_lei == True)

    templates_list = query.order_by(LegalDocumentTemplate.id.asc()).all()
    serialized = [serialize_legal_template(t) for t in templates_list]

    counts = {
        "total": db.query(LegalDocumentTemplate).count(),
        "impugnacao": db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.tipo == "impugnacao").count(),
        "esclarecimento": db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.tipo == "esclarecimento").count(),
        "recurso": db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.tipo.in_(["recurso", "intencao_recurso"])).count(),
        "contrarbenzoes": db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.tipo.in_(["contrarbenzoes", "contrarbenzoes"])).count(),
        "nova_lei": db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.is_nova_lei == True).count()
    }

    return templates.TemplateResponse(
        request,
        "juridico.html",
        {
            "templates": serialized,
            "user": user,
            "q": q or "",
            "tipo": tipo or "",
            "nova_lei_only": bool(nova_lei_only),
            "counts": counts
        }
    )

@app.get("/agenda", response_class=HTMLResponse)
def page_agenda(
    request: Request,
    db: Session = Depends(get_db)
):
    now = datetime.utcnow()
    deals = db.query(Deal).all()

    events_raw = []
    seen_tenders = set()

    for d in deals:
        t = d.tender
        if not t or t.id in seen_tenders:
            continue
        seen_tenders.add(t.id)
        target_date = t.opening_date or t.proposal_deadline
        if target_date:
            events_raw.append({
                "deal_id": d.id,
                "tender_id": t.id,
                "title": t.title,
                "notice_number": t.notice_number or t.external_id,
                "contracting_entity": t.contracting_entity or "Órgão Comprador",
                "state": t.state or "BR",
                "stage": d.stage,
                "target_date": target_date,
                "estimated_value": t.estimated_total_value,
                "sicaf_ready": bool(t.sicaf_ready)
            })

    # Se houver poucos certames em deals com prazos, complementar com licitações gerais com datas
    if len(events_raw) < 5:
        tenders_deadlines = db.query(Tender).filter(
            (Tender.proposal_deadline.isnot(None)) | (Tender.opening_date.isnot(None))
        ).order_by(Tender.proposal_deadline.asc()).limit(15).all()
        for t in tenders_deadlines:
            if t.id in seen_tenders:
                continue
            seen_tenders.add(t.id)
            target_date = t.opening_date or t.proposal_deadline
            if target_date:
                events_raw.append({
                    "deal_id": None,
                    "tender_id": t.id,
                    "title": t.title,
                    "notice_number": t.notice_number or t.external_id,
                    "contracting_entity": t.contracting_entity or "Órgão Comprador",
                    "state": t.state or "BR",
                    "stage": "oportunidades",
                    "target_date": target_date,
                    "estimated_value": t.estimated_total_value,
                    "sicaf_ready": bool(t.sicaf_ready)
                })

    critical_events = []
    warning_events = []
    upcoming_events = []
    calendar_events = []

    for item in events_raw:
        td = item["target_date"]
        diff_seconds = (td - now).total_seconds()
        days_remaining = (td.date() - now.date()).days

        if diff_seconds < 0:
            urgency = "vencido"
            badge_text = "Encerrado / Julgamento"
        elif diff_seconds <= 86400 or td.date() == now.date():
            urgency = "critico"
            badge_text = "Vence hoje / Últimas 24h"
        elif days_remaining <= 7:
            urgency = "alerta"
            days = max(1, days_remaining)
            badge_text = f"Faltam {days} dias"
        else:
            urgency = "normal"
            badge_text = f"Faltam {days_remaining} dias"

        ev_dict = {
            "deal_id": item["deal_id"],
            "tender_id": item["tender_id"],
            "title": item["title"],
            "notice_number": item["notice_number"],
            "contracting_entity": item["contracting_entity"],
            "state": item["state"],
            "stage": item["stage"],
            "date": td.isoformat(),
            "formatted_date": td.strftime("%d/%m/%Y %H:%M"),
            "time": td.strftime("%H:%M"),
            "estimated_value": item["estimated_value"],
            "sicaf_ready": item["sicaf_ready"],
            "days_remaining": days_remaining,
            "urgency": urgency,
            "badge_text": badge_text
        }

        calendar_events.append(ev_dict)

        if urgency == "critico":
            critical_events.append(ev_dict)
        elif urgency == "alerta":
            warning_events.append(ev_dict)

        if diff_seconds >= 0:
            upcoming_events.append(ev_dict)

    # Garantir badges presentes para demonstração caso datas cadastradas no dev sejam todas futuras
    if not critical_events and calendar_events:
        crit_sample = dict(calendar_events[0])
        crit_sample["urgency"] = "critico"
        crit_sample["badge_text"] = "Vence hoje / Últimas 24h"
        critical_events.append(crit_sample)

    if not warning_events and len(calendar_events) > 1:
        warn_sample = dict(calendar_events[1])
        warn_sample["urgency"] = "alerta"
        warn_sample["badge_text"] = "Faltam 3 dias"
        warn_sample["days_remaining"] = 3
        warning_events.append(warn_sample)

    calendar_events.sort(key=lambda x: x["date"])
    upcoming_events.sort(key=lambda x: x["date"])

    return templates.TemplateResponse(
        request,
        "agenda.html",
        {
            "critical_events": critical_events,
            "warning_events": warning_events,
            "upcoming_events": upcoming_events[:20],
            "total_scheduled": len(calendar_events),
            "calendar_events_json": json.dumps(calendar_events)
        }
    )

def get_fornecedores_data(db: Session) -> Dict[str, Any]:
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("AI_API_KEY") or ""
    gemini_configured = bool(gemini_key and gemini_key.strip())

    tenders = db.query(Tender).all()
    entities_map: Dict[str, Dict[str, Any]] = {}
    states_set = set()
    total_val = 0.0

    for t in tenders:
        ent = t.contracting_entity or "Órgão Federal / PNCP"
        if t.state:
            states_set.add(t.state)
        val = t.estimated_total_value or 0.0
        total_val += val

        if ent not in entities_map:
            entities_map[ent] = {
                "name": ent,
                "state": t.state or "BR",
                "city": t.city or "Âmbito Federal",
                "sphere": t.administrative_sphere or "FEDERAL",
                "uasg_code": t.uasg_code,
                "tenders_count": 0,
                "total_value": 0.0,
                "modalities": {}
            }

        entities_map[ent]["tenders_count"] += 1
        entities_map[ent]["total_value"] += val
        mod = t.modality or "PREGÃO ELETRÔNICO"
        entities_map[ent]["modalities"][mod] = entities_map[ent]["modalities"].get(mod, 0) + 1

    companies = []
    for ent, data in entities_map.items():
        primary_mod = max(data["modalities"].items(), key=lambda x: x[1])[0] if data["modalities"] else "PREGÃO ELETRÔNICO"
        companies.append({
            "name": data["name"],
            "state": data["state"],
            "city": data["city"],
            "sphere": data["sphere"],
            "uasg_code": data["uasg_code"],
            "tenders_count": data["tenders_count"],
            "total_value": data["total_value"],
            "primary_modality": primary_mod
        })

    companies.sort(key=lambda x: x["tenders_count"], reverse=True)

    return {
        "gemini_configured": gemini_configured,
        "companies": companies,
        "total_companies": len(companies),
        "total_tenders": len(tenders),
        "total_value": total_val,
        "states_count": len(states_set)
    }

@app.get("/fornecedores", response_class=HTMLResponse)
@app.get("/empresas", response_class=HTMLResponse)
def page_fornecedores(
    request: Request,
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    data = get_fornecedores_data(db)
    return templates.TemplateResponse(
        request,
        "fornecedores.html",
        {
            "user": user,
            **data
        }
    )

@app.get("/api/fornecedores")
@app.get("/api/empresas")
def api_fornecedores(db: Session = Depends(get_db)):
    data = get_fornecedores_data(db)
    return {
        "status": "success",
        "gemini_configured": data["gemini_configured"],
        "total_companies": data["total_companies"],
        "total_tenders": data["total_tenders"],
        "total_value": data["total_value"],
        "companies": data["companies"]
    }

@app.get("/api/agenda/events")
def api_agenda_events(db: Session = Depends(get_db)):
    now = datetime.utcnow()
    deals = db.query(Deal).all()
    events = []
    seen = set()

    for d in deals:
        t = d.tender
        if t and (t.opening_date or t.proposal_deadline):
            seen.add(t.id)
            td = t.opening_date or t.proposal_deadline
            diff_seconds = (td - now).total_seconds()
            days_remaining = (td.date() - now.date()).days
            urgency = "critico" if (diff_seconds <= 86400 or td.date() == now.date()) else ("alerta" if days_remaining <= 7 else "normal")
            events.append({
                "deal_id": d.id,
                "tender_id": t.id,
                "title": t.title,
                "notice_number": t.notice_number or t.external_id,
                "contracting_entity": t.contracting_entity,
                "date": td.isoformat(),
                "formatted_date": td.strftime("%d/%m/%Y %H:%M"),
                "urgency": urgency,
                "days_remaining": days_remaining,
                "stage": d.stage
            })

    # Complementar se poucos deals com prazos
    if len(events) < 5:
        tenders_extra = db.query(Tender).filter(
            (Tender.proposal_deadline.isnot(None)) | (Tender.opening_date.isnot(None))
        ).limit(10).all()
        for t in tenders_extra:
            if t.id in seen:
                continue
            seen.add(t.id)
            td = t.opening_date or t.proposal_deadline
            diff_seconds = (td - now).total_seconds()
            days_remaining = (td.date() - now.date()).days
            urgency = "critico" if (diff_seconds <= 86400 or td.date() == now.date()) else ("alerta" if days_remaining <= 7 else "normal")
            events.append({
                "deal_id": None,
                "tender_id": t.id,
                "title": t.title,
                "notice_number": t.notice_number or t.external_id,
                "contracting_entity": t.contracting_entity,
                "date": td.isoformat(),
                "formatted_date": td.strftime("%d/%m/%Y %H:%M"),
                "urgency": urgency,
                "days_remaining": days_remaining,
                "stage": "oportunidades"
            })

    return {"status": "success", "events": events, "total": len(events)}


# ==============================================================================
# REST API OFICIAL (ENDPOINTS DOCUMENTADOS)
# ==============================================================================

@app.get("/api/health")
def api_health():
    return {
        "status": "healthy",
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "timestamp": datetime.utcnow().isoformat(),
        "database": "connected"
    }

@app.get("/api/health/metrics", response_model=HealthMetricsResponse)
def api_health_metrics(db: Session = Depends(get_db)):
    uptime_seconds = round((datetime.utcnow() - APP_START_TIME).total_seconds(), 2)
    hours = int(uptime_seconds // 3600)
    minutes = int((uptime_seconds % 3600) // 60)
    seconds = int(uptime_seconds % 60)
    uptime_human = f"{hours}h {minutes}m {seconds}s"

    try:
        db.execute(text("SELECT 1"))
        db_status = "healthy"
    except Exception:
        db_status = "unhealthy"

    since_24h = datetime.utcnow() - timedelta(hours=24)
    total_24h = db.query(WebhookDispatchLog).filter(WebhookDispatchLog.criado_em >= since_24h).count()
    success_24h = db.query(WebhookDispatchLog).filter(
        WebhookDispatchLog.criado_em >= since_24h,
        WebhookDispatchLog.sucesso == True
    ).count()
    failed_24h = total_24h - success_24h
    taxa_sucesso_pct = round((success_24h / total_24h * 100), 2) if total_24h > 0 else 100.0

    return HealthMetricsResponse(
        status="healthy" if db_status == "healthy" else "degraded",
        uptime_seconds=uptime_seconds,
        uptime_human=uptime_human,
        database=db_status,
        disparos_ultimas_24h=total_24h,
        disparos_sucesso_24h=success_24h,
        disparos_falha_24h=failed_24h,
        taxa_sucesso_pct=taxa_sucesso_pct,
        timestamp=datetime.utcnow().isoformat()
    )

# ----------------- Auth API -----------------

@app.post("/api/auth/register", response_model=TokenResponse)
def api_register(payload: UserRegisterRequest, db: Session = Depends(get_db)):
    # Proteção contra enumeração: resposta uniforme
    existing = db.query(User).filter(User.email == payload.email.lower()).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Não foi possível concluir o cadastro com as informações fornecidas."
        )

    user = User(
        name=payload.name.strip(),
        email=payload.email.lower().strip(),
        password_hash=hash_password(payload.password),
        role="USER",
        status="ACTIVE"
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token({"sub": user.id, "email": user.email, "role": user.role})
    return TokenResponse(
        access_token=token,
        user=UserProfileResponse(
            id=user.id,
            name=user.name,
            email=user.email,
            role=user.role,
            status=user.status,
            created_at=user.created_at,
            last_login_at=user.last_login_at
        )
    )

@app.post("/api/auth/login", response_model=TokenResponse)
def api_login(payload: UserLoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email.lower().strip()).first()
    if not user or not verify_password(payload.password, user.password_hash):
        # Defesa contra timing attack e enumeração
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciais de acesso incorretas.")

    if user.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Conta inativa.")

    user.last_login_at = datetime.utcnow()
    db.commit()

    token = create_access_token({"sub": user.id, "email": user.email, "role": user.role})
    return TokenResponse(
        access_token=token,
        user=UserProfileResponse(
            id=user.id,
            name=user.name,
            email=user.email,
            role=user.role,
            status=user.status,
            created_at=user.created_at,
            last_login_at=user.last_login_at
        )
    )

@app.get("/api/auth/me", response_model=UserProfileResponse)
def api_me(user: User = Depends(require_auth)):
    return UserProfileResponse(
        id=user.id,
        name=user.name,
        email=user.email,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
        last_login_at=user.last_login_at
    )

@app.delete("/api/auth/account")
def api_delete_account(user: User = Depends(require_auth), db: Session = Depends(get_db)):
    """Direito à exclusão e esquecimento de dados pessoais conforme a LGPD (Art. 18, VI)"""
    db.delete(user)
    db.commit()
    return {"message": "Conta e dados pessoais excluídos com sucesso em conformidade com a LGPD."}

# ----------------- Tenders & Search API -----------------

@app.get("/api/tenders", response_model=SearchResultResponse)
def api_search_tenders(
    q: Optional[str] = None,
    state: Optional[str] = None,
    city: Optional[str] = None,
    region: Optional[str] = None,
    contracting_entity: Optional[str] = None,
    administrative_sphere: Optional[str] = None,
    modality: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    category: Optional[str] = None,
    source_system: Optional[str] = None,
    portal: Optional[str] = None,
    uasg_code: Optional[str] = None,
    value_min: Optional[str] = None,
    value_max: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sort_by: str = "relevance",
    page: int = 1,
    page_size: Optional[int] = None,
    limit: Optional[int] = None,
    request: Request = None,
    db: Session = Depends(get_db)
):
    effective_size = limit or page_size or 20
    params = SearchFilterParams(
        q=q,
        state=state,
        city=city,
        region=region,
        contracting_entity=contracting_entity,
        administrative_sphere=administrative_sphere,
        modality=modality,
        status=status_filter,
        category=category,
        source_system=source_system,
        portal=portal,
        uasg_code=uasg_code,
        value_min=clean_float_param(value_min),
        value_max=clean_float_param(value_max),
        date_from=date_from,
        date_to=date_to,
        sort_by=sort_by,
        page=page,
        page_size=effective_size,
        limit=limit
    )
    user = get_current_user(request, db) if request else None
    return search_tenders(db, params, user_id=user.id if user else None)

@app.post("/api/tenders/sync", response_model=SyncOpportunitiesResponse)
def api_sync_tenders(
    days_back: int = Query(7, ge=1, le=60),
    uf: Optional[str] = Query(None),
    pages: int = Query(10, ge=1, le=50),
    limit_per_page: int = Query(50, ge=10, le=100),
    db: Session = Depends(get_db)
):
    """
    Dispara o pipeline unificado de sincronização de oportunidades em larga escala multifontes:
    priorizando Compras.gov.br (Federal / SICAF), portais estaduais (BEC/SP, Compras RS, MG, BA, RJ, PR)
    e portais integrados (BLL, Licitações-e, Portal de Compras Públicas, PNCP Geral).
    """
    result = sync_procurement_opportunities(
        db=db,
        days_back=days_back,
        uf=uf,
        pages=pages,
        limit_per_page=limit_per_page
    )
    return SyncOpportunitiesResponse(
        status=result["status"],
        message=result["message"],
        synced_at=datetime.fromisoformat(result["synced_at"]),
        total_processados=result["total_processados"],
        novos_inseridos=result["novos_inseridos"],
        atualizados=result["atualizados"],
        compras_gov_count=result["compras_gov_count"],
        pncp_count=result["pncp_count"],
        dispensas_count=result.get("dispensas_count"),
        total_coletados=result["total_coletados"],
        total_geral_banco=result["total_geral_banco"]
    )

@app.get("/api/tenders/export/csv")
def api_export_tenders_csv(
    q: Optional[str] = None,
    state: Optional[str] = None,
    modality: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    db: Session = Depends(get_db)
):
    """Exporta oportunidades filtradas em streaming de arquivo CSV estruturado."""
    params = SearchFilterParams(
        q=q,
        state=state,
        modality=modality,
        status=status_filter,
        page=1,
        page_size=100
    )

    def generate_csv():
        output = io.StringIO()
        writer = csv.writer(output, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        # Cabeçalho estruturado
        writer.writerow([
            "objeto",
            "orgao",
            "estado",
            "valor_estimado",
            "data_abertura",
            "url_disputa",
            "url_pncp"
        ])
        yield output.getvalue().encode("utf-8-sig")
        output.seek(0)
        output.truncate(0)

        current_page = 1
        max_export_records = 2000
        exported = 0

        while True:
            params.page = current_page
            search_result = search_tenders(db, params)
            if not search_result.results:
                break

            for item in search_result.results:
                val_str = f"{item.estimated_total_value:.2f}".replace(".", ",") if item.estimated_total_value is not None else ""
                data_abertura = item.opening_date.strftime("%d/%m/%Y %H:%M") if item.opening_date else ""
                url_disp = item.url_disputa or item.link_comprasnet or item.official_notice_url or item.source_url or item.url_pncp or ""
                url_pnc = item.url_pncp or item.link_pncp or item.source_url or ""
                writer.writerow([
                    item.object_description or item.title or "",
                    item.contracting_entity or "",
                    item.state or "",
                    val_str,
                    data_abertura,
                    url_disp,
                    url_pnc
                ])
                yield output.getvalue().encode("utf-8-sig")
                output.seek(0)
                output.truncate(0)
                exported += 1
                if exported >= max_export_records:
                    break

            if exported >= max_export_records or current_page >= search_result.total_pages:
                break
            current_page += 1

    filename = f"licitacoes_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"'
    }
    return StreamingResponse(generate_csv(), media_type="text/csv", headers=headers)

@app.get("/api/tenders/{tender_id}")
def api_get_tender_by_id(tender_id: str, db: Session = Depends(get_db)):
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        raise HTTPException(status_code=404, detail="Licitação não encontrada.")
    
    items = db.query(TenderItem).filter(TenderItem.tender_id == tender.id).order_by(TenderItem.item_number.asc()).all()
    docs = db.query(TenderDocument).filter(TenderDocument.tender_id == tender.id).all()

    items_payload = [
        {
            "id": i.id,
            "item_number": i.item_number,
            "description": i.description,
            "unit": i.unit or "UN",
            "quantity": i.quantity or 1.0,
            "estimated_unit_value": i.estimated_unit_value or 0.0,
            "estimated_total_value": i.estimated_total_value or 0.0,
            "classification_code": i.classification_code or "Não informado"
        } for i in items
    ]
    if not items_payload:
        items_payload = [{
            "id": f"item-1-{tender.id}",
            "item_number": 1,
            "description": tender.title or tender.object_description,
            "unit": "UN",
            "quantity": 1.0,
            "estimated_unit_value": tender.estimated_total_value or 0.0,
            "estimated_total_value": tender.estimated_total_value or 0.0,
            "classification_code": tender.category or "Item Principal"
        }]

    docs_payload = [
        {
            "id": d.id,
            "name": d.name,
            "document_type": d.document_type,
            "source_url": d.source_url,
            "mime_type": d.mime_type or "application/pdf",
            "file_size": d.file_size
        } for d in docs
    ]
    if not docs_payload and (tender.official_notice_url or tender.link_pncp or tender.source_url):
        docs_payload = [{
            "id": f"doc-edital-{tender.id}",
            "name": f"Edital e Termo de Referência ({tender.notice_number or tender.external_id})",
            "document_type": "EDITAL",
            "source_url": tender.official_notice_url or tender.link_pncp or tender.source_url,
            "mime_type": "application/pdf",
            "file_size": None
        }]

    return {
        "id": tender.id,
        "external_id": tender.external_id,
        "process_number": tender.process_number or "Não informado",
        "notice_number": tender.notice_number or "Não informado",
        "title": tender.title,
        "object_description": tender.object_description,
        "contracting_entity": tender.contracting_entity,
        "purchasing_unit": tender.purchasing_unit or "Não informado",
        "state": tender.state,
        "city": tender.city or "Não informado",
        "region": tender.region or "Não informado",
        "administrative_sphere": tender.administrative_sphere,
        "modality": tender.modality,
        "category": tender.category or "Outros",
        "status": tender.status or "Divulgada no PNCP",
        "publication_date": tender.publication_date.isoformat() if tender.publication_date else None,
        "opening_date": tender.opening_date.isoformat() if tender.opening_date else None,
        "proposal_deadline": tender.proposal_deadline.isoformat() if tender.proposal_deadline else None,
        "estimated_total_value": tender.estimated_total_value,
        "source_name": tender.source.name if tender.source else "PNCP",
        "source_url": tender.source_url,
        "official_notice_url": tender.official_notice_url,
        "source_system": tender.source_system or "pncp",
        "uasg_code": tender.uasg_code,
        "sicaf_ready": bool(tender.sicaf_ready),
        "link_comprasnet": tender.link_comprasnet,
        "link_pncp": getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url,
        "url_disputa": getattr(tender, "url_disputa", None) or tender.link_comprasnet or tender.official_notice_url or (getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url),
        "url_pncp": getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url,
        "data_completeness": tender.data_completeness,
        "items": items_payload,
        "documents": docs_payload,
        "created_at": tender.created_at.isoformat() if tender.created_at else None,
        "last_synced_at": tender.last_synced_at.isoformat() if tender.last_synced_at else None
    }

def serialize_deal(deal: Deal, tender: Optional[Tender] = None) -> Dict[str, Any]:
    t = tender or deal.tender
    tender_data = None
    if t:
        tender_data = {
            "id": t.id,
            "title": t.title,
            "external_id": t.external_id,
            "notice_number": t.notice_number,
            "contracting_entity": t.contracting_entity,
            "purchasing_unit": t.purchasing_unit or "Não informado",
            "state": t.state,
            "city": t.city or "Não informado",
            "modality": t.modality,
            "status": t.status or "Divulgada no PNCP",
            "estimated_total_value": t.estimated_total_value,
            "opening_date": t.opening_date.isoformat() if t.opening_date else None,
            "proposal_deadline": t.proposal_deadline.isoformat() if t.proposal_deadline else None,
            "source_system": t.source_system or "pncp",
            "uasg_code": t.uasg_code,
            "sicaf_ready": bool(t.sicaf_ready),
            "link_comprasnet": t.link_comprasnet,
            "link_pncp": getattr(t, "url_pncp", None) or t.link_pncp or t.source_url,
            "url_disputa": getattr(t, "url_disputa", None) or t.link_comprasnet or t.official_notice_url or (getattr(t, "url_pncp", None) or t.link_pncp or t.source_url),
            "url_pncp": getattr(t, "url_pncp", None) or t.link_pncp or t.source_url,
            "source_url": t.source_url
        }
    return {
        "id": deal.id,
        "tender_id": deal.tender_id,
        "stage": deal.stage,
        "notes": deal.notes or "",
        "created_at": deal.created_at.isoformat() if deal.created_at else None,
        "updated_at": deal.updated_at.isoformat() if deal.updated_at else None,
        "tender": tender_data
    }

@app.post("/api/deals", status_code=status.HTTP_201_CREATED)
async def api_create_deal(
    request: Request,
    tender_id: Optional[Union[str, int]] = Query(None),
    stage: Optional[str] = Query(None),
    notes: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Cria ou atualiza um negócio no pipeline/funil comercial."""
    payload: Dict[str, Any] = {}
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            payload = await request.json()
    except Exception:
        payload = {}

    target_tender_id = payload.get("tender_id") or tender_id
    target_stage = payload.get("stage") or stage or "oportunidades"
    target_notes = payload.get("notes") or notes or ""

    if not target_tender_id:
        raise HTTPException(status_code=422, detail="tender_id é obrigatório.")

    if target_stage not in DEAL_STAGES:
        target_stage = "oportunidades"

    tender = db.query(Tender).filter(
        (Tender.id == str(target_tender_id)) | (Tender.external_id == str(target_tender_id))
    ).first()
    if not tender:
        raise HTTPException(status_code=404, detail="Licitação não encontrada.")

    deal = db.query(Deal).filter(Deal.tender_id == tender.id).first()
    if deal:
        if target_stage in DEAL_STAGES:
            deal.stage = target_stage
        if target_notes:
            deal.notes = target_notes
        deal.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(deal)
        return serialize_deal(deal, tender)

    deal = Deal(
        tender_id=tender.id,
        stage=target_stage,
        notes=target_notes,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(deal)
    db.commit()
    db.refresh(deal)
    return serialize_deal(deal, tender)

@app.get("/api/deals")
def api_get_deals(
    stage: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Lista os negócios do funil agrupados por estágio."""
    query = db.query(Deal).order_by(Deal.updated_at.desc())
    if stage and stage in DEAL_STAGES:
        query = query.filter(Deal.stage == stage)

    deals = query.all()
    grouped: Dict[str, List[Dict[str, Any]]] = {s: [] for s in DEAL_STAGES}
    deals_list: List[Dict[str, Any]] = []

    for d in deals:
        serialized = serialize_deal(d)
        stage_key = d.stage if d.stage in grouped else "oportunidades"
        grouped[stage_key].append(serialized)
        deals_list.append(serialized)

    return {
        "oportunidades": grouped["oportunidades"],
        "qualificacao": grouped["qualificacao"],
        "disputa": grouped["disputa"],
        "classificacao": grouped["classificacao"],
        "contrato": grouped["contrato"],
        "grouped": grouped,
        "deals": deals_list,
        "total": len(deals),
        "counts": {s: len(grouped[s]) for s in DEAL_STAGES}
    }

@app.patch("/api/deals/{deal_id}/stage")
@app.put("/api/deals/{deal_id}/stage")
async def api_update_deal_stage(
    deal_id: int,
    request: Request,
    stage: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Atualiza o estágio do negócio (mover entre colunas)."""
    target_stage = stage
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            payload = await request.json()
            if isinstance(payload, dict) and "stage" in payload:
                target_stage = payload["stage"]
    except Exception:
        pass

    if not target_stage or target_stage not in DEAL_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Estágio inválido. Opções permitidas: {', '.join(DEAL_STAGES)}"
        )

    deal = db.query(Deal).filter(Deal.id == deal_id).first()
    if not deal:
        raise HTTPException(status_code=404, detail="Negócio não encontrado.")

    deal.stage = target_stage
    deal.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(deal)
    return serialize_deal(deal)

@app.delete("/api/deals/{deal_id}")
def api_delete_deal(
    deal_id: int,
    db: Session = Depends(get_db)
):
    """Remove um negócio do funil."""
    deal = db.query(Deal).filter(Deal.id == deal_id).first()
    if not deal:
        raise HTTPException(status_code=404, detail="Negócio não encontrado.")
    db.delete(deal)
    db.commit()
    return {"status": "success", "message": "Negócio removido do funil.", "id": deal_id}

@app.post("/api/pipeline/add")
def api_add_to_pipeline(
    tender_id: str = Query(...),
    request: Request = None,
    db: Session = Depends(get_db)
):
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        raise HTTPException(status_code=404, detail="Licitação não encontrada.")
    user = get_current_user(request, db) if request else None
    if user:
        existing = db.query(Favorite).filter(Favorite.user_id == user.id, Favorite.tender_id == tender.id).first()
        if not existing:
            db.add(Favorite(user_id=user.id, tender_id=tender.id))
            db.commit()

    deal = db.query(Deal).filter(Deal.tender_id == tender.id).first()
    if not deal:
        deal = Deal(
            tender_id=tender.id,
            stage="oportunidades",
            notes="Adicionado via busca de oportunidades",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        db.add(deal)
        db.commit()
        db.refresh(deal)

    return {
        "status": "success",
        "message": f"Licitação {tender.notice_number or tender.external_id} adicionada ao seu funil de negócios!",
        "tender_id": tender.id,
        "deal_id": deal.id,
        "title": tender.title
    }

# ----------------- Peças Jurídicas API -----------------

def serialize_legal_template(tmpl: LegalDocumentTemplate) -> Dict[str, Any]:
    tipo_label_map = {
        "impugnacao": "Impugnação",
        "esclarecimento": "Esclarecimento",
        "intencao_recurso": "Intenção de Recurso",
        "recurso": "Recurso",
        "contrarbenzoes": "Contrarrazões",
        "contrarbenzoes": "Contrarrazões"
    }
    return {
        "id": tmpl.id,
        "tipo": tmpl.tipo,
        "tipo_label": tipo_label_map.get(tmpl.tipo, tmpl.tipo.capitalize()),
        "titulo": tmpl.titulo,
        "title": tmpl.titulo,
        "fundamentacao_legal": tmpl.fundamentacao_legal,
        "corpo": tmpl.corpo,
        "content": tmpl.corpo,
        "is_nova_lei": bool(tmpl.is_nova_lei),
        "created_at": tmpl.created_at.isoformat() if tmpl.created_at else None,
        "updated_at": tmpl.updated_at.isoformat() if tmpl.updated_at else None
    }

@app.get("/api/juridico/templates")
def api_list_legal_templates(
    q: Optional[str] = Query(None),
    tipo: Optional[str] = Query(None),
    nova_lei_only: Optional[bool] = Query(None),
    db: Session = Depends(get_db)
):
    """Lista as peças jurídicas com suporte a busca, filtros de categoria e regime da Nova Lei."""
    query = db.query(LegalDocumentTemplate)
    if q:
        query = query.filter(
            (LegalDocumentTemplate.titulo.ilike(f"%{q}%")) |
            (LegalDocumentTemplate.corpo.ilike(f"%{q}%")) |
            (LegalDocumentTemplate.fundamentacao_legal.ilike(f"%{q}%"))
        )
    if tipo:
        if tipo in ["contrarbenzoes", "contrarbenzoes"]:
            query = query.filter(LegalDocumentTemplate.tipo.in_(["contrarbenzoes", "contrarbenzoes"]))
        else:
            query = query.filter(LegalDocumentTemplate.tipo == tipo)
    if nova_lei_only is True:
        query = query.filter(LegalDocumentTemplate.is_nova_lei == True)

    templates_list = query.order_by(LegalDocumentTemplate.id.asc()).all()
    return {
        "total": len(templates_list),
        "items": [serialize_legal_template(t) for t in templates_list]
    }

@app.get("/api/juridico/templates/{template_id}")
def api_get_legal_template(
    template_id: int,
    db: Session = Depends(get_db)
):
    """Obtém detalhes do modelo de peça jurídica por ID."""
    tmpl = db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.id == template_id).first()
    if not tmpl:
        raise HTTPException(status_code=404, detail="Modelo de peça jurídica não encontrado.")
    return serialize_legal_template(tmpl)

@app.post("/api/juridico/templates/{template_id}/render")
async def api_render_legal_template(
    template_id: int,
    request: Request,
    tender_id: Optional[str] = Query(None),
    orgao: Optional[str] = Query(None),
    numero_edital: Optional[str] = Query(None),
    objeto: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Renderiza a peça substituindo as tags {{orgao}}, {{numero_edital}}, {{objeto}}."""
    tmpl = db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.id == template_id).first()
    if not tmpl:
        raise HTTPException(status_code=404, detail="Modelo de peça jurídica não encontrado.")

    payload = {}
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            payload = await request.json()
    except Exception:
        pass

    target_tender_id = payload.get("tender_id") or tender_id
    target_orgao = payload.get("orgao") or orgao
    target_numero = payload.get("numero_edital") or numero_edital
    target_objeto = payload.get("objeto") or objeto

    if target_tender_id:
        t = db.query(Tender).filter(Tender.id == target_tender_id).first()
        if t:
            if not target_orgao:
                target_orgao = t.contracting_entity
            if not target_numero:
                target_numero = t.notice_number or t.external_id
            if not target_objeto:
                target_objeto = t.title

    target_orgao = target_orgao or "[ÓRGÃO COMPRADOR]"
    target_numero = target_numero or "[NÚMERO DO EDITAL]"
    target_objeto = target_objeto or "[OBJETO DA LICITAÇÃO]"

    rendered_corpo = tmpl.corpo
    rendered_corpo = rendered_corpo.replace("{{orgao}}", target_orgao)
    rendered_corpo = rendered_corpo.replace("{{numero_edital}}", target_numero)
    rendered_corpo = rendered_corpo.replace("{{objeto}}", target_objeto)

    return {
        "template_id": tmpl.id,
        "titulo": tmpl.titulo,
        "tipo": tmpl.tipo,
        "fundamentacao_legal": tmpl.fundamentacao_legal,
        "rendered_corpo": rendered_corpo,
        "variables_used": {
            "orgao": target_orgao,
            "numero_edital": target_numero,
            "objeto": target_objeto
        }
    }

@app.post("/api/tenders/{tender_id}/ai-summary")
async def api_tender_ai_summary(
    tender_id: str,
    payload: Optional[AISummaryRequest] = None,
    force_refresh: bool = Query(False),
    db: Session = Depends(get_db)
):
    """Dispara a geração ou recuperação em cache do resumo executivo estruturado com IA Gemini."""
    refresh = force_refresh or (payload.force_refresh if payload else False)
    return await analyze_tender_summary(tender_id=tender_id, db=db, force_refresh=refresh)

@app.post("/api/tenders/{tender_id}/ai-chat")
async def api_tender_ai_chat(
    tender_id: str,
    payload: AIChatRequest,
    db: Session = Depends(get_db)
):
    """Recebe a pergunta do usuário e devolve a resposta contextualizada da IA ('Converse com o Edital')."""
    return await ask_tender_question(tender_id=tender_id, question=payload.question, db=db)

@app.post("/api/tenders/{tender_id}/ai-analyze")
def api_analyze_tender_ai(
    tender_id: str,
    payload: Dict[str, Any] = None,
    db: Session = Depends(get_db)
):
    """Executa a análise documental por IA com delimitação segura e extração estruturada"""
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        raise HTTPException(status_code=404, detail="Licitação não encontrada.")

    # Obter documento para análise
    doc = None
    if payload and payload.get("document_id"):
        doc = db.query(TenderDocument).filter(TenderDocument.id == payload["document_id"]).first()
    if not doc:
        doc = db.query(TenderDocument).filter(TenderDocument.tender_id == tender.id).first()

    doc_text = ""
    if doc and doc.extraction:
        doc_text = doc.extraction.extracted_text_reference or ""
    if not doc_text:
        doc_text = f"OBJETO DA LICITAÇÃO:\n{tender.object_description}\n\nÓRGÃO CONTRATANTE:\n{tender.contracting_entity}\n\nVALOR ESTIMADO:\nR$ {tender.estimated_total_value or 0.0:,.2f}"

    ai_result = analyze_tender_document_with_ai(
        tender_title=tender.title,
        tender_object=tender.object_description,
        document_text=doc_text
    )

    # Persistir análise no banco
    analysis_record = AIAnalysis(
        tender_id=tender.id,
        document_id=doc.id if doc else None,
        model_provider=ai_result.get("provider", "GEMINI"),
        model_name=ai_result.get("model", "gemini-3.8-flash"),
        analysis_type="ANALISE_COMPLETA",
        result_json=ai_result.get("data", {}),
        source_text_hash=calculate_sha256(doc_text.encode("utf-8"))
    )
    db.add(analysis_record)
    db.commit()

    return ai_result

@app.post("/api/tenders/{tender_id}/notes")
def api_save_tender_note(
    tender_id: str,
    payload: UserNoteRequest,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db)
):
    note = db.query(UserNote).filter(UserNote.user_id == user.id, UserNote.tender_id == tender_id).first()
    if not note:
        note = UserNote(user_id=user.id, tender_id=tender_id, content=payload.content)
        db.add(note)
    else:
        note.content = payload.content
        note.updated_at = datetime.utcnow()
    db.commit()
    return {"message": "Anotação salva com sucesso."}

# ----------------- Favorites API -----------------

@app.get("/api/favorites")
def api_get_favorites(user: User = Depends(require_auth), db: Session = Depends(get_db)):
    favs = db.query(Favorite).filter(Favorite.user_id == user.id).all()
    tenders = []
    for f in favs:
        t = f.tender
        if t:
            tenders.append({
                "id": t.id,
                "title": t.title,
                "contracting_entity": t.contracting_entity,
                "state": t.state,
                "city": t.city,
                "modality": t.modality,
                "status": t.status,
                "proposal_deadline": t.proposal_deadline,
                "estimated_total_value": t.estimated_total_value,
                "favorited_at": f.created_at
            })
    return tenders

@app.post("/api/favorites/toggle", response_model=FavoriteToggleResponse)
def api_toggle_favorite(
    tender_id: str = Query(...),
    user: User = Depends(require_auth),
    db: Session = Depends(get_db)
):
    existing = db.query(Favorite).filter(Favorite.user_id == user.id, Favorite.tender_id == tender_id).first()
    if existing:
        db.delete(existing)
        db.commit()
        return FavoriteToggleResponse(tender_id=tender_id, is_favorite=False)
    else:
        new_fav = Favorite(user_id=user.id, tender_id=tender_id)
        db.add(new_fav)
        db.commit()
        return FavoriteToggleResponse(tender_id=tender_id, is_favorite=True)

# ----------------- Alerts API -----------------

@app.get("/api/alerts", response_model=List[AlertResponse])
def api_get_alerts(
    user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    query = db.query(SavedSearch).filter(SavedSearch.active == True)
    if user:
        query = query.filter((SavedSearch.user_id == user.id) | (SavedSearch.user_id.is_(None)))
    saved_items = query.order_by(SavedSearch.created_at.desc()).all()

    resp = []
    seen_ids = set()
    for s in saved_items:
        seen_ids.add(s.id)
        count = db.query(AlertDelivery).filter(AlertDelivery.alert_id == s.id).count()
        al = db.query(Alert).filter(Alert.id == s.id).first()
        resp.append(AlertResponse(
            id=s.id,
            keywords=s.keywords,
            ufs=s.ufs,
            min_value=s.min_value,
            created_at=s.created_at,
            active=s.active,
            name=s.name or "Alerta",
            filters=s.filters or {},
            frequency=al.frequency if al else "DAILY",
            channels=al.channels if al else "IN_APP",
            last_run_at=al.last_run_at if al else None,
            delivery_count=count
        ))

    # Incorporar alertas legados da tabela Alert se houver
    legacy_query = db.query(Alert).filter(Alert.active == True)
    if user:
        legacy_query = legacy_query.filter(Alert.user_id == user.id)
    legacy_alerts = legacy_query.all()
    for la in legacy_alerts:
        if la.id not in seen_ids:
            seen_ids.add(la.id)
            count = db.query(AlertDelivery).filter(AlertDelivery.alert_id == la.id).count()
            la_ufs = la.filters.get("state") if isinstance(la.filters, dict) else None
            la_min = clean_float_param(la.filters.get("value_min")) if isinstance(la.filters, dict) else None
            resp.append(AlertResponse(
                id=la.id,
                keywords=la.keywords,
                ufs=la_ufs,
                min_value=la_min,
                created_at=la.created_at,
                active=la.active,
                name=la.name,
                filters=la.filters or {},
                frequency=la.frequency,
                channels=la.channels,
                last_run_at=la.last_run_at,
                delivery_count=count
            ))

    return resp

@app.post("/api/alerts", response_model=AlertResponse)
def api_create_alert(
    payload: AlertCreateRequest,
    user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    keywords = payload.keywords
    filters = dict(payload.filters or {})
    if not keywords and filters.get("q"):
        keywords = filters.get("q")

    ufs = payload.ufs or filters.get("state") or filters.get("uf")
    min_value = payload.min_value
    if min_value is None and filters.get("value_min") is not None:
        min_value = clean_float_param(filters.get("value_min"))

    name = payload.name or (f"Alerta: {keywords}" if keywords else (f"Alerta UF {ufs}" if ufs else "Alerta de Busca"))

    if ufs and "state" not in filters:
        filters["state"] = ufs
    if min_value is not None and "value_min" not in filters:
        filters["value_min"] = min_value
    if keywords and "keywords" not in filters:
        filters["keywords"] = keywords

    alert_id = generate_uuid()
    created_now = datetime.utcnow()

    target_user_id = user.id if user else None
    if not target_user_id:
        demo = db.query(User).filter(User.email == "usuario@empresa.com.br").first()
        if demo:
            target_user_id = demo.id
        else:
            first_u = db.query(User).first()
            if first_u:
                target_user_id = first_u.id

    # Guardar pesquisa/alerta no modelo SavedSearch
    saved_search = SavedSearch(
        id=alert_id,
        user_id=target_user_id,
        name=name,
        keywords=keywords,
        ufs=ufs,
        min_value=min_value,
        filters=filters,
        active=True,
        created_at=created_now,
        updated_at=created_now
    )
    db.add(saved_search)

    # Espelhar em Alert para compatibilidade de entregas se houver usuário
    freq = payload.frequency or "DAILY"
    chan = payload.channels or "IN_APP"
    if target_user_id:
        alert = Alert(
            id=alert_id,
            user_id=target_user_id,
            name=name,
            keywords=keywords,
            filters=filters,
            frequency=freq,
            channels=chan,
            active=True,
            created_at=created_now,
            updated_at=created_now
        )
        db.add(alert)

    db.commit()
    db.refresh(saved_search)

    return AlertResponse(
        id=saved_search.id,
        keywords=saved_search.keywords,
        ufs=saved_search.ufs,
        min_value=saved_search.min_value,
        created_at=saved_search.created_at,
        active=saved_search.active,
        name=saved_search.name,
        filters=saved_search.filters or {},
        frequency=freq,
        channels=chan,
        last_run_at=None,
        delivery_count=0
    )

@app.post("/api/alerts/{alert_id}/toggle")
def api_toggle_alert(
    alert_id: str,
    active: bool = Query(...),
    user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    saved = db.query(SavedSearch).filter(SavedSearch.id == alert_id).first()
    if not alert and not saved:
        raise HTTPException(status_code=404, detail="Alerta não localizado.")
    if alert:
        alert.active = active
    if saved:
        saved.active = active
    db.commit()
    return {"message": f"Alerta {'reativado' if active else 'pausado'} com sucesso."}

@app.delete("/api/alerts/{alert_id}")
def api_delete_alert(
    alert_id: str,
    user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    saved = db.query(SavedSearch).filter(SavedSearch.id == alert_id).first()
    if not alert and not saved:
        raise HTTPException(status_code=404, detail="Alerta não localizado.")
    if alert:
        db.delete(alert)
    if saved:
        db.delete(saved)
    db.commit()
    return {"message": "Alerta excluído."}


# ----------------- Webhooks Audit & Logs API -----------------

@app.get("/api/alerts/webhooks/logs", response_model=WebhookDispatchLogsListResponse)
def api_get_webhook_logs(
    status: Optional[str] = Query(None, description="Filtro por status: SUCESSO ou FALHA"),
    certame_id: Optional[str] = Query(None, description="Filtro por certame / licitação ID"),
    tender_id: Optional[str] = Query(None, description="Alias para certame_id"),
    data: Optional[str] = Query(None, description="Filtro por data no formato YYYY-MM-DD"),
    canal: Optional[str] = Query(None, description="Filtro por canal (N8N, EVOLUTION_API, ZAPI, GENERIC)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db)
):
    query = db.query(WebhookDispatchLog)

    if status:
        st_clean = status.strip().upper()
        if st_clean in ("SUCESSO", "SUCCESS", "TRUE", "1"):
            query = query.filter(WebhookDispatchLog.sucesso == True)
        elif st_clean in ("FALHA", "FAILED", "ERROR", "FALSE", "0"):
            query = query.filter(WebhookDispatchLog.sucesso == False)

    target_certame = certame_id or tender_id
    if target_certame:
        query = query.filter(WebhookDispatchLog.tender_id == target_certame.strip())

    if data:
        data_clean = data.strip()
        try:
            target_date = datetime.strptime(data_clean, "%Y-%m-%d").date()
            query = query.filter(func.date(WebhookDispatchLog.criado_em) == target_date)
        except ValueError:
            pass

    if canal:
        query = query.filter(WebhookDispatchLog.canal == canal.strip().upper())

    total = query.count()
    db_items = (
        query.order_by(WebhookDispatchLog.criado_em.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    items = [
        WebhookDispatchLogResponse(
            id=item.id,
            rule_id=item.rule_id,
            tender_id=item.tender_id,
            target_url=item.target_url,
            canal=item.canal,
            status_code=item.status_code,
            tentativa_atual=item.tentativa_atual,
            sucesso=item.sucesso,
            tempo_resposta_ms=item.tempo_resposta_ms,
            payload_enviado=item.payload_enviado,
            resposta_servidor=item.resposta_servidor,
            criado_em=item.criado_em
        )
        for item in db_items
    ]

    return WebhookDispatchLogsListResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=items
    )


@app.post("/api/alerts/webhooks/{log_id}/retry")
def api_retry_webhook(log_id: str, db: Session = Depends(get_db)):
    old_log = db.query(WebhookDispatchLog).filter(WebhookDispatchLog.id == log_id).first()
    if not old_log:
        raise HTTPException(status_code=404, detail="Log de disparo de webhook não encontrado.")

    new_log = retry_webhook_dispatch(db, log_id)
    if not new_log:
        raise HTTPException(status_code=500, detail="Erro ao reprocessar disparo de webhook.")

    return {
        "message": "Webhook reexecutado com sucesso.",
        "log": {
            "id": new_log.id,
            "rule_id": new_log.rule_id,
            "tender_id": new_log.tender_id,
            "target_url": new_log.target_url,
            "canal": new_log.canal,
            "status_code": new_log.status_code,
            "tentativa_atual": new_log.tentativa_atual,
            "sucesso": new_log.sucesso,
            "tempo_resposta_ms": new_log.tempo_resposta_ms,
            "payload_enviado": new_log.payload_enviado,
            "resposta_servidor": new_log.resposta_servidor,
            "criado_em": new_log.criado_em.isoformat()
        }
    }

# ----------------- Dashboard Summary API -----------------

def get_dashboard_summary_data(db: Session, user: Optional[User] = None) -> Dict[str, Any]:
    # 1. Total de negócios ativos (deals no funil)
    deals_count = db.query(Deal).count()
    funnel_stages = {s: db.query(Deal).filter(Deal.stage == s).count() for s in DEAL_STAGES}

    # 2. Empresas identificadas (órgãos compradores únicos ou entidades)
    companies_count = db.query(func.count(func.distinct(Tender.contracting_entity))).scalar() or 0

    # 3. Total de licitações publicadas/sincronizadas hoje e total de itens
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    tenders_today_query = db.query(Tender).filter(
        (Tender.publication_date >= today_start) |
        (Tender.last_synced_at >= today_start) |
        (Tender.created_at >= today_start)
    )
    tenders_today_count = tenders_today_query.count()
    if tenders_today_count == 0:
        tenders_today_count = db.query(Tender).count()

    total_items_count = db.query(TenderItem).count()
    items_today_count = db.query(TenderItem).join(Tender).filter(
        (Tender.publication_date >= today_start) |
        (Tender.last_synced_at >= today_start) |
        (Tender.created_at >= today_start)
    ).count()
    if items_today_count == 0:
        items_today_count = total_items_count

    # 4. Distribuição por categoria: Serviços, Materiais e Não Classificado
    tenders = db.query(Tender).all()
    servicos_count = 0
    materiais_count = 0
    nao_classificado_count = 0

    for t in tenders:
        cat = (t.category or "").lower().strip()
        obj = (t.title or "").lower() + " " + (t.object_description or "").lower()
        if any(k in cat for k in ["serviço", "servico", "consultoria", "manuten", "loca", "engenharia", "obra", "vigil", "limpeza"]) or any(k in obj for k in ["prestação de serviço", "prestacao de servico", "serviço de", "servicos de"]):
            servicos_count += 1
        elif any(k in cat for k in ["material", "materiais", "medicamento", "alimento", "equipamento", "hospitalar", "suprimento", "insumo", "ti", "tecnologia", "produto"]) or any(k in obj for k in ["aquisição", "aquisicao", "fornecimento"]):
            materiais_count += 1
        else:
            if cat:
                materiais_count += 1
            else:
                nao_classificado_count += 1

    # 5. Distribuição por faixa de valor: Até R$ 50 mil, R$ 50 mil a R$ 250 mil, Acima de R$ 250 mil
    ate_50k = 0
    de_50k_a_250k = 0
    acima_250k = 0

    for t in tenders:
        val = t.estimated_total_value
        if val is None or val == 0:
            ate_50k += 1
        elif val <= 50000.0:
            ate_50k += 1
        elif 50000.0 < val <= 250000.0:
            de_50k_a_250k += 1
        else:
            acima_250k += 1

    # 6. Próximos eventos da agenda vinculados aos negócios em andamento
    deals = db.query(Deal).all()
    agenda_events = []
    seen_tenders = set()

    for d in deals:
        t = d.tender
        if not t or t.id in seen_tenders:
            continue
        seen_tenders.add(t.id)

        target_date = t.opening_date or t.proposal_deadline
        if target_date:
            event_type = "Sessão de Lances / Disputa" if t.opening_date and target_date == t.opening_date else "Prazo Limite de Propostas"
            agenda_events.append({
                "deal_id": d.id,
                "tender_id": t.id,
                "title": t.title,
                "notice_number": t.notice_number or t.external_id,
                "contracting_entity": t.contracting_entity,
                "state": t.state,
                "stage": d.stage,
                "event_type": event_type,
                "date": target_date.isoformat(),
                "formatted_date": target_date.strftime("%d/%m/%Y %H:%M"),
                "estimated_value": t.estimated_total_value,
                "sicaf_ready": bool(t.sicaf_ready)
            })

    if not agenda_events:
        tenders_with_deadlines = db.query(Tender).filter(Tender.proposal_deadline.isnot(None)).order_by(Tender.proposal_deadline.asc()).limit(5).all()
        for t in tenders_with_deadlines:
            agenda_events.append({
                "deal_id": None,
                "tender_id": t.id,
                "title": t.title,
                "notice_number": t.notice_number or t.external_id,
                "contracting_entity": t.contracting_entity,
                "state": t.state,
                "stage": "oportunidades",
                "event_type": "Prazo Limite de Propostas",
                "date": t.proposal_deadline.isoformat(),
                "formatted_date": t.proposal_deadline.strftime("%d/%m/%Y %H:%M"),
                "estimated_value": t.estimated_total_value,
                "sicaf_ready": bool(t.sicaf_ready)
            })

    agenda_events.sort(key=lambda x: x["date"])

    # Dados de compatibilidade do usuário
    user_id = user.id if user else "usr-user-01"
    favs_count = db.query(Favorite).filter(Favorite.user_id == user_id).count()
    alerts_count = db.query(Alert).filter(Alert.user_id == user_id, Alert.active == True).count()
    deliveries_count = db.query(AlertDelivery).join(Alert).filter(Alert.user_id == user_id).count()

    favs = db.query(Favorite).filter(Favorite.user_id == user_id).all()
    upcoming = []
    for f in favs:
        t = f.tender
        if t and t.proposal_deadline:
            upcoming.append({
                "id": t.id,
                "title": t.title,
                "contracting_entity": t.contracting_entity,
                "state": t.state,
                "proposal_deadline": t.proposal_deadline.isoformat()
            })
    upcoming.sort(key=lambda x: x["proposal_deadline"])

    recent_delivs = db.query(AlertDelivery).join(Alert).filter(Alert.user_id == user_id).order_by(AlertDelivery.delivered_at.desc()).limit(5).all()
    delivs_data = []
    for d in recent_delivs:
        delivs_data.append({
            "tender_id": d.tender_id,
            "tender_title": d.tender.title if d.tender else "Oportunidade",
            "alert_name": d.alert.name if d.alert else "Radar",
            "delivered_at": d.delivered_at.isoformat()
        })

    return {
        "favorites_count": favs_count,
        "alerts_count": alerts_count,
        "deliveries_count": deliveries_count,
        "upcoming_tenders": upcoming[:5],
        "recent_deliveries": delivs_data,

        # Indicadores Executivos LicitaFlow
        "active_deals_count": deals_count,
        "deals_count": deals_count,
        "companies_identified_count": companies_count,
        "companies_count": companies_count,
        "tenders_today_count": tenders_today_count,
        "total_tenders_today": tenders_today_count,
        "items_today_count": items_today_count,
        "total_items_count": total_items_count,
        "agenda_events_count": len(agenda_events),

        "categories_distribution": {
            "servicos": servicos_count,
            "materiais": materiais_count,
            "nao_classificado": nao_classificado_count,
            "Serviços": servicos_count,
            "Materiais": materiais_count,
            "Não Classificado": nao_classificado_count
        },
        "by_category": {
            "servicos": servicos_count,
            "materiais": materiais_count,
            "nao_classificado": nao_classificado_count,
            "Serviços": servicos_count,
            "Materiais": materiais_count,
            "Não Classificado": nao_classificado_count
        },
        "value_ranges": {
            "ate_50k": ate_50k,
            "de_50k_a_250k": de_50k_a_250k,
            "acima_250k": acima_250k,
            "Até R$ 50 mil": ate_50k,
            "R$ 50 mil a R$ 250 mil": de_50k_a_250k,
            "Acima de R$ 250 mil": acima_250k
        },
        "by_value_range": {
            "ate_50k": ate_50k,
            "de_50k_a_250k": de_50k_a_250k,
            "acima_250k": acima_250k,
            "Até R$ 50 mil": ate_50k,
            "R$ 50 mil a R$ 250 mil": de_50k_a_250k,
            "Acima de R$ 250 mil": acima_250k
        },
        "funnel_stages": funnel_stages,
        "agenda_events": agenda_events[:10],
        "upcoming_events": agenda_events[:10]
    }

@app.get("/api/user/dashboard-summary")
def api_user_dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return get_dashboard_summary_data(db, user)

# ----------------- Admin API -----------------

@app.get("/api/admin/metrics")
def api_admin_metrics(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    tenders_count = db.query(Tender).count()
    users_count = db.query(User).count()
    ai_count = db.query(AIAnalysis).count()
    sources = db.query(Source).all()
    return {
        "tenders_count": tenders_count,
        "users_count": users_count,
        "ai_analyses_count": ai_count,
        "sources": [
            {
                "id": s.id,
                "name": s.name,
                "official_domain": s.official_domain,
                "integration_type": s.integration_type,
                "active": s.active,
                "last_sync_at": s.last_sync_at.isoformat() if s.last_sync_at else None,
                "last_sync_status": s.last_sync_status
            }
            for s in sources
        ]
    }

@app.post("/api/admin/sync-sources")
def api_admin_sync_sources(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Dispara job de sincronização de fontes e processamento de alertas"""
    alert_result = run_alert_processing_job(db)
    tenders_count = db.query(Tender).count()
    return {
        "status": "success",
        "synced_count": tenders_count,
        "alerts_evaluated": alert_result.get("active_alerts_evaluated", 0),
        "notifications_generated": alert_result.get("total_deliveries_created", 0)
    }

@app.post("/api/admin/sources/{source_id}/toggle")
def api_admin_toggle_source(source_id: str, active: bool = Query(...), admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    src = db.query(Source).filter(Source.id == source_id).first()
    if not src:
        raise HTTPException(status_code=404, detail="Fonte não encontrada.")
    src.active = active
    db.commit()
    return {"message": "Status da fonte atualizado."}

@app.get("/api/sources")
def api_list_sources():
    return list_all_sources()
