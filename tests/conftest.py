import pytest
from server.database import engine, Base, SessionLocal
from server.seed_data import seed_database

@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    """Cria todas as tabelas e popula dados iniciais para a suíte de testes"""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_database(db)
    finally:
        db.close()
    yield
