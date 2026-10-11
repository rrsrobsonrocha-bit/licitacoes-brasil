"""Contas novas zeradas, dados privados isolados por usuário, catálogo global de editais vigentes e cards do painel."""
import uuid
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import text

from server.database import SessionLocal, engine
from server.main import SESSION_COOKIE, app
from server.migrate import _remover_deals_de_demonstracao
from server.models import Deal, Tender
from server.search_engine import agora_sao_paulo_naive
from server.seed_data import seed_database

client = TestClient(app)


def _novo_tender(db, *, publicado_ha_dias: int, prazo_em_dias: int, status: str = "Divulgada no PNCP") -> str:
    agora = agora_sao_paulo_naive()
    tender = Tender(
        source_id="pncp-source-01",
        external_id=f"TESTE-VIGENTE-{uuid.uuid4().hex[:12]}",
        title=f"Certame de teste vigente {uuid.uuid4().hex[:6]}",
        object_description="Objeto de teste do catálogo global de editais vigentes.",
        contracting_entity="Órgão de Teste do Catálogo",
        state="SP",
        modality="Pregão Eletrônico",
        status=status,
        publication_date=agora - timedelta(days=publicado_ha_dias),
        proposal_deadline=agora + timedelta(days=prazo_em_dias),
        opening_date=agora + timedelta(days=prazo_em_dias),
        source_url="https://pncp.gov.br/app/editais/teste-vigente",
    )
    db.add(tender)
    db.commit()
    return tender.id


# ---------------- 1. Conta nova começa zerada (sem seed de negócios/agenda) ----------------
def test_conta_nova_cadastrada_comeca_com_funil_agenda_e_alertas_zerados():
    c = TestClient(app)
    email = f"nova-{uuid.uuid4().hex[:8]}@exemplo.com.br"
    resp = c.post("/api/auth/register", json={"name": "Conta Nova", "email": email, "password": "SenhaForte@2026"})
    assert resp.status_code == 200
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    deals = c.get("/api/deals", headers=headers).json()
    assert deals["total"] == 0
    assert c.get("/api/agenda/events", headers=headers).json()["total"] == 0
    assert c.get("/api/alerts", headers=headers).json() == []
    assert c.get("/api/favorites", headers=headers).json() == []
    assert c.get("/api/alerts/feed", headers=headers).json()["total"] == 0

    resumo = c.get("/api/user/dashboard-summary", headers=headers).json()
    assert resumo["active_deals_count"] == 0
    assert resumo["agenda_events_count"] == 0
    assert resumo["agenda_events"] == []
    assert resumo["favorites_count"] == 0

    # Login também não semeia nada
    login = c.post("/api/auth/login", json={"email": email, "password": "SenhaForte@2026"})
    assert login.status_code == 200
    assert c.get("/api/deals", headers=headers).json()["total"] == 0

    c.delete("/api/auth/account", headers=headers)


def test_seed_nao_cria_negocios_e_migracao_remove_demos_antigos():
    db = SessionLocal()
    try:
        antes = db.query(Deal).count()
        seed_database(db)
        assert db.query(Deal).count() == antes

        tender = db.query(Tender).first()
        demo = Deal(tender_id=tender.id, stage="disputa", notes="Certame prioritário acompanhado no estágio disputa.")
        legado_real = Deal(tender_id=tender.id, stage="oportunidades", notes="Negócio legado de verdade")
        db.add_all([demo, legado_real])
        db.commit()
        demo_id, legado_id = demo.id, legado_real.id
    finally:
        db.close()

    _remover_deals_de_demonstracao()

    db = SessionLocal()
    try:
        assert db.query(Deal).filter(Deal.id == demo_id).first() is None
        assert db.query(Deal).filter(Deal.id == legado_id).first() is not None  # só os demos são apagados
        db.query(Deal).filter(Deal.id == legado_id).delete()
        db.commit()
    finally:
        db.close()


def test_visitante_nao_ve_agenda_nem_negocios_genericos():
    assert client.get("/api/agenda/events").json()["total"] == 0
    resumo = client.get("/api/user/dashboard-summary").json()
    assert resumo["active_deals_count"] == 0
    assert resumo["agenda_events"] == []
    assert resumo["favorites_count"] == 0

    html = client.get("/negocios").text
    assert 'id="login-required-banner"' in html
    assert 'id="login-required-banner"' in client.get("/agenda").text


# ---------------- 2. Multitenancy: dados privados isolados ----------------
def test_negocios_isolados_entre_usuarios(criar_usuario):
    a, b = criar_usuario("Empresa A"), criar_usuario("Empresa B")
    db = SessionLocal()
    try:
        tender = db.query(Tender).first()
        tender_id, titulo = tender.id, tender.title
    finally:
        db.close()

    deal_a = client.post("/api/deals", json={"tender_id": tender_id, "stage": "disputa"}, headers=a.headers).json()
    assert client.get("/api/deals", headers=a.headers).json()["total"] == 1
    assert client.get("/api/deals", headers=b.headers).json()["total"] == 0

    # B não move nem apaga o negócio de A
    assert client.patch(f"/api/deals/{deal_a['id']}/stage", json={"stage": "contrato"}, headers=b.headers).status_code == 404
    assert client.delete(f"/api/deals/{deal_a['id']}", headers=b.headers).status_code == 404

    # O mesmo certame pode estar no funil de B, num negócio independente
    deal_b = client.post("/api/deals", json={"tender_id": tender_id}, headers=b.headers).json()
    assert deal_b["id"] != deal_a["id"]
    assert deal_b["stage"] == "oportunidades"
    assert client.get("/api/deals", headers=a.headers).json()["disputa"][0]["id"] == deal_a["id"]

    # Página renderizada no servidor via cookie de sessão
    html_a = client.get("/negocios", cookies=a.cookies).text
    assert f'id="deal-card-{deal_a["id"]}"' in html_a
    assert f'id="deal-card-{deal_b["id"]}"' not in html_a

    # Agenda e painel contam apenas os negócios do próprio usuário
    assert client.get("/api/user/dashboard-summary", headers=a.headers).json()["active_deals_count"] == 1
    assert all(e["deal_id"] == deal_a["id"] for e in client.get("/api/agenda/events", headers=a.headers).json()["events"])
    assert titulo  # sanidade


def test_alertas_isolados_entre_usuarios(criar_usuario):
    a, b = criar_usuario("Empresa A"), criar_usuario("Empresa B")
    alerta = client.post("/api/alerts", json={"keywords": "notebook", "name": "Alerta A"}, headers=a.headers).json()

    assert [x["id"] for x in client.get("/api/alerts", headers=a.headers).json()] == [alerta["id"]]
    assert client.get("/api/alerts", headers=b.headers).json() == []
    assert client.get("/api/alerts").json() == []  # visitante

    assert client.post(f"/api/alerts/{alerta['id']}/toggle?active=false", headers=b.headers).status_code == 404
    assert client.delete(f"/api/alerts/{alerta['id']}", headers=b.headers).status_code == 404
    assert client.delete(f"/api/alerts/{alerta['id']}", headers=a.headers).status_code == 200


def test_cookie_de_sessao_no_login_sync_e_logout(usuario_auth):
    c = TestClient(app)
    sync = c.post("/api/auth/session", headers=usuario_auth.headers)
    assert sync.status_code == 200
    assert SESSION_COOKIE in sync.headers.get("set-cookie", "")
    assert "httponly" in sync.headers.get("set-cookie", "").lower()

    # Sem token nem cookie: 401
    assert TestClient(app).post("/api/auth/session").status_code == 401

    logout = c.post("/api/auth/logout")
    assert logout.status_code == 200
    assert f'{SESSION_COOKIE}=""' in logout.headers.get("set-cookie", "") or "Max-Age=0" in logout.headers.get("set-cookie", "")


# ---------------- 3. Catálogo global de editais vigentes ----------------
def test_conta_nova_acessa_catalogo_global_completo(usuario_auth):
    db = SessionLocal()
    try:
        total = db.query(Tender).count()
    finally:
        db.close()
    anonimo = client.get("/api/tenders?page_size=1").json()["total"]
    logado = client.get("/api/tenders?page_size=1", headers=usuario_auth.headers).json()["total"]
    assert anonimo == logado == total


def test_filtro_vigentes_independe_da_data_de_publicacao():
    db = SessionLocal()
    try:
        ids = {
            "hoje": _novo_tender(db, publicado_ha_dias=0, prazo_em_dias=3),
            "30_dias": _novo_tender(db, publicado_ha_dias=30, prazo_em_dias=5),
            "encerrado": _novo_tender(db, publicado_ha_dias=15, prazo_em_dias=-2),
            "suspenso": _novo_tender(db, publicado_ha_dias=5, prazo_em_dias=5, status="Suspensa"),
        }
    finally:
        db.close()
    try:
        resp = client.get("/api/tenders?vigentes=true&page_size=100&q=vigente&sort_by=deadline_asc").json()
        encontrados = {r["id"] for r in resp["results"]}
        assert ids["hoje"] in encontrados
        assert ids["30_dias"] in encontrados
        assert ids["encerrado"] not in encontrados
        assert ids["suspenso"] not in encontrados

        # Sem o filtro, a busca padrão mostra todo o catálogo (inclusive o publicado há 30 dias)
        todos = {r["id"] for r in client.get("/api/tenders?page_size=100&q=vigente").json()["results"]}
        assert set(ids.values()) <= todos

        html = client.get("/search?vigentes=true").text
        assert 'id="toggle-vigentes"' in html
        assert "Somente vigentes" in html
    finally:
        db = SessionLocal()
        try:
            db.query(Tender).filter(Tender.id.in_(list(ids.values()))).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()


# ---------------- 4. Cards do painel clicáveis ----------------
def test_cards_do_dashboard_sao_links():
    html = client.get("/dashboard").text
    assert 'id="card-active-deals" href="/negocios?view=quadros"' in html
    assert 'id="card-companies" href="/fornecedores"' in html
    assert 'id="card-agenda" href="/agenda"' in html
    assert 'id="card-tenders-today"' in html
    assert 'id="card-vigentes"' in html and "/search?vigentes=true" in html
    assert html.count("metric-card group cursor-pointer") == 4

    resumo = client.get("/api/user/dashboard-summary").json()
    assert resumo["vigentes_count"] <= resumo["total_tenders_count"]
    with engine.connect() as conn:
        assert resumo["total_tenders_count"] == conn.execute(text("SELECT COUNT(*) FROM tenders")).scalar()
