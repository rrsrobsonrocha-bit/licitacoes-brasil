"""Carrossel do Kanban no celular (trava de eixo, limiar de 35%, abas de etapas) e busca paginada no banco (OOM)."""
import re
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect

from server.database import SessionLocal
from server.main import app
from server.models import Deal, Tender, TenderItem
from server.schemas import SearchFilterParams
from server.search_engine import PAGE_SIZE_MAXIMO, agora_sao_paulo_naive, search_tenders

client = TestClient(app)


@pytest.fixture
def catalogo_grande():
    """60 editais com um marcador único, valores distintos e um item que casa com o termo pesquisado."""
    marcador = "OOM" + uuid.uuid4().hex[:8].upper()
    agora = agora_sao_paulo_naive()
    db = SessionLocal()
    ids = []
    for n in range(60):
        t = Tender(source_id="pncp-source-01", external_id=f"{marcador}-{n}", title=f"Edital {marcador} numero {n}",
                   object_description="Objeto comum " * 40, contracting_entity="Órgão de Teste", state="SP",
                   status="Divulgada no PNCP", source_url="https://pncp.gov.br/app/editais",
                   estimated_total_value=1000.0 * (n + 1), publication_date=agora - timedelta(hours=n),
                   proposal_deadline=agora + timedelta(days=5), ai_resumo="resumo pesado " * 500)
        db.add(t)
        db.flush()
        ids.append(t.id)
    db.add(TenderItem(tender_id=ids[59], item_number=1, description=f"Cadeira {marcador} giratoria", origem="PNCP"))
    db.commit()
    db.close()
    yield {"marcador": marcador, "ids": ids}
    db = SessionLocal()
    db.query(TenderItem).filter(TenderItem.tender_id.in_(ids)).delete(synchronize_session=False)
    db.query(Tender).filter(Tender.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    db.close()


def test_busca_pagina_no_banco_com_limite_rigido(catalogo_grande):
    m = catalogo_grande["marcador"]
    padrao = client.get(f"/api/tenders?q={m}").json()
    assert padrao["page_size"] == 25 and len(padrao["results"]) == 25 and padrao["total"] == 60

    enorme = client.get(f"/api/tenders?q={m}&page_size=100").json()
    assert enorme["page_size"] == PAGE_SIZE_MAXIMO == 50 and len(enorme["results"]) == 50

    # Ordenação feita no SQL e contínua entre páginas (LIMIT/OFFSET)
    p1 = client.get(f"/api/tenders?q={m}&sort_by=value_desc&page_size=25&page=1").json()["results"]
    p3 = client.get(f"/api/tenders?q={m}&sort_by=value_desc&page_size=25&page=3").json()["results"]
    assert [r["estimated_total_value"] for r in p1[:2]] == [60000.0, 59000.0]
    assert [r["estimated_total_value"] for r in p3] == [1000.0 * v for v in range(10, 0, -1)]


def test_relevancia_pontua_itens_no_sql_e_carrega_so_a_pagina(catalogo_grande):
    m = catalogo_grande["marcador"]
    carregados = []

    def ao_carregar(alvo, _contexto):
        carregados.append(("ai_resumo" in inspect(alvo).unloaded, "object_description" in inspect(alvo).unloaded))

    event.listen(Tender, "load", ao_carregar)
    db = SessionLocal()
    try:
        r = search_tenders(db, SearchFilterParams(q=m, sort_by="relevance", page_size=5))
    finally:
        event.remove(Tender, "load", ao_carregar)
        db.close()
    assert r.total == 60 and len(r.results) == 5
    # O único edital com item que contém o termo sobe para o topo
    assert r.results[0].id == catalogo_grande["ids"][59] and r.results[0].match_score > r.results[1].match_score
    # Só a página virou objeto ORM, e sem as colunas pesadas (resumos da IA)
    assert carregados == [(True, False)] * 5


def test_kanban_mobile_vira_carrossel_com_abas(usuario_auth):
    html = client.get("/negocios?view=quadros", headers=usuario_auth.headers).text
    quadro = re.search(r'<div id="kanban-board" class="([^"]+)"', html).group(1)
    for classe in ("flex", "flex-row", "overflow-x-auto", "overflow-y-hidden", "snap-x", "snap-mandatory",
                   "scroll-smooth", "touch-pan-x", "lg:grid", "lg:grid-cols-5"):
        assert classe in quadro.split(), classe
    assert "md:grid-cols-2" not in quadro and "grid-cols-1" not in quadro.split()  # sem empilhar colunas
    assert html.count("kanban-column w-full min-w-full flex-shrink-0 snap-center snap-always px-2") == 5
    assert html.count("h-[calc(100vh-250px)]") == 5 and html.count("touch-pan-y") == 5

    abas = html[html.index('id="kanban-abas"'):html.index("</nav>", html.index('id="kanban-abas"'))]
    assert 'class="lg:hidden' in html[html.index('id="kanban-abas"') - 5:html.index('id="kanban-abas"') + 40]
    assert abas.count('role="tab"') == 5 and 'id="aba-count-oportunidades"' in abas and "Qualificação (" in abas
    assert "IntersectionObserver" in html and "LIMIAR_TROCA_COLUNA = 0.35" in html
    assert "Math.abs(deltaX) / larguraTela > LIMIAR_TROCA_COLUNA" in html and "{ passive: false }" in html

    # Cabeçalho do celular: só o título; topo, filtros e subtítulo a partir de md
    assert "kanban-topo hidden md:flex" in html and '<p class="hidden md:block text-xs' in html
    assert 'hidden md:inline text-emerald-400">Kanban' in html
    # Hambúrguer do topo só no tablet; no celular o menu fica na barra inferior
    assert re.search(r'id="mobile-menu-toggle"[^>]*class="hidden md:inline-flex lg:hidden', html)
    assert 'id="bottom-menu-toggle"' in html


def test_cartao_com_setas_e_area_de_toque_h10(usuario_auth):
    marcador = uuid.uuid4().hex[:8]
    db = SessionLocal()
    t = Tender(source_id="pncp-source-01", external_id=f"MOB-{marcador}", title="Certame mobile", object_description="x",
               contracting_entity="Órgão", state="AL", status="Divulgada no PNCP", source_url="https://pncp.gov.br/app/editais",
               estimated_total_value=1000.0, publication_date=agora_sao_paulo_naive())
    db.add(t)
    db.commit()
    tid = t.id
    db.close()
    try:
        client.post("/api/deals", headers=usuario_auth.headers, json={"tender_id": tid, "modo": "global", "stage": "qualificacao"})
        html = client.get("/negocios?view=quadros", headers=usuario_auth.headers).text
        rodape = html[html.index('class="deal-acoes'):]
        assert ">← Voltar</button>" in rodape and ">Avançar →</button>" in rodape
        assert rodape.count("flex-1 h-10 ") >= 3
    finally:
        db = SessionLocal()
        db.query(Deal).filter(Deal.tender_id == tid).delete(synchronize_session=False)
        db.query(Tender).filter(Tender.id == tid).delete(synchronize_session=False)
        db.commit()
        db.close()
