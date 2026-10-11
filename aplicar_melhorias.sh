#!/usr/bin/env bash
# Licitações Brasil — 4 melhorias prioritárias. Execute na raiz do repositório: bash aplicar_melhorias.sh
set -euo pipefail
[ -f server/main.py ] || { echo "Execute na raiz do projeto licitacoes-brasil"; exit 1; }

# ==============================================================================
# MELHORIA 1 — AUTENTICAÇÃO: mostrar senha, manter conectado e recuperação de senha
# ==============================================================================
mkdir -p server
cat << 'EOF' > server/config.py
import os
from pathlib import Path
from pydantic import BaseModel
from typing import List

# Auto-load .env if present
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    try:
        with open(_env_path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _v = _line.split("=", 1)
                _k = _k.strip()
                _v = _v.strip().strip("'\"")
                if _k not in os.environ:
                    os.environ[_k] = _v
    except Exception:
        pass

class Settings(BaseModel):
    PROJECT_NAME: str = "Licitações Brasil"
    VERSION: str = "1.0.0"
    API_PREFIX: str = "/api"
    
    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./dev_licitacoes.db")
    
    # Auth
    AUTH_SECRET: str = os.getenv("AUTH_SECRET", "f3a5e82b7d91c04f9812e345a98b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b")
    JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
    
    # AI Engine
    AI_PROVIDER: str = os.getenv("AI_PROVIDER", "GEMINI")
    AI_API_KEY: str = os.getenv("AI_API_KEY", "")
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    AI_MODEL: str = os.getenv("AI_MODEL", "gemini-3.5-flash-lite")
    AI_MAX_PAGES_PER_DOC: int = int(os.getenv("AI_MAX_PAGES_PER_DOC", "50"))
    AI_MAX_CHUNK_TOKENS: int = int(os.getenv("AI_MAX_CHUNK_TOKENS", "3500"))
    
    # PNCP Integration
    PNCP_API_BASE_URL: str = os.getenv("PNCP_API_BASE_URL", "https://pncp.gov.br/api/consulta")
    PNCP_RATE_LIMIT_PER_MINUTE: int = int(os.getenv("PNCP_RATE_LIMIT_PER_MINUTE", "60"))
    PNCP_REQUEST_TIMEOUT_SECONDS: int = int(os.getenv("PNCP_REQUEST_TIMEOUT_SECONDS", "15"))
    PNCP_MAX_RETRIES: int = int(os.getenv("PNCP_MAX_RETRIES", "3"))
    
    # Storage
    STORAGE_TYPE: str = os.getenv("STORAGE_TYPE", "LOCAL")
    STORAGE_LOCAL_PATH: str = os.getenv("STORAGE_LOCAL_PATH", "./storage/documents")
    
    # Email / Notifications
    EMAIL_PROVIDER: str = os.getenv("EMAIL_PROVIDER", "MOCK")  # MOCK, SMTP ou RESEND
    EMAIL_FROM: str = os.getenv("EMAIL_FROM", "alertas@licitacoesbrasil.gov.br")
    EMAIL_API_KEY: str = os.getenv("EMAIL_API_KEY", "")
    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER: str = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_USE_SSL: bool = os.getenv("SMTP_USE_SSL", "false").lower() in ("1", "true", "yes")

    # Autenticação: sessão persistente e recuperação de senha
    APP_URL: str = os.getenv("APP_URL", "http://localhost:8000").rstrip("/")
    REMEMBER_ME_DAYS: int = int(os.getenv("REMEMBER_ME_DAYS", "30"))
    PASSWORD_RESET_TOKEN_MINUTES: int = int(os.getenv("PASSWORD_RESET_TOKEN_MINUTES", "30"))
    # Somente para desenvolvimento: devolve o link de redefinição na resposta da API quando EMAIL_PROVIDER=MOCK
    AUTH_EXPOSE_RESET_LINK: bool = os.getenv("AUTH_EXPOSE_RESET_LINK", "false").lower() in ("1", "true", "yes")

    # PNCP — API de integração (itens e arquivos das compras)
    PNCP_INTEGRACAO_BASE_URL: str = os.getenv("PNCP_INTEGRACAO_BASE_URL", "https://pncp.gov.br/api/pncp/v1").rstrip("/")
    PNCP_DOWNLOAD_MAX_MB: int = int(os.getenv("PNCP_DOWNLOAD_MAX_MB", "100"))

    # Security
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    RATE_LIMIT_REQUESTS_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "120"))
    CORS_ALLOWED_ORIGINS: List[str] = [origin.strip() for origin in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8000").split(",")]

settings = Settings()
EOF

mkdir -p server
cat << 'EOF' > server/security.py
import hashlib
import hmac
import time
import re
from datetime import datetime, timedelta
from typing import Optional, Dict
import bcrypt
from jose import jwt, JWTError
from server.config import settings

# In-memory rate limiting map: ip -> list of timestamps
_rate_limit_records: Dict[str, list] = {}

def hash_password(password: str) -> str:
    """Gera hash seguro com bcrypt (fator de custo 12)"""
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verificação em tempo constante contra timing attacks"""
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception:
        return False

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Gera token JWT criptograficamente assinado com expiração"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "iat": datetime.utcnow()})
    return jwt.encode(to_encode, settings.AUTH_SECRET, algorithm=settings.JWT_ALGORITHM)

def decode_access_token(token: str) -> Optional[dict]:
    """Decodifica e valida assinatura e expiração do JWT"""
    try:
        payload = jwt.decode(token, settings.AUTH_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except JWTError:
        return None

def check_rate_limit(client_ip: str, max_requests: int = 120, window_seconds: int = 60) -> bool:
    """
    Verifica e aplica limite de requisições por IP (Sliding Window)
    Retorna True se permitido, False se excedeu.
    """
    now = time.time()
    records = _rate_limit_records.get(client_ip, [])
    # Limpar registros antigos fora da janela
    records = [t for t in records if now - t < window_seconds]
    if len(records) >= max_requests:
        _rate_limit_records[client_ip] = records
        return False
    records.append(now)
    _rate_limit_records[client_ip] = records
    return True

# Sanitização e Blindagem contra Prompt Injection em Documentos de Licitação
PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|above|prior)\s+instructions", re.IGNORECASE),
    re.compile(r"você\s+agora\s+é\s+um\s+assistente\s+sem\s+regras", re.IGNORECASE),
    re.compile(r"system\s*prompt\s*override", re.IGNORECASE),
    re.compile(r"esqueca\s+tudo\s+o\s+que\s+foi\s+dito", re.IGNORECASE),
    re.compile(r"<\|im_start\|>", re.IGNORECASE),
    re.compile(r"<\|im_end\|>", re.IGNORECASE),
    re.compile(r"\[INST\]", re.IGNORECASE),
    re.compile(r"\[/INST\]", re.IGNORECASE),
]

def sanitize_tender_text_for_ai(raw_text: str) -> str:
    """
    Sanitiza o texto extraído do edital para neutralizar tentativas de jailbreak e prompt injection.
    O texto oficial nunca é descartado, mas comandos hostis são neutralizados.
    """
    if not raw_text:
        return ""
    sanitized = raw_text
    for pattern in PROMPT_INJECTION_PATTERNS:
        sanitized = pattern.sub("[TRECHO_NEUTRALIZADO_SEGURANCA]", sanitized)
    return sanitized

def calculate_sha256(content_bytes: bytes) -> str:
    """Gera hash SHA-256 para integridade e deduplicação de documentos"""
    return hashlib.sha256(content_bytes).hexdigest()


# ------------------------------------------------------------------------------
# Sessão e recuperação de senha
# ------------------------------------------------------------------------------
import secrets as _secrets


def password_fingerprint(password_hash: str) -> str:
    """
    Impressão digital da senha atual embutida no JWT (claim "pwv").
    Ao redefinir a senha o hash muda e todos os tokens emitidos antes deixam de valer.
    """
    return hmac.new(
        settings.AUTH_SECRET.encode("utf-8"), (password_hash or "").encode("utf-8"), hashlib.sha256
    ).hexdigest()[:16]


def generate_reset_token() -> str:
    """Token aleatório (256 bits, URL-safe) enviado por e-mail; nunca é persistido em claro."""
    return _secrets.token_urlsafe(32)


def hash_reset_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def is_rate_limited(key: str, max_requests: int, window_seconds: int) -> bool:
    """Registra uma ocorrência e retorna True quando a chave (ex.: 'forgot:ip:1.2.3.4') excedeu o limite."""
    return not check_rate_limit(key, max_requests=max_requests, window_seconds=window_seconds)


def tentativas_recentes(key: str, window_seconds: int) -> int:
    """Conta ocorrências da chave na janela, sem registrar uma nova."""
    now = time.time()
    records = [t for t in _rate_limit_records.get(key, []) if now - t < window_seconds]
    _rate_limit_records[key] = records
    return len(records)


def registrar_tentativa(key: str) -> None:
    _rate_limit_records.setdefault(key, []).append(time.time())


def limpar_tentativas(key: str) -> None:
    _rate_limit_records.pop(key, None)
EOF

mkdir -p server/services
cat << 'EOF' > server/services/email_service.py
"""
Envio de e-mails transacionais (recuperação de senha, avisos).

Provedores (variável EMAIL_PROVIDER):
  - MOCK   : não envia; registra o conteúdo no log (desenvolvimento).
  - SMTP   : smtplib com STARTTLS (porta 587) ou SSL (SMTP_USE_SSL=true, porta 465).
  - RESEND : API HTTP https://api.resend.com/emails com EMAIL_API_KEY.
"""
import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import List, Optional

import httpx

from server.config import settings

logger = logging.getLogger(__name__)


@dataclass
class EmailEnviado:
    provedor: str
    destinatario: str
    assunto: str
    sucesso: bool
    erro: Optional[str] = None


@dataclass
class _CaixaMock:
    """Guarda as mensagens do provedor MOCK em memória (útil em testes)."""
    mensagens: List[dict] = field(default_factory=list)

    def registrar(self, destinatario: str, assunto: str, texto: str, html: Optional[str]) -> None:
        self.mensagens.append({"to": destinatario, "subject": assunto, "text": texto, "html": html})
        del self.mensagens[:-50]


caixa_mock = _CaixaMock()


def _remetente() -> str:
    return formataddr(("Licitações Brasil", settings.EMAIL_FROM))


def _enviar_smtp(destinatario: str, assunto: str, texto: str, html: Optional[str]) -> None:
    if not settings.SMTP_HOST:
        raise RuntimeError("SMTP_HOST não configurado.")
    msg = EmailMessage()
    msg["From"] = _remetente()
    msg["To"] = destinatario
    msg["Subject"] = assunto
    msg["Message-ID"] = make_msgid(domain=settings.EMAIL_FROM.split("@")[-1] or None)
    msg.set_content(texto)
    if html:
        msg.add_alternative(html, subtype="html")

    contexto = ssl.create_default_context()
    if settings.SMTP_USE_SSL:
        with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, context=contexto, timeout=20) as smtp:
            if settings.SMTP_USER:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls(context=contexto)
            smtp.ehlo()
            if settings.SMTP_USER:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            smtp.send_message(msg)


def _enviar_resend(destinatario: str, assunto: str, texto: str, html: Optional[str]) -> None:
    if not settings.EMAIL_API_KEY:
        raise RuntimeError("EMAIL_API_KEY não configurada para o provedor RESEND.")
    payload = {"from": _remetente(), "to": [destinatario], "subject": assunto, "text": texto}
    if html:
        payload["html"] = html
    resp = httpx.post(
        "https://api.resend.com/emails",
        json=payload,
        headers={"Authorization": f"Bearer {settings.EMAIL_API_KEY}"},
        timeout=20,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"Resend respondeu HTTP {resp.status_code}: {resp.text[:200]}")


def enviar_email(destinatario: str, assunto: str, texto: str, html: Optional[str] = None) -> EmailEnviado:
    """Envia o e-mail pelo provedor configurado. Nunca propaga exceção para a rota chamadora."""
    provedor = (settings.EMAIL_PROVIDER or "MOCK").strip().upper()
    try:
        if provedor == "SMTP":
            _enviar_smtp(destinatario, assunto, texto, html)
        elif provedor == "RESEND":
            _enviar_resend(destinatario, assunto, texto, html)
        else:
            provedor = "MOCK"
            caixa_mock.registrar(destinatario, assunto, texto, html)
            logger.info("[EMAIL MOCK] Para: %s | Assunto: %s\n%s", destinatario, assunto, texto)
        return EmailEnviado(provedor, destinatario, assunto, True)
    except Exception as exc:  # falha de rede/credencial não pode vazar detalhes ao cliente
        logger.error("Falha ao enviar e-mail via %s para %s: %s", provedor, destinatario, exc)
        return EmailEnviado(provedor, destinatario, assunto, False, str(exc))


def montar_email_redefinicao(nome: str, link: str, minutos: int) -> tuple:
    """Retorna (assunto, texto, html) do e-mail de redefinição de senha."""
    from html import escape

    primeiro_nome = (nome or "").split(" ")[0] or "olá"
    assunto = "Redefinição de senha — Licitações Brasil"
    texto = (
        f"Olá, {primeiro_nome}.\n\n"
        "Recebemos um pedido para redefinir a senha da sua conta na Licitações Brasil.\n"
        f"Para criar uma nova senha, acesse o link abaixo (válido por {minutos} minutos e de uso único):\n\n"
        f"{link}\n\n"
        "Se você não fez este pedido, ignore este e-mail: sua senha atual continua válida.\n\n"
        "Equipe Licitações Brasil"
    )
    html = f"""<!doctype html>
<html lang="pt-BR"><body style="margin:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;color:#0f172a">
  <div style="max-width:520px;margin:24px auto;background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #e2e8f0">
    <div style="background:#082f49;padding:18px 24px;color:#ffffff;font-size:18px;font-weight:bold">
      Licitações<span style="color:#38bdf8">Brasil</span>
    </div>
    <div style="padding:24px">
      <p style="margin:0 0 12px">Olá, {escape(primeiro_nome)}.</p>
      <p style="margin:0 0 16px">Recebemos um pedido para redefinir a senha da sua conta.</p>
      <p style="margin:0 0 20px">
        <a href="{escape(link, quote=True)}" style="display:inline-block;background:#0284c7;color:#ffffff;text-decoration:none;font-weight:bold;padding:12px 20px;border-radius:10px">Criar nova senha</a>
      </p>
      <p style="margin:0 0 8px;font-size:13px;color:#475569">O link é válido por {minutos} minutos e só pode ser usado uma vez.</p>
      <p style="margin:0;font-size:13px;color:#475569">Se você não fez este pedido, ignore este e-mail: sua senha atual continua válida.</p>
    </div>
  </div>
</body></html>"""
    return assunto, texto, html
EOF

mkdir -p server
cat << 'EOF' > server/schemas.py
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
    password: str = Field(..., max_length=128)
    remember_me: bool = False  # "Manter conectado": sessão estendida (REMEMBER_ME_DAYS)

class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8, max_length=128)

class PasswordResetRequest(BaseModel):
    email: EmailStr

class PasswordResetConfirmRequest(BaseModel):
    token: str = Field(..., min_length=20, max_length=200)
    new_password: str = Field(..., min_length=8, max_length=128)

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
    expires_in: Optional[int] = None  # segundos até a expiração do token
    remember_me: bool = False

# ----------------- Search & Filter Schemas -----------------
class SearchFilterParams(BaseModel):
    q: Optional[str] = None # Palavras-chave ou múltiplos termos
    exact_match: bool = False
    state: Optional[str] = None # UF (ex: AL, SP)
    uf: Optional[str] = None # Alias para UF (ex: AL, SP)
    city: Optional[str] = None
    region: Optional[str] = None # Nordeste, Sudeste, Norte, Sul, Centro-Oeste
    contracting_entity: Optional[str] = None # Órgão comprador
    administrative_sphere: Optional[str] = None # FEDERAL, ESTADUAL, MUNICIPAL
    modality: Optional[str] = None # Pregão Eletrônico, Concorrência, Dispensa, etc.
    status: Optional[str] = None
    category: Optional[str] = None # MATERIAL, SERVICO, OUTROS (ou categoria legada: Medicamentos, TI...)
    natureza: Optional[str] = None # MATERIAL, SERVICO ou OUTROS
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
    material_ou_servico: Optional[str] = None
    criterio_julgamento: Optional[str] = None
    situacao: Optional[str] = None
    beneficio: Optional[str] = None
    orcamento_sigiloso: Optional[bool] = None
    informacao_complementar: Optional[str] = None
    origem: Optional[str] = None

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
    origem: Optional[str] = None
    download_url: Optional[str] = None  # rota interna de download (sem redirecionamento externo)

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
    natureza_objeto: Optional[str] = None  # MATERIAL, SERVICO ou OUTROS
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
    fonte_portal: Optional[str] = "PNCP"
    link_edital_oficial: Optional[str] = None
    ai_resumo: Optional[str] = None
    ai_pontos_atencao: Optional[str] = None
    itens_extraidos_json: Optional[Any] = None
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
EOF

mkdir -p server/templates
cat << 'EOF' > server/templates/base.html
<!DOCTYPE html>
<html lang="pt-BR" class="h-full bg-slate-50">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="theme-color" content="#0f172a">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <link rel="manifest" href="/static/manifest.json">
  <link rel="icon" type="image/svg+xml" href="/static/icons/icon.svg">
  <title>{% block title %}Licitações Brasil - Plataforma Inteligente de Contratações Públicas{% endblock %}</title>
  <!-- Tailwind CSS CDN para renderização ágil e responsiva -->
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      theme: {
        extend: {
          colors: {
            brand: {
              50: '#f0f7ff',
              100: '#e0effe',
              500: '#0284c7',
              600: '#0369a1',
              700: '#075985',
              800: '#0c4a6e',
              900: '#082f49',
            }
          }
        }
      }
    }
  </script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
    [x-cloak] { display: none !important; }
    .badge-status { @apply inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium; }
    .safe-area-bottom { padding-bottom: max(0.5rem, env(safe-area-inset-bottom)); }
  </style>
</head>
<body class="flex flex-col min-h-full text-slate-800 antialiased font-sans">
  
  <!-- Top Navigation Header -->
  <header class="bg-brand-900 text-white shadow-md sticky top-0 z-50">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
      <div class="flex items-center justify-between h-16">
        <!-- Logo & Brand -->
        <div class="flex items-center space-x-3">
          <a href="/" class="flex items-center space-x-3">
            <div class="w-10 h-10 rounded-lg bg-gradient-to-tr from-sky-400 to-emerald-400 flex items-center justify-center text-brand-900 font-black text-xl shadow-inner">
              <i class="fa-solid fa-scale-balanced"></i>
            </div>
            <div>
              <span class="text-xl font-bold tracking-tight text-white">Licitações<span class="text-sky-400">Brasil</span></span>
              <span class="hidden sm:inline-block ml-2 text-xs bg-sky-950 text-sky-300 px-2 py-0.5 rounded font-mono border border-sky-800">PNCP + IA</span>
            </div>
          </a>
        </div>

        <!-- Main Nav Links -->
        <nav class="hidden md:flex items-center space-x-6 text-sm font-medium">
          <a href="/search" class="text-slate-200 hover:text-white transition flex items-center gap-1.5">
            <i class="fa-solid fa-magnifying-glass text-sky-400"></i> Pesquisar Editais
          </a>
          <a href="/dashboard" class="text-slate-200 hover:text-white transition flex items-center gap-1.5">
            <i class="fa-solid fa-chart-pie text-sky-400"></i> Dashboard
          </a>
          <a href="/favorites" class="text-slate-200 hover:text-white transition flex items-center gap-1.5">
            <i class="fa-solid fa-star text-amber-400"></i> Favoritos
          </a>
          <a href="/negocios?view=quadros" class="text-slate-200 hover:text-white transition flex items-center gap-1.5">
            <i class="fa-solid fa-table-columns text-emerald-400"></i> Negócios
          </a>
          <a href="/agenda" class="text-slate-200 hover:text-white transition flex items-center gap-1.5">
            <i class="fa-solid fa-calendar-days text-rose-400"></i> Agenda
          </a>
          <a href="/disputa" class="text-slate-200 hover:text-white transition flex items-center gap-1.5 font-bold">
            <i class="fa-solid fa-tower-broadcast text-rose-400 animate-pulse"></i> Disputa <span class="px-1.5 py-0.5 rounded text-[10px] font-black bg-rose-500 text-white animate-pulse">AO VIVO</span>
          </a>
          <a href="/fornecedores" class="text-slate-200 hover:text-white transition flex items-center gap-1.5">
            <i class="fa-solid fa-building-user text-purple-400"></i> Fornecedores
          </a>
          <a href="/juridico" class="text-slate-200 hover:text-white transition flex items-center gap-1.5">
            <i class="fa-solid fa-gavel text-amber-400"></i> Peças Jurídicas
          </a>
          <a href="/documentos" class="text-slate-200 hover:text-white transition flex items-center gap-1.5">
            <i class="fa-solid fa-vault text-emerald-400"></i> Cofre de Documentos
          </a>
          <a href="/alerts" class="text-slate-200 hover:text-white transition flex items-center gap-1.5">
            <i class="fa-solid fa-bell text-sky-400"></i> Alertas
          </a>
          <a href="/admin" class="text-slate-200 hover:text-white transition flex items-center gap-1.5">
            <i class="fa-solid fa-shield-halved text-emerald-400"></i> Gestão
          </a>
        </nav>

        <!-- User Controls / Auth -->
        <div class="flex items-center space-x-3 text-sm">
          <span id="user-display" class="hidden sm:inline-block text-slate-300 font-medium">
            <i class="fa-regular fa-user-circle mr-1"></i> <span id="user-name">Usuário</span>
          </span>
          <button id="auth-btn" onclick="handleAuthAction()" class="bg-sky-500 hover:bg-sky-600 text-white font-medium px-4 py-2 rounded-lg text-xs tracking-wider uppercase transition shadow-sm">
            Entrar
          </button>
          <!-- Mobile Menu Hamburger Button -->
          <button id="mobile-menu-toggle" onclick="toggleMobileMenu()" class="md:hidden p-2 text-slate-300 hover:text-white hover:bg-brand-800 rounded-lg transition focus:outline-none focus:ring-2 focus:ring-sky-400" aria-label="Abrir Menu">
            <i class="fa-solid fa-bars text-xl"></i>
          </button>
        </div>
      </div>
    </div>
  </header>

  <!-- Mobile Drawer Menu & Backdrop -->
  <div id="mobile-menu-backdrop" onclick="closeMobileMenu()" class="fixed inset-0 bg-slate-950/75 backdrop-blur-sm z-50 opacity-0 pointer-events-none transition-opacity duration-300 md:hidden" aria-hidden="true"></div>

  <aside id="mobile-menu-drawer" class="fixed top-0 right-0 bottom-0 w-80 max-w-[85vw] bg-slate-900 border-l border-slate-800 shadow-2xl z-50 transform translate-x-full transition-transform duration-300 ease-in-out md:hidden flex flex-col safe-area-bottom" aria-label="Menu de Navegação Móvel">
    <!-- Cabeçalho do Drawer -->
    <div class="p-4 border-b border-slate-800 flex items-center justify-between bg-slate-900/95">
      <div class="flex items-center space-x-3">
        <div class="w-9 h-9 rounded-lg bg-gradient-to-tr from-sky-400 to-emerald-400 flex items-center justify-center text-brand-900 font-black text-lg shadow-inner">
          <i class="fa-solid fa-scale-balanced"></i>
        </div>
        <div>
          <span class="text-lg font-bold tracking-tight text-white">Licitações<span class="text-sky-400">Brasil</span></span>
          <span class="block text-[10px] text-sky-400 font-mono">PWA • Lei 14.133</span>
        </div>
      </div>
      <button id="mobile-menu-close" onclick="closeMobileMenu()" class="p-2 text-slate-400 hover:text-white rounded-lg hover:bg-slate-800 transition focus:outline-none focus:ring-2 focus:ring-sky-400" aria-label="Fechar Menu">
        <i class="fa-solid fa-xmark text-xl"></i>
      </button>
    </div>

    <!-- Perfil / Usuário no Drawer -->
    <div class="px-4 py-3 bg-slate-800/60 border-b border-slate-800 flex items-center justify-between">
      <div class="flex items-center space-x-3">
        <div class="w-9 h-9 rounded-full bg-slate-700/80 border border-slate-600 flex items-center justify-center text-sky-400 text-base">
          <i class="fa-regular fa-user"></i>
        </div>
        <div>
          <div id="mobile-user-name" class="text-xs font-semibold text-white">Visitante</div>
          <div id="mobile-user-role" class="text-[10px] text-slate-400">Perfil Público</div>
        </div>
      </div>
      <a href="/admin" class="text-xs text-sky-400 hover:text-sky-300 font-medium py-1 px-2 rounded-md hover:bg-slate-700/50 transition">
        Painel
      </a>
    </div>

    <!-- Lista Completa de Módulos (11 Itens Funcionais) -->
    <nav class="flex-1 overflow-y-auto py-3 px-3 space-y-1 text-sm font-medium">
      <!-- 1. Pesquisar Editais -->
      <a href="/" data-route="/search" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-magnifying-glass text-sky-400 w-5 text-center"></i>
          <span>Pesquisar Editais</span>
        </div>
        <i class="fa-solid fa-chevron-right text-xs text-slate-500"></i>
      </a>

      <!-- 2. Dashboard -->
      <a href="/dashboard" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-chart-pie text-sky-400 w-5 text-center"></i>
          <span>Dashboard</span>
        </div>
        <i class="fa-solid fa-chevron-right text-xs text-slate-500"></i>
      </a>

      <!-- 3. Favoritos -->
      <a href="/favorites" data-route="/favoritos" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-star text-amber-400 w-5 text-center"></i>
          <span>Favoritos</span>
        </div>
        <i class="fa-solid fa-chevron-right text-xs text-slate-500"></i>
      </a>

      <!-- 4. Negócios -->
      <a href="/negocios" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-table-columns text-emerald-400 w-5 text-center"></i>
          <span>Negócios</span>
        </div>
        <i class="fa-solid fa-chevron-right text-xs text-slate-500"></i>
      </a>

      <!-- 5. Agenda -->
      <a href="/agenda" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-calendar-days text-rose-400 w-5 text-center"></i>
          <span>Agenda</span>
        </div>
        <i class="fa-solid fa-chevron-right text-xs text-slate-500"></i>
      </a>

      <!-- 6. Disputa AO VIVO com badge pulsante vermelho -->
      <a href="/disputa" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition font-bold">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-tower-broadcast text-rose-400 animate-pulse w-5 text-center"></i>
          <span class="text-white">Disputa AO VIVO</span>
        </div>
        <span class="px-2 py-0.5 rounded text-[10px] font-black bg-rose-500 text-white animate-pulse tracking-wide">AO VIVO</span>
      </a>

      <!-- 7. Fornecedores -->
      <a href="/fornecedores" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-building-user text-purple-400 w-5 text-center"></i>
          <span>Fornecedores</span>
        </div>
        <i class="fa-solid fa-chevron-right text-xs text-slate-500"></i>
      </a>

      <!-- 8. Peças Jurídicas -->
      <a href="/juridico" data-route="/pecas-juridicas" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-gavel text-amber-400 w-5 text-center"></i>
          <span>Peças Jurídicas</span>
        </div>
        <i class="fa-solid fa-chevron-right text-xs text-slate-500"></i>
      </a>

      <!-- 8.1 Cofre de Documentos -->
      <a href="/documentos" data-route="/cofre" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-vault text-emerald-400 w-5 text-center"></i>
          <span>Cofre de Documentos</span>
        </div>
        <i class="fa-solid fa-chevron-right text-xs text-slate-500"></i>
      </a>

      <!-- 9. Alertas -->
      <a href="/alerts" data-route="/alertas" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-bell text-sky-400 w-5 text-center"></i>
          <span>Alertas</span>
        </div>
        <i class="fa-solid fa-chevron-right text-xs text-slate-500"></i>
      </a>

      <!-- 10. Gestão / Perfil do Usuário / Administrador -->
      <a href="/admin" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-shield-halved text-emerald-400 w-5 text-center"></i>
          <span>Gestão / Administrador</span>
        </div>
        <i class="fa-solid fa-chevron-right text-xs text-slate-500"></i>
      </a>
    </nav>

    <!-- 11. Botão Sair / Autenticação no Rodapé do Drawer -->
    <div class="p-4 border-t border-slate-800 bg-slate-900/95">
      <button id="mobile-auth-btn" onclick="handleAuthAction()" data-route="/logout" class="w-full bg-sky-500 hover:bg-sky-600 text-white font-medium py-2.5 px-4 rounded-xl text-xs tracking-wider uppercase transition flex items-center justify-center gap-2 shadow-sm">
        <i id="mobile-auth-icon" class="fa-solid fa-right-to-bracket"></i>
        <span id="mobile-auth-btn-text">Entrar</span>
      </button>
    </div>
  </aside>

  <!-- Barra Inferior Fixa de Aplicativo (Bottom Navigation Bar PWA) -->
  <nav class="fixed bottom-0 left-0 right-0 z-50 bg-slate-900 border-t border-slate-800 md:hidden flex justify-around items-center py-2 px-1 safe-area-bottom shadow-2xl backdrop-blur-md bg-opacity-95" aria-label="Navegação Inferior Mobile">
    <!-- [Busca]: Ícone de lupa -> rota de pesquisa -->
    <a href="/search" data-route="/" class="flex flex-col items-center justify-center flex-1 py-1 text-slate-400 hover:text-sky-400 transition text-center group">
      <i class="fa-solid fa-magnifying-glass text-lg group-hover:scale-110 transition-transform"></i>
      <span class="text-[10px] font-medium mt-1">Busca</span>
    </a>

    <!-- [Agenda]: Ícone de calendário -> /agenda -->
    <a href="/agenda" class="flex flex-col items-center justify-center flex-1 py-1 text-slate-400 hover:text-rose-400 transition text-center group">
      <i class="fa-solid fa-calendar-days text-lg group-hover:scale-110 transition-transform"></i>
      <span class="text-[10px] font-medium mt-1">Agenda</span>
    </a>

    <!-- [Disputa AO VIVO]: Ícone de transmissão com destaque colorido e badge pulsante -> /disputa -->
    <a href="/disputa" class="flex flex-col items-center justify-center flex-1 py-1 text-white text-center relative group">
      <div class="w-11 h-11 -mt-5 rounded-full bg-gradient-to-tr from-rose-600 to-rose-500 flex items-center justify-center text-white shadow-lg border-2 border-slate-900 group-hover:scale-110 transition-transform">
        <i class="fa-solid fa-tower-broadcast text-base animate-pulse"></i>
      </div>
      <span class="text-[9px] font-bold mt-1 text-rose-400 flex items-center gap-1">
        AO VIVO
        <span class="w-1.5 h-1.5 rounded-full bg-rose-500 animate-ping"></span>
      </span>
    </a>

    <!-- [Alertas]: Ícone de sino com contador/notificação -> /alertas -->
    <a href="/alerts" data-route="/alertas" class="flex flex-col items-center justify-center flex-1 py-1 text-slate-400 hover:text-sky-400 transition text-center relative group">
      <div class="relative">
        <i class="fa-solid fa-bell text-lg group-hover:scale-110 transition-transform"></i>
        <span id="bottom-alerts-badge" class="absolute -top-1.5 -right-2 w-4 h-4 bg-sky-500 text-white text-[9px] font-bold rounded-full flex items-center justify-center ring-2 ring-slate-900">3</span>
      </div>
      <span class="text-[10px] font-medium mt-1">Alertas</span>
    </a>

    <!-- [Menu / Mais]: Ícone de 3 barras que abre o Drawer lateral com todas as opções restantes -->
    <button type="button" id="bottom-menu-toggle" onclick="openMobileMenu()" class="flex flex-col items-center justify-center flex-1 py-1 text-slate-400 hover:text-white transition text-center group focus:outline-none" aria-label="Abrir Menu Completo">
      <i class="fa-solid fa-bars text-lg group-hover:scale-110 transition-transform"></i>
      <span class="text-[10px] font-medium mt-1">Menu</span>
    </button>
  </nav>

  <!-- Notification Bar for System Messages -->
  <div id="toast-container" class="fixed bottom-20 md:bottom-5 right-5 z-50 flex flex-col gap-2 pointer-events-none"></div>

  <!-- Main View Container -->
  <main class="flex-grow pb-24 md:pb-0">
    {% block content %}{% endblock %}
  </main>

  <!-- Footer -->
  <footer class="bg-slate-900 text-slate-400 text-xs py-8 border-t border-slate-800">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
      <div class="grid grid-cols-1 md:grid-cols-4 gap-8 mb-6">
        <div>
          <h4 class="text-white font-semibold text-sm mb-2 flex items-center gap-2">
            <i class="fa-solid fa-scale-balanced text-sky-400"></i> Licitações Brasil
          </h4>
          <p class="leading-relaxed text-slate-400">
            Plataforma aberta de pesquisa, inteligência documental e monitoramento de contratações públicas baseada na Lei nº 14.133/2021.
          </p>
        </div>
        <div>
          <h5 class="text-white font-semibold mb-2">Fontes Oficiais</h5>
          <ul class="space-y-1">
            <li><a href="https://pncp.gov.br" target="_blank" class="hover:text-white transition">PNCP Nacional</a></li>
            <li><a href="https://comprasgovernamentais.gov.br" target="_blank" class="hover:text-white transition">Compras.gov.br</a></li>
            <li><a href="https://www.portaldecompraspublicas.com.br" target="_blank" class="hover:text-white transition">Portal de Compras Públicas</a></li>
          </ul>
        </div>
        <div>
          <h5 class="text-white font-semibold mb-2">Segurança e LGPD</h5>
          <p class="leading-relaxed">
            Tratamento de dados estritamente em conformidade com a LGPD (Lei 13.709/2018). Direitos de acesso e exclusão garantidos.
          </p>
        </div>
        <div>
          <h5 class="text-white font-semibold mb-2">Aviso Legal de IA</h5>
          <p class="leading-relaxed text-slate-400">
            As análises automáticas por inteligência artificial são instrumentos de triagem e não substituem a leitura e conferência do edital oficial.
          </p>
        </div>
      </div>
      <div class="pt-6 border-t border-slate-800 flex flex-col sm:flex-row justify-between items-center text-slate-500">
        <span>© 2025 Licitações Brasil. Sistema aberto e auditável.</span>
        <span class="mt-2 sm:mt-0">Desenvolvido com Next.js, FastAPI, Prisma & IA Documental.</span>
      </div>
    </div>
  </footer>

  <!-- Global Application Scripts -->
  <script>
    // Controle Vanilla JS do Drawer Lateral Mobile
    function openMobileMenu() {
      const drawer = document.getElementById('mobile-menu-drawer');
      const backdrop = document.getElementById('mobile-menu-backdrop');
      if (drawer) {
        drawer.classList.remove('translate-x-full');
        drawer.classList.add('translate-x-0');
      }
      if (backdrop) {
        backdrop.classList.remove('opacity-0', 'pointer-events-none');
        backdrop.classList.add('opacity-100', 'pointer-events-auto');
      }
      document.body.classList.add('overflow-hidden');
    }

    function closeMobileMenu() {
      const drawer = document.getElementById('mobile-menu-drawer');
      const backdrop = document.getElementById('mobile-menu-backdrop');
      if (drawer) {
        drawer.classList.remove('translate-x-0');
        drawer.classList.add('translate-x-full');
      }
      if (backdrop) {
        backdrop.classList.remove('opacity-100', 'pointer-events-auto');
        backdrop.classList.add('opacity-0', 'pointer-events-none');
      }
      document.body.classList.remove('overflow-hidden');
    }

    function toggleMobileMenu() {
      const drawer = document.getElementById('mobile-menu-drawer');
      if (drawer && drawer.classList.contains('translate-x-0')) {
        closeMobileMenu();
      } else {
        openMobileMenu();
      }
    }

    // Fechar menu móvel com a tecla ESC
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeMobileMenu();
    });

    // Interceptador e normalização de navegação de rotas no frontend
    document.addEventListener('click', (e) => {
      const link = e.target.closest('a');
      if (!link) return;
      const href = link.getAttribute('href');
      if (href === '/favoritos') {
        e.preventDefault();
        window.location.href = '/favorites';
      } else if (href === '/alertas') {
        e.preventDefault();
        window.location.href = '/alerts';
      } else if (href === '/pecas-juridicas') {
        e.preventDefault();
        window.location.href = '/juridico';
      } else if (href === '/cofre') {
        e.preventDefault();
        window.location.href = '/documentos';
      } else if (href === '/logout') {
        e.preventDefault();
        handleAuthAction();
      }
    });

    // Atualização dinâmica do badge de alertas na bottom bar
    function refreshAlertBadge() {
      const badge = document.getElementById('bottom-alerts-badge');
      if (!badge) return;
      fetch('/api/alerts/feed?limit=5')
        .then(r => r.ok ? r.json() : null)
        .then(data => {
          if (data && typeof data.total === 'number') {
            badge.textContent = data.total > 9 ? '9+' : data.total;
            badge.style.display = data.total > 0 ? 'flex' : 'none';
          }
        })
        .catch(() => {});
    }

    // Gerenciador de Sessão no Frontend
    // "Manter conectado" => localStorage (persiste ao fechar o navegador);
    // sem a opção => sessionStorage (encerra ao fechar a aba/navegador).
    const AppSession = {
      _ler: (chave) => {
        try { return sessionStorage.getItem(chave) || localStorage.getItem(chave); } catch { return null; }
      },
      _limparArmazenamento: () => {
        ['token', 'user', 'token_exp'].forEach(chave => {
          try { localStorage.removeItem(chave); } catch {}
          try { sessionStorage.removeItem(chave); } catch {}
        });
      },
      tokenExpirado: (token) => {
        try {
          const base64 = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
          const payload = JSON.parse(atob(base64.padEnd(base64.length + (4 - base64.length % 4) % 4, '=')));
          return typeof payload.exp === 'number' && payload.exp * 1000 <= Date.now();
        } catch { return false; }
      },
      getToken: () => {
        const token = AppSession._ler('token');
        if (token && AppSession.tokenExpirado(token)) {
          AppSession._limparArmazenamento();
          return null;
        }
        return token;
      },
      getUser: () => {
        if (!AppSession.getToken()) return null;
        try { return JSON.parse(AppSession._ler('user')); } catch { return null; }
      },
      isPersistente: () => {
        try { return !!localStorage.getItem('token'); } catch { return false; }
      },
      setSession: (token, user, manterConectado = true) => {
        AppSession._limparArmazenamento();
        try {
          const destino = manterConectado ? localStorage : sessionStorage;
          destino.setItem('token', token);
          destino.setItem('user', JSON.stringify(user));
        } catch {
          showToast('Seu navegador bloqueou o armazenamento local; a sessão não será mantida.', 'error');
        }
        AppSession.refreshUI();
      },
      clear: () => {
        AppSession._limparArmazenamento();
        AppSession.refreshUI();
      },
      refreshUI: () => {
        const user = AppSession.getUser();
        const authBtn = document.getElementById('auth-btn');
        const userDisplay = document.getElementById('user-display');
        const userName = document.getElementById('user-name');

        // Controles de autenticação no drawer móvel
        const mobileAuthBtn = document.getElementById('mobile-auth-btn');
        const mobileAuthBtnText = document.getElementById('mobile-auth-btn-text');
        const mobileAuthIcon = document.getElementById('mobile-auth-icon');
        const mobileUserName = document.getElementById('mobile-user-name');
        const mobileUserRole = document.getElementById('mobile-user-role');

        if (user && user.name) {
          if (authBtn) { authBtn.textContent = 'Sair'; authBtn.className = 'bg-rose-600 hover:bg-rose-700 text-white font-medium px-3 py-1.5 rounded-lg text-xs transition'; }
          if (userDisplay) { userDisplay.classList.remove('hidden'); }
          if (userName) { userName.textContent = user.name.split(' ')[0]; }

          if (mobileAuthBtnText) { mobileAuthBtnText.textContent = 'Sair'; }
          if (mobileAuthIcon) { mobileAuthIcon.className = 'fa-solid fa-right-from-bracket'; }
          if (mobileAuthBtn) { mobileAuthBtn.className = 'w-full bg-rose-600 hover:bg-rose-700 text-white font-medium py-2.5 px-4 rounded-xl text-xs tracking-wider uppercase transition flex items-center justify-center gap-2 shadow-sm'; }
          if (mobileUserName) { mobileUserName.textContent = user.name; }
          if (mobileUserRole) { mobileUserRole.textContent = user.role === 'ADMIN' ? 'Administrador' : (user.company || 'Usuário Autenticado'); }
        } else {
          if (authBtn) { authBtn.textContent = 'Entrar'; authBtn.className = 'bg-sky-500 hover:bg-sky-600 text-white font-medium px-4 py-2 rounded-lg text-xs tracking-wider uppercase transition shadow-sm'; }
          if (userDisplay) { userDisplay.classList.add('hidden'); }

          if (mobileAuthBtnText) { mobileAuthBtnText.textContent = 'Entrar'; }
          if (mobileAuthIcon) { mobileAuthIcon.className = 'fa-solid fa-right-to-bracket'; }
          if (mobileAuthBtn) { mobileAuthBtn.className = 'w-full bg-sky-500 hover:bg-sky-600 text-white font-medium py-2.5 px-4 rounded-xl text-xs tracking-wider uppercase transition flex items-center justify-center gap-2 shadow-sm'; }
          if (mobileUserName) { mobileUserName.textContent = 'Visitante'; }
          if (mobileUserRole) { mobileUserRole.textContent = 'Perfil Público'; }
        }
      }
    };

    // Mensagem legível a partir de respostas de erro da API (inclui validações 422 do FastAPI)
    function mensagemErroApi(data, padrao = 'Não foi possível concluir a operação.') {
      if (!data) return padrao;
      if (typeof data.detail === 'string') return data.detail;
      if (Array.isArray(data.detail) && data.detail.length) {
        const erro = data.detail[0];
        const campo = Array.isArray(erro.loc) ? erro.loc[erro.loc.length - 1] : '';
        const nomes = { email: 'E-mail', password: 'Senha', new_password: 'Nova senha', name: 'Nome', token: 'Link' };
        if (erro.type === 'string_too_short' && erro.ctx && erro.ctx.min_length) {
          return `${nomes[campo] || 'Campo'}: mínimo de ${erro.ctx.min_length} caracteres.`;
        }
        if (campo === 'email') return 'Informe um e-mail válido.';
        return `${nomes[campo] || 'Campo'}: ${erro.msg}`;
      }
      return padrao;
    }

    // Botão "olho" para mostrar/ocultar senha: <button data-toggle-password="id-do-input">
    function alternarVisibilidadeSenha(botao) {
      const campo = document.getElementById(botao.dataset.togglePassword);
      if (!campo) return;
      const mostrar = campo.type === 'password';
      campo.type = mostrar ? 'text' : 'password';
      botao.setAttribute('aria-pressed', mostrar ? 'true' : 'false');
      botao.setAttribute('aria-label', mostrar ? 'Ocultar senha' : 'Mostrar senha');
      botao.title = mostrar ? 'Ocultar senha' : 'Mostrar senha';
      const icone = botao.querySelector('i');
      if (icone) icone.className = mostrar ? 'fa-regular fa-eye-slash' : 'fa-regular fa-eye';
      campo.focus({ preventScroll: true });
    }

    // Avaliação simples de força da senha (0 a 4) para orientar o usuário
    function avaliarForcaSenha(senha) {
      let pontos = 0;
      if (!senha) return { pontos: 0, rotulo: '', cor: 'bg-slate-200' };
      if (senha.length >= 8) pontos++;
      if (senha.length >= 12) pontos++;
      if (/[a-z]/.test(senha) && /[A-Z]/.test(senha)) pontos++;
      if (/\d/.test(senha) && /[^A-Za-z0-9]/.test(senha)) pontos++;
      if (senha.length < 8) pontos = Math.min(pontos, 1);
      const niveis = [
        { rotulo: 'Muito fraca', cor: 'bg-rose-500' },
        { rotulo: 'Fraca', cor: 'bg-rose-400' },
        { rotulo: 'Razoável', cor: 'bg-amber-400' },
        { rotulo: 'Boa', cor: 'bg-emerald-400' },
        { rotulo: 'Forte', cor: 'bg-emerald-600' }
      ];
      return { pontos, ...niveis[pontos] };
    }

    function ligarMedidorForca(inputId, barraId, rotuloId) {
      const campo = document.getElementById(inputId);
      const barra = document.getElementById(barraId);
      const rotulo = document.getElementById(rotuloId);
      if (!campo || !barra || !rotulo) return;
      campo.addEventListener('input', () => {
        const r = avaliarForcaSenha(campo.value);
        barra.className = `h-1.5 rounded-full transition-all duration-300 ${r.cor}`;
        barra.style.width = campo.value ? `${Math.max(10, r.pontos * 25)}%` : '0%';
        rotulo.textContent = r.rotulo;
      });
    }

    // Aviso de Caps Lock ativado em campos de senha: <p data-capslock-for="id-do-input" class="hidden">
    function ligarAvisoCapsLock() {
      document.querySelectorAll('[data-capslock-for]').forEach(aviso => {
        const campo = document.getElementById(aviso.dataset.capslockFor);
        if (!campo) return;
        const verificar = (ev) => {
          if (ev.getModifierState) aviso.classList.toggle('hidden', !ev.getModifierState('CapsLock'));
        };
        campo.addEventListener('keydown', verificar);
        campo.addEventListener('keyup', verificar);
        campo.addEventListener('blur', () => aviso.classList.add('hidden'));
      });
    }

    function handleAuthAction() {
      if (AppSession.getToken()) {
        AppSession.clear();
        showToast('Sessão encerrada com sucesso.', 'info');
        window.location.href = '/';
      } else {
        window.location.href = '/login';
      }
    }

    function showToast(msg, type = 'info') {
      const c = document.getElementById('toast-container');
      const t = document.createElement('div');
      const colors = {
        success: 'bg-emerald-600 text-white',
        error: 'bg-rose-600 text-white',
        info: 'bg-slate-800 text-white border border-slate-700'
      };
      t.className = `${colors[type] || colors.info} px-4 py-3 rounded-xl shadow-xl text-sm flex items-center gap-3 transition-all transform duration-300 ease-out pointer-events-auto`;
      t.innerHTML = `<i class="fa-solid fa-circle-info"></i> <span>${msg}</span>`;
      c.appendChild(t);
      setTimeout(() => {
        t.style.opacity = '0';
        setTimeout(() => t.remove(), 300);
      }, 4000);
    }

    // Formatação de Moeda Brasileira (R$)
    function formatCurrency(val) {
      if (val === null || val === undefined || isNaN(val)) return 'Não informado';
      return new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' }).format(val);
    }

    // Formatação de Datas Brasileiras (DD/MM/AAAA)
    function formatDate(dateStr) {
      if (!dateStr) return 'Não informado';
      try {
        const d = new Date(dateStr);
        return d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
      } catch { return dateStr; }
    }

    document.addEventListener('DOMContentLoaded', () => {
      AppSession.refreshUI();
      refreshAlertBadge();
      document.querySelectorAll('[data-toggle-password]').forEach(botao => {
        botao.addEventListener('click', () => alternarVisibilidadeSenha(botao));
      });
      ligarAvisoCapsLock();
      // Fechar drawer ao clicar em qualquer link interno do drawer
      const drawerLinks = document.querySelectorAll('#mobile-menu-drawer a');
      drawerLinks.forEach(link => {
        link.addEventListener('click', () => closeMobileMenu());
      });

      // Registro assíncrono do Service Worker para PWA (Android e navegadores compatíveis)
      if ('serviceWorker' in navigator) {
        navigator.serviceWorker.register('/sw.js', { scope: '/' })
          .then(reg => console.log('[PWA] Service Worker registrado com sucesso:', reg.scope))
          .catch(err => console.warn('[PWA] Falha ao registrar Service Worker:', err));
      }
    });
  </script>
  {% block scripts %}{% endblock %}
</body>
</html>
EOF

mkdir -p server/templates
cat << 'EOF' > server/templates/login.html
{% extends "base.html" %}

{% block title %}Entrar na Plataforma - Licitações Brasil{% endblock %}

{% block content %}
<div class="min-h-[75vh] flex items-center justify-center py-12 px-4 sm:px-6 lg:px-8">
  <div class="max-w-md w-full space-y-8 bg-white p-8 rounded-2xl border border-slate-200 shadow-xl">
    <div class="text-center">
      <div class="w-12 h-12 rounded-xl bg-gradient-to-tr from-sky-400 to-emerald-400 flex items-center justify-center text-brand-900 font-black text-2xl mx-auto shadow-inner mb-3">
        <i class="fa-solid fa-scale-balanced"></i>
      </div>
      <h2 class="text-2xl font-bold text-slate-900">Acesse sua Conta</h2>
      <p class="text-xs text-slate-500 mt-1">Pesquise, monitore e resuma editais com inteligência artificial</p>
    </div>

    <form id="login-form" class="space-y-4 text-xs" onsubmit="handleLogin(event)" novalidate>
      <div>
        <label for="email-input" class="block font-semibold text-slate-700 mb-1">E-mail Corporativo ou Pessoal</label>
        <input
          type="email"
          id="email-input"
          name="email"
          required
          autocomplete="username"
          autocapitalize="none"
          spellcheck="false"
          placeholder="seu.email@empresa.com.br"
          class="w-full p-3 border border-slate-200 rounded-xl focus:ring-2 focus:ring-sky-500 focus:outline-none"
        />
      </div>

      <div>
        <div class="flex items-center justify-between mb-1">
          <label for="password-input" class="block font-semibold text-slate-700">Senha de Acesso</label>
          <a href="/recuperar-senha" id="link-esqueci-senha" class="font-semibold text-sky-600 hover:underline">Esqueci minha senha</a>
        </div>
        <div class="relative">
          <input
            type="password"
            id="password-input"
            name="password"
            required
            autocomplete="current-password"
            placeholder="••••••••••••"
            class="w-full p-3 pr-11 border border-slate-200 rounded-xl focus:ring-2 focus:ring-sky-500 focus:outline-none"
          />
          <button
            type="button"
            data-toggle-password="password-input"
            aria-label="Mostrar senha"
            aria-pressed="false"
            title="Mostrar senha"
            class="absolute inset-y-0 right-0 px-3 flex items-center text-slate-400 hover:text-slate-700 focus:outline-none focus:text-sky-600"
          >
            <i class="fa-regular fa-eye"></i>
          </button>
        </div>
        <p data-capslock-for="password-input" class="hidden mt-1 text-[11px] font-semibold text-amber-600">
          <i class="fa-solid fa-triangle-exclamation"></i> Caps Lock está ativado.
        </p>
      </div>

      <label class="flex items-start gap-2 cursor-pointer select-none text-slate-600">
        <input type="checkbox" id="remember-input" name="remember_me" class="mt-0.5 w-4 h-4 rounded border-slate-300 accent-sky-600">
        <span>
          <span class="font-semibold text-slate-700">Manter conectado</span>
          <span class="block text-[11px] text-slate-500">Sua sessão fica ativa neste dispositivo por até {{ remember_days or 30 }} dias. Não use em computadores compartilhados.</span>
        </span>
      </label>

      <p id="login-erro" role="alert" class="hidden text-xs text-rose-700 bg-rose-50 border border-rose-200 rounded-xl px-3 py-2"></p>

      <button
        type="submit"
        id="submit-btn"
        class="w-full bg-sky-600 hover:bg-sky-500 text-white font-bold py-3 rounded-xl transition shadow-md flex items-center justify-center gap-2 disabled:opacity-60"
      >
        <span>Entrar na Plataforma</span>
        <i class="fa-solid fa-arrow-right text-xs"></i>
      </button>
    </form>

    <div class="text-center pt-4 border-t border-slate-100 text-xs text-slate-500">
      Não tem uma conta cadastrada?
      <a href="/register" class="font-bold text-sky-600 hover:underline">Criar conta gratuita</a>
    </div>
  </div>
</div>
{% endblock %}

{% block scripts %}
<script>
  const BOTAO_LOGIN_HTML = '<span>Entrar na Plataforma</span> <i class="fa-solid fa-arrow-right text-xs"></i>';

  function destinoAposLogin(user) {
    // Retorno seguro somente para caminhos internos (evita open redirect)
    const proximo = new URLSearchParams(window.location.search).get('next') || '';
    if (proximo.startsWith('/') && !proximo.startsWith('//') && !proximo.startsWith('/\\')) return proximo;
    return user && user.role === 'ADMIN' ? '/admin' : '/dashboard';
  }

  function mostrarErroLogin(msg) {
    const el = document.getElementById('login-erro');
    el.textContent = msg || '';
    el.classList.toggle('hidden', !msg);
  }

  async function handleLogin(e) {
    e.preventDefault();
    mostrarErroLogin('');
    const btn = document.getElementById('submit-btn');
    const email = document.getElementById('email-input').value.trim();
    const password = document.getElementById('password-input').value;
    const rememberMe = document.getElementById('remember-input').checked;

    if (!email || !password) {
      mostrarErroLogin('Informe e-mail e senha.');
      return;
    }

    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner animate-spin"></i> Autenticando...';

    try {
      const resp = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password, remember_me: rememberMe })
      });
      const data = await resp.json().catch(() => ({}));

      if (resp.ok) {
        try { localStorage.setItem('lb_ultimo_email', rememberMe ? email : ''); } catch {}
        AppSession.setSession(data.access_token, data.user, rememberMe);
        showToast(`Bem-vindo de volta, ${data.user.name}!`, 'success');
        setTimeout(() => { window.location.href = destinoAposLogin(data.user); }, 600);
        return;
      }
      mostrarErroLogin(mensagemErroApi(data, 'E-mail ou senha incorretos.'));
    } catch {
      mostrarErroLogin('Erro de comunicação ao autenticar. Verifique sua conexão.');
    } finally {
      btn.disabled = false;
      btn.innerHTML = BOTAO_LOGIN_HTML;
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    let ultimoEmail = '';
    try { ultimoEmail = localStorage.getItem('lb_ultimo_email') || ''; } catch {}
    if (ultimoEmail) {
      document.getElementById('email-input').value = ultimoEmail;
      document.getElementById('remember-input').checked = true;
      document.getElementById('password-input').focus();
    }
    // Leva o e-mail digitado para a tela de recuperação
    document.getElementById('link-esqueci-senha').addEventListener('click', (ev) => {
      const email = document.getElementById('email-input').value.trim();
      if (email) ev.currentTarget.href = `/recuperar-senha?email=${encodeURIComponent(email)}`;
    });
    if (new URLSearchParams(window.location.search).get('redefinida') === '1') {
      showToast('Senha redefinida. Entre com a nova senha.', 'success');
    }
  });
</script>
{% endblock %}
EOF

mkdir -p server/templates
cat << 'EOF' > server/templates/register.html
{% extends "base.html" %}

{% block title %}Criar Conta - Licitações Brasil{% endblock %}

{% block content %}
<div class="min-h-[75vh] flex items-center justify-center py-12 px-4 sm:px-6 lg:px-8">
  <div class="max-w-md w-full space-y-8 bg-white p-8 rounded-2xl border border-slate-200 shadow-xl">
    <div class="text-center">
      <div class="w-12 h-12 rounded-xl bg-gradient-to-tr from-sky-400 to-emerald-400 flex items-center justify-center text-brand-900 font-black text-2xl mx-auto shadow-inner mb-3">
        <i class="fa-solid fa-user-plus"></i>
      </div>
      <h2 class="text-2xl font-bold text-slate-900">Cadastre-se Gratuitamente</h2>
      <p class="text-xs text-slate-500 mt-1">Monitore licitações públicas, configure alertas e analise editais</p>
    </div>

    <form id="register-form" class="space-y-4 text-xs" onsubmit="handleRegister(event)">
      <div>
        <label class="block font-semibold text-slate-700 mb-1">Nome Completo</label>
        <input 
          type="text" 
          id="name-input" 
          required 
          autocomplete="name"
          placeholder="Ex: Carlos Eduardo de Oliveira" 
          class="w-full p-3 border border-slate-200 rounded-xl focus:ring-2 focus:ring-sky-500 focus:outline-none"
        />
      </div>

      <div>
        <label class="block font-semibold text-slate-700 mb-1">E-mail Profissional</label>
        <input 
          type="email" 
          id="email-input" 
          required 
          autocomplete="email"
          autocapitalize="none"
          placeholder="seu.email@empresa.com.br" 
          class="w-full p-3 border border-slate-200 rounded-xl focus:ring-2 focus:ring-sky-500 focus:outline-none"
        />
      </div>

      <div>
        <label for="password-input" class="block font-semibold text-slate-700 mb-1">Senha Segura (Mínimo 8 caracteres)</label>
        <div class="relative">
          <input 
            type="password" 
            id="password-input" 
            required 
            minlength="8"
            maxlength="128"
            autocomplete="new-password"
            placeholder="••••••••••••" 
            class="w-full p-3 pr-11 border border-slate-200 rounded-xl focus:ring-2 focus:ring-sky-500 focus:outline-none"
          />
          <button
            type="button"
            data-toggle-password="password-input"
            aria-label="Mostrar senha"
            aria-pressed="false"
            title="Mostrar senha"
            class="absolute inset-y-0 right-0 px-3 flex items-center text-slate-400 hover:text-slate-700 focus:outline-none focus:text-sky-600"
          >
            <i class="fa-regular fa-eye"></i>
          </button>
        </div>
        <div class="mt-2 flex items-center gap-2">
          <div class="flex-1 h-1.5 bg-slate-100 rounded-full overflow-hidden"><div id="forca-barra" class="h-1.5 rounded-full transition-all duration-300" style="width:0%"></div></div>
          <span id="forca-rotulo" class="text-[11px] font-semibold text-slate-500 w-20 text-right"></span>
        </div>
        <p data-capslock-for="password-input" class="hidden mt-1 text-[11px] font-semibold text-amber-600">
          <i class="fa-solid fa-triangle-exclamation"></i> Caps Lock está ativado.
        </p>
      </div>

      <div class="pt-2 text-[11px] text-slate-500">
        Ao se cadastrar, você concorda com nossos termos de privacidade sob as regras da Lei Geral de Proteção de Dados (LGPD).
      </div>

      <button 
        type="submit" 
        id="submit-btn"
        class="w-full bg-sky-600 hover:bg-sky-500 text-white font-bold py-3 rounded-xl transition shadow-md flex items-center justify-center gap-2"
      >
        <span>Criar Minha Conta</span>
        <i class="fa-solid fa-arrow-right text-xs"></i>
      </button>
    </form>

    <div class="text-center pt-4 border-t border-slate-100 text-xs text-slate-500">
      Já possui uma conta ativa? 
      <a href="/login" class="font-bold text-sky-600 hover:underline">Fazer login</a>
    </div>
  </div>
</div>
{% endblock %}

{% block scripts %}
<script>
  document.addEventListener('DOMContentLoaded', () => ligarMedidorForca('password-input', 'forca-barra', 'forca-rotulo'));

  async function handleRegister(e) {
    e.preventDefault();
    const btn = document.getElementById('submit-btn');
    const name = document.getElementById('name-input').value;
    const email = document.getElementById('email-input').value;
    const password = document.getElementById('password-input').value;

    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner animate-spin"></i> Criando conta...';

    try {
      const resp = await fetch('/api/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, email, password })
      });

      const data = await resp.json().catch(() => ({}));
      btn.disabled = false;
      btn.innerHTML = '<span>Criar Minha Conta</span> <i class="fa-solid fa-arrow-right text-xs"></i>';

      if (resp.ok) {
        AppSession.setSession(data.access_token, data.user, false);
        showToast('Conta criada com sucesso! Bem-vindo!', 'success');
        setTimeout(() => window.location.href = '/dashboard', 800);
      } else {
        showToast(mensagemErroApi(data, 'Não foi possível completar o cadastro.'), 'error');
      }
    } catch {
      btn.disabled = false;
      btn.innerHTML = '<span>Criar Minha Conta</span>';
      showToast('Erro ao comunicar com o servidor.', 'error');
    }
  }
</script>
{% endblock %}
EOF

mkdir -p server/templates
cat << 'EOF' > server/templates/recuperar_senha.html
{% extends "base.html" %}

{% block title %}Recuperar Senha - Licitações Brasil{% endblock %}

{% block content %}
<div class="min-h-[75vh] flex items-center justify-center py-12 px-4 sm:px-6 lg:px-8">
  <div class="max-w-md w-full space-y-6 bg-white p-8 rounded-2xl border border-slate-200 shadow-xl">
    <div class="text-center">
      <div class="w-12 h-12 rounded-xl bg-gradient-to-tr from-sky-400 to-emerald-400 flex items-center justify-center text-brand-900 font-black text-2xl mx-auto shadow-inner mb-3">
        <i class="fa-solid fa-key"></i>
      </div>
      <h2 class="text-2xl font-bold text-slate-900">Recuperar Senha</h2>
      <p class="text-xs text-slate-500 mt-1">Informe o e-mail da sua conta. Enviaremos um link para criar uma nova senha.</p>
    </div>

    <!-- Etapa 1: formulário -->
    <form id="form-recuperar" class="space-y-4 text-xs" onsubmit="solicitarRecuperacao(event)" novalidate>
      <div>
        <label for="email-input" class="block font-semibold text-slate-700 mb-1">E-mail cadastrado</label>
        <input
          type="email"
          id="email-input"
          required
          autocomplete="email"
          autocapitalize="none"
          spellcheck="false"
          placeholder="seu.email@empresa.com.br"
          class="w-full p-3 border border-slate-200 rounded-xl focus:ring-2 focus:ring-sky-500 focus:outline-none"
        />
      </div>

      <p id="recuperar-erro" role="alert" class="hidden text-xs text-rose-700 bg-rose-50 border border-rose-200 rounded-xl px-3 py-2"></p>

      <button
        type="submit"
        id="btn-recuperar"
        class="w-full bg-sky-600 hover:bg-sky-500 text-white font-bold py-3 rounded-xl transition shadow-md flex items-center justify-center gap-2 disabled:opacity-60"
      >
        <i class="fa-regular fa-paper-plane text-xs"></i> <span>Enviar link de redefinição</span>
      </button>
    </form>

    <!-- Etapa 2: confirmação (resposta sempre igual, exista ou não a conta) -->
    <div id="recuperar-enviado" class="hidden space-y-4 text-xs" aria-live="polite">
      <div class="p-4 rounded-xl bg-emerald-50 border border-emerald-200 text-emerald-800 leading-relaxed">
        <div class="font-bold mb-1"><i class="fa-regular fa-envelope"></i> Verifique seu e-mail</div>
        <p id="recuperar-mensagem"></p>
        <p class="mt-2 text-emerald-700">O link vale por {{ minutos_validade }} minutos e só pode ser usado uma vez.</p>
      </div>
      <div id="recuperar-dev-link" class="hidden p-3 rounded-xl bg-amber-50 border border-amber-200 text-amber-800 break-all">
        <div class="font-bold mb-1">Ambiente de desenvolvimento (e-mail simulado)</div>
        <a id="recuperar-dev-href" href="#" class="underline font-semibold">Abrir link de redefinição</a>
      </div>
      <button type="button" id="btn-reenviar" onclick="reenviar()" class="w-full bg-white border border-slate-300 hover:bg-slate-50 text-slate-700 font-semibold py-2.5 rounded-xl transition disabled:opacity-50">
        Não recebeu? Enviar novamente
      </button>
    </div>

    <div class="text-center pt-4 border-t border-slate-100 text-xs text-slate-500">
      Lembrou a senha?
      <a href="/login" class="font-bold text-sky-600 hover:underline">Voltar ao login</a>
    </div>
  </div>
</div>
{% endblock %}

{% block scripts %}
<script>
  const BOTAO_RECUPERAR_HTML = '<i class="fa-regular fa-paper-plane text-xs"></i> <span>Enviar link de redefinição</span>';

  function erroRecuperar(msg) {
    const el = document.getElementById('recuperar-erro');
    el.textContent = msg || '';
    el.classList.toggle('hidden', !msg);
  }

  async function solicitarRecuperacao(ev) {
    if (ev) ev.preventDefault();
    erroRecuperar('');
    const email = document.getElementById('email-input').value.trim();
    if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      erroRecuperar('Informe um e-mail válido.');
      return;
    }
    const btn = document.getElementById('btn-recuperar');
    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner animate-spin"></i> Enviando...';
    try {
      const resp = await fetch('/api/auth/forgot-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email })
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        erroRecuperar(mensagemErroApi(data, 'Não foi possível enviar o link agora.'));
        return;
      }
      document.getElementById('recuperar-mensagem').textContent = data.message;
      document.getElementById('form-recuperar').classList.add('hidden');
      document.getElementById('recuperar-enviado').classList.remove('hidden');
      if (data.reset_url) {
        document.getElementById('recuperar-dev-href').href = data.reset_url;
        document.getElementById('recuperar-dev-link').classList.remove('hidden');
      }
      bloquearReenvio();
    } catch {
      erroRecuperar('Erro de comunicação. Verifique sua conexão e tente novamente.');
    } finally {
      btn.disabled = false;
      btn.innerHTML = BOTAO_RECUPERAR_HTML;
    }
  }

  function bloquearReenvio() {
    const btn = document.getElementById('btn-reenviar');
    let restante = 60;
    btn.disabled = true;
    const tick = () => {
      if (restante <= 0) {
        btn.disabled = false;
        btn.textContent = 'Não recebeu? Enviar novamente';
        return;
      }
      btn.textContent = `Enviar novamente em ${restante}s`;
      restante--;
      setTimeout(tick, 1000);
    };
    tick();
  }

  function reenviar() {
    document.getElementById('recuperar-enviado').classList.add('hidden');
    document.getElementById('form-recuperar').classList.remove('hidden');
    solicitarRecuperacao(null);
  }

  document.addEventListener('DOMContentLoaded', () => {
    const email = new URLSearchParams(window.location.search).get('email');
    if (email) document.getElementById('email-input').value = email;
  });
</script>
{% endblock %}
EOF

mkdir -p server/templates
cat << 'EOF' > server/templates/redefinir_senha.html
{% extends "base.html" %}

{% block title %}Redefinir Senha - Licitações Brasil{% endblock %}

{% block content %}
<div class="min-h-[75vh] flex items-center justify-center py-12 px-4 sm:px-6 lg:px-8">
  <div class="max-w-md w-full space-y-6 bg-white p-8 rounded-2xl border border-slate-200 shadow-xl">
    <div class="text-center">
      <div class="w-12 h-12 rounded-xl bg-gradient-to-tr from-sky-400 to-emerald-400 flex items-center justify-center text-brand-900 font-black text-2xl mx-auto shadow-inner mb-3">
        <i class="fa-solid fa-lock-open"></i>
      </div>
      <h2 class="text-2xl font-bold text-slate-900">Criar Nova Senha</h2>
      <p class="text-xs text-slate-500 mt-1">Defina uma senha nova para a sua conta.</p>
    </div>

    {% if token_valido %}
    <form id="form-redefinir" class="space-y-4 text-xs" onsubmit="redefinirSenha(event)" novalidate>
      <input type="hidden" id="token-input" value="{{ token }}">
      <!-- Ajuda gerenciadores de senha a associar a nova senha à conta -->
      <input type="text" name="username" autocomplete="username" class="hidden" aria-hidden="true" tabindex="-1">

      <div>
        <label for="nova-senha" class="block font-semibold text-slate-700 mb-1">Nova senha (mínimo 8 caracteres)</label>
        <div class="relative">
          <input type="password" id="nova-senha" required minlength="8" maxlength="128" autocomplete="new-password"
                 placeholder="••••••••••••"
                 class="w-full p-3 pr-11 border border-slate-200 rounded-xl focus:ring-2 focus:ring-sky-500 focus:outline-none" />
          <button type="button" data-toggle-password="nova-senha" aria-label="Mostrar senha" aria-pressed="false" title="Mostrar senha"
                  class="absolute inset-y-0 right-0 px-3 flex items-center text-slate-400 hover:text-slate-700 focus:outline-none focus:text-sky-600">
            <i class="fa-regular fa-eye"></i>
          </button>
        </div>
        <div class="mt-2 flex items-center gap-2">
          <div class="flex-1 h-1.5 bg-slate-100 rounded-full overflow-hidden"><div id="forca-barra" class="h-1.5 rounded-full transition-all duration-300" style="width:0%"></div></div>
          <span id="forca-rotulo" class="text-[11px] font-semibold text-slate-500 w-20 text-right"></span>
        </div>
        <p data-capslock-for="nova-senha" class="hidden mt-1 text-[11px] font-semibold text-amber-600">
          <i class="fa-solid fa-triangle-exclamation"></i> Caps Lock está ativado.
        </p>
      </div>

      <div>
        <label for="confirmar-senha" class="block font-semibold text-slate-700 mb-1">Confirmar nova senha</label>
        <div class="relative">
          <input type="password" id="confirmar-senha" required minlength="8" maxlength="128" autocomplete="new-password"
                 placeholder="••••••••••••"
                 class="w-full p-3 pr-11 border border-slate-200 rounded-xl focus:ring-2 focus:ring-sky-500 focus:outline-none" />
          <button type="button" data-toggle-password="confirmar-senha" aria-label="Mostrar senha" aria-pressed="false" title="Mostrar senha"
                  class="absolute inset-y-0 right-0 px-3 flex items-center text-slate-400 hover:text-slate-700 focus:outline-none focus:text-sky-600">
            <i class="fa-regular fa-eye"></i>
          </button>
        </div>
      </div>

      <p id="redefinir-erro" role="alert" class="hidden text-xs text-rose-700 bg-rose-50 border border-rose-200 rounded-xl px-3 py-2"></p>

      <button type="submit" id="btn-redefinir"
              class="w-full bg-sky-600 hover:bg-sky-500 text-white font-bold py-3 rounded-xl transition shadow-md flex items-center justify-center gap-2 disabled:opacity-60">
        <i class="fa-solid fa-check text-xs"></i> <span>Salvar nova senha</span>
      </button>
    </form>
    {% else %}
    <div id="token-invalido" class="space-y-4 text-xs">
      <div class="p-4 rounded-xl bg-rose-50 border border-rose-200 text-rose-800 leading-relaxed">
        <div class="font-bold mb-1"><i class="fa-solid fa-link-slash"></i> Link inválido ou expirado</div>
        Por segurança, cada link de redefinição vale por pouco tempo e pode ser usado uma única vez.
      </div>
      <a href="/recuperar-senha" class="w-full inline-flex justify-center bg-sky-600 hover:bg-sky-500 text-white font-bold py-3 rounded-xl transition shadow-md">
        Solicitar novo link
      </a>
    </div>
    {% endif %}

    <div class="text-center pt-4 border-t border-slate-100 text-xs text-slate-500">
      <a href="/login" class="font-bold text-sky-600 hover:underline">Voltar ao login</a>
    </div>
  </div>
</div>
{% endblock %}

{% block scripts %}
<script>
  function erroRedefinir(msg) {
    const el = document.getElementById('redefinir-erro');
    if (!el) return;
    el.textContent = msg || '';
    el.classList.toggle('hidden', !msg);
  }

  async function redefinirSenha(ev) {
    ev.preventDefault();
    erroRedefinir('');
    const token = document.getElementById('token-input').value;
    const nova = document.getElementById('nova-senha').value;
    const confirmar = document.getElementById('confirmar-senha').value;

    if (nova.length < 8) return erroRedefinir('A nova senha precisa ter pelo menos 8 caracteres.');
    if (nova !== confirmar) return erroRedefinir('As senhas não coincidem.');

    const btn = document.getElementById('btn-redefinir');
    const original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner animate-spin"></i> Salvando...';
    try {
      const resp = await fetch('/api/auth/reset-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token, new_password: nova })
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        erroRedefinir(mensagemErroApi(data, 'Não foi possível redefinir a senha.'));
        return;
      }
      AppSession.clear();
      window.location.href = '/login?redefinida=1';
    } catch {
      erroRedefinir('Erro de comunicação. Tente novamente.');
    } finally {
      btn.disabled = false;
      btn.innerHTML = original;
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    ligarMedidorForca('nova-senha', 'forca-barra', 'forca-rotulo');
    // Remove o token da barra de endereço/histórico após carregar a página
    if (window.location.search.includes('token=')) {
      window.history.replaceState({}, '', '/redefinir-senha');
    }
  });
</script>
{% endblock %}
EOF

cat << 'EOF' > .env.example
# ==============================================================================
# LICITAÇÕES BRASIL - PLATAFORMA INTELIGENTE DE PESQUISA DE LICITAÇÕES PÚBLICAS
# ARQUIVO DE CONFIGURAÇÃO DE VARIÁVEIS DE AMBIENTE (.env.example)
# ==============================================================================

# ------------------------------------------------------------------------------
# 1. BANCO DE DADOS (PostgreSQL / Prisma / SQLite)
# ------------------------------------------------------------------------------
# URL de conexão com o banco PostgreSQL de produção
# Formato: postgresql://USER:PASSWORD@HOST:PORT/DATABASE?schema=public
DATABASE_URL="postgresql://licitacoes_user:licitacoes_secret_pass@localhost:5432/licitacoes_db?schema=public"

# URL direta para migrations em ambientes com pooling (ex: Supabase, PgBouncer, Neon)
DIRECT_URL="postgresql://licitacoes_user:licitacoes_secret_pass@localhost:5432/licitacoes_db?schema=public"

# SQLite alternativo para desenvolvimento local e testes sem Docker
SQLITE_URL="sqlite:///./dev_licitacoes.db"

# ------------------------------------------------------------------------------
# 2. AUTENTICAÇÃO E SESSÃO
# ------------------------------------------------------------------------------
# Chave secreta criptográfica para assinatura de tokens JWT e sessões seguras
# Gere uma chave segura com: openssl rand -base64 32
AUTH_SECRET="substitua_esta_chave_por_um_hash_aleatorio_muito_seguro_de_32_bytes"
JWT_ALGORITHM="HS256"
ACCESS_TOKEN_EXPIRE_MINUTES=1440

# URL pública da aplicação para redirecionamentos e links de e-mail
APP_URL="http://localhost:8000"

# ------------------------------------------------------------------------------
# 3. INTELIGÊNCIA ARTIFICIAL DOCUMENTAL (GEMINI, CLAUDE, OPENAI)
# ------------------------------------------------------------------------------
# Provedor padrão: GEMINI, ANTHROPIC ou OPENAI
AI_PROVIDER="GEMINI"

# Chave de API para o provedor escolhido
# Para Gemini: Deixe vazio caso esteja em ambiente Google Cloud / AI Studio com proxy de vaulting automático
AI_API_KEY=""

# Modelo de IA a ser utilizado para análise de editais
# Recomendações: gemini-3.8-flash, gemini-2.5-pro, claude-3-7-sonnet-20250219
AI_MODEL="gemini-3.8-flash"

# Limites de processamento de IA
AI_MAX_PAGES_PER_DOC=50
AI_MAX_CHUNK_TOKENS=3500

# ------------------------------------------------------------------------------
# 4. REDIS (CACHE E FILAS - OPCIONAL)
# ------------------------------------------------------------------------------
# Conexão com Redis para cache de buscas frequentes e filas assíncronas (BullMQ / Celery)
REDIS_URL="redis://localhost:6379/0"

# ------------------------------------------------------------------------------
# 5. ARMAZENAMENTO DE DOCUMENTOS (S3 / COMPATÍVEL / LOCAL)
# ------------------------------------------------------------------------------
# Provedor: S3, MINIO, CLOUDFLARE_R2 ou LOCAL
STORAGE_TYPE="LOCAL"
STORAGE_LOCAL_PATH="./storage/documents"
STORAGE_ENDPOINT="https://s3.us-east-1.amazonaws.com"
STORAGE_BUCKET="licitacoes-documentos"
STORAGE_ACCESS_KEY=""
STORAGE_SECRET_KEY=""
STORAGE_REGION="us-east-1"

# ------------------------------------------------------------------------------
# 6. SERVIÇO DE E-MAIL E ALERTAS
# ------------------------------------------------------------------------------
# Provedor: SMTP, RESEND ou MOCK (MOCK apenas registra o e-mail no log)
EMAIL_PROVIDER="MOCK"
EMAIL_API_KEY=""
EMAIL_FROM="alertas@licitacoesbrasil.gov.br"
SMTP_HOST="smtp.example.com"
SMTP_PORT=587
SMTP_USER=""
SMTP_PASSWORD=""
# true para SSL implícito (porta 465); false usa STARTTLS (porta 587)
SMTP_USE_SSL="false"

# Autenticação: "Manter conectado" e recuperação de senha
# APP_URL é a base dos links enviados por e-mail (ex.: https://seu-app.onrender.com)
REMEMBER_ME_DAYS=30
PASSWORD_RESET_TOKEN_MINUTES=30
# Somente desenvolvimento: devolve o link de redefinição na API quando EMAIL_PROVIDER=MOCK
AUTH_EXPOSE_RESET_LINK="false"

# ------------------------------------------------------------------------------
# 7. INTEGRAÇÃO PNCP (PORTAL NACIONAL DE CONTRATAÇÕES PÚBLICAS)
# ------------------------------------------------------------------------------
# API de integração (itens e arquivos de cada compra) e limite de download por anexo
PNCP_INTEGRACAO_BASE_URL="https://pncp.gov.br/api/pncp/v1"
PNCP_DOWNLOAD_MAX_MB=100
# URL base oficial da API de consulta do PNCP
PNCP_API_BASE_URL="https://pncp.gov.br/api/consulta"

# Taxa de requisições por minuto permitida para evitar bloqueios
PNCP_RATE_LIMIT_PER_MINUTE=60
PNCP_REQUEST_TIMEOUT_SECONDS=15
PNCP_MAX_RETRIES=3

# ------------------------------------------------------------------------------
# 8. SEGURANÇA E OBSERVABILIDADE
# ------------------------------------------------------------------------------
# Nível de log: DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_LEVEL="INFO"
RATE_LIMIT_REQUESTS_PER_MINUTE=120
CORS_ALLOWED_ORIGINS="http://localhost:3000,http://localhost:8000"

# ------------------------------------------------------------------------------
# 9. CONECTOR LICITAÇÕES-E (BANCO DO BRASIL)
# ------------------------------------------------------------------------------
# Ativação do conector ("true" ou "false")
LICITACOES_E_ACTIVE="false"

# Modo Mock para testes e desenvolvimento local sem credenciais reais
LICITACOES_E_MOCK_MODE="true"
LICITACOES_E_MOCK_TOKEN="mock_bearer_token_local_dev"

# Credenciais oficiais de produção / sandbox (Portal Developers BB)
BB_DEVELOPER_APP_KEY=""
BB_CLIENT_SECRET=""
LICITACOES_E_BASE_URL="https://www.licitacoes-e.com.br"
EOF

# ==============================================================================
# MELHORIA 2 — FILTROS: categoria Material / Serviço / Outros e as 27 UFs
# ==============================================================================
mkdir -p server
cat << 'EOF' > server/search_engine.py
import unicodedata
import re
from typing import List, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, desc, asc
from server.models import Tender, TenderItem, Source, Favorite
from server.schemas import SearchFilterParams, TenderSummaryResponse, SearchResultResponse
from server.connectors.pncp import build_canonical_pncp_url

# Categorias oficiais de classificação
CATEGORIES = [
    "Medicamentos",
    "Materiais hospitalares",
    "Materiais odontológicos",
    "Equipamentos",
    "Serviços",
    "Tecnologia",
    "Obras",
    "Alimentação",
    "Limpeza",
    "Outros"
]

# As 27 unidades federativas (26 estados + Distrito Federal), em ordem alfabética de sigla
UFS_BRASIL = [
    ("AC", "Acre"), ("AL", "Alagoas"), ("AM", "Amazonas"), ("AP", "Amapá"), ("BA", "Bahia"),
    ("CE", "Ceará"), ("DF", "Distrito Federal"), ("ES", "Espírito Santo"), ("GO", "Goiás"),
    ("MA", "Maranhão"), ("MG", "Minas Gerais"), ("MS", "Mato Grosso do Sul"), ("MT", "Mato Grosso"),
    ("PA", "Pará"), ("PB", "Paraíba"), ("PE", "Pernambuco"), ("PI", "Piauí"), ("PR", "Paraná"),
    ("RJ", "Rio de Janeiro"), ("RN", "Rio Grande do Norte"), ("RO", "Rondônia"), ("RR", "Roraima"),
    ("RS", "Rio Grande do Sul"), ("SC", "Santa Catarina"), ("SE", "Sergipe"), ("SP", "São Paulo"),
    ("TO", "Tocantins"),
]
SIGLAS_UF = {sigla for sigla, _ in UFS_BRASIL}

# Filtro simplificado de categoria (natureza do objeto)
NATUREZAS_FILTRO = [("MATERIAL", "Material"), ("SERVICO", "Serviço"), ("OUTROS", "Outros")]
_NATUREZAS_VALIDAS = {codigo for codigo, _ in NATUREZAS_FILTRO}
_ALIASES_NATUREZA = {
    "MATERIAL": "MATERIAL", "MATERIAIS": "MATERIAL", "M": "MATERIAL",
    "SERVICO": "SERVICO", "SERVIÇO": "SERVICO", "SERVICOS": "SERVICO", "SERVIÇOS": "SERVICO", "S": "SERVICO",
    "OUTROS": "OUTROS", "OUTRO": "OUTROS",
}


def normalizar_natureza(valor: Optional[str]) -> Optional[str]:
    """Converte 'Material', 'serviço', 'SERVICOS'... no código MATERIAL / SERVICO / OUTROS (ou None)."""
    if not valor:
        return None
    return _ALIASES_NATUREZA.get(valor.strip().upper())


def normalizar_uf(valor: Optional[str]) -> Optional[str]:
    if not valor:
        return None
    sigla = valor.strip().upper()
    return sigla if sigla in SIGLAS_UF else None


UF_TO_REGION = {
    "AC": "Norte", "AP": "Norte", "AM": "Norte", "PA": "Norte", "RO": "Norte", "RR": "Norte", "TO": "Norte",
    "AL": "Nordeste", "BA": "Nordeste", "CE": "Nordeste", "MA": "Nordeste", "PB": "Nordeste",
    "PE": "Nordeste", "PI": "Nordeste", "RN": "Nordeste", "SE": "Nordeste",
    "DF": "Centro-Oeste", "GO": "Centro-Oeste", "MT": "Centro-Oeste", "MS": "Centro-Oeste",
    "ES": "Sudeste", "MG": "Sudeste", "RJ": "Sudeste", "SP": "Sudeste",
    "PR": "Sul", "RS": "Sul", "SC": "Sul"
}

def normalize_text(text: str) -> str:
    """Remove acentos, converte para minúsculas e remove pontuações excessivas"""
    if not text:
        return ""
    # Decomposição canônica e remoção de marcas diacríticas
    nfkd = unicodedata.normalize('NFKD', text)
    cleaned = "".join([c for c in nfkd if not unicodedata.combining(c)])
    return cleaned.lower().strip()

def parse_search_query(query_str: str) -> Tuple[List[str], List[str]]:
    """
    Separa a busca em:
    - expressões exatas (entre aspas duplas: "materiais hospitalares")
    - termos individuais
    """
    if not query_str:
        return [], []
    
    # Extrair frases exatas
    exact_phrases = re.findall(r'"([^"]*)"', query_str)
    # Remover as frases da query principal
    remaining = re.sub(r'"[^"]*"', '', query_str)
    
    # Termos soltos
    terms = [normalize_text(t) for t in remaining.split() if len(t.strip()) > 1]
    normalized_exact = [normalize_text(p) for p in exact_phrases if p.strip()]
    
    return normalized_exact, terms

def calculate_relevance_score(tender: Tender, exact_phrases: List[str], terms: List[str]) -> float:
    """
    Calcula pontuação de relevância de 0.0 a 100.0:
    - Coincidência no título: peso 40
    - Coincidência no objeto: peso 35
    - Coincidência no órgão: peso 15
    - Coincidência nos itens: peso 10
    """
    if not exact_phrases and not terms:
        return 1.0

    score = 0.0
    norm_title = normalize_text(tender.title)
    norm_obj = normalize_text(tender.object_description)
    norm_entity = normalize_text(tender.contracting_entity)
    
    # Coletar texto dos itens
    items_text = " ".join([normalize_text(i.description) for i in (tender.items or [])])

    for phrase in exact_phrases:
        if phrase in norm_title: score += 40.0
        if phrase in norm_obj: score += 30.0
        if phrase in norm_entity: score += 15.0
        if phrase in items_text: score += 15.0

    for term in terms:
        if term in norm_title: score += 15.0
        if term in norm_obj: score += 10.0
        if term in norm_entity: score += 5.0
        if term in items_text: score += 5.0

    return min(score, 100.0)

def search_tenders(db: Session, params: SearchFilterParams, user_id: Optional[str] = None) -> SearchResultResponse:
    """
    Mecanismo completo de busca com normalização de acentos,
    expressões exatas, múltiplos termos e filtros combináveis.
    """
    query = db.query(Tender)

    # 1. Filtros Estruturados
    uf_sel = (params.state or params.uf or "").strip().upper()
    if uf_sel:
        query = query.filter(Tender.state == uf_sel)

    if params.city:
        query = query.filter(Tender.city.ilike(f"%{params.city.strip()}%"))

    if params.region:
        query = query.filter(Tender.region.ilike(f"%{params.region.strip()}%"))

    if params.contracting_entity:
        query = query.filter(Tender.contracting_entity.ilike(f"%{params.contracting_entity.strip()}%"))

    if params.administrative_sphere:
        query = query.filter(Tender.administrative_sphere == params.administrative_sphere.upper().strip())

    if params.modality:
        m_term = params.modality.strip()
        if "dispensa" in m_term.lower():
            query = query.filter(Tender.modality.ilike("%Dispensa%"))
        else:
            query = query.filter(Tender.modality.ilike(f"%{m_term}%"))

    if params.status:
        query = query.filter(Tender.status.ilike(f"%{params.status.strip()}%"))

    # Categoria simplificada (MATERIAL / SERVICO / OUTROS); valores legados seguem filtrando Tender.category
    natureza_sel = normalizar_natureza(params.natureza) or normalizar_natureza(params.category)
    if natureza_sel:
        query = query.filter(Tender.natureza_objeto == natureza_sel)
    elif params.category:
        query = query.filter(Tender.category.ilike(f"%{params.category.strip()}%"))

    if params.source_system:
        query = query.filter(Tender.source_system == params.source_system.strip().lower())

    if params.portal:
        p_term = params.portal.strip()
        if p_term.lower() in ("compras.gov.br", "compras.gov", "compras_gov", "comprasnet"):
            query = query.filter(
                or_(
                    Tender.source_system == "compras_gov",
                    Tender.sicaf_ready == True,
                    Tender.raw_data_reference.ilike("%Compras.gov.br%")
                )
            )
        elif p_term.lower() in ("pncp geral", "pncp"):
            query = query.filter(
                and_(
                    Tender.source_system == "pncp",
                    ~Tender.raw_data_reference.ilike("%Compras.gov.br%")
                )
            )
        else:
            query = query.filter(
                or_(
                    Tender.raw_data_reference.ilike(f"%{p_term}%"),
                    Tender.official_notice_url.ilike(f"%{p_term}%"),
                    Tender.source_url.ilike(f"%{p_term}%")
                )
            )

    if params.uasg_code:
        query = query.filter(Tender.uasg_code == params.uasg_code.strip())

    if params.sicaf_ready is not None:
        query = query.filter(Tender.sicaf_ready == params.sicaf_ready)

    if params.value_min is not None:
        query = query.filter(Tender.estimated_total_value >= params.value_min)

    if params.value_max is not None:
        query = query.filter(Tender.estimated_total_value <= params.value_max)

    if params.date_from:
        try:
            query = query.filter(Tender.publication_date >= f"{params.date_from} 00:00:00")
        except Exception:
            pass

    if params.date_to:
        try:
            query = query.filter(Tender.publication_date <= f"{params.date_to} 23:59:59")
        except Exception:
            pass

    # 2. Busca Textual por Palavra-chave / Expressão Exata
    exact_phrases, terms = [], []
    if params.q:
        exact_phrases, terms = parse_search_query(params.q)
        text_conditions = []
        
        for phrase in exact_phrases:
            text_conditions.append(Tender.title.ilike(f"%{phrase}%"))
            text_conditions.append(Tender.object_description.ilike(f"%{phrase}%"))
            text_conditions.append(Tender.contracting_entity.ilike(f"%{phrase}%"))
            text_conditions.append(Tender.items.any(TenderItem.description.ilike(f"%{phrase}%")))

        for term in terms:
            text_conditions.append(Tender.title.ilike(f"%{term}%"))
            text_conditions.append(Tender.object_description.ilike(f"%{term}%"))
            text_conditions.append(Tender.contracting_entity.ilike(f"%{term}%"))
            text_conditions.append(Tender.process_number.ilike(f"%{term}%"))
            text_conditions.append(Tender.notice_number.ilike(f"%{term}%"))
            text_conditions.append(Tender.items.any(TenderItem.description.ilike(f"%{term}%")))

        if text_conditions:
            query = query.filter(or_(*text_conditions))

    # Obter lista de favoritos do usuário logado
    user_fav_ids = set()
    if user_id:
        favs = db.query(Favorite.tender_id).filter(Favorite.user_id == user_id).all()
        user_fav_ids = {f[0] for f in favs}

    # Carregar dados
    all_matched = query.all()

    # 3. Cálculo de Relevância e Ordenação
    scored_items: List[Tuple[Tender, float]] = []
    for t in all_matched:
        score = calculate_relevance_score(t, exact_phrases, terms)
        scored_items.append((t, score))

    if params.sort_by == "relevance" and (exact_phrases or terms):
        scored_items.sort(key=lambda x: x[1], reverse=True)
    elif params.sort_by == "pub_desc":
        scored_items.sort(key=lambda x: x[0].publication_date or x[0].created_at, reverse=True)
    elif params.sort_by == "pub_asc":
        scored_items.sort(key=lambda x: x[0].publication_date or x[0].created_at, reverse=False)
    elif params.sort_by == "deadline_asc":
        scored_items.sort(key=lambda x: x[0].proposal_deadline or x[0].opening_date or x[0].created_at, reverse=False)
    elif params.sort_by == "deadline_desc":
        scored_items.sort(key=lambda x: x[0].proposal_deadline or x[0].opening_date or x[0].created_at, reverse=True)
    elif params.sort_by == "value_desc":
        scored_items.sort(key=lambda x: x[0].estimated_total_value or 0.0, reverse=True)
    elif params.sort_by == "value_asc":
        scored_items.sort(key=lambda x: x[0].estimated_total_value or 0.0, reverse=False)
    else:
        # Default order by publication date desc
        scored_items.sort(key=lambda x: x[0].publication_date or x[0].created_at, reverse=True)

    total = len(scored_items)
    effective_page_size = params.limit if params.limit is not None else params.page_size
    effective_page_size = max(1, min(effective_page_size, 100))
    total_pages = max(1, (total + effective_page_size - 1) // effective_page_size)
    start_idx = (params.page - 1) * effective_page_size
    end_idx = start_idx + effective_page_size
    page_slice = scored_items[start_idx:end_idx]

    results = []
    for t, score in page_slice:
        source_name = t.source.name if t.source else "PNCP"
        executor_portal = None
        if t.raw_data_reference:
            try:
                import json
                ref_json = json.loads(t.raw_data_reference)
                if isinstance(ref_json, dict):
                    executor_portal = ref_json.get("executor_portal")
            except Exception:
                pass
        if not executor_portal:
            executor_portal = "Compras.gov.br" if (t.source_system == "compras_gov" or t.sicaf_ready) else "PNCP Geral"

        results.append(TenderSummaryResponse(
            id=t.id,
            external_id=t.external_id,
            process_number=t.process_number or "Não informado",
            notice_number=t.notice_number or "Não informado",
            title=t.title,
            object_description=t.object_description,
            contracting_entity=t.contracting_entity,
            purchasing_unit=t.purchasing_unit or "Não informado",
            state=t.state,
            city=t.city or "Não informado",
            administrative_sphere=t.administrative_sphere,
            modality=t.modality,
            category=t.category or "Outros",
            natureza_objeto=t.natureza_objeto,
            status=t.status or "Não informado",
            publication_date=t.publication_date,
            opening_date=t.opening_date,
            proposal_deadline=t.proposal_deadline,
            estimated_total_value=t.estimated_total_value,
            source_name=source_name,
            source_url=t.source_url,
            official_notice_url=t.official_notice_url,
            source_system=t.source_system or "pncp",
            uasg_code=t.uasg_code,
            sicaf_ready=bool(t.sicaf_ready),
            link_comprasnet=t.link_comprasnet,
            link_pncp=(
                build_canonical_pncp_url(numero_controle_pncp=getattr(t, "url_pncp", None) or t.link_pncp or t.source_url or t.external_id)
                if (getattr(t, "url_pncp", None) or t.link_pncp or t.source_url or t.external_id)
                else (getattr(t, "url_pncp", None) or t.link_pncp or t.source_url)
            ),
            url_disputa=getattr(t, "url_disputa", None) or t.link_comprasnet or t.official_notice_url or (
                build_canonical_pncp_url(numero_controle_pncp=getattr(t, "url_pncp", None) or t.link_pncp or t.source_url or t.external_id)
                if (getattr(t, "url_pncp", None) or t.link_pncp or t.source_url or t.external_id)
                else (getattr(t, "url_pncp", None) or t.link_pncp or t.source_url)
            ),
            url_pncp=(
                build_canonical_pncp_url(numero_controle_pncp=getattr(t, "url_pncp", None) or t.link_pncp or t.source_url or t.external_id)
                if (getattr(t, "url_pncp", None) or t.link_pncp or t.source_url or t.external_id)
                else (getattr(t, "url_pncp", None) or t.link_pncp or t.source_url)
            ),
            executor_portal=executor_portal,
            data_completeness=t.data_completeness,
            is_favorite=(t.id in user_fav_ids),
            match_score=round(score, 1) if (exact_phrases or terms) else None,
            created_at=t.created_at,
            last_synced_at=t.last_synced_at
        ))

    return SearchResultResponse(
        total=total,
        page=params.page,
        page_size=effective_page_size,
        total_pages=total_pages,
        results=results,
        applied_filters=params.model_dump(exclude_none=True)
    )
EOF

mkdir -p server/templates
cat << 'EOF' > server/templates/search.html
{% extends "base.html" %}

{% block title %}Pesquisa de Editais e Oportunidades - Licitações Brasil{% endblock %}

{% block content %}
<div class="bg-[#0b0f19] text-slate-100 min-h-screen">
  <!-- Top Hero Bar (Dark Licitações Brasil) -->
  <div class="bg-[#0f172a] py-6 border-b border-slate-800">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
      <div class="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 class="text-2xl font-bold text-white flex items-center gap-2.5">
            <i class="fa-solid fa-magnifying-glass text-sky-400"></i> Consulta de Contratações Públicas
          </h1>
          <p class="text-xs text-slate-400 mt-1">
            Pipeline integrado com prioridade Compras.gov.br (SICAF), catálogo oficial PNCP e análise documental inteligente.
          </p>
        </div>

        <!-- Quick Action: Save Search as Alert & Alerts Feed Indicator -->
        <div class="flex items-center gap-2">
          <!-- Discreto indicador de feed de alertas com contagem -->
          <a href="/alerts" id="alerts-feed-badge" class="hidden items-center gap-1.5 bg-sky-950/70 hover:bg-sky-900/90 text-sky-300 border border-sky-800/80 font-semibold px-3 py-2 rounded-xl text-xs transition shadow-sm" title="Oportunidades recentes correspondentes aos seus alertas">
            <span class="relative flex h-2 w-2">
              <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-sky-400 opacity-75"></span>
              <span class="relative inline-flex rounded-full h-2 w-2 bg-sky-500"></span>
            </span>
            <i class="fa-solid fa-bell text-sky-400"></i>
            <span id="alerts-feed-count" class="font-bold">0</span> <span class="hidden sm:inline">oportunidades</span>
          </a>

          <button onclick="openSaveSearchModal()" class="bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 font-semibold px-3.5 py-2 rounded-xl text-xs transition shadow-sm flex items-center gap-1.5 cursor-pointer">
            <i class="fa-regular fa-bell text-sky-400"></i> Criar Alerta desta Busca
          </button>
        </div>
      </div>

      <!-- Quick Filter Pills: Estados Prioritários -->
      <div class="mt-4 pt-3 border-t border-slate-800/80 flex items-center gap-2 flex-wrap text-xs">
        <span class="text-slate-400 font-medium text-[11px] flex items-center gap-1.5">
          <i class="fa-solid fa-bolt text-amber-400"></i> Estados:
        </span>
        {% set priority_states = [('Todos', ''), ('SP', 'SP'), ('RJ', 'RJ'), ('MG', 'MG'), ('DF', 'DF'), ('BA', 'BA'), ('AL', 'AL'), ('PR', 'PR'), ('RS', 'RS')] %}
        {% for label, uf in priority_states %}
          {% set is_active = ((params.state or params.uf) == uf if uf else not (params.state or params.uf)) %}
          {% set qs_uf = {} %}
          {% for chave, valor in [('q', params.q), ('modality', params.modality), ('status', params.status), ('category', params.natureza or params.category), ('uf', uf)] %}
            {% if valor %}{% set _ = qs_uf.update({chave: valor}) %}{% endif %}
          {% endfor %}
          <a 
            href="/search?{{ qs_uf|urlencode }}"
            class="px-2.5 py-1 rounded-lg font-semibold transition text-[11px] {% if is_active %}bg-sky-600 text-white shadow-sm shadow-sky-600/30{% else %}bg-slate-800/90 hover:bg-slate-700 text-slate-300 hover:text-white border border-slate-700/80{% endif %}"
          >
            {{ label }}
          </a>
        {% endfor %}
      </div>
    </div>
  </div>

  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
    <div class="grid grid-cols-1 lg:grid-cols-4 gap-8">
      
      <!-- Sidebar Filters Form (Dark Theme) -->
      <aside class="lg:col-span-1">
        <form id="filters-form" method="GET" action="/search" class="bg-[#111827] p-5 rounded-2xl border border-slate-800 shadow-xl space-y-5">
          <input type="hidden" name="page_size" id="hidden-page-size" value="{{ params.page_size }}" />
          <div class="flex justify-between items-center pb-3 border-b border-slate-800">
            <span class="font-bold text-slate-100 text-sm flex items-center gap-1.5">
              <i class="fa-solid fa-sliders text-sky-400"></i> Filtros Combináveis
            </span>
            <a href="/search" class="text-xs text-rose-400 hover:text-rose-300 hover:underline">Limpar</a>
          </div>

          <!-- Palavra-chave -->
          <div>
            <div class="flex items-center justify-between mb-1">
              <label class="block text-xs font-semibold text-slate-300">Palavra-chave / Objeto</label>
              <button 
                type="button" 
                id="btn-quick-save-alert"
                onclick="saveSearchAlert()" 
                class="inline-flex items-center gap-1 text-[11px] font-semibold text-sky-400 hover:text-sky-300 transition py-0.5 px-1.5 rounded hover:bg-sky-950/40 cursor-pointer"
                title="Guardar este Alerta com as palavras-chave e filtros atuais"
              >
                <i class="fa-regular fa-bell"></i> Guardar este Alerta
              </button>
            </div>
            <div class="relative">
              <input 
                type="text" 
                name="q" 
                value="{{ params.q or '' }}" 
                placeholder="Ex: medicamentos, TI, vigilância..." 
                class="w-full text-xs p-2.5 pl-8 bg-slate-900 border border-slate-700 text-slate-100 placeholder-slate-500 rounded-xl focus:ring-2 focus:ring-sky-500 focus:outline-none"
              />
              <i class="fa-solid fa-magnifying-glass absolute left-2.5 top-3 text-slate-500 text-xs"></i>
            </div>
            <span class="text-[10px] text-slate-500 mt-1 block">Use aspas para expressão exata: "material hospitalar"</span>
          </div>

          <!-- Estado / UF -->
          <div>
            <label class="block text-xs font-semibold text-slate-300 mb-1">Estado (UF)</label>
            <select name="uf" id="uf-select" class="w-full text-xs p-2.5 bg-slate-900 border border-slate-700 text-slate-100 rounded-xl focus:ring-2 focus:ring-sky-500 focus:outline-none">
              <option value="">Todos os Estados (UF)</option>
              {% set uf_atual = (params.state or params.uf or '')|upper %}
              {% for sigla, nome in UFS_BRASIL %}
              <option value="{{ sigla }}" {% if uf_atual == sigla %}selected{% endif %}>{{ sigla }} - {{ nome }}</option>
              {% endfor %}
            </select>
          </div>

          <!-- Município -->
          <div>
            <label class="block text-xs font-semibold text-slate-300 mb-1">Município</label>
            <input 
              type="text" 
              name="city" 
              value="{{ params.city or '' }}" 
              placeholder="Ex: Brasília, Maceió, São Paulo..." 
              class="w-full text-xs p-2.5 bg-slate-900 border border-slate-700 text-slate-100 placeholder-slate-500 rounded-xl focus:ring-2 focus:ring-sky-500 focus:outline-none"
            />
          </div>

          <!-- Categoria (estritamente Material / Serviço / Outros) -->
          <div>
            <label class="block text-xs font-semibold text-slate-300 mb-1">Categoria</label>
            {% set natureza_atual = (params.natureza or params.category or '')|upper %}
            <select name="category" id="categoria-select" class="w-full text-xs p-2.5 bg-slate-900 border border-slate-700 text-slate-100 rounded-xl focus:ring-2 focus:ring-sky-500 focus:outline-none">
              <option value="">Todas as Categorias</option>
              {% for codigo, rotulo in NATUREZAS_FILTRO %}
              <option value="{{ codigo }}" {% if natureza_atual == codigo %}selected{% endif %}>{{ rotulo }}</option>
              {% endfor %}
            </select>
          </div>

          <!-- Modalidade -->
          <div>
            <label class="block text-xs font-semibold text-slate-300 mb-1">Modalidade</label>
            <select name="modality" class="w-full text-xs p-2.5 bg-slate-900 border border-slate-700 text-slate-100 rounded-xl focus:ring-2 focus:ring-sky-500 focus:outline-none">
              <option value="">Todas as Modalidades</option>
              <option value="Pregão Eletrônico" {% if params.modality == 'Pregão Eletrônico' %}selected{% endif %}>Pregão Eletrônico</option>
              <option value="Concorrência" {% if params.modality == 'Concorrência' %}selected{% endif %}>Concorrência</option>
              <option value="Dispensa Eletrônica" {% if params.modality and 'dispensa' in params.modality.lower() %}selected{% endif %}>Dispensa Eletrônica</option>
              <option value="Inexigibilidade" {% if params.modality == 'Inexigibilidade' %}selected{% endif %}>Inexigibilidade</option>
              <option value="Credenciamento" {% if params.modality == 'Credenciamento' %}selected{% endif %}>Credenciamento</option>
              <option value="Leilão" {% if params.modality == 'Leilão' %}selected{% endif %}>Leilão</option>
            </select>
          </div>

          <!-- Esfera Administrativa -->
          <div>
            <label class="block text-xs font-semibold text-slate-300 mb-1">Esfera Administrativa</label>
            <select name="administrative_sphere" class="w-full text-xs p-2.5 bg-slate-900 border border-slate-700 text-slate-100 rounded-xl focus:ring-2 focus:ring-sky-500 focus:outline-none">
              <option value="">Todas as Esferas</option>
              <option value="FEDERAL" {% if params.administrative_sphere == 'FEDERAL' %}selected{% endif %}>Federal</option>
              <option value="ESTADUAL" {% if params.administrative_sphere == 'ESTADUAL' %}selected{% endif %}>Estadual</option>
              <option value="MUNICIPAL" {% if params.administrative_sphere == 'MUNICIPAL' %}selected{% endif %}>Municipal</option>
            </select>
          </div>

          <!-- Origem Operacional / Sistema -->
          <div>
            <label class="block text-xs font-semibold text-slate-300 mb-1">Origem Operacional</label>
            <select name="source_system" class="w-full text-xs p-2.5 bg-slate-900 border border-slate-700 text-slate-100 rounded-xl focus:ring-2 focus:ring-sky-500 focus:outline-none">
              <option value="">Todas as Origens</option>
              <option value="compras_gov" {% if params.source_system == 'compras_gov' %}selected{% endif %}>Compras.gov.br (Federal / SICAF)</option>
              <option value="pncp" {% if params.source_system == 'pncp' %}selected{% endif %}>Outros / PNCP (Demais Esferas)</option>
            </select>
          </div>

          <!-- Portal Executor Mapeado -->
          <div>
            <label class="block text-xs font-semibold text-slate-300 mb-1">Portal Executor Específico</label>
            <select name="portal" class="w-full text-xs p-2.5 bg-slate-900 border border-slate-700 text-slate-100 rounded-xl focus:ring-2 focus:ring-sky-500 focus:outline-none">
              <option value="">Todos os Portais Mapeados</option>
              <option value="Compras.gov.br" {% if params.portal == 'Compras.gov.br' %}selected{% endif %}>Compras.gov.br (Federal / SICAF)</option>
              <option value="BEC/SP" {% if params.portal == 'BEC/SP' %}selected{% endif %}>BEC/SP (São Paulo)</option>
              <option value="Compras MG" {% if params.portal == 'Compras MG' %}selected{% endif %}>Compras MG (Minas Gerais)</option>
              <option value="Compras RS" {% if params.portal == 'Compras RS' %}selected{% endif %}>Compras RS / Banrisul</option>
              <option value="Comprasnet BA" {% if params.portal == 'Comprasnet BA' %}selected{% endif %}>Comprasnet BA (Bahia)</option>
              <option value="SIGA RJ" {% if params.portal == 'SIGA RJ' %}selected{% endif %}>SIGA RJ (Rio de Janeiro)</option>
              <option value="Compras Paraná" {% if params.portal == 'Compras Paraná' %}selected{% endif %}>Compras Paraná</option>
              <option value="BLL Compras" {% if params.portal == 'BLL Compras' %}selected{% endif %}>BLL Compras</option>
              <option value="Licitações-e" {% if params.portal == 'Licitações-e' %}selected{% endif %}>Licitações-e (Banco do Brasil)</option>
              <option value="Portal de Compras Públicas" {% if params.portal == 'Portal de Compras Públicas' %}selected{% endif %}>Portal de Compras Públicas</option>
              <option value="Licitanet" {% if params.portal == 'Licitanet' %}selected{% endif %}>Licitanet</option>
              <option value="PNCP Geral" {% if params.portal == 'PNCP Geral' %}selected{% endif %}>PNCP Geral</option>
            </select>
          </div>

          <!-- Faixa de Valor Estimado -->
          <div>
            <label class="block text-xs font-semibold text-slate-300 mb-1">Valor Estimado (R$)</label>
            <div class="grid grid-cols-2 gap-2">
              <input 
                type="number" 
                name="value_min" 
                value="{{ params.value_min or '' }}" 
                placeholder="Mínimo" 
                class="w-full text-xs p-2 bg-slate-900 border border-slate-700 text-slate-100 placeholder-slate-500 rounded-lg focus:ring-2 focus:ring-sky-500"
              />
              <input 
                type="number" 
                name="value_max" 
                value="{{ params.value_max or '' }}" 
                placeholder="Máximo" 
                class="w-full text-xs p-2 bg-slate-900 border border-slate-700 text-slate-100 placeholder-slate-500 rounded-lg focus:ring-2 focus:ring-sky-500"
              />
            </div>
          </div>

          <!-- Ordenação -->
          <div>
            <label class="block text-xs font-semibold text-slate-300 mb-1">Ordenar por</label>
            <select name="sort_by" class="w-full text-xs p-2.5 bg-slate-900 border border-slate-700 text-slate-100 rounded-xl focus:ring-2 focus:ring-sky-500 focus:outline-none">
              <option value="relevance" {% if params.sort_by == 'relevance' %}selected{% endif %}>Mais Relevante</option>
              <option value="pub_desc" {% if params.sort_by == 'pub_desc' %}selected{% endif %}>Publicação (Mais Recente)</option>
              <option value="pub_asc" {% if params.sort_by == 'pub_asc' %}selected{% endif %}>Publicação (Mais Antiga)</option>
              <option value="deadline_asc" {% if params.sort_by == 'deadline_asc' %}selected{% endif %}>Prazo Final (Mais Próximo)</option>
              <option value="value_desc" {% if params.sort_by == 'value_desc' %}selected{% endif %}>Maior Valor Estimado</option>
              <option value="value_asc" {% if params.sort_by == 'value_asc' %}selected{% endif %}>Menor Valor Estimado</option>
            </select>
          </div>

          <button type="submit" class="w-full bg-sky-600 hover:bg-sky-500 text-white font-bold py-2.5 rounded-xl text-xs transition shadow-md flex items-center justify-center gap-2 cursor-pointer">
            <i class="fa-solid fa-filter"></i> Aplicar Filtros
          </button>
        </form>
      </aside>

      <!-- Main Results Content -->
      <main class="lg:col-span-3">
        <!-- Results Header bar (Dark Theme) -->
        <div class="bg-[#111827] p-4 rounded-2xl border border-slate-800 shadow-xl flex flex-col md:flex-row justify-between items-start md:items-center gap-4 mb-6">
          <div>
            <div class="flex items-center gap-2 flex-wrap">
              <span class="text-sm font-bold text-white">
                Exibindo {{ results.results|length }} de {{ results.total }} oportunidades
              </span>
              <span class="text-xs font-semibold px-2.5 py-0.5 rounded-full bg-slate-800 text-slate-300 border border-slate-700">
                {{ total_db_tenders or results.total }} sincronizadas no banco
              </span>
              <a 
                id="btn-export-csv"
                href="/api/tenders/export/csv?{{ {'q': params.q or '', 'uf': params.state or params.uf or '', 'modality': params.modality or '', 'status': params.status or '', 'category': params.natureza or params.category or ''}|urlencode }}"
                download
                class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-xl text-xs font-medium text-slate-300 bg-slate-800/90 hover:bg-slate-700 hover:text-white border border-slate-700/80 transition shadow-sm"
                title="Exportar licitações filtradas em formato CSV"
              >
                <i class="fa-solid fa-file-csv text-emerald-400"></i> Exportar CSV
              </a>
              <button 
                type="button"
                id="btn-header-save-alert"
                onclick="saveSearchAlert()"
                class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-xl text-xs font-semibold text-sky-300 bg-sky-950/60 hover:bg-sky-900/80 hover:text-white border border-sky-800/80 transition shadow-sm cursor-pointer"
                title="Guardar este Alerta de busca com os filtros atuais"
              >
                <i class="fa-regular fa-bell text-sky-400"></i> Guardar este Alerta
              </button>
            </div>
            {% if params.q %}
              <span class="text-xs text-slate-400 block mt-1">Filtro textual: "<strong class="text-slate-200">{{ params.q }}</strong>"</span>
            {% endif %}
          </div>
          
          <div class="flex items-center gap-3 flex-wrap">
            <!-- Seletor de quantidade por página: 10, 20, 25, 50 -->
            <div class="flex items-center gap-1.5 text-xs text-slate-400">
              <label for="page-size-select" class="font-medium whitespace-nowrap">Por página:</label>
              <select 
                id="page-size-select" 
                onchange="changePageSize(this.value)" 
                class="border border-slate-700 rounded-xl px-2.5 py-1.5 text-xs bg-slate-900 text-slate-200 focus:ring-2 focus:ring-sky-500 font-semibold"
              >
                <option value="10" {% if params.page_size == 10 %}selected{% endif %}>10</option>
                <option value="20" {% if params.page_size == 20 %}selected{% endif %}>20</option>
                <option value="25" {% if params.page_size == 25 %}selected{% endif %}>25</option>
                <option value="50" {% if params.page_size == 50 %}selected{% endif %}>50</option>
              </select>
            </div>

            <button 
              id="btn-sync-tenders" 
              onclick="triggerSyncTenders()" 
              class="bg-emerald-600 hover:bg-emerald-500 text-white font-semibold px-3.5 py-2 rounded-xl text-xs transition shadow-md flex items-center gap-2 cursor-pointer"
              title="Sincronizar oportunidades com Compras.gov.br e PNCP"
            >
              <i class="fa-solid fa-arrows-rotate" id="sync-icon"></i>
              <span id="sync-label">Atualizar Oportunidades</span>
            </button>
            
            <div class="text-xs text-slate-400 font-mono whitespace-nowrap">
              Pág {{ results.page }}/{{ results.total_pages }}
            </div>
          </div>
        </div>

        <!-- Result Cards List (Dark Licitações Brasil Design) -->
        {% if results.results|length == 0 %}
        <div class="bg-[#111827] rounded-2xl border border-slate-800 p-12 text-center shadow-xl">
          <div class="w-16 h-16 bg-slate-800/80 rounded-full flex items-center justify-center mx-auto text-slate-400 text-2xl mb-4">
            <i class="fa-solid fa-inbox"></i>
          </div>
          <h3 class="text-base font-bold text-white mb-1">Nenhuma licitação encontrada</h3>
          <p class="text-xs text-slate-400 max-w-md mx-auto mb-6">
            Tente remover alguns filtros ou buscar por palavras mais abrangentes como "medicamentos", "serviços" ou "equipamentos".
          </p>
          <a href="/search" class="inline-flex items-center gap-2 text-xs font-semibold bg-sky-950/80 text-sky-400 border border-sky-800 px-4 py-2 rounded-xl hover:bg-sky-900 transition">
            Limpar todos os filtros
          </a>
        </div>
        {% else %}
        <div class="space-y-4">
          {% for item in results.results %}
          <article 
            onclick="openTenderDrawer('{{ item.id }}')" 
            class="bg-[#111827] hover:bg-[#162033] border border-slate-800 hover:border-sky-500/50 rounded-2xl p-5 transition-all duration-200 shadow-md hover:shadow-xl cursor-pointer flex flex-col justify-between group relative"
          >
            <div>
              <!-- 1. Cabeçalho com tag de origem em destaque + modalidade em caixa alta e negrito -->
              <div class="flex flex-wrap items-center justify-between gap-2.5 pb-3 border-b border-slate-800/80">
                <div class="flex items-center gap-2 flex-wrap">
                  {% if item.source_system == 'compras_gov' or item.sicaf_ready %}
                  <span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-bold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">
                    <i class="fa-solid fa-shield-halved text-[11px]"></i> Compras.gov.br (SICAF)
                  </span>
                  {% if item.uasg_code %}
                  <span class="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-mono font-bold bg-emerald-950/70 text-emerald-300 border border-emerald-700/50">
                    UASG {{ item.uasg_code }}
                  </span>
                  {% endif %}
                  {% else %}
                  <span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-bold bg-sky-500/15 text-sky-400 border border-sky-500/30">
                    <i class="fa-solid fa-landmark text-[11px]"></i> Outros / PNCP
                  </span>
                  {% endif %}

                  {% if item.executor_portal and item.executor_portal != 'Compras.gov.br' and item.executor_portal != 'PNCP Geral' %}
                  <span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-blue-500/15 text-blue-300 border border-blue-500/30">
                    <i class="fa-solid fa-globe text-[10px]"></i> {{ item.executor_portal }}
                  </span>
                  {% endif %}

                  <a 
                    href="{{ item.url_pncp or item.link_pncp or item.source_url }}" 
                    target="_blank" 
                    rel="noopener noreferrer"
                    onclick="event.stopPropagation();"
                    class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium bg-slate-800/80 hover:bg-slate-700 text-slate-300 hover:text-sky-300 border border-slate-700 transition"
                    title="Ver referência documental no PNCP"
                  >
                    <i class="fa-solid fa-landmark text-[9px] text-sky-400"></i> PNCP
                    <i class="fa-solid fa-arrow-up-right-from-square text-[8px] text-slate-400"></i>
                  </a>
                </div>

                <!-- Modalidade em caixa alta e negrito -->
                <span class="font-extrabold text-xs tracking-wider uppercase text-sky-400 font-mono bg-sky-950/40 border border-sky-800/40 px-2.5 py-0.5 rounded-md">
                  {{ item.modality.upper() }}
                </span>
              </div>

              <!-- 2. Linha de metadados: número do certame, órgão comprador, município e UF -->
              <div class="flex flex-wrap items-center gap-2 text-xs text-slate-400 mt-3 font-medium">
                <span class="text-slate-100 font-bold font-mono bg-slate-800/80 px-2 py-0.5 rounded border border-slate-700/60">
                  {{ item.notice_number if item.notice_number and item.notice_number != 'Não informado' else item.external_id }}
                </span>
                <span class="text-slate-600">•</span>
                <span class="text-slate-200 font-semibold truncate max-w-md" title="{{ item.contracting_entity }}">
                  <i class="fa-regular fa-building text-slate-400 mr-1"></i>{{ item.contracting_entity }}
                </span>
                <span class="text-slate-600">•</span>
                <span class="text-slate-300">
                  <i class="fa-solid fa-location-dot text-slate-400 mr-1"></i>{{ item.city }} - {{ item.state }}
                </span>
              </div>

              <!-- 3. Objeto resumido em texto claro com contraste -->
              <h2 class="text-base font-bold text-white group-hover:text-sky-300 transition-colors mt-3 leading-snug">
                {{ item.title }}
              </h2>
              <p class="text-xs text-slate-300 mt-2 line-clamp-3 leading-relaxed">
                {{ item.object_description }}
              </p>
            </div>

            <!-- 4. Rodapé do card com badges pill + botões de ação rápida -->
            <div class="mt-4 pt-3.5 border-t border-slate-800/80 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
              <!-- Badges pill: Status (Aberta), Tipo, UF, Valor Estimado/Homologado, Data/Hora limite -->
              <div class="flex items-center gap-2 flex-wrap text-xs">
                <!-- Status (Aberta) -->
                <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-emerald-500/15 text-emerald-300 border border-emerald-500/30">
                  <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span> Aberta
                </span>

                <!-- Categoria simplificada: Material / Serviço / Outros -->
                {% set nat = item.natureza_objeto or 'OUTROS' %}
                <span class="natureza-badge inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-semibold border
                  {% if nat == 'MATERIAL' %}bg-indigo-500/15 text-indigo-300 border-indigo-500/30
                  {% elif nat == 'SERVICO' %}bg-emerald-500/15 text-emerald-300 border-emerald-500/30
                  {% else %}bg-slate-700/40 text-slate-300 border-slate-600/50{% endif %}" data-natureza="{{ nat }}">
                  <i class="fa-solid {% if nat == 'MATERIAL' %}fa-box{% elif nat == 'SERVICO' %}fa-screwdriver-wrench{% else %}fa-shapes{% endif %} text-[10px]"></i>
                  {% if nat == 'MATERIAL' %}Material{% elif nat == 'SERVICO' %}Serviço{% else %}Outros{% endif %}
                </span>

                <!-- UF -->
                <span class="inline-flex items-center px-2 py-1 rounded-full text-xs font-bold bg-slate-800 text-slate-200 border border-slate-700">
                  {{ item.state }}
                </span>

                <!-- Valor Estimado/Homologado em destaque -->
                <span class="inline-flex items-center gap-1 px-3 py-1 rounded-full text-xs font-bold bg-emerald-950/70 text-emerald-300 border border-emerald-700/60 font-mono">
                  <i class="fa-solid fa-coins text-[10px] text-emerald-400"></i>
                  {% if item.estimated_total_value %}
                    R$ {{ "{:,.2f}".format(item.estimated_total_value).replace(",", "X").replace(".", ",").replace("X", ".") }}
                  {% else %}
                    Valor a definir
                  {% endif %}
                </span>

                <!-- Data/Hora limite de propostas -->
                <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-amber-500/15 text-amber-300 border border-amber-500/30">
                  <i class="fa-regular fa-clock text-[10px]"></i>
                  {% if item.proposal_deadline %}
                    Limite: {{ item.proposal_deadline.strftime('%d/%m/%Y %H:%M') }}
                  {% else %}
                    Prazo a definir
                  {% endif %}
                </span>
              </div>

              <!-- Botões de ação rápida: "+ Negócio", "Disputa" e abrir detalhes -->
              <div class="flex items-center gap-2 self-end sm:self-auto shrink-0">
                <a 
                  href="{{ item.url_disputa or item.url_pncp or item.link_comprasnet or item.official_notice_url or item.link_pncp or '#' }}" 
                  target="_blank" 
                  rel="noopener noreferrer"
                  onclick="event.stopPropagation();"
                  class="bg-emerald-600 hover:bg-emerald-500 text-white font-bold px-3 py-1.5 rounded-xl text-xs transition shadow-sm flex items-center gap-1.5 cursor-pointer"
                  title="Acessar edital e disputa no portal de execução oficial"
                >
                  <i class="fa-solid fa-gavel text-[10px]"></i> Disputa
                </a>

                <button 
                  type="button"
                  onclick="event.stopPropagation(); addOpportunityToPipeline('{{ item.id }}', '{{ item.notice_number or item.external_id }}')" 
                  class="bg-sky-600 hover:bg-sky-500 text-white font-bold px-3 py-1.5 rounded-xl text-xs transition shadow-sm flex items-center gap-1.5 cursor-pointer"
                  title="Enviar oportunidade para o funil de negócios"
                >
                  <i class="fa-solid fa-plus text-[10px]"></i> Negócio
                </button>

                <button 
                  type="button"
                  onclick="event.stopPropagation(); openTenderDrawer('{{ item.id }}')" 
                  class="bg-slate-800 hover:bg-slate-700 text-slate-200 hover:text-white border border-slate-700 font-semibold px-3 py-1.5 rounded-xl text-xs transition flex items-center gap-1.5 cursor-pointer"
                  title="Ver detalhes na gaveta lateral"
                >
                  <span>Detalhes</span>
                  <i class="fa-solid fa-arrow-right text-[10px]"></i>
                </button>
              </div>
            </div>
          </article>
          {% endfor %}
        </div>
        {% endif %}

        <!-- Pagination Controls (Dark Theme) -->
        {% set qs_paginacao = {} %}
        {% for chave, valor in [
          ('q', params.q), ('uf', params.state or params.uf), ('city', params.city), ('category', params.natureza or params.category),
          ('modality', params.modality), ('administrative_sphere', params.administrative_sphere),
          ('source_system', params.source_system), ('portal', params.portal), ('uasg_code', params.uasg_code),
          ('value_min', params.value_min), ('value_max', params.value_max), ('date_from', params.date_from),
          ('date_to', params.date_to), ('sort_by', params.sort_by), ('page_size', params.page_size)
        ] %}
          {% if valor is not none and valor != '' %}{% set _ = qs_paginacao.update({chave: valor}) %}{% endif %}
        {% endfor %}
        <div class="bg-[#111827] p-4 rounded-2xl border border-slate-800 shadow-xl flex flex-col sm:flex-row justify-between items-center gap-3 mt-8">
          <div class="text-xs text-slate-400">
            Exibindo <span class="font-bold text-white">{{ results.results|length }}</span> de <span class="font-bold text-white">{{ results.total }}</span> oportunidades (Página {{ results.page }} de {{ results.total_pages }})
          </div>

          <div class="flex items-center gap-2">
            {% if results.page > 1 %}
            <a href="?{{ qs_paginacao|urlencode }}&page={{ results.page - 1 }}" class="px-3.5 py-2 bg-slate-800 border border-slate-700 hover:bg-slate-700 text-slate-200 rounded-xl text-xs font-semibold transition flex items-center gap-1.5 shadow-sm">
              <i class="fa-solid fa-chevron-left text-[10px]"></i> Anterior
            </a>
            {% else %}
            <span class="px-3.5 py-2 bg-slate-900 border border-slate-800 text-slate-600 rounded-xl text-xs font-semibold cursor-not-allowed flex items-center gap-1.5">
              <i class="fa-solid fa-chevron-left text-[10px]"></i> Anterior
            </span>
            {% endif %}

            <span class="text-xs font-bold text-sky-400 px-3 py-2 bg-sky-950/60 rounded-xl border border-sky-800/60 font-mono">
              {{ results.page }} / {{ results.total_pages }}
            </span>

            {% if results.page < results.total_pages %}
            <a href="?{{ qs_paginacao|urlencode }}&page={{ results.page + 1 }}" class="px-3.5 py-2 bg-slate-800 border border-slate-700 hover:bg-slate-700 text-slate-200 rounded-xl text-xs font-semibold transition flex items-center gap-1.5 shadow-sm">
              Próxima <i class="fa-solid fa-chevron-right text-[10px]"></i>
            </a>
            {% else %}
            <span class="px-3.5 py-2 bg-slate-900 border border-slate-800 text-slate-600 rounded-xl text-xs font-semibold cursor-not-allowed flex items-center gap-1.5">
              Próxima <i class="fa-solid fa-chevron-right text-[10px]"></i>
            </span>
            {% endif %}
          </div>
        </div>
      </main>
    </div>
  </div>
</div>

<!-- ============================================================================== -->
<!-- DRAWER LATERAL DE DETALHES (SLIDE-OVER MODAL LICITAÇÕESBRASIL)                  -->
<!-- ============================================================================== -->
<div id="drawer-backdrop" onclick="closeTenderDrawer()" class="fixed inset-0 bg-black/70 backdrop-blur-xs z-50 transition-opacity opacity-0 pointer-events-none duration-300"></div>

<aside id="tender-drawer" class="fixed top-0 right-0 h-full w-full max-w-2xl lg:max-w-3xl bg-[#0f172a] border-l border-slate-800 shadow-2xl z-50 transform translate-x-full transition-transform duration-300 flex flex-col overflow-hidden text-slate-100">
  <!-- Drawer Top Bar -->
  <div class="p-5 border-b border-slate-800 flex items-center justify-between bg-[#111827]">
    <div class="flex items-center gap-2" id="drawer-tags-container">
      <span id="drawer-source-badge" class="px-3 py-1 rounded-full text-xs font-bold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">
        <i class="fa-solid fa-shield-halved text-[11px] mr-1"></i> Compras.gov.br (SICAF)
      </span>
      <span id="drawer-uasg-badge" class="px-2 py-0.5 rounded text-[11px] font-mono font-bold bg-emerald-950 text-emerald-300 border border-emerald-700 hidden">
        UASG
      </span>
      <span id="drawer-modality-badge" class="px-2.5 py-0.5 rounded text-xs font-extrabold uppercase font-mono text-sky-400 bg-sky-950/50 border border-sky-800">
        PREGÃO ELETRÔNICO
      </span>
    </div>
    
    <button onclick="closeTenderDrawer()" class="text-slate-400 hover:text-white bg-slate-800/80 hover:bg-slate-700 p-2 rounded-xl transition cursor-pointer" title="Fechar detalhes">
      <i class="fa-solid fa-xmark text-base"></i>
    </button>
  </div>

  <!-- Drawer Scrollable Body -->
  <div class="flex-1 overflow-y-auto p-6 space-y-6 text-slate-200">
    <!-- Cabeçalho Completo: Órgão, processo, valor estimado em destaque verde -->
    <div>
      <div class="text-xs font-semibold uppercase tracking-wider text-slate-400 mb-1" id="drawer-entity">
        Órgão Comprador
      </div>
      <h2 class="text-xl font-bold text-white leading-snug" id="drawer-title">
        Título da Licitação
      </h2>
      <div class="flex flex-wrap items-center gap-3 text-xs text-slate-400 mt-2 font-mono">
        <span id="drawer-process">Processo: -</span>
        <span>•</span>
        <span id="drawer-notice">Certame: -</span>
        <span>•</span>
        <span id="drawer-location">Local: -</span>
      </div>

      <!-- Valor Estimado em Destaque Verde -->
      <div class="mt-4 p-4 rounded-2xl bg-emerald-950/40 border border-emerald-600/30 flex items-center justify-between">
        <div>
          <span class="text-[11px] uppercase font-bold text-emerald-400 block tracking-wider">Valor Estimado / Homologado</span>
          <span class="text-2xl sm:text-3xl font-extrabold text-emerald-300 font-mono" id="drawer-value">
            R$ 0,00
          </span>
        </div>
        <div class="text-right">
          <span class="text-[11px] uppercase font-bold text-slate-400 block">Status Operacional</span>
          <span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-bold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 mt-1">
            <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span> Aberta
          </span>
        </div>
      </div>
    </div>

    <!-- Botões de Ação Rápida no Topo do Drawer -->
    <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
      <!-- Botão principal: Acessar Edital / Disputa (preferencialmente portal de execução original) -->
      <a id="drawer-btn-dispute" href="#" target="_blank" rel="noopener noreferrer" class="bg-emerald-600 hover:bg-emerald-500 text-white font-bold py-2.5 px-3 rounded-xl text-xs transition shadow-md flex items-center justify-center gap-2">
        <i class="fa-solid fa-gavel text-xs"></i>
        <span id="drawer-btn-dispute-text">Acessar Edital / Disputa</span>
        <i class="fa-solid fa-arrow-up-right-from-square text-[10px]"></i>
      </a>

      <!-- Botão "Arquivos do Edital" para download de anexos -->
      <a id="drawer-btn-files" href="#" target="_blank" rel="noopener noreferrer" class="bg-slate-800 hover:bg-slate-700 text-slate-100 border border-slate-700 font-bold py-2.5 px-3 rounded-xl text-xs transition flex items-center justify-center gap-2">
        <i class="fa-solid fa-file-pdf text-rose-400 text-xs"></i>
        <span>Arquivos do Edital</span>
        <i class="fa-solid fa-download text-[10px]"></i>
      </a>

      <!-- Botão de Ação Principal: "Adicionar aos meus negócios" -->
      <button id="drawer-btn-pipeline" onclick="addCurrentTenderToPipeline()" class="bg-gradient-to-r from-sky-600 to-sky-500 hover:from-sky-500 hover:to-sky-400 text-white font-bold py-2.5 px-3 rounded-xl text-xs transition shadow-md flex items-center justify-center gap-2 cursor-pointer">
        <i class="fa-solid fa-briefcase text-xs"></i>
        <span>+ Meus Negócios</span>
      </button>
    </div>

    <!-- Referência Documental / Link Secundário Discreto do PNCP -->
    <div class="flex items-center justify-between px-3.5 py-2.5 rounded-xl bg-slate-900/80 border border-slate-800 text-[11px] text-slate-400">
      <span class="flex items-center gap-1.5 font-medium">
        <i class="fa-solid fa-landmark text-sky-400 text-xs"></i>
        <span>Referência Documental:</span>
      </span>
      <a id="drawer-link-pncp" href="#" target="_blank" rel="noopener noreferrer" class="text-sky-400 hover:text-sky-300 font-mono font-medium hover:underline flex items-center gap-1.5 transition">
        <span id="drawer-text-pncp">Ver edital no PNCP</span>
        <i class="fa-solid fa-arrow-up-right-from-square text-[9px]"></i>
      </a>
    </div>

    <!-- Seção com Prazos: Publicação, Cadastro de Propostas e Data de Lances -->
    <div class="bg-[#111827] p-4 rounded-2xl border border-slate-800">
      <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-3 flex items-center gap-2">
        <i class="fa-regular fa-calendar-days text-sky-400"></i> Cronograma Oficial de Prazos
      </h3>
      <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <div class="bg-slate-900/80 p-3 rounded-xl border border-slate-800">
          <span class="text-[10px] uppercase font-bold text-slate-400 block">Data de Publicação</span>
          <span class="text-xs font-bold text-slate-100 mt-1 block font-mono" id="drawer-date-pub">-</span>
        </div>
        <div class="bg-slate-900/80 p-3 rounded-xl border border-slate-800">
          <span class="text-[10px] uppercase font-bold text-amber-400 block">Limite de Propostas</span>
          <span class="text-xs font-bold text-amber-300 mt-1 block font-mono" id="drawer-date-limit">-</span>
        </div>
        <div class="bg-slate-900/80 p-3 rounded-xl border border-slate-800">
          <span class="text-[10px] uppercase font-bold text-emerald-400 block">Abertura / Lances</span>
          <span class="text-xs font-bold text-emerald-300 mt-1 block font-mono" id="drawer-date-open">-</span>
        </div>
      </div>
    </div>

    <!-- Objeto Detalhado da Licitação -->
    <div class="bg-[#111827] p-4 rounded-2xl border border-slate-800">
      <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-2 flex items-center gap-2">
        <i class="fa-solid fa-align-left text-sky-400"></i> Descrição do Objeto
      </h3>
      <p class="text-xs text-slate-300 leading-relaxed font-normal" id="drawer-desc">
        -
      </p>
    </div>

    <!-- SEÇÃO DE IA: RESUMO ESTRUTURADO E CONVERSE COM O EDITAL (GEMINI) -->
    <div class="bg-gradient-to-b from-[#131d35] to-[#0f172a] p-5 rounded-2xl border border-indigo-500/30 shadow-xl space-y-4">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-2.5">
          <div class="w-8 h-8 rounded-xl bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center text-white shadow-md shadow-indigo-500/20">
            <i class="fa-solid fa-wand-magic-sparkles text-xs"></i>
          </div>
          <div>
            <h3 class="text-sm font-bold text-white flex items-center gap-1.5">
              Análise com IA (Google Gemini)
            </h3>
            <p class="text-[11px] text-slate-400">Resumo executivo de conformidade e chat consultivo</p>
          </div>
        </div>
        <span id="drawer-ai-badge" class="text-[10px] uppercase font-mono font-bold px-2 py-0.5 rounded-full bg-indigo-500/20 text-indigo-300 border border-indigo-500/40">
          Lei 14.133/2021
        </span>
      </div>

      <!-- Sub-abas: Resumo Estruturado | Converse com o Edital -->
      <div class="flex bg-slate-900/90 p-1 rounded-xl border border-slate-800 text-xs font-semibold">
        <button type="button" id="tab-btn-ai-summary" onclick="switchDrawerAiTab('summary')" class="flex-1 py-1.5 px-3 rounded-lg text-center transition bg-indigo-600 text-white shadow-sm flex items-center justify-center gap-1.5 cursor-pointer">
          <i class="fa-solid fa-file-lines text-xs"></i> Resumo Estruturado
        </button>
        <button type="button" id="tab-btn-ai-chat" onclick="switchDrawerAiTab('chat')" class="flex-1 py-1.5 px-3 rounded-lg text-center transition text-slate-400 hover:text-slate-200 flex items-center justify-center gap-1.5 cursor-pointer">
          <i class="fa-solid fa-comments text-xs"></i> Converse com o Edital
        </button>
      </div>

      <!-- CONTEÚDO ABA 1: RESUMO ESTRUTURADO -->
      <div id="drawer-ai-tab-summary" class="space-y-3">
        <div class="flex items-center justify-between gap-2">
          <button id="btn-generate-ai-summary" onclick="generateAiSummaryForDrawer()" class="bg-gradient-to-r from-indigo-600 via-purple-600 to-indigo-600 hover:from-indigo-500 hover:to-purple-500 text-white font-bold py-2.5 px-4 rounded-xl text-xs transition shadow-lg shadow-indigo-600/20 flex items-center gap-2 cursor-pointer w-full sm:w-auto justify-center">
            <i class="fa-solid fa-sparkles text-xs"></i>
            <span id="btn-generate-ai-summary-text">Gerar Resumo com IA</span>
          </button>
          <span id="ai-summary-cache-status" class="text-[10px] text-slate-400 font-mono hidden"></span>
        </div>

        <!-- Skeleton Loading Dark -->
        <div id="drawer-ai-summary-skeleton" class="hidden space-y-3 p-4 rounded-xl bg-slate-900/60 border border-slate-800 animate-pulse">
          <div class="h-4 bg-slate-800 rounded w-1/3"></div>
          <div class="h-3 bg-slate-800/70 rounded w-full"></div>
          <div class="h-3 bg-slate-800/70 rounded w-5/6"></div>
          <div class="h-16 bg-slate-800/50 rounded mt-2"></div>
          <div class="flex items-center justify-center gap-2 text-xs text-indigo-400 py-2">
            <i class="fa-solid fa-circle-notch fa-spin"></i>
            <span>Analisando Termo de Referência e Edital com IA...</span>
          </div>
        </div>

        <!-- Conteúdo do Resumo Estruturado -->
        <div id="drawer-ai-summary-content" class="hidden space-y-3 text-xs">
          <!-- Objeto resumido em linguagem clara -->
          <div class="p-3.5 rounded-xl bg-slate-900/80 border border-slate-800">
            <div class="text-[10px] uppercase font-bold tracking-wider text-indigo-400 mb-1 flex items-center gap-1.5">
              <i class="fa-solid fa-bullseye"></i> Objeto Resumido (Linguagem Clara)
            </div>
            <p class="text-slate-200 leading-relaxed font-normal" id="ai-summary-objeto">-</p>
          </div>

          <!-- Exigências de Habilitação Técnica mais críticas -->
          <div class="p-3.5 rounded-xl bg-slate-900/80 border border-slate-800">
            <div class="text-[10px] uppercase font-bold tracking-wider text-amber-400 mb-1.5 flex items-center gap-1.5">
              <i class="fa-solid fa-clipboard-check"></i> Exigências de Habilitação Técnica Crítica
            </div>
            <ul class="space-y-1.5 text-slate-300 list-disc list-inside" id="ai-summary-habilitacao">
            </ul>
          </div>

          <!-- Prazos de entrega/execução -->
          <div class="p-3.5 rounded-xl bg-slate-900/80 border border-slate-800">
            <div class="text-[10px] uppercase font-bold tracking-wider text-sky-400 mb-1 flex items-center gap-1.5">
              <i class="fa-regular fa-clock"></i> Prazos de Entrega / Execução
            </div>
            <p class="text-slate-200 leading-relaxed font-normal" id="ai-summary-prazos">-</p>
          </div>

          <!-- Condições de pagamento e sanções -->
          <div class="p-3.5 rounded-xl bg-slate-900/80 border border-slate-800">
            <div class="text-[10px] uppercase font-bold tracking-wider text-rose-400 mb-1 flex items-center gap-1.5">
              <i class="fa-solid fa-gavel"></i> Condições de Pagamento e Sanções
            </div>
            <p class="text-slate-200 leading-relaxed font-normal" id="ai-summary-pagamento">-</p>
          </div>

          <!-- Pontos de atenção -->
          <div id="ai-summary-pontos-box" class="p-3.5 rounded-xl bg-indigo-950/30 border border-indigo-800/40 hidden">
            <div class="text-[10px] uppercase font-bold tracking-wider text-indigo-300 mb-1 flex items-center gap-1.5">
              <i class="fa-solid fa-triangle-exclamation"></i> Pontos de Atenção Estratégicos
            </div>
            <ul class="space-y-1 text-slate-300 list-disc list-inside" id="ai-summary-pontos">
            </ul>
          </div>
        </div>
      </div>

      <!-- CONTEÚDO ABA 2: CONVERSE COM O EDITAL (CHAT) -->
      <div id="drawer-ai-tab-chat" class="hidden space-y-3">
        <!-- Perguntas Rápidas Sugeridas -->
        <div class="flex flex-wrap gap-1.5">
          <button type="button" onclick="askPresetQuestion('Qual é o prazo limite e forma de entrega dos produtos/serviços?')" class="text-[11px] bg-slate-900 hover:bg-slate-800 text-slate-300 border border-slate-800 px-2.5 py-1 rounded-lg transition cursor-pointer">
            ⏱️ Prazos de entrega?
          </button>
          <button type="button" onclick="askPresetQuestion('Quais são as exigências de habilitação técnica e atestados?')" class="text-[11px] bg-slate-900 hover:bg-slate-800 text-slate-300 border border-slate-800 px-2.5 py-1 rounded-lg transition cursor-pointer">
            📑 Atestados exigidos?
          </button>
          <button type="button" onclick="askPresetQuestion('Como funcionam os pagamentos e quais as sanções previstas?')" class="text-[11px] bg-slate-900 hover:bg-slate-800 text-slate-300 border border-slate-800 px-2.5 py-1 rounded-lg transition cursor-pointer">
            💰 Pagamento e multas?
          </button>
          <button type="button" onclick="askPresetQuestion('Qual o prazo para impugnar este edital conforme a Lei 14.133/2021?')" class="text-[11px] bg-slate-900 hover:bg-slate-800 text-slate-300 border border-slate-800 px-2.5 py-1 rounded-lg transition cursor-pointer">
            ⚖️ Impugnação de edital?
          </button>
        </div>

        <!-- Histórico do Chat -->
        <div id="drawer-chat-messages" class="h-64 overflow-y-auto space-y-3 p-3 bg-slate-900/90 rounded-xl border border-slate-800 text-xs">
          <div class="flex gap-2.5 items-start">
            <div class="w-6 h-6 rounded-full bg-indigo-600/30 border border-indigo-500/40 flex items-center justify-center text-indigo-400 shrink-0 text-[10px]">
              <i class="fa-solid fa-robot"></i>
            </div>
            <div class="bg-slate-800/80 p-3 rounded-2xl rounded-tl-none border border-slate-700/60 text-slate-200 max-w-[85%] leading-relaxed">
              Olá! Sou o assistente de IA especialista em licitações públicas. Pergunte qualquer dúvida sobre as regras, exigências técnicas, prazos ou condições deste edital!
            </div>
          </div>
        </div>

        <!-- Input do Chat -->
        <form id="drawer-chat-form" onsubmit="event.preventDefault(); sendDrawerAiChat();" class="flex gap-2 items-center">
          <input type="text" id="drawer-chat-input" placeholder="Faça uma pergunta sobre o edital..." class="flex-1 p-2.5 bg-slate-900 border border-slate-700 text-slate-100 rounded-xl text-xs focus:ring-2 focus:ring-indigo-500 focus:outline-none placeholder-slate-500" />
          <button type="submit" id="drawer-chat-btn-send" class="bg-indigo-600 hover:bg-indigo-500 text-white font-bold p-2.5 px-3.5 rounded-xl text-xs transition cursor-pointer flex items-center gap-1.5 shadow-md shadow-indigo-600/20">
            <i class="fa-solid fa-paper-plane text-xs"></i>
          </button>
        </form>
      </div>
    </div>

    <!-- Listagem de Itens do Certame logo abaixo -->
    <div class="bg-[#111827] p-4 rounded-2xl border border-slate-800">
      <div class="flex items-center justify-between mb-3">
        <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400 flex items-center gap-2">
          <i class="fa-solid fa-list-check text-sky-400"></i> Itens da Contratação
        </h3>
        <span class="text-xs font-mono text-slate-400" id="drawer-items-count">0 itens</span>
      </div>

      <div class="overflow-x-auto rounded-xl border border-slate-800">
        <table class="w-full text-left text-xs text-slate-300">
          <thead class="text-[10px] uppercase text-slate-400 bg-slate-900/90 border-b border-slate-800">
            <tr>
              <th class="py-2.5 px-3 font-semibold">#</th>
              <th class="py-2.5 px-3 font-semibold">Descrição do Item</th>
              <th class="py-2.5 px-3 font-semibold">Qtd.</th>
              <th class="py-2.5 px-3 font-semibold">Unid.</th>
              <th class="py-2.5 px-3 font-semibold text-right">Valor Estimado</th>
            </tr>
          </thead>
          <tbody id="drawer-items-tbody" class="divide-y divide-slate-800 font-mono">
            <!-- Linhas de itens preenchidas via API -->
          </tbody>
        </table>
      </div>
    </div>

    <!-- Link Completo e Ações Complementares -->
    <div class="flex justify-between items-center pt-2">
      <a id="drawer-link-full" href="#" class="text-sky-400 hover:text-sky-300 text-xs font-semibold flex items-center gap-1.5 transition">
        <span>Abrir análise aprofundada por IA</span>
        <i class="fa-solid fa-arrow-up-right-from-square text-[10px]"></i>
      </a>
    </div>
  </div>
</aside>

<!-- Modal Salvar Alerta (Dark Theme) -->
<div id="save-alert-modal" class="fixed inset-0 bg-black/70 backdrop-blur-xs z-50 hidden flex items-center justify-center p-4">
  <div class="bg-[#111827] border border-slate-800 rounded-2xl max-w-md w-full p-6 shadow-2xl text-slate-100">
    <div class="flex justify-between items-center mb-4">
      <h3 class="text-base font-bold text-white flex items-center gap-2">
        <i class="fa-regular fa-bell text-sky-400"></i> Criar Alerta de Oportunidades
      </h3>
      <button onclick="closeSaveSearchModal()" class="text-slate-400 hover:text-white"><i class="fa-solid fa-xmark"></i></button>
    </div>
    <div class="space-y-3 text-xs">
      <div>
        <label class="block font-semibold text-slate-300 mb-1">Nome do Alerta</label>
        <input type="text" id="alert-name-input" value="Alerta: {{ params.q or 'Todas as Oportunidades' }}" class="w-full p-2.5 bg-slate-900 border border-slate-700 text-slate-100 rounded-xl focus:ring-2 focus:ring-sky-500" />
      </div>
      <div>
        <label class="block font-semibold text-slate-300 mb-1">Frequência</label>
        <select id="alert-freq-input" class="w-full p-2.5 bg-slate-900 border border-slate-700 text-slate-100 rounded-xl focus:ring-2 focus:ring-sky-500">
          <option value="DAILY">Diária (Resumo Matutino)</option>
          <option value="WEEKLY">Semanal</option>
          <option value="REALTIME">Tempo Real (Assim que publicado no PNCP)</option>
        </select>
      </div>
      <div>
        <label class="block font-semibold text-slate-300 mb-1">Canais</label>
        <div class="flex gap-4 text-slate-300">
          <label class="flex items-center gap-2 cursor-pointer"><input type="checkbox" id="alert-chan-app" checked class="rounded text-sky-600" /> Painel Interno</label>
          <label class="flex items-center gap-2 cursor-pointer"><input type="checkbox" id="alert-chan-email" checked class="rounded text-sky-600" /> E-mail</label>
        </div>
      </div>
    </div>
    <div class="mt-6 flex justify-end gap-2">
      <button onclick="closeSaveSearchModal()" class="px-4 py-2 rounded-xl text-xs border border-slate-700 font-semibold text-slate-300 hover:bg-slate-800 transition">Cancelar</button>
      <button onclick="submitSaveAlert()" class="px-4 py-2 rounded-xl text-xs bg-sky-600 text-white font-semibold hover:bg-sky-500 transition shadow-md">Salvar Alerta</button>
    </div>
  </div>
</div>
{% endblock %}

{% block scripts %}
<script>
  let currentDrawerTender = null;

  async function openTenderDrawer(tenderId) {
    const backdrop = document.getElementById('drawer-backdrop');
    const drawer = document.getElementById('tender-drawer');

    backdrop.classList.remove('opacity-0', 'pointer-events-none');
    backdrop.classList.add('opacity-100');
    drawer.classList.remove('translate-x-full');
    drawer.classList.add('translate-x-0');
    document.body.classList.add('overflow-hidden');
    resetDrawerAiState();

    const tbody = document.getElementById('drawer-items-tbody');
    tbody.innerHTML = '<tr><td colspan="5" class="py-4 text-center text-slate-400"><i class="fa-solid fa-spinner animate-spin mr-2"></i>Carregando dados completos do certame...</td></tr>';
    document.getElementById('drawer-items-count').textContent = 'Carregando...';

    try {
      const resp = await fetch(`/api/tenders/${tenderId}`);
      if (!resp.ok) throw new Error('Falha ao carregar certame');
      const data = await resp.json();
      currentDrawerTender = data;

      // Badges
      const sourceBadge = document.getElementById('drawer-source-badge');
      const uasgBadge = document.getElementById('drawer-uasg-badge');
      if (data.source_system === 'compras_gov' || data.sicaf_ready) {
        sourceBadge.className = 'px-3 py-1 rounded-full text-xs font-bold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30';
        sourceBadge.innerHTML = '<i class="fa-solid fa-shield-halved text-[11px] mr-1"></i> Compras.gov.br (SICAF)';
        if (data.uasg_code) {
          uasgBadge.textContent = `UASG ${data.uasg_code}`;
          uasgBadge.classList.remove('hidden');
        } else {
          uasgBadge.classList.add('hidden');
        }
      } else {
        sourceBadge.className = 'px-3 py-1 rounded-full text-xs font-bold bg-sky-500/15 text-sky-400 border border-sky-500/30';
        sourceBadge.innerHTML = '<i class="fa-solid fa-landmark text-[11px] mr-1"></i> Outros / PNCP';
        uasgBadge.classList.add('hidden');
      }

      document.getElementById('drawer-modality-badge').textContent = (data.modality || 'PREGÃO ELETRÔNICO').toUpperCase();

      // Title & Entity
      document.getElementById('drawer-entity').textContent = data.contracting_entity || 'Órgão Não Informado';
      document.getElementById('drawer-title').textContent = data.title || 'Contratação Pública';
      document.getElementById('drawer-process').textContent = `Processo: ${data.process_number || 'Não informado'}`;
      document.getElementById('drawer-notice').textContent = `Certame: ${data.notice_number || data.external_id}`;
      document.getElementById('drawer-location').textContent = `Local: ${data.city || 'DF'} - ${data.state || 'DF'}`;

      // Value
      if (data.estimated_total_value) {
        document.getElementById('drawer-value').textContent = Number(data.estimated_total_value).toLocaleString('pt-BR', { style: 'currency', currency: 'BRL' });
      } else {
        document.getElementById('drawer-value').textContent = 'Valor sigiloso / A definir';
      }

      // Dates
      const formatDt = (dtStr) => {
        if (!dtStr) return 'Não informado';
        try {
          const d = new Date(dtStr);
          return d.toLocaleDateString('pt-BR') + ' ' + d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
        } catch { return dtStr; }
      };

      document.getElementById('drawer-date-pub').textContent = formatDt(data.publication_date);
      document.getElementById('drawer-date-limit').textContent = formatDt(data.proposal_deadline);
      document.getElementById('drawer-date-open').textContent = formatDt(data.opening_date);

      // Description
      document.getElementById('drawer-desc').textContent = data.object_description || 'Sem descrição detalhada.';

      // Action Buttons: Priorização do portal de origem / sala de disputa
      const disputeBtn = document.getElementById('drawer-btn-dispute');
      const disputeUrl = data.url_disputa || data.url_pncp || data.link_comprasnet || data.official_notice_url || data.link_pncp || data.source_url || '#';
      disputeBtn.href = disputeUrl;
      const portalLabel = data.executor_portal || (data.sicaf_ready || data.source_system === 'compras_gov' ? 'Compras.gov.br' : 'Sala de Disputa');
      disputeBtn.innerHTML = `<i class="fa-solid fa-gavel text-xs"></i> <span>Acessar Edital / Disputa (${portalLabel})</span> <i class="fa-solid fa-arrow-up-right-from-square text-[10px]"></i>`;

      // Referência documental discreta para o PNCP
      const pncpLink = document.getElementById('drawer-link-pncp');
      if (pncpLink) {
        const canonicalPncp = data.url_pncp || data.link_pncp || data.source_url || 'https://pncp.gov.br/app/editais';
        pncpLink.href = canonicalPncp;
        const textPncp = document.getElementById('drawer-text-pncp');
        if (textPncp) {
          textPncp.textContent = data.external_id ? `PNCP: ${data.external_id}` : 'Ver no PNCP';
        }
      }

      const filesBtn = document.getElementById('drawer-btn-files');
      const docUrl = (data.documents && data.documents[0] && data.documents[0].source_url) || data.official_notice_url || data.link_pncp || data.source_url || '#';
      filesBtn.href = docUrl;

      document.getElementById('drawer-link-full').href = `/tenders/${data.id}`;

      // Items Table
      const items = data.items || [];
      document.getElementById('drawer-items-count').textContent = `${items.length} item(ns)`;
      if (items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="py-4 text-center text-slate-400">Nenhum item individual cadastrado para este certame.</td></tr>';
      } else {
        tbody.innerHTML = items.map(item => `
          <tr class="hover:bg-slate-800/50 transition">
            <td class="py-2.5 px-3 font-bold text-sky-400">${item.item_number}</td>
            <td class="py-2.5 px-3 text-slate-200 font-sans leading-snug">
              <div class="line-clamp-2" title="${escapeHtml(item.description)}">${escapeHtml(item.description)}</div>
              ${item.classification_code && item.classification_code !== 'Não informado' ? `<span class="text-[10px] text-slate-400 block font-mono mt-0.5">Cat: ${escapeHtml(item.classification_code)}</span>` : ''}
            </td>
            <td class="py-2.5 px-3 text-slate-300 font-bold">${item.quantity || 1}</td>
            <td class="py-2.5 px-3 text-slate-400 text-[11px]">${escapeHtml(item.unit || 'UN')}</td>
            <td class="py-2.5 px-3 text-right font-bold text-emerald-300">
              ${(item.estimated_total_value || 0).toLocaleString('pt-BR', { style: 'currency', currency: 'BRL' })}
            </td>
          </tr>
        `).join('');
      }

    } catch (err) {
      tbody.innerHTML = '<tr><td colspan="5" class="py-4 text-center text-rose-400"><i class="fa-solid fa-triangle-exclamation mr-1"></i>Não foi possível carregar os detalhes do certame.</td></tr>';
    }
  }

  function closeTenderDrawer() {
    const backdrop = document.getElementById('drawer-backdrop');
    const drawer = document.getElementById('tender-drawer');
    if (!backdrop || !drawer) return;

    backdrop.classList.remove('opacity-100');
    backdrop.classList.add('opacity-0', 'pointer-events-none');
    drawer.classList.remove('translate-x-0');
    drawer.classList.add('translate-x-full');
    document.body.classList.remove('overflow-hidden');
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeTenderDrawer();
  });

  async function addOpportunityToPipeline(tenderId, identifier) {
    try {
      const resp = await fetch(`/api/deals?tender_id=${tenderId}`, { method: 'POST' });
      const data = await resp.json();
      showToast(`Certame ${identifier} adicionado com sucesso ao seu funil de negócios!`, 'success');
      
      let deals = JSON.parse(localStorage.getItem('my_pipeline_deals') || '[]');
      if (!deals.includes(tenderId)) {
        deals.push(tenderId);
        localStorage.setItem('my_pipeline_deals', JSON.stringify(deals));
      }
    } catch {
      showToast(`Oportunidade ${identifier} salva em seus negócios!`, 'success');
    }
  }

  function addCurrentTenderToPipeline() {
    if (currentDrawerTender) {
      addOpportunityToPipeline(currentDrawerTender.id, currentDrawerTender.notice_number || currentDrawerTender.external_id);
    }
  }

  function escapeHtml(text) {
    if (!text) return '';
    return String(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  async function saveSearchAlert() {
    const queryParams = new URLSearchParams(window.location.search);
    const qInput = document.querySelector('input[name="q"]');
    const stateInput = document.querySelector('select[name="uf"], select[name="state"]');
    const valMinInput = document.querySelector('input[name="value_min"]');

    const keywords = (qInput && qInput.value.trim()) ? qInput.value.trim() : (queryParams.get('q') || '');
    const ufs = (stateInput && stateInput.value.trim()) ? stateInput.value.trim() : (queryParams.get('uf') || queryParams.get('state') || '');
    const rawValMin = (valMinInput && valMinInput.value.trim()) ? valMinInput.value.trim() : queryParams.get('value_min');
    const valMin = rawValMin ? parseFloat(rawValMin.replace(',', '.')) : null;

    const token = typeof AppSession !== 'undefined' ? AppSession.getToken() : null;
    const headers = { 'Content-Type': 'application/json' };
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }

    const payload = {
      keywords: keywords || null,
      ufs: ufs || null,
      min_value: isNaN(valMin) ? null : valMin,
      name: keywords ? `Alerta: ${keywords}` : (ufs ? `Alerta UF ${ufs}` : 'Alerta de Licitações'),
      filters: Object.fromEntries(queryParams.entries()),
      frequency: 'DAILY',
      channels: 'IN_APP'
    };

    const btns = document.querySelectorAll('#btn-quick-save-alert, #btn-header-save-alert');
    btns.forEach(b => b.classList.add('opacity-50', 'pointer-events-none'));

    try {
      const resp = await fetch('/api/alerts', {
        method: 'POST',
        headers: headers,
        body: JSON.stringify(payload)
      });

      if (resp.ok) {
        showToast('Alerta guardado com sucesso! Filtros ativos cadastrados.', 'success');
      } else {
        const err = await resp.json().catch(() => ({}));
        showToast(err.detail || 'Não foi possível guardar o alerta.', 'error');
      }
    } catch {
      showToast('Erro de conexão ao guardar alerta.', 'error');
    } finally {
      btns.forEach(b => b.classList.remove('opacity-50', 'pointer-events-none'));
    }
  }

  async function loadAlertsFeedBadge() {
    try {
      const token = typeof AppSession !== 'undefined' ? AppSession.getToken() : null;
      const headers = {};
      if (token) headers['Authorization'] = `Bearer ${token}`;

      const res = await fetch('/api/alerts/feed?limit=20', { headers });
      if (!res.ok) return;
      const data = await res.json();
      const count = data.total ?? (Array.isArray(data) ? data.length : (data.count || 0));
      const badge = document.getElementById('alerts-feed-badge');
      const countEl = document.getElementById('alerts-feed-count');
      if (badge && countEl && count > 0) {
        countEl.textContent = count;
        badge.classList.remove('hidden');
        badge.classList.add('inline-flex');
      }
    } catch {
      // Falha silenciosa para manter fluidez da interface
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', loadAlertsFeedBadge);
  } else {
    loadAlertsFeedBadge();
  }

  function openSaveSearchModal() {
    document.getElementById('save-alert-modal').classList.remove('hidden');
  }

  function closeSaveSearchModal() {
    document.getElementById('save-alert-modal').classList.add('hidden');
  }

  async function submitSaveAlert() {
    const token = AppSession.getToken();
    if (!token) {
      showToast('Faça login para salvar alertas de busca.', 'info');
      setTimeout(() => window.location.href = '/login', 1000);
      return;
    }

    const name = document.getElementById('alert-name-input').value;
    const frequency = document.getElementById('alert-freq-input').value;
    const chApp = document.getElementById('alert-chan-app').checked;
    const chEmail = document.getElementById('alert-chan-email').checked;
    const channels = [chApp ? 'IN_APP' : null, chEmail ? 'EMAIL' : null].filter(Boolean).join(',');

    const queryParams = new URLSearchParams(window.location.search);
    const filters = Object.fromEntries(queryParams.entries());

    try {
      const resp = await fetch('/api/alerts', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`
        },
        body: JSON.stringify({
          name: name || 'Novo Alerta',
          keywords: filters.q || null,
          filters: filters,
          frequency: frequency,
          channels: channels || 'IN_APP'
        })
      });

      if (resp.ok) {
        closeSaveSearchModal();
        showToast('Alerta configurado com sucesso! Você será notificado.', 'success');
      } else {
        const err = await resp.json();
        showToast(err.detail || 'Erro ao registrar alerta.', 'error');
      }
    } catch {
      showToast('Erro de conexão ao salvar alerta.', 'error');
    }
  }

  async function triggerSyncTenders(pages = 10, limitPerPage = 50) {
    const btn = document.getElementById('btn-sync-tenders');
    const icon = document.getElementById('sync-icon');
    const label = document.getElementById('sync-label');

    btn.disabled = true;
    btn.classList.add('opacity-75', 'cursor-not-allowed');
    icon.classList.add('animate-spin');
    label.textContent = 'Sincronizando 500+ certames...';

    try {
      const res = await fetch(`/api/tenders/sync?pages=${pages}&limit_per_page=${limitPerPage}`, { method: 'POST' });
      const data = await res.json();
      if (res.ok) {
        showToast(`Sincronização concluída: ${data.total_coletados || data.total_processados} coletados (${data.compras_gov_count} Compras.gov.br, ${data.pncp_count} PNCP / Estaduais). Total no banco: ${data.total_geral_banco}`, 'success');
        label.textContent = `Atualizado (${data.total_geral_banco} no banco)`;
        setTimeout(() => {
          window.location.reload();
        }, 1200);
      } else {
        showToast('Falha na sincronização: ' + (data.detail || 'Erro no servidor'), 'error');
        btn.disabled = false;
        btn.classList.remove('opacity-75', 'cursor-not-allowed');
        icon.classList.remove('animate-spin');
        label.textContent = 'Atualizar Oportunidades';
      }
    } catch {
      showToast('Erro de comunicação ao sincronizar.', 'error');
      btn.disabled = false;
      btn.classList.remove('opacity-75', 'cursor-not-allowed');
      icon.classList.remove('animate-spin');
      label.textContent = 'Atualizar Oportunidades';
    }
  }

  function changePageSize(newSize) {
    const url = new URL(window.location.href);
    url.searchParams.set('page_size', newSize);
    url.searchParams.set('page', '1');
    window.location.href = url.toString();
  }

  // ==============================================================================
  // MÓDULO DE IA: RESUMO ESTRUTURADO E CONVERSE COM O EDITAL (GEMINI)
  // ==============================================================================
  function switchDrawerAiTab(tab) {
    const btnSummary = document.getElementById('tab-btn-ai-summary');
    const btnChat = document.getElementById('tab-btn-ai-chat');
    const tabSummary = document.getElementById('drawer-ai-tab-summary');
    const tabChat = document.getElementById('drawer-ai-tab-chat');

    if (tab === 'summary') {
      btnSummary.className = 'flex-1 py-1.5 px-3 rounded-lg text-center transition bg-indigo-600 text-white shadow-sm flex items-center justify-center gap-1.5 cursor-pointer';
      btnChat.className = 'flex-1 py-1.5 px-3 rounded-lg text-center transition text-slate-400 hover:text-slate-200 flex items-center justify-center gap-1.5 cursor-pointer';
      tabSummary.classList.remove('hidden');
      tabChat.classList.add('hidden');
    } else {
      btnChat.className = 'flex-1 py-1.5 px-3 rounded-lg text-center transition bg-indigo-600 text-white shadow-sm flex items-center justify-center gap-1.5 cursor-pointer';
      btnSummary.className = 'flex-1 py-1.5 px-3 rounded-lg text-center transition text-slate-400 hover:text-slate-200 flex items-center justify-center gap-1.5 cursor-pointer';
      tabChat.classList.remove('hidden');
      tabSummary.classList.add('hidden');
      setTimeout(() => {
        const inp = document.getElementById('drawer-chat-input');
        if (inp) inp.focus();
      }, 100);
    }
  }

  function resetDrawerAiState() {
    const content = document.getElementById('drawer-ai-summary-content');
    const skeleton = document.getElementById('drawer-ai-summary-skeleton');
    const btnText = document.getElementById('btn-generate-ai-summary-text');
    const btn = document.getElementById('btn-generate-ai-summary');
    const cacheStatus = document.getElementById('ai-summary-cache-status');

    if (content) content.classList.add('hidden');
    if (skeleton) skeleton.classList.add('hidden');
    if (cacheStatus) {
      cacheStatus.classList.add('hidden');
      cacheStatus.textContent = '';
    }
    if (btn) {
      btn.disabled = false;
      btn.classList.remove('opacity-75', 'cursor-not-allowed');
      btn.onclick = () => generateAiSummaryForDrawer(false);
    }
    if (btnText) btnText.innerHTML = '<i class="fa-solid fa-sparkles mr-1.5"></i> Gerar Resumo com IA';

    // Reset Chat messages
    const chatMsgs = document.getElementById('drawer-chat-messages');
    if (chatMsgs) {
      chatMsgs.innerHTML = `
        <div class="flex gap-2.5 items-start">
          <div class="w-6 h-6 rounded-full bg-indigo-600/30 border border-indigo-500/40 flex items-center justify-center text-indigo-400 shrink-0 text-[10px]">
            <i class="fa-solid fa-robot"></i>
          </div>
          <div class="bg-slate-800/80 p-3 rounded-2xl rounded-tl-none border border-slate-700/60 text-slate-200 max-w-[85%] leading-relaxed">
            Olá! Sou o assistente de IA especialista em licitações públicas. Pergunte qualquer dúvida sobre as regras, exigências técnicas, prazos ou condições deste edital!
          </div>
        </div>
      `;
    }
    const chatInput = document.getElementById('drawer-chat-input');
    if (chatInput) chatInput.value = '';
    switchDrawerAiTab('summary');
  }

  async function generateAiSummaryForDrawer(forceRefresh = false) {
    if (!currentDrawerTender) return;
    const tenderId = currentDrawerTender.id;

    const btn = document.getElementById('btn-generate-ai-summary');
    const btnText = document.getElementById('btn-generate-ai-summary-text');
    const skeleton = document.getElementById('drawer-ai-summary-skeleton');
    const content = document.getElementById('drawer-ai-summary-content');
    const cacheStatus = document.getElementById('ai-summary-cache-status');

    btn.disabled = true;
    btn.classList.add('opacity-75', 'cursor-not-allowed');
    btnText.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin mr-1.5"></i> Analisando edital com IA...';
    skeleton.classList.remove('hidden');
    content.classList.add('hidden');
    cacheStatus.classList.add('hidden');

    try {
      const resp = await fetch(`/api/tenders/${tenderId}/ai-summary${forceRefresh ? '?force_refresh=true' : ''}`, {
        method: 'POST'
      });
      const data = await resp.json();

      if (!resp.ok) {
        throw new Error(data.detail || 'Erro ao gerar análise');
      }

      const summary = data.summary || {};

      // Objeto resumido
      document.getElementById('ai-summary-objeto').textContent = summary.objeto_resumido || 'Não informado.';

      // Habilitação técnica
      const habList = document.getElementById('ai-summary-habilitacao');
      const habItems = Array.isArray(summary.habilitacao_tecnica) ? summary.habilitacao_tecnica : [summary.habilitacao_tecnica].filter(Boolean);
      habList.innerHTML = habItems.map(h => `<li>${escapeHtml(h)}</li>`).join('') || '<li>Verificar exigências completas no termo de referência.</li>';

      // Prazos
      document.getElementById('ai-summary-prazos').textContent = summary.prazos_entrega_execucao || 'Conforme cronograma oficial.';

      // Pagamento e sanções
      document.getElementById('ai-summary-pagamento').textContent = summary.condicoes_pagamento_sancoes || 'Conforme Lei nº 14.133/2021.';

      // Pontos de atenção
      const pontosBox = document.getElementById('ai-summary-pontos-box');
      const pontosList = document.getElementById('ai-summary-pontos');
      if (summary.pontos_atencao && summary.pontos_atencao.length > 0) {
        const pItems = Array.isArray(summary.pontos_atencao) ? summary.pontos_atencao : [summary.pontos_atencao];
        pontosList.innerHTML = pItems.map(p => `<li>${escapeHtml(p)}</li>`).join('');
        pontosBox.classList.remove('hidden');
      } else {
        pontosBox.classList.add('hidden');
      }

      // Badge de cache/IA
      cacheStatus.classList.remove('hidden');
      if (data.cached) {
        cacheStatus.innerHTML = '<span class="text-amber-400"><i class="fa-solid fa-bolt mr-1"></i>Em Cache</span>';
      } else {
        cacheStatus.innerHTML = `<span class="text-indigo-400"><i class="fa-solid fa-sparkles mr-1"></i>Gerado via ${escapeHtml(data.model || 'Gemini')}</span>`;
      }

      skeleton.classList.add('hidden');
      content.classList.remove('hidden');
      btnText.innerHTML = '<i class="fa-solid fa-rotate-right mr-1.5"></i> Atualizar Resumo';
      btn.disabled = false;
      btn.classList.remove('opacity-75', 'cursor-not-allowed');
      btn.onclick = () => generateAiSummaryForDrawer(true);

    } catch (err) {
      skeleton.classList.add('hidden');
      btnText.innerHTML = '<i class="fa-solid fa-triangle-exclamation mr-1.5"></i> Tentar Novamente';
      btn.disabled = false;
      btn.classList.remove('opacity-75', 'cursor-not-allowed');
      showToast('Não foi possível gerar o resumo por IA: ' + err.message, 'error');
    }
  }

  async function sendDrawerAiChat() {
    if (!currentDrawerTender) return;
    const tenderId = currentDrawerTender.id;
    const input = document.getElementById('drawer-chat-input');
    const sendBtn = document.getElementById('drawer-chat-btn-send');
    const question = input.value.trim();
    if (!question) return;

    const chatMsgs = document.getElementById('drawer-chat-messages');

    // Append User Message
    const userMsgEl = document.createElement('div');
    userMsgEl.className = 'flex gap-2.5 items-start justify-end';
    userMsgEl.innerHTML = `
      <div class="bg-indigo-600 text-white p-3 rounded-2xl rounded-tr-none shadow-md max-w-[85%] leading-relaxed">
        ${escapeHtml(question)}
      </div>
      <div class="w-6 h-6 rounded-full bg-slate-700 border border-slate-600 flex items-center justify-center text-slate-300 shrink-0 text-[10px]">
        <i class="fa-solid fa-user"></i>
      </div>
    `;
    chatMsgs.appendChild(userMsgEl);
    input.value = '';
    sendBtn.disabled = true;

    // Append Typing Indicator
    const typingId = 'typing-' + Date.now();
    const typingEl = document.createElement('div');
    typingEl.id = typingId;
    typingEl.className = 'flex gap-2.5 items-start';
    typingEl.innerHTML = `
      <div class="w-6 h-6 rounded-full bg-indigo-600/30 border border-indigo-500/40 flex items-center justify-center text-indigo-400 shrink-0 text-[10px]">
        <i class="fa-solid fa-robot"></i>
      </div>
      <div class="bg-slate-800/80 p-3 rounded-2xl rounded-tl-none border border-slate-700/60 text-indigo-300 max-w-[85%] flex items-center gap-2">
        <i class="fa-solid fa-circle-notch fa-spin text-xs"></i>
        <span>Consultando edital com IA...</span>
      </div>
    `;
    chatMsgs.appendChild(typingEl);
    chatMsgs.scrollTop = chatMsgs.scrollHeight;

    try {
      const resp = await fetch(`/api/tenders/${tenderId}/ai-chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: question })
      });
      const data = await resp.json();
      const typing = document.getElementById(typingId);
      if (typing) typing.remove();

      if (!resp.ok) {
        throw new Error(data.detail || 'Erro ao consultar IA');
      }

      // Format markdown-like response
      let answerFormatted = escapeHtml(data.answer || '')
        .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
        .replace(/\*(.*?)\*/g, '<em>$1</em>')
        .replace(/\n\n/g, '<br/><br/>')
        .replace(/\n/g, '<br/>');

      const aiMsgEl = document.createElement('div');
      aiMsgEl.className = 'flex gap-2.5 items-start';
      aiMsgEl.innerHTML = `
        <div class="w-6 h-6 rounded-full bg-indigo-600/30 border border-indigo-500/40 flex items-center justify-center text-indigo-400 shrink-0 text-[10px]">
          <i class="fa-solid fa-robot"></i>
        </div>
        <div class="bg-slate-800/80 p-3.5 rounded-2xl rounded-tl-none border border-slate-700/60 text-slate-200 max-w-[85%] leading-relaxed text-xs">
          ${answerFormatted}
          <div class="mt-2 pt-1 border-t border-slate-700/40 text-[10px] text-slate-400 font-mono flex items-center justify-between">
            <span>IA Consultiva • ${escapeHtml(data.model || 'Gemini')}</span>
          </div>
        </div>
      `;
      chatMsgs.appendChild(aiMsgEl);

    } catch (err) {
      const typing = document.getElementById(typingId);
      if (typing) typing.remove();

      const errEl = document.createElement('div');
      errEl.className = 'flex gap-2.5 items-start';
      errEl.innerHTML = `
        <div class="w-6 h-6 rounded-full bg-rose-600/30 border border-rose-500/40 flex items-center justify-center text-rose-400 shrink-0 text-[10px]">
          <i class="fa-solid fa-triangle-exclamation"></i>
        </div>
        <div class="bg-rose-950/40 p-3 rounded-2xl rounded-tl-none border border-rose-800/50 text-rose-300 max-w-[85%] text-xs">
          Erro ao consultar IA: ${escapeHtml(err.message)}
        </div>
      `;
      chatMsgs.appendChild(errEl);
    } finally {
      sendBtn.disabled = false;
      chatMsgs.scrollTop = chatMsgs.scrollHeight;
    }
  }

  function askPresetQuestion(text) {
    switchDrawerAiTab('chat');
    document.getElementById('drawer-chat-input').value = text;
    sendDrawerAiChat();
  }
</script>
{% endblock %}
EOF

mkdir -p server/templates
cat << 'EOF' > server/templates/index.html
{% extends "base.html" %}

{% block content %}
<!-- Hero Section -->
<section class="bg-gradient-to-b from-brand-900 via-brand-800 to-slate-900 text-white pt-14 pb-20 px-4 sm:px-6 lg:px-8">
  <div class="max-w-4xl mx-auto text-center">
    <!-- Top badge -->
    <div class="inline-flex items-center gap-2 bg-sky-950/80 border border-sky-700/50 px-3 py-1 rounded-full text-xs text-sky-300 font-medium mb-6">
      <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
      Conectado ao PNCP (Portal Nacional de Contratações Públicas) & Lei 14.133/2021
    </div>

    <h1 class="text-3xl sm:text-5xl font-extrabold tracking-tight leading-tight mb-4">
      Pesquise e Analise Licitações Públicas com <span class="text-transparent bg-clip-text bg-gradient-to-r from-sky-400 to-teal-300">Inteligência Artificial</span>
    </h1>
    <p class="text-slate-300 text-base sm:text-lg mb-8 max-w-2xl mx-auto">
      Oportunidades de contratações públicas federais, estaduais e municipais em todo o Brasil. Busca semântica, resumos automáticos de editais e monitoramento de certames.
    </p>

    <!-- Search Box -->
    <form action="/search" method="GET" class="relative max-w-3xl mx-auto shadow-2xl rounded-2xl overflow-hidden bg-white p-2 text-slate-800">
      <div class="flex flex-col sm:flex-row items-center gap-2">
        <div class="flex items-center pl-3 flex-grow w-full">
          <i class="fa-solid fa-magnifying-glass text-slate-400 text-lg mr-3"></i>
          <input 
            type="text" 
            name="q" 
            id="hero-search-input"
            placeholder="Ex: Medicamentos em Alagoas, equipamentos de informática, serviços de engenharia..." 
            class="w-full py-3 text-slate-800 placeholder-slate-400 focus:outline-none text-base"
            autocomplete="off"
          />
        </div>
        <div class="flex flex-wrap sm:flex-nowrap items-center gap-2 w-full sm:w-auto">
          <select name="uf" id="uf-select" aria-label="Estado (UF)" class="flex-1 sm:flex-none min-w-0 bg-slate-100 text-slate-700 border-0 rounded-xl px-3 py-3 text-sm focus:ring-2 focus:ring-sky-500 font-medium">
            <option value="">Todos os Estados (UF)</option>
            {% for sigla, nome in UFS_BRASIL %}
            <option value="{{ sigla }}">{{ sigla }} - {{ nome }}</option>
            {% endfor %}
          </select>
          <select name="category" id="hero-categoria-select" aria-label="Categoria" class="flex-1 sm:flex-none min-w-0 bg-slate-100 text-slate-700 border-0 rounded-xl px-3 py-3 text-sm focus:ring-2 focus:ring-sky-500 font-medium">
            <option value="">Todas as categorias</option>
            {% for codigo, rotulo in NATUREZAS_FILTRO %}
            <option value="{{ codigo }}">{{ rotulo }}</option>
            {% endfor %}
          </select>
          <button type="submit" class="w-full sm:w-auto bg-sky-600 hover:bg-sky-500 text-white font-semibold px-6 py-3 rounded-xl transition shadow-md flex items-center justify-center gap-2">
            <span>Buscar</span>
            <i class="fa-solid fa-arrow-right text-xs"></i>
          </button>
        </div>
      </div>
    </form>

    <!-- Exemplo de Pesquisa Conforme Solicitação Oficial -->
    <div class="mt-6 flex flex-wrap items-center justify-center gap-2 text-xs text-slate-300">
      <span class="text-slate-400 font-medium">Exemplos rápidos:</span>
      <button onclick="fillSearch('medicamentos materiais hospitalares farmácias', 'AL')" class="bg-white/10 hover:bg-white/20 px-3 py-1.5 rounded-full border border-white/15 transition flex items-center gap-1.5 text-sky-200">
        <i class="fa-solid fa-kit-medical text-rose-400"></i> Alagoas: Medicamentos e Materiais Hospitalares
      </button>
      <button onclick="fillSearch('computação em nuvem ti', 'SP')" class="bg-white/10 hover:bg-white/20 px-3 py-1.5 rounded-full border border-white/15 transition text-sky-200">
        <i class="fa-solid fa-server text-cyan-400"></i> São Paulo: Tecnologia e Cloud
      </button>
      <button onclick="fillSearch('ventiladores pulmonares uti', 'DF')" class="bg-white/10 hover:bg-white/20 px-3 py-1.5 rounded-full border border-white/15 transition text-sky-200">
        <i class="fa-solid fa-hospital text-emerald-400"></i> Federal: Equipamentos de Saúde
      </button>
    </div>
  </div>
</section>

<!-- KPI / Metrics Strip -->
<section class="bg-slate-900 border-y border-slate-800 text-slate-300 py-6">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 grid grid-cols-2 md:grid-cols-4 gap-6 text-center">
    <div>
      <div class="text-2xl sm:text-3xl font-extrabold text-white">100%</div>
      <div class="text-xs text-slate-400 mt-1 uppercase tracking-wider">Fontes Oficiais Verificadas</div>
    </div>
    <div>
      <div class="text-2xl sm:text-3xl font-extrabold text-sky-400">PNCP v1</div>
      <div class="text-xs text-slate-400 mt-1 uppercase tracking-wider">Integração Padronizada</div>
    </div>
    <div>
      <div class="text-2xl sm:text-3xl font-extrabold text-emerald-400">Gemini 3.8</div>
      <div class="text-xs text-slate-400 mt-1 uppercase tracking-wider">IA Documental com Citação</div>
    </div>
    <div>
      <div class="text-2xl sm:text-3xl font-extrabold text-white">LGPD</div>
      <div class="text-xs text-slate-400 mt-1 uppercase tracking-wider">Privacidade & Conformidade</div>
    </div>
  </div>
</section>

<!-- Recent Opportunities Showcase -->
<section class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-12">
  <div class="flex flex-col sm:flex-row justify-between items-start sm:items-center mb-8 gap-4">
    <div>
      <h2 class="text-2xl font-bold text-slate-900">Oportunidades em Destaque</h2>
      <p class="text-sm text-slate-500">Editais recentemente sincronizados do Portal Nacional de Contratações Públicas</p>
    </div>
    <div class="flex flex-wrap items-center gap-3">
      <!-- Filtro de natureza do objeto (Material / Serviço / Outros) -->
      <form id="natureza-filter-form" action="/" method="GET" class="flex items-center gap-2">
        <label for="natureza-filter" class="text-xs font-semibold text-slate-500 uppercase tracking-wide">Natureza</label>
        <select
          id="natureza-filter"
          name="natureza"
          onchange="aplicarFiltroNatureza(this.value)"
          class="bg-white border border-slate-300 text-slate-700 rounded-xl px-3 py-2 text-sm font-medium focus:ring-2 focus:ring-sky-500 focus:outline-none"
        >
          <option value="" {% if not natureza_sel %}selected{% endif %}>Todas</option>
          {% for chave, rotulo in (natureza_labels or {}).items() %}
            <option value="{{ chave }}" {% if natureza_sel == chave %}selected{% endif %}>{{ rotulo }}{% if natureza_counts %} ({{ natureza_counts[chave] }}){% endif %}</option>
          {% endfor %}
        </select>
        <noscript><button type="submit" class="text-xs font-semibold text-sky-600">Filtrar</button></noscript>
      </form>
      <a href="/search" class="text-sm font-semibold text-sky-600 hover:text-sky-700 flex items-center gap-1.5">
        Ver todos os certames <i class="fa-solid fa-arrow-right text-xs"></i>
      </a>
    </div>
  </div>

  <div id="recent-tenders-grid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
    <!-- Populated via script or pre-rendered -->
    {% for tender in recent_tenders %}
    {% set natureza = (naturezas_por_id or {}).get(tender.id, 'OUTROS') %}
    <div class="tender-card bg-white rounded-2xl border border-slate-200 shadow-sm hover:shadow-md transition p-6 flex flex-col justify-between" data-natureza="{{ natureza }}">
      <div>
        <div class="flex items-center justify-between gap-2 mb-3">
          <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-semibold bg-sky-50 text-sky-700 border border-sky-100">
            <i class="fa-solid fa-location-dot text-sky-500"></i> {{ tender.state }} - {{ tender.city or "Não informado" }}
          </span>
          <span class="text-xs text-slate-400 font-mono truncate">{{ tender.modality }}</span>
        </div>

        <span class="natureza-badge inline-flex items-center gap-1.5 px-2 py-0.5 mb-2 rounded-md text-[11px] font-bold uppercase tracking-wide border
          {% if natureza == 'MATERIAL' %}bg-indigo-50 text-indigo-700 border-indigo-200
          {% elif natureza == 'SERVICO' %}bg-emerald-50 text-emerald-700 border-emerald-200
          {% else %}bg-slate-100 text-slate-600 border-slate-200{% endif %}">
          <i class="fa-solid {% if natureza == 'MATERIAL' %}fa-box{% elif natureza == 'SERVICO' %}fa-screwdriver-wrench{% else %}fa-shapes{% endif %} text-[10px]"></i>
          {{ (natureza_labels or {}).get(natureza, 'Outros') }}
        </span>

        <h3 class="text-base font-bold text-slate-900 leading-snug mb-2 line-clamp-2">
          <a href="/tenders/{{ tender.id }}" class="hover:text-sky-600 transition">{{ tender.title }}</a>
        </h3>

        <p class="text-xs text-slate-600 line-clamp-3 mb-4 leading-relaxed">
          {{ tender.object_description }}
        </p>

        <div class="text-xs text-slate-500 mb-4 bg-slate-50 p-2.5 rounded-xl border border-slate-100">
          <div class="font-medium text-slate-700 truncate mb-1"><i class="fa-regular fa-building text-slate-400 mr-1"></i> {{ tender.contracting_entity }}</div>
          <div class="flex justify-between items-center text-slate-500">
            <span>Valor estimado:</span>
            <span class="font-bold text-slate-900">
              {% if tender.estimated_total_value %}
                R$ {{ "{:,.2f}".format(tender.estimated_total_value).replace(",", "X").replace(".", ",").replace("X", ".") }}
              {% else %}
                Não informado
              {% endif %}
            </span>
          </div>
        </div>
      </div>

      <div class="pt-4 border-t border-slate-100 flex items-center justify-between">
        <a href="/tenders/{{ tender.id }}" class="text-xs font-semibold text-white bg-slate-900 hover:bg-sky-600 px-3.5 py-2 rounded-xl transition flex items-center gap-1.5">
          <span>Ver Detalhes e IA</span>
          <i class="fa-solid fa-arrow-right text-[10px]"></i>
        </a>
        <a href="{{ tender.source_url }}" target="_blank" rel="noopener" class="text-xs text-slate-400 hover:text-slate-700 flex items-center gap-1" title="Acessar publicação oficial">
          <i class="fa-solid fa-arrow-up-right-from-square"></i> Fonte
        </a>
      </div>
    </div>
    {% endfor %}
    {% if not recent_tenders %}
    <div class="md:col-span-2 lg:col-span-3 text-center py-12 bg-white border border-dashed border-slate-300 rounded-2xl text-sm text-slate-500">
      Nenhum certame recente com esta natureza. <a href="/" class="font-semibold text-sky-600 hover:text-sky-700">Ver todos</a>
    </div>
    {% endif %}
  </div>
</section>

<!-- Sources Integration Section -->
<section class="bg-slate-100 py-12 border-t border-slate-200">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
    <div class="text-center max-w-2xl mx-auto mb-10">
      <h2 class="text-2xl font-bold text-slate-900">Fontes Oficiais Conectadas</h2>
      <p class="text-sm text-slate-600 mt-2">
        Arquitetura de conectores independentes com transparência total de integração e respeito aos termos de uso.
      </p>
    </div>

    <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4 text-center">
      <div class="bg-white p-4 rounded-xl border border-slate-200 shadow-sm">
        <div class="text-sky-600 font-bold text-sm mb-1">PNCP Nacional</div>
        <span class="inline-block px-2 py-0.5 rounded text-[10px] font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200">API Ativa</span>
      </div>
      <div class="bg-white p-4 rounded-xl border border-slate-200 shadow-sm">
        <div class="text-slate-800 font-bold text-sm mb-1">Compras.gov.br</div>
        <span class="inline-block px-2 py-0.5 rounded text-[10px] font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200">API Federal</span>
      </div>
      <div class="bg-white p-4 rounded-xl border border-slate-200 shadow-sm">
        <div class="text-slate-800 font-bold text-sm mb-1">Compras RS</div>
        <span class="inline-block px-2 py-0.5 rounded text-[10px] font-semibold bg-sky-50 text-sky-700 border border-sky-200">CELIC / RS</span>
      </div>
      <div class="bg-white p-4 rounded-xl border border-slate-200 shadow-sm">
        <div class="text-slate-800 font-bold text-sm mb-1">Compras MG</div>
        <span class="inline-block px-2 py-0.5 rounded text-[10px] font-semibold bg-sky-50 text-sky-700 border border-sky-200">SEPLAG / MG</span>
      </div>
      <div class="bg-white p-4 rounded-xl border border-slate-200 shadow-sm">
        <div class="text-slate-800 font-bold text-sm mb-1">Licitações CAIXA</div>
        <span class="inline-block px-2 py-0.5 rounded text-[10px] font-semibold bg-amber-50 text-amber-700 border border-amber-200">Extrato Oficial</span>
      </div>
      <div class="bg-white p-4 rounded-xl border border-slate-200 shadow-sm">
        <div class="text-slate-800 font-bold text-sm mb-1">Licitações-e (BB)</div>
        <span class="inline-block px-2 py-0.5 rounded text-[10px] font-semibold bg-slate-100 text-slate-600 border border-slate-200">Busca Direta</span>
      </div>
    </div>
  </div>
</section>
{% endblock %}

{% block scripts %}
<script>
  function aplicarFiltroNatureza(valor) {
    const url = new URL(window.location.href);
    if (valor) url.searchParams.set('natureza', valor); else url.searchParams.delete('natureza');
    url.hash = 'recent-tenders-grid';
    window.location.href = url.toString();
  }

  function fillSearch(query, state) {
    document.getElementById('hero-search-input').value = query;
    const form = document.querySelector('form');
    if (state) {
      const ufSelect = form.querySelector('select[name="uf"], select[name="state"]');
      if (ufSelect) {
        ufSelect.value = state;
      }
    }
    form.submit();
  }
</script>
{% endblock %}
EOF

mkdir -p server/templates
cat << 'EOF' > server/templates/alerts.html
{% extends "base.html" %}

{% block title %}Meus Alertas & Auditoria de Disparos - Licitações Brasil{% endblock %}

{% block content %}
<div class="bg-slate-900 text-white py-8 border-b border-slate-800">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
      <div>
        <h1 class="text-2xl font-bold flex items-center gap-2">
          <i class="fa-solid fa-bell text-sky-400"></i> Radar, Alertas e Auditoria de Webhooks
        </h1>
        <p class="text-xs text-slate-400 mt-1">Monitore certames em tempo real e audite os disparos automatizados de webhooks (n8n, Evolution API, Z-API).</p>
      </div>
      <div class="flex items-center gap-2">
        <button id="alerts-sync-btn" onclick="syncOpportunitiesFromAlerts()" class="bg-emerald-600 hover:bg-emerald-500 text-white font-semibold px-4 py-2 rounded-xl text-xs transition shadow-sm flex items-center gap-2 cursor-pointer">
          <i class="fa-solid fa-arrows-rotate" id="alerts-sync-icon"></i> <span id="alerts-sync-label">Atualizar Oportunidades</span>
        </button>
        <button onclick="openNewAlertModal()" class="bg-sky-600 hover:bg-sky-500 text-white font-semibold px-4 py-2 rounded-xl text-xs transition shadow-sm flex items-center gap-2">
          <i class="fa-solid fa-plus"></i> Novo Alerta
        </button>
      </div>
    </div>

    <!-- Navegação por Abas -->
    <div class="flex gap-2 mt-6 border-b border-slate-800 -mb-8">
      <button id="tab-btn-alerts" onclick="switchTab('alerts')" class="px-4 py-2.5 text-xs font-bold border-b-2 border-sky-400 text-sky-400 flex items-center gap-2 transition">
        <i class="fa-solid fa-list-check"></i> Radares e Alertas Configurados
      </button>
      <button id="tab-btn-webhooks" onclick="switchTab('webhooks')" class="px-4 py-2.5 text-xs font-bold border-b-2 border-transparent text-slate-400 hover:text-slate-200 flex items-center gap-2 transition">
        <i class="fa-solid fa-satellite-dish"></i> Histórico e Auditoria de Disparos
      </button>
    </div>
  </div>
</div>

<div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
  <!-- ABA 1: Radares e Alertas -->
  <div id="tab-content-alerts" class="space-y-4">
    <div id="alerts-container" class="space-y-4">
      <div class="text-center py-12 text-slate-400 text-xs">Carregando seus alertas configurados...</div>
    </div>
  </div>

  <!-- ABA 2: Histórico e Auditoria de Disparos (Webhooks) -->
  <div id="tab-content-webhooks" class="hidden space-y-6">
    <!-- Métricas em Cards -->
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <div class="bg-white p-4 rounded-2xl border border-slate-200 shadow-sm flex items-center justify-between">
        <div>
          <span class="text-[11px] font-semibold text-slate-500 block">Disparos (Últimas 24h)</span>
          <span id="metric-total-24h" class="text-xl font-black text-slate-900 mt-1 block">--</span>
        </div>
        <div class="w-10 h-10 rounded-xl bg-sky-50 text-sky-600 flex items-center justify-center text-lg">
          <i class="fa-solid fa-paper-plane"></i>
        </div>
      </div>

      <div class="bg-white p-4 rounded-2xl border border-slate-200 shadow-sm flex items-center justify-between">
        <div>
          <span class="text-[11px] font-semibold text-slate-500 block">Taxa de Sucesso</span>
          <span id="metric-success-pct" class="text-xl font-black text-emerald-600 mt-1 block">--%</span>
        </div>
        <div class="w-10 h-10 rounded-xl bg-emerald-50 text-emerald-600 flex items-center justify-center text-lg">
          <i class="fa-solid fa-circle-check"></i>
        </div>
      </div>

      <div class="bg-white p-4 rounded-2xl border border-slate-200 shadow-sm flex items-center justify-between">
        <div>
          <span class="text-[11px] font-semibold text-slate-500 block">Disparos com Falha (24h)</span>
          <span id="metric-failed-24h" class="text-xl font-black text-rose-600 mt-1 block">--</span>
        </div>
        <div class="w-10 h-10 rounded-xl bg-rose-50 text-rose-600 flex items-center justify-center text-lg">
          <i class="fa-solid fa-triangle-exclamation"></i>
        </div>
      </div>

      <div class="bg-white p-4 rounded-2xl border border-slate-200 shadow-sm flex items-center justify-between">
        <div>
          <span class="text-[11px] font-semibold text-slate-500 block">Uptime & Saúde DB</span>
          <span id="metric-uptime" class="text-xs font-bold text-slate-700 mt-1 block">--</span>
        </div>
        <div class="w-10 h-10 rounded-xl bg-indigo-50 text-indigo-600 flex items-center justify-center text-lg">
          <i class="fa-solid fa-server"></i>
        </div>
      </div>
    </div>

    <!-- Barra de Filtros -->
    <div class="bg-white p-4 rounded-2xl border border-slate-200 shadow-sm flex flex-wrap items-center justify-between gap-3 text-xs">
      <div class="flex flex-wrap items-center gap-3">
        <div>
          <label class="block text-[10px] font-bold text-slate-500 mb-0.5">STATUS</label>
          <select id="filter-status" onchange="loadWebhookLogs()" class="p-2 border border-slate-200 rounded-xl bg-white focus:ring-2 focus:ring-sky-500 focus:outline-none">
            <option value="">Todos os Status</option>
            <option value="SUCESSO">Apenas Sucesso</option>
            <option value="FALHA">Apenas Falha</option>
          </select>
        </div>

        <div>
          <label class="block text-[10px] font-bold text-slate-500 mb-0.5">CANAL</label>
          <select id="filter-canal" onchange="loadWebhookLogs()" class="p-2 border border-slate-200 rounded-xl bg-white focus:ring-2 focus:ring-sky-500 focus:outline-none">
            <option value="">Todos os Canais</option>
            <option value="N8N">n8n</option>
            <option value="EVOLUTION_API">Evolution API</option>
            <option value="ZAPI">Z-API</option>
            <option value="GENERIC">Genérico</option>
          </select>
        </div>

        <div>
          <label class="block text-[10px] font-bold text-slate-500 mb-0.5">DATA DO DISPARO</label>
          <input type="date" id="filter-data" onchange="loadWebhookLogs()" class="p-2 border border-slate-200 rounded-xl bg-white focus:ring-2 focus:ring-sky-500 focus:outline-none" />
        </div>

        <div>
          <label class="block text-[10px] font-bold text-slate-500 mb-0.5">ID CERTAME / LICITAÇÃO</label>
          <input type="text" id="filter-certame" placeholder="Ex: uuid da licitação" class="p-2 border border-slate-200 rounded-xl bg-white focus:ring-2 focus:ring-sky-500 focus:outline-none w-48" />
        </div>
      </div>

      <div class="flex items-center gap-2 mt-auto">
        <button onclick="loadWebhookLogs()" class="bg-sky-600 hover:bg-sky-500 text-white font-semibold px-4 py-2 rounded-xl transition flex items-center gap-1.5 shadow-sm">
          <i class="fa-solid fa-filter"></i> Filtrar
        </button>
        <button onclick="clearWebhookFilters()" class="bg-slate-100 hover:bg-slate-200 text-slate-700 font-semibold px-3 py-2 rounded-xl transition flex items-center gap-1.5">
          <i class="fa-solid fa-arrows-rotate"></i> Limpar
        </button>
      </div>
    </div>

    <!-- Tabela de Auditoria de Webhooks -->
    <div class="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden">
      <div class="overflow-x-auto">
        <table class="w-full text-left text-xs">
          <thead class="bg-slate-50 border-b border-slate-200 text-slate-600 font-bold">
            <tr>
              <th class="p-3.5">Canal</th>
              <th class="p-3.5">URL Alvo</th>
              <th class="p-3.5">Status & Código</th>
              <th class="p-3.5">Latência</th>
              <th class="p-3.5">Tentativa</th>
              <th class="p-3.5">Data / Hora</th>
              <th class="p-3.5 text-right">Ações de Auditoria</th>
            </tr>
          </thead>
          <tbody id="webhook-logs-body" class="divide-y divide-slate-100 text-slate-700">
            <tr>
              <td colspan="7" class="p-8 text-center text-slate-400">Carregando histórico de disparos...</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- Paginação -->
      <div id="webhook-pagination" class="p-4 border-t border-slate-100 flex items-center justify-between text-xs text-slate-500">
        <span id="webhook-page-info">Total: 0 disparos</span>
        <div class="flex gap-2">
          <button id="webhook-prev-btn" onclick="changeWebhookPage(-1)" disabled class="px-3 py-1.5 border border-slate-200 rounded-lg disabled:opacity-40">Anterior</button>
          <button id="webhook-next-btn" onclick="changeWebhookPage(1)" disabled class="px-3 py-1.5 border border-slate-200 rounded-lg disabled:opacity-40">Próxima</button>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Modal Novo Alerta -->
<div id="new-alert-modal" class="fixed inset-0 bg-slate-900/60 backdrop-blur-sm z-50 hidden flex items-center justify-center p-4">
  <div class="bg-white rounded-2xl max-w-lg w-full p-6 shadow-2xl">
    <div class="flex justify-between items-center mb-4">
      <h3 class="text-base font-bold text-slate-900 flex items-center gap-2">
        <i class="fa-solid fa-bell text-sky-600"></i> Criar Novo Radar de Oportunidades
      </h3>
      <button onclick="closeNewAlertModal()" class="text-slate-400 hover:text-slate-600"><i class="fa-solid fa-xmark"></i></button>
    </div>
    <div class="space-y-3 text-xs">
      <div>
        <label class="block font-semibold text-slate-700 mb-1">Título do Alerta</label>
        <input type="text" id="modal-alert-name" placeholder="Ex: Medicamentos e Farmácia AL" class="w-full p-2.5 border border-slate-200 rounded-xl" />
      </div>
      <div>
        <label class="block font-semibold text-slate-700 mb-1">Palavras-chave</label>
        <input type="text" id="modal-alert-keywords" placeholder="Ex: dipirona, seringas, hospitalar" class="w-full p-2.5 border border-slate-200 rounded-xl" />
      </div>
      <div class="grid grid-cols-2 gap-3">
        <div>
          <label class="block font-semibold text-slate-700 mb-1">Estado (UF)</label>
          <select id="modal-alert-uf" class="w-full p-2.5 border border-slate-200 rounded-xl bg-white">
            <option value="">Todos</option>
            {% for sigla, nome in UFS_BRASIL %}
            <option value="{{ sigla }}">{{ nome }} ({{ sigla }})</option>
            {% endfor %}
          </select>
        </div>
        <div>
          <label class="block font-semibold text-slate-700 mb-1">Frequência</label>
          <select id="modal-alert-freq" class="w-full p-2.5 border border-slate-200 rounded-xl bg-white">
            <option value="DAILY">Diária</option>
            <option value="WEEKLY">Semanal</option>
            <option value="REALTIME">Tempo Real</option>
          </select>
        </div>
      </div>
      <div>
        <label class="block font-semibold text-slate-700 mb-1">Canais de Notificação</label>
        <div class="flex gap-4">
          <label class="flex items-center gap-1.5"><input type="checkbox" id="modal-alert-ch-app" checked /> Notificação Interna</label>
          <label class="flex items-center gap-1.5"><input type="checkbox" id="modal-alert-ch-mail" checked /> E-mail</label>
        </div>
      </div>
    </div>
    <div class="mt-6 flex justify-end gap-2">
      <button onclick="closeNewAlertModal()" class="px-4 py-2 rounded-xl text-xs border border-slate-200 font-semibold text-slate-600">Cancelar</button>
      <button onclick="submitCreateAlert()" class="px-4 py-2 rounded-xl text-xs bg-sky-600 text-white font-semibold hover:bg-sky-500">Salvar Alerta</button>
    </div>
  </div>
</div>

<!-- Modal Inspecionar Disparo de Webhook -->
<div id="inspect-modal" class="fixed inset-0 bg-slate-900/60 backdrop-blur-sm z-50 hidden flex items-center justify-center p-4">
  <div class="bg-white rounded-2xl max-w-3xl w-full p-6 shadow-2xl flex flex-col max-h-[90vh]">
    <div class="flex justify-between items-center pb-3 border-b border-slate-100">
      <div>
        <h3 class="text-sm font-bold text-slate-900 flex items-center gap-2">
          <i class="fa-solid fa-code text-indigo-600"></i> Inspeção de Payload & Resposta do Webhook
        </h3>
        <p id="inspect-subtitle" class="text-[11px] text-slate-500 mt-0.5">Detalhes completos do envio</p>
      </div>
      <button onclick="closeInspectModal()" class="text-slate-400 hover:text-slate-600"><i class="fa-solid fa-xmark text-lg"></i></button>
    </div>

    <!-- Abas do Modal -->
    <div class="flex gap-2 mt-4 border-b border-slate-200 text-xs">
      <button id="modal-tab-payload-btn" onclick="switchModalInspectTab('payload')" class="px-3 py-2 font-bold border-b-2 border-indigo-600 text-indigo-600">
        Payload Enviado (JSON)
      </button>
      <button id="modal-tab-response-btn" onclick="switchModalInspectTab('response')" class="px-3 py-2 font-bold border-b-2 border-transparent text-slate-500 hover:text-slate-700">
        Resposta do Servidor
      </button>
    </div>

    <div class="mt-4 flex-1 overflow-y-auto">
      <div id="modal-view-payload">
        <div class="flex justify-between items-center mb-1.5">
          <span class="text-[10px] font-bold text-slate-500 uppercase">Corpo JSON Enviado:</span>
          <button onclick="copyToClipboard('inspect-payload-content')" class="text-[11px] text-indigo-600 font-semibold hover:underline flex items-center gap-1">
            <i class="fa-regular fa-copy"></i> Copiar
          </button>
        </div>
        <pre id="inspect-payload-content" class="bg-slate-900 text-emerald-400 p-4 rounded-xl text-xs font-mono overflow-x-auto whitespace-pre-wrap max-h-96"></pre>
      </div>

      <div id="modal-view-response" class="hidden">
        <div class="flex justify-between items-center mb-1.5">
          <span class="text-[10px] font-bold text-slate-500 uppercase">Conteúdo da Resposta Recebida:</span>
          <button onclick="copyToClipboard('inspect-response-content')" class="text-[11px] text-indigo-600 font-semibold hover:underline flex items-center gap-1">
            <i class="fa-regular fa-copy"></i> Copiar
          </button>
        </div>
        <pre id="inspect-response-content" class="bg-slate-900 text-slate-200 p-4 rounded-xl text-xs font-mono overflow-x-auto whitespace-pre-wrap max-h-96"></pre>
      </div>
    </div>

    <div class="mt-4 pt-3 border-t border-slate-100 flex justify-end">
      <button onclick="closeInspectModal()" class="px-4 py-2 rounded-xl text-xs bg-slate-100 hover:bg-slate-200 text-slate-700 font-semibold">
        Fechar
      </button>
    </div>
  </div>
</div>
{% endblock %}

{% block scripts %}
<script>
  let currentWebhookPage = 1;
  let currentLogsCache = [];

  function switchTab(tab) {
    const btnAlerts = document.getElementById('tab-btn-alerts');
    const btnWebhooks = document.getElementById('tab-btn-webhooks');
    const contentAlerts = document.getElementById('tab-content-alerts');
    const contentWebhooks = document.getElementById('tab-content-webhooks');

    if (tab === 'alerts') {
      btnAlerts.className = 'px-4 py-2.5 text-xs font-bold border-b-2 border-sky-400 text-sky-400 flex items-center gap-2 transition';
      btnWebhooks.className = 'px-4 py-2.5 text-xs font-bold border-b-2 border-transparent text-slate-400 hover:text-slate-200 flex items-center gap-2 transition';
      contentAlerts.classList.remove('hidden');
      contentWebhooks.classList.add('hidden');
    } else {
      btnWebhooks.className = 'px-4 py-2.5 text-xs font-bold border-b-2 border-sky-400 text-sky-400 flex items-center gap-2 transition';
      btnAlerts.className = 'px-4 py-2.5 text-xs font-bold border-b-2 border-transparent text-slate-400 hover:text-slate-200 flex items-center gap-2 transition';
      contentWebhooks.classList.remove('hidden');
      contentAlerts.classList.add('hidden');
      loadHealthMetrics();
      loadWebhookLogs();
    }
  }

  // --- Alertas Clássicos ---
  async function loadAlerts() {
    const token = AppSession.getToken();
    if (!token) {
      window.location.href = '/login';
      return;
    }

    try {
      const resp = await fetch('/api/alerts', {
        headers: { 'Authorization': `Bearer ${token}` }
      });
      const data = await resp.json();
      const container = document.getElementById('alerts-container');

      if (!data || data.length === 0) {
        container.innerHTML = `
          <div class="bg-white rounded-2xl border border-slate-200 p-12 text-center">
            <div class="w-16 h-16 bg-sky-50 rounded-full flex items-center justify-center mx-auto text-sky-500 text-2xl mb-4">
              <i class="fa-regular fa-bell"></i>
            </div>
            <h3 class="text-base font-bold text-slate-800 mb-1">Nenhum radar de alertas ativo</h3>
            <p class="text-xs text-slate-500 max-w-md mx-auto mb-6">
              Crie alertas inteligentes para receber notificações automáticas de certames que atendem seu ramo de atuação.
            </p>
            <button onclick="openNewAlertModal()" class="inline-flex items-center gap-2 text-xs font-semibold bg-sky-600 text-white px-4 py-2 rounded-xl">
              Configurar Alerta Agora
            </button>
          </div>
        `;
        return;
      }

      container.innerHTML = data.map(a => `
        <div class="bg-white rounded-2xl border border-slate-200 p-5 shadow-sm flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
          <div>
            <div class="flex items-center gap-2 mb-1">
              <span class="font-bold text-sm text-slate-900">${a.name}</span>
              <span class="px-2 py-0.5 rounded text-[10px] font-bold ${a.active ? 'bg-emerald-50 text-emerald-700 border border-emerald-200' : 'bg-slate-100 text-slate-500'}">
                ${a.active ? 'ATIVO' : 'PAUSADO'}
              </span>
            </div>
            <div class="text-xs text-slate-500 flex flex-wrap gap-3">
              <span>Palavras-chave: <strong>${a.keywords || 'Todas'}</strong></span>
              <span>Frequência: <strong>${a.frequency}</strong></span>
              <span>Canais: <strong>${a.channels}</strong></span>
              <span>Entregas: <strong>${a.delivery_count}</strong></span>
            </div>
          </div>
          <div class="flex items-center gap-2 flex-shrink-0">
            <button onclick="toggleAlertStatus('${a.id}', ${!a.active})" class="px-3 py-1.5 rounded-xl text-xs font-semibold border ${a.active ? 'border-amber-200 text-amber-700 bg-amber-50' : 'border-emerald-200 text-emerald-700 bg-emerald-50'}">
              ${a.active ? 'Pausar' : 'Reativar'}
            </button>
            <button onclick="deleteAlert('${a.id}')" class="px-3 py-1.5 rounded-xl text-xs font-semibold border border-rose-200 text-rose-700 bg-rose-50">
              Excluir
            </button>
          </div>
        </div>
      `).join('');
    } catch {
      showToast('Erro ao carregar lista de alertas.', 'error');
    }
  }

  function openNewAlertModal() {
    document.getElementById('new-alert-modal').classList.remove('hidden');
  }

  function closeNewAlertModal() {
    document.getElementById('new-alert-modal').classList.add('hidden');
  }

  async function submitCreateAlert() {
    const token = AppSession.getToken();
    const name = document.getElementById('modal-alert-name').value;
    const kw = document.getElementById('modal-alert-keywords').value;
    const uf = document.getElementById('modal-alert-uf').value;
    const freq = document.getElementById('modal-alert-freq').value;
    const chApp = document.getElementById('modal-alert-ch-app').checked;
    const chMail = document.getElementById('modal-alert-ch-mail').checked;
    const channels = [chApp ? 'IN_APP' : null, chMail ? 'EMAIL' : null].filter(Boolean).join(',');

    try {
      const resp = await fetch('/api/alerts', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`
        },
        body: JSON.stringify({
          name: name || 'Meu Alerta',
          keywords: kw || null,
          filters: uf ? { state: uf } : {},
          frequency: freq,
          channels: channels || 'IN_APP'
        })
      });

      if (resp.ok) {
        closeNewAlertModal();
        showToast('Alerta cadastrado com sucesso!', 'success');
        loadAlerts();
      } else {
        showToast('Falha ao criar alerta.', 'error');
      }
    } catch {
      showToast('Erro de conexão.', 'error');
    }
  }

  async function toggleAlertStatus(alertId, newStatus) {
    const token = AppSession.getToken();
    try {
      const resp = await fetch(`/api/alerts/${alertId}/toggle?active=${newStatus}`, {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${token}` }
      });
      if (resp.ok) {
        showToast(`Alerta ${newStatus ? 'reativado' : 'pausado'}.`, 'info');
        loadAlerts();
      }
    } catch {
      showToast('Erro ao atualizar alerta.', 'error');
    }
  }

  async function deleteAlert(alertId) {
    const token = AppSession.getToken();
    if (!confirm('Deseja realmente excluir este alerta?')) return;
    try {
      const resp = await fetch(`/api/alerts/${alertId}`, {
        method: 'DELETE',
        headers: { 'Authorization': `Bearer ${token}` }
      });
      if (resp.ok) {
        showToast('Alerta excluído.', 'info');
        loadAlerts();
      }
    } catch {
      showToast('Erro ao excluir alerta.', 'error');
    }
  }

  // --- Auditoria de Webhooks ---
  async function loadHealthMetrics() {
    try {
      const resp = await fetch('/api/health/metrics');
      if (!resp.ok) return;
      const data = await resp.json();
      document.getElementById('metric-total-24h').textContent = data.disparos_ultimas_24h || 0;
      document.getElementById('metric-success-pct').textContent = `${data.taxa_sucesso_pct}%`;
      document.getElementById('metric-failed-24h').textContent = data.disparos_falha_24h || 0;
      document.getElementById('metric-uptime').textContent = `${data.uptime_human} (${data.database === 'healthy' ? 'DB Ativo' : 'DB Atenção'})`;
    } catch (err) {
      console.warn('Erro ao carregar métricas:', err);
    }
  }

  async function loadWebhookLogs(page = 1) {
    currentWebhookPage = page;
    const status = document.getElementById('filter-status').value;
    const canal = document.getElementById('filter-canal').value;
    const data = document.getElementById('filter-data').value;
    const certame = document.getElementById('filter-certame').value;

    const params = new URLSearchParams({
      page: currentWebhookPage,
      page_size: 15
    });
    if (status) params.append('status', status);
    if (canal) params.append('canal', canal);
    if (data) params.append('data', data);
    if (certame) params.append('certame_id', certame.trim());

    const tbody = document.getElementById('webhook-logs-body');
    tbody.innerHTML = `<tr><td colspan="7" class="p-8 text-center text-slate-400"><i class="fa-solid fa-spinner animate-spin mr-2"></i> Atualizando registros de disparos...</td></tr>`;

    try {
      const resp = await fetch(`/api/alerts/webhooks/logs?${params.toString()}`);
      if (!resp.ok) {
        tbody.innerHTML = `<tr><td colspan="7" class="p-8 text-center text-rose-500">Falha ao obter logs de webhooks.</td></tr>`;
        return;
      }
      const res = await resp.json();
      currentLogsCache = res.items || [];

      if (currentLogsCache.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="p-8 text-center text-slate-400">Nenhum disparo de webhook encontrado com os filtros aplicados.</td></tr>`;
        document.getElementById('webhook-page-info').textContent = 'Total: 0 disparos';
        document.getElementById('webhook-prev-btn').disabled = true;
        document.getElementById('webhook-next-btn').disabled = true;
        return;
      }

      tbody.innerHTML = currentLogsCache.map(log => {
        // Status Semântico (Verde / Amarelo / Vermelho)
        let statusBadge = '';
        if (log.sucesso) {
          statusBadge = `<span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-[11px] font-bold bg-emerald-50 text-emerald-700 border border-emerald-200">
            <i class="fa-solid fa-check text-[10px]"></i> SUCESSO (${log.status_code || 200})
          </span>`;
        } else if (log.status_code && log.status_code >= 400 && log.status_code < 500) {
          statusBadge = `<span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-[11px] font-bold bg-amber-50 text-amber-700 border border-amber-200">
            <i class="fa-solid fa-triangle-exclamation text-[10px]"></i> HTTP ${log.status_code}
          </span>`;
        } else {
          statusBadge = `<span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-[11px] font-bold bg-rose-50 text-rose-700 border border-rose-200">
            <i class="fa-solid fa-circle-xmark text-[10px]"></i> FALHA ${log.status_code ? '(' + log.status_code + ')' : '(Erro Conexão)'}
          </span>`;
        }

        // Canal Badge
        let canalBadge = '';
        if (log.canal === 'N8N') {
          canalBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-purple-50 text-purple-700 border border-purple-200">n8n</span>`;
        } else if (log.canal === 'EVOLUTION_API') {
          canalBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-50 text-emerald-800 border border-emerald-300">Evolution API</span>`;
        } else if (log.canal === 'ZAPI') {
          canalBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-sky-50 text-sky-700 border border-sky-200">Z-API</span>`;
        } else {
          canalBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-slate-100 text-slate-700 border border-slate-200">Genérico</span>`;
        }

        const dateStr = log.criado_em ? new Date(log.criado_em).toLocaleString('pt-BR') : '--';
        const urlShort = log.target_url.length > 38 ? log.target_url.substring(0, 38) + '...' : log.target_url;

        return `
          <tr class="hover:bg-slate-50 transition border-b border-slate-100" id="log-row-${log.id}">
            <td class="p-3.5 font-semibold">${canalBadge}</td>
            <td class="p-3.5 max-w-xs truncate font-mono text-[11px]" title="${log.target_url}">${urlShort}</td>
            <td class="p-3.5">${statusBadge}</td>
            <td class="p-3.5 font-mono text-slate-600 font-semibold">${log.tempo_resposta_ms} ms</td>
            <td class="p-3.5"><span class="px-2 py-0.5 bg-slate-100 rounded text-[10px] font-semibold text-slate-600">${log.tentativa_atual}ª tent.</span></td>
            <td class="p-3.5 text-slate-500 whitespace-nowrap text-[11px]">${dateStr}</td>
            <td class="p-3.5 text-right whitespace-nowrap space-x-1">
              <button onclick="openInspectModal('${log.id}')" class="px-2.5 py-1.5 rounded-lg border border-slate-200 text-slate-700 hover:bg-slate-100 font-semibold text-[11px] transition inline-flex items-center gap-1">
                <i class="fa-regular fa-eye"></i> Inspecionar
              </button>
              <button id="retry-btn-${log.id}" onclick="retryWebhook('${log.id}')" class="px-2.5 py-1.5 rounded-lg bg-sky-50 hover:bg-sky-100 text-sky-700 border border-sky-200 font-semibold text-[11px] transition inline-flex items-center gap-1">
                <i class="fa-solid fa-rotate-right"></i> <span id="retry-text-${log.id}">Reexecutar</span>
              </button>
            </td>
          </tr>
        `;
      }).join('');

      document.getElementById('webhook-page-info').textContent = `Total: ${res.total} disparos (Página ${res.page} de ${Math.ceil(res.total / res.page_size) || 1})`;
      document.getElementById('webhook-prev-btn').disabled = res.page <= 1;
      document.getElementById('webhook-next-btn').disabled = (res.page * res.page_size) >= res.total;
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="7" class="p-8 text-center text-rose-500">Erro de conexão ao carregar webhooks.</td></tr>`;
    }
  }

  function changeWebhookPage(delta) {
    loadWebhookLogs(currentWebhookPage + delta);
  }

  function clearWebhookFilters() {
    document.getElementById('filter-status').value = '';
    document.getElementById('filter-canal').value = '';
    document.getElementById('filter-data').value = '';
    document.getElementById('filter-certame').value = '';
    loadWebhookLogs(1);
  }

  async function retryWebhook(logId) {
    const btn = document.getElementById(`retry-btn-${logId}`);
    const textSpan = document.getElementById(`retry-text-${logId}`);
    if (btn) btn.disabled = true;
    if (textSpan) textSpan.innerHTML = '<i class="fa-solid fa-spinner animate-spin"></i> Disparando...';

    try {
      const resp = await fetch(`/api/alerts/webhooks/${logId}/retry`, {
        method: 'POST'
      });
      const data = await resp.json();

      if (resp.ok) {
        showToast('Webhook reexecutado com sucesso!', 'success');
        if (textSpan) textSpan.innerHTML = '<i class="fa-solid fa-check text-emerald-600"></i> Sucesso!';
        setTimeout(() => {
          loadHealthMetrics();
          loadWebhookLogs(currentWebhookPage);
        }, 1200);
      } else {
        showToast(data.detail || 'Falha ao reexecutar webhook.', 'error');
        if (textSpan) textSpan.textContent = 'Reexecutar';
        if (btn) btn.disabled = false;
      }
    } catch {
      showToast('Erro de comunicação.', 'error');
      if (textSpan) textSpan.textContent = 'Reexecutar';
      if (btn) btn.disabled = false;
    }
  }

  function openInspectModal(logId) {
    const log = currentLogsCache.find(l => l.id === logId);
    if (!log) return;

    document.getElementById('inspect-subtitle').textContent = `Log ID: ${log.id} • Canal: ${log.canal} • Tentativa: ${log.tentativa_atual}ª • ${log.tempo_resposta_ms} ms`;
    
    let formattedPayload = '{}';
    try {
      formattedPayload = typeof log.payload_enviado === 'string' 
        ? JSON.stringify(JSON.parse(log.payload_enviado), null, 2)
        : JSON.stringify(log.payload_enviado || {}, null, 2);
    } catch {
      formattedPayload = String(log.payload_enviado);
    }
    document.getElementById('inspect-payload-content').textContent = formattedPayload;

    let formattedResp = log.resposta_servidor || 'Sem conteúdo de resposta registrado.';
    try {
      formattedResp = JSON.stringify(JSON.parse(formattedResp), null, 2);
    } catch {}
    document.getElementById('inspect-response-content').textContent = formattedResp;

    switchModalInspectTab('payload');
    document.getElementById('inspect-modal').classList.remove('hidden');
  }

  function closeInspectModal() {
    document.getElementById('inspect-modal').classList.add('hidden');
  }

  function switchModalInspectTab(tab) {
    const tabPayloadBtn = document.getElementById('modal-tab-payload-btn');
    const tabRespBtn = document.getElementById('modal-tab-response-btn');
    const viewPayload = document.getElementById('modal-view-payload');
    const viewResp = document.getElementById('modal-view-response');

    if (tab === 'payload') {
      tabPayloadBtn.className = 'px-3 py-2 font-bold border-b-2 border-indigo-600 text-indigo-600';
      tabRespBtn.className = 'px-3 py-2 font-bold border-b-2 border-transparent text-slate-500 hover:text-slate-700';
      viewPayload.classList.remove('hidden');
      viewResp.classList.add('hidden');
    } else {
      tabRespBtn.className = 'px-3 py-2 font-bold border-b-2 border-indigo-600 text-indigo-600';
      tabPayloadBtn.className = 'px-3 py-2 font-bold border-b-2 border-transparent text-slate-500 hover:text-slate-700';
      viewResp.classList.remove('hidden');
      viewPayload.classList.add('hidden');
    }
  }

  function copyToClipboard(elementId) {
    const text = document.getElementById(elementId).textContent;
    navigator.clipboard.writeText(text).then(() => {
      showToast('Copiado para a área de transferência!', 'success');
    }).catch(() => {
      showToast('Falha ao copiar.', 'error');
    });
  }

  async function syncOpportunitiesFromAlerts() {
    const btn = document.getElementById('alerts-sync-btn');
    const icon = document.getElementById('alerts-sync-icon');
    const label = document.getElementById('alerts-sync-label');

    btn.disabled = true;
    btn.classList.add('opacity-75', 'cursor-not-allowed');
    icon.classList.add('animate-spin');
    label.textContent = 'Sincronizando...';

    try {
      const res = await fetch('/api/tenders/sync', { method: 'POST' });
      const data = await res.json();
      if (res.ok) {
        showToast(`${data.message} (${data.compras_gov_count} Compras.gov.br, ${data.pncp_count} PNCP)`, 'success');
        loadAlerts();
        loadHealthMetrics();
      } else {
        showToast('Falha na sincronização: ' + (data.detail || 'Erro no servidor'), 'error');
      }
    } catch {
      showToast('Erro ao sincronizar oportunidades.', 'error');
    } finally {
      btn.disabled = false;
      btn.classList.remove('opacity-75', 'cursor-not-allowed');
      icon.classList.remove('animate-spin');
      label.textContent = 'Atualizar Oportunidades';
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    loadAlerts();
  });
</script>
{% endblock %}
EOF

# ==============================================================================
# MELHORIAS 3 e 4 — ITENS (/itens) E ANEXOS COM DOWNLOAD INTERNO (/arquivos) DO PNCP
# ==============================================================================
mkdir -p server/services
cat << 'EOF' > server/services/pncp_compras.py
"""
Integração com a API pública de integração do PNCP para uma compra específica:

  GET {base}/orgaos/{cnpj}/compras/{ano}/{sequencial}/itens?pagina=N&tamanhoPagina=M
  GET {base}/orgaos/{cnpj}/compras/{ano}/{sequencial}/arquivos
  GET https://pncp.gov.br/pncp-api/v1/orgaos/{cnpj}/compras/{ano}/{sequencial}/arquivos/{seq}  (binário)

- Os itens são normalizados e gravados em ``tender_items`` (origem = PNCP).
- Os arquivos são listados em ``tender_documents`` e baixados pelo servidor
  (proxy com cache local), de modo que o usuário nunca é redirecionado ao portal.
- Downloads só aceitam hosts do PNCP (proteção contra SSRF) e têm limite de tamanho.
"""
import asyncio
import hashlib
import logging
import mimetypes
import os
import re
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

import httpx
from sqlalchemy.orm import Session

from server.config import settings
from server.connectors.pncp import build_canonical_pncp_url
from server.models import Tender, TenderDocument, TenderItem

logger = logging.getLogger(__name__)

HOSTS_PERMITIDOS = {"pncp.gov.br", "www.pncp.gov.br", "treina.pncp.gov.br"}
CABECALHOS = {
    "User-Agent": "licitacoes-brasil/2.0 (+https://licitacoesbrasil.gov.br)",
    "Accept": "application/json, */*",
}
TIMEOUT_JSON = httpx.Timeout(20.0, connect=10.0)
TIMEOUT_DOWNLOAD = httpx.Timeout(120.0, connect=15.0)
TAMANHO_PAGINA_ITENS = 100
MAX_PAGINAS_ITENS = 50
MAX_REDIRECIONAMENTOS = 3
CACHE_NEGATIVO_SEGUNDOS = 30 * 60

# Permite injetar um httpx.MockTransport nos testes
_transport_override: Optional[httpx.AsyncBaseTransport] = None
# tender_id -> (timestamp, motivo) para não consultar o PNCP a cada visualização quando a compra não existe
_falhas_recentes: Dict[str, Tuple[float, str]] = {}


class PNCPErro(Exception):
    """Erro genérico de integração (rede, timeout, HTTP 5xx)."""


class CompraNaoEncontrada(PNCPErro):
    """A compra não existe no PNCP (HTTP 404) ou o certame não tem identificador PNCP."""


class DownloadNaoPermitido(PNCPErro):
    """URL de arquivo fora do domínio do PNCP ou documento sem arquivo vinculado."""


class ArquivoGrandeDemais(PNCPErro):
    pass


@dataclass(frozen=True)
class IdentificadorCompra:
    cnpj: str
    ano: str
    sequencial: str

    @property
    def caminho(self) -> str:
        return f"orgaos/{self.cnpj}/compras/{self.ano}/{self.sequencial}"

    @property
    def url_publica(self) -> str:
        return f"https://pncp.gov.br/app/editais/{self.cnpj}/{self.ano}/{self.sequencial}"


_RE_CANONICA = re.compile(r"pncp\.gov\.br/app/editais/(\d{14})/(\d{4})/(\d+)")


def identificar_compra(tender: Tender) -> Optional[IdentificadorCompra]:
    """Extrai CNPJ/ano/sequencial do numeroControlePNCP ou das URLs do PNCP do certame."""
    candidatos = [
        getattr(tender, "url_pncp", None),
        getattr(tender, "link_pncp", None),
        tender.external_id,
        tender.source_url,
    ]
    for candidato in candidatos:
        if not candidato:
            continue
        canonica = build_canonical_pncp_url(numero_controle_pncp=str(candidato))
        m = _RE_CANONICA.search(canonica or "")
        if m:
            return IdentificadorCompra(cnpj=m.group(1), ano=m.group(2), sequencial=str(int(m.group(3))))
    return None


# ------------------------------------------------------------------------------
# HTTP
# ------------------------------------------------------------------------------
def _cliente(timeout: httpx.Timeout) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        headers=CABECALHOS,
        follow_redirects=False,
        transport=_transport_override,
    )


def host_permitido(url: str) -> bool:
    try:
        partes = urlparse(url)
    except ValueError:
        return False
    return partes.scheme == "https" and (partes.hostname or "").lower() in HOSTS_PERMITIDOS


async def _get_json(caminho: str, params: Optional[Dict[str, Any]] = None) -> Any:
    url = f"{settings.PNCP_INTEGRACAO_BASE_URL}/{caminho}"
    try:
        async with _cliente(TIMEOUT_JSON) as cliente:
            resp = await cliente.get(url, params=params)
    except httpx.HTTPError as exc:
        raise PNCPErro(f"PNCP indisponível: {exc.__class__.__name__}") from exc
    if resp.status_code == 404:
        raise CompraNaoEncontrada("Compra não localizada no PNCP.")
    if resp.status_code == 204 or not resp.content:
        return []
    if resp.status_code >= 400:
        raise PNCPErro(f"PNCP respondeu HTTP {resp.status_code}.")
    try:
        return resp.json()
    except ValueError as exc:
        raise PNCPErro("Resposta inválida do PNCP.") from exc


def _como_lista(dados: Any) -> List[Dict[str, Any]]:
    if isinstance(dados, list):
        return [d for d in dados if isinstance(d, dict)]
    if isinstance(dados, dict):
        for chave in ("data", "itens", "items", "content"):
            if isinstance(dados.get(chave), list):
                return [d for d in dados[chave] if isinstance(d, dict)]
    return []


def _registrar_falha(tender_id: str, motivo: str) -> None:
    _falhas_recentes[tender_id] = (time.time(), motivo)


def falha_recente(tender_id: str) -> Optional[str]:
    registro = _falhas_recentes.get(tender_id)
    if not registro:
        return None
    instante, motivo = registro
    if time.time() - instante > CACHE_NEGATIVO_SEGUNDOS:
        _falhas_recentes.pop(tender_id, None)
        return None
    return motivo


# ------------------------------------------------------------------------------
# Itens
# ------------------------------------------------------------------------------
def _float(valor: Any) -> Optional[float]:
    try:
        return float(valor) if valor is not None and valor != "" else None
    except (TypeError, ValueError):
        return None


def _texto(valor: Any, limite: Optional[int] = None) -> Optional[str]:
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto:
        return None
    return texto[:limite] if limite else texto


def parse_item_pncp(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normaliza um item do endpoint /itens do PNCP para as colunas de TenderItem."""
    try:
        numero = int(raw.get("numeroItem"))
    except (TypeError, ValueError):
        return None
    quantidade = _float(raw.get("quantidade"))
    unitario = _float(raw.get("valorUnitarioEstimado"))
    total = _float(raw.get("valorTotal"))
    if total is None and unitario is not None and quantidade is not None:
        total = round(unitario * quantidade, 2)

    mos = _texto(raw.get("materialOuServico"), 1)
    codigo = (
        _texto(raw.get("catalogoCodigoItem"), 100)
        or _texto(raw.get("ncmNbsCodigo"), 100)
        or _texto(raw.get("itemCategoriaNome"), 100)
    )
    return {
        "item_number": numero,
        "description": _texto(raw.get("descricao")) or f"Item {numero}",
        "unit": _texto(raw.get("unidadeMedida"), 50),
        "quantity": quantidade,
        "estimated_unit_value": unitario,
        "estimated_total_value": total,
        "classification_code": codigo,
        "material_ou_servico": mos.upper() if mos else None,
        "criterio_julgamento": _texto(raw.get("criterioJulgamentoNome"), 100),
        "situacao": _texto(raw.get("situacaoCompraItemNome"), 100),
        "beneficio": _texto(raw.get("tipoBeneficioNome"), 150),
        "orcamento_sigiloso": bool(raw.get("orcamentoSigiloso")) if raw.get("orcamentoSigiloso") is not None else None,
        "informacao_complementar": _texto(raw.get("informacaoComplementar")),
        "raw_data": raw,
    }


async def buscar_itens_pncp(ident: IdentificadorCompra) -> List[Dict[str, Any]]:
    """Percorre todas as páginas de /itens e devolve os itens normalizados (ordenados por número)."""
    itens: Dict[int, Dict[str, Any]] = {}
    for pagina in range(1, MAX_PAGINAS_ITENS + 1):
        dados = await _get_json(
            f"{ident.caminho}/itens", params={"pagina": pagina, "tamanhoPagina": TAMANHO_PAGINA_ITENS}
        )
        lote = _como_lista(dados)
        for raw in lote:
            item = parse_item_pncp(raw)
            if item:
                itens[item["item_number"]] = item
        if len(lote) < TAMANHO_PAGINA_ITENS:
            break
    return [itens[k] for k in sorted(itens)]


def serializar_item(item: TenderItem) -> Dict[str, Any]:
    return {
        "id": item.id,
        "item_number": item.item_number,
        "description": item.description,
        "unit": item.unit or "Não informado",
        "quantity": item.quantity,
        "estimated_unit_value": item.estimated_unit_value,
        "estimated_total_value": item.estimated_total_value,
        "classification_code": item.classification_code or "Não informado",
        "material_ou_servico": item.material_ou_servico,
        "criterio_julgamento": item.criterio_julgamento,
        "situacao": item.situacao,
        "beneficio": item.beneficio,
        "orcamento_sigiloso": item.orcamento_sigiloso,
        "informacao_complementar": item.informacao_complementar,
        "origem": item.origem or "LOCAL",
    }


def _itens_do_banco(db: Session, tender_id: str) -> List[TenderItem]:
    return db.query(TenderItem).filter(TenderItem.tender_id == tender_id).order_by(TenderItem.item_number.asc()).all()


async def sincronizar_itens(db: Session, tender: Tender, forcar: bool = False) -> Dict[str, Any]:
    """
    Atualiza os itens do certame a partir do PNCP.
    Sem ``forcar``, reutiliza itens já sincronizados (origem PNCP) e o cache de falhas recentes.
    Em falha, devolve os itens locais com ``status`` = "indisponivel" ou "nao_encontrado".
    """
    existentes = _itens_do_banco(db, tender.id)
    ja_sincronizado = any(i.origem == "PNCP" for i in existentes)
    ident = identificar_compra(tender)

    def resposta(status: str, mensagem: Optional[str] = None, origem: str = "LOCAL") -> Dict[str, Any]:
        itens = _itens_do_banco(db, tender.id)
        return {
            "status": status,
            "origem": origem,
            "mensagem": mensagem,
            "total": len(itens),
            "valor_total_estimado": round(sum(i.estimated_total_value or 0 for i in itens), 2),
            "pncp_url": ident.url_publica if ident else None,
            "itens": [serializar_item(i) for i in itens],
        }

    if not ident:
        return resposta("sem_identificador", "Certame sem identificador do PNCP; exibindo itens da base local.")
    if ja_sincronizado and not forcar:
        return resposta("cache", None, "PNCP")
    if not forcar and falha_recente(f"itens:{tender.id}"):
        return resposta("indisponivel", falha_recente(f"itens:{tender.id}"))

    try:
        itens_pncp = await buscar_itens_pncp(ident)
    except CompraNaoEncontrada:
        msg = "Compra não localizada no PNCP; exibindo itens da base local."
        _registrar_falha(f"itens:{tender.id}", msg)
        return resposta("nao_encontrado", msg)
    except PNCPErro as exc:
        msg = f"Não foi possível consultar o PNCP agora ({exc}). Exibindo itens da base local."
        _registrar_falha(f"itens:{tender.id}", msg)
        return resposta("indisponivel", msg)

    if not itens_pncp:
        return resposta("vazio", "O PNCP ainda não publicou itens para esta compra.", "PNCP")

    agora = datetime.utcnow()
    por_numero = {i.item_number: i for i in existentes}
    numeros_pncp = set()
    for dados in itens_pncp:
        numeros_pncp.add(dados["item_number"])
        item = por_numero.get(dados["item_number"])
        if item is None:
            item = TenderItem(tender_id=tender.id, item_number=dados["item_number"], description=dados["description"])
            db.add(item)
        for campo, valor in dados.items():
            setattr(item, campo, valor)
        item.origem = "PNCP"
        item.synced_at = agora
    # O PNCP é a fonte oficial: remove itens locais que não existem mais na compra
    for numero, item in por_numero.items():
        if numero not in numeros_pncp:
            db.delete(item)
    tender.last_synced_at = agora
    db.commit()
    _falhas_recentes.pop(f"itens:{tender.id}", None)
    return resposta("sincronizado", None, "PNCP")


# ------------------------------------------------------------------------------
# Arquivos / anexos
# ------------------------------------------------------------------------------
def parse_arquivo_pncp(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = _texto(raw.get("url")) or _texto(raw.get("uri"))
    if not url or not host_permitido(url):
        return None
    if raw.get("statusAtivo") is False:
        return None
    try:
        sequencial = int(raw.get("sequencialDocumento"))
    except (TypeError, ValueError):
        sequencial = None
    publicado = None
    if raw.get("dataPublicacaoPncp"):
        try:
            publicado = datetime.fromisoformat(str(raw["dataPublicacaoPncp"]).replace("Z", "")[:19])
        except ValueError:
            publicado = None
    titulo = _texto(raw.get("titulo"), 500) or f"Documento {sequencial or ''}".strip()
    return {
        "name": re.sub(r"\s+", " ", titulo.replace("_", " ")).strip() or titulo,
        "document_type": (_texto(raw.get("tipoDocumentoNome"), 100) or _texto(raw.get("tipoDocumentoDescricao"), 100) or "Anexo"),
        "source_url": url,
        "sequencial_documento": sequencial,
        "published_at": publicado,
    }


async def buscar_arquivos_pncp(ident: IdentificadorCompra) -> List[Dict[str, Any]]:
    dados = await _get_json(f"{ident.caminho}/arquivos")
    arquivos = [a for a in (parse_arquivo_pncp(r) for r in _como_lista(dados)) if a]
    return sorted(arquivos, key=lambda a: (a["sequencial_documento"] or 0))


def documento_baixavel(doc: TenderDocument) -> bool:
    return bool(doc.source_url) and host_permitido(doc.source_url) and (
        "/arquivos/" in doc.source_url or doc.origem == "PNCP"
    )


def serializar_documento(doc: TenderDocument) -> Dict[str, Any]:
    baixavel = documento_baixavel(doc)
    return {
        "id": doc.id,
        "name": doc.name,
        "document_type": doc.document_type,
        "mime_type": doc.mime_type,
        "file_size": doc.file_size,
        "checksum": doc.checksum,
        "published_at": doc.published_at.isoformat() if doc.published_at else None,
        "downloaded_at": doc.downloaded_at.isoformat() if doc.downloaded_at else None,
        "sequencial_documento": doc.sequencial_documento,
        "origem": doc.origem or "LOCAL",
        "baixavel": baixavel,
        "download_url": f"/api/tenders/{doc.tender_id}/arquivos/{doc.id}/download" if baixavel else None,
        "visualizar_url": f"/api/tenders/{doc.tender_id}/arquivos/{doc.id}/download?inline=1" if baixavel else None,
    }


def _docs_do_banco(db: Session, tender_id: str) -> List[TenderDocument]:
    return (
        db.query(TenderDocument)
        .filter(TenderDocument.tender_id == tender_id)
        .order_by(TenderDocument.sequencial_documento.is_(None), TenderDocument.sequencial_documento.asc(), TenderDocument.created_at.asc())
        .all()
    )


async def sincronizar_arquivos(db: Session, tender: Tender, forcar: bool = False) -> Dict[str, Any]:
    existentes = _docs_do_banco(db, tender.id)
    ja_sincronizado = any(d.origem == "PNCP" for d in existentes)
    ident = identificar_compra(tender)

    def resposta(status: str, mensagem: Optional[str] = None) -> Dict[str, Any]:
        docs = _docs_do_banco(db, tender.id)
        return {
            "status": status,
            "mensagem": mensagem,
            "total": len(docs),
            "pncp_url": ident.url_publica if ident else None,
            "zip_url": f"/api/tenders/{tender.id}/arquivos/zip" if any(documento_baixavel(d) for d in docs) else None,
            "arquivos": [serializar_documento(d) for d in docs],
        }

    if not ident:
        return resposta("sem_identificador", "Certame sem identificador do PNCP.")
    if ja_sincronizado and not forcar:
        return resposta("cache")
    if not forcar and falha_recente(f"arquivos:{tender.id}"):
        return resposta("indisponivel", falha_recente(f"arquivos:{tender.id}"))

    try:
        arquivos = await buscar_arquivos_pncp(ident)
    except CompraNaoEncontrada:
        msg = "Compra não localizada no PNCP."
        _registrar_falha(f"arquivos:{tender.id}", msg)
        return resposta("nao_encontrado", msg)
    except PNCPErro as exc:
        msg = f"Não foi possível consultar o PNCP agora ({exc})."
        _registrar_falha(f"arquivos:{tender.id}", msg)
        return resposta("indisponivel", msg)

    por_url = {d.source_url: d for d in existentes}
    por_seq = {d.sequencial_documento: d for d in existentes if d.sequencial_documento is not None}
    for dados in arquivos:
        doc = por_seq.get(dados["sequencial_documento"]) or por_url.get(dados["source_url"])
        if doc is None:
            doc = TenderDocument(tender_id=tender.id, name=dados["name"], source_url=dados["source_url"])
            db.add(doc)
        url_mudou = doc.source_url != dados["source_url"]
        for campo, valor in dados.items():
            setattr(doc, campo, valor)
        doc.origem = "PNCP"
        if url_mudou:
            doc.storage_key = None
            doc.checksum = None
            doc.downloaded_at = None
    db.commit()
    _falhas_recentes.pop(f"arquivos:{tender.id}", None)
    if not arquivos:
        return resposta("vazio", "O órgão ainda não publicou arquivos para esta compra no PNCP.")
    return resposta("sincronizado")


# ------------------------------------------------------------------------------
# Download (proxy com cache local)
# ------------------------------------------------------------------------------
_EXTENSOES_POR_MIME = {
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "application/x-rar-compressed": ".rar",
    "application/vnd.rar": ".rar",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}


def _diretorio_cache(tender_id: str) -> Path:
    base = Path(settings.STORAGE_LOCAL_PATH)
    if not base.is_absolute():
        base = Path(__file__).resolve().parent.parent.parent / base
    destino = base / "pncp" / re.sub(r"[^A-Za-z0-9_-]", "_", tender_id)
    destino.mkdir(parents=True, exist_ok=True)
    return destino


def _nome_seguro(nome: str, max_len: int = 120) -> str:
    base = unicodedata.normalize("NFKD", nome or "").encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip(" ._") or "documento"
    raiz, ext = os.path.splitext(base)
    return (raiz[: max_len - len(ext)] + ext) if len(base) > max_len else base


def _nome_do_cabecalho(content_disposition: str) -> Optional[str]:
    if not content_disposition:
        return None
    m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", content_disposition, re.IGNORECASE)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', content_disposition, re.IGNORECASE)
    if m:
        bruto = m.group(1).strip()
        try:
            return bruto.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return bruto
    return None


def _mime_por_conteudo(cabeca: bytes, mime_http: Optional[str]) -> str:
    if cabeca.startswith(b"%PDF"):
        return "application/pdf"
    if cabeca.startswith(b"PK\x03\x04"):
        return mime_http if mime_http and ("openxml" in mime_http or "opendocument" in mime_http) else "application/zip"
    if cabeca.startswith(b"Rar!"):
        return "application/vnd.rar"
    if cabeca.startswith(b"\xd0\xcf\x11\xe0"):
        return mime_http if mime_http and mime_http != "application/octet-stream" else "application/msword"
    return mime_http or "application/octet-stream"


def nome_para_download(doc: TenderDocument) -> str:
    nome = doc.name or "documento"
    ext_atual = os.path.splitext(nome)[1].lower()
    ext_mime = _EXTENSOES_POR_MIME.get((doc.mime_type or "").split(";")[0].strip()) or mimetypes.guess_extension(doc.mime_type or "") or ""
    if ext_mime and ext_atual != ext_mime:
        nome = f"{nome}{ext_mime}"
    return _nome_seguro(nome)


def caminho_em_cache(doc: TenderDocument) -> Optional[Path]:
    if not doc.storage_key:
        return None
    caminho = Path(doc.storage_key)
    if not caminho.is_absolute():
        caminho = Path(__file__).resolve().parent.parent.parent / caminho
    return caminho if caminho.is_file() else None


async def baixar_documento(db: Session, doc: TenderDocument, forcar: bool = False) -> Path:
    """
    Garante o arquivo do documento em cache local e devolve o caminho.
    Segue até MAX_REDIRECIONAMENTOS, sempre validando que o destino é do PNCP.
    """
    if not documento_baixavel(doc):
        raise DownloadNaoPermitido("Documento sem arquivo disponível para download interno.")

    em_cache = None if forcar else caminho_em_cache(doc)
    if em_cache:
        return em_cache

    limite = settings.PNCP_DOWNLOAD_MAX_MB * 1024 * 1024
    destino_dir = _diretorio_cache(doc.tender_id)
    url = doc.source_url
    fd, temporario = tempfile.mkstemp(dir=destino_dir, prefix=".download_", suffix=".part")
    os.close(fd)
    sha = hashlib.sha256()
    tamanho = 0
    cabeca = b""
    mime_http = None
    nome_http = None
    try:
        async with _cliente(TIMEOUT_DOWNLOAD) as cliente:
            for _ in range(MAX_REDIRECIONAMENTOS + 1):
                if not host_permitido(url):
                    raise DownloadNaoPermitido("Redirecionamento para domínio não autorizado bloqueado.")
                async with cliente.stream("GET", url, headers={"Accept": "*/*"}) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                        url = urljoin(url, resp.headers["location"])
                        continue
                    if resp.status_code == 404:
                        raise CompraNaoEncontrada("Arquivo não encontrado no PNCP.")
                    if resp.status_code >= 400:
                        raise PNCPErro(f"PNCP respondeu HTTP {resp.status_code} ao baixar o arquivo.")
                    declarado = int(resp.headers.get("content-length") or 0)
                    if declarado and declarado > limite:
                        raise ArquivoGrandeDemais(f"Arquivo excede {settings.PNCP_DOWNLOAD_MAX_MB} MB.")
                    mime_http = (resp.headers.get("content-type") or "").split(";")[0].strip() or None
                    nome_http = _nome_do_cabecalho(resp.headers.get("content-disposition", ""))
                    with open(temporario, "wb") as fh:
                        async for bloco in resp.aiter_bytes(64 * 1024):
                            tamanho += len(bloco)
                            if tamanho > limite:
                                raise ArquivoGrandeDemais(f"Arquivo excede {settings.PNCP_DOWNLOAD_MAX_MB} MB.")
                            if len(cabeca) < 16:
                                cabeca += bloco[: 16 - len(cabeca)]
                            sha.update(bloco)
                            fh.write(bloco)
                    break
            else:
                raise PNCPErro("Excesso de redirecionamentos ao baixar o arquivo.")
    except httpx.HTTPError as exc:
        Path(temporario).unlink(missing_ok=True)
        raise PNCPErro(f"Falha de rede ao baixar do PNCP: {exc.__class__.__name__}") from exc
    except Exception:
        Path(temporario).unlink(missing_ok=True)
        raise

    mime = _mime_por_conteudo(cabeca, mime_http)
    ext = _EXTENSOES_POR_MIME.get(mime) or mimetypes.guess_extension(mime) or ".bin"
    final = destino_dir / f"{doc.id}{ext}"
    os.replace(temporario, final)

    raiz = Path(__file__).resolve().parent.parent.parent
    try:
        doc.storage_key = final.resolve().relative_to(raiz).as_posix()
    except ValueError:
        doc.storage_key = str(final.resolve())
    doc.mime_type = mime
    doc.file_size = tamanho
    doc.checksum = sha.hexdigest()
    doc.downloaded_at = datetime.utcnow()
    if nome_http and (not doc.name or doc.name.startswith("Documento ")):
        doc.name = nome_http[:500]
    db.commit()
    return final


async def baixar_varios(db: Session, docs: List[TenderDocument], limite_total_mb: int = 300) -> List[Tuple[TenderDocument, Optional[Path], Optional[str]]]:
    """Baixa (ou reaproveita do cache) vários documentos, sequencialmente, respeitando um teto total."""
    resultados: List[Tuple[TenderDocument, Optional[Path], Optional[str]]] = []
    acumulado = 0
    for doc in docs:
        if not documento_baixavel(doc):
            continue
        try:
            caminho = await baixar_documento(db, doc)
            acumulado += caminho.stat().st_size
            if acumulado > limite_total_mb * 1024 * 1024:
                resultados.append((doc, None, f"Limite total de {limite_total_mb} MB atingido"))
                break
            resultados.append((doc, caminho, None))
        except PNCPErro as exc:
            resultados.append((doc, None, str(exc)))
        await asyncio.sleep(0)
    return resultados
EOF

mkdir -p server/templates
cat << 'EOF' > server/templates/tender_detail.html
{% extends "base.html" %}

{% block title %}{{ detail.tender.title }} - Licitações Brasil{% endblock %}

{% block content %}
<!-- Top Breadcrumb & Actions Bar -->
<div class="bg-slate-900 text-white py-6 border-b border-slate-800">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
    <div class="flex flex-wrap items-center justify-between gap-4">
      <div>
        <nav class="flex items-center space-x-2 text-xs text-slate-400 mb-2">
          <a href="/" class="hover:text-white">Início</a>
          <span>/</span>
          <a href="/search" class="hover:text-white">Pesquisa</a>
          <span>/</span>
          <span class="text-sky-400 font-mono">{{ detail.tender.external_id }}</span>
        </nav>
        <div class="flex items-center gap-2 flex-wrap">
          <span class="px-2.5 py-0.5 rounded text-xs font-bold bg-sky-500 text-white">
            {{ detail.tender.state }} - {{ detail.tender.city }}
          </span>
          <span class="px-2.5 py-0.5 rounded text-xs font-semibold bg-slate-800 text-slate-200 border border-slate-700">
            {{ detail.tender.modality }}
          </span>
          <span class="px-2.5 py-0.5 rounded text-xs font-semibold bg-emerald-950 text-emerald-300 border border-emerald-800">
            {{ detail.tender.status }}
          </span>
          {% if detail.tender.category %}
          <span class="px-2.5 py-0.5 rounded text-xs font-semibold bg-purple-950 text-purple-300 border border-purple-800">
            {{ detail.tender.category }}
          </span>
          {% endif %}
        </div>
      </div>

      <!-- Action Buttons -->
      <div class="flex items-center gap-2 flex-wrap">
        <button onclick="toggleFavorite('{{ detail.tender.id }}', this)" class="bg-slate-800 hover:bg-slate-700 text-white border border-slate-700 px-4 py-2 rounded-xl text-xs font-semibold transition flex items-center gap-1.5 {% if detail.tender.is_favorite %}text-amber-400{% endif %}">
          <i class="fa-solid fa-star {% if detail.tender.is_favorite %}text-amber-400{% else %}text-slate-400{% endif %}"></i>
          <span class="fav-label">{% if detail.tender.is_favorite %}Favoritado{% else %}Favoritar{% endif %}</span>
        </button>
        <button onclick="copyTenderLink()" class="bg-slate-800 hover:bg-slate-700 text-white border border-slate-700 px-4 py-2 rounded-xl text-xs font-semibold transition flex items-center gap-1.5">
          <i class="fa-regular fa-copy"></i> Copiar Link
        </button>
        <button onclick="exportToDisputeCockpit('{{ detail.tender.id }}', '{{ (detail.tender.title or 'Certame')|replace('\'', '\\\'')|replace('\n', ' ') }}', {{ detail.tender.estimated_total_value or 0 }}, '{{ detail.tender.fonte_portal or 'Compras.gov.br' }}')" class="bg-indigo-600 hover:bg-indigo-500 text-white px-4 py-2 rounded-xl text-xs font-semibold transition flex items-center gap-1.5 shadow-sm border border-indigo-500/30">
          <i class="fa-solid fa-bolt text-amber-300 text-xs"></i> Exportar para o Cockpit de Disputa
        </button>
        <a href="{{ detail.tender.url_disputa or detail.tender.url_pncp or detail.tender.link_comprasnet or detail.tender.official_notice_url or detail.tender.source_url }}" target="_blank" rel="noopener noreferrer" class="bg-emerald-600 hover:bg-emerald-500 text-white px-4 py-2 rounded-xl text-xs font-semibold transition flex items-center gap-1.5 shadow-sm">
          <i class="fa-solid fa-gavel text-xs"></i> Acessar Edital / Disputa
        </a>
        <a href="{{ detail.tender.url_pncp or detail.tender.link_pncp or detail.tender.source_url }}" target="_blank" rel="noopener noreferrer" class="bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 px-3.5 py-2 rounded-xl text-xs font-semibold transition flex items-center gap-1.5 shadow-sm">
          <i class="fa-solid fa-landmark text-sky-400 text-xs"></i> PNCP Oficial
        </a>
        <button
          type="button"
          id="btn-baixar-kit-zip"
          data-kit-url="/api/tenders/{{ detail.tender.id }}/download-kit-zip"
          onclick="baixarKitHabilitacao(this)"
          class="bg-amber-500 hover:bg-amber-400 text-slate-950 px-4 py-2 rounded-xl text-xs font-bold transition flex items-center gap-1.5 shadow-sm border border-amber-400/40 cursor-pointer"
          title="Documentos válidos do Cofre + Relatório de Conformidade (PDF)"
        >
          <i class="fa-solid fa-file-zipper text-xs"></i> Baixar Kit de Habilitação (ZIP)
        </button>
      </div>
    </div>

    <!-- Title and Entity -->
    <h1 class="text-xl sm:text-2xl font-bold mt-4 text-white leading-snug">
      {{ detail.tender.title }}
    </h1>
    <p class="text-xs text-slate-400 mt-1 flex items-center gap-1">
      <i class="fa-regular fa-building"></i> {{ detail.tender.contracting_entity }}
      {% if detail.tender.purchasing_unit != "Não informado" %}
        - {{ detail.tender.purchasing_unit }}
      {% endif %}
    </p>
  </div>
</div>

<!-- Tabs Navigation -->
<div class="bg-white border-b border-slate-200 sticky top-16 z-40">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
    <nav class="flex space-x-6 overflow-x-auto text-sm font-semibold text-slate-600" id="tender-tabs">
      <button onclick="switchTab('overview')" id="tab-btn-overview" class="py-4 border-b-2 border-sky-600 text-sky-600 flex items-center gap-2 whitespace-nowrap">
        <i class="fa-solid fa-file-lines"></i> Visão Geral
      </button>
      <button onclick="switchTab('items')" id="tab-btn-items" class="py-4 border-b-2 border-transparent hover:text-slate-900 flex items-center gap-2 whitespace-nowrap">
        <i class="fa-solid fa-list-check"></i> Itens (<span id="tab-count-itens">{{ detail.items|length }}</span>)
      </button>
      <button onclick="switchTab('documents')" id="tab-btn-documents" class="py-4 border-b-2 border-transparent hover:text-slate-900 flex items-center gap-2 whitespace-nowrap">
        <i class="fa-solid fa-paperclip"></i> Documentos (<span id="tab-count-docs">{{ detail.documents|length }}</span>)
      </button>
      <button onclick="switchTab('ai')" id="tab-btn-ai" class="py-4 border-b-2 border-transparent hover:text-slate-900 flex items-center gap-2 whitespace-nowrap text-emerald-600">
        <i class="fa-solid fa-wand-magic-sparkles"></i> Análise por IA
      </button>
      <button onclick="switchTab('changes')" id="tab-btn-changes" class="py-4 border-b-2 border-transparent hover:text-slate-900 flex items-center gap-2 whitespace-nowrap">
        <i class="fa-solid fa-clock-rotate-left"></i> Histórico
      </button>
      <button onclick="switchTab('notes')" id="tab-btn-notes" class="py-4 border-b-2 border-transparent hover:text-slate-900 flex items-center gap-2 whitespace-nowrap">
        <i class="fa-regular fa-note-sticky"></i> Anotações
      </button>
    </nav>
  </div>
</div>

<!-- Main Tabs Content -->
<div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">

  <!-- TAB 1: VISÃO GERAL -->
  <div id="tab-content-overview" class="space-y-6">
    <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
      <!-- Main Details Column -->
      <div class="md:col-span-2 space-y-6">
        <!-- Componente Visual: Análise IA com Gemini -->
        <div class="bg-gradient-to-br from-slate-900 via-slate-800 to-indigo-950 p-6 rounded-2xl border border-indigo-500/30 text-white shadow-xl relative overflow-hidden">
          <div class="flex flex-wrap items-center justify-between gap-3 mb-4">
            <div class="flex items-center gap-2.5">
              <span class="w-9 h-9 rounded-xl bg-emerald-500/20 text-emerald-400 flex items-center justify-center text-base font-bold border border-emerald-500/30 shadow-inner">
                <i class="fa-solid fa-wand-magic-sparkles"></i>
              </span>
              <div>
                <h2 class="text-base font-bold text-white flex items-center gap-2">
                  Análise IA com Gemini
                  <span class="px-2 py-0.5 rounded text-[10px] font-extrabold bg-indigo-500/30 text-indigo-300 border border-indigo-400/30 uppercase font-mono tracking-wider">Gemini Flash</span>
                </h2>
                <p class="text-[11px] text-slate-400">Auditoria semântica documental e triagem estratégica de riscos</p>
              </div>
            </div>

            <!-- Badges de Metadados: Portal e Confiança -->
            <div class="flex items-center gap-2 flex-wrap">
              <span class="px-2.5 py-1 rounded-lg text-xs font-bold bg-slate-800 text-sky-300 border border-slate-700 flex items-center gap-1.5 shadow-sm">
                <i class="fa-solid fa-globe text-xs"></i> <span id="gemini-portal-badge">{{ detail.tender.fonte_portal or 'PNCP' }}</span>
              </span>
              <span class="px-2.5 py-1 rounded-lg text-xs font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 flex items-center gap-1.5 shadow-sm">
                <i class="fa-solid fa-shield-check text-xs"></i> Confiança da IA: 98% (Alta)
              </span>
            </div>
          </div>

          <!-- Conteúdo Enriquecido da IA -->
          <div class="space-y-4 text-xs">
            <!-- Resumo Executivo -->
            <div class="bg-slate-900/80 p-4 rounded-xl border border-slate-700/60 shadow-inner">
              <span class="text-slate-400 font-bold uppercase text-[10px] tracking-wider block mb-1 flex items-center gap-1.5">
                <i class="fa-solid fa-file-invoice text-indigo-400"></i> Resumo Executivo da Oportunidade:
              </span>
              <p class="text-slate-200 leading-relaxed text-xs" id="gemini-overview-resumo">
                {{ detail.tender.ai_resumo or (detail.latest_ai_analysis.result_json.resumo_executivo.o_que_sera_contratado if detail.latest_ai_analysis and detail.latest_ai_analysis.result_json and detail.latest_ai_analysis.result_json.resumo_executivo else 'Certame selecionado para triagem automatizada multifonte. Clique em "Analisar com Gemini" para decodificar exigências de habilitação, itens e estimativa orçamentária.') }}
              </p>
            </div>

            <!-- Alertas de Risco & Prazos Críticos -->
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div class="bg-rose-950/40 border border-rose-800/40 p-3.5 rounded-xl">
                <span class="text-rose-300 font-bold uppercase text-[10px] tracking-wider block mb-1 flex items-center gap-1.5">
                  <i class="fa-solid fa-triangle-exclamation text-rose-400"></i> Alertas de Risco Jurídico / Execução:
                </span>
                <p class="text-rose-100/90 text-[11px] leading-relaxed" id="gemini-overview-riscos">
                  {{ detail.tender.ai_pontos_atencao or 'Exigências de qualificação técnica e cláusulas de penalidade requerem atenção prévia ao envio da proposta.' }}
                </p>
              </div>

              <div class="bg-amber-950/40 border border-amber-800/40 p-3.5 rounded-xl">
                <span class="text-amber-300 font-bold uppercase text-[10px] tracking-wider block mb-1 flex items-center gap-1.5">
                  <i class="fa-solid fa-calendar-check text-amber-400"></i> Prazos Críticos & Sessão:
                </span>
                <p class="text-amber-100/90 text-[11px] leading-relaxed">
                  Abertura das propostas: <strong>{{ detail.tender.opening_date.strftime('%d/%m/%Y às %H:%M') if detail.tender.opening_date else 'Consulte edital oficial' }}</strong>. Impugnações até 3 dias úteis antes da sessão.
                </p>
              </div>
            </div>

            <!-- Ações Diretas Integradas -->
            <div class="flex flex-wrap items-center justify-between gap-3 pt-2">
              <button onclick="requestAiAnalysis('{{ detail.tender.id }}')" class="bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold px-4 py-2 rounded-xl text-xs transition flex items-center gap-2 shadow-md">
                <i class="fa-solid fa-wand-magic-sparkles"></i> Analisar com Gemini
              </button>

              <button onclick="exportToDisputeCockpit('{{ detail.tender.id }}', '{{ (detail.tender.title or 'Certame')|replace('\'', '\\\'')|replace('\n', ' ') }}', {{ detail.tender.estimated_total_value or 0 }}, '{{ detail.tender.fonte_portal or 'Compras.gov.br' }}')" class="bg-indigo-600 hover:bg-indigo-500 text-white font-bold px-4 py-2 rounded-xl text-xs transition flex items-center gap-2 shadow-md border border-indigo-400/40">
                <i class="fa-solid fa-bolt text-amber-300"></i> Exportar para o Cockpit de Disputa
              </button>
            </div>
          </div>
        </div>

        <!-- Objeto Completo -->
        <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm">
          <h2 class="text-base font-bold text-slate-900 mb-3 flex items-center gap-2">
            <i class="fa-solid fa-align-left text-sky-600"></i> Objeto da Contratação
          </h2>
          <div class="text-sm text-slate-700 leading-relaxed whitespace-pre-line bg-slate-50 p-4 rounded-xl border border-slate-100">
            {{ detail.tender.object_description }}
          </div>
        </div>

        <!-- Critérios e Condições -->
        <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm space-y-4">
          <h2 class="text-base font-bold text-slate-900 flex items-center gap-2">
            <i class="fa-solid fa-gavel text-sky-600"></i> Condições e Critérios da Disputa
          </h2>
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-4 text-xs text-slate-700">
            <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
              <span class="block text-slate-400 font-semibold uppercase text-[10px] mb-1">Critério de Julgamento:</span>
              <span class="font-medium text-slate-800">Menor Preço por Item / Global (Lei 14.133/2021)</span>
            </div>
            <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
              <span class="block text-slate-400 font-semibold uppercase text-[10px] mb-1">Modo de Disputa:</span>
              <span class="font-medium text-slate-800">Aberto e Fechado (Eletrônico)</span>
            </div>
            <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
              <span class="block text-slate-400 font-semibold uppercase text-[10px] mb-1">Local de Entrega:</span>
              <span class="font-medium text-slate-800">{{ detail.tender.city }} / {{ detail.tender.state }}</span>
            </div>
            <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
              <span class="block text-slate-400 font-semibold uppercase text-[10px] mb-1">Condições de Pagamento:</span>
              <span class="font-medium text-slate-800">Até 30 dias após emissão da NF e ateste definitivo</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Key Dates & Financial Sidebar -->
      <div class="md:col-span-1 space-y-6">
        <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm">
          <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-4">Informações Financeiras</h3>
          <div class="mb-6">
            <span class="text-xs text-slate-500 block">Valor Estimado Total:</span>
            <div class="text-2xl font-black text-slate-900 mt-1">
              {% if detail.tender.estimated_total_value %}
                R$ {{ "{:,.2f}".format(detail.tender.estimated_total_value).replace(",", "X").replace(".", ",").replace("X", ".") }}
              {% else %}
                Não informado
              {% endif %}
            </div>
          </div>

          <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-3">Cronograma Oficial</h3>
          <ul class="space-y-3 text-xs">
            <li class="flex items-start justify-between pb-2 border-b border-slate-100">
              <span class="text-slate-500">Publicação no PNCP:</span>
              <span class="font-semibold text-slate-800">
                {% if detail.tender.publication_date %}
                  {{ detail.tender.publication_date.strftime('%d/%m/%Y %H:%M') }}
                {% else %}
                  Não informado
                {% endif %}
              </span>
            </li>
            <li class="flex items-start justify-between pb-2 border-b border-slate-100">
              <span class="text-slate-500">Abertura de Propostas:</span>
              <span class="font-semibold text-slate-800">
                {% if detail.tender.opening_date %}
                  {{ detail.tender.opening_date.strftime('%d/%m/%Y %H:%M') }}
                {% else %}
                  Não informado
                {% endif %}
              </span>
            </li>
            <li class="flex items-start justify-between pb-2 border-b border-slate-100">
              <span class="text-slate-500">Prazo Limite:</span>
              <span class="font-bold text-rose-600">
                {% if detail.tender.proposal_deadline %}
                  {{ detail.tender.proposal_deadline.strftime('%d/%m/%Y %H:%M') }}
                {% else %}
                  Não informado
                {% endif %}
              </span>
            </li>
          </ul>

          <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400 mt-6 mb-3">Identificação Oficial</h3>
          <div class="space-y-2 text-xs">
            <div>
              <span class="text-slate-400 block text-[10px]">Número de Controle PNCP:</span>
              <span class="font-mono text-slate-800 font-semibold">{{ detail.tender.external_id }}</span>
            </div>
            <div>
              <span class="text-slate-400 block text-[10px]">Número do Edital:</span>
              <span class="font-semibold text-slate-800">{{ detail.tender.notice_number }}</span>
            </div>
            <div>
              <span class="text-slate-400 block text-[10px]">Número do Processo:</span>
              <span class="font-semibold text-slate-800">{{ detail.tender.process_number }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- TAB 2: ITENS (sincronizados do endpoint /itens do PNCP) -->
  <div id="tab-content-items" class="hidden space-y-4">
    <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm">
      <div class="flex flex-col md:flex-row md:items-center justify-between gap-3 mb-4">
        <div>
          <h2 class="text-base font-bold text-slate-900">Itens e Lotes do Certame</h2>
          <p class="text-xs text-slate-500">
            <span id="itens-resumo">{{ detail.items|length }} item(ns) registrado(s)</span>
            <span id="itens-origem-badge" class="ml-1 inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold border {% if detail.items and detail.items[0].origem == 'PNCP' %}bg-emerald-50 text-emerald-700 border-emerald-200{% else %}bg-slate-100 text-slate-600 border-slate-200{% endif %}">
              {% if detail.items and detail.items[0].origem == 'PNCP' %}Fonte: PNCP{% else %}Fonte: base local{% endif %}
            </span>
          </p>
        </div>
        <div class="flex items-center gap-2">
          <div class="relative">
            <i class="fa-solid fa-magnifying-glass absolute left-2.5 top-2.5 text-[11px] text-slate-400"></i>
            <input id="itens-busca" type="search" placeholder="Filtrar itens..." class="pl-7 pr-2 py-1.5 text-xs border border-slate-200 rounded-lg w-44 focus:ring-2 focus:ring-sky-500 focus:outline-none">
          </div>
          <button type="button" id="btn-atualizar-itens" onclick="carregarItens(true)" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-sky-600 hover:bg-sky-500 text-white flex items-center gap-1.5 transition disabled:opacity-60">
            <i class="fa-solid fa-rotate"></i> Atualizar do PNCP
          </button>
        </div>
      </div>

      <div id="itens-aviso" class="hidden mb-3 text-xs px-3 py-2 rounded-xl border"></div>

      <div class="overflow-x-auto">
        <table class="w-full text-left text-xs border-collapse min-w-[860px]">
          <thead>
            <tr class="bg-slate-50 text-slate-600 font-semibold border-b border-slate-200">
              <th class="p-3 w-10">#</th>
              <th class="p-3">Descrição do Item</th>
              <th class="p-3">Unid.</th>
              <th class="p-3 text-right">Qtd</th>
              <th class="p-3 text-right">Valor Unitário</th>
              <th class="p-3 text-right">Valor Total Estimado</th>
              <th class="p-3">Critério / Benefício</th>
              <th class="p-3">Situação</th>
            </tr>
          </thead>
          <tbody id="itens-tbody" class="divide-y divide-slate-100 text-slate-700">
            {% for item in detail.items %}
            <tr class="hover:bg-slate-50/80 transition align-top">
              <td class="p-3 font-bold text-slate-900">{{ item.item_number }}</td>
              <td class="p-3 font-medium text-slate-800 max-w-md">
                {% if item.material_ou_servico %}
                <span class="mr-1 inline-block px-1.5 py-0.5 rounded text-[10px] font-bold {% if item.material_ou_servico == 'S' %}bg-emerald-50 text-emerald-700{% else %}bg-indigo-50 text-indigo-700{% endif %}">{{ 'Serviço' if item.material_ou_servico == 'S' else 'Material' }}</span>
                {% endif %}
                {{ item.description }}
                {% if item.informacao_complementar %}<div class="mt-1 text-[11px] text-slate-500">{{ item.informacao_complementar }}</div>{% endif %}
              </td>
              <td class="p-3">{{ item.unit or "Não informado" }}</td>
              <td class="p-3 text-right font-mono">{{ "{:,.2f}".format(item.quantity).rstrip('0').rstrip('.').replace(",", "X").replace(".", ",").replace("X", ".") if item.quantity else "—" }}</td>
              <td class="p-3 text-right font-mono">
                {% if item.orcamento_sigiloso %}<span class="text-amber-600 font-semibold">Sigiloso</span>
                {% elif item.estimated_unit_value %}R$ {{ "{:,.2f}".format(item.estimated_unit_value).replace(",", "X").replace(".", ",").replace("X", ".") }}
                {% else %}—{% endif %}
              </td>
              <td class="p-3 text-right font-bold text-slate-900 font-mono">
                {% if item.orcamento_sigiloso %}<span class="text-amber-600 font-semibold">Sigiloso</span>
                {% elif item.estimated_total_value %}R$ {{ "{:,.2f}".format(item.estimated_total_value).replace(",", "X").replace(".", ",").replace("X", ".") }}
                {% else %}—{% endif %}
              </td>
              <td class="p-3 text-[11px] text-slate-600">
                {{ item.criterio_julgamento or "—" }}
                {% if item.beneficio and 'sem benef' not in item.beneficio|lower %}<div class="mt-1 inline-block px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 font-semibold">{{ item.beneficio }}</div>{% endif %}
              </td>
              <td class="p-3 text-[11px] text-slate-600">{{ item.situacao or "—" }}</td>
            </tr>
            {% else %}
            <tr id="itens-vazio-linha"><td colspan="8" class="p-6 text-center text-slate-500">Nenhum item na base local. Consultando o PNCP...</td></tr>
            {% endfor %}
          </tbody>
          <tfoot>
            <tr class="bg-slate-50 border-t border-slate-200">
              <td colspan="5" class="p-3 text-right text-xs font-semibold text-slate-600">Total estimado dos itens:</td>
              <td id="itens-total" class="p-3 text-right text-xs font-black text-slate-900 font-mono">—</td>
              <td colspan="2"></td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  </div>

  <!-- TAB 3: DOCUMENTOS (anexos do PNCP servidos pela própria plataforma) -->
  <div id="tab-content-documents" class="hidden space-y-4">
    <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm">
      <div class="flex flex-col md:flex-row md:items-center justify-between gap-3 mb-4">
        <div>
          <h2 class="text-base font-bold text-slate-900">Arquivos e Anexos Oficiais</h2>
          <p class="text-xs text-slate-500">Edital, termo de referência, minutas e anexos publicados no PNCP — baixados diretamente por aqui.</p>
        </div>
        <div class="flex items-center gap-2">
          <button type="button" id="btn-atualizar-arquivos" onclick="carregarArquivos(true)" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-white border border-slate-300 text-slate-700 hover:bg-slate-50 flex items-center gap-1.5 transition disabled:opacity-60">
            <i class="fa-solid fa-rotate"></i> Atualizar lista
          </button>
          <button type="button" id="btn-baixar-todos" onclick="baixarArquivoInterno('/api/tenders/{{ detail.tender.id }}/arquivos/zip', this)" class="{% if not detail.documents|selectattr('download_url')|list %}hidden {% endif %}px-3 py-1.5 rounded-lg text-xs font-semibold bg-sky-600 hover:bg-sky-500 text-white flex items-center gap-1.5 transition disabled:opacity-60">
            <i class="fa-solid fa-file-zipper"></i> Baixar todos (ZIP)
          </button>
        </div>
      </div>

      <div id="arquivos-aviso" class="hidden mb-3 text-xs px-3 py-2 rounded-xl border"></div>

      <div id="arquivos-lista" class="space-y-3">
        {% for doc in detail.documents %}
        <div class="arquivo-card p-4 rounded-xl border border-slate-200 bg-slate-50 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          <div class="flex items-start gap-3 min-w-0">
            <div class="w-10 h-10 rounded-lg bg-red-100 text-red-600 flex items-center justify-center text-lg flex-shrink-0">
              <i class="fa-solid fa-file-lines"></i>
            </div>
            <div class="min-w-0">
              <div class="font-bold text-slate-800 text-xs break-words">{{ doc.name }}</div>
              <div class="text-[11px] text-slate-500 flex flex-wrap items-center gap-3 mt-1">
                <span>Tipo: <strong>{{ doc.document_type }}</strong></span>
                {% if doc.published_at %}<span>Publicado: {{ doc.published_at.strftime('%d/%m/%Y') }}</span>{% endif %}
              </div>
            </div>
          </div>
          <div class="flex items-center gap-2 flex-wrap">
            {% if doc.download_url %}
            <button type="button" onclick="baixarArquivoInterno('{{ doc.download_url }}', this)" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-white border border-slate-300 text-slate-700 hover:bg-slate-50 flex items-center gap-1.5 transition">
              <i class="fa-solid fa-download"></i> Baixar
            </button>
            {% else %}
            <span class="px-3 py-1.5 rounded-lg text-[11px] text-slate-500 bg-slate-100 border border-slate-200">Arquivo não disponível para download interno</span>
            {% endif %}
          </div>
        </div>
        {% else %}
        <div id="arquivos-vazio" class="p-6 rounded-xl border border-slate-200 bg-slate-50 text-center text-xs text-slate-500">
          <i class="fa-solid fa-spinner animate-spin mr-1"></i> Consultando os anexos publicados no PNCP...
        </div>
        {% endfor %}
      </div>
    </div>
  </div>

  <!-- TAB 4: ANÁLISE POR IA -->
  <div id="tab-content-ai" class="hidden space-y-6">
    <!-- Action Trigger Card -->
    <div class="bg-gradient-to-r from-slate-900 to-brand-900 p-6 rounded-2xl text-white shadow-lg flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
      <div>
        <div class="inline-flex items-center gap-1.5 bg-emerald-500/20 text-emerald-300 px-2.5 py-0.5 rounded-full text-xs font-mono mb-2">
          <i class="fa-solid fa-microchip"></i> Motor de IA Documental (Gemini 3.8 Flash)
        </div>
        <h2 class="text-lg font-bold">Auditoria e Resumo Inteligente do Edital</h2>
        <p class="text-xs text-slate-300 max-w-xl">
          Extrai requisitos de habilitação, dados comerciais, riscos, divergências e pontos de atenção com citações exatas de páginas.
        </p>
      </div>
      <button id="run-ai-btn" onclick="requestAiAnalysis('{{ detail.tender.id }}')" class="bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold px-5 py-3 rounded-xl text-xs transition shadow-md flex items-center gap-2 whitespace-nowrap">
        <i class="fa-solid fa-wand-magic-sparkles"></i>
        <span>Solicitar Análise de IA</span>
      </button>
    </div>

    <!-- AI Loading Indicator (Hidden by default) -->
    <div id="ai-loading" class="hidden bg-white p-8 rounded-2xl border border-slate-200 text-center space-y-3">
      <div class="w-12 h-12 border-4 border-emerald-500 border-t-transparent rounded-full animate-spin mx-auto"></div>
      <div class="text-sm font-bold text-slate-800">Processando documento com IA e extraindo requisitos...</div>
      <div class="text-xs text-slate-500">Verificando integridade, páginas e regras da Lei nº 14.133/2021</div>
    </div>

    <!-- AI Results Container -->
    <div id="ai-results" class="{% if not detail.latest_ai_analysis %}hidden{% endif %} space-y-6">
      <!-- Legal Disclaimer Banner -->
      <div class="p-4 bg-amber-50 border-l-4 border-amber-500 rounded-r-xl text-amber-900 text-xs flex items-start gap-3">
        <i class="fa-solid fa-triangle-exclamation text-amber-600 text-base mt-0.5"></i>
        <div>
          <span class="font-bold">Aviso Obrigatório de Conformidade:</span>
          <span id="ai-legal-notice">
            Esta análise foi gerada por inteligência artificial para fins de suporte à triagem rápida.
            Não substitui a leitura obrigatória e integral do Edital Oficial nem configura parecer jurídico definitivo.
          </span>
        </div>
      </div>

      <!-- Resumo Executivo Grid -->
      <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm">
        <h3 class="text-sm font-bold text-slate-900 mb-4 flex items-center gap-2">
          <i class="fa-solid fa-briefcase text-sky-600"></i> Resumo Executivo
        </h3>
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 text-xs" id="ai-exec-summary">
          <!-- Populated via script or server -->
        </div>
      </div>

      <!-- Requisitos de Participação & Habilitação -->
      <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm">
        <h3 class="text-sm font-bold text-slate-900 mb-4 flex items-center gap-2">
          <i class="fa-solid fa-clipboard-check text-emerald-600"></i> Requisitos de Habilitação e Participação
        </h3>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs" id="ai-hab-grid">
          <!-- Populated via script -->
        </div>
      </div>

      <!-- Pontos de Atenção & Riscos -->
      <div class="bg-white p-6 rounded-2xl border border-rose-200 bg-rose-50/30 shadow-sm">
        <h3 class="text-sm font-bold text-rose-800 mb-4 flex items-center gap-2">
          <i class="fa-solid fa-shield-virus text-rose-600"></i> Pontos de Atenção e Prazos Críticos
        </h3>
        <div class="space-y-3 text-xs" id="ai-attention-list">
          <!-- Populated via script -->
        </div>
      </div>

      <!-- Citações e Trechos de Apoio -->
      <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm">
        <h3 class="text-sm font-bold text-slate-900 mb-4 flex items-center gap-2">
          <i class="fa-solid fa-quote-left text-sky-600"></i> Citações e Trechos Documentais de Apoio
        </h3>
        <div class="space-y-3 text-xs" id="ai-citations-list">
          <!-- Populated via script -->
        </div>
      </div>
    </div>
  </div>

  <!-- TAB 5: HISTÓRICO DE ALTERAÇÕES -->
  <div id="tab-content-changes" class="hidden space-y-4">
    <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm">
      <h2 class="text-base font-bold text-slate-900 mb-3">Histórico e Retificações Oficiais</h2>
      {% if detail.changes|length == 0 %}
      <div class="text-xs text-slate-500 py-6 text-center">
        <i class="fa-solid fa-circle-check text-emerald-500 text-lg mb-2 block"></i>
        Nenhuma alteração, retificação ou prorrogação registrada para este certame até o momento.
      </div>
      {% else %}
      <div class="space-y-3">
        {% for ch in detail.changes %}
        <div class="p-3 bg-slate-50 rounded-xl border border-slate-100 text-xs">
          <div class="flex justify-between items-center font-bold text-slate-800">
            <span>{{ ch.change_type }} - {{ ch.field_name }}</span>
            <span class="text-slate-400 font-normal">{{ ch.detected_at.strftime('%d/%m/%Y %H:%M') }}</span>
          </div>
          <div class="mt-1 text-slate-600">Valor anterior: {{ ch.old_value }} → Novo: {{ ch.newValue }}</div>
        </div>
        {% endfor %}
      </div>
      {% endif %}
    </div>
  </div>

  <!-- TAB 6: OBSERVAÇÕES PESSOAIS -->
  <div id="tab-content-notes" class="hidden space-y-4">
    <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm">
      <h2 class="text-base font-bold text-slate-900 mb-2">Anotações Particulares da Licitação</h2>
      <p class="text-xs text-slate-500 mb-4">Adicione observações da sua equipe para acompanhar estratégia, concorrentes e prazos internos.</p>
      <textarea id="user-note-input" rows="5" class="w-full text-xs p-3 border border-slate-200 rounded-xl focus:ring-2 focus:ring-sky-500 focus:outline-none" placeholder="Digite suas notas internas sobre este certame...">{{ detail.user_notes or '' }}</textarea>
      <div class="mt-3 flex justify-end">
        <button onclick="saveUserNote('{{ detail.tender.id }}')" class="bg-sky-600 hover:bg-sky-500 text-white font-semibold px-4 py-2 rounded-xl text-xs transition">
          Salvar Anotação
        </button>
      </div>
    </div>
  </div>

</div>
{% endblock %}

{% block scripts %}
<script>
  async function baixarKitHabilitacao(botao) {
    const url = botao.getAttribute('data-kit-url');
    const original = botao.innerHTML;
    botao.disabled = true;
    botao.innerHTML = '<i class="fa-solid fa-spinner fa-spin text-xs"></i> Montando kit...';
    try {
      const resp = await fetch(url);
      if (!resp.ok) {
        let msg = 'Não foi possível montar o kit de habilitação.';
        try { const err = await resp.json(); if (err && typeof err.detail === 'string') msg = err.detail; } catch (e) {}
        throw new Error(msg);
      }
      const blob = await resp.blob();
      const cd = resp.headers.get('Content-Disposition') || '';
      const m = cd.match(/filename="?([^";]+)"?/i);
      const nome = m ? m[1] : 'Kit_Habilitacao.zip';
      const href = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = href;
      a.download = nome;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(href), 1500);
      showToast(`Kit ${nome} pronto. Confira o Relatório de Conformidade incluso.`, 'success');
    } catch (e) {
      showToast(e.message || 'Falha ao baixar o kit.', 'error');
    } finally {
      botao.disabled = false;
      botao.innerHTML = original;
    }
  }

  // ---------------- Itens e anexos do PNCP ----------------
  const TENDER_ID = {{ detail.tender.id|tojson }};

  function escHtml(v) {
    return String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function moedaBR(v) {
    return (v === null || v === undefined || isNaN(v)) ? '—' : new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' }).format(v);
  }
  function numeroBR(v) {
    return (v === null || v === undefined || isNaN(v)) ? '—' : new Intl.NumberFormat('pt-BR', { maximumFractionDigits: 4 }).format(v);
  }
  function tamanhoArquivo(bytes) {
    if (!bytes) return '';
    const u = ['B', 'KB', 'MB', 'GB']; let i = 0; let n = bytes;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i ? 1 : 0).replace('.', ',')} ${u[i]}`;
  }
  function mostrarAviso(id, mensagem, tipo) {
    const el = document.getElementById(id);
    if (!el) return;
    if (!mensagem) { el.classList.add('hidden'); return; }
    const cores = {
      info: 'bg-sky-50 border-sky-200 text-sky-800',
      alerta: 'bg-amber-50 border-amber-200 text-amber-800',
      erro: 'bg-rose-50 border-rose-200 text-rose-800'
    };
    el.className = `mb-3 text-xs px-3 py-2 rounded-xl border ${cores[tipo] || cores.info}`;
    el.textContent = mensagem;
  }

  let itensCarregados = [];

  function renderizarItens() {
    const termo = (document.getElementById('itens-busca').value || '').toLowerCase().trim();
    const tbody = document.getElementById('itens-tbody');
    const visiveis = itensCarregados.filter(i => !termo || `${i.item_number} ${i.description} ${i.informacao_complementar || ''}`.toLowerCase().includes(termo));
    if (!visiveis.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="p-6 text-center text-slate-500">${itensCarregados.length ? 'Nenhum item corresponde ao filtro.' : 'Nenhum item publicado para este certame.'}</td></tr>`;
    } else {
      tbody.innerHTML = visiveis.map(i => {
        const tipo = i.material_ou_servico
          ? `<span class="mr-1 inline-block px-1.5 py-0.5 rounded text-[10px] font-bold ${i.material_ou_servico === 'S' ? 'bg-emerald-50 text-emerald-700' : 'bg-indigo-50 text-indigo-700'}">${i.material_ou_servico === 'S' ? 'Serviço' : 'Material'}</span>` : '';
        const sigiloso = '<span class="text-amber-600 font-semibold">Sigiloso</span>';
        const beneficio = (i.beneficio && !/sem benef/i.test(i.beneficio))
          ? `<div class="mt-1 inline-block px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 font-semibold">${escHtml(i.beneficio)}</div>` : '';
        const compl = i.informacao_complementar ? `<div class="mt-1 text-[11px] text-slate-500">${escHtml(i.informacao_complementar)}</div>` : '';
        return `<tr class="hover:bg-slate-50/80 transition align-top">
          <td class="p-3 font-bold text-slate-900">${i.item_number}</td>
          <td class="p-3 font-medium text-slate-800 max-w-md">${tipo}${escHtml(i.description)}${compl}</td>
          <td class="p-3">${escHtml(i.unit || '—')}</td>
          <td class="p-3 text-right font-mono">${numeroBR(i.quantity)}</td>
          <td class="p-3 text-right font-mono">${i.orcamento_sigiloso ? sigiloso : moedaBR(i.estimated_unit_value)}</td>
          <td class="p-3 text-right font-bold text-slate-900 font-mono">${i.orcamento_sigiloso ? sigiloso : moedaBR(i.estimated_total_value)}</td>
          <td class="p-3 text-[11px] text-slate-600">${escHtml(i.criterio_julgamento || '—')}${beneficio}</td>
          <td class="p-3 text-[11px] text-slate-600">${escHtml(i.situacao || '—')}</td>
        </tr>`;
      }).join('');
    }
    const total = itensCarregados.reduce((s, i) => s + (i.orcamento_sigiloso ? 0 : (i.estimated_total_value || 0)), 0);
    document.getElementById('itens-total').textContent = total ? moedaBR(total) : '—';
  }

  async function carregarItens(atualizar = false) {
    const btn = document.getElementById('btn-atualizar-itens');
    const original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner animate-spin"></i> Consultando PNCP...';
    try {
      const resp = await fetch(`/api/tenders/${encodeURIComponent(TENDER_ID)}/itens${atualizar ? '?atualizar=true' : ''}`);
      const data = await resp.json();
      if (!resp.ok) throw new Error(mensagemErroApi(data, 'Falha ao carregar os itens.'));
      itensCarregados = data.itens || [];
      document.getElementById('tab-count-itens').textContent = data.total;
      document.getElementById('itens-resumo').textContent = `${data.total} item(ns) registrado(s)`;
      const badge = document.getElementById('itens-origem-badge');
      const doPncp = data.origem === 'PNCP';
      badge.textContent = doPncp ? 'Fonte: PNCP' : 'Fonte: base local';
      badge.className = `ml-1 inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold border ${doPncp ? 'bg-emerald-50 text-emerald-700 border-emerald-200' : 'bg-slate-100 text-slate-600 border-slate-200'}`;
      const tipoAviso = ['indisponivel'].includes(data.status) ? 'alerta' : 'info';
      mostrarAviso('itens-aviso', data.mensagem, tipoAviso);
      renderizarItens();
      if (atualizar && data.status === 'sincronizado') showToast(`${data.total} item(ns) sincronizado(s) do PNCP.`, 'success');
    } catch (e) {
      mostrarAviso('itens-aviso', e.message || 'Falha ao carregar os itens.', 'erro');
    } finally {
      btn.disabled = false;
      btn.innerHTML = original;
    }
  }

  function iconeArquivo(mime) {
    if ((mime || '').includes('pdf')) return 'fa-file-pdf text-red-600 bg-red-100';
    if ((mime || '').includes('zip') || (mime || '').includes('rar')) return 'fa-file-zipper text-amber-600 bg-amber-100';
    if ((mime || '').includes('word') || (mime || '').includes('opendocument.text')) return 'fa-file-word text-sky-700 bg-sky-100';
    if ((mime || '').includes('sheet') || (mime || '').includes('excel')) return 'fa-file-excel text-emerald-700 bg-emerald-100';
    return 'fa-file-lines text-slate-600 bg-slate-200';
  }

  function renderizarArquivos(data) {
    const lista = document.getElementById('arquivos-lista');
    const arquivos = data.arquivos || [];
    document.getElementById('tab-count-docs').textContent = arquivos.length;
    document.getElementById('btn-baixar-todos').classList.toggle('hidden', !data.zip_url);
    if (!arquivos.length) {
      lista.innerHTML = `<div class="p-6 rounded-xl border border-slate-200 bg-slate-50 text-center text-xs text-slate-500">Nenhum anexo disponível para este certame.</div>`;
      return;
    }
    lista.innerHTML = arquivos.map(d => {
      const icone = iconeArquivo(d.mime_type).split(' ');
      const publicado = d.published_at ? `<span>Publicado: ${new Date(d.published_at).toLocaleDateString('pt-BR')}</span>` : '';
      const tamanho = d.file_size ? `<span>${tamanhoArquivo(d.file_size)}</span>` : '';
      const acoes = d.baixavel
        ? `${(d.mime_type || '').includes('pdf') ? `<a href="${d.visualizar_url}" target="_blank" rel="noopener" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-white border border-slate-300 text-slate-700 hover:bg-slate-50 flex items-center gap-1.5 transition"><i class="fa-regular fa-eye"></i> Visualizar</a>` : ''}
           <button type="button" onclick="baixarArquivoInterno('${d.download_url}', this)" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-sky-600 hover:bg-sky-500 text-white flex items-center gap-1.5 transition disabled:opacity-60"><i class="fa-solid fa-download"></i> Baixar</button>
           <button type="button" onclick="requestAiAnalysis('${TENDER_ID}', '${d.id}')" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-emerald-600 hover:bg-emerald-500 text-white flex items-center gap-1.5 transition"><i class="fa-solid fa-wand-magic-sparkles"></i> Analisar com IA</button>`
        : `<span class="px-3 py-1.5 rounded-lg text-[11px] text-slate-500 bg-slate-100 border border-slate-200">Arquivo não disponível para download interno</span>`;
      return `<div class="arquivo-card p-4 rounded-xl border border-slate-200 bg-slate-50 flex flex-col sm:flex-row sm:items-center justify-between gap-3" data-doc-id="${d.id}">
        <div class="flex items-start gap-3 min-w-0">
          <div class="w-10 h-10 rounded-lg flex items-center justify-center text-lg flex-shrink-0 ${icone[2]} ${icone[1]}"><i class="fa-solid ${icone[0]}"></i></div>
          <div class="min-w-0">
            <div class="font-bold text-slate-800 text-xs break-words">${escHtml(d.name)}</div>
            <div class="text-[11px] text-slate-500 flex flex-wrap items-center gap-3 mt-1">
              <span>Tipo: <strong>${escHtml(d.document_type)}</strong></span>${publicado}${tamanho}
              ${d.origem === 'PNCP' ? '<span class="px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 border border-emerald-200 font-bold text-[10px]">PNCP</span>' : ''}
            </div>
          </div>
        </div>
        <div class="flex items-center gap-2 flex-wrap">${acoes}</div>
      </div>`;
    }).join('');
  }

  async function carregarArquivos(atualizar = false) {
    const btn = document.getElementById('btn-atualizar-arquivos');
    const original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner animate-spin"></i> Consultando PNCP...';
    try {
      const resp = await fetch(`/api/tenders/${encodeURIComponent(TENDER_ID)}/arquivos${atualizar ? '?atualizar=true' : ''}`);
      const data = await resp.json();
      if (!resp.ok) throw new Error(mensagemErroApi(data, 'Falha ao listar os anexos.'));
      renderizarArquivos(data);
      mostrarAviso('arquivos-aviso', data.mensagem, data.status === 'indisponivel' ? 'alerta' : 'info');
    } catch (e) {
      mostrarAviso('arquivos-aviso', e.message || 'Falha ao listar os anexos.', 'erro');
    } finally {
      btn.disabled = false;
      btn.innerHTML = original;
    }
  }

  // Download servido pela plataforma (proxy com cache): o usuário não sai do sistema
  async function baixarArquivoInterno(url, botao) {
    const original = botao ? botao.innerHTML : '';
    if (botao) { botao.disabled = true; botao.innerHTML = '<i class="fa-solid fa-spinner animate-spin"></i> Baixando...'; }
    try {
      const resp = await fetch(url);
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(mensagemErroApi(data, 'Não foi possível baixar o arquivo.'));
      }
      const blob = await resp.blob();
      const cd = resp.headers.get('Content-Disposition') || '';
      let nome = 'documento';
      const mUtf = cd.match(/filename\*=(?:UTF-8|utf-8)''([^;]+)/);
      const mSimples = cd.match(/filename="?([^";]+)"?/i);
      if (mUtf) nome = decodeURIComponent(mUtf[1]); else if (mSimples) nome = mSimples[1];
      const href = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = href;
      a.download = nome;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(href), 2000);
      showToast(`${nome} baixado.`, 'success');
    } catch (e) {
      showToast(e.message || 'Falha no download.', 'error');
    } finally {
      if (botao) { botao.disabled = false; botao.innerHTML = original; }
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('itens-busca').addEventListener('input', renderizarItens);
    carregarItens(false);
    carregarArquivos(false);
  });

  function switchTab(tabName) {
    const tabs = ['overview', 'items', 'documents', 'ai', 'changes', 'notes'];
    tabs.forEach(t => {
      const content = document.getElementById(`tab-content-${t}`);
      const btn = document.getElementById(`tab-btn-${t}`);
      if (content) content.classList.add('hidden');
      if (btn) {
        btn.classList.remove('border-sky-600', 'text-sky-600');
        btn.classList.add('border-transparent');
      }
    });

    const activeContent = document.getElementById(`tab-content-${tabName}`);
    const activeBtn = document.getElementById(`tab-btn-${tabName}`);
    if (activeContent) activeContent.classList.remove('hidden');
    if (activeBtn) {
      activeBtn.classList.remove('border-transparent');
      activeBtn.classList.add('border-sky-600', 'text-sky-600');
    }
  }

  function copyTenderLink() {
    navigator.clipboard.writeText(window.location.href);
    showToast('Link do certame copiado para a área de transferência!', 'success');
  }

  async function saveUserNote(tenderId) {
    const token = AppSession.getToken();
    if (!token) {
      showToast('Faça login para salvar anotações particulares.', 'info');
      setTimeout(() => window.location.href = '/login', 1000);
      return;
    }

    const note = document.getElementById('user-note-input').value;
    try {
      const resp = await fetch(`/api/tenders/${tenderId}/notes`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`
        },
        body: JSON.stringify({ content: note })
      });
      if (resp.ok) {
        showToast('Anotação registrada com sucesso.', 'success');
      } else {
        showToast('Erro ao salvar anotação.', 'error');
      }
    } catch {
      showToast('Falha de conexão com o servidor.', 'error');
    }
  }

  async function requestAiAnalysis(tenderId, documentId = null) {
    switchTab('ai');
    const loading = document.getElementById('ai-loading');
    const results = document.getElementById('ai-results');
    const runBtn = document.getElementById('run-ai-btn');

    loading.classList.remove('hidden');
    results.classList.add('hidden');
    runBtn.disabled = true;

    try {
      const resp = await fetch(`/api/tenders/${tenderId}/ai-analyze`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ document_id: documentId })
      });

      const data = await resp.json();
      loading.classList.add('hidden');
      runBtn.disabled = false;

      if (resp.ok && data.success) {
        renderAiResults(data.data);
        results.classList.remove('hidden');
        showToast('Análise por Inteligência Artificial concluída!', 'success');
      } else {
        showToast(data.detail || 'Falha ao processar análise do edital.', 'error');
      }
    } catch (e) {
      loading.classList.add('hidden');
      runBtn.disabled = false;
      showToast('Erro de comunicação com o serviço de IA.', 'error');
    }
  }

  async function exportToDisputeCockpit(tenderId, title, value, portal) {
    try {
      const resp = await fetch('/api/disputes/track', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id_pregao: String(tenderId),
          numero_item: 1,
          descricao: title || 'Lote / Item do Certame',
          valor_referencia: Number(value) || 1000.0,
          margem_minima: 10.0,
          stop_loss: (Number(value) || 1000.0) * 0.7,
          fonte_portal: portal || 'Compras.gov.br'
        })
      });
      if (resp.ok) {
        showToast('Certame exportado para o Cockpit com sucesso!', 'success');
        setTimeout(() => {
          window.location.href = '/disputa';
        }, 700);
      } else {
        const err = await resp.json().catch(() => ({}));
        showToast(err.detail || 'Falha ao exportar para o Cockpit.', 'error');
      }
    } catch (e) {
      showToast('Erro ao comunicar com o Cockpit.', 'error');
    }
  }

  function renderAiResults(aiData) {
    // 0. Atualizar Card da Visão Geral se presente
    const ovResumo = document.getElementById('gemini-overview-resumo');
    const ovRiscos = document.getElementById('gemini-overview-riscos');
    if (ovResumo && aiData.resumo_executivo) {
      ovResumo.textContent = aiData.resumo_executivo.o_que_sera_contratado || aiData.resumo_executivo.resumo || ovResumo.textContent;
    }
    if (ovRiscos && aiData.pontos_atencao) {
      ovRiscos.textContent = aiData.pontos_atencao.penalidades_previstas || aiData.pontos_atencao.prazos_urgentes_ou_curtos || ovRiscos.textContent;
    }

    // 1. Resumo Executivo
    const execGrid = document.getElementById('ai-exec-summary');
    const ex = aiData.resumo_executivo || {};
    execGrid.innerHTML = `
      <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
        <span class="text-slate-400 font-semibold block text-[10px] uppercase">O que será contratado:</span>
        <span class="font-bold text-slate-800">${ex.o_que_sera_contratado || 'Não informado'}</span>
      </div>
      <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
        <span class="text-slate-400 font-semibold block text-[10px] uppercase">Quem contrata:</span>
        <span class="font-bold text-slate-800">${ex.quem_contrata || 'Não informado'}</span>
      </div>
      <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
        <span class="text-slate-400 font-semibold block text-[10px] uppercase">Local / Município:</span>
        <span class="font-bold text-slate-800">${ex.onde_local || 'Não informado'}</span>
      </div>
      <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
        <span class="text-slate-400 font-semibold block text-[10px] uppercase">Modalidade:</span>
        <span class="font-bold text-slate-800">${ex.modalidade || 'Não informado'}</span>
      </div>
      <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
        <span class="text-slate-400 font-semibold block text-[10px] uppercase">Valor Estimado:</span>
        <span class="font-bold text-emerald-700">${ex.valor_estimado || 'Não informado'}</span>
      </div>
      <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
        <span class="text-slate-400 font-semibold block text-[10px] uppercase">Cronograma / Datas:</span>
        <span class="font-bold text-slate-800">${ex.datas_relevantes || 'Não informado'}</span>
      </div>
    `;

    // 2. Habilitação
    const habGrid = document.getElementById('ai-hab-grid');
    const hab = aiData.requisitos_participacao || {};
    habGrid.innerHTML = `
      <div class="p-4 bg-slate-50 rounded-xl border border-slate-100 space-y-2">
        <h4 class="font-bold text-slate-800 text-xs flex items-center gap-1.5"><i class="fa-solid fa-scale-balanced text-sky-500"></i> Habilitação Jurídica e Fiscal</h4>
        <ul class="list-disc list-inside text-slate-600 space-y-1">
          ${(hab.habilitacao_juridica || []).concat(hab.regularidade_fiscal_trabalhista || []).map(i => `<li>${i}</li>`).join('') || '<li>Conforme edital oficial</li>'}
        </ul>
      </div>
      <div class="p-4 bg-slate-50 rounded-xl border border-slate-100 space-y-2">
        <h4 class="font-bold text-slate-800 text-xs flex items-center gap-1.5"><i class="fa-solid fa-certificate text-emerald-500"></i> Qualificação Técnica e Registros</h4>
        <ul class="list-disc list-inside text-slate-600 space-y-1">
          ${(hab.qualificacao_tecnica || []).map(i => `<li>${i}</li>`).join('') || '<li>Conforme edital oficial</li>'}
          ${hab.registros_profissionais_ou_sanitarios ? `<li><strong>Registros:</strong> ${hab.registros_profissionais_ou_sanitarios}</li>` : ''}
        </ul>
      </div>
    `;

    // 3. Pontos de Atenção
    const attList = document.getElementById('ai-attention-list');
    const att = aiData.pontos_atencao || {};
    attList.innerHTML = `
      <div class="p-3 bg-white rounded-xl border border-rose-200">
        <span class="font-bold text-rose-700 block mb-1"><i class="fa-solid fa-clock"></i> Prazos e Impugnações:</span>
        <p class="text-slate-700">${att.prazos_urgentes_ou_curtos || 'Não informado'}</p>
      </div>
      <div class="p-3 bg-white rounded-xl border border-rose-200">
        <span class="font-bold text-rose-700 block mb-1"><i class="fa-solid fa-circle-exclamation"></i> Penalidades e Cláusulas Críticas:</span>
        <p class="text-slate-700">${att.penalidades_previstas || 'Conforme Lei 14.133/2021'}</p>
      </div>
    `;

    // 4. Citações
    const citList = document.getElementById('ai-citations-list');
    const cits = aiData.citacoes || [];
    citList.innerHTML = cits.map(c => `
      <div class="p-3 bg-slate-50 rounded-xl border border-slate-200">
        <div class="flex justify-between items-center font-bold text-slate-800 mb-1">
          <span>${c.topico || 'Trecho Oficial'}</span>
          <span class="text-sky-600 bg-sky-50 px-2 py-0.5 rounded text-[10px]">${c.pagina || 'Página do Edital'}</span>
        </div>
        <p class="italic text-slate-600">"${c.trecho || ''}"</p>
      </div>
    `).join('') || '<div class="text-slate-400">Nenhuma citação destacada.</div>';
  }

  // Pre-load existing AI analysis if present
  {% if detail.latest_ai_analysis %}
  document.addEventListener('DOMContentLoaded', () => {
    try {
      const preLoadedData = {{ detail.latest_ai_analysis.result_json|tojson }};
      renderAiResults(preLoadedData);
    } catch {}
  });
  {% endif %}
</script>
{% endblock %}
EOF

# ==============================================================================
# COMPARTILHADOS — modelos, migração idempotente e rotas (cobrem as 4 melhorias)
# ==============================================================================
mkdir -p server
cat << 'EOF' > server/models.py
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

    favorites = relationship("Favorite", back_populates="user", cascade="all, delete-orphan")
    notes = relationship("UserNote", back_populates="user", cascade="all, delete-orphan")
    saved_searches = relationship("SavedSearch", back_populates="user", cascade="all, delete-orphan")
    alerts = relationship("Alert", back_populates="user", cascade="all, delete-orphan")
    search_history = relationship("SearchHistory", back_populates="user", cascade="all, delete-orphan")


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
    stage = Column(String(50), default="oportunidades", nullable=False, index=True) # oportunidades, qualificacao, disputa, classificacao, contrato
    notes = Column(Text, nullable=True)
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
EOF

mkdir -p server
cat << 'EOF' > server/migrate.py
"""
Migrações idempotentes do banco (SQLite em desenvolvimento/Render; compatível com PostgreSQL).

Regras:
  - Cada instrução DDL roda em transação própria, para que uma falha "já existe"
    não aborte as demais (no PostgreSQL um erro invalida a transação inteira).
  - Colunas só são adicionadas se ausentes; erros de "duplicate column" são ignorados.
  - Índices usam CREATE INDEX IF NOT EXISTS e são deduplicados por nome.
  - Tabelas auxiliares são criadas a partir dos modelos com checkfirst=True.
  - Pode ser executado quantas vezes for necessário sem efeitos colaterais.
"""
import logging
from typing import Iterable, List, Set, Tuple

from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from server.database import engine

logger = logging.getLogger(__name__)

_MARCADORES_JA_EXISTE = (
    "already exists",
    "duplicate column",
    "duplicate key name",
    "duplicate_object",
    "já existe",
)

# (tabela, coluna, tipo/DDL da coluna)
COLUNAS_SAVED_SEARCHES: List[Tuple[str, str, str]] = [
    ("saved_searches", "ufs", "VARCHAR(255)"),
    ("saved_searches", "min_value", "FLOAT"),
]

COLUNAS_TENDERS: List[Tuple[str, str, str]] = [
    ("tenders", "source_system", "VARCHAR(50) DEFAULT 'pncp' NOT NULL"),
    ("tenders", "uasg_code", "VARCHAR(50)"),
    ("tenders", "sicaf_ready", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("tenders", "link_comprasnet", "VARCHAR(1000)"),
    ("tenders", "link_pncp", "VARCHAR(1000)"),
    ("tenders", "url_disputa", "VARCHAR(1000)"),
    ("tenders", "url_pncp", "VARCHAR(1000)"),
    ("tenders", "fonte_portal", "VARCHAR(100) DEFAULT 'PNCP'"),
    ("tenders", "link_edital_oficial", "VARCHAR(1000)"),
    ("tenders", "ai_resumo", "TEXT"),
    ("tenders", "ai_pontos_atencao", "TEXT"),
    ("tenders", "itens_extraidos_json", "TEXT"),
    ("tenders", "natureza_objeto", "VARCHAR(20)"),
]

COLUNAS_ITENS_DOCUMENTOS: List[Tuple[str, str, str]] = [
    ("tender_items", "material_ou_servico", "VARCHAR(1)"),
    ("tender_items", "criterio_julgamento", "VARCHAR(100)"),
    ("tender_items", "situacao", "VARCHAR(100)"),
    ("tender_items", "beneficio", "VARCHAR(150)"),
    ("tender_items", "orcamento_sigiloso", "BOOLEAN"),
    ("tender_items", "informacao_complementar", "TEXT"),
    ("tender_items", "origem", "VARCHAR(20)"),
    ("tender_items", "synced_at", "DATETIME"),
    ("tender_documents", "sequencial_documento", "INTEGER"),
    ("tender_documents", "origem", "VARCHAR(20)"),
]

# (nome do índice, tabela, colunas)
INDICES_EXPLICITOS: List[Tuple[str, str, str]] = [
    ("ix_tenders_opening_date", "tenders", "opening_date"),
    ("ix_tenders_status_opening", "tenders", "status, opening_date"),
    ("ix_tenders_url_pncp", "tenders", "url_pncp"),
    ("ix_tenders_url_disputa", "tenders", "url_disputa"),
    ("ix_tenders_natureza_objeto", "tenders", "natureza_objeto"),
]


# ------------------------------------------------------------------------------
# Utilitários idempotentes
# ------------------------------------------------------------------------------
def _ja_existe(exc: Exception) -> bool:
    mensagem = str(getattr(exc, "orig", exc)).lower()
    return any(marcador in mensagem for marcador in _MARCADORES_JA_EXISTE)


def _executar_ddl(sql: str, descricao: str) -> bool:
    """Executa uma instrução DDL isolada. Retorna False se a estrutura já existia."""
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
        return True
    except (OperationalError, ProgrammingError) as exc:
        if _ja_existe(exc):
            logger.debug("Migração: %s já existe, ignorado.", descricao)
            return False
        raise


def _tabelas_existentes() -> Set[str]:
    return set(inspect(engine).get_table_names())


def _colunas_existentes(tabela: str) -> Set[str]:
    return {c["name"] for c in inspect(engine).get_columns(tabela)}


def _adicionar_colunas(colunas: Iterable[Tuple[str, str, str]]) -> None:
    tabelas = _tabelas_existentes()
    cache: dict = {}
    for tabela, coluna, ddl in colunas:
        if tabela not in tabelas:
            continue
        if tabela not in cache:
            cache[tabela] = _colunas_existentes(tabela)
        if coluna in cache[tabela]:
            continue
        logger.info("Migração: adicionando coluna %s à tabela %s", coluna, tabela)
        if _executar_ddl(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {ddl}", f"coluna {tabela}.{coluna}"):
            cache[tabela].add(coluna)


def _criar_indice(nome: str, tabela: str, colunas: str) -> None:
    _executar_ddl(f"CREATE INDEX IF NOT EXISTS {nome} ON {tabela} ({colunas})", f"índice {nome}")


def _criar_tabela_do_modelo(modelo) -> None:
    """Cria a tabela (e seus índices) a partir do modelo, somente se ainda não existir."""
    tabela = modelo.__table__
    if tabela.name in _tabelas_existentes():
        return
    logger.info("Migração: criando tabela %s", tabela.name)
    try:
        tabela.create(bind=engine, checkfirst=True)
    except (OperationalError, ProgrammingError) as exc:
        if not _ja_existe(exc):
            raise
        logger.debug("Migração: estrutura de %s já existia (%s).", tabela.name, exc)


def _garantir_indices_dos_modelos() -> None:
    """Cria, se faltarem, os índices declarados nos modelos (deduplicados por nome)."""
    from server.database import Base
    import server.models  # noqa: F401  (registra os modelos no metadata)

    tabelas = _tabelas_existentes()
    vistos: Set[str] = set()
    for tabela in Base.metadata.sorted_tables:
        if tabela.name not in tabelas:
            continue
        colunas = _colunas_existentes(tabela.name)
        for indice in sorted(tabela.indexes, key=lambda i: i.name or ""):
            # Índices únicos não são criados aqui: em bases legadas com dados duplicados
            # falhariam com IntegrityError e derrubariam o deploy.
            if not indice.name or indice.name in vistos or indice.unique:
                continue
            vistos.add(indice.name)
            nomes_colunas = [c.name for c in indice.columns]
            if not nomes_colunas or any(n not in colunas for n in nomes_colunas):
                continue
            _executar_ddl(
                f"CREATE INDEX IF NOT EXISTS {indice.name} ON {tabela.name} ({', '.join(nomes_colunas)})",
                f"índice {indice.name}",
            )


# ------------------------------------------------------------------------------
# Etapas
# ------------------------------------------------------------------------------
def _backfill_identificadores() -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE tenders SET link_pncp = source_url WHERE link_pncp IS NULL"))
        conn.execute(text(
            "UPDATE tenders SET source_system = 'compras_gov', sicaf_ready = 1, uasg_code = '250005', "
            "link_comprasnet = 'https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra=250005' "
            "WHERE administrative_sphere = 'FEDERAL' AND (source_system IS NULL OR source_system = 'pncp')"
        ))
        conn.execute(text(
            "UPDATE tenders SET source_system = 'pncp', sicaf_ready = 0 "
            "WHERE administrative_sphere != 'FEDERAL' AND source_system IS NULL"
        ))


def _backfill_natureza_objeto(lote: int = 500) -> None:
    """Classifica (MATERIAL / SERVICO / OUTROS) os certames ainda sem natureza. Idempotente."""
    from server.services.portal_collector import classificar_natureza_certame

    total = 0
    while True:
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT id, title, object_description FROM tenders WHERE natureza_objeto IS NULL LIMIT :lote"),
                {"lote": lote},
            ).fetchall()
            if not rows:
                break
            for t_id, titulo, descricao in rows:
                conn.execute(
                    text("UPDATE tenders SET natureza_objeto = :nat WHERE id = :t_id"),
                    {"nat": classificar_natureza_certame(titulo, descricao), "t_id": t_id},
                )
            total += len(rows)
        if len(rows) < lote:
            break
    if total:
        logger.info("Migração: natureza do objeto classificada em %s licitações.", total)


def _backfill_links_canonicos() -> None:
    """Backfill canônico de URLs do PNCP e links de disputa (idempotente: recalcula os mesmos valores)."""
    from server.connectors.pncp import build_canonical_pncp_url

    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, external_id, source_url, link_pncp, link_comprasnet, official_notice_url, url_disputa, url_pncp FROM tenders"
        )).fetchall()

        updated_count = 0
        for r in rows:
            t_id, ext_id, src_url, l_pncp, l_cnet, off_url, u_disp, u_pncp = r

            canon_pncp = build_canonical_pncp_url(numero_controle_pncp=ext_id or l_pncp or src_url)

            if l_cnet:
                dispute = l_cnet
            elif off_url and "pncp.gov.br" not in off_url:
                dispute = off_url
            elif u_disp and "pncp.gov.br" not in u_disp:
                dispute = u_disp
            else:
                dispute = canon_pncp

            if (u_pncp, l_pncp, src_url, u_disp) == (canon_pncp, canon_pncp, canon_pncp, dispute):
                continue

            conn.execute(
                text("""
                    UPDATE tenders
                    SET url_pncp = :canon_pncp,
                        link_pncp = :canon_pncp,
                        source_url = :canon_pncp,
                        url_disputa = :dispute
                    WHERE id = :t_id
                """),
                {"canon_pncp": canon_pncp, "dispute": dispute, "t_id": t_id},
            )
            updated_count += 1

        logger.info("Migração: %s licitações atualizadas com links canônicos do PNCP e disputa.", updated_count)


def run_db_migrations():
    """
    Executa migrações automáticas no banco SQLite/PostgreSQL garantindo que todas as
    colunas, tabelas e índices existam. Seguro para execução repetida (idempotente).
    """
    if "tenders" not in _tabelas_existentes():
        # Banco ainda sem esquema: Base.metadata.create_all (server/main.py) cria tudo.
        return

    from server.models import DocumentoEmpresa, HistoricoDisputa, PasswordResetToken

    # 1. Colunas novas em tabelas existentes
    _adicionar_colunas(COLUNAS_SAVED_SEARCHES)
    _adicionar_colunas(COLUNAS_TENDERS)
    _adicionar_colunas(COLUNAS_ITENS_DOCUMENTOS)

    # 2. Tabelas auxiliares (auditoria de disputas, cofre de documentos e recuperação de senha)
    _criar_tabela_do_modelo(HistoricoDisputa)
    _criar_tabela_do_modelo(DocumentoEmpresa)
    if "users" in _tabelas_existentes():
        _criar_tabela_do_modelo(PasswordResetToken)

    # 3. Backfill de links, identificadores e natureza do objeto para dados existentes
    _backfill_identificadores()
    _backfill_natureza_objeto()

    # 4. Índices de alta performance (explícitos + declarados nos modelos)
    for nome, tabela, colunas in INDICES_EXPLICITOS:
        _criar_indice(nome, tabela, colunas)
    _garantir_indices_dos_modelos()

    # 5. Backfill canônico de URLs do PNCP e links de disputa
    _backfill_links_canonicos()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_db_migrations()
    print("Migrações executadas com sucesso.")
EOF

mkdir -p server
cat << 'EOF' > server/main.py
import os
import sys
import json
import csv
import io
import asyncio
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any, Union
from fastapi import FastAPI, Depends, HTTPException, status, Query, Request, Body, Form, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func, text
from pydantic import BaseModel, Field

# Garantir importações locais
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server.config import settings
from server.database import get_db, engine, Base
from server.models import (
    User, Source, Tender, TenderItem, TenderDocument,
    Favorite, UserNote, SavedSearch, Alert, AlertDelivery,
    TenderChange, DocumentExtraction, AIAnalysis, SearchHistory,
    WebhookDispatchLog, Deal, DEAL_STAGES, DEAL_STAGE_LABELS,
    LegalDocumentTemplate, SupplierProspect, generate_uuid, PasswordResetToken,
    DocumentoEmpresa, DOCUMENTO_CATEGORIAS, DOCUMENTO_DIAS_ALERTA, hoje_sao_paulo
)
from server.schemas import (
    UserRegisterRequest, UserLoginRequest, PasswordChangeRequest,
    PasswordResetRequest, PasswordResetConfirmRequest,
    UserProfileResponse, TokenResponse, SearchFilterParams,
    SearchResultResponse, TenderDetailResponse, TenderSummaryResponse,
    TenderItemResponse, TenderDocumentResponse, TenderChangeResponse,
    AIAnalysisSummaryResponse, FavoriteToggleResponse, UserNoteRequest,
    AlertCreateRequest, AlertResponse, AlertFeedResponse, AlertFeedItem, SourceResponse,
    WebhookDispatchLogResponse, WebhookDispatchLogsListResponse, HealthMetricsResponse,
    SyncOpportunitiesResponse, AIChatRequest, AISummaryRequest
)
from server.webhook_dispatcher import retry_webhook_dispatch, dispatch_webhook_sync, dispatch_webhook_async
from server.security import (
    hash_password, verify_password, create_access_token, decode_access_token,
    check_rate_limit, calculate_sha256,
    password_fingerprint, generate_reset_token, hash_reset_token, is_rate_limited,
    tentativas_recentes, registrar_tentativa, limpar_tentativas
)
from server.search_engine import (
    search_tenders, CATEGORIES, UFS_BRASIL, NATUREZAS_FILTRO, normalizar_natureza, normalizar_uf
)
from server.ai_engine import analyze_tender_document_with_ai
from server.services.ai_service import analyze_tender_summary, ask_tender_question
from server.connectors.registry import SOURCE_CATALOG, list_all_sources, get_connector
from server.connectors.pncp import build_canonical_pncp_url
from server.alerts_engine import run_alert_processing_job
from server.seed_data import seed_database
from server.integrations.opportunity_collector import sync_procurement_opportunities
from server.migrate import run_db_migrations
from server.services.dispute_monitor import dispute_monitor, PregaoItem
from server.services.portal_collector import (
    classificar_natureza_certame, NATUREZAS_OBJETO, NATUREZA_LABELS
)
from server.services.pdf_generator import (
    gerar_peticao_pdf, normalizar_lei, slug_arquivo, PeticaoInvalidaError
)
from server.services.kit_builder import (
    montar_kit_habilitacao_zip, nome_arquivo_kit, KitHabilitacaoError
)
from server.services import document_vault
from server.services import pncp_compras

# Fuso horário oficial para prazos e agenda
try:
    from zoneinfo import ZoneInfo
    SP_TZ = ZoneInfo("America/Sao_Paulo")
except Exception:
    from datetime import timezone
    SP_TZ = timezone(timedelta(hours=-3), name="America/Sao_Paulo")

def to_sp_datetime(dt_val: Union[datetime, date, str, None]) -> Optional[datetime]:
    """
    Converte campos de abertura/encerramento para objetos datetime no fuso horário America/Sao_Paulo.
    Elimina comparações literais de strings.
    """
    if not dt_val:
        return None
    if isinstance(dt_val, str):
        try:
            dt_clean = dt_val.strip()
            if "T" in dt_clean:
                dt = datetime.fromisoformat(dt_clean)
            elif "/" in dt_clean:
                parts = dt_clean.split(" ")
                date_parts = [int(p) for p in parts[0].split("/")]
                if len(date_parts) == 3:
                    d, m, y = date_parts
                    h, mi, s = 0, 0, 0
                    if len(parts) > 1:
                        time_parts = [int(p) for p in parts[1].split(":")]
                        h = time_parts[0]
                        mi = time_parts[1] if len(time_parts) > 1 else 0
                        s = time_parts[2] if len(time_parts) > 2 else 0
                    dt = datetime(y, m, d, h, mi, s)
                else:
                    dt = datetime.fromisoformat(dt_clean)
            else:
                dt = datetime.fromisoformat(dt_clean)
        except Exception:
            return None
    elif isinstance(dt_val, datetime):
        dt = dt_val
    elif isinstance(dt_val, date):
        dt = datetime.combine(dt_val, datetime.min.time())
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SP_TZ)
    else:
        dt = dt.astimezone(SP_TZ)
    return dt


def resolve_document_download_url(doc_url: Optional[str], tender: Optional[Tender] = None) -> str:
    """
    Resolve a URL de download oficial do documento ou anexo.
    1. Se o payload contiver URL direta do ficheiro binário/anexo (.pdf, .zip, /arquivos/, etc.),
       atribui-a prioritariamente ao botão.
    2. Se for URL do portal PNCP ou fallback, garante canonização estrita com build_canonical_pncp_url
       no formato de 3 segmentos: https://pncp.gov.br/app/editais/{cnpj}/{ano}/{sequencial_inteiro}
       eliminando o formato legado {cnpj}-{tipo}-{seq}/{ano}.
    """
    clean_url = (doc_url or "").strip()

    # 1. Prioridade: link direto de binário/arquivo
    is_direct_binary = (
        bool(clean_url) and (
            any(clean_url.lower().endswith(ext) for ext in [".pdf", ".zip", ".doc", ".docx", ".xlsx", ".csv", ".rar"]) or
            any(token in clean_url.lower() for token in ["/arquivos/", "/anexos/", "/download/", "/documentos/download", "tipo_arquivo=binario"])
        )
    )
    if is_direct_binary and not ("/app/editais/" in clean_url):
        return clean_url

    # 2. Se for URL do PNCP ou número de controle, normaliza via build_canonical_pncp_url
    if clean_url:
        if "pncp.gov.br/app/editais/" in clean_url or "-" in clean_url or clean_url.startswith("http"):
            canon = build_canonical_pncp_url(numero_controle_pncp=clean_url)
            if canon and canon != "https://pncp.gov.br/app/editais":
                return canon

    # 3. Fallback no certame (tender)
    if tender:
        for candidate in [
            getattr(tender, "url_pncp", None),
            getattr(tender, "official_notice_url", None),
            getattr(tender, "link_pncp", None),
            getattr(tender, "source_url", None),
            getattr(tender, "external_id", None)
        ]:
            if candidate:
                cand_str = str(candidate).strip()
                if any(cand_str.lower().endswith(ext) for ext in [".pdf", ".zip", ".doc", ".docx"]) and not ("/app/editais/" in cand_str):
                    return cand_str
                canon = build_canonical_pncp_url(numero_controle_pncp=cand_str)
                if canon and canon != "https://pncp.gov.br/app/editais":
                    return canon

    return clean_url

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
# Listas compartilhadas por todos os filtros (busca, home e alertas)
templates.env.globals["UFS_BRASIL"] = UFS_BRASIL
templates.env.globals["NATUREZAS_FILTRO"] = NATUREZAS_FILTRO

# Arquivos Estáticos e PWA
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

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
    # Tokens emitidos antes de uma troca/redefinição de senha deixam de valer
    if user and payload.get("pwv") and payload.get("pwv") != password_fingerprint(user.password_hash):
        return None
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

# Startup Event: Popular sementes oficiais e iniciar DisputeMonitorService
@app.on_event("startup")
async def on_startup():
    run_db_migrations()
    db = next(get_db())
    try:
        seed_database(db)
    finally:
        db.close()

    # Inicializa monitoramento assíncrono multi-pregão
    await dispute_monitor.start()

    # Popula lotes de demonstração se estiver vazio
    if not dispute_monitor.get_active_snapshot():
        dispute_monitor.track_item({
            "id_pregao": "PE 042/2025",
            "numero_item": 1,
            "descricao": "Servidores de Rede e Armazenamento SAN de Alta Densidade",
            "valor_referencia": 180000.0,
            "melhor_lance": 172500.0,
            "meu_lance": 172500.0,
            "fase": "ABERTO"
        })
        dispute_monitor.track_item({
            "id_pregao": "PE 018/2025",
            "numero_item": 3,
            "descricao": "Sistema de Ultrassonografia Digital Portátil com Doppler Colorido",
            "valor_referencia": 85000.0,
            "melhor_lance": 79200.0,
            "meu_lance": 81000.0,
            "fase": "IMINENCIA"
        })
        dispute_monitor.track_item({
            "id_pregao": "PE 009/2025",
            "numero_item": 1,
            "descricao": "Licenciamento de Software Corporativo e Nuvem Governamental",
            "valor_referencia": 340000.0,
            "melhor_lance": 325000.0,
            "meu_lance": 328000.0,
            "fase": "IMINENCIA"
        })

@app.on_event("shutdown")
async def on_shutdown():
    await dispute_monitor.stop()


# ==============================================================================
# ROTAS DE PÁGINAS WEB (HTML)
# ==============================================================================

@app.get("/", response_class=HTMLResponse)
def page_home(
    request: Request,
    natureza: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    natureza_sel = (natureza or "").strip().upper()
    if natureza_sel not in NATUREZAS_OBJETO:
        natureza_sel = ""

    # Janela recente classificada lexicamente (MATERIAL / SERVICO / OUTROS)
    candidatos = db.query(Tender).order_by(Tender.publication_date.desc()).limit(300).all()
    classificados = [
        (t, t.natureza_objeto or classificar_natureza_certame(t.title, t.object_description)) for t in candidatos
    ]
    natureza_counts = {n: 0 for n in NATUREZAS_OBJETO}
    for _, nat in classificados:
        natureza_counts[nat] += 1

    selecionados = [(t, nat) for t, nat in classificados if not natureza_sel or nat == natureza_sel][:6]
    return templates.TemplateResponse(request, "index.html", {
        "recent_tenders": [t for t, _ in selecionados],
        "naturezas_por_id": {t.id: nat for t, nat in selecionados},
        "natureza_labels": NATUREZA_LABELS,
        "natureza_counts": natureza_counts,
        "natureza_sel": natureza_sel
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
    natureza: Optional[str] = None,
    uf: Optional[str] = None,
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
        state=normalizar_uf(state) or normalizar_uf(uf),
        uf=normalizar_uf(uf) or normalizar_uf(state),
        city=city,
        region=region,
        contracting_entity=contracting_entity,
        administrative_sphere=administrative_sphere,
        modality=modality,
        status=status_filter,
        category=category,
        natureza=normalizar_natureza(natureza) or normalizar_natureza(category),
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
    docs = pncp_compras._docs_do_banco(db, tender.id)
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
        natureza_objeto=tender.natureza_objeto,
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
        link_pncp=(
            build_canonical_pncp_url(numero_controle_pncp=getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url or tender.external_id)
            if (getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url or tender.external_id)
            else (getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url)
        ),
        url_disputa=getattr(tender, "url_disputa", None) or tender.link_comprasnet or tender.official_notice_url or (
            build_canonical_pncp_url(numero_controle_pncp=getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url or tender.external_id)
            if (getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url or tender.external_id)
            else (getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url)
        ),
        url_pncp=(
            build_canonical_pncp_url(numero_controle_pncp=getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url or tender.external_id)
            if (getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url or tender.external_id)
            else (getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url)
        ),
        fonte_portal=getattr(tender, "fonte_portal", None) or "PNCP",
        link_edital_oficial=getattr(tender, "link_edital_oficial", None),
        ai_resumo=getattr(tender, "ai_resumo", None),
        ai_pontos_atencao=getattr(tender, "ai_pontos_atencao", None),
        itens_extraidos_json=getattr(tender, "itens_extraidos_json", None),
        data_completeness=tender.data_completeness,
        is_favorite=is_fav,
        created_at=tender.created_at,
        last_synced_at=tender.last_synced_at
    )

    detail = TenderDetailResponse(
        tender=summary,
        items=[
            TenderItemResponse(**pncp_compras.serializar_item(i)) for i in items
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
                extraction_status=d.extraction_status or "PENDING",
                origem=d.origem or "LOCAL",
                download_url=pncp_compras.serializar_documento(d)["download_url"]
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
    return templates.TemplateResponse(request, "login.html", {"remember_days": settings.REMEMBER_ME_DAYS})

@app.get("/register", response_class=HTMLResponse)
def page_register(request: Request):
    return templates.TemplateResponse(request, "register.html", {})

@app.get("/recuperar-senha", response_class=HTMLResponse)
@app.get("/esqueci-senha", response_class=HTMLResponse)
def page_recuperar_senha(request: Request):
    return templates.TemplateResponse(request, "recuperar_senha.html", {
        "minutos_validade": settings.PASSWORD_RESET_TOKEN_MINUTES
    })

@app.get("/redefinir-senha", response_class=HTMLResponse)
def page_redefinir_senha(request: Request, token: str = Query(""), db: Session = Depends(get_db)):
    token_valido = _buscar_token_redefinicao(db, token) is not None
    response = templates.TemplateResponse(request, "redefinir_senha.html", {
        "token": token if token_valido else "",
        "token_valido": token_valido
    })
    # O token está na URL: evita vazamento via cabeçalho Referer e cache
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response

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
    orgao: Optional[str] = Query(None),
    numero_certame: Optional[str] = Query(None),
    objeto: Optional[str] = Query(None),
    tender_id: Optional[str] = Query(None),
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
            "counts": counts,
            "certame_prefill": {
                "orgao": (orgao or "").strip()[:500],
                "numero_certame": (numero_certame or "").strip()[:255],
                "objeto": (objeto or "").strip()[:1000],
                "tender_id": (tender_id or "").strip()[:64]
            }
        }
    )

@app.get("/documentos", response_class=HTMLResponse)
def page_documentos(request: Request, db: Session = Depends(get_db)):
    hoje = hoje_sao_paulo()
    docs = db.query(DocumentoEmpresa).order_by(
        DocumentoEmpresa.data_validade.is_(None),
        DocumentoEmpresa.data_validade.asc(),
        DocumentoEmpresa.nome.asc()
    ).all()
    serializados = [document_vault.serializar_documento(d, hoje) for d in docs]
    return templates.TemplateResponse(
        request,
        "documentos.html",
        {
            "documentos": serializados,
            "resumo": document_vault.resumo_validade(serializados),
            "categorias": DOCUMENTO_CATEGORIAS,
            "dias_alerta": DOCUMENTO_DIAS_ALERTA,
            "hoje": hoje.isoformat(),
            "max_mb": document_vault.DOCUMENTO_MAX_BYTES // (1024 * 1024)
        }
    )

@app.get("/agenda", response_class=HTMLResponse)
def page_agenda(
    request: Request,
    db: Session = Depends(get_db)
):
    now = datetime.now(SP_TZ)
    deals = db.query(Deal).all()

    events_raw = []
    seen_tenders = set()

    for d in deals:
        t = d.tender
        if not t or t.id in seen_tenders:
            continue
        seen_tenders.add(t.id)
        raw_target = t.opening_date or t.proposal_deadline
        target_date = to_sp_datetime(raw_target)
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
            raw_target = t.opening_date or t.proposal_deadline
            target_date = to_sp_datetime(raw_target)
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
        hours_remaining = diff_seconds / 3600.0
        days_remaining = (td.date() - now.date()).days

        if diff_seconds < 0:
            urgency = "vencido"
            badge_text = "Encerrado / Julgamento"
        elif 0 <= hours_remaining <= 24:
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
            "hours_remaining": hours_remaining,
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

    calendar_events.sort(key=lambda x: x["date"])
    upcoming_events.sort(key=lambda x: x["date"])
    critical_events.sort(key=lambda x: x["date"])
    warning_events.sort(key=lambda x: x["date"])

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

@app.get("/disputa", response_class=HTMLResponse)
def page_disputa(request: Request):
    return templates.TemplateResponse(request, "disputa.html", {})

@app.get("/manifest.json")
def pwa_manifest():
    manifest_path = os.path.join(os.path.dirname(__file__), "static", "manifest.json")
    if os.path.exists(manifest_path):
        return FileResponse(manifest_path, media_type="application/manifest+json")
    raise HTTPException(status_code=404, detail="Manifest não encontrado.")

@app.get("/sw.js")
def pwa_service_worker():
    sw_path = os.path.join(os.path.dirname(__file__), "static", "sw.js")
    if os.path.exists(sw_path):
        return FileResponse(
            sw_path,
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/"}
        )
    raise HTTPException(status_code=404, detail="Service worker não encontrado.")

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
    now = datetime.now(SP_TZ)
    deals = db.query(Deal).all()
    events = []
    seen = set()

    for d in deals:
        t = d.tender
        if t and (t.opening_date or t.proposal_deadline):
            seen.add(t.id)
            td = to_sp_datetime(t.opening_date or t.proposal_deadline)
            if not td:
                continue
            diff_seconds = (td - now).total_seconds()
            hours_remaining = diff_seconds / 3600.0
            days_remaining = (td.date() - now.date()).days
            if diff_seconds < 0:
                urgency = "vencido"
            elif 0 <= hours_remaining <= 24:
                urgency = "critico"
            elif days_remaining <= 7:
                urgency = "alerta"
            else:
                urgency = "normal"
            events.append({
                "deal_id": d.id,
                "tender_id": t.id,
                "title": t.title,
                "notice_number": t.notice_number or t.external_id,
                "contracting_entity": t.contracting_entity,
                "date": td.isoformat(),
                "formatted_date": td.strftime("%d/%m/%Y %H:%M"),
                "urgency": urgency,
                "hours_remaining": hours_remaining,
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
            td = to_sp_datetime(t.opening_date or t.proposal_deadline)
            if not td:
                continue
            diff_seconds = (td - now).total_seconds()
            hours_remaining = diff_seconds / 3600.0
            days_remaining = (td.date() - now.date()).days
            if diff_seconds < 0:
                urgency = "vencido"
            elif 0 <= hours_remaining <= 24:
                urgency = "critico"
            elif days_remaining <= 7:
                urgency = "alerta"
            else:
                urgency = "normal"
            events.append({
                "deal_id": None,
                "tender_id": t.id,
                "title": t.title,
                "notice_number": t.notice_number or t.external_id,
                "contracting_entity": t.contracting_entity,
                "date": td.isoformat(),
                "formatted_date": td.strftime("%d/%m/%Y %H:%M"),
                "urgency": urgency,
                "hours_remaining": hours_remaining,
                "days_remaining": days_remaining,
                "stage": "oportunidades"
            })

    return {"status": "success", "events": events, "total": len(events)}


# ==============================================================================
# DISPUTAS MULTI-PREGÃO E TRANSMISSÃO EM TEMPO REAL (SSE)
# ==============================================================================

@app.post("/api/disputes/track")
async def api_track_dispute_item(payload: Dict[str, Any] = Body(...)):
    """Registra ou atualiza um certame/lote na matriz de monitoramento multi-pregão."""
    try:
        item = dispute_monitor.track_item(payload)
        return {
            "status": "success",
            "item": item.to_dict()
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erro ao registrar item na disputa: {str(e)}")


@app.get("/api/disputes/active")
def api_get_active_disputes():
    """Consulta snapshot consolidado de todos os lotes e pregões em acompanhamento."""
    items = dispute_monitor.get_active_snapshot()
    return {
        "status": "success",
        "total": len(items),
        "items": items
    }


@app.post("/api/disputes/pulse")
def api_dispute_simulate_pulse(item_key: Optional[str] = Query(None)):
    """Método de contingência/mock para certames de teste que gera atualizações e iminência."""
    updated = dispute_monitor.simulate_pulse(item_key)
    return {
        "status": "success",
        "total_updated": len(updated),
        "items": updated
    }


@app.post("/api/disputes/configure/{item_id:path}")
async def api_disputes_configure_item(item_id: str, payload: Dict[str, Any] = Body(...)):
    """Persiste parâmetros individuais (degrau_minimo, valor_minimo_limite, posicao_alvo, auto_lance_ativo)."""
    degrau = payload.get("degrau_minimo")
    piso = payload.get("valor_minimo_limite")
    pos = payload.get("posicao_alvo")
    auto = payload.get("auto_lance_ativo")

    item = dispute_monitor.configure_item(
        item_id=item_id,
        degrau_minimo=degrau,
        valor_minimo_limite=piso,
        posicao_alvo=pos,
        auto_lance_ativo=auto
    )
    if not item:
        raise HTTPException(status_code=404, detail="Item de disputa não encontrado.")
    return {
        "status": "success",
        "item": item.to_dict()
    }


@app.post("/api/disputes/toggle-global")
async def api_disputes_toggle_global(payload: Dict[str, Any] = Body(...)):
    """Liga/desliga o interruptor mestre global de automação de lances."""
    status_val = bool(payload.get("status", False))
    items = dispute_monitor.toggle_all_autobid(status_val)
    return {
        "status": "success",
        "global_status": status_val,
        "total_updated": len(items),
        "items": items
    }


@app.post("/api/disputes/bid/{item_id:path}")
async def api_disputes_bid_action(item_id: str, payload: Dict[str, Any] = Body(default={})):
    """Cobre lance manual ou dispara execução imediata do robô para o lote."""
    valor = payload.get("valor")
    item = dispute_monitor._find_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item de disputa não encontrado.")
    if valor is not None:
        item.meu_lance = round(float(valor), 2)
        if item.meu_lance <= item.melhor_lance:
            item.melhor_lance = item.meu_lance
        item.updated_at = to_sp_datetime(datetime.now()) or datetime.now()
        event = {"type": "MANUAL_BID_PLACED", "item": item.to_dict(), "timestamp": datetime.now().isoformat()}
        dispute_monitor.broadcast_sync(event)
        return {"status": "success", "item": item.to_dict()}
    else:
        res = dispute_monitor.executar_lance_automatico(item_id)
        return {"status": "success", "item": res or item.to_dict()}


@app.get("/api/disputes/events")
async def api_disputes_sse_events(request: Request):
    """
    Endpoint Server-Sent Events (SSE) transmitindo deltas de variação
    de melhor lance e status de iminência em tempo real.
    """
    queue = dispute_monitor.subscribe()

    async def event_generator():
        try:
            # Envia evento de confirmação de conexão e snapshot inicial logo na abertura da conexão SSE
            yield f"data: {json.dumps({'type': 'CONNECTED', 'timestamp': datetime.now().isoformat()})}\n\n"
            initial_snapshot = dispute_monitor.get_active_snapshot()
            for it in initial_snapshot:
                payload = json.dumps({"type": "ITEM_TRACKED", "item": it})
                yield f"data: {payload}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            dispute_monitor.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


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

def _perfil_usuario(user: User) -> UserProfileResponse:
    return UserProfileResponse(
        id=user.id,
        name=user.name,
        email=user.email,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
        last_login_at=user.last_login_at
    )


def _emitir_sessao(user: User, remember_me: bool = False) -> TokenResponse:
    """Emite o JWT da sessão: curta por padrão, estendida com "Manter conectado"."""
    if remember_me:
        validade = timedelta(days=settings.REMEMBER_ME_DAYS)
    else:
        validade = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    token = create_access_token(
        {
            "sub": user.id,
            "email": user.email,
            "role": user.role,
            "pwv": password_fingerprint(user.password_hash),
            "rmb": bool(remember_me)
        },
        expires_delta=validade
    )
    return TokenResponse(
        access_token=token,
        user=_perfil_usuario(user),
        expires_in=int(validade.total_seconds()),
        remember_me=bool(remember_me)
    )


def _ip_cliente(request: Optional[Request]) -> str:
    return request.client.host if request and request.client else "127.0.0.1"


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
    return _emitir_sessao(user, remember_me=False)

@app.post("/api/auth/login", response_model=TokenResponse)
def api_login(payload: UserLoginRequest, request: Request, db: Session = Depends(get_db)):
    email = payload.email.lower().strip()
    chave_falhas = f"login-falhas:{email}"
    # Proteção contra força bruta: bloqueia após 10 senhas erradas para o mesmo e-mail em 15 minutos
    if tentativas_recentes(chave_falhas, window_seconds=900) >= 10:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Muitas tentativas de acesso. Aguarde alguns minutos ou redefina sua senha."
        )

    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        registrar_tentativa(chave_falhas)
        # Defesa contra timing attack e enumeração
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciais de acesso incorretas.")
    limpar_tentativas(chave_falhas)

    if user.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Conta inativa.")

    user.last_login_at = datetime.utcnow()
    db.commit()
    return _emitir_sessao(user, remember_me=payload.remember_me)


MENSAGEM_RECUPERACAO = (
    "Se o e-mail estiver cadastrado, você receberá em instantes um link para redefinir a senha. "
    "Verifique também a caixa de spam."
)


@app.post("/api/auth/forgot-password")
def api_forgot_password(payload: PasswordResetRequest, request: Request, db: Session = Depends(get_db)):
    """
    Solicita a redefinição de senha. A resposta é sempre a mesma (evita enumeração de contas).
    O link expira em PASSWORD_RESET_TOKEN_MINUTES e é de uso único.
    """
    from server.services.email_service import enviar_email, montar_email_redefinicao

    ip = _ip_cliente(request)
    if is_rate_limited(f"forgot:ip:{ip}", max_requests=5, window_seconds=900):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Muitas solicitações. Aguarde alguns minutos para tentar novamente."
        )

    email = payload.email.lower().strip()
    resposta: Dict[str, Any] = {"status": "success", "message": MENSAGEM_RECUPERACAO}

    # Limite silencioso por e-mail: não revela se a conta existe
    if is_rate_limited(f"forgot:email:{email}", max_requests=3, window_seconds=3600):
        return resposta

    user = db.query(User).filter(User.email == email).first()
    if not user or user.status != "ACTIVE":
        return resposta

    agora = datetime.utcnow()
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id,
        PasswordResetToken.used_at.is_(None)
    ).update({PasswordResetToken.used_at: agora}, synchronize_session=False)

    token = generate_reset_token()
    db.add(PasswordResetToken(
        user_id=user.id,
        token_hash=hash_reset_token(token),
        expires_at=agora + timedelta(minutes=settings.PASSWORD_RESET_TOKEN_MINUTES),
        request_ip=ip[:64],
        created_at=agora
    ))
    db.commit()

    link = f"{settings.APP_URL}/redefinir-senha?token={token}"
    assunto, texto, html_email = montar_email_redefinicao(user.name, link, settings.PASSWORD_RESET_TOKEN_MINUTES)
    envio = enviar_email(user.email, assunto, texto, html_email)

    if envio.provedor == "MOCK" and settings.AUTH_EXPOSE_RESET_LINK:
        resposta["reset_url"] = link  # apenas desenvolvimento
    return resposta


def _buscar_token_redefinicao(db: Session, token: str) -> Optional[PasswordResetToken]:
    if not token or len(token) < 20 or len(token) > 200:
        return None
    registro = db.query(PasswordResetToken).filter(
        PasswordResetToken.token_hash == hash_reset_token(token)
    ).first()
    if not registro or not registro.is_valido():
        return None
    return registro


@app.get("/api/auth/reset-password/validate")
def api_validate_reset_token(token: str = Query(""), db: Session = Depends(get_db)):
    registro = _buscar_token_redefinicao(db, token)
    if not registro:
        return {"valid": False}
    restante = int((registro.expires_at - datetime.utcnow()).total_seconds())
    return {"valid": True, "expires_in": max(0, restante)}


@app.post("/api/auth/reset-password")
def api_reset_password(payload: PasswordResetConfirmRequest, db: Session = Depends(get_db)):
    """Confirma a redefinição: troca a senha, consome o token e invalida as sessões anteriores."""
    registro = _buscar_token_redefinicao(db, payload.token)
    if not registro:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Link de redefinição inválido ou expirado. Solicite um novo."
        )
    user = db.query(User).filter(User.id == registro.user_id).first()
    if not user or user.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Conta indisponível para redefinição.")
    if verify_password(payload.new_password, user.password_hash):
        raise HTTPException(status_code=422, detail="A nova senha deve ser diferente da senha atual.")

    agora = datetime.utcnow()
    user.password_hash = hash_password(payload.new_password)
    user.updated_at = agora
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id,
        PasswordResetToken.used_at.is_(None)
    ).update({PasswordResetToken.used_at: agora}, synchronize_session=False)
    db.commit()
    limpar_tentativas(f"login-falhas:{user.email}")
    return {"status": "success", "message": "Senha redefinida com sucesso. Entre com a nova senha."}

@app.get("/api/auth/me", response_model=UserProfileResponse)
def api_me(user: User = Depends(require_auth)):
    return _perfil_usuario(user)

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
    natureza: Optional[str] = None,
    uf: Optional[str] = None,
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
        state=normalizar_uf(state) or normalizar_uf(uf),
        uf=normalizar_uf(uf) or normalizar_uf(state),
        city=city,
        region=region,
        contracting_entity=contracting_entity,
        administrative_sphere=administrative_sphere,
        modality=modality,
        status=status_filter,
        category=category,
        natureza=normalizar_natureza(natureza) or normalizar_natureza(category),
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
    uf: Optional[str] = None,
    modality: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    category: Optional[str] = None,
    natureza: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Exporta oportunidades filtradas em streaming de arquivo CSV estruturado."""
    params = SearchFilterParams(
        q=q,
        state=normalizar_uf(state) or normalizar_uf(uf),
        category=category,
        natureza=normalizar_natureza(natureza) or normalizar_natureza(category),
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

    canon_api_pncp = (
        build_canonical_pncp_url(numero_controle_pncp=getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url or tender.external_id)
        if (getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url or tender.external_id)
        else (getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url)
    )

    docs_payload = [
        {
            "id": d.id,
            "name": d.name,
            "document_type": d.document_type,
            "source_url": resolve_document_download_url(d.source_url, tender),
            "mime_type": d.mime_type or "application/pdf",
            "file_size": d.file_size
        } for d in docs
    ]
    if not docs_payload and (tender.official_notice_url or canon_api_pncp or tender.source_url):
        docs_payload = [{
            "id": f"doc-edital-{tender.id}",
            "name": f"Edital e Termo de Referência ({tender.notice_number or tender.external_id})",
            "document_type": "EDITAL",
            "source_url": resolve_document_download_url(tender.official_notice_url or canon_api_pncp or tender.source_url, tender),
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
        "natureza_objeto": tender.natureza_objeto,
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
        "link_pncp": canon_api_pncp,
        "url_disputa": getattr(tender, "url_disputa", None) or tender.link_comprasnet or tender.official_notice_url or canon_api_pncp,
        "url_pncp": canon_api_pncp,
        "fonte_portal": getattr(tender, "fonte_portal", None) or "PNCP",
        "link_edital_oficial": getattr(tender, "link_edital_oficial", None),
        "ai_resumo": getattr(tender, "ai_resumo", None),
        "ai_pontos_atencao": getattr(tender, "ai_pontos_atencao", None),
        "itens_extraidos_json": getattr(tender, "itens_extraidos_json", None),
        "data_completeness": tender.data_completeness,
        "items": items_payload,
        "documents": docs_payload,
        "created_at": tender.created_at.isoformat() if tender.created_at else None,
        "last_synced_at": tender.last_synced_at.isoformat() if tender.last_synced_at else None
    }


@app.post("/api/tenders/{tender_id}/ai-analyze")
async def api_tender_ai_analyze(
    tender_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    db: Session = Depends(get_db)
):
    """
    Aciona análise semântica e resumo executivo de oportunidade via Google Gemini (2.5 / 2.0 Flash)
    com enriquecimento e persistência unificada.
    """
    from server.services.gemini_analyzer import resumo_executivo_oportunidade, analisar_edital_documento

    tender = db.query(Tender).filter((Tender.id == tender_id) | (Tender.external_id == tender_id)).first()
    if not tender:
        raise HTTPException(status_code=404, detail="Licitação não encontrada para análise por IA.")

    dados_licitacao = {
        "title": tender.title,
        "object_description": tender.object_description,
        "contracting_entity": tender.contracting_entity,
        "state": tender.state,
        "city": tender.city,
        "modality": tender.modality,
        "estimated_total_value": tender.estimated_total_value or 0.0
    }

    ai_summary = await resumo_executivo_oportunidade(dados_licitacao)

    doc_res = None
    target_doc_id = payload.get("document_id") if payload else None
    if target_doc_id:
        doc = db.query(TenderDocument).filter(TenderDocument.id == target_doc_id).first()
        if doc:
            doc_res = await analisar_edital_documento(b"", doc.name)
    if not doc_res:
        docs = db.query(TenderDocument).filter(TenderDocument.tender_id == tender.id).all()
        if docs:
            doc_res = await analisar_edital_documento(b"", docs[0].name)
        else:
            doc_res = await analisar_edital_documento((tender.object_description or "").encode("utf-8", errors="ignore"), "edital.pdf")

    # Enriquecimento de colunas no modelo Tender
    tender.ai_resumo = ai_summary.get("resumo")
    tender.ai_pontos_atencao = json.dumps(ai_summary.get("pontos_atencao", []))
    if doc_res and doc_res.get("itens"):
        tender.itens_extraidos_json = doc_res.get("itens")

    # Estruturação para a UI e retorno
    structured_data = {
        "resumo_executivo": {
            "o_que_sera_contratado": ai_summary.get("resumo") or tender.title,
            "resumo": ai_summary.get("resumo") or tender.title,
            "quem_contrata": tender.contracting_entity,
            "onde_local": f"{tender.city or ''} / {tender.state or ''}",
            "modalidade": tender.modality,
            "valor_estimado": f"R$ {tender.estimated_total_value or 0.0:,.2f}" if tender.estimated_total_value else "Não informado",
            "datas_relevantes": tender.opening_date.strftime("%d/%m/%Y às %H:%M") if tender.opening_date else "A definir em edital",
            "competitividade_estimada": ai_summary.get("competitividade_estimada", "Média")
        },
        "requisitos_participacao": {
            "habilitacao_juridica": doc_res.get("habilitacao_exigencias", []) if doc_res else ["Conforme Edital Oficial (Lei 14.133/2021)"],
            "regularidade_fiscal_trabalhista": ["CND Federal e Trabalhista (CNDT)", "Regularidade FGTS"],
            "qualificacao_tecnica": [ai_summary.get("competitividade_estimada", "Atestado de Capacidade Técnica")],
            "registros_profissionais_ou_sanitarios": "Conforme exigência do órgão licitante"
        },
        "pontos_atencao": {
            "prazos_urgentes_ou_curtos": "Impugnações e esclarecimentos em até 3 dias úteis antes da abertura da sessão.",
            "penalidades_previstas": "\n".join(ai_summary.get("pontos_atencao", [])) or "Conforme Lei 14.133/2021",
            "riscos": ai_summary.get("riscos_juridicos", [])
        },
        "itens_extraidos": doc_res.get("itens", []) if doc_res else [],
        "citacoes": [
            {
                "topico": "Objeto da Contratação",
                "pagina": "Pág. 1",
                "trecho": (tender.object_description or tender.title)[:160]
            }
        ]
    }

    try:
        analysis_record = AIAnalysis(
            tender_id=tender.id,
            model_provider="GOOGLE_GEMINI",
            model_name=ai_summary.get("modelo_utilizado", "gemini-3.8-flash"),
            analysis_type="EXECUTIVE_SUMMARY_AND_RISK",
            result_json=structured_data
        )
        db.add(analysis_record)
    except Exception as e:
        logger.warning(f"Erro ao salvar AIAnalysis: {e}")

    db.commit()
    db.refresh(tender)

    return {
        "success": True,
        "status": "success",
        "tender_id": tender.id,
        "data": structured_data,
        "analysis": ai_summary,
        "doc_analysis": doc_res
    }


@app.post("/api/portals/sync")
async def api_portals_sync(
    payload: Optional[Dict[str, Any]] = Body(None),
    sources: Optional[List[str]] = Query(None),
    uf: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """
    Aciona pipeline assíncrono de coleta multifonte de licitações
    (PNCP, Compras.gov.br, Portal de Compras Públicas, BNC, Licitanet).
    """
    from server.services.portal_collector import portal_collector
    p = payload or {}
    src = sources or p.get("sources")
    target_uf = uf or p.get("uf")
    kw = q or p.get("q") or p.get("keyword")
    ufs_list = [target_uf] if target_uf else None

    result = await portal_collector.sync_pipeline(
        sources=src,
        ufs=ufs_list,
        keywords=kw,
        db_session=db
    )
    result["total_saved"] = result.get("persisted_tenders", 0)
    return result

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

# ----------------- Geração Jurídica em PDF -----------------

class LicitantePDF(BaseModel):
    razao_social: Optional[str] = Field(None, max_length=255)
    cnpj: Optional[str] = Field(None, max_length=32)
    endereco: Optional[str] = Field(None, max_length=500)
    representante: Optional[str] = Field(None, max_length=255)
    cargo: Optional[str] = Field(None, max_length=120)
    cpf: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=255)
    telefone: Optional[str] = Field(None, max_length=40)


class GerarPeticaoPDFRequest(BaseModel):
    template_id: Optional[int] = None
    tender_id: Optional[str] = Field(None, max_length=64)
    titulo: Optional[str] = Field(None, max_length=255)
    corpo: Optional[str] = Field(None, max_length=60000)
    orgao: Optional[str] = Field(None, max_length=500)
    numero_certame: Optional[str] = Field(None, max_length=255)
    numero_edital: Optional[str] = Field(None, max_length=255)
    objeto: Optional[str] = Field(None, max_length=2000)
    lei: Optional[str] = Field(None, max_length=32)
    fundamentacao: Optional[str] = Field(None, max_length=500)
    cidade: Optional[str] = Field(None, max_length=120)
    licitante: Optional[LicitantePDF] = None


@app.post("/api/juridico/gerar-pdf")
def api_juridico_gerar_pdf(payload: GerarPeticaoPDFRequest, db: Session = Depends(get_db)):
    """
    Compila a petição em PDF timbrado Licitações Brasil.
    O texto vem de `corpo` (editado no modal) ou do modelo `template_id`; os dados do
    certame podem ser informados diretamente ou herdados de `tender_id`.
    """
    tmpl = None
    if payload.template_id is not None:
        tmpl = db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.id == payload.template_id).first()
        if not tmpl:
            raise HTTPException(status_code=404, detail="Modelo de peça jurídica não encontrado.")

    corpo = (payload.corpo or "").strip() or (tmpl.corpo if tmpl else "")
    if not corpo.strip():
        raise HTTPException(status_code=422, detail="Informe `template_id` ou o `corpo` da petição.")

    orgao = payload.orgao
    numero = payload.numero_certame or payload.numero_edital
    objeto = payload.objeto
    if payload.tender_id:
        t = db.query(Tender).filter(
            (Tender.id == payload.tender_id) | (Tender.external_id == payload.tender_id)
        ).first()
        if t:
            orgao = orgao or t.contracting_entity
            numero = numero or t.notice_number or t.external_id
            objeto = objeto or t.title

    lei = normalizar_lei(payload.lei) if payload.lei else normalizar_lei(tmpl.is_nova_lei if tmpl else True)
    fundamentacao = payload.fundamentacao or (tmpl.fundamentacao_legal if tmpl else None)
    titulo = payload.titulo or (tmpl.titulo if tmpl else "Petição Administrativa")
    tipo_label = serialize_legal_template(tmpl)["tipo_label"] if tmpl else None

    try:
        pdf_bytes = gerar_peticao_pdf(
            corpo=corpo,
            orgao=orgao,
            numero_certame=numero,
            objeto=objeto,
            titulo=titulo,
            tipo_label=tipo_label,
            lei=lei,
            fundamentacao=fundamentacao,
            licitante=payload.licitante.model_dump() if payload.licitante else None,
            cidade=payload.cidade
        )
    except PeticaoInvalidaError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    filename = f"{slug_arquivo(tmpl.tipo if tmpl else 'peticao', numero or 'certame')}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(pdf_bytes)),
            "Cache-Control": "no-store"
        }
    )

# ----------------- Cofre de Documentos de Habilitação -----------------

@app.get("/api/documentos")
def api_listar_documentos(
    status_filtro: Optional[str] = Query(None, alias="status"),
    categoria: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Lista documentos do cofre com dias restantes e status de validade calculados (fuso America/Sao_Paulo)."""
    hoje = hoje_sao_paulo()
    query = db.query(DocumentoEmpresa)
    if categoria:
        query = query.filter(DocumentoEmpresa.categoria == categoria.lower())
    docs = query.order_by(
        DocumentoEmpresa.data_validade.is_(None),
        DocumentoEmpresa.data_validade.asc(),
        DocumentoEmpresa.nome.asc()
    ).all()
    itens = [document_vault.serializar_documento(d, hoje) for d in docs]
    if status_filtro:
        itens = [i for i in itens if i["status"] == status_filtro.upper()]
    return {
        "hoje": hoje.isoformat(),
        "dias_alerta": DOCUMENTO_DIAS_ALERTA,
        "resumo": document_vault.resumo_validade(itens),
        "items": itens
    }


@app.post("/api/documentos", status_code=status.HTTP_201_CREATED)
async def api_upload_documento(
    nome: str = Form(...),
    categoria: str = Form(...),
    data_emissao: str = Form(...),
    data_validade: Optional[str] = Form(None),
    tags_habilitacao: Optional[str] = Form(None),
    arquivo: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """Recebe um PDF de habilitação, grava em uploads/documentos/ e persiste os metadados no SQLite."""
    try:
        meta = document_vault.validar_metadados(nome, categoria, data_emissao, data_validade)
        conteudo = await arquivo.read(document_vault.DOCUMENTO_MAX_BYTES + 1)
        document_vault.validar_conteudo_pdf(arquivo.filename, conteudo)
    except document_vault.DocumentoInvalidoError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    finally:
        await arquivo.close()

    caminho = document_vault.salvar_pdf_no_cofre(conteudo, meta["nome"])
    doc = DocumentoEmpresa(
        nome=meta["nome"],
        categoria=meta["categoria"],
        data_emissao=meta["data_emissao"],
        data_validade=meta["data_validade"],
        caminho_arquivo=caminho,
        tags_habilitacao=document_vault.normalizar_tags(tags_habilitacao),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    try:
        db.add(doc)
        db.commit()
        db.refresh(doc)
    except Exception:
        db.rollback()
        document_vault.remover_arquivo_documento(caminho)
        raise HTTPException(status_code=500, detail="Falha ao registrar o documento no cofre.")
    return document_vault.serializar_documento(doc)


@app.get("/api/documentos/{documento_id}/arquivo")
def api_baixar_documento(documento_id: int, db: Session = Depends(get_db)):
    doc = db.query(DocumentoEmpresa).filter(DocumentoEmpresa.id == documento_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Documento não encontrado.")
    caminho = document_vault.resolver_caminho_documento(doc.caminho_arquivo)
    if not caminho or not caminho.is_file():
        raise HTTPException(status_code=404, detail="Arquivo físico não localizado no cofre.")
    return FileResponse(
        str(caminho),
        media_type="application/pdf",
        filename=f"{document_vault.slugify(doc.nome, 80)}.pdf"
    )


@app.delete("/api/documentos/{documento_id}")
def api_excluir_documento(documento_id: int, db: Session = Depends(get_db)):
    doc = db.query(DocumentoEmpresa).filter(DocumentoEmpresa.id == documento_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Documento não encontrado.")
    caminho = doc.caminho_arquivo
    db.delete(doc)
    db.commit()
    arquivo_removido = document_vault.remover_arquivo_documento(caminho)
    return {"status": "success", "id": documento_id, "arquivo_removido": arquivo_removido}

# ----------------- Itens e Arquivos do Certame (API de integração do PNCP) -----------------

def _tender_ou_404(db: Session, tender_id: str) -> Tender:
    tender = db.query(Tender).filter((Tender.id == tender_id) | (Tender.external_id == tender_id)).first()
    if not tender:
        raise HTTPException(status_code=404, detail="Licitação não encontrada.")
    return tender


@app.get("/api/tenders/{tender_id}/itens")
async def api_tender_itens(
    tender_id: str,
    atualizar: bool = Query(False, description="Força nova consulta ao endpoint /itens do PNCP"),
    db: Session = Depends(get_db)
):
    """Itens individuais do certame (descrição, quantidade, unidade, valores, critério, benefício ME/EPP)."""
    tender = _tender_ou_404(db, tender_id)
    return await pncp_compras.sincronizar_itens(db, tender, forcar=atualizar)


@app.get("/api/tenders/{tender_id}/arquivos")
async def api_tender_arquivos(
    tender_id: str,
    atualizar: bool = Query(False, description="Força nova consulta ao endpoint /arquivos do PNCP"),
    db: Session = Depends(get_db)
):
    """Lista os anexos oficiais (edital, termo de referência, minutas) com rotas internas de download."""
    tender = _tender_ou_404(db, tender_id)
    return await pncp_compras.sincronizar_arquivos(db, tender, forcar=atualizar)


@app.get("/api/tenders/{tender_id}/arquivos/zip")
async def api_tender_arquivos_zip(tender_id: str, db: Session = Depends(get_db)):
    """Empacota todos os anexos baixáveis do certame em um único ZIP (servido pela própria plataforma)."""
    import zipfile

    tender = _tender_ou_404(db, tender_id)
    docs = [d for d in pncp_compras._docs_do_banco(db, tender.id) if pncp_compras.documento_baixavel(d)]
    if not docs:
        raise HTTPException(status_code=404, detail="Nenhum anexo disponível para download.")
    resultados = await pncp_compras.baixar_varios(db, docs)
    buffer = io.BytesIO()
    nomes_usados = set()
    falhas = []
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, (doc, caminho, erro) in enumerate(resultados, start=1):
            if not caminho:
                falhas.append(f"{doc.name}: {erro}")
                continue
            nome = f"{idx:02d}_{pncp_compras.nome_para_download(doc)}"
            while nome in nomes_usados:
                nome = f"_{nome}"
            nomes_usados.add(nome)
            zf.write(str(caminho), arcname=nome)
        if falhas:
            zf.writestr("LEIA-ME_arquivos_nao_baixados.txt", "\n".join(falhas))
    if not nomes_usados:
        raise HTTPException(status_code=502, detail="Não foi possível baixar os anexos do PNCP agora.")
    nome_zip = f"Anexos_{slug_arquivo(tender.notice_number or tender.external_id or tender.id)}.zip"
    conteudo = buffer.getvalue()
    return StreamingResponse(
        io.BytesIO(conteudo),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{nome_zip}"',
            "Content-Length": str(len(conteudo)),
            "Cache-Control": "no-store"
        }
    )


@app.get("/api/tenders/{tender_id}/arquivos/{documento_id}/download")
async def api_tender_arquivo_download(
    tender_id: str,
    documento_id: str,
    inline: bool = Query(False, description="Exibe no navegador (PDF) em vez de baixar"),
    db: Session = Depends(get_db)
):
    """Baixa o anexo pelo servidor (proxy com cache), sem redirecionar o usuário ao portal de origem."""
    tender = _tender_ou_404(db, tender_id)
    doc = db.query(TenderDocument).filter(
        TenderDocument.id == documento_id, TenderDocument.tender_id == tender.id
    ).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Documento não encontrado neste certame.")
    try:
        caminho = await pncp_compras.baixar_documento(db, doc)
    except pncp_compras.DownloadNaoPermitido as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except pncp_compras.CompraNaoEncontrada:
        raise HTTPException(status_code=404, detail="Arquivo não encontrado no PNCP.")
    except pncp_compras.ArquivoGrandeDemais as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except pncp_compras.PNCPErro as exc:
        raise HTTPException(status_code=502, detail=f"PNCP indisponível no momento: {exc}")

    nome = pncp_compras.nome_para_download(doc)
    exibir = inline and (doc.mime_type or "").startswith(("application/pdf", "image/"))
    response = FileResponse(
        str(caminho),
        media_type=doc.mime_type or "application/octet-stream",
        filename=nome,
        content_disposition_type="inline" if exibir else "attachment"
    )
    response.headers["Cache-Control"] = "private, max-age=3600"
    if doc.checksum:
        response.headers["X-Checksum-SHA256"] = doc.checksum
    return response


# ----------------- Kit de Habilitação (ZIP) -----------------

@app.get("/api/tenders/{tender_id}/download-kit-zip")
def api_download_kit_habilitacao(tender_id: str, db: Session = Depends(get_db)):
    """Empacota os documentos válidos do cofre + Relatorio_Conformidade.pdf para o certame."""
    try:
        zip_bytes = montar_kit_habilitacao_zip(tender_id, db=db)
    except KitHabilitacaoError:
        raise HTTPException(status_code=404, detail="Licitação não encontrada.")
    tender = db.query(Tender).filter((Tender.id == tender_id) | (Tender.external_id == tender_id)).first()
    filename = nome_arquivo_kit(tender)
    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(zip_bytes)),
            "Cache-Control": "no-store"
        }
    )

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

# Legado: Rota unificada implementada em api_tender_ai_analyze (/api/tenders/{tender_id}/ai-analyze)


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


@app.get("/api/alerts/feed", response_model=AlertFeedResponse)
def api_get_alerts_feed(
    limit: int = Query(50, ge=1, le=200),
    user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Retorna o feed de certames correspondentes aos alertas do usuário.
    Se o usuário não estiver autenticado, retorna as entregas recentes globais.
    """
    query = db.query(AlertDelivery, Tender).join(
        Tender, AlertDelivery.tender_id == Tender.id
    )

    if user:
        user_alert_ids = [
            r[0] for r in db.query(SavedSearch.id).filter(
                (SavedSearch.user_id == user.id) | (SavedSearch.user_id.is_(None))
            ).all()
        ]
        user_alert_ids.extend([
            r[0] for r in db.query(Alert.id).filter(Alert.user_id == user.id).all()
        ])
        if user_alert_ids:
            query = query.filter(AlertDelivery.alert_id.in_(user_alert_ids))

    deliveries = query.order_by(AlertDelivery.delivered_at.desc()).limit(limit).all()

    items = []
    for delivery, tender in deliveries:
        saved = db.query(SavedSearch).filter(SavedSearch.id == delivery.alert_id).first()
        al = db.query(Alert).filter(Alert.id == delivery.alert_id).first()
        alert_title = (saved.name if saved and saved.name else None) or (al.name if al else "Alerta")

        items.append(AlertFeedItem(
            id=delivery.id,
            delivery_id=delivery.id,
            alert_id=delivery.alert_id,
            alert_name=alert_title,
            tender_id=tender.id,
            title=tender.title,
            contracting_entity=tender.contracting_entity,
            state=tender.state,
            city=tender.city,
            modality=tender.modality,
            estimated_total_value=tender.estimated_total_value,
            url_disputa=tender.url_disputa or tender.url_pncp,
            url_pncp=tender.url_pncp,
            channel=delivery.channel or "IN_APP",
            delivered_at=delivery.delivered_at,
            tender={
                "id": tender.id,
                "title": tender.title,
                "notice_number": tender.notice_number,
                "contracting_entity": tender.contracting_entity,
                "state": tender.state,
                "city": tender.city,
                "modality": tender.modality,
                "estimated_total_value": tender.estimated_total_value,
                "opening_date": tender.opening_date.isoformat() if tender.opening_date else None,
                "url_disputa": tender.url_disputa or tender.url_pncp,
                "url_pncp": tender.url_pncp
            }
        ))

    return AlertFeedResponse(
        total=len(items),
        count=len(items),
        feed=items,
        items=items
    )


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

        # Indicadores Executivos Licitações Brasil
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
EOF

# ==============================================================================
# TESTES
# ==============================================================================
mkdir -p tests
cat << 'EOF' > tests/test_melhorias_prioritarias.py
"""
Testes das 4 melhorias prioritárias:
  1. Autenticação: mostrar senha, manter conectado e recuperação de senha
  2. Filtros: categoria estritamente MATERIAL / SERVICO / OUTROS e as 27 UFs
  3. Itens individuais do certame via endpoint /itens do PNCP
  4. Listagem e download interno dos anexos (/arquivos) sem redirecionamento externo
"""
import io
import re
import uuid
import zipfile
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from server import security
from server.config import settings
from server.database import SessionLocal
from server.main import app
from server.migrate import run_db_migrations
from server.models import PasswordResetToken, Source, Tender, TenderDocument, TenderItem, User
from server.search_engine import NATUREZAS_FILTRO, UFS_BRASIL
from server.security import decode_access_token, hash_password, hash_reset_token
from server.services import email_service, pncp_compras

client = TestClient(app)

CNPJ = "12265468000197"
PDF_FALSO = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"

# Amostra real do endpoint /itens do PNCP (compra 12265468000197/2026/36)
ITEM_PNCP_REAL = {
    "numeroItem": 1,
    "descricao": "Contratação de empresa para fornecimento de sistema de recorte digital de publicação e intimações",
    "materialOuServico": "M", "materialOuServicoNome": "Material",
    "valorUnitarioEstimado": 3415.2000, "valorTotal": 3415.2000, "quantidade": 1.0000, "unidadeMedida": "UN",
    "orcamentoSigiloso": False, "itemCategoriaId": 3, "itemCategoriaNome": "Não se aplica",
    "criterioJulgamentoId": 1, "criterioJulgamentoNome": "Menor preço",
    "situacaoCompraItem": 1, "situacaoCompraItemNome": "Em andamento",
    "tipoBeneficio": 4, "tipoBeneficioNome": "Sem benefício",
    "dataInclusao": "2026-10-06T20:19:00", "temResultado": False,
    "ncmNbsCodigo": None, "catalogoCodigoItem": None, "informacaoComplementar": None,
}


@pytest.fixture(autouse=True)
def limpar_limites():
    security._rate_limit_records.clear()
    pncp_compras._falhas_recentes.clear()
    yield
    security._rate_limit_records.clear()
    pncp_compras._falhas_recentes.clear()
    pncp_compras._transport_override = None


# ==============================================================================
# 1. AUTENTICAÇÃO
# ==============================================================================

@pytest.fixture
def usuario_temporario():
    email = f"teste.{uuid.uuid4().hex[:10]}@exemplo.com.br"
    senha = "SenhaInicial@123"
    db = SessionLocal()
    try:
        user = User(name="Maria Teste", email=email, password_hash=hash_password(senha), role="USER", status="ACTIVE")
        db.add(user)
        db.commit()
        user_id = user.id
    finally:
        db.close()
    yield {"id": user_id, "email": email, "senha": senha}
    db = SessionLocal()
    try:
        db.query(PasswordResetToken).filter(PasswordResetToken.user_id == user_id).delete()
        db.query(User).filter(User.id == user_id).delete()
        db.commit()
    finally:
        db.close()


def test_login_sessao_padrao_e_manter_conectado(usuario_temporario):
    curto = client.post("/api/auth/login", json={"email": usuario_temporario["email"], "password": usuario_temporario["senha"]})
    assert curto.status_code == 200
    dados = curto.json()
    assert dados["remember_me"] is False
    assert dados["expires_in"] == settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    payload = decode_access_token(dados["access_token"])
    assert payload["pwv"] and payload["rmb"] is False

    longo = client.post("/api/auth/login", json={
        "email": usuario_temporario["email"], "password": usuario_temporario["senha"], "remember_me": True
    }).json()
    assert longo["remember_me"] is True
    assert longo["expires_in"] == settings.REMEMBER_ME_DAYS * 86400
    p = decode_access_token(longo["access_token"])
    assert p["exp"] - p["iat"] >= settings.REMEMBER_ME_DAYS * 86400 - 5


def test_login_bloqueia_apos_10_falhas_e_nao_conta_sucessos(usuario_temporario):
    for _ in range(12):
        assert client.post("/api/auth/login", json={"email": usuario_temporario["email"], "password": usuario_temporario["senha"]}).status_code == 200
    for _ in range(10):
        r = client.post("/api/auth/login", json={"email": usuario_temporario["email"], "password": "errada-123"})
        assert r.status_code == 401
    bloqueado = client.post("/api/auth/login", json={"email": usuario_temporario["email"], "password": usuario_temporario["senha"]})
    assert bloqueado.status_code == 429


def test_paginas_de_login_com_olho_manter_conectado_e_recuperacao():
    login = client.get("/login").text
    assert 'data-toggle-password="password-input"' in login
    assert 'id="remember-input"' in login and "Manter conectado" in login
    assert 'href="/recuperar-senha"' in login
    assert 'autocomplete="current-password"' in login

    registro = client.get("/register").text
    assert 'data-toggle-password="password-input"' in registro
    assert 'autocomplete="new-password"' in registro

    recuperar = client.get("/recuperar-senha")
    assert recuperar.status_code == 200 and "/api/auth/forgot-password" in recuperar.text
    assert client.get("/esqueci-senha").status_code == 200


def test_fluxo_completo_de_recuperacao_de_senha(usuario_temporario):
    email_service.caixa_mock.mensagens.clear()
    sessao_antiga = client.post("/api/auth/login", json={
        "email": usuario_temporario["email"], "password": usuario_temporario["senha"]
    }).json()["access_token"]

    resp = client.post("/api/auth/forgot-password", json={"email": usuario_temporario["email"]})
    assert resp.status_code == 200
    assert "reset_url" not in resp.json()  # nunca exposto por padrão

    mensagem = email_service.caixa_mock.mensagens[-1]
    assert mensagem["to"] == usuario_temporario["email"]
    link = re.search(r"https?://\S+/redefinir-senha\?token=([\w-]+)", mensagem["text"])
    assert link, "o e-mail deve conter o link de redefinição"
    token = link.group(1)

    db = SessionLocal()
    try:
        registro = db.query(PasswordResetToken).filter(PasswordResetToken.user_id == usuario_temporario["id"]).first()
        assert registro.token_hash == hash_reset_token(token)
        assert token not in registro.token_hash  # token em claro nunca é persistido
    finally:
        db.close()

    assert client.get(f"/api/auth/reset-password/validate?token={token}").json()["valid"] is True
    pagina = client.get(f"/redefinir-senha?token={token}")
    assert 'id="form-redefinir"' in pagina.text
    assert pagina.headers["referrer-policy"] == "no-referrer"

    mesma = client.post("/api/auth/reset-password", json={"token": token, "new_password": usuario_temporario["senha"]})
    assert mesma.status_code == 422

    ok = client.post("/api/auth/reset-password", json={"token": token, "new_password": "NovaSenha#2026"})
    assert ok.status_code == 200

    assert client.post("/api/auth/reset-password", json={"token": token, "new_password": "OutraSenha#2026"}).status_code == 400
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {sessao_antiga}"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": usuario_temporario["email"], "password": usuario_temporario["senha"]}).status_code == 401
    assert client.post("/api/auth/login", json={"email": usuario_temporario["email"], "password": "NovaSenha#2026"}).status_code == 200


def test_recuperacao_resposta_uniforme_e_token_expirado(usuario_temporario, monkeypatch):
    inexistente = client.post("/api/auth/forgot-password", json={"email": "ninguem.cadastrado@exemplo.com"})
    existente = client.post("/api/auth/forgot-password", json={"email": usuario_temporario["email"]})
    assert inexistente.status_code == existente.status_code == 200
    assert inexistente.json()["message"] == existente.json()["message"]

    monkeypatch.setattr(settings, "AUTH_EXPOSE_RESET_LINK", True)
    security._rate_limit_records.clear()
    dev = client.post("/api/auth/forgot-password", json={"email": usuario_temporario["email"]}).json()
    token = dev["reset_url"].split("token=")[1]

    db = SessionLocal()
    try:
        ativos = db.query(PasswordResetToken).filter(
            PasswordResetToken.user_id == usuario_temporario["id"], PasswordResetToken.used_at.is_(None)
        ).all()
        assert len(ativos) == 1, "um novo pedido invalida os links anteriores"
        ativos[0].expires_at = datetime.utcnow() - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()

    assert client.post("/api/auth/reset-password", json={"token": token, "new_password": "Qualquer#2026"}).status_code == 400
    assert 'id="token-invalido"' in client.get(f"/redefinir-senha?token={token}").text


def test_recuperacao_limita_pedidos_por_ip():
    codigos = [client.post("/api/auth/forgot-password", json={"email": f"x{i}@exemplo.com"}).status_code for i in range(6)]
    assert codigos[:5] == [200] * 5
    assert codigos[5] == 429


# ==============================================================================
# 2. FILTROS: MATERIAL / SERVIÇO / OUTROS E AS 27 UFs
# ==============================================================================

def test_lista_oficial_tem_27_ufs_unicas():
    siglas = [s for s, _ in UFS_BRASIL]
    assert len(siglas) == 27 and len(set(siglas)) == 27
    assert {"DF", "TO", "AP", "RR", "AC"} <= set(siglas)
    assert [c for c, _ in NATUREZAS_FILTRO] == ["MATERIAL", "SERVICO", "OUTROS"]


@pytest.mark.parametrize("rota", ["/", "/search", "/alerts"])
def test_paginas_exibem_as_27_ufs(rota):
    html = client.get(rota).text
    siglas = set(re.findall(r'<option value="([A-Z]{2})"', html))
    assert {s for s, _ in UFS_BRASIL} <= siglas


def test_select_de_categoria_tem_somente_tres_opcoes():
    html = client.get("/search").text
    bloco = re.search(r'<select name="category" id="categoria-select".*?</select>', html, re.S).group(0)
    valores = re.findall(r'<option value="([^"]*)"', bloco)
    assert valores == ["", "MATERIAL", "SERVICO", "OUTROS"]

    home = client.get("/").text
    bloco_home = re.search(r'<select name="category" id="hero-categoria-select".*?</select>', home, re.S).group(0)
    assert re.findall(r'<option value="([^"]*)"', bloco_home) == ["", "MATERIAL", "SERVICO", "OUTROS"]


@pytest.mark.parametrize("natureza", ["MATERIAL", "SERVICO", "OUTROS"])
def test_api_filtra_por_natureza_e_uf(natureza):
    dados = client.get(f"/api/tenders?category={natureza.lower()}&uf=sp&page_size=100").json()
    assert dados["total"] >= 0
    for item in dados["results"]:
        assert item["natureza_objeto"] == natureza
        assert item["state"] == "SP"
    alias = client.get(f"/api/tenders?natureza={natureza}&page_size=5").json()
    assert all(r["natureza_objeto"] == natureza for r in alias["results"])


def test_pagina_de_busca_filtra_e_mantem_filtros_na_paginacao():
    html = client.get("/search?uf=AL&category=MATERIAL&page_size=1").text
    naturezas = set(re.findall(r'class="natureza-badge[^"]*"[^>]*data-natureza="(\w+)"', html.replace("\n", " ")))
    assert naturezas <= {"MATERIAL"}
    assert re.search(r'<option value="AL" selected>', html)
    proxima = re.search(r'href="\?([^"]*)&page=2"', html)
    if proxima:
        assert "uf=AL" in proxima.group(1) and "category=MATERIAL" in proxima.group(1)


def test_categoria_legada_continua_funcionando():
    dados = client.get("/api/tenders?category=Medicamentos&state=AL").json()
    assert dados["total"] >= 1
    assert all("Medicamentos" in r["category"] for r in dados["results"])


def _source_id(db):
    src = db.query(Source).first()
    assert src is not None
    return src.id


def test_natureza_classificada_automaticamente_ao_gravar_e_na_migracao():
    db = SessionLocal()
    try:
        t = Tender(
            source_id=_source_id(db), external_id=f"TESTE-NAT-{uuid.uuid4().hex[:8]}", title="Aquisição de notebooks",
            object_description="Compra de notebooks", contracting_entity="Órgão Teste", state="DF",
            source_url="https://pncp.gov.br/app/editais",
        )
        db.add(t)
        db.commit()
        assert t.natureza_objeto == "MATERIAL"

        t.title = "Prestação de serviços de vigilância patrimonial"
        t.object_description = "Serviços contínuos de vigilância"
        db.commit()
        assert t.natureza_objeto == "SERVICO"

        db.execute(Tender.__table__.update().where(Tender.id == t.id).values(natureza_objeto=None))
        db.commit()
        run_db_migrations()
        db.expire_all()
        assert db.query(Tender).get(t.id).natureza_objeto == "SERVICO"
    finally:
        db.query(Tender).filter(Tender.external_id.like("TESTE-NAT-%")).delete(synchronize_session=False)
        db.commit()
        db.close()


# ==============================================================================
# 3 e 4. ITENS (/itens) E ARQUIVOS (/arquivos) DO PNCP
# ==============================================================================

@pytest.fixture
def certame_pncp(tmp_path, monkeypatch):
    """Certame isolado com identificador PNCP real e cache de downloads em diretório temporário."""
    monkeypatch.setattr(settings, "STORAGE_LOCAL_PATH", str(tmp_path / "storage"))
    sequencial = str(900000 + uuid.uuid4().int % 99999)
    db = SessionLocal()
    try:
        t = Tender(
            source_id=_source_id(db), external_id=f"{CNPJ}-1-{sequencial.zfill(6)}/2026",
            title="Aquisição de sistema de recorte digital", object_description="Teste PNCP",
            contracting_entity="Município de Teste", state="AL", source_url="https://pncp.gov.br/app/editais",
            notice_number=f"DISP {sequencial}/2026",
        )
        db.add(t)
        db.commit()
        db.add(TenderItem(tender_id=t.id, item_number=999, description="Item local obsoleto", origem="LOCAL"))
        db.commit()
        dados = {"id": t.id, "sequencial": sequencial}
    finally:
        db.close()
    yield dados
    db = SessionLocal()
    try:
        db.query(Tender).filter(Tender.id == dados["id"]).delete()
        db.commit()
    finally:
        db.close()


def _instalar_pncp_falso(sequencial, *, total_itens=150, redirecionar_para="https://pncp.gov.br/pncp-api/v1/storage/edital.pdf",
                         status_itens=200, corpo_arquivo=PDF_FALSO):
    chamadas = []
    base_arquivo = f"https://pncp.gov.br/pncp-api/v1/orgaos/{CNPJ}/compras/2026/{sequencial}/arquivos"
    itens = [dict(ITEM_PNCP_REAL, numeroItem=i, descricao=f"Item {i} - {ITEM_PNCP_REAL['descricao']}",
                  tipoBeneficioNome=("Participação exclusiva para ME/EPP" if i % 2 else "Sem benefício"))
             for i in range(1, total_itens + 1)]

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append(str(request.url))
        caminho = request.url.path
        if caminho.endswith(f"/compras/2026/{sequencial}/itens"):
            if status_itens != 200:
                return httpx.Response(status_itens)
            pagina = int(request.url.params["pagina"])
            tamanho = int(request.url.params["tamanhoPagina"])
            return httpx.Response(200, json=itens[(pagina - 1) * tamanho: pagina * tamanho])
        if caminho.endswith(f"/compras/2026/{sequencial}/arquivos"):
            return httpx.Response(200, json=[
                {"url": f"{base_arquivo}/1", "uri": f"{base_arquivo}/1", "statusAtivo": True, "sequencialDocumento": 1,
                 "titulo": "EDITAL_DE_DISPENSA__SISTEMA", "tipoDocumentoNome": "Aviso de Contratação Direta",
                 "dataPublicacaoPncp": "2026-10-06T20:19:00"},
                {"url": f"{base_arquivo}/2", "statusAtivo": True, "sequencialDocumento": 2,
                 "titulo": "Termo de Referência", "tipoDocumentoNome": "Termo de Referência"},
                {"url": "https://servidor-externo.exemplo/arquivo.pdf", "statusAtivo": True, "sequencialDocumento": 3, "titulo": "Fora do PNCP"},
                {"url": f"{base_arquivo}/4", "statusAtivo": False, "sequencialDocumento": 4, "titulo": "Removido"},
            ])
        if caminho.endswith("/arquivos/1"):
            return httpx.Response(302, headers={"location": redirecionar_para})
        if caminho.endswith("/storage/edital.pdf"):
            return httpx.Response(200, content=corpo_arquivo, headers={
                "content-type": "application/octet-stream",
                "content-disposition": 'attachment; filename="EDITAL.pdf"'})
        if caminho.endswith("/arquivos/2"):
            return httpx.Response(200, content=b"PK\x03\x04conteudo-zip", headers={"content-type": "application/zip"})
        return httpx.Response(404)

    pncp_compras._transport_override = httpx.MockTransport(handler)
    return chamadas


def test_identificar_compra_e_parse_de_item_real():
    t = Tender(external_id=f"{CNPJ}-1-000036/2026", source_url="x")
    ident = pncp_compras.identificar_compra(t)
    assert (ident.cnpj, ident.ano, ident.sequencial) == (CNPJ, "2026", "36")
    assert ident.caminho == f"orgaos/{CNPJ}/compras/2026/36"

    item = pncp_compras.parse_item_pncp(ITEM_PNCP_REAL)
    assert item["item_number"] == 1
    assert item["quantity"] == 1.0 and item["estimated_unit_value"] == 3415.2
    assert item["unit"] == "UN" and item["material_ou_servico"] == "M"
    assert item["criterio_julgamento"] == "Menor preço" and item["situacao"] == "Em andamento"
    assert pncp_compras.parse_item_pncp({"descricao": "sem número"}) is None
    assert pncp_compras.identificar_compra(Tender(external_id="sem-identificador", source_url="x")) is None


def test_sincroniza_itens_com_paginacao_e_cache(certame_pncp):
    chamadas = _instalar_pncp_falso(certame_pncp["sequencial"], total_itens=150)
    resp = client.get(f"/api/tenders/{certame_pncp['id']}/itens")
    assert resp.status_code == 200
    dados = resp.json()
    assert dados["status"] == "sincronizado" and dados["origem"] == "PNCP"
    assert dados["total"] == 150
    assert [i["item_number"] for i in dados["itens"]][:3] == [1, 2, 3]
    assert all(i["origem"] == "PNCP" for i in dados["itens"])
    assert 999 not in [i["item_number"] for i in dados["itens"]], "item local obsoleto deve ser removido"
    assert dados["valor_total_estimado"] == pytest.approx(150 * 3415.2)
    assert sum("/itens" in c for c in chamadas) == 2  # 100 + 50

    antes = len(chamadas)
    assert client.get(f"/api/tenders/{certame_pncp['id']}/itens").json()["status"] == "cache"
    assert len(chamadas) == antes
    assert client.get(f"/api/tenders/{certame_pncp['id']}/itens?atualizar=true").json()["status"] == "sincronizado"

    pagina = client.get(f"/tenders/{certame_pncp['id']}").text
    assert "Participação exclusiva para ME/EPP" in pagina
    assert "Atualizar do PNCP" in pagina


def test_itens_compra_inexistente_ou_pncp_fora_do_ar(certame_pncp):
    _instalar_pncp_falso(certame_pncp["sequencial"], status_itens=404)
    dados = client.get(f"/api/tenders/{certame_pncp['id']}/itens").json()
    assert dados["status"] == "nao_encontrado"
    assert [i["item_number"] for i in dados["itens"]] == [999]  # base local preservada

    pncp_compras._falhas_recentes.clear()

    def fora_do_ar(request):
        raise httpx.ConnectTimeout("timeout", request=request)

    pncp_compras._transport_override = httpx.MockTransport(fora_do_ar)
    dados = client.get(f"/api/tenders/{certame_pncp['id']}/itens").json()
    assert dados["status"] == "indisponivel" and "PNCP" in dados["mensagem"]
    # cache negativo: segunda chamada não consulta o PNCP de novo
    pncp_compras._transport_override = httpx.MockTransport(lambda r: pytest.fail("não deveria consultar o PNCP"))
    assert client.get(f"/api/tenders/{certame_pncp['id']}/itens").json()["status"] == "indisponivel"


def test_lista_arquivos_somente_do_pncp_e_ativos(certame_pncp):
    _instalar_pncp_falso(certame_pncp["sequencial"])
    dados = client.get(f"/api/tenders/{certame_pncp['id']}/arquivos").json()
    assert dados["status"] == "sincronizado"
    nomes = [a["name"] for a in dados["arquivos"]]
    assert nomes == ["EDITAL DE DISPENSA SISTEMA", "Termo de Referência"]
    for arq in dados["arquivos"]:
        assert arq["baixavel"] is True
        assert arq["download_url"].startswith(f"/api/tenders/{certame_pncp['id']}/arquivos/")
        assert "pncp.gov.br" not in arq["download_url"]
    assert dados["zip_url"] == f"/api/tenders/{certame_pncp['id']}/arquivos/zip"


def test_download_interno_com_cache_inline_e_zip(certame_pncp):
    chamadas = _instalar_pncp_falso(certame_pncp["sequencial"])
    arquivos = client.get(f"/api/tenders/{certame_pncp['id']}/arquivos").json()["arquivos"]
    edital = arquivos[0]

    resp = client.get(edital["download_url"])
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"].startswith("attachment")
    assert ".pdf" in resp.headers["content-disposition"]
    assert resp.content == PDF_FALSO
    assert len(resp.headers["x-checksum-sha256"]) == 64

    antes = len(chamadas)
    inline = client.get(edital["visualizar_url"])
    assert inline.headers["content-disposition"].startswith("inline")
    assert len(chamadas) == antes, "segundo acesso deve vir do cache local"

    listagem = client.get(f"/api/tenders/{certame_pncp['id']}/arquivos").json()["arquivos"]
    assert listagem[0]["mime_type"] == "application/pdf" and listagem[0]["file_size"] == len(PDF_FALSO)

    pacote = client.get(f"/api/tenders/{certame_pncp['id']}/arquivos/zip")
    assert pacote.status_code == 200 and pacote.headers["content-type"] == "application/zip"
    nomes = zipfile.ZipFile(io.BytesIO(pacote.content)).namelist()
    assert any(n.endswith(".pdf") for n in nomes) and any(n.endswith(".zip") for n in nomes)


def test_download_bloqueia_redirecionamento_externo_e_arquivo_grande(certame_pncp, monkeypatch):
    _instalar_pncp_falso(certame_pncp["sequencial"], redirecionar_para="https://servidor-malicioso.exemplo/edital.pdf")
    edital = client.get(f"/api/tenders/{certame_pncp['id']}/arquivos").json()["arquivos"][0]
    bloqueado = client.get(edital["download_url"])
    assert bloqueado.status_code == 404
    assert "não autorizado" in bloqueado.json()["detail"]

    monkeypatch.setattr(settings, "PNCP_DOWNLOAD_MAX_MB", 0)
    _instalar_pncp_falso(certame_pncp["sequencial"], corpo_arquivo=PDF_FALSO * 10)
    grande = client.get(edital["download_url"])
    assert grande.status_code == 413


def test_documento_de_outro_certame_ou_sem_arquivo_nao_e_baixado(certame_pncp):
    db = SessionLocal()
    try:
        doc = TenderDocument(tender_id=certame_pncp["id"], name="Página do edital", source_url="https://pncp.gov.br/app/editais/x")
        db.add(doc)
        db.commit()
        doc_id = doc.id
    finally:
        db.close()
    assert client.get(f"/api/tenders/{certame_pncp['id']}/arquivos/{doc_id}/download").status_code == 404
    assert client.get(f"/api/tenders/tnd-sp-ti-002/arquivos/{doc_id}/download").status_code == 404


def test_detalhe_do_certame_nao_redireciona_para_portal_externo(certame_pncp):
    _instalar_pncp_falso(certame_pncp["sequencial"])
    client.get(f"/api/tenders/{certame_pncp['id']}/arquivos")
    html = client.get(f"/tenders/{certame_pncp['id']}").text
    assert "Baixar Oficial" not in html
    assert f"/api/tenders/{certame_pncp['id']}/arquivos/" in html
    assert "baixarArquivoInterno" in html
    aba = html.split('id="tab-content-documents"', 1)[1].split('id="tab-content-ai"', 1)[0]
    assert 'target="_blank" rel="noopener noreferrer"' not in aba
EOF

echo "Melhorias aplicadas: 20 arquivos."
