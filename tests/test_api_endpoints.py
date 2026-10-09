import pytest
from fastapi.testclient import TestClient
from server.main import app

client = TestClient(app)

def test_api_health_endpoint():
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "Licitações Brasil"

def test_api_search_endpoint():
    response = client.get("/api/tenders?q=medicamentos&state=AL")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 1
    assert data["results"][0]["state"] == "AL"

def test_api_login_success():
    response = client.post("/api/auth/login", json={
        "email": "usuario@empresa.com.br",
        "password": "UsuarioSenha@123"
    })
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["user"]["email"] == "usuario@empresa.com.br"

def test_api_login_invalid_password():
    response = client.post("/api/auth/login", json={
        "email": "usuario@empresa.com.br",
        "password": "SenhaCompletamenteIncorreta"
    })
    assert response.status_code == 401
    assert "detail" in response.json()

def test_api_sources_list():
    response = client.get("/api/sources")
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 5
    source_names = [s["name"] for s in data]
    assert any("PNCP" in name for name in source_names)
