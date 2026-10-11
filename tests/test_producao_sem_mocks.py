"""Produção sem dados fictícios: seed mínimo, migrações de limpeza/órfãos e filtro estrito de publicação."""
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.config import settings
from server.database import SessionLocal
from server.main import app
from server.migrate import (
    _associar_registros_orfaos,
    _remover_contas_demonstracao,
    _remover_certames_ficticios_legados,
)
from server.models import Alert, Deal, Favorite, SavedSearch, Tender, TenderItem, User, hoje_sao_paulo
from server.security import hash_password
from server.seed_data import (
    EMAILS_CONTAS_DEMONSTRACAO, garantir_admin_inicial, garantir_usuario_inicial, seed_database
)

client = TestClient(app)


def _tender(db, *, external_id=None, publicado_em=None, criado_em=None, **extra) -> Tender:
    tender = Tender(
        source_id="pncp-source-01",
        external_id=external_id or f"TESTE-PROD-{uuid.uuid4().hex[:12]}",
        title=extra.pop("title", f"Certame de teste {uuid.uuid4().hex[:6]}"),
        object_description="Objeto de teste de produção sem dados fictícios.",
        contracting_entity="Órgão de Teste",
        state="SP",
        modality="Pregão Eletrônico",
        status="Divulgada no PNCP",
        publication_date=publicado_em,
        source_url="https://pncp.gov.br/app/editais/teste",
        **extra,
    )
    if criado_em:
        tender.created_at = criado_em
        tender.last_synced_at = criado_em
    db.add(tender)
    db.commit()
    return tender


def _apagar_tenders(ids):
    db = SessionLocal()
    try:
        db.query(Tender).filter(Tender.id.in_(list(ids))).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


# ---------------- Seed de produção ----------------
def test_seed_de_producao_nao_cria_contas_certames_nem_negocios(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_EMAIL", "")
    db = SessionLocal()
    try:
        antes = (db.query(User).count(), db.query(Tender).count(), db.query(Deal).count(), db.query(SavedSearch).count())
        seed_database(db)
        depois = (db.query(User).count(), db.query(Tender).count(), db.query(Deal).count(), db.query(SavedSearch).count())
        assert antes == depois
    finally:
        db.close()


def test_admin_inicial_somente_via_variaveis_de_ambiente(monkeypatch):
    email = f"admin-{uuid.uuid4().hex[:8]}@exemplo.com.br"
    db = SessionLocal()
    try:
        monkeypatch.setattr(settings, "ADMIN_EMAIL", email)
        monkeypatch.setattr(settings, "ADMIN_PASSWORD", "curta")  # senha fraca: não cria
        garantir_admin_inicial(db)
        assert db.query(User).filter(User.email == email).first() is None

        monkeypatch.setattr(settings, "ADMIN_PASSWORD", "SenhaAdmin@2026")
        garantir_admin_inicial(db)
        admin = db.query(User).filter(User.email == email).first()
        assert admin is not None and admin.role == "ADMIN"

        # Conta existente é promovida, reativada e renomeada por ADMIN_NAME; a senha é mantida
        admin.role, admin.status = "USER", "INACTIVE"
        senha_hash = admin.password_hash
        db.commit()
        monkeypatch.setattr(settings, "ADMIN_NAME", "Nome Definido no Ambiente")
        monkeypatch.setattr(settings, "ADMIN_PASSWORD", "OutraSenha@2026")
        garantir_admin_inicial(db)
        db.refresh(admin)
        assert admin.role == "ADMIN" and admin.status == "ACTIVE"
        assert admin.name == "Nome Definido no Ambiente"
        assert admin.password_hash == senha_hash
        db.delete(admin)
        db.commit()
    finally:
        db.close()


def test_usuario_inicial_somente_via_variaveis_de_ambiente(monkeypatch):
    email = f"usuario-{uuid.uuid4().hex[:8]}@exemplo.com.br"
    db = SessionLocal()
    try:
        monkeypatch.setattr(settings, "USUARIO_INICIAL_EMAIL", email)
        monkeypatch.setattr(settings, "USUARIO_INICIAL_NOME", "Usuária Inicial")
        monkeypatch.setattr(settings, "USUARIO_INICIAL_SENHA", "curta")  # senha fraca: não cria
        garantir_usuario_inicial(db)
        assert db.query(User).filter(User.email == email).first() is None

        monkeypatch.setattr(settings, "USUARIO_INICIAL_SENHA", "SenhaUsuaria@2026")
        garantir_usuario_inicial(db)
        conta = db.query(User).filter(User.email == email).one()
        assert (conta.name, conta.role, conta.status) == ("Usuária Inicial", "USER", "ACTIVE")
        resp = client.post("/api/auth/login", json={"email": email, "password": "SenhaUsuaria@2026"})
        assert resp.status_code == 200

        # Conta existente não é alterada (nem senha, nem nome)
        senha_hash = conta.password_hash
        monkeypatch.setattr(settings, "USUARIO_INICIAL_SENHA", "OutraSenha@2026")
        monkeypatch.setattr(settings, "USUARIO_INICIAL_NOME", "Outro Nome")
        garantir_usuario_inicial(db)
        db.refresh(conta)
        assert conta.password_hash == senha_hash and conta.name == "Usuária Inicial"
        db.delete(conta)
        db.commit()
    finally:
        db.close()


# ---------------- Migrações de limpeza ----------------
def test_contas_de_demonstracao_sao_apagadas_com_dados_transferidos(criar_usuario, monkeypatch):
    db = SessionLocal()
    # Contas demo legadas como podem existir em bancos antigos: uma ativa, outra já desativada
    demos = [
        User(name="Demo admin legado", email=EMAILS_CONTAS_DEMONSTRACAO[0], role="ADMIN", status="INACTIVE",
             password_hash=hash_password("QualquerSenha@1")),
        User(name="Demo usuário legado", email=EMAILS_CONTAS_DEMONSTRACAO[1], role="USER", status="ACTIVE",
             password_hash=hash_password("QualquerSenha@2")),
    ]
    db.add_all(demos)
    db.commit()
    ids_demo = [d.id for d in demos]
    try:
        # Sem conta real ativa para receber os dados: nada é apagado
        reais = db.query(User).filter(~User.email.in_(EMAILS_CONTAS_DEMONSTRACAO), User.status == "ACTIVE").all()
        for u in reais:
            u.status = "SUSPENDED"
        db.commit()
        _remover_contas_demonstracao()
        assert db.query(User).filter(User.id.in_(ids_demo)).count() == 2
        for u in reais:
            u.status = "ACTIVE"
        db.commit()

        # Com conta principal: tudo o que as demos criaram vai para ela e as demos são apagadas
        principal = criar_usuario("Conta Principal")
        monkeypatch.setattr(settings, "ORPHAN_OWNER_EMAIL", principal.email)
        tender = db.query(Tender).first()
        negocio = Deal(tender_id=tender.id, user_id=ids_demo[1], stage="disputa", notes="feito na conta demo")
        alerta = Alert(user_id=ids_demo[0], name="Alerta da demo", keywords="obras", frequency="DAILY",
                       channels="IN_APP", active=True)
        favorito = Favorite(user_id=ids_demo[1], tender_id=tender.id)
        db.add_all([negocio, alerta, favorito])
        db.commit()
        ids = (negocio.id, alerta.id, favorito.id)

        _remover_contas_demonstracao()
        _remover_contas_demonstracao()  # idempotente
        db.expire_all()
        assert db.query(User).filter(User.email.in_(EMAILS_CONTAS_DEMONSTRACAO)).count() == 0
        assert db.get(Deal, ids[0]).user_id == principal.id
        assert db.get(Alert, ids[1]).user_id == principal.id
        assert db.get(Favorite, ids[2]).user_id == principal.id
    finally:
        db.rollback()
        db.query(User).filter(User.id.in_(ids_demo)).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_certames_ficticios_legados_sao_removidos_exceto_em_uso(usuario_auth):
    ids = json.loads((Path(__file__).resolve().parent.parent / "server" / "data" / "certames_ficticios_legados.json")
                     .read_text(encoding="utf-8"))["external_ids"]
    assert len(ids) >= 700
    db = SessionLocal()
    try:
        livre = _tender(db, external_id=ids[0])
        em_uso = _tender(db, external_id=ids[1])
        real = _tender(db)
        db.add(Deal(tender_id=em_uso.id, user_id=usuario_auth.id, stage="oportunidades"))
        db.add(TenderItem(tender_id=livre.id, item_number=1, description="item ficticio"))
        db.commit()
        livre_id, em_uso_id, real_id = livre.id, em_uso.id, real.id
    finally:
        db.close()

    _remover_certames_ficticios_legados()

    db = SessionLocal()
    try:
        assert db.query(Tender).filter(Tender.id == livre_id).first() is None
        assert db.query(TenderItem).filter(TenderItem.tender_id == livre_id).count() == 0
        assert db.query(Tender).filter(Tender.id == em_uso_id).first() is not None
        assert db.query(Tender).filter(Tender.id == real_id).first() is not None
    finally:
        db.close()
    _apagar_tenders([em_uso_id, real_id])


def test_registros_orfaos_vao_para_a_conta_principal(criar_usuario, monkeypatch):
    principal = criar_usuario("Conta Principal")
    monkeypatch.setattr(settings, "ORPHAN_OWNER_EMAIL", principal.email)
    db = SessionLocal()
    try:
        tender = db.query(Tender).first()
        orfao = Deal(tender_id=tender.id, user_id=None, stage="qualificacao", notes="Negócio real legado")
        busca = SavedSearch(id=str(uuid.uuid4()), user_id=None, name="Busca legada", keywords="x", active=True)
        db.add_all([orfao, busca])
        db.commit()
        orfao_id, busca_id = orfao.id, busca.id
    finally:
        db.close()

    _associar_registros_orfaos()

    db = SessionLocal()
    try:
        assert db.query(Deal).filter(Deal.user_id.is_(None)).count() == 0
        assert db.query(Deal).filter(Deal.id == orfao_id).first().user_id == principal.id
        assert db.query(SavedSearch).filter(SavedSearch.id == busca_id).first().user_id == principal.id
    finally:
        db.close()

    # O negócio legado passa a aparecer no funil da conta principal
    deals = client.get("/api/deals", headers=principal.headers).json()
    assert any(d["id"] == orfao_id for d in deals["deals"])


# ---------------- Filtro estrito de data de publicação (Brasília) ----------------
def test_publicadas_hoje_usa_data_oficial_do_pncp_e_nao_a_insercao_local():
    hoje = hoje_sao_paulo()
    inicio_hoje = datetime.combine(hoje, datetime.min.time())
    db = SessionLocal()
    try:
        hoje_cedo = _tender(db, publicado_em=inicio_hoje + timedelta(minutes=1), title="PUBHOJE cedo")
        ontem_tarde = _tender(db, publicado_em=inicio_hoje - timedelta(minutes=1), title="PUBHOJE ontem",
                              criado_em=datetime.utcnow())  # inserido hoje, publicado ontem
        sem_data = _tender(db, publicado_em=None, title="PUBHOJE sem data", criado_em=datetime.utcnow())
        ha_6_dias = _tender(db, publicado_em=inicio_hoje - timedelta(days=6) + timedelta(hours=9), title="PUBHOJE seis")
        ha_8_dias = _tender(db, publicado_em=inicio_hoje - timedelta(days=8), title="PUBHOJE oito")
        ids = {t.title: t.id for t in (hoje_cedo, ontem_tarde, sem_data, ha_6_dias, ha_8_dias)}
    finally:
        db.close()
    try:
        h = hoje.isoformat()
        hoje_res = {r["id"] for r in client.get(f"/api/tenders?q=PUBHOJE&date_from={h}&date_to={h}&page_size=100").json()["results"]}
        assert hoje_res == {ids["PUBHOJE cedo"]}

        sete = (hoje - timedelta(days=6)).isoformat()
        semana = {r["id"] for r in client.get(f"/api/tenders?q=PUBHOJE&date_from={sete}&date_to={h}&page_size=100").json()["results"]}
        assert semana == {ids["PUBHOJE cedo"], ids["PUBHOJE ontem"], ids["PUBHOJE seis"]}

        # Data inválida é ignorada (não derruba a busca)
        assert client.get("/api/tenders?q=PUBHOJE&date_from=31-99-2026").status_code == 200

        # O card do painel conta só as publicadas hoje no PNCP (sem cair para o total do banco)
        db = SessionLocal()
        try:
            esperado = db.query(Tender).filter(
                Tender.publication_date >= datetime.combine(hoje, datetime.min.time()),
                Tender.publication_date < datetime.combine(hoje + timedelta(days=1), datetime.min.time()),
            ).count()
        finally:
            db.close()
        resumo = client.get("/api/user/dashboard-summary").json()
        assert resumo["tenders_today_count"] == esperado
    finally:
        _apagar_tenders(ids.values())


def test_barra_lateral_tem_atalhos_e_seletores_de_publicacao():
    html = client.get("/search").text
    hoje = hoje_sao_paulo()
    assert 'id="filtro-periodo-publicacao"' in html
    assert 'id="atalho-publicadas-hoje"' in html and f"date_from={hoje.isoformat()}" in html
    assert 'id="atalho-ultimos-7-dias"' in html and f"date_from={(hoje - timedelta(days=6)).isoformat()}" in html
    assert 'id="atalho-todas-vigentes"' in html and "vigentes=true" in html
    assert 'type="date" id="filtro-date-from" name="date_from"' in html
    assert 'type="date" id="filtro-date-to" name="date_to"' in html

    ativo = client.get(f"/search?date_from={hoje.isoformat()}&date_to={hoje.isoformat()}").text
    bloco = ativo.split('id="atalho-publicadas-hoje"', 1)[1].split("</a>", 1)[0]
    assert "bg-sky-500/15" in bloco  # atalho destacado quando ativo


def test_identidade_licitacoes_brasil_sem_nome_antigo():
    raiz = Path(__file__).resolve().parent.parent
    antigo = "licita" + "flow"
    for arquivo in list((raiz / "server").rglob("*")) + [raiz / "README.md", raiz / "package.json", raiz / "CLAUDE.md"]:
        if arquivo.is_file() and arquivo.suffix in {".py", ".html", ".js", ".json", ".md", ".txt"}:
            assert antigo not in arquivo.read_text(encoding="utf-8", errors="ignore").lower(), arquivo
    assert "Licitações Brasil" in (raiz / "CLAUDE.md").read_text(encoding="utf-8")
