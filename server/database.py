import importlib.util
from typing import Any, Dict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker
from server.config import settings

# Parâmetros de URL que não são do libpq (Prisma/Supabase colam "?schema=public" e "pgbouncer=true");
# o psycopg recusa a conexão com "invalid connection option".
PARAMETROS_NAO_LIBPQ = {"schema", "pgbouncer", "connection_limit", "pool_timeout", "statement_cache_size"}
HOSTS_LOCAIS = {"", "localhost", "127.0.0.1", "::1", "db", "postgres"}


def driver_postgres_disponivel() -> str:
    """Driver PostgreSQL instalado: psycopg (3) de preferência, senão psycopg2."""
    if importlib.util.find_spec("psycopg") is not None:
        return "psycopg"
    return "psycopg2"


def preparar_url_banco(url: str, driver: str = None) -> str:
    """
    Normaliza a DATABASE_URL: "postgres://" (Render/Heroku/Supabase) e "postgresql://" sem driver
    passam a apontar para o driver instalado; parâmetros que não são do libpq são removidos.
    """
    url = (url or "").strip()
    if not url.startswith(("postgres://", "postgresql://", "postgresql+")):
        return url
    driver = driver or driver_postgres_disponivel()
    esquema, _, resto = url.partition("://")
    if esquema in ("postgres", "postgresql"):
        esquema = f"postgresql+{driver}"
    partes = urlsplit(f"{esquema}://{resto}")
    consulta = [(k, v) for k, v in parse_qsl(partes.query, keep_blank_values=True)
                if k.lower() not in PARAMETROS_NAO_LIBPQ]
    return urlunsplit(partes._replace(query=urlencode(consulta)))


def eh_postgres(url: str) -> bool:
    return url.startswith("postgresql")


def argumentos_de_conexao(url: str) -> Dict[str, Any]:
    """connect_args do driver: SQLite multi-thread; PostgreSQL externo com TLS e compatível com pooler."""
    if url.startswith("sqlite"):
        return {"check_same_thread": False, "timeout": 30}
    if not eh_postgres(url):
        return {}
    partes = urlsplit(url)
    parametros = {k.lower() for k, _ in parse_qsl(partes.query)}
    args: Dict[str, Any] = {"connect_timeout": 15}
    if (partes.hostname or "").lower() not in HOSTS_LOCAIS and "sslmode" not in parametros:
        args["sslmode"] = "require"  # Supabase/Render externos exigem TLS
    if url.startswith("postgresql+psycopg://"):
        # Transaction pooler do Supabase (PgBouncer, porta 6543) não suporta prepared statements
        args["prepare_threshold"] = None
    return args


def opcoes_do_pool(url: str) -> Dict[str, Any]:
    if not eh_postgres(url):
        return {}
    # Pool pequeno (o pooler do Supabase limita conexões por projeto) e reciclagem antes do
    # tempo ocioso em que o pooler derruba a conexão.
    return {"pool_size": 5, "max_overflow": 5, "pool_recycle": 280, "pool_timeout": 30}


db_url = preparar_url_banco(settings.DATABASE_URL)

engine = create_engine(
    db_url,
    connect_args=argumentos_de_conexao(db_url),
    pool_pre_ping=True,
    **opcoes_do_pool(db_url),
)

if db_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
