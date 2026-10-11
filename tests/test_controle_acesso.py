"""Controle de acesso: menu Gestão só para ADMIN, vitrine com paywall, derrubada de sessão, prazos flexíveis."""
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from server.config import settings
from server.database import SessionLocal
from server.main import SESSION_COOKIE, SP_TZ, app
from server.models import MENSAGEM_ACESSO_SUSPENSO, Tender, User, hoje_sao_paulo
from server.search_engine import agora_sao_paulo_naive

client = TestClient(app)
SENHA = "SenhaTeste@123"
MENSAGEM_VITRINE = "Inicie sessão ou crie a sua conta para aceder aos detalhes e oportunidades"


def _get(url, conta=None, **kwargs):
    return client.get(url, cookies=conta.cookies if conta else None, follow_redirects=False, **kwargs)


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
    return criar_usuario("Admin Gestão", role="ADMIN")


@pytest.fixture
def cliente(criar_usuario):
    return criar_usuario("Cliente Comum")


@pytest.fixture
def paywall(monkeypatch):
    monkeypatch.setattr(settings, "ACESSO_EXIGE_LOGIN", True)


# ---------------- a) Menu Gestão e rotas administrativas ----------------
def test_menu_gestao_so_aparece_para_admin(admin, cliente):
    html_admin = _get("/dashboard", admin).text
    assert 'id="nav-gestao"' in html_admin and 'id="nav-gestao-mobile"' in html_admin

    for conta in (cliente, None):
        html = _get("/dashboard", conta).text
        assert 'id="nav-gestao"' not in html and 'id="nav-gestao-mobile"' not in html
        assert 'href="/admin"' not in html  # nenhuma pista do painel de gestão


def test_rotas_administrativas_bloqueadas_para_usuario_comum(admin, cliente):
    for rota in ("/admin", "/gestao"):
        assert _get(rota, admin).status_code == 200
        resp = _get(rota, cliente)
        assert resp.status_code == 303 and resp.headers["location"] == "/dashboard"
        anon = _get(rota)
        assert anon.status_code == 303 and anon.headers["location"].startswith("/login?")
    assert client.get("/api/admin/users", headers=cliente.headers).status_code == 403
    assert client.get("/api/admin/metrics", headers=cliente.headers).status_code == 403


# ---------------- b) Vitrine pública e paywall ----------------
def test_vitrine_publica_e_rotas_do_sistema_exigem_login(paywall, cliente):
    home = _get("/")
    assert home.status_code == 200 and 'data-visitante="1"' in home.text
    assert "motivo: 'vitrine'" in home.text  # interações do visitante levam ao login
    assert 'data-visitante="0"' in _get("/", cliente).text  # conta logada navega normalmente

    for rota in ("/search?q=obras", "/dashboard", "/negocios", "/alerts", "/agenda", "/favorites",
                 "/juridico", "/disputa", "/fornecedores", "/documentos", "/tenders/qualquer"):
        resp = _get(rota)
        assert resp.status_code == 303, rota
        destino = urlparse(resp.headers["location"])
        assert destino.path == "/login"
        params = parse_qs(destino.query)
        assert params["motivo"] == ["login"] and params["next"] == [rota]
        assert _get(rota, cliente).status_code in (200, 404), rota

    api = client.get("/api/tenders")
    assert api.status_code == 401 and api.json()["detail"] == MENSAGEM_VITRINE
    assert client.get("/api/tenders", headers=cliente.headers).status_code == 200

    # Públicos: login, cadastro, recuperação de senha, health check e autenticação
    for rota in ("/login", "/register", "/recuperar-senha", "/healthz"):
        assert _get(rota).status_code == 200, rota
    assert client.post("/api/auth/login", json={"email": cliente.email, "password": SENHA}).status_code == 200


def test_login_exibe_convite_da_vitrine(paywall):
    html = _get("/login?motivo=vitrine&next=/search").text
    assert MENSAGEM_VITRINE in html and 'data-encerrar-sessao="0"' in html


# ---------------- c) Derrubada imediata de sessão ----------------
def test_suspensao_derruba_sessao_na_proxima_requisicao(admin, cliente):
    assert _get("/dashboard", cliente).status_code == 200
    client.post(f"/api/admin/users/{cliente.id}/suspender", headers=admin.headers)

    # Página: cookie apagado e redirecionamento para o login com o motivo
    resp = _get("/dashboard", cliente)
    assert resp.status_code == 303
    assert parse_qs(urlparse(resp.headers["location"]).query)["motivo"] == ["suspenso"]
    cookie = resp.headers.get("set-cookie", "")
    assert cookie.startswith(f'{SESSION_COOKIE}=""') or f"{SESSION_COOKIE}=;" in cookie
    assert "max-age=0" in cookie.lower() or "expires=" in cookie.lower()

    # API (token Bearer já emitido): 403 com o sinal para o navegador deslogar
    api = client.get("/api/favorites", headers=cliente.headers)
    assert api.status_code == 403 and api.headers["X-Sessao-Encerrada"] == "suspenso"
    assert api.json()["detail"] == MENSAGEM_ACESSO_SUSPENSO

    # Tela de login mostra a mensagem e descarta a sessão guardada no navegador
    login_html = _get("/login?motivo=suspenso").text
    assert MENSAGEM_ACESSO_SUSPENSO in login_html and 'data-encerrar-sessao="1"' in login_html

    # Novas tentativas de login bloqueadas até a reativação
    tentativa = client.post("/api/auth/login", json={"email": cliente.email, "password": SENHA})
    assert tentativa.status_code == 403 and tentativa.json()["detail"] == MENSAGEM_ACESSO_SUSPENSO

    _ajustar(cliente.id, subscription_status="CANCELED")
    assert client.post("/api/auth/login", json={"email": cliente.email, "password": SENHA}).status_code == 403

    client.post(f"/api/admin/users/{cliente.id}/reativar", headers=admin.headers)
    assert client.post("/api/auth/login", json={"email": cliente.email, "password": SENHA}).status_code == 200
    assert _get("/dashboard", cliente).status_code == 200


def test_prazo_vencido_derruba_sessao_com_motivo_expirado(cliente):
    _ajustar(cliente.id, access_expires_at=datetime.utcnow() - timedelta(minutes=1))
    resp = _get("/negocios", cliente)
    assert parse_qs(urlparse(resp.headers["location"]).query)["motivo"] == ["expirado"]


# ---------------- d) Prazos flexíveis ----------------
@pytest.mark.parametrize("dias", [3, 7, 15, 30, 365])
def test_extensoes_de_prazo(admin, cliente, dias):
    prazo = datetime.utcnow() + timedelta(days=2)
    _ajustar(cliente.id, access_expires_at=prazo)
    resp = client.post(f"/api/admin/users/{cliente.id}/estender?dias={dias}", headers=admin.headers)
    assert resp.status_code == 200
    assert abs((_ler(cliente.id).access_expires_at - (prazo + timedelta(days=dias))).total_seconds()) < 1
    assert resp.json()["trial_days_left"] == dias + 2


def test_acesso_vitalicio_e_data_personalizada(admin, cliente):
    url = f"/api/admin/users/{cliente.id}/prazo"
    _ajustar(cliente.id, access_expires_at=datetime.utcnow() - timedelta(days=1))  # vencido

    resp = client.post(url, json={"vitalicio": True}, headers=admin.headers).json()
    assert resp["access_expires_at"] is None and resp["trial_days_left"] is None and resp["acesso_liberado"]
    # Vitalício não tem prazo para somar dias
    assert client.post(f"/api/admin/users/{cliente.id}/estender?dias=30", headers=admin.headers).status_code == 409

    data = hoje_sao_paulo() + timedelta(days=20)
    client.post(url, json={"data": data.isoformat()}, headers=admin.headers)
    fim_brasilia = _ler(cliente.id).access_expires_at.replace(tzinfo=timezone.utc).astimezone(SP_TZ)
    assert fim_brasilia.date() == data and (fim_brasilia.hour, fim_brasilia.minute) == (23, 59)

    ontem = (hoje_sao_paulo() - timedelta(days=1)).isoformat()
    assert client.post(url, json={"data": ontem}, headers=admin.headers).status_code == 422
    assert client.post(url, json={}, headers=admin.headers).status_code == 422
    assert client.post(url, json={"vitalicio": True}, headers=cliente.headers).status_code == 403


def test_plano_trial_pelo_admin(admin, cliente):
    resp = client.post(f"/api/admin/users/{cliente.id}/plano", json={"plan_type": "TRIAL"}, headers=admin.headers).json()
    assert resp["plan_type"] == "TRIAL" and resp["trial_days_left"] == settings.TRIAL_DIAS


def test_painel_admin_oferece_todos_os_prazos(admin):
    html = _get("/dashboard", admin).text
    for rotulo in ("+3 Dias", "+7 Dias", "+15 Dias", "+30 Dias (1 Mês)", "+1 Ano", "Acesso Vitalício", "Data personalizada"):
        assert rotulo in html


# ---------------- Vigentes de ciclo longo ----------------
def _tender(db, marcador, *, publicado_ha, prazo_em=None, abertura_em=None):
    agora = agora_sao_paulo_naive()
    t = Tender(
        source_id="pncp-source-01", external_id=f"{marcador}-{uuid.uuid4().hex[:8]}",
        title=f"Edital {marcador}", object_description="Objeto", contracting_entity="Órgão", state="SP",
        status="Divulgada no PNCP", source_url="https://pncp.gov.br",
        publication_date=agora - timedelta(days=publicado_ha),
        proposal_deadline=agora + timedelta(days=prazo_em) if prazo_em is not None else None,
        opening_date=agora + timedelta(days=abertura_em) if abertura_em is not None else None,
    )
    db.add(t)
    db.commit()
    return t.id


def test_vigentes_por_prazo_futuro_sem_corte_por_publicacao():
    marcador = f"CICLO{uuid.uuid4().hex[:6]}"
    db = SessionLocal()
    try:
        longo_60 = _tender(db, marcador, publicado_ha=60, prazo_em=5)
        longo_45 = _tender(db, marcador, publicado_ha=45, prazo_em=1)
        so_abertura = _tender(db, marcador, publicado_ha=50, abertura_em=3)
        encerrado = _tender(db, marcador, publicado_ha=2, prazo_em=-1)
        sem_datas = _tender(db, marcador, publicado_ha=1)
        ids = {r["id"] for r in client.get(f"/api/tenders?vigentes=true&q={marcador}&page_size=50").json()["results"]}
        assert ids == {longo_60, longo_45, so_abertura}
        assert encerrado not in ids and sem_datas not in ids
    finally:
        db.query(Tender).filter(Tender.external_id.like(f"{marcador}%")).delete(synchronize_session=False)
        db.commit()
        db.close()


# ---------------- Cache das contagens administrativas ----------------
def test_contagens_administrativas_em_cache(admin, monkeypatch):
    from server.cache import limpar_cache

    monkeypatch.setattr(settings, "CACHE_CONTAGENS_SEGUNDOS", 60)
    limpar_cache()
    antes = client.get("/api/admin/metrics", headers=admin.headers).json()["users_count"]
    db = SessionLocal()
    extra = User(name="Conta Extra", email=f"extra-{uuid.uuid4().hex[:6]}@exemplo.com.br", password_hash="x")
    db.add(extra)
    db.commit()
    try:
        assert client.get("/api/admin/metrics", headers=admin.headers).json()["users_count"] == antes  # em cache
        limpar_cache()
        assert client.get("/api/admin/metrics", headers=admin.headers).json()["users_count"] == antes + 1
    finally:
        db.delete(extra)
        db.commit()
        db.close()
        limpar_cache()
