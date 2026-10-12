import pytest
from fastapi.testclient import TestClient
from server.main import app
from server.database import SessionLocal
from server.models import Tender, Deal, DEAL_STAGES

client = TestClient(app)

def test_deal_model_structure():
    db = SessionLocal()
    try:
        tender = db.query(Tender).first()
        assert tender is not None

        # Criação de Deal diretamente via model
        deal = Deal(
            tender_id=tender.id,
            stage="oportunidades",
            notes="Teste unitário de modelo"
        )
        db.add(deal)
        db.commit()
        db.refresh(deal)

        assert isinstance(deal.id, int)
        assert deal.tender_id == tender.id
        assert deal.stage == "oportunidades"
        assert deal.notes == "Teste unitário de modelo"
        assert deal.created_at is not None
        assert deal.updated_at is not None
        assert deal.tender.id == tender.id

        # Limpeza
        db.delete(deal)
        db.commit()
    finally:
        db.close()

def test_api_post_deal_creation(usuario_auth):
    db = SessionLocal()
    try:
        tender = db.query(Tender).first()
        assert tender is not None
        t_id = tender.id
    finally:
        db.close()

    # 1. Via Query Param
    resp = client.post(f"/api/deals?tender_id={t_id}&stage=oportunidades&notes=Nota+Teste", headers=usuario_auth.headers)
    assert resp.status_code in [200, 201]
    data = resp.json()
    assert "id" in data
    assert data["tender_id"] == t_id
    assert data["stage"] == "oportunidades"
    assert data["tender"] is not None
    assert data["tender"]["title"] is not None

    deal_id = data["id"]

    # 2. Via JSON Body
    resp_json = client.post("/api/deals", json={
        "tender_id": t_id,
        "stage": "qualificacao",
        "notes": "Atualizado via JSON"
    }, headers=usuario_auth.headers)
    assert resp_json.status_code in [200, 201]
    data_json = resp_json.json()
    assert data_json["id"] == deal_id
    assert data_json["stage"] == "qualificacao"

def test_api_post_deal_validation(usuario_auth):
    # Sem autenticação -> 401 (o funil é privado de cada conta)
    assert client.post("/api/deals?tender_id=qualquer").status_code == 401

    # Sem tender_id -> 422
    resp_empty = client.post("/api/deals", headers=usuario_auth.headers)
    assert resp_empty.status_code == 422

    # Licitação inexistente -> 404
    resp_not_found = client.post("/api/deals?tender_id=licitacao-inexistente-xyz", headers=usuario_auth.headers)
    assert resp_not_found.status_code == 404

def test_api_get_deals_grouped_by_stage(usuario_auth):
    assert client.get("/api/deals").status_code == 401
    resp = client.get("/api/deals", headers=usuario_auth.headers)
    assert resp.status_code == 200
    data = resp.json()

    # 5 etapas oficiais do Kanban, na ordem do funil
    assert DEAL_STAGES == ["oportunidades", "qualificacao", "proposta", "disputa", "homologado"]
    for stage in DEAL_STAGES:
        assert stage in data
        assert isinstance(data[stage], list)
    assert [e["label"] for e in data["stages"]] == [
        "Oportunidades", "Qualificação", "Proposta Feita", "Disputa", "Homologado / Ganho"]
    assert "classificacao" not in data and "contrato" not in data

    assert "total" in data
    assert isinstance(data["total"], int)
    assert "grouped" in data

def test_api_patch_deal_stage_transitions(usuario_auth):
    db = SessionLocal()
    try:
        tender = db.query(Tender).first()
        deal = Deal(tender_id=tender.id, user_id=usuario_auth.id, stage="oportunidades")
        db.add(deal)
        db.commit()
        db.refresh(deal)
        deal_id = deal.id
    finally:
        db.close()

    # Movimentação sequencial: qualificacao -> proposta -> disputa -> homologado
    for target in ["qualificacao", "proposta", "disputa", "homologado", "oportunidades"]:
        resp = client.patch(f"/api/deals/{deal_id}/stage", json={"stage": target}, headers=usuario_auth.headers)
        assert resp.status_code == 200
        assert resp.json()["stage"] == target

    # Chaves antigas continuam aceitas e viram "homologado"
    for antiga in ("classificacao", "contrato"):
        resp = client.patch(f"/api/deals/{deal_id}/stage", json={"stage": antiga}, headers=usuario_auth.headers)
        assert resp.status_code == 200 and resp.json()["stage"] == "homologado"

    # Estágio inválido -> 400
    resp_bad = client.patch(f"/api/deals/{deal_id}/stage", json={"stage": "estagio_invalido"}, headers=usuario_auth.headers)
    assert resp_bad.status_code == 400
    assert "Estágio inválido" in resp_bad.json()["detail"]

    # Deal inexistente -> 404
    resp_404 = client.patch("/api/deals/999999/stage", json={"stage": "disputa"}, headers=usuario_auth.headers)
    assert resp_404.status_code == 404

def test_kanban_html_view_and_cards(usuario_auth):
    db = SessionLocal()
    try:
        tender = db.query(Tender).first()
        db.add(Deal(tender_id=tender.id, user_id=usuario_auth.id, stage="qualificacao"))
        db.commit()
    finally:
        db.close()

    resp = client.get("/negocios?view=quadros", headers=usuario_auth.headers)
    assert resp.status_code == 200
    html = resp.text

    # Validar presença das 5 colunas no Kanban, na ordem oficial
    rotulos = ["Oportunidades", "Qualificação", "Proposta Feita", "Disputa", "Homologado / Ganho"]
    posicoes = [html.index(f'truncate" title="{d}">{r}</h2>') for d, r in zip(
        ["Leads e editais identificados", "Análise técnica e viabilidade", "Proposta enviada no portal oficial",
         "Sala de disputa / lances ao vivo", "Adjudicado / vencido"], rotulos)]
    assert posicoes == sorted(posicoes)
    assert "Classificação</h2>" not in html and ">Contrato</h2>" not in html

    # Contadores e colunas de destino do arraste com as novas chaves
    for st in DEAL_STAGES:
        assert f'id="count-{st}"' in html
        assert f"handleDrop(event, '{st}')" in html
    assert 'const STAGES = ["oportunidades", "qualificacao", "proposta", "disputa", "homologado"];' in html

    # Validar botões de movimentação
    assert ">← Voltar</button>" in html
    assert ">Avançar →</button>" in html

    # Validar elementos do card
    assert "Valor do negócio" in html
    assert "countdown-badge" in html

def test_pipeline_add_endpoint_integration(usuario_auth):
    db = SessionLocal()
    try:
        tender = db.query(Tender).first()
        assert tender is not None
        t_id = tender.id
    finally:
        db.close()

    assert client.post(f"/api/pipeline/add?tender_id={t_id}").status_code == 401
    resp = client.post(f"/api/pipeline/add?tender_id={t_id}", headers=usuario_auth.headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert "deal_id" in data
    assert data["tender_id"] == t_id


def test_migracao_de_etapas_legadas_para_homologado(usuario_auth):
    from sqlalchemy import text
    from server.database import engine
    from server.migrate import _migrar_etapas_legadas_do_funil

    db = SessionLocal()
    tender = db.query(Tender).first()
    ids = []
    for antiga in ("classificacao", "contrato", "disputa"):
        deal = Deal(tender_id=tender.id, user_id=usuario_auth.id, stage="oportunidades")
        db.add(deal)
        db.commit()
        ids.append(deal.id)
        with engine.begin() as conn:  # grava a etapa antiga direto no banco, como em bases anteriores
            conn.execute(text("UPDATE deals SET stage = :s WHERE id = :i"), {"s": antiga, "i": deal.id})
    db.close()

    # Antes da migração a página já agrupa as antigas em "Homologado / Ganho"
    dados = client.get("/api/deals", headers=usuario_auth.headers).json()
    assert {d["id"] for d in dados["homologado"]} >= set(ids[:2])

    _migrar_etapas_legadas_do_funil()
    _migrar_etapas_legadas_do_funil()  # idempotente
    db = SessionLocal()
    etapas = {d.id: d.stage for d in db.query(Deal).filter(Deal.id.in_(ids))}
    db.close()
    assert etapas == {ids[0]: "homologado", ids[1]: "homologado", ids[2]: "disputa"}

    dados = client.get("/api/deals", headers=usuario_auth.headers).json()
    assert dados["counts"]["homologado"] >= 2 and "valores" in dados and "valor_pipeline" in dados
