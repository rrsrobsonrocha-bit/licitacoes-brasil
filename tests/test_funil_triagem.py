"""Itens em camadas (banco → PNCP → Compras.gov.br), pré-carregamento estratégico e triagem do "+ Negócio"."""
import asyncio
import uuid
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from server.database import SessionLocal
from server.main import app
from server.models import Deal, Favorite, Tender, TenderItem
from server.search_engine import agora_sao_paulo_naive
from server.services import pncp_compras, sync_items

client = TestClient(app)
CNPJ = "13031547000104"

ITENS_COMPRAS = {
    "resultado": [
        {"numeroItemPncp": 1, "numeroItemCompra": 1, "descricaoResumida": "Bomba centrífuga de água 0,5 CV",
         "materialOuServico": "M", "unidadeMedida": "Unidade", "quantidade": 12.0, "valorUnitarioEstimado": 551.51,
         "valorTotal": 6618.12, "tipoBeneficio": "1", "tipoBeneficioNome": "Participação exclusiva para ME/EPP",
         "criterioJulgamentoNome": "Menor preço", "situacaoCompraItemNome": "Em andamento", "orcamentoSigiloso": False,
         "numeroControlePNCPCompra": f"{CNPJ}-1-000062/2026"},
        {"numeroItemPncp": 2, "numeroItemCompra": 2, "descricaoResumida": "Mangueira de sucção",
         "materialOuServico": "M", "unidadeMedida": "Metro", "quantidade": 50.0, "valorUnitarioEstimado": 20.0,
         "valorTotal": 1000.0, "tipoBeneficio": "4", "tipoBeneficioNome": "Sem benefício", "orcamentoSigiloso": False},
    ],
    "totalRegistros": 2, "totalPaginas": 1, "paginasRestantes": 0,
}


@pytest.fixture
def certame():
    """Certame federal com numeroControlePNCP; itens, negócios e o próprio certame são apagados ao final."""
    sequencial = uuid.uuid4().int % 900000 + 100
    agora = agora_sao_paulo_naive()
    db = SessionLocal()
    t = Tender(
        source_id="pncp-source-01", external_id=f"{CNPJ}-1-{sequencial:06d}/2026", title="Aquisição de bombas",
        object_description="Bombas centrífugas", contracting_entity="Universidade Federal", state="RN",
        status="Divulgada no PNCP", estimated_total_value=9999.99, source_url="https://pncp.gov.br/app/editais",
        publication_date=agora - timedelta(days=1), proposal_deadline=agora + timedelta(days=20),
    )
    db.add(t)
    db.commit()
    dados = {"id": t.id, "external_id": t.external_id, "sequencial": sequencial}
    db.close()
    yield dados
    pncp_compras._transport_override = None
    db = SessionLocal()
    db.query(Deal).filter(Deal.tender_id == dados["id"]).delete(synchronize_session=False)
    db.query(TenderItem).filter(TenderItem.tender_id == dados["id"]).delete(synchronize_session=False)
    db.query(Tender).filter(Tender.id == dados["id"]).delete(synchronize_session=False)
    db.commit()
    db.close()


def _transporte(chamadas, pncp="timeout", compras=ITENS_COMPRAS):
    def responder(request):
        url = str(request.url)
        chamadas.append(url)
        if "pncp.gov.br/api/pncp" in url:
            if pncp == "timeout":
                raise httpx.ReadTimeout("PNCP sem resposta", request=request)
            if pncp == "404":
                return httpx.Response(404)
            return httpx.Response(200, json=pncp)
        if "dadosabertos.compras.gov.br" in url:
            if compras == "timeout":
                raise httpx.ReadTimeout("Compras sem resposta", request=request)
            return httpx.Response(200, json=compras)
        return httpx.Response(404)
    return httpx.MockTransport(responder)


def _gravar_itens(tender_id, origem="PNCP"):
    db = SessionLocal()
    itens = [TenderItem(tender_id=tender_id, item_number=n, description=f"Item {n}", quantity=n * 10,
                        estimated_unit_value=10.0, estimated_total_value=valor, origem=origem)
             for n, valor in ((1, 100.0), (2, 250.5), (3, 1000.0))]
    db.add_all(itens)
    db.commit()
    ids = [i.id for i in itens]
    db.close()
    return ids


# ------------------------------------------------------------------------------
# 1. Camadas: banco local, PNCP, Compras.gov.br
# ------------------------------------------------------------------------------
def test_itens_ja_gravados_respondem_sem_rede(certame):
    _gravar_itens(certame["id"])
    chamadas = []
    pncp_compras._transport_override = _transporte(chamadas)
    r = asyncio.run(sync_items.get_or_sync_tender_items(certame["id"]))
    assert chamadas == []  # nenhuma chamada de rede
    assert r["status"] == "cache" and r["origem"] == "PNCP" and r["total"] == 3
    assert r["tempo_ms"] < 500


def test_fallback_compras_gov_quando_o_pncp_falha_e_grava_no_banco(certame):
    chamadas = []
    pncp_compras._transport_override = _transporte(chamadas, pncp="timeout")
    r = asyncio.run(sync_items.get_or_sync_tender_items(certame["external_id"]))
    assert r["status"] == "sincronizado" and r["origem"] == "COMPRAS"
    assert "Compras.gov.br" in r["mensagem"]
    assert [i["item_number"] for i in r["itens"]] == [1, 2]
    bomba = r["itens"][0]
    assert bomba["description"] == "Bomba centrífuga de água 0,5 CV" and bomba["quantity"] == 12.0
    assert bomba["estimated_total_value"] == 6618.12 and bomba["beneficio_tipo"] == "EXCLUSIVO_ME_EPP"
    assert "pncp.gov.br/api/pncp" in chamadas[0]
    assert f"codigo={CNPJ}-1-{certame['sequencial']:06d}%2F2026" in chamadas[1]

    db = SessionLocal()
    gravados = db.query(TenderItem).filter(TenderItem.tender_id == certame["id"]).all()
    tender = db.get(Tender, certame["id"])
    assert {i.origem for i in gravados} == {"COMPRAS"} and len(gravados) == 2
    assert tender.itens_exclusivos_me_epp == 1 and tender.itens_ampla_concorrencia == 1
    db.close()

    # Próxima consulta: instantânea, direto do banco
    chamadas.clear()
    assert asyncio.run(sync_items.get_or_sync_tender_items(certame["id"]))["status"] == "cache"
    assert chamadas == []


def test_sem_fallback_quando_o_pncp_diz_que_a_compra_nao_existe(certame):
    chamadas = []
    pncp_compras._transport_override = _transporte(chamadas, pncp="404")
    r = asyncio.run(sync_items.get_or_sync_tender_items(certame["id"]))
    assert r["status"] == "nao_encontrado" and len(chamadas) == 1


def test_pncp_e_compras_fora_do_ar_nao_inventam_itens(certame):
    chamadas = []
    pncp_compras._transport_override = _transporte(chamadas, pncp="timeout", compras={"resultado": []})
    r = asyncio.run(sync_items.get_or_sync_tender_items(certame["id"]))
    assert r["status"] == "indisponivel" and r["itens"] == []
    with pytest.raises(sync_items.CertameNaoEncontrado):
        asyncio.run(sync_items.get_or_sync_tender_items("nao-existe"))


def test_pncp_responde_e_compras_nao_e_consultado(certame):
    chamadas = []
    itens_pncp = [{"numeroItem": 1, "descricao": "Bomba", "quantidade": 1, "valorUnitarioEstimado": 10, "valorTotal": 10}]
    pncp_compras._transport_override = _transporte(chamadas, pncp=itens_pncp)
    r = asyncio.run(sync_items.get_or_sync_tender_items(certame["id"]))
    assert r["origem"] == "PNCP" and r["total"] == 1
    assert not any("compras.gov.br" in c for c in chamadas)


# ------------------------------------------------------------------------------
# 2. Pré-carregamento estratégico
# ------------------------------------------------------------------------------
def test_certames_estrategicos_priorizam_sessao_proxima_favoritos_al_e_dispensas(usuario_auth):
    agora = agora_sao_paulo_naive()
    marcador = uuid.uuid4().hex[:8]
    db = SessionLocal()

    def criar(sufixo, **campos):
        dados = dict(source_id="pncp-source-01", external_id=f"EST-{marcador}-{sufixo}", title=f"Estratégico {sufixo}",
                     object_description="x", contracting_entity="Órgão", state="SP", status="Divulgada no PNCP",
                     source_url="https://pncp.gov.br/app/editais", publication_date=agora - timedelta(days=1),
                     proposal_deadline=agora + timedelta(days=30), modality="Pregão Eletrônico")
        dados.update(campos)
        t = Tender(**dados)
        db.add(t)
        db.flush()
        return t.id

    try:
        sessao = criar("sessao", proposal_deadline=agora + timedelta(hours=30))
        favorito = criar("favorito")
        alagoas = criar("al", state="AL")
        dispensa = criar("dispensa", modality="Dispensa Eletrônica")
        comum = criar("comum")
        ja_tem = criar("ja-tem", proposal_deadline=agora + timedelta(hours=10))
        db.add(Favorite(user_id=usuario_auth.id, tender_id=favorito))
        db.add(TenderItem(tender_id=ja_tem, item_number=1, description="x", origem="PNCP"))
        db.commit()

        ids = sync_items.certames_estrategicos(db, limite=100000)
        assert {sessao, favorito, alagoas, dispensa} <= set(ids)
        assert comum not in ids and ja_tem not in ids  # sem prioridade / itens já gravados
        assert ids.index(sessao) < ids.index(favorito) < ids.index(alagoas) < ids.index(dispensa)
    finally:
        db.rollback()
        db.query(Favorite).filter(Favorite.user_id == usuario_auth.id).delete(synchronize_session=False)
        criados = [t for (t,) in db.query(Tender.id).filter(Tender.external_id.like(f"EST-{marcador}-%"))]
        db.query(TenderItem).filter(TenderItem.tender_id.in_(criados)).delete(synchronize_session=False)
        db.query(Tender).filter(Tender.id.in_(criados)).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_pre_carregamento_grava_itens_com_pausas_entre_lotes(certame, monkeypatch):
    chamadas, pausas = [], []
    pncp_compras._transport_override = _transporte(chamadas, pncp="timeout")
    monkeypatch.setattr(sync_items, "certames_estrategicos", lambda db, limite: [certame["id"]] * 3)
    monkeypatch.setattr(sync_items, "ITENS_POR_LOTE", 2)
    monkeypatch.setattr(sync_items, "PAUSA_ENTRE_LOTES_SEGUNDOS", 1.5)
    resumo = sync_items.pre_carregar_itens_estrategicos(limite=3, dormir=pausas.append)
    assert resumo["fila"] == 3 and resumo["sincronizados"] == 1 and resumo["via_compras"] == 1
    assert pausas == [1.5]  # uma pausa extra após o lote de 2
    db = SessionLocal()
    assert db.query(TenderItem).filter(TenderItem.tender_id == certame["id"], TenderItem.origem == "COMPRAS").count() == 2
    db.close()


def test_pre_carregamento_para_quando_pedido(certame, monkeypatch):
    monkeypatch.setattr(sync_items, "certames_estrategicos", lambda db, limite: [certame["id"]] * 4)
    resumo = sync_items.pre_carregar_itens_estrategicos(limite=4, parar=lambda: True, dormir=lambda s: None)
    assert resumo["pendentes"] == 4 and resumo["sincronizados"] == 0


# ------------------------------------------------------------------------------
# 3. Funil: itens selecionados e modo global
# ------------------------------------------------------------------------------
def test_negocio_com_itens_selecionados_soma_valores_e_vincula_registros(certame, usuario_auth):
    ids = _gravar_itens(certame["id"])
    resp = client.post("/api/deals", headers=usuario_auth.headers, json={
        "tender_id": certame["id"], "item_ids": [ids[0], ids[2]], "stage": "qualificacao",
        "notes": "Lance até 8% abaixo da referência"})
    assert resp.status_code == 201, resp.text
    deal = resp.json()
    assert deal["modo"] == "itens" and deal["selected_items_count"] == 2
    assert [i["item_number"] for i in deal["selected_items"]] == [1, 3]
    assert [i["tender_item_id"] for i in deal["selected_items"]] == [ids[0], ids[2]]
    assert deal["valor_negocio"] == 1100.0
    assert deal["stage"] == "qualificacao" and deal["notes"] == "Lance até 8% abaixo da referência"

    # Item de outro certame não entra
    assert client.post("/api/deals", headers=usuario_auth.headers,
                       json={"tender_id": certame["id"], "item_ids": ["id-de-outro-certame"]}).status_code == 422


def test_negocio_em_modo_global_de_contingencia(certame, usuario_auth):
    resp = client.post("/api/deals", headers=usuario_auth.headers,
                       json={"tender_id": certame["id"], "modo": "global"})
    assert resp.status_code == 201, resp.text
    deal = resp.json()
    assert deal["modo"] == "global" and deal["selected_items"] is None
    assert deal["valor_negocio"] == 9999.99 and deal["stage"] == "oportunidades"

    # Depois que os itens sincronizam, o operador pode refinar a seleção no mesmo negócio
    ids = _gravar_itens(certame["id"])
    refinado = client.post("/api/deals", headers=usuario_auth.headers,
                           json={"tender_id": certame["id"], "item_ids": [ids[1]]}).json()
    assert refinado["id"] == deal["id"] and refinado["modo"] == "itens" and refinado["valor_negocio"] == 250.5


def test_rota_de_itens_usa_o_fallback(certame):
    pncp_compras._transport_override = _transporte([], pncp="timeout")
    dados = client.get(f"/api/tenders/{certame['id']}/itens").json()
    assert dados["origem"] == "COMPRAS" and dados["total"] == 2 and all(i["id"] for i in dados["itens"])


# ------------------------------------------------------------------------------
# 4. Gaveta de triagem
# ------------------------------------------------------------------------------
def test_busca_abre_gaveta_de_triagem_no_mais_negocio():
    html = client.get("/search").text
    for marcador in ("abrirTriagemFunil(", "data-negocio-btn=", "Adicionar ao Funil de Negócios",
                     "Adicionar Itens Selecionados ao Funil", "Marcar Todos", "Desmarcar Todos",
                     "Valor Estimado Selecionado", "Itens ainda não sincronizados pelos portais governamentais.",
                     "Tentar Sincronizar Agora (PNCP / Compras)", "Adicionar Edital Inteiro ao Funil (Modo Global)",
                     'id="deal-etapa"', 'id="deal-notas"', "item_ids"):
        assert marcador in html, marcador
