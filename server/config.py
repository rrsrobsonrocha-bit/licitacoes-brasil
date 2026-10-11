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

    # Administrador inicial (opcional). Sem ADMIN_EMAIL nenhuma conta padrão é criada.
    # Com ADMIN_EMAIL de conta existente, ela é promovida a ADMIN; com ADMIN_PASSWORD, a conta é criada.
    ADMIN_EMAIL: str = os.getenv("ADMIN_EMAIL", "")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "")
    ADMIN_NAME: str = os.getenv("ADMIN_NAME", "")
    # Conta que recebe os negócios/alertas legados sem dono (padrão: ADMIN_EMAIL ou a conta real principal)
    ORPHAN_OWNER_EMAIL: str = os.getenv("ORPHAN_OWNER_EMAIL", "")
    # Limpeza automática das contas/certames de demonstração antigos na migração (desligada só na suíte de testes)
    REMOVER_DADOS_DEMONSTRACAO: bool = os.getenv("REMOVER_DADOS_DEMONSTRACAO", "true").lower() in ("1", "true", "yes")

    # PNCP — API de integração (itens e arquivos das compras)
    PNCP_INTEGRACAO_BASE_URL: str = os.getenv("PNCP_INTEGRACAO_BASE_URL", "https://pncp.gov.br/api/pncp/v1").rstrip("/")
    PNCP_DOWNLOAD_MAX_MB: int = int(os.getenv("PNCP_DOWNLOAD_MAX_MB", "100"))

    # Security
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    RATE_LIMIT_REQUESTS_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "120"))
    CORS_ALLOWED_ORIGINS: List[str] = [origin.strip() for origin in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8000").split(",")]

settings = Settings()
