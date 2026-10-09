import pytest
from server.security import check_rate_limit
from server.database import SessionLocal
from server.models import User
from server.security import hash_password

def test_rate_limiting_sliding_window():
    test_ip = "192.168.1.99"
    # Fazer 5 requisições com limite de 5
    for _ in range(5):
        assert check_rate_limit(test_ip, max_requests=5, window_seconds=10) is True

    # A 6ª requisição deve ser bloqueada
    assert check_rate_limit(test_ip, max_requests=5, window_seconds=10) is False

def test_lgpd_account_deletion_flow():
    db = SessionLocal()
    try:
        # Criar usuário temporário para teste de exclusão (direito ao esquecimento)
        temp_user = User(
            name="Usuario Para Exclusao",
            email="excluir.me@empresa.com.br",
            password_hash=hash_password("TempPassword123!"),
            role="USER"
        )
        db.add(temp_user)
        db.commit()
        db.refresh(temp_user)
        user_id = temp_user.id

        # Simular exclusão de conta
        db.delete(temp_user)
        db.commit()

        # Verificar se não existe mais no banco
        deleted = db.query(User).filter(User.id == user_id).first()
        assert deleted is None
    finally:
        db.close()
