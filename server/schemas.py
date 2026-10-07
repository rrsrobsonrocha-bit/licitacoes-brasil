from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from typing import Optional, List, Dict, Any, Union
from datetime import datetime

# ----------------- Auth Schemas -----------------
class UserRegisterRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)

class UserLoginRequest(BaseModel):
    email: EmailStr
    password: str

class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8, max_length=128)

class PasswordResetRequest(BaseModel):
    email: EmailStr

class UserProfileResponse(BaseModel):
    id: str
    name: str
    email: str
    role: str
    status: str
    created_at: datetime
    last_login_at: Optional[datetime] = None

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserProfileResponse

# ----------------- Search & Filter Schemas -----------------
class SearchFilterParams(BaseModel):
    q: Optional[str] = None # Palavras-chave ou múltiplos termos
    exact_match: bool = False
    state: Optional[str] = None # UF (ex: AL, SP)
    city: Optional[str] = None
    region: Optional[str] = None # Nordeste, Sudeste, Norte, Sul, Centro-Oeste
    contracting_entity: Optional[str] = None # Órgão comprador
    administrative_sphere: Optional[str] = None # FEDERAL, ESTADUAL, MUNICIPAL
    modality: Optional[str] = None # Pregão Eletrônico, Concorrência, Dispensa, etc.
    status: Optional[str] = None
    category: Optional[str] = None # Medicamentos, Materiais Hospitalares, TI, etc.
    source_system: Optional[str] = None # compras_gov, pncp
    portal: Optional[str] = None # Compras.gov.br, BEC/SP, BLL Compras, Licitações-e, Compras MG, etc.
    uasg_code: Optional[str] = None
    sicaf_ready: Optional[bool] = None
    date_from: Optional[str] = None # YYYY-MM-DD
    date_to: Optional[str] = None # YYYY-MM-DD
    value_min: Optional[float] = None
    value_max: Optional[float] = None
    sort_by: str = "relevance" # relevance, pub_desc, pub_asc, deadline_asc, deadline_desc, value_desc, value_asc
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    limit: Optional[int] = Field(default=None, ge=1, le=100)

    @field_validator("page", mode="before")
    @classmethod
    def parse_page(cls, v):
        if v is None or v == "":
            return 1
        try:
            val = int(v)
            return val if val >= 1 else 1
        except (ValueError, TypeError):
            return 1

    @field_validator("page_size", mode="before")
    @classmethod
    def parse_page_size(cls, v):
        if v is None or v == "":
            return 20
        try:
            val = int(v)
            return val if val >= 1 else 20
        except (ValueError, TypeError):
            return 20

    @field_validator("limit", mode="before")
    @classmethod
    def parse_limit(cls, v):
        if v is None or v == "":
            return None
        try:
            val = int(v)
            return val if val >= 1 else None
        except (ValueError, TypeError):
            return None

    @model_validator(mode="after")
    def sync_limit_and_page_size(self):
        if self.limit is not None:
            self.page_size = self.limit
        elif self.page_size is not None:
            self.limit = self.page_size
        return self

    @field_validator("value_min", "value_max", mode="before")
    @classmethod
    def parse_empty_float(cls, v):
        if v is None or v == "":
            return None
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return None
            try:
                return float(v.replace(",", "."))
            except (ValueError, TypeError):
                return None
        return v

class TenderItemResponse(BaseModel):
    id: str
    item_number: int
    description: str
    unit: Optional[str] = "Não informado"
    quantity: Optional[float] = None
    estimated_unit_value: Optional[float] = None
    estimated_total_value: Optional[float] = None
    classification_code: Optional[str] = "Não informado"

class TenderDocumentResponse(BaseModel):
    id: str
    name: str
    document_type: str
    source_url: str
    mime_type: str
    file_size: Optional[int] = None
    checksum: Optional[str] = None
    published_at: Optional[datetime] = None
    extraction_status: str

class TenderChangeResponse(BaseModel):
    id: str
    change_type: str
    field_name: str
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    detected_at: datetime
    source_reference: Optional[str] = None

class AIAnalysisSummaryResponse(BaseModel):
    id: str
    model_provider: str
    model_name: str
    analysis_type: str
    result_json: Dict[str, Any]
    created_at: datetime

class TenderSummaryResponse(BaseModel):
    id: str
    external_id: str
    process_number: Optional[str] = "Não informado"
    notice_number: Optional[str] = "Não informado"
    title: str
    object_description: str
    contracting_entity: str
    purchasing_unit: Optional[str] = "Não informado"
    state: str
    city: Optional[str] = "Não informado"
    administrative_sphere: str
    modality: str
    category: Optional[str] = "Outros"
    status: str
    publication_date: Optional[datetime] = None
    opening_date: Optional[datetime] = None
    proposal_deadline: Optional[datetime] = None
    estimated_total_value: Optional[float] = None
    source_name: str
    source_url: str
    official_notice_url: Optional[str] = None
    source_system: str = "pncp"
    uasg_code: Optional[str] = None
    sicaf_ready: bool = False
    link_comprasnet: Optional[str] = None
    link_pncp: Optional[str] = None
    url_disputa: Optional[str] = None
    url_pncp: Optional[str] = None
    executor_portal: Optional[str] = None
    data_completeness: str
    is_favorite: bool = False
    match_score: Optional[float] = None
    created_at: datetime
    last_synced_at: datetime

class SyncOpportunitiesResponse(BaseModel):
    status: str
    message: str
    synced_at: datetime
    total_processados: int
    novos_inseridos: int
    atualizados: int
    compras_gov_count: int
    pncp_count: int
    dispensas_count: Optional[int] = None
    total_coletados: int
    total_geral_banco: int

class SearchResultResponse(BaseModel):
    total: int
    page: int
    page_size: int
    total_pages: int
    results: List[TenderSummaryResponse]
    applied_filters: Dict[str, Any]

class TenderDetailResponse(BaseModel):
    tender: TenderSummaryResponse
    items: List[TenderItemResponse]
    documents: List[TenderDocumentResponse]
    changes: List[TenderChangeResponse]
    latest_ai_analysis: Optional[AIAnalysisSummaryResponse] = None
    user_notes: Optional[str] = None

# ----------------- Favorites & Alerts Schemas -----------------
class FavoriteToggleResponse(BaseModel):
    tender_id: str
    is_favorite: bool

class UserNoteRequest(BaseModel):
    content: str

class SavedSearchCreate(BaseModel):
    name: Optional[str] = "Alerta"
    keywords: Optional[str] = None
    ufs: Optional[str] = None
    min_value: Optional[float] = None
    filters: Optional[Dict[str, Any]] = None

class AlertCreateRequest(BaseModel):
    name: Optional[str] = "Alerta"
    keywords: Optional[str] = None
    ufs: Optional[str] = None
    min_value: Optional[float] = None
    filters: Optional[Dict[str, Any]] = None
    frequency: Optional[str] = "DAILY" # DAILY, WEEKLY, REALTIME
    channels: Optional[str] = "IN_APP" # IN_APP, EMAIL

class AlertResponse(BaseModel):
    id: str
    keywords: Optional[str] = None
    ufs: Optional[str] = None
    min_value: Optional[float] = None
    created_at: datetime
    active: bool = True
    name: Optional[str] = "Alerta"
    filters: Optional[Dict[str, Any]] = {}
    frequency: Optional[str] = "DAILY"
    last_run_at: Optional[datetime] = None
    delivery_count: int = 0

class AlertFeedItem(BaseModel):
    id: str
    delivery_id: str
    alert_id: str
    alert_name: Optional[str] = "Alerta"
    tender_id: str
    title: str
    contracting_entity: Optional[str] = None
    state: Optional[str] = None
    city: Optional[str] = None
    modality: Optional[str] = None
    estimated_total_value: Optional[float] = None
    url_disputa: Optional[str] = None
    url_pncp: Optional[str] = None
    channel: str = "IN_APP"
    delivered_at: datetime
    tender: Optional[Dict[str, Any]] = None

class AlertFeedResponse(BaseModel):
    total: int
    count: int
    feed: List[AlertFeedItem]
    items: List[AlertFeedItem]

# ----------------- Source & Admin Schemas -----------------
class SourceResponse(BaseModel):
    id: str
    name: str
    base_url: str
    official_domain: str
    source_type: str
    integration_type: str
    active: bool
    authorization_notes: Optional[str] = None
    last_sync_at: Optional[datetime] = None
    last_sync_status: Optional[str] = None
    tender_count: int = 0

# ----------------- Webhook Dispatch Schemas -----------------
class WebhookDispatchLogResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    rule_id: Optional[str] = None
    tender_id: Optional[str] = None
    target_url: str
    canal: str
    status_code: Optional[int] = None
    tentativa_atual: int = 1
    sucesso: bool = False
    tempo_resposta_ms: float = 0.0
    payload_enviado: Optional[Any] = None
    resposta_servidor: Optional[str] = None
    criado_em: datetime

class WebhookDispatchLogsListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[WebhookDispatchLogResponse]

# ----------------- Health & Metrics Schemas -----------------
class HealthMetricsResponse(BaseModel):
    status: str
    uptime_seconds: float
    uptime_human: str
    database: str
    disparos_ultimas_24h: int
    disparos_sucesso_24h: int
    disparos_falha_24h: int
    taxa_sucesso_pct: float
    timestamp: str

# ----------------- AI Analysis & Chat Schemas -----------------
class AIChatRequest(BaseModel):
    question: str

class AISummaryRequest(BaseModel):
    force_refresh: Optional[bool] = False

