import ipaddress
import os
import socket
import tempfile

# ------------------------------------------------------------------------------
# Ambiente de teste determinístico.
# Precisa rodar ANTES de importar `server.*`: server/config.py lê o .env apenas
# para chaves ausentes de os.environ, então os valores abaixo prevalecem.
# ------------------------------------------------------------------------------
_TEST_DB_DIR = tempfile.mkdtemp(prefix="licitacoes_tests_")
# Banco SQLite novo a cada sessão — nunca o DATABASE_URL do .env (que pode ser produção)
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{os.path.join(_TEST_DB_DIR, 'test.db')}"
# Sem chaves reais: serviços de IA usam o fallback local, a menos que o teste faça mock
os.environ["GEMINI_API_KEY"] = ""
os.environ["AI_API_KEY"] = ""
os.environ["EMAIL_PROVIDER"] = "MOCK"
# Conector Licitações-e ativo em modo mock (não depende do .env da máquina)
os.environ["LICITACOES_E_ACTIVE"] = "true"
os.environ["LICITACOES_E_MOCK_MODE"] = "true"
os.environ.setdefault("LICITACOES_E_MOCK_TOKEN", "mock_bearer_token_local_dev")

import pytest  # noqa: E402

from server.database import engine, Base, SessionLocal  # noqa: E402
from server.seed_data import seed_database  # noqa: E402


# ------------------------------------------------------------------------------
# Bloqueio de rede externa: PNCP, Gemini, webhooks etc. caem nos caminhos de
# fallback/mocks. Apenas loopback é permitido (event loop, servidores locais).
# Use RUN_NETWORK_TESTS=1 para liberar chamadas reais.
# ------------------------------------------------------------------------------
_LOCAL_HOSTS = {"localhost", "testserver", "", None}


def _host_local(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode(errors="ignore")
    if host in _LOCAL_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(str(host).split("%")[0])
        return ip.is_loopback or ip.is_unspecified
    except ValueError:
        return False


class RedeExternaBloqueada(OSError):
    pass


@pytest.fixture(scope="session", autouse=True)
def bloquear_rede_externa():
    if os.environ.get("RUN_NETWORK_TESTS") == "1":
        yield
        return

    getaddrinfo_original = socket.getaddrinfo
    connect_original = socket.socket.connect
    connect_ex_original = socket.socket.connect_ex

    def getaddrinfo_guard(host, *args, **kwargs):
        if not _host_local(host):
            raise RedeExternaBloqueada(f"Rede externa bloqueada nos testes: {host}")
        return getaddrinfo_original(host, *args, **kwargs)

    def _checar(endereco):
        if isinstance(endereco, tuple) and endereco and not _host_local(endereco[0]):
            raise RedeExternaBloqueada(f"Rede externa bloqueada nos testes: {endereco[0]}")

    def connect_guard(self, endereco):
        _checar(endereco)
        return connect_original(self, endereco)

    def connect_ex_guard(self, endereco):
        _checar(endereco)
        return connect_ex_original(self, endereco)

    socket.getaddrinfo = getaddrinfo_guard
    socket.socket.connect = connect_guard
    socket.socket.connect_ex = connect_ex_guard
    try:
        yield
    finally:
        socket.getaddrinfo = getaddrinfo_original
        socket.socket.connect = connect_original
        socket.socket.connect_ex = connect_ex_original


@pytest.fixture(scope="session", autouse=True)
def setup_test_database(bloquear_rede_externa):
    """Cria todas as tabelas e popula dados iniciais para a suíte de testes"""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_database(db)
    finally:
        db.close()
    yield
    engine.dispose()
