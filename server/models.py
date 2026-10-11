import math
import uuid
from datetime import datetime, date, timedelta, timezone
from typing import List, Optional
from sqlalchemy import (
    Column, String, Text, Integer, Float, Boolean, DateTime, Date,
    ForeignKey, UniqueConstraint, Index, JSON
)
from sqlalchemy.orm import relationship
from server.database import Base

def generate_uuid():
    return str(uuid.uuid4())

class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default="USER", nullable=False) # USER, ADMIN
    status = Column(String(20), default="ACTIVE", nullable=False) # ACTIVE, INACTIVE, SUSPENDED
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    last_login_at = Column(DateTime, nullable=True)

    # Assinatura (gestão manual pelo admin; base para a integração com a Hotmart)
    plan_type = Column(String(20), default="GRATUITO", nullable=False)  # GRATUITO, MENSAL, ANUAL
    subscription_status = Column(String(20), default="ACTIVE", nullable=False)  # ACTIVE, TRIAL, SUSPENDED, CANCELED
    access_expires_at = Column(DateTime, nullable=True)  # sem data: acesso sem prazo
    hotmart_subscriber_code = Column(String(100), nullable=True)

    favorites = relationship("Favorite", back_populates="user", cascade="all, delete-orphan")
    notes = relationship("UserNote", back_populates="user", cascade="all, delete-orphan")
    saved_searches = relationship("SavedSearch", back_populates="user", cascade="all, delete-orphan")
    alerts = relationship("Alert", back_populates="user", cascade="all, delete-orphan")
    search_history = relationship("SearchHistory", back_populates="user", cascade="all, delete-orphan")

    @property
    def trial_days_left(self) -> Optional[int]:
        """Dias (arredondados para cima) até access_expires_at; None quando o acesso não tem prazo."""
        if not self.access_expires_at:
            return None
        segundos = (self.access_expires_at - datetime.utcnow()).total_seconds()
        return max(0, math.ceil(segundos / 86400))

    def motivo_bloqueio(self) -> Optional[str]:
        """
        Regra única de acesso da assinatura: devolve o motivo do bloqueio ou None se liberado.
        Administradores nunca são bloqueados (evita perder o acesso ao próprio painel).
        """
        if self.role == "ADMIN":
            return None
        situacao = self.subscription_status or "ACTIVE"
        if situacao == "SUSPENDED":
            return "Acesso suspenso. Entre em contato com o suporte da Licitações Brasil."
        if situacao == "CANCELED":
            return "Assinatura cancelada. Renove o seu plano para voltar a acessar a plataforma."
        if self.access_expires_at and self.access_expires_at <= datetime.utcnow():
            return "O seu período de acesso terminou. Renove o seu plano para continuar."
        return None


PLANOS = ("GRATUITO", "MENSAL", "ANUAL")
SITUACOES_ASSINATURA = ("ACTIVE", "TRIAL", "SUSPENDED", "CANCELED")


class Source(Base):
    __tablename__ = "sources"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    name = Column(String(255), nullable=False)
    base_url = Column(String(500), nullable=False)
    official_domain = Column(String(255), nullable=False, index=True)
    source_type = Column(String(50), default="FEDERAL", nullable=False) # FEDERAL, ESTADUAL, MUNICIPAL, SISTEMA_S
    integration_type = Column(String(50), default="API_OFICIAL", nullable=False) # API_OFICIAL, EXPORTACAO_ARQUIVO, IMPORTACAO_MANUAL, DESATIVADO
    active = Column(Boolean, default=True, index=True)
    authorization_notes = Column(Text, nullable=True)
    last_sync_at = Column(DateTime, nullable=True)
    last_sync_status = Column(String(50), nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    tenders = relationship("Tender", back_populates="source", cascade="all, delete-orphan")


class Tender(Base):
    __tablename__ = "tenders"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    source_id = Column(String(36), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    external_id = Column(String(255), nullable=False, index=True) # Ex: numeroControlePNCP
    process_number = Column(String(255), nullable=True)
    notice_number = Column(String(255), nullable=True) # Edital nº
    title = Column(String(500), nullable=False)
    object_description = Column(Text, nullable=False)
    contracting_entity = Column(String(500), nullable=False, index=True) # Órgão comprador
    purchasing_unit = Column(String(500), nullable=True) # Unidade
    state = Column(String(2), nullable=False, index=True) # UF (ex: AL, SP)
    city = Column(String(255), nullable=True, index=True)
    region = Column(String(50), nullable=True) # Nordeste, Sudeste, etc.
    administrative_sphere = Column(String(50), default="FEDERAL", index=True) # FEDERAL, ESTADUAL, MUNICIPAL
    modality = Column(String(100), default="PREGAO_ELETRONICO", index=True)
    category = Column(String(100), nullable=True, index=True) # Medicamentos, Materiais Hospitalares, TI, etc.
    natureza_objeto = Column(String(20), nullable=True, index=True) # MATERIAL, SERVICO ou OUTROS (classificação léxica)
    status = Column(String(100), default="Divulgada no PNCP", index=True)
    publication_date = Column(DateTime, nullable=True, index=True)
    opening_date = Column(DateTime, nullable=True, index=True)
    proposal_deadline = Column(DateTime, nullable=True, index=True)
    estimated_total_value = Column(Float, nullable=True, index=True)
    source_url = Column(String(1000), nullable=False)
    official_notice_url = Column(String(1000), nullable=True)
    source_system = Column(String(50), default="pncp", nullable=False, index=True) # "compras_gov" ou "pncp"
    uasg_code = Column(String(50), nullable=True, index=True) # código da UASG/Unidade Compradora
    sicaf_ready = Column(Boolean, default=False, nullable=False, index=True) # True se for do Compras.gov.br para permitir lances via SICAF
    link_comprasnet = Column(String(1000), nullable=True) # URL de acesso direto/sala de disputa
    link_pncp = Column(String(1000), nullable=True) # URL do termo/edital no PNCP
    url_disputa = Column(String(1000), nullable=True) # URL oficial da sala de disputa / portal de origem
    url_pncp = Column(String(1000), nullable=True) # URL canônica pública do PNCP (/{cnpj}/{ano}/{sequencial})
    fonte_portal = Column(String(100), default="PNCP", nullable=True, index=True) # Portal de origem: PNCP, Compras.gov.br, BNC, etc.
    link_edital_oficial = Column(String(1000), nullable=True) # Link direto para download do edital nos portais oficiais
    ai_resumo = Column(Text, nullable=True) # Resumo executivo estruturado gerado via IA Gemini
    ai_pontos_atencao = Column(Text, nullable=True) # Pontos críticos e riscos extraídos pela IA
    itens_extraidos_json = Column(JSON, nullable=True) # Lista estruturada de itens extraídos via IA
    data_completeness = Column(String(50), default="COMPLETO") # COMPLETO, PARCIAL, DADOS_AUSENTES
    raw_data_reference = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    last_synced_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    @property
    def dispute_url(self) -> str:
        return self.url_disputa or self.link_comprasnet or self.official_notice_url or self.source_url or self.canonical_pncp_url

    @property
    def canonical_pncp_url(self) -> str:
        return self.url_pncp or self.link_pncp or "https://pncp.gov.br/app/editais"

    source = relationship("Source", back_populates="tenders")
    items = relationship("TenderItem", back_populates="tender", cascade="all, delete-orphan")
    documents = relationship("TenderDocument", back_populates="tender", cascade="all, delete-orphan")
    favorites = relationship("Favorite", back_populates="tender", cascade="all, delete-orphan")
    notes = relationship("UserNote", back_populates="tender", cascade="all, delete-orphan")
    changes = relationship("TenderChange", back_populates="tender", cascade="all, delete-orphan")
    ai_analyses = relationship("AIAnalysis", back_populates="tender", cascade="all, delete-orphan")
    alert_deliveries = relationship("AlertDelivery", back_populates="tender", cascade="all, delete-orphan")
    deals = relationship("Deal", back_populates="tender", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_source_external_id"),
        Index("ix_tenders_status_opening", "status", "opening_date"),
        Index("ix_tenders_url_pncp", "url_pncp"),
        Index("ix_tenders_url_disputa", "url_disputa"),
    )


class TenderItem(Base):
    __tablename__ = "tender_items"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    tender_id = Column(String(36), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False, index=True)
    item_number = Column(Integer, nullable=False)
    description = Column(Text, nullable=False)
    unit = Column(String(50), nullable=True)
    quantity = Column(Float, nullable=True)
    estimated_unit_value = Column(Float, nullable=True)
    estimated_total_value = Column(Float, nullable=True)
    classification_code = Column(String(100), nullable=True, index=True) # CATMAT / CATSER
    material_ou_servico = Column(String(1), nullable=True)  # "M" (material) ou "S" (serviço), conforme PNCP
    criterio_julgamento = Column(String(100), nullable=True)
    situacao = Column(String(100), nullable=True)
    beneficio = Column(String(150), nullable=True)  # Ex.: participação exclusiva ME/EPP
    orcamento_sigiloso = Column(Boolean, nullable=True)
    informacao_complementar = Column(Text, nullable=True)
    origem = Column(String(20), nullable=True)  # PNCP ou LOCAL
    synced_at = Column(DateTime, nullable=True)
    raw_data = Column(JSON, nullable=True)

    tender = relationship("Tender", back_populates="items")


class TenderDocument(Base):
    __tablename__ = "tender_documents"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    tender_id = Column(String(36), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(500), nullable=False)
    document_type = Column(String(100), default="EDITAL", nullable=False) # Edital, Termo de Referência, etc.
    source_url = Column(String(1000), nullable=False)
    storage_key = Column(String(500), nullable=True)
    mime_type = Column(String(100), default="application/pdf", nullable=False)
    file_size = Column(Integer, nullable=True)
    checksum = Column(String(64), nullable=True, index=True) # SHA-256
    published_at = Column(DateTime, nullable=True)
    downloaded_at = Column(DateTime, nullable=True)
    extraction_status = Column(String(50), default="PENDING")
    extraction_error = Column(Text, nullable=True)
    sequencial_documento = Column(Integer, nullable=True)  # sequencialDocumento do PNCP
    origem = Column(String(20), nullable=True)  # PNCP ou LOCAL
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    tender = relationship("Tender", back_populates="documents")
    extraction = relationship("DocumentExtraction", back_populates="document", uselist=False, cascade="all, delete-orphan")
    ai_analyses = relationship("AIAnalysis", back_populates="document")


class Favorite(Base):
    __tablename__ = "favorites"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    tender_id = Column(String(36), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="favorites")
    tender = relationship("Tender", back_populates="favorites")

    __table_args__ = (
        UniqueConstraint("user_id", "tender_id", name="uq_user_tender_favorite"),
    )


class UserNote(Base):
    __tablename__ = "user_notes"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    tender_id = Column(String(36), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False, index=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="notes")
    tender = relationship("Tender", back_populates="notes")


class SavedSearch(Base):
    __tablename__ = "saved_searches"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    name = Column(String(255), nullable=True, default="Alerta")
    keywords = Column(String(500), nullable=True)
    ufs = Column(String(255), nullable=True)
    min_value = Column(Float, nullable=True)
    filters = Column(JSON, nullable=True, default=dict)
    active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="saved_searches")


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    keywords = Column(String(500), nullable=True)
    filters = Column(JSON, nullable=False, default=dict)
    frequency = Column(String(50), default="DAILY") # DAILY, WEEKLY, REALTIME
    channels = Column(String(100), default="IN_APP") # IN_APP, EMAIL
    active = Column(Boolean, default=True, index=True)
    last_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="alerts")
    deliveries = relationship("AlertDelivery", back_populates="alert", cascade="all, delete-orphan")


class AlertDelivery(Base):
    __tablename__ = "alert_deliveries"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    alert_id = Column(String(36), ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False, index=True)
    tender_id = Column(String(36), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False, index=True)
    channel = Column(String(50), default="IN_APP")
    delivered_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    status = Column(String(50), default="SENT")

    alert = relationship("Alert", back_populates="deliveries")
    tender = relationship("Tender", back_populates="alert_deliveries")


class TenderChange(Base):
    __tablename__ = "tender_changes"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    tender_id = Column(String(36), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False, index=True)
    change_type = Column(String(100), nullable=False) # RETIFICACAO, PRORROGACAO, CANCELAMENTO
    field_name = Column(String(100), nullable=False)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    detected_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    source_reference = Column(String(255), nullable=True)

    tender = relationship("Tender", back_populates="changes")


class DocumentExtraction(Base):
    __tablename__ = "document_extractions"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    document_id = Column(String(36), ForeignKey("tender_documents.id", ondelete="CASCADE"), unique=True, nullable=False)
    extraction_status = Column(String(50), default="PENDING")
    extracted_text_reference = Column(Text, nullable=True)
    extracted_text_hash = Column(String(64), nullable=True)
    page_count = Column(Integer, default=0)
    ocr_used = Column(Boolean, default=False)
    processed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    error_message = Column(Text, nullable=True)

    document = relationship("TenderDocument", back_populates="extraction")


class AIAnalysis(Base):
    __tablename__ = "ai_analyses"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    tender_id = Column(String(36), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False, index=True)
    document_id = Column(String(36), ForeignKey("tender_documents.id", ondelete="SET NULL"), nullable=True, index=True)
    model_provider = Column(String(50), default="GEMINI")
    model_name = Column(String(100), default="gemini-3.8-flash")
    prompt_version = Column(String(50), default="v1.0")
    analysis_type = Column(String(50), default="ANALISE_COMPLETA")
    result_json = Column(JSON, nullable=False)
    source_text_hash = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    tender = relationship("Tender", back_populates="ai_analyses")
    document = relationship("TenderDocument", back_populates="ai_analyses")


class SearchHistory(Base):
    __tablename__ = "search_history"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    query = Column(String(500), nullable=True)
    filters = Column(JSON, nullable=True)
    result_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    user = relationship("User", back_populates="search_history")


class WebhookDispatchLog(Base):
    __tablename__ = "webhook_dispatch_logs"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    rule_id = Column(String(36), ForeignKey("alerts.id", ondelete="SET NULL"), nullable=True, index=True)
    tender_id = Column(String(36), ForeignKey("tenders.id", ondelete="SET NULL"), nullable=True, index=True)
    target_url = Column(String(1000), nullable=False)
    canal = Column(String(50), default="GENERIC", nullable=False) # N8N, EVOLUTION_API, ZAPI, GENERIC
    status_code = Column(Integer, nullable=True)
    tentativa_atual = Column(Integer, default=1, nullable=False)
    sucesso = Column(Boolean, default=False, nullable=False, index=True)
    tempo_resposta_ms = Column(Float, default=0.0, nullable=False)
    payload_enviado = Column(JSON, nullable=True)
    resposta_servidor = Column(Text, nullable=True)
    criado_em = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    rule = relationship("Alert", backref="webhook_logs")
    tender = relationship("Tender", backref="webhook_logs")


DEAL_STAGES = ["oportunidades", "qualificacao", "disputa", "classificacao", "contrato"]
DEAL_STAGE_LABELS = {
    "oportunidades": "Oportunidades",
    "qualificacao": "Qualificação",
    "disputa": "Disputa",
    "classificacao": "Classificação",
    "contrato": "Contrato"
}

class Deal(Base):
    __tablename__ = "deals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tender_id = Column(Integer, ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False, index=True)
    # Dono do negócio (multitenancy): cada usuário só enxerga o próprio funil.
    # NULL = negócio legado criado antes do isolamento por usuário (não aparece para ninguém).
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    stage = Column(String(50), default="oportunidades", nullable=False, index=True) # oportunidades, qualificacao, disputa, classificacao, contrato
    notes = Column(Text, nullable=True)
    # Itens do certame que a empresa decidiu disputar (lista de dicts com item_number, description, ...).
    # None = nenhuma seleção feita (o negócio vale para o certame inteiro).
    selected_items = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    tender = relationship("Tender", back_populates="deals")

    @property
    def target_date(self):
        if self.tender:
            return self.tender.opening_date or self.tender.proposal_deadline
        return None

    @property
    def days_remaining(self):
        td = self.target_date
        if not td:
            return None
        try:
            from zoneinfo import ZoneInfo
            SP_TZ = ZoneInfo("America/Sao_Paulo")
        except Exception:
            from datetime import timezone
            SP_TZ = timezone(timedelta(hours=-3), name="America/Sao_Paulo")
        now = datetime.now(SP_TZ)
        if isinstance(td, datetime):
            td_sp = td.replace(tzinfo=SP_TZ) if td.tzinfo is None else td.astimezone(SP_TZ)
        else:
            return None
        delta = (td_sp.date() - now.date()).days
        return delta

    @property
    def deadline_urgency(self):
        td = self.target_date
        if not td:
            return "sem_data"
        try:
            from zoneinfo import ZoneInfo
            SP_TZ = ZoneInfo("America/Sao_Paulo")
        except Exception:
            from datetime import timezone
            SP_TZ = timezone(timedelta(hours=-3), name="America/Sao_Paulo")
        now = datetime.now(SP_TZ)
        if isinstance(td, datetime):
            td_sp = td.replace(tzinfo=SP_TZ) if td.tzinfo is None else td.astimezone(SP_TZ)
        else:
            return "sem_data"
        diff = (td_sp - now).total_seconds()
        hours_remaining = diff / 3600.0
        if diff < 0:
            return "vencido"
        elif 0 <= hours_remaining <= 24:
            return "critico"
        elif diff <= 7 * 86400:
            return "alerta"
        return "normal"


class LegalDocumentTemplate(Base):
    __tablename__ = "legal_document_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tipo = Column(String(50), nullable=False, index=True) # "impugnacao", "esclarecimento", "intencao_recurso", "recurso", "contrarbenzoes"
    titulo = Column(String(255), nullable=False)
    fundamentacao_legal = Column(String(500), nullable=False, default="Lei 14.133/2021")
    corpo = Column(Text, nullable=False)
    is_nova_lei = Column(Boolean, default=True, index=True) # True para Lei 14.133/2021
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    @property
    def template_type(self):
        return self.tipo

    @property
    def title(self):
        return self.titulo

    @property
    def content(self):
        return self.corpo


class SupplierProspect(Base):
    __tablename__ = "supplier_prospects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, index=True)
    cnpj = Column(String(20), nullable=True, index=True)
    state = Column(String(2), nullable=True, index=True)
    city = Column(String(255), nullable=True)
    segment = Column(String(100), nullable=True)
    total_tenders = Column(Integer, default=0)
    total_value = Column(Float, default=0.0)
    ai_summary = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class HistoricoDisputa(Base):
    __tablename__ = "historico_disputas"

    id = Column(Integer, primary_key=True, autoincrement=True)
    id_pregao = Column(String(255), nullable=False, index=True)
    numero_item = Column(Integer, nullable=False, index=True)
    tipo_evento = Column(String(50), nullable=False, index=True) # AUTO_BID, MANUAL_BID, STOP_LOSS, COMPETITOR_BID
    valor_ofertado = Column(Float, nullable=False)
    posicao = Column(Integer, default=1)
    autor = Column(String(100), default="Robô Licitações Brasil")
    detalhes = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


# ==============================================================================
# COFRE DE DOCUMENTOS DE HABILITAÇÃO DA EMPRESA
# ==============================================================================

# Categorias alinhadas aos grupos de habilitação do art. 62 da Lei 14.133/2021.
DOCUMENTO_CATEGORIAS = {
    "juridica": "Habilitação Jurídica",
    "fiscal": "Regularidade Fiscal, Social e Trabalhista",
    "economica": "Qualificação Econômico-Financeira",
    "tecnica": "Qualificação Técnica",
    "declaracoes": "Declarações",
    "outros": "Outros",
}

# Limiar (em dias) a partir do qual um documento passa a "A VENCER" (badge amarelo).
DOCUMENTO_DIAS_ALERTA = 15

DOCUMENTO_STATUS_VALIDO = "VALIDO"
DOCUMENTO_STATUS_A_VENCER = "A_VENCER"
DOCUMENTO_STATUS_VENCIDO = "VENCIDO"
DOCUMENTO_STATUS_SEM_VALIDADE = "SEM_VALIDADE"


def hoje_sao_paulo() -> date:
    """Data corrente no fuso oficial America/Sao_Paulo (referência para validade documental)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Sao_Paulo")).date()
    except Exception:
        return datetime.now(timezone(timedelta(hours=-3))).date()


class DocumentoEmpresa(Base):
    __tablename__ = "documentos_empresa"

    id = Column(Integer, primary_key=True, autoincrement=True)
    nome = Column(String(255), nullable=False, index=True)
    categoria = Column(String(50), nullable=False, index=True)  # chave de DOCUMENTO_CATEGORIAS
    data_emissao = Column(Date, nullable=False)
    data_validade = Column(Date, nullable=True, index=True)  # None = sem vencimento (ex.: contrato social)
    caminho_arquivo = Column(String(1000), nullable=False)
    tags_habilitacao = Column(String(500), nullable=True)  # lista separada por vírgulas
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    @property
    def categoria_label(self) -> str:
        return DOCUMENTO_CATEGORIAS.get(self.categoria, self.categoria or "Outros")

    @property
    def tags(self) -> List[str]:
        if not self.tags_habilitacao:
            return []
        return [t.strip() for t in self.tags_habilitacao.split(",") if t.strip()]

    def dias_restantes(self, hoje: Optional[date] = None) -> Optional[int]:
        """Dias corridos até o vencimento (negativo se vencido; None se sem validade)."""
        if not self.data_validade:
            return None
        ref = hoje or hoje_sao_paulo()
        return (self.data_validade - ref).days

    def status_validade(self, hoje: Optional[date] = None) -> str:
        """VALIDO (>15 dias), A_VENCER (0 a 15 dias), VENCIDO (<0) ou SEM_VALIDADE."""
        dias = self.dias_restantes(hoje)
        if dias is None:
            return DOCUMENTO_STATUS_SEM_VALIDADE
        if dias < 0:
            return DOCUMENTO_STATUS_VENCIDO
        if dias <= DOCUMENTO_DIAS_ALERTA:
            return DOCUMENTO_STATUS_A_VENCER
        return DOCUMENTO_STATUS_VALIDO

    def is_valido(self, hoje: Optional[date] = None) -> bool:
        return self.status_validade(hoje) != DOCUMENTO_STATUS_VENCIDO


# ==============================================================================
# RECUPERAÇÃO DE SENHA
# ==============================================================================

class PasswordResetToken(Base):
    """Token de uso único para redefinição de senha (apenas o hash SHA-256 é armazenado)."""
    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    request_ip = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def is_valido(self, agora: Optional[datetime] = None) -> bool:
        ref = agora or datetime.utcnow()
        return self.used_at is None and self.expires_at > ref


# ==============================================================================
# CLASSIFICAÇÃO AUTOMÁTICA DA NATUREZA (MATERIAL / SERVICO / OUTROS)
# ==============================================================================

from sqlalchemy import event as _sa_event  # noqa: E402


def _atualizar_natureza_tender(target: "Tender", force: bool = False) -> None:
    if not force and target.natureza_objeto:
        return
    try:
        from server.services.portal_collector import classificar_natureza_certame
        target.natureza_objeto = classificar_natureza_certame(target.title, target.object_description)
    except Exception:  # classificação nunca pode impedir a gravação do certame
        target.natureza_objeto = target.natureza_objeto or "OUTROS"


@_sa_event.listens_for(Tender, "before_insert")
def _tender_before_insert(mapper, connection, target):
    _atualizar_natureza_tender(target)


@_sa_event.listens_for(Tender, "before_update")
def _tender_before_update(mapper, connection, target):
    from sqlalchemy import inspect as _sa_inspect

    estado = _sa_inspect(target)
    mudou_texto = estado.attrs.title.history.has_changes() or estado.attrs.object_description.history.has_changes()
    mudou_natureza = estado.attrs.natureza_objeto.history.has_changes()
    if mudou_texto and not mudou_natureza:
        _atualizar_natureza_tender(target, force=True)
    elif not target.natureza_objeto:
        _atualizar_natureza_tender(target)
