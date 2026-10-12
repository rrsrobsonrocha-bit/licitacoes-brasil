"""Itens via Compras.gov.br por UASG (idCompra) para órgãos municipais/estaduais, disjuntor e botão de sincronização."""
import asyncio
import time
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from server.database import SessionLocal
from server.main import app
from server.models import Tender, TenderItem
from server.search_engine import agora_sao_paulo_naive
from server.services import pncp_compras, sync_items

client = TestClient(app)
CNPJ_MACEIO = "12200135000180"
UASG = "927211"


def _itens_compras(numero_controle, cnpj=CNPJ_MACEIO):
    base = {"materialOuServico": "M", "orcamentoSigiloso": False, "criterioJulgamentoNome": "Menor preço",
            "situacaoCompraItemNome": "Em andamento", "idContratacaoPNCP": numero_controle, "orgaoEntidadeCnpj": cnpj}
    return {"resultado": [
        {**base, "numeroItemPncp": 1, "descricaoResumida": "Notebook 16 GB RAM", "unidadeMedida": "Unidade",
         "quantidade": 40.0, "valorUnitarioEstimado": 5200.0, "valorTotal": 208000.0,
         "tipoBeneficio": "4", "tipoBeneficioNome": "Sem benefício"},
        {**base, "numeroItemPncp": 2, "descricaoResumida": "Monitor 24 polegadas", "unidadeMedida": "Unidade",
         "quantidade": 40.0, "valorUnitarioEstimado": 900.0, "valorTotal": 36000.0,
         "tipoBeneficio": "1", "tipoBeneficioNome": "Participação exclusiva para ME/EPP"},
    ], "totalRegistros": 2}


@pytest.fixture
def municipal():
    """Pregão municipal (Prefeitura de Maceió / IPLAN) disputado no Compras.gov.br."""
    seq = uuid.uuid4().int % 90000 + 100
    numero = f"{CNPJ_MACEIO}-1-{seq:06d}/2026"
    agora = agora_sao_paulo_naive()
    db = SessionLocal()
    t = Tender(
        source_id="pncp-source-01", external_id=numero, title="Aquisição de notebooks para o IPLAN",
        object_description="Aquisição de notebooks e monitores. " * 60, contracting_entity="MUNICIPIO DE MACEIO",
        purchasing_unit="INSTITUTO DE PLANEJAMENTO - IPLAN", state="AL", city="Maceió", administrative_sphere="M",
        modality="Pregão - Eletrônico", notice_number="90015/2026", uasg_code=UASG, status="Divulgada no PNCP",
        estimated_total_value=244000.0, source_url=f"https://pncp.gov.br/app/editais/{CNPJ_MACEIO}/2026/{seq}",
        url_disputa=f"https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra={UASG}05900152026",
        publication_date=agora - timedelta(days=1), proposal_deadline=agora + timedelta(days=10),
    )
    db.add(t)
    db.commit()
    dados = {"id": t.id, "numero": numero, "seq": seq}
    db.close()
    yield dados
    pncp_compras._transport_override = None
    db = SessionLocal()
    db.query(TenderItem).filter(TenderItem.tender_id == dados["id"]).delete(synchronize_session=False)
    db.query(Tender).filter(Tender.id == dados["id"]).delete(synchronize_session=False)
    db.commit()
    db.close()


def _transporte(chamadas, numero, pncp="timeout", por_id=True, por_numero=False, cnpj=CNPJ_MACEIO):
    """PNCP simulado (timeout) e Dados Abertos do Compras.gov.br respondendo por idCompra e/ou numeroControlePNCP."""
    def responder(request):
        url = str(request.url)
        chamadas.append(url)
        if "pncp.gov.br/api/pncp" in url:
            if pncp == "timeout":
                raise httpx.ReadTimeout("PNCP sem resposta", request=request)
            return httpx.Response(200, json=pncp)
        if "dadosabertos.compras.gov.br" in url:
            tipo = parse_qs(urlparse(url).query)["tipo"][0]
            responde = (tipo == "idCompra" and por_id) or (tipo == "numeroControlePNCPCompra" and por_numero)
            return httpx.Response(200, json=_itens_compras(numero, cnpj) if responde else {"resultado": []})
        return httpx.Response(404)
    return httpx.MockTransport(responder)


def _consultas_compras(chamadas):
    return [parse_qs(urlparse(c).query) for c in chamadas if "dadosabertos.compras.gov.br" in c]


def test_municipal_no_compras_gov_recupera_itens_por_uasg_quando_o_pncp_da_timeout(municipal):
    chamadas = []
    pncp_compras._transport_override = _transporte(chamadas, municipal["numero"])
    r = asyncio.run(sync_items.get_or_sync_tender_items(municipal["id"]))
    assert r["status"] == "sincronizado" and r["origem"] == "COMPRAS" and r["compras_gov"] is True
    assert "Itens obtidos no Compras.gov.br" in r["mensagem"] and "ReadTimeout" in r["mensagem"]
    assert "pncp.gov.br/api/pncp" in chamadas[0]  # o PNCP é tentado primeiro
    assert _consultas_compras(chamadas)[0] == {"tipo": ["idCompra"], "codigo": [f"{UASG}05900152026"]}
    assert [(i["item_number"], i["estimated_total_value"]) for i in r["itens"]] == [(1, 208000.0), (2, 36000.0)]

    db = SessionLocal()
    gravados = db.query(TenderItem).filter(TenderItem.tender_id == municipal["id"]).all()
    tender = db.get(Tender, municipal["id"])
    assert len(gravados) == 2 and {i.origem for i in gravados} == {"COMPRAS"}  # persistido na base local
    assert tender.itens_exclusivos_me_epp == 1 and tender.itens_ampla_concorrencia == 1
    db.close()


def test_id_compra_montado_da_uasg_sem_link_da_sala(municipal):
    db = SessionLocal()
    t = db.get(Tender, municipal["id"])
    t.url_disputa = None
    db.commit()
    db.close()
    assert pncp_compras.id_compra_siasg({"uasg_code": UASG, "modality": "Dispensa Eletrônica", "notice_number": "107"},
                                        pncp_compras.IdentificadorCompra(CNPJ_MACEIO, "2026", "5")) == f"{UASG}06001072026"
    assert pncp_compras.id_compra_siasg({"uasg_code": "548", "modality": "Pregão", "notice_number": "1/2026"}) is None
    chamadas = []
    pncp_compras._transport_override = _transporte(chamadas, municipal["numero"])
    r = asyncio.run(sync_items.get_or_sync_tender_items(municipal["id"]))
    assert r["origem"] == "COMPRAS" and r["total"] == 2
    assert _consultas_compras(chamadas)[0]["codigo"] == [f"{UASG}05900152026"]


def test_sem_resultado_por_uasg_tenta_numero_de_controle(municipal):
    chamadas = []
    pncp_compras._transport_override = _transporte(chamadas, municipal["numero"], por_id=False, por_numero=True)
    r = asyncio.run(sync_items.get_or_sync_tender_items(municipal["id"]))
    assert r["origem"] == "COMPRAS" and r["total"] == 2
    assert [c["tipo"][0] for c in _consultas_compras(chamadas)] == ["idCompra", "numeroControlePNCPCompra"]


def test_itens_de_outro_orgao_sao_descartados(municipal):
    chamadas = []
    pncp_compras._transport_override = _transporte(chamadas, municipal["numero"], cnpj="99999999000199")
    r = asyncio.run(sync_items.get_or_sync_tender_items(municipal["id"]))
    assert r["status"] == "indisponivel" and r["itens"] == []
    assert "ainda não tem os itens desta compra" in r["mensagem"]


def test_disjuntor_ativo_vai_direto_ao_compras_gov(municipal):
    pncp_compras._pncp_fora_ate = time.time() + 120
    chamadas = []
    pncp_compras._transport_override = _transporte(chamadas, municipal["numero"])
    dados = client.get(f"/api/tenders/{municipal['id']}/itens").json()
    assert dados["origem"] == "COMPRAS" and dados["total"] == 2
    assert not any("pncp.gov.br/api/pncp" in c for c in chamadas)  # sem esperar o PNCP


def test_forcar_ignora_o_disjuntor_de_120s(municipal):
    db = SessionLocal()
    t = db.get(Tender, municipal["id"])
    t.url_disputa, t.uasg_code = "https://bnccompras.com/Process/ProcessView?param1=x", None  # fora do Compras.gov.br
    db.commit()
    db.close()
    pncp_compras._pncp_fora_ate = time.time() + 120
    chamadas = []
    itens_pncp = [{"numeroItem": 1, "descricao": "Caminhão basculante", "quantidade": 1, "valorUnitarioEstimado": 648000.0,
                   "valorTotal": 648000.0, "unidadeMedida": "Unidade"}]
    pncp_compras._transport_override = _transporte(chamadas, municipal["numero"], pncp=itens_pncp)

    sem_forcar = client.get(f"/api/tenders/{municipal['id']}/itens").json()
    assert sem_forcar["status"] == "indisponivel" and sem_forcar["compras_gov"] is False
    assert not any("pncp.gov.br/api/pncp" in c for c in chamadas)

    forcado = client.get(f"/api/tenders/{municipal['id']}/itens?forcar=true").json()
    assert forcado["status"] == "sincronizado" and forcado["origem"] == "PNCP" and forcado["total"] == 1
    assert any("pncp.gov.br/api/pncp" in c for c in chamadas)


def test_tela_mostra_botao_compras_gov_e_caixas_com_rolagem(municipal):
    html = client.get(f"/tenders/{municipal['id']}").text
    assert "Sincronizar Itens (Compras.gov.br / PNCP)" in html and "Atualizar do PNCP</button>" not in html
    assert "Consultando portal oficial..." in html and "?forcar=true" in html
    assert html.count("max-h-48 overflow-y-auto pr-2 custom-scrollbar") >= 8  # objeto, resumos e cards da IA
    assert 'id="objeto-completo"' in html and ".custom-scrollbar::-webkit-scrollbar" in html

    drawer = client.get(f"/api/tenders/{municipal['id']}/drawer?sincronizar=false").json()
    assert drawer["tender"]["compras_gov"] is True
    pagina_busca = client.get("/search").text
    assert "Sincronizar Itens (Compras.gov.br / PNCP)" in pagina_busca and "/itens?forcar=true" in pagina_busca
