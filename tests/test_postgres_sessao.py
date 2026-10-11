"""Driver PostgreSQL (Supabase) e persistência de sessões."""
import importlib

from sqlalchemy import create_engine, inspect

from server import config
from server.database import argumentos_de_conexao, opcoes_do_pool, preparar_url_banco

SUPABASE = "postgres://postgres.abc:segredo@aws-0-sa-east-1.pooler.supabase.com:6543/postgres"


def test_url_postgres_usa_driver_instalado():
    assert preparar_url_banco(SUPABASE, "psycopg").startswith("postgresql+psycopg://postgres.abc:segredo@")
    assert preparar_url_banco(SUPABASE, "psycopg2").startswith("postgresql+psycopg2://")
    assert preparar_url_banco("postgresql://u:p@h/db", "psycopg").startswith("postgresql+psycopg://")
    # Driver explícito e SQLite ficam como estão
    assert preparar_url_banco("postgresql+psycopg2://u:p@h/db", "psycopg") == "postgresql+psycopg2://u:p@h/db"
    assert preparar_url_banco("sqlite:///./x.db") == "sqlite:///./x.db"


def test_url_remove_parametros_que_nao_sao_do_libpq():
    url = preparar_url_banco(SUPABASE + "?schema=public&pgbouncer=true&sslmode=verify-full", "psycopg")
    assert "schema=" not in url and "pgbouncer" not in url
    assert url.endswith("?sslmode=verify-full")


def test_conexao_externa_exige_tls_e_dispensa_prepared_statements():
    url = preparar_url_banco(SUPABASE, "psycopg")
    args = argumentos_de_conexao(url)
    assert args["sslmode"] == "require"
    assert args["prepare_threshold"] is None  # transaction pooler (PgBouncer)
    assert opcoes_do_pool(url)["pool_recycle"] > 0

    # sslmode já informado na URL não é sobrescrito; banco local dispensa TLS
    assert "sslmode" not in argumentos_de_conexao(preparar_url_banco(SUPABASE + "?sslmode=disable", "psycopg"))
    assert "sslmode" not in argumentos_de_conexao("postgresql+psycopg2://u:p@localhost:5432/db")
    assert "prepare_threshold" not in argumentos_de_conexao("postgresql+psycopg2://u:p@h.example.com/db")
    assert argumentos_de_conexao("sqlite:///./x.db")["check_same_thread"] is False


def test_engine_postgres_e_criado_com_os_dois_drivers():
    for driver in ("psycopg", "psycopg2"):
        url = preparar_url_banco(SUPABASE, driver)
        motor = create_engine(url, connect_args=argumentos_de_conexao(url), pool_pre_ping=True, **opcoes_do_pool(url))
        assert motor.dialect.name == "postgresql" and motor.dialect.driver == driver
        motor.dispose()


def test_segredo_de_sessao_e_estavel_entre_boots(monkeypatch):
    for nome in ("AUTH_SECRET", "SECRET_KEY"):
        monkeypatch.delenv(nome, raising=False)
    monkeypatch.setenv("DATABASE_URL", SUPABASE)
    primeiro, segundo = config.segredo_de_assinatura(), config.segredo_de_assinatura()
    assert primeiro == segundo and primeiro != config.SEGREDO_DESENVOLVIMENTO
    assert "segredo" not in primeiro  # derivado, nunca a URL em claro

    monkeypatch.setenv("SECRET_KEY", "chave-do-ambiente")
    assert config.segredo_de_assinatura() == "chave-do-ambiente"
    monkeypatch.setenv("AUTH_SECRET", "chave-principal")
    assert config.segredo_de_assinatura() == "chave-principal"


def _login(conta, remember_me):
    from fastapi.testclient import TestClient
    from server.main import app

    return TestClient(app).post("/api/auth/login", json={
        "email": conta.email, "password": "SenhaTeste@123", "remember_me": remember_me,
    })


def test_cookie_de_sessao_dura_30_dias(criar_usuario):
    resp = _login(criar_usuario(), remember_me=False)
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"].lower()
    assert "lb_session=" in cookie and "max-age=2592000" in cookie
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert resp.json()["expires_in"] == 2592000


def test_cookie_seguro_em_producao(criar_usuario, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    resp = _login(criar_usuario(), remember_me=True)
    assert resp.status_code == 200
    assert "secure" in resp.headers["set-cookie"].lower()


def test_seed_do_admin_preserva_senha_e_sessoes(monkeypatch):
    from server.database import SessionLocal
    from server.models import User
    from server.security import hash_password
    from server.seed_data import garantir_admin_inicial

    email = "admin.persistente@exemplo.com.br"
    db = SessionLocal()
    try:
        db.query(User).filter(User.email == email).delete()
        db.add(User(name="Admin", email=email, password_hash=hash_password("SenhaOriginal@1"),
                    role="ADMIN", status="ACTIVE"))
        db.commit()
        antes = db.query(User).filter(User.email == email).one().password_hash

        monkeypatch.setattr(config.settings, "ADMIN_EMAIL", email)
        monkeypatch.setattr(config.settings, "ADMIN_PASSWORD", "OutraSenha@2")
        garantir_admin_inicial(db)
        db.expire_all()
        assert db.query(User).filter(User.email == email).one().password_hash == antes  # tokens "pwv" seguem válidos
    finally:
        db.query(User).filter(User.email == email).delete()
        db.commit()
        db.close()


def test_migracao_cria_o_esquema_completo_em_banco_vazio(tmp_path, monkeypatch):
    import server.database as database
    import server.migrate as migrate

    motor = create_engine(f"sqlite:///{tmp_path / 'vazio.db'}")
    monkeypatch.setattr(migrate, "engine", motor)
    monkeypatch.setattr(database, "engine", motor)
    migrate.run_db_migrations()
    tabelas = set(inspect(motor).get_table_names())
    assert {"tenders", "users", "favorites", "deals", "alerts", "sync_progresso"} <= tabelas
    migrate.run_db_migrations()  # idempotente
    motor.dispose()


def test_chaves_estrangeiras_tem_o_mesmo_tipo_da_coluna_referenciada():
    """No PostgreSQL, FK entre tipos diferentes derruba o CREATE TABLE (ex.: deals.tender_id INTEGER)."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    import server.models  # noqa: F401
    from server.database import Base

    dialeto = postgresql.dialect()
    divergentes = []
    for tabela in Base.metadata.sorted_tables:
        CreateTable(tabela).compile(dialect=dialeto)  # DDL do PostgreSQL compila para todas as tabelas
        for fk in tabela.foreign_keys:
            origem = fk.parent.type.compile(dialect=dialeto)
            destino = fk.column.type.compile(dialect=dialeto)
            if origem != destino:
                divergentes.append(f"{tabela.name}.{fk.parent.name} {origem} -> {fk.target_fullname} {destino}")
    assert divergentes == []


def test_deals_tender_id_e_varchar_como_tenders_id():
    from server.models import Deal, Tender

    assert str(Deal.__table__.c.tender_id.type) == str(Tender.__table__.c.id.type) == "VARCHAR(36)"


def test_sql_bruto_usa_literais_booleanos_do_postgresql():
    """PostgreSQL recusa "coluna_booleana = 1" (DatatypeMismatch); o SQL bruto usa TRUE/FALSE."""
    import re
    from pathlib import Path

    from sqlalchemy import Boolean

    import server.models  # noqa: F401
    from server.database import Base

    booleanas = {c.name for t in Base.metadata.tables.values() for c in t.columns if isinstance(c.type, Boolean)}
    padrao = re.compile(r"\b(" + "|".join(sorted(booleanas)) + r")\s*(=|!=|<>|IS)\s*[01]\b|BOOLEAN\s+DEFAULT\s+[01]\b",
                        re.IGNORECASE)
    raiz = Path(__file__).resolve().parent.parent / "server"
    ocorrencias = [f"{arq.relative_to(raiz)}:{n}: {linha.strip()}"
                   for arq in raiz.rglob("*.py")
                   for n, linha in enumerate(arq.read_text(encoding="utf-8").splitlines(), 1)
                   if padrao.search(linha)]
    assert ocorrencias == []


def test_ddl_das_colunas_novas_usa_tipos_do_postgresql():
    import server.migrate as migrate

    listas = [v for k, v in vars(migrate).items() if k.startswith("COLUNAS_")]
    tipos = {ddl.split()[0].upper() for lista in listas for _, _, ddl in lista}
    assert not tipos & {"DATETIME", "TINYINT", "BLOB"}, tipos
