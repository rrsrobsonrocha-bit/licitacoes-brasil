"""Drawer lateral de detalhes, realce de busca, filtros avançados (ME/EPP, esfera, portal) e minutas legais."""
import io
import uuid
import zipfile
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from server.config import settings
from server.database import SessionLocal
from server.main import app, valor_compacto
from server.models import Tender, TenderItem
from server.search_engine import agora_sao_paulo_naive, normalizar_esfera, portal_de_disputa
from server.services import minutas, pncp_compras

client = TestClient(app)
CNPJ = "12265468000197"


@pytest.fixture(autouse=True)
def pncp_isolado():
    pncp_compras._falhas_recentes.clear()
    yield
    pncp_compras._falhas_recentes.clear()
    pncp_compras._transport_override = None


def _criar(db, marcador, **campos):
    agora = agora_sao_paulo_naive()
    dados = dict(
        source_id="pncp-source-01", external_id=f"{marcador}-{uuid.uuid4().hex[:8]}",
        title=f"Aquisição de medicamentos {marcador}", object_description="Objeto oficial", contracting_entity="Órgão",
        state="SP", status="Divulgada no PNCP", source_url="https://pncp.gov.br/app/editais",
        publication_date=agora - timedelta(days=3), proposal_deadline=agora + timedelta(days=10),
    )
    dados.update(campos)
    t = Tender(**dados)
    db.add(t)
    db.commit()
    return t.id


def _apagar(marcador):
    db = SessionLocal()
    try:
        db.query(Tender).filter(Tender.external_id.like(f"%{marcador}%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


# ---------------- a) API do drawer: cabeçalho, itens e arquivos ----------------
ITENS_PNCP = [
    {"numeroItem": 1, "descricao": "Dipirona sódica 500 mg comprimido", "materialOuServico": "M",
     "unidadeMedida": "Comprimido", "quantidade": 12000, "valorUnitarioEstimado": 0.12, "valorTotal": 1440.0,
     "tipoBeneficio": 1, "tipoBeneficioNome": "Participação exclusiva para ME/EPP", "orcamentoSigiloso": False},
    {"numeroItem": 2, "descricao": "Serviço de entrega fracionada de medicamentos", "materialOuServico": "S",
     "unidadeMedida": "Serviço", "quantidade": 1, "valorUnitarioEstimado": 250000.0, "valorTotal": 250000.0,
     "tipoBeneficio": 4, "tipoBeneficioNome": "Sem benefício", "orcamentoSigiloso": False},
    {"numeroItem": 3, "descricao": "Amoxicilina 500 mg cápsula", "materialOuServico": "M",
     "unidadeMedida": "Cápsula", "quantidade": 5000, "valorUnitarioEstimado": 0.5, "valorTotal": 2500.0,
     "tipoBeneficio": 3, "tipoBeneficioNome": "Cota reservada para ME/EPP", "orcamentoSigiloso": False},
]


@pytest.fixture
def certame_drawer(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_LOCAL_PATH", str(tmp_path / "storage"))
    sequencial = str(800000 + uuid.uuid4().int % 99999)
    marcador = f"DRW{uuid.uuid4().hex[:6]}"
    db = SessionLocal()
    try:
        tid = _criar(
            db, marcador, external_id=f"{CNPJ}-1-{sequencial.zfill(6)}/2026-{marcador}",
            url_pncp=f"https://pncp.gov.br/app/editais/{CNPJ}/2026/{sequencial}",
            contracting_entity="Prefeitura Municipal de Maceió", purchasing_unit="Secretaria Municipal de Saúde",
            city="Maceió", state="AL", modality="Pregão Eletrônico", administrative_sphere="M",
            estimated_total_value=1_250_000.0, url_disputa="https://licitanet.com.br/sessao/201855",
            notice_number=None, process_number="2026/0451",
        )
    finally:
        db.close()
    chamadas = []
    base_arquivo = f"https://pncp.gov.br/pncp-api/v1/orgaos/{CNPJ}/compras/2026/{sequencial}/arquivos"

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append(str(request.url))
        caminho = request.url.path
        if caminho.endswith(f"/compras/2026/{sequencial}/itens"):
            return httpx.Response(200, json=ITENS_PNCP)
        if caminho.endswith(f"/compras/2026/{sequencial}/arquivos"):
            return httpx.Response(200, json=[
                {"url": f"{base_arquivo}/1", "statusAtivo": True, "sequencialDocumento": 1, "titulo": "Edital",
                 "tipoDocumentoNome": "Edital", "dataPublicacaoPncp": "2026-10-06T20:19:00"},
                {"url": f"{base_arquivo}/2", "statusAtivo": True, "sequencialDocumento": 2,
                 "titulo": "Termo de Referência", "tipoDocumentoNome": "Termo de Referência"},
                {"url": f"{base_arquivo}/3", "statusAtivo": True, "sequencialDocumento": 3,
                 "titulo": "Minuta do contrato", "tipoDocumentoNome": "Minuta do Contrato"},
            ])
        if caminho.endswith("/arquivos/1"):
            return httpx.Response(200, content=b"%PDF-1.4 edital", headers={"content-type": "application/pdf"})
        return httpx.Response(404)

    pncp_compras._transport_override = httpx.MockTransport(handler)
    yield {"id": tid, "sequencial": sequencial, "chamadas": chamadas}
    _apagar(marcador)


def test_api_do_drawer_entrega_cabecalho_itens_e_arquivos(certame_drawer):
    resp = client.get(f"/api/tenders/{certame_drawer['id']}/drawer")
    assert resp.status_code == 200
    dados = resp.json()

    t = dados["tender"]
    assert t["orgao"] == "Prefeitura Municipal de Maceió" and (t["municipio"], t["uf"]) == ("Maceió", "AL")
    assert t["modalidade"] == "Pregão Eletrônico" and t["numero_ano"] == f"{certame_drawer['sequencial']}/2026"
    assert (t["esfera"], t["esfera_rotulo"]) == ("MUNICIPAL", "Municipal")
    assert t["valor_estimado"] == 1_250_000.0 and t["valor_estimado_compacto"] == "R$ 1,2 mi"
    assert " às " in t["data_limite_formatada"]
    assert t["url_pncp"] == f"https://pncp.gov.br/app/editais/{CNPJ}/2026/{certame_drawer['sequencial']}"
    assert t["url_portal"] == "https://licitanet.com.br/sessao/201855" and t["portal_nome"] == "Licitanet"
    assert t["em_negocios"] is False

    itens = dados["itens"]
    assert itens["status"] == "sincronizado" and itens["total"] == 3
    por_numero = {i["item_number"]: i for i in itens["lista"]}
    assert (por_numero[1]["tipo_item"], por_numero[1]["beneficio_rotulo"]) == ("Material", "Exclusivo para ME/EPP")
    assert (por_numero[2]["tipo_item"], por_numero[2]["beneficio_rotulo"]) == ("Serviço", "Ampla Concorrência")
    assert por_numero[3]["beneficio_tipo"] == "COTA_RESERVADA_ME_EPP"
    assert (por_numero[1]["unit"], por_numero[1]["quantity"], por_numero[1]["estimated_unit_value"]) == ("Comprimido", 12000, 0.12)
    assert por_numero[1]["estimated_total_value"] == 1440.0
    # O resumo de benefício fica no certame (base do filtro da busca)
    assert (t["itens_exclusivos_me_epp"], t["itens_ampla_concorrencia"]) == (2, 1)

    arquivos = dados["arquivos"]
    assert arquivos["total"] == 3 and arquivos["zip_url"]
    assert [a["document_type"] for a in arquivos["lista"]] == ["Edital", "Termo de Referência", "Minuta do Contrato"]
    assert all(a["download_url"].startswith(f"/api/tenders/{certame_drawer['id']}/arquivos/") for a in arquivos["lista"])
    download = client.get(arquivos["lista"][0]["download_url"])
    assert download.status_code == 200 and download.content.startswith(b"%PDF")

    # Segunda abertura usa o cache: nenhuma nova consulta de itens/arquivos ao PNCP
    antes = [c for c in certame_drawer["chamadas"] if c.endswith(("/itens", "/arquivos")) or "/itens?" in c]
    assert client.get(f"/api/tenders/{certame_drawer['id']}/drawer").status_code == 200
    depois = [c for c in certame_drawer["chamadas"] if c.endswith(("/itens", "/arquivos")) or "/itens?" in c]
    assert len(depois) == len(antes)


def test_drawer_marca_certame_ja_no_funil_e_404(certame_drawer, criar_usuario):
    conta = criar_usuario("Conta Drawer")
    client.post("/api/deals", json={"tender_id": certame_drawer["id"]}, headers=conta.headers)
    assert client.get(f"/api/tenders/{certame_drawer['id']}/drawer", headers=conta.headers).json()["tender"]["em_negocios"] is True
    assert client.get("/api/tenders/nao-existe/drawer").status_code == 404


def test_drawer_sem_resposta_do_pncp_nao_inventa_itens(certame_drawer):
    def fora_do_ar(request):
        raise httpx.ConnectTimeout("timeout", request=request)

    pncp_compras._transport_override = httpx.MockTransport(fora_do_ar)
    dados = client.get(f"/api/tenders/{certame_drawer['id']}/drawer").json()
    assert dados["itens"]["status"] == "indisponivel" and dados["itens"]["lista"] == []
    assert dados["arquivos"]["status"] == "indisponivel" and dados["arquivos"]["lista"] == []
    assert dados["tender"]["itens_exclusivos_me_epp"] is None


def test_paginas_incluem_drawer_e_realce(criar_usuario):
    busca = client.get("/search?q=medicamentos").text
    for marcador in ('id="tender-drawer"', 'id="drawer-sec-itens"', 'id="drawer-sec-arquivos"', "Abrir no PNCP",
                     "Abrir no Portal", "Resumo com IA", "Tenho Interesse", "/drawer?sincronizar=false`", "mark.lb-realce",
                     "data-realce", 'id="deal-items-modal"'):
        assert marcador in busca, marcador
    conta = criar_usuario("Conta Painel")
    painel = client.get("/dashboard", cookies=conta.cookies).text
    assert 'id="tender-drawer"' in painel and "data-abrir-drawer" in painel


def test_valor_compacto_e_classificacao_de_beneficio():
    assert [valor_compacto(v) for v in (None, 950, 1500, 250_000, 999_999, 1_250_000, 2.5e9)] == [
        None, "R$ 950,00", "R$ 1,5 mil", "R$ 250 mil", "R$ 1 mi", "R$ 1,2 mi", "R$ 2,5 bi"]
    classificar = pncp_compras.classificar_beneficio
    assert classificar({"tipoBeneficio": 1}) == "EXCLUSIVO_ME_EPP"
    assert classificar({"tipoBeneficioId": 3}) == "COTA_RESERVADA_ME_EPP"
    assert classificar({"tipoBeneficio": 2}) == classificar({"tipoBeneficio": 5}) == "AMPLA_CONCORRENCIA"
    assert classificar(None, "Participação exclusiva para ME/EPP") == "EXCLUSIVO_ME_EPP"
    assert classificar({}, None) is None


# ---------------- b) Filtros: benefício ME/EPP, esfera e portal ----------------
@pytest.fixture
def catalogo_filtros():
    marcador = f"FLT{uuid.uuid4().hex[:6]}"
    db = SessionLocal()
    try:
        ids = {
            "me_epp_municipal": _criar(db, marcador, administrative_sphere="M", itens_exclusivos_me_epp=2, itens_ampla_concorrencia=0,
                                       url_disputa="https://bnccompras.com/Process/ProcessView?param1=x"),
            "ampla_federal": _criar(db, marcador, administrative_sphere="F", itens_exclusivos_me_epp=0, itens_ampla_concorrencia=5,
                                    url_disputa="https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/x"),
            "misto_estadual": _criar(db, marcador, administrative_sphere="ESTADUAL", itens_exclusivos_me_epp=1, itens_ampla_concorrencia=3,
                                     url_disputa="https://licitanet.com.br/sessao/1"),
            "nao_analisado_distrital": _criar(db, marcador, administrative_sphere="D"),
            "consorcio": _criar(db, marcador, administrative_sphere="N", url_disputa="https://pncp.gov.br/app/editais/1/2026/1"),
        }
    finally:
        db.close()
    yield marcador, ids
    _apagar(marcador)


def _ids(marcador, extra):
    resp = client.get(f"/api/tenders?q={marcador}&page_size=100&{extra}")
    assert resp.status_code == 200, resp.text
    return {r["id"] for r in resp.json()["results"]}


def test_filtro_itens_exclusivos_me_epp_e_ampla_concorrencia(catalogo_filtros):
    marcador, ids = catalogo_filtros
    assert _ids(marcador, "beneficio=ME_EPP") == {ids["me_epp_municipal"], ids["misto_estadual"]}
    assert _ids(marcador, "beneficio=AMPLA") == {ids["ampla_federal"], ids["misto_estadual"]}
    # As duas caixas marcadas: qualquer uma (certames ainda não analisados ficam de fora)
    ambos = _ids(marcador, "beneficio=ME_EPP&beneficio=AMPLA")
    assert ambos == {ids["me_epp_municipal"], ids["ampla_federal"], ids["misto_estadual"]}
    assert _ids(marcador, "beneficio=me_epp,ampla") == ambos
    assert len(_ids(marcador, "")) == 5


def test_filtro_esfera_governamental(catalogo_filtros):
    marcador, ids = catalogo_filtros
    assert _ids(marcador, "esfera=FEDERAL") == {ids["ampla_federal"]}  # gravado como "F" pelo PNCP
    assert _ids(marcador, "esfera=ESTADUAL") == {ids["misto_estadual"]}  # gravado por extenso (legado)
    assert _ids(marcador, "esfera=MUNICIPAL&esfera=DISTRITAL") == {ids["me_epp_municipal"], ids["nao_analisado_distrital"]}
    assert _ids(marcador, "esfera=OUTROS") == {ids["consorcio"]}
    assert _ids(marcador, "administrative_sphere=FEDERAL") == {ids["ampla_federal"]}  # parâmetro legado
    # Combinação com o benefício
    assert _ids(marcador, "beneficio=ME_EPP&esfera=MUNICIPAL") == {ids["me_epp_municipal"]}
    assert [normalizar_esfera(v) for v in ("F", "Estadual", "m", "D", "N", None)] == [
        "FEDERAL", "ESTADUAL", "MUNICIPAL", "DISTRITAL", "OUTROS", None]


def test_filtro_portal_de_disputa(catalogo_filtros):
    marcador, ids = catalogo_filtros
    assert _ids(marcador, "portal_disputa=BNC") == {ids["me_epp_municipal"]}
    assert _ids(marcador, "portal_disputa=COMPRAS_GOV&portal_disputa=LICITANET") == {ids["ampla_federal"], ids["misto_estadual"]}
    resultado = client.get(f"/api/tenders?q={marcador}&portal_disputa=LICITANET").json()["results"][0]
    assert resultado["portal_disputa"] == "Licitanet" and resultado["esfera"] == "ESTADUAL"
    assert resultado["itens_exclusivos_me_epp"] == 1
    assert portal_de_disputa("https://pncp.gov.br/app/editais/x") is None
    assert portal_de_disputa("https://www.licitasalto.com.br/a")["nome"] == "licitasalto.com.br"


def test_pagina_de_busca_mantem_filtros_avancados_marcados(catalogo_filtros):
    marcador, ids = catalogo_filtros
    html = client.get(f"/search?q={marcador}&beneficio=ME_EPP&esfera=MUNICIPAL&administrative_sphere=F&portal_disputa=BNC").text
    assert 'name="beneficio" value="ME_EPP" checked' in html
    assert 'name="esfera" value="MUNICIPAL" checked' in html and 'name="esfera" value="FEDERAL" checked' in html
    assert 'name="portal_disputa" value="BNC" checked' in html
    assert "Exclusivo ME/EPP" in html and 'class="badge-me-epp' in html
    # Paginação e atalhos preservam os filtros (vírgula = qualquer um)
    assert "beneficio=ME_EPP" in html and "esfera=MUNICIPAL%2CFEDERAL" in html


# ---------------- c) Minutas e templates legais ----------------
def _texto_docx(conteudo: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(conteudo)) as zf:
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml"} <= set(zf.namelist())
        return zf.read("word/document.xml").decode("utf-8")


def test_lista_de_minutas():
    resp = client.get("/api/minutas")
    assert resp.status_code == 200
    titulos = [m["titulo"] for m in resp.json()["minutas"]]
    assert titulos == [
        "Declaração de Enquadramento como ME/EPP",
        "Declaração de Inexistência de Fato Impeditivo",
        "Declaração de Cumprimento dos Requisitos de Habilitação",
        "Modelo de Proposta Comercial Readequada",
    ]


@pytest.mark.parametrize("slug,trecho", [
    ("enquadramento-me-epp", "Lei Complementar nº 123"),
    ("inexistencia-fato-impeditivo", "art. 14 da Lei nº 14.133/2021"),
    ("cumprimento-requisitos-habilitacao", "art. 63, inciso I"),
    ("proposta-comercial-readequada", "VALOR GLOBAL DA PROPOSTA"),
])
def test_download_das_minutas_em_docx_editavel(slug, trecho):
    resp = client.get(f"/api/minutas/{slug}/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == minutas.MIME_DOCX
    assert resp.headers["content-disposition"] == f'attachment; filename="{slug}.docx"'
    xml = _texto_docx(resp.content)
    assert trecho in xml and minutas.MINUTAS[slug].titulo.upper() in xml
    assert "[RAZÃO SOCIAL]" in xml and 'w:highlight w:val="yellow"' in xml  # campos a preencher realçados


def test_minuta_preenchida_com_dados_oficiais_do_certame():
    marcador = f"MIN{uuid.uuid4().hex[:6]}"
    db = SessionLocal()
    try:
        tid = _criar(db, marcador, contracting_entity="Fundo Municipal de Saúde de Arapiraca", modality="Pregão Eletrônico",
                     notice_number="90012/2026", process_number="2026.0007")
        db.add(TenderItem(tender_id=tid, item_number=1, description="Luva de procedimento <látex>", unit="Caixa", quantity=300))
        db.commit()
    finally:
        db.close()
    try:
        resp = client.get(f"/api/minutas/proposta-comercial-readequada/download?tender_id={tid}")
        assert resp.status_code == 200
        assert resp.headers["content-disposition"] == 'attachment; filename="proposta-comercial-readequada_pregao-eletronico-no-90012-2026.docx"'
        xml = _texto_docx(resp.content)
        assert "Fundo Municipal de Saúde de Arapiraca" in xml and "Pregão Eletrônico nº 90012/2026" in xml
        assert "Processo nº 2026.0007" in xml
        assert "Luva de procedimento &lt;látex&gt;" in xml and ">Caixa<" in xml and ">300<" in xml

        assert client.get("/api/minutas/nao-existe/download").status_code == 404
        assert client.get("/api/minutas/enquadramento-me-epp/download?tender_id=nao-existe").status_code == 404

        juridico = client.get(f"/juridico?tender_id={tid}").text
        assert 'id="minutas"' in juridico
        assert f"/api/minutas/enquadramento-me-epp/download?tender_id={tid}" in juridico
    finally:
        _apagar(marcador)


def test_minutas_exigem_login_com_paywall(monkeypatch):
    monkeypatch.setattr(settings, "ACESSO_EXIGE_LOGIN", True)
    assert client.get("/api/minutas").status_code == 401
    assert client.get("/api/minutas/enquadramento-me-epp/download").status_code == 401
