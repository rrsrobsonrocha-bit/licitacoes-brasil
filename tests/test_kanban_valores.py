"""Valores financeiros no Funil (Kanban): cartões, totais por etapa e valor total em pipeline."""
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from server.database import SessionLocal
from server.main import app, formatar_brl
from server.models import Deal, Tender, TenderItem
from server.search_engine import agora_sao_paulo_naive

client = TestClient(app)


@pytest.fixture
def certames():
    marcador = uuid.uuid4().hex[:8]
    agora = agora_sao_paulo_naive()
    db = SessionLocal()
    ids = []
    for n, valor in ((1, 50000.0), (2, 380500.0), (3, 12000.0)):
        t = Tender(source_id="pncp-source-01", external_id=f"KANBAN-{marcador}-{n}", title=f"Certame Kanban {n}",
                   object_description="x", contracting_entity=f"Órgão Kanban {n}", state="DF",
                   status="Divulgada no PNCP", source_url="https://pncp.gov.br/app/editais", estimated_total_value=valor,
                   notice_number=f"90{n}/2026", publication_date=agora - timedelta(days=1),
                   proposal_deadline=(agora + timedelta(days=5)).replace(hour=9, minute=30, second=0, microsecond=0))
        db.add(t)
        db.flush()
        ids.append(t.id)
    itens = [TenderItem(tender_id=ids[0], item_number=i, description=f"Item {i}", estimated_total_value=v, origem="PNCP")
             for i, v in ((1, 40000.0), (2, 5200.0), (3, 4800.0))]
    db.add_all(itens)
    db.commit()
    dados = {"ids": ids, "itens": [i.id for i in itens]}
    db.close()
    yield dados
    db = SessionLocal()
    db.query(Deal).filter(Deal.tender_id.in_(ids)).delete(synchronize_session=False)
    db.query(TenderItem).filter(TenderItem.tender_id.in_(ids)).delete(synchronize_session=False)
    db.query(Tender).filter(Tender.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    db.close()


def test_formatar_brl():
    assert formatar_brl(45200) == "R$ 45.200,00"
    assert formatar_brl(1234567.891) == "R$ 1.234.567,89"
    assert formatar_brl(None) == ""


def test_kanban_exibe_valores_etiquetas_e_totais(certames, usuario_auth):
    h = usuario_auth.headers
    a, b, c = certames["ids"]
    # Itens selecionados: 40.000 + 5.200 = 45.200
    client.post("/api/deals", headers=h, json={"tender_id": a, "item_ids": certames["itens"][:2]})
    # Edital global: 380.500 na etapa Qualificação
    client.post("/api/deals", headers=h, json={"tender_id": b, "modo": "global", "stage": "qualificacao"})
    # Negócio antigo (sem valor gravado): cai no valor do edital
    db = SessionLocal()
    db.add(Deal(tender_id=c, user_id=usuario_auth.id, stage="oportunidades"))
    db.commit()
    db.close()

    html = client.get("/negocios", headers=h).text
    assert "R$ 45.200,00" in html and "🏷️ 2 itens selecionados" in html
    assert "R$ 380.500,00" in html and "🌐 Edital Global" in html
    assert 'data-valor="45200.0"' in html
    assert "Órgão Kanban 1" in html and "90" in html and "Sessão:" in html and "09:30" in html
    # Totais por coluna e do pipeline
    assert "2 negócios · <span class=\"text-emerald-300 font-bold\">R$ 57.200,00</span>" in html
    assert "1 negócio · <span class=\"text-emerald-300 font-bold\">R$ 380.500,00</span>" in html
    assert "Valor Total em Pipeline:" in html and "R$ 437.700,00" in html

    dados = client.get("/api/deals", headers=h).json()
    assert dados["valores"]["oportunidades"] == 57200.0 and dados["valores"]["qualificacao"] == 380500.0
    assert dados["valor_pipeline"] == 437700.0
    legado = next(d for d in dados["deals"] if d["tender_id"] == c)
    assert legado["valor_negocio"] == 12000.0 and legado["modo"] == "global"


def test_kanban_atualiza_totais_ao_mover_cartoes():
    html = client.get("/negocios").text
    for marcador in ("function updateColumnCounts", "resumo-${st}", "dataset.valor", "kanban-total-valor",
                     'id="resumo-contrato"'):
        assert marcador in html, marcador
