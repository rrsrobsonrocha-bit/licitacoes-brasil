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
