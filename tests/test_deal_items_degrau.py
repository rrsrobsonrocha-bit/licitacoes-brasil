"""Seleção de itens em "+ Meus Negócios", card clicável do painel e degrau mínimo da sala de disputa."""
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from server.database import SessionLocal
from server.main import app
from server.models import Deal, Tender, TenderItem, hoje_sao_paulo
from server.services import pncp_compras
from server.services.dispute_monitor import dispute_monitor

client = TestClient(app)
TEMPLATES = Path(__file__).resolve().parent.parent / "server" / "templates"


def _tender_com_itens(degrau_raw=None):
    """Cria um certame isolado com dois itens; o item 1 pode trazer degrau no raw_data."""
    db = SessionLocal()
    try:
        source_id = db.query(Tender.source_id).filter(Tender.source_id.isnot(None)).limit(1).scalar()
        tender = Tender(
            source_id=source_id,
            external_id=f"TESTE-DEGRAU-{uuid.uuid4().hex[:12]}",
            title="Aquisição de notebooks (teste)",
            object_description="Aquisição de notebooks e mouses para teste automatizado.",
            source_url="https://pncp.gov.br/app/editais/teste-degrau",
            contracting_entity="Órgão de Teste",
            state="SP",
            modality="Pregão Eletrônico",
        )
        db.add(tender)
        db.flush()
        raw1 = {"numeroItem": 1, "descricao": "Notebook"}
        if degrau_raw is not None:
            raw1["intervaloMinimoEntreLances"] = degrau_raw
        db.add_all([
            TenderItem(tender_id=tender.id, item_number=1, description="Notebook", quantity=10,
                       unit="UN", estimated_unit_value=5000, estimated_total_value=50000, raw_data=raw1),
            TenderItem(tender_id=tender.id, item_number=2, description="Mouse", quantity=10,
                       unit="UN", estimated_unit_value=50, estimated_total_value=500,
                       raw_data={"numeroItem": 2, "descricao": "Mouse"}),
        ])
        db.commit()
        return tender.id
    finally:
        db.close()


def _remover(tender_id):
    db = SessionLocal()
    try:
        db.query(Deal).filter(Deal.tender_id == tender_id).delete()
        db.query(TenderItem).filter(TenderItem.tender_id == tender_id).delete()
        db.query(Tender).filter(Tender.id == tender_id).delete()
        db.commit()
    finally:
        db.close()


# ---------------- 1. Card do Kanban sem resíduo de shell ----------------
def test_deal_card_sem_heredoc_de_terminal():
    conteudo = (TEMPLATES / "partials_deal_card.html").read_text(encoding="utf-8")
    assert "cat << 'EOF'" not in conteudo
    assert not conteudo.rstrip().endswith("EOF")
    assert conteudo.lstrip().startswith("<div")


# ---------------- 2. Seleção de itens no negócio ----------------
def test_criar_negocio_com_itens_selecionados_e_preservar_selecao(usuario_auth):
    h = usuario_auth.headers
    tender_id = _tender_com_itens()
    try:
        resp = client.post("/api/deals", json={"tender_id": tender_id, "selected_items": [{"item_number": 2}]}, headers=h)
        assert resp.status_code == 201
        data = resp.json()
        assert data["selected_items_count"] == 1
        item = data["selected_items"][0]
        assert item["item_number"] == 2
        assert item["description"] == "Mouse"  # completado a partir do banco
        assert item["estimated_total_value"] == 500

        # Atualiza a seleção (aceita números puros) e não recua o estágio já avançado
        client.patch(f"/api/deals/{data['id']}/stage", json={"stage": "disputa"}, headers=h)
        resp2 = client.post("/api/deals", json={"tender_id": tender_id, "selected_items": [1, 2, "x"]}, headers=h)
        data2 = resp2.json()
        assert data2["id"] == data["id"]
        assert [i["item_number"] for i in data2["selected_items"]] == [1, 2]
        assert data2["stage"] == "disputa"

        # Sem a chave selected_items a seleção é preservada
        data3 = client.post(f"/api/deals?tender_id={tender_id}", headers=h).json()
        assert data3["selected_items_count"] == 2

        # O card do Kanban exibe os itens em disputa
        html = client.get("/negocios", headers=h).text
        assert "2 itens em disputa: 1, 2" in html
    finally:
        _remover(tender_id)


def test_search_tem_modal_de_selecao_de_itens():
    html = (TEMPLATES / "search.html").read_text(encoding="utf-8")
    assert 'id="deal-items-modal"' in html
    assert 'id="deal-items-select-all"' in html
    assert "selected_items" in html


# ---------------- 3. Card "Licitações publicadas hoje" clicável ----------------
def test_card_publicadas_hoje_leva_para_busca_filtrada():
    html = client.get("/dashboard").text
    hoje = hoje_sao_paulo().isoformat()
    assert 'id="card-tenders-today"' in html
    assert f"/search?date_from={hoje}&amp;date_to={hoje}" in html or f"/search?date_from={hoje}&date_to={hoje}" in html

    busca = client.get(f"/search?date_from={hoje}&date_to={hoje}")
    assert busca.status_code == 200
    assert 'id="active-date-filter"' in busca.text


# ---------------- 4. Degrau mínimo vindo do edital ----------------
def test_extrair_degrau_minimo():
    assert pncp_compras.extrair_degrau_minimo({"intervaloMinimoEntreLances": 0.5}) == 0.5
    assert pncp_compras.extrair_degrau_minimo({"degrauMinimo": "R$ 1.234,50"}) == 1234.5
    assert pncp_compras.extrair_degrau_minimo('{"variacaoMinimaEntreLances": "2.00"}') == 2.0
    assert pncp_compras.extrair_degrau_minimo({"descricao": "sem degrau"}) is None
    assert pncp_compras.extrair_degrau_minimo({"degrauMinimo": 0}) is None
    assert pncp_compras.extrair_degrau_minimo(None) is None


def test_disputa_usa_degrau_do_edital_e_fallback():
    tender_id = _tender_com_itens(degrau_raw=1.5)
    try:
        r = client.get(f"/api/disputes/degrau?id_pregao={tender_id}&numero_item=1").json()
        assert r == {"degrau_minimo": 1.5, "origem": "edital"}
        r2 = client.get(f"/api/disputes/degrau?id_pregao={tender_id}&numero_item=2").json()
        assert r2 == {"degrau_minimo": 0.01, "origem": "padrao"}

        base = {"id_pregao": tender_id, "descricao": "Notebook", "valor_referencia": 50000, "meu_lance": 49000}
        item1 = client.post("/api/disputes/track", json={**base, "numero_item": 1, "degrau_minimo": None}).json()["item"]
        assert item1["degrau_minimo"] == 1.5 and item1["degrau_origem"] == "edital"

        item2 = client.post("/api/disputes/track", json={**base, "numero_item": 2}).json()["item"]
        assert item2["degrau_minimo"] == 0.01 and item2["degrau_origem"] == "padrao"

        # Valor digitado pelo usuário prevalece sobre o edital
        item3 = client.post("/api/disputes/track", json={**base, "numero_item": 1, "degrau_minimo": 3}).json()["item"]
        assert item3["degrau_minimo"] == 3.0 and item3["degrau_origem"] == "manual"
    finally:
        for n in (1, 2):
            dispute_monitor._items.pop(f"{tender_id}:{n}", None)
        _remover(tender_id)
