"""Gestão administrativa de clientes e assinaturas, regra de acesso e webhook da Hotmart (modo de espera)."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from server.config import settings
from server.database import SessionLocal
from server.main import app
from server.models import User

client = TestClient(app)
SENHA = "SenhaTeste@123"  # senha das contas criadas pela fixture criar_usuario


def _ajustar(user_id: str, **campos) -> None:
    db = SessionLocal()
    try:
        u = db.get(User, user_id)
        for campo, valor in campos.items():
            setattr(u, campo, valor)
        db.commit()
    finally:
        db.close()


def _ler(user_id: str) -> User:
    db = SessionLocal()
    try:
        u = db.get(User, user_id)
        db.expunge(u)
        return u
    finally:
        db.close()


@pytest.fixture
def admin(criar_usuario):
    return criar_usuario("Admin de Teste", role="ADMIN")


@pytest.fixture
def cliente(criar_usuario):
    return criar_usuario("Cliente de Teste")


# ---------------- Autorização ----------------
def test_rotas_de_gestao_sao_exclusivas_do_admin(cliente, admin):
    rotas = [
        ("get", "/api/admin/users"),
        ("post", f"/api/admin/users/{cliente.id}/suspender"),
        ("post", f"/api/admin/users/{cliente.id}/reativar"),
        ("post", f"/api/admin/users/{cliente.id}/estender"),
    ]
    for metodo, rota in rotas:
        assert getattr(client, metodo)(rota).status_code == 401  # visitante
        assert getattr(client, metodo)(rota, headers=cliente.headers).status_code == 403  # usuário comum
    resp = client.post(f"/api/admin/users/{cliente.id}/plano", json={"plan_type": "ANUAL"}, headers=cliente.headers)
    assert resp.status_code == 403

    lista = client.get("/api/admin/users", headers=admin.headers).json()
    linha = next(u for u in lista["usuarios"] if u["id"] == cliente.id)
    for campo in ("name", "email", "plan_type", "subscription_status", "trial_days_left", "acesso_liberado"):
        assert campo in linha
    assert (linha["plan_type"], linha["subscription_status"], linha["trial_days_left"]) == ("GRATUITO", "ACTIVE", None)


# ---------------- Suspender / reativar ----------------
def test_suspender_revoga_sessao_e_login_e_reativar_devolve_acesso(cliente, admin):
    assert client.get("/api/auth/me", headers=cliente.headers).status_code == 200

    resp = client.post(f"/api/admin/users/{cliente.id}/suspender", headers=admin.headers)
    assert resp.status_code == 200 and resp.json()["subscription_status"] == "SUSPENDED"
    assert resp.json()["acesso_liberado"] is False

    # A sessão já emitida deixa de valer e um novo login é recusado com o motivo
    bloqueado = client.get("/api/auth/me", headers=cliente.headers)
    assert bloqueado.status_code == 403 and "suspenso" in bloqueado.json()["detail"]
    login = client.post("/api/auth/login", json={"email": cliente.email, "password": SENHA})
    assert login.status_code == 403 and "suspenso" in login.json()["detail"]
    # Qualquer API devolve 403 com o sinal de sessão encerrada (o navegador desloga na hora)
    api = client.get("/api/user/dashboard-summary", headers=cliente.headers)
    assert api.status_code == 403 and api.headers["X-Sessao-Encerrada"] == "suspenso"

    resp = client.post(f"/api/admin/users/{cliente.id}/reativar", headers=admin.headers)
    assert resp.status_code == 200 and resp.json()["subscription_status"] == "ACTIVE"
    assert client.get("/api/auth/me", headers=cliente.headers).status_code == 200
    assert client.post("/api/auth/login", json={"email": cliente.email, "password": SENHA}).status_code == 200


def test_admin_nao_suspende_a_propria_conta_e_nunca_fica_bloqueado(admin):
    resp = client.post(f"/api/admin/users/{admin.id}/suspender", headers=admin.headers)
    assert resp.status_code == 400
    # Mesmo com assinatura vencida, o administrador mantém o acesso ao painel
    _ajustar(admin.id, subscription_status="CANCELED", access_expires_at=datetime.utcnow() - timedelta(days=1))
    assert client.get("/api/admin/users", headers=admin.headers).status_code == 200


def test_reativar_nao_libera_prazo_vencido_ate_estender(cliente, admin):
    _ajustar(cliente.id, subscription_status="SUSPENDED", access_expires_at=datetime.utcnow() - timedelta(days=2))
    resp = client.post(f"/api/admin/users/{cliente.id}/reativar", headers=admin.headers).json()
    assert resp["subscription_status"] == "ACTIVE" and resp["acesso_liberado"] is False
    assert "terminou" in resp["motivo_bloqueio"]

    resp = client.post(f"/api/admin/users/{cliente.id}/estender", headers=admin.headers).json()
    assert resp["acesso_liberado"] is True and resp["trial_days_left"] == 3


# ---------------- Extensão de cortesia (+3 dias) ----------------
def test_mais_3_dias_soma_ao_prazo_vigente(cliente, admin):
    prazo = datetime.utcnow() + timedelta(days=5)
    _ajustar(cliente.id, subscription_status="TRIAL", access_expires_at=prazo)

    resp = client.post(f"/api/admin/users/{cliente.id}/estender", headers=admin.headers)
    assert resp.status_code == 200
    novo = _ler(cliente.id).access_expires_at
    assert abs((novo - (prazo + timedelta(days=3))).total_seconds()) < 1
    assert resp.json()["trial_days_left"] == 8

    # Outra quantidade de dias, dentro do limite
    client.post(f"/api/admin/users/{cliente.id}/estender?dias=10", headers=admin.headers)
    assert abs((_ler(cliente.id).access_expires_at - (prazo + timedelta(days=13))).total_seconds()) < 1
    assert client.post(f"/api/admin/users/{cliente.id}/estender?dias=0", headers=admin.headers).status_code == 422


def test_mais_3_dias_em_prazo_vencido_conta_a_partir_de_agora(cliente, admin):
    _ajustar(cliente.id, subscription_status="TRIAL", access_expires_at=datetime.utcnow() - timedelta(days=10))
    assert client.post("/api/auth/login", json={"email": cliente.email, "password": SENHA}).status_code == 403

    antes = datetime.utcnow()
    client.post(f"/api/admin/users/{cliente.id}/estender", headers=admin.headers)
    novo = _ler(cliente.id).access_expires_at
    assert antes + timedelta(days=3) <= novo <= datetime.utcnow() + timedelta(days=3)
    assert client.post("/api/auth/login", json={"email": cliente.email, "password": SENHA}).status_code == 200


def test_mais_3_dias_nao_cria_prazo_em_conta_sem_expiracao(cliente, admin):
    resp = client.post(f"/api/admin/users/{cliente.id}/estender", headers=admin.headers)
    assert resp.status_code == 409
    assert _ler(cliente.id).access_expires_at is None  # continua com acesso sem prazo


def test_estender_usuario_inexistente(admin):
    assert client.post("/api/admin/users/nao-existe/estender", headers=admin.headers).status_code == 404


# ---------------- Troca manual de plano ----------------
def test_alteracao_manual_de_plano(cliente, admin):
    url = f"/api/admin/users/{cliente.id}/plano"
    resp = client.post(url, json={"plan_type": "mensal"}, headers=admin.headers).json()
    assert (resp["plan_type"], resp["subscription_status"], resp["trial_days_left"]) == ("MENSAL", "ACTIVE", 30)

    resp = client.post(url, json={"plan_type": "ANUAL"}, headers=admin.headers).json()
    assert resp["plan_type"] == "ANUAL" and resp["trial_days_left"] == 30 + 365  # soma ao prazo vigente

    resp = client.post(url, json={"plan_type": "GRATUITO"}, headers=admin.headers).json()
    assert resp["plan_type"] == "GRATUITO" and resp["trial_days_left"] is None

    assert client.post(url, json={"plan_type": "VITALICIO"}, headers=admin.headers).status_code == 422


# ---------------- Contas novas ----------------
def _registrar(email: str):
    return client.post("/api/auth/register", json={"name": "Conta Nova", "email": email, "password": "SenhaNova@2026"})


def test_conta_nova_recebe_7_dias_de_degustacao(monkeypatch):
    import uuid
    db = SessionLocal()
    emails = [f"nova-{uuid.uuid4().hex[:8]}@exemplo.com.br" for _ in range(2)]
    try:
        antes = datetime.utcnow()
        perfil = _registrar(emails[0]).json()["user"]
        assert (perfil["plan_type"], perfil["subscription_status"], perfil["trial_days_left"]) == ("TRIAL", "ACTIVE", 7)
        u = db.query(User).filter(User.email == emails[0]).one()
        assert antes + timedelta(days=7) <= u.access_expires_at <= datetime.utcnow() + timedelta(days=7)

        # TRIAL_DIAS=0: conta nasce no plano gratuito, sem data de expiração
        monkeypatch.setattr(settings, "TRIAL_DIAS", 0)
        assert _registrar(emails[1]).status_code == 200
        u = db.query(User).filter(User.email == emails[1]).one()
        assert (u.plan_type, u.subscription_status, u.access_expires_at) == ("GRATUITO", "ACTIVE", None)
    finally:
        db.query(User).filter(User.email.in_(emails)).delete(synchronize_session=False)
        db.commit()
        db.close()


# ---------------- Painel ----------------
def test_secao_de_gestao_aparece_somente_para_admin(cliente, admin):
    html_admin = client.get("/gestao", cookies=admin.cookies).text
    assert 'id="admin-assinaturas"' in html_admin and "Gestão de Clientes &amp; Assinaturas" in html_admin
    for rotulo in ("Nome", "E-mail", "Plano atual", "Status", "Dias restantes", "Suspender", "Reativar", "+3 Dias"):
        assert rotulo in html_admin
    # O painel (dashboard) não traz mais a gestão de clientes, nem para o ADMIN
    for cookies in (admin.cookies, cliente.cookies, None):
        html = client.get("/dashboard", cookies=cookies).text
        assert 'id="admin-assinaturas"' not in html and "carregarClientes" not in html
    assert client.get("/gestao", cookies=cliente.cookies, follow_redirects=False).status_code == 303


# ---------------- Webhook Hotmart (modo de espera) ----------------
COMPRA = {
    "event": "PURCHASE_APPROVED",
    "data": {
        "buyer": {"email": "Comprador@Exemplo.com.br"},
        "subscription": {"subscriber": {"code": "ABC123"}, "plan": {"name": "Plano Anual"}},
    },
}


def test_webhook_hotmart_sem_configuracao_aceita_sem_efeito(monkeypatch, cliente):
    monkeypatch.setattr(settings, "HOTMART_HOTTOK", "")
    evento = dict(COMPRA, data=dict(COMPRA["data"], buyer={"email": cliente.email}))
    resp = client.post("/api/webhooks/hotmart", json=evento)
    assert resp.status_code == 200
    assert resp.json() == {"status": "recebido", "modo": "espera", "evento": "PURCHASE_APPROVED",
                           "acao_prevista": "ativar", "aplicado": False}
    conta = _ler(cliente.id)
    assert (conta.plan_type, conta.hotmart_subscriber_code) == ("GRATUITO", None)  # nada foi alterado

    # Corpo inválido ou evento desconhecido não derrubam o endpoint
    assert client.post("/api/webhooks/hotmart", content=b"nao-e-json").status_code == 200
    assert client.post("/api/webhooks/hotmart", json={"event": "OUTRO"}).json()["acao_prevista"] == "ignorar"


def test_webhook_hotmart_valida_hottok_quando_configurado(monkeypatch):
    monkeypatch.setattr(settings, "HOTMART_HOTTOK", "token-secreto")
    assert client.post("/api/webhooks/hotmart", json=COMPRA).status_code == 401
    assert client.post("/api/webhooks/hotmart", json=COMPRA, headers={"X-HOTMART-HOTTOK": "errado"}).status_code == 401
    ok = client.post("/api/webhooks/hotmart", json=COMPRA, headers={"X-HOTMART-HOTTOK": "token-secreto"})
    assert ok.status_code == 200 and ok.json()["acao_prevista"] == "ativar"
    cancelamento = {"event": "SUBSCRIPTION_CANCELLATION", "hottok": "token-secreto"}  # formato antigo: no corpo
    assert client.post("/api/webhooks/hotmart", json=cancelamento).json()["acao_prevista"] == "cancelar"
