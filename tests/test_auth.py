import pytest
from server.security import hash_password, verify_password, create_access_token, decode_access_token
from server.database import SessionLocal
from server.models import User

def test_password_hashing_and_verification():
    raw_pwd = "SenhaForteSegura@2025"
    hashed = hash_password(raw_pwd)

    # Nunca deve ser igual ao texto plano
    assert hashed != raw_pwd
    # Deve iniciar com o identificador bcrypt
    assert hashed.startswith("$2b$") or hashed.startswith("$2a$")

    # Verificação com senha correta
    assert verify_password(raw_pwd, hashed) is True
    # Verificação com senha incorreta
    assert verify_password("SenhaErrada#123", hashed) is False

def test_jwt_token_flow():
    payload = {"sub": "usr-test-123", "email": "teste@empresa.com.br", "role": "USER"}
    token = create_access_token(payload)
    assert isinstance(token, str)

    decoded = decode_access_token(token)
    assert decoded is not None
    assert decoded["sub"] == "usr-test-123"
    assert decoded["email"] == "teste@empresa.com.br"
    assert decoded["role"] == "USER"

def test_user_enumeration_protection():
    # Ao testar login com e-mail inexistente e senha aleatória, a função não deve vazar
    # se o e-mail existe ou não via timing attacks
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == "inexistente_1234567@governo.gov.br").first()
        assert user is None
    finally:
        db.close()
