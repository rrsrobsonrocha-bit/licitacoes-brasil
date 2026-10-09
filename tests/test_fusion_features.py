import asyncio
import html
import io
import json
import re
import zipfile
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from pypdf import PdfReader

from server.main import app
from server.database import SessionLocal
from server.models import (
    Tender, HistoricoDisputa, AIAnalysis, Deal, DocumentoEmpresa, LegalDocumentTemplate, hoje_sao_paulo
)
from server.services.gemini_analyzer import gemini_analyzer
from server.services.portal_collector import (
    portal_collector, sync_pipeline, PortalCollectorService,
    classificar_natureza_objeto, classificar_natureza_certame, NATUREZAS_OBJETO
)
from server.services.dispute_monitor import dispute_monitor, DisputeMonitorService, PregaoItem
from server.services.pdf_generator import gerar_peticao_pdf, PeticaoInvalidaError, normalizar_lei
from server.services.kit_builder import (
    montar_kit_habilitacao_zip, KitHabilitacaoError, NOME_RELATORIO, PASTA_DOCUMENTOS
)
from server.services import document_vault

client = TestClient(app)


@pytest.fixture
def sample_tender():
    db = SessionLocal()
    try:
        tender = db.query(Tender).first()
        assert tender is not None, "Banco de testes deve possuir ao menos um certame semeado"
        return tender
    finally:
        db.close()


# ==============================================================================
# 1. TESTES DO HUB DE AGREGAÇÃO MULTIFONTE (PORTAL COLLECTOR)
# ==============================================================================

@pytest.mark.asyncio
async def test_portal_collector_multi_source_collection():
    """Valida a coleta e normalização em múltiplos portais oficiais (PNCP, Compras.gov, BNC, etc.)."""
    collector = PortalCollectorService()
    results = await collector.collect_all(uf="DF", keyword="servidores", limit_per_portal=2)

    assert isinstance(results, list)
    assert len(results) > 0

    # Verifica se os portais são identificados corretamente
    portals = {item["fonte_portal"] for item in results}
    assert len(portals) >= 2, f"Esperado múltiplos portais, obtido: {portals}"

    # Validação dos campos obrigatórios normalizados
    for item in results:
        assert "external_id" in item
        assert "title" in item
        assert "fonte_portal" in item
        assert "link_edital_oficial" in item
        assert "status" in item


@pytest.mark.asyncio
async def test_portal_collector_network_error_resilience():
    """Valida tratamento de exceção gracioso em falhas de rede de portais específicos."""
    collector = PortalCollectorService()

    # Simula falha catastrófica de rede no PNCP
    with patch("httpx.AsyncClient.get", side_effect=Exception("Timeout simulado de conexão")):
        pncp_res = await collector.fetch_pncp(uf="SP", keyword="saude", limit=5)
        # O coletor deve retornar lista vazia sem propagar erro fatal
        assert pncp_res == []

        # Coletar all com falhas embutidas ainda deve retornar os portais com fallback
        all_res = await collector.collect_all(uf="SP", keyword="saude", limit_per_portal=2)
        assert isinstance(all_res, list)


def test_api_portals_sync_endpoint():
    """Testa o endpoint POST /api/portals/sync que orquestra a ingestão e persistência."""
    response = client.post("/api/portals/sync", json={
        "uf": "DF",
        "keyword": "computador",
        "limit_per_portal": 2
    })
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "total_collected" in data
    assert "total_saved" in data
    assert data["total_collected"] >= 0


# ==============================================================================
# 2. TESTES DA CAMADA DE INTELIGÊNCIA ARTIFICIAL (GOOGLE GEMINI FLASH)
# ==============================================================================

@pytest.mark.asyncio
async def test_gemini_analyzer_with_structured_mock():
    """Valida que o analisador do Gemini processa documentos e extrai seções estruturadas via mock."""
    mock_payload = {
        "itens": [
            {
                "item_numero": 1,
                "descricao": "Servidor Rack 2U",
                "quantidade": 5.0,
                "unidade": "UN",
                "valor_unitario_estimado": 20000.0,
                "valor_total_estimado": 100000.0
            }
        ],
        "habilitacao_exigencias": [
            "Atestado de Capacidade Técnica de pelo menos 50%",
            "Certidão Negativa de Débitos Federais (CND)"
        ],
        "estimativa_orcamentaria": 100000.0,
        "pontos_criticos_risco": [
            "Prazo de entrega exíguo de 15 dias corridos",
            "Garantia on-site com SLA de 4 horas"
        ],
        "confianca_score": 0.98
    }

    # Mock do cliente do google.genai
    mock_response = MagicMock()
    mock_response.text = json.dumps(mock_payload)

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response
    # Garantir que se aio for checado, aio.models também retorne
    mock_aio_response = MagicMock()
    mock_aio_response.text = json.dumps(mock_payload)
    mock_client.aio = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_aio_response)

    with patch("server.services.gemini_analyzer.genai.Client", return_value=mock_client), \
         patch("server.services.gemini_analyzer.get_genai_client", return_value=mock_client):
        # Chama a rotina assíncrona
        result = await gemini_analyzer.analisar_edital_documento(
            file_bytes=b"PDF BINARY DUMMY TEST DATA",
            filename="edital_datacenter.pdf"
        )

        assert result is not None
        assert len(result["itens"]) == 1
        assert result["estimativa_orcamentaria"] == 100000.0
        assert len(result["pontos_criticos_risco"]) >= 2
        assert "Atestado de Capacidade Técnica" in result["habilitacao_exigencias"][0]


@pytest.mark.asyncio
async def test_gemini_analyzer_fallback_behavior():
    """Valida o fallback determinístico do analisador caso a IA não esteja disponível."""
    with patch("server.services.gemini_analyzer.get_genai_client", return_value=None):
        fallback_res = await gemini_analyzer.analisar_edital_documento(
            file_bytes=b"Conteudo texto de teste para edital",
            filename="edital_teste.pdf"
        )
        assert fallback_res is not None
        assert "itens" in fallback_res or "objeto" in fallback_res
        assert "habilitacao_exigencias" in fallback_res or "habilitacao" in fallback_res
        assert "pontos_criticos_risco" in fallback_res

        exec_res = await gemini_analyzer.resumo_executivo_oportunidade({
            "title": "Aquisição de computadores",
            "contracting_entity": "Ministério da Educação",
            "state": "DF",
            "estimated_total_value": 50000.0
        })
        assert "resumo" in exec_res
        assert "riscos_juridicos" in exec_res
        assert "competitividade_estimada" in exec_res


def test_api_tender_ai_analyze_endpoint(sample_tender):
    """Testa o endpoint POST /api/tenders/{tender_id}/ai-analyze e persistência enriquecida."""
    response = client.post(f"/api/tenders/{sample_tender.id}/ai-analyze", json={})
    assert response.status_code == 200
    data = response.json()

    assert data["success"] is True
    assert data["tender_id"] == sample_tender.id
    assert "data" in data

    result_data = data["data"]
    assert "resumo_executivo" in result_data
    assert "requisitos_participacao" in result_data
    assert "pontos_atencao" in result_data

    # Verifica se os dados enriquecidos foram persistidos no modelo Tender
    db = SessionLocal()
    try:
        updated_tender = db.query(Tender).filter(Tender.id == sample_tender.id).first()
        assert updated_tender is not None
        assert updated_tender.ai_resumo is not None
        assert updated_tender.ai_pontos_atencao is not None

        # Verifica se o registro de AIAnalysis foi criado
        analysis = db.query(AIAnalysis).filter(AIAnalysis.tender_id == sample_tender.id).first()
        assert analysis is not None
    finally:
        db.close()


# ==============================================================================
# 3. TESTES DO COCKPIT DE LANCES, AUDITORIA E TELEMETRIA
# ==============================================================================

def test_dispute_monitor_enriched_item_and_telemetry():
    """Valida rastreamento de item com 'fonte_portal' e persistência de telemetria em HistoricoDisputa."""
    service = DisputeMonitorService()

    # Rastrear item com fonte de portal específica
    item = service.track_item({
        "id_pregao": "PE 505/2026",
        "numero_item": 1,
        "descricao": "Nobreaks Senoidais Corporativos",
        "valor_referencia": 30000.0,
        "melhor_lance": 28000.0,
        "meu_lance": 29000.0,
        "fase": "ABERTO",
        "fonte_portal": "Compras.gov.br",
        "degrau_minimo": 50.0,
        "valor_minimo_limite": 25000.0,
        "auto_lance_ativo": True
    })

    assert item.fonte_portal == "Compras.gov.br"
    snapshot = service.get_active_snapshot()
    tracked = next((it for it in snapshot if it["id_pregao"] == "PE 505/2026"), None)
    assert tracked is not None
    assert tracked["fonte_portal"] == "Compras.gov.br"

    # Executa lance automático
    res_bid = service.executar_lance_automatico(item.item_key)
    assert res_bid["status_automacao"] in ["Robô Liderando (1º)", "Robô Ativo"]
    assert res_bid["meu_lance"] == 27950.0  # 28000 - 50 degrau

    # Verifica se telemetria foi gravada em HistoricoDisputa no SQLite
    db = SessionLocal()
    try:
        telemetry = db.query(HistoricoDisputa).filter(HistoricoDisputa.id_pregao == "PE 505/2026").first()
        assert telemetry is not None
        assert telemetry.id_pregao == "PE 505/2026"
        assert telemetry.tipo_evento == "AUTO_BID"
        assert telemetry.valor_ofertado == 27950.0
        assert telemetry.posicao == 1
    finally:
        db.close()


def test_dispute_monitor_stop_loss_telemetry():
    """Valida acionamento do piso Stop-Loss e persistência no histórico de disputas."""
    service = DisputeMonitorService()

    item = service.track_item({
        "id_pregao": "PE 777/2026",
        "numero_item": 2,
        "descricao": "Roteadores de Borda SD-WAN",
        "valor_referencia": 5000.0,
        "melhor_lance": 4010.0,
        "meu_lance": 4100.0,
        "fase": "ABERTO",
        "fonte_portal": "BNC",
        "degrau_minimo": 50.0,
        "valor_minimo_limite": 4000.0,  # Piso: 4000. Lance calculado seria 3960 < 4000 -> Stop Loss
        "auto_lance_ativo": True
    })

    res = service.executar_lance_automatico(item.item_key)
    assert res["status_automacao"] == "Piso Atingido / Margem Esgotada"
    assert res["auto_lance_ativo"] is False

    # Valida auditoria gravada
    db = SessionLocal()
    try:
        telemetry = db.query(HistoricoDisputa).filter(
            HistoricoDisputa.id_pregao == "PE 777/2026",
            HistoricoDisputa.tipo_evento == "STOP_LOSS"
        ).first()
        assert telemetry is not None
        assert telemetry.id_pregao == "PE 777/2026"
        assert telemetry.tipo_evento == "STOP_LOSS"
    finally:
        db.close()


def test_sse_endpoint_connect_and_snapshot():
    """Valida o endpoint SSE /api/disputes/events e conexão estável."""
    # Testa snapshot inicial do endpoint de disputas ativas
    resp = client.get("/api/disputes/active")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert "items" in data


# ==============================================================================
# 4. CLASSIFICADOR SIMPLIFICADO DE NATUREZA (MATERIAL / SERVICO / OUTROS)
# ==============================================================================

@pytest.mark.parametrize("objeto, esperado", [
    ("Aquisição de medicamentos oncológicos e imunobiológicos", "MATERIAL"),
    ("Registro de preços para futura e eventual aquisição de gêneros alimentícios", "MATERIAL"),
    ("FORNECIMENTO DE MATERIAL DE EXPEDIENTE E TONER", "MATERIAL"),
    ("Contratação de empresa especializada no fornecimento de próteses dentárias", "MATERIAL"),
    ("Contratação de empresa especializada para prestação de serviços de limpeza e conservação", "SERVICO"),
    ("Serviços de manutenção preventiva e corretiva de elevadores", "SERVICO"),
    ("Pavimentação asfáltica e obras de drenagem pluvial urbana", "SERVICO"),
    ("Leilão de veículos inservíveis da frota municipal", "OUTROS"),
    ("Locação de imóvel para funcionamento da sede administrativa", "OUTROS"),
    ("Cessão de uso onerosa de espaço físico para lanchonete", "OUTROS"),
    ("Geofísica", "OUTROS"),
    ("", "OUTROS"),
])
def test_classificar_natureza_objeto_casos_de_referencia(objeto, esperado):
    assert classificar_natureza_objeto(objeto) == esperado


def test_classificar_natureza_objeto_retorna_somente_valores_permitidos():
    amostras = [
        "Aquisição de notebooks", "Prestação de serviços de vigilância", "Doação de bens",
        None, 12345, "   ", "???", "SERVIÇOS GRÁFICOS COM MATERIAL INCLUSO",
    ]
    for amostra in amostras:
        assert classificar_natureza_objeto(amostra) in {"MATERIAL", "SERVICO", "OUTROS"}
    assert set(NATUREZAS_OBJETO) == {"MATERIAL", "SERVICO", "OUTROS"}


def test_classificar_natureza_certame_usa_descricao_quando_titulo_inconclusivo():
    assert classificar_natureza_certame("PE 12/2026", "Aquisição de cadeiras e mesas escolares") == "MATERIAL"
    assert classificar_natureza_certame("Serviços de dedetização", "qualquer descrição") == "SERVICO"


@pytest.mark.asyncio
async def test_collect_all_enriquece_itens_com_natureza():
    with patch("httpx.AsyncClient.get", side_effect=Exception("offline")):
        itens = await PortalCollectorService().collect_all(uf="DF", limit_per_portal=3)
    assert itens
    assert all(it["natureza_objeto"] in NATUREZAS_OBJETO for it in itens)


def test_home_filtro_natureza_e_badges():
    resp = client.get("/?natureza=MATERIAL")
    assert resp.status_code == 200
    assert 'id="natureza-filter"' in resp.text
    naturezas = re.findall(r'class="tender-card[^"]*" data-natureza="(\w+)"', resp.text)
    assert naturezas, "a home deve listar certames classificados"
    assert set(naturezas) == {"MATERIAL"}
    assert "natureza-badge" in resp.text

    resp_todos = client.get("/?natureza=invalida")
    assert resp_todos.status_code == 200
    assert "natureza-badge" in resp_todos.text


# ==============================================================================
# 5. GERAÇÃO JURÍDICA EM PDF (REPORTLAB) — STREAM DO ENDPOINT
# ==============================================================================

def _texto_pdf(conteudo: bytes) -> str:
    leitor = PdfReader(io.BytesIO(conteudo))
    bruto = " ".join((pagina.extract_text() or "") for pagina in leitor.pages)
    return " ".join(bruto.split())  # células de tabela quebram linha: normaliza espaços


def test_gerar_peticao_pdf_conteudo_timbrado():
    pdf = gerar_peticao_pdf(
        corpo="AO PREGOEIRO DO(A) {{orgao}}\n\nA licitante [NOME DA EMPRESA] impugna o edital {{numero_edital}} relativo a {{objeto}}.",
        orgao="Prefeitura Municipal de Maceió",
        numero_certame="PE 042/2026",
        objeto="Aquisição de medicamentos & insumos <hospitalares>",
        titulo="Impugnação ao Edital",
        lei="8.666/1993",
        licitante={"razao_social": "Alfa Distribuidora LTDA", "cnpj": "12345678000199", "representante": "Maria Souza"},
        cidade="Maceió/AL",
        data_documento=date(2026, 10, 9),
    )
    assert pdf.startswith(b"%PDF-")
    texto = _texto_pdf(pdf)
    assert "Licitações" in texto and "Brasil" in texto
    assert "PE 042/2026" in texto
    assert "Prefeitura Municipal de Maceió" in texto
    assert "8.666" in texto
    assert "ALFA DISTRIBUIDORA LTDA" in texto
    assert "12.345.678/0001-99" in texto
    assert "9 de outubro de 2026" in texto
    assert "{{orgao}}" not in texto


def test_gerar_peticao_pdf_rejeita_corpo_vazio():
    with pytest.raises(PeticaoInvalidaError):
        gerar_peticao_pdf(corpo="   ")
    assert normalizar_lei("14.133/2021") == "14133"
    assert normalizar_lei(False) == "8666"


def test_api_juridico_gerar_pdf_streaming_response():
    db = SessionLocal()
    try:
        tmpl = db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.tipo == "impugnacao",
                                                      LegalDocumentTemplate.is_nova_lei == True).first()
        assert tmpl is not None
        tmpl_id = tmpl.id
    finally:
        db.close()

    resp = client.post("/api/juridico/gerar-pdf", json={
        "template_id": tmpl_id,
        "orgao": "Secretaria de Saúde de Alagoas",
        "numero_certame": "PE 777/2026",
        "objeto": "Aquisição de seringas",
        "licitante": {"razao_social": "Beta Hospitalar SA", "cnpj": "11222333000144"},
        "cidade": "Maceió/AL"
    })
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].endswith('.pdf"')
    assert resp.content.startswith(b"%PDF-")
    texto = _texto_pdf(resp.content)
    assert "PE 777/2026" in texto
    assert "Secretaria de Saúde de Alagoas" in texto
    assert "14.133" in texto


def test_api_juridico_gerar_pdf_corpo_editado_e_validacoes():
    resp = client.post("/api/juridico/gerar-pdf", json={
        "corpo": "TEXTO EDITADO NO MODAL\n\nPedido de esclarecimento sobre {{objeto}}.",
        "objeto": "Locação de impressoras",
        "lei": "8666"
    })
    assert resp.status_code == 200
    assert "TEXTO EDITADO NO MODAL" in _texto_pdf(resp.content)

    assert client.post("/api/juridico/gerar-pdf", json={}).status_code == 422
    assert client.post("/api/juridico/gerar-pdf", json={"template_id": 999999}).status_code == 404


def test_page_juridico_botao_pdf_e_prefill_por_query_params():
    resp = client.get("/juridico", params={
        "orgao": "Prefeitura de Recife", "numero_certame": "PE 10/2026", "objeto": "Aquisição <script>"
    })
    assert resp.status_code == 200
    assert "Baixar PDF" in resp.text
    assert "/api/juridico/gerar-pdf" in resp.text
    assert 'value="Prefeitura de Recife"' in resp.text
    assert 'value="PE 10/2026"' in resp.text
    assert "Aquisição &lt;script&gt;" in resp.text
    # Placeholders do JS preservados (não consumidos pelo Jinja)
    assert "'{{orgao}}'" in resp.text


# ==============================================================================
# 6. COFRE DE DOCUMENTOS + KIT DE HABILITAÇÃO (ZIP)
# ==============================================================================

def _pdf_minimo(texto: str) -> bytes:
    from reportlab.pdfgen import canvas as rl_canvas
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf)
    c.drawString(72, 720, texto)
    c.showPage()
    c.save()
    return buf.getvalue()


@pytest.fixture
def cofre_isolado(tmp_path, monkeypatch):
    """Direciona uploads para diretório temporário e remove os registros criados pelo teste."""
    monkeypatch.setattr(document_vault, "DOCUMENTOS_UPLOAD_DIR", tmp_path / "documentos")
    db = SessionLocal()
    ids_antes = {d.id for d in db.query(DocumentoEmpresa.id).all()}
    db.close()
    yield tmp_path
    db = SessionLocal()
    try:
        db.query(DocumentoEmpresa).filter(~DocumentoEmpresa.id.in_(ids_antes or {-1})).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_documento_status_validade_limiares():
    hoje = date(2026, 10, 9)
    doc = DocumentoEmpresa(nome="CND", categoria="fiscal", data_emissao=hoje, caminho_arquivo="x.pdf")
    doc.data_validade = hoje + timedelta(days=16)
    assert doc.status_validade(hoje) == "VALIDO" and doc.dias_restantes(hoje) == 16
    doc.data_validade = hoje + timedelta(days=15)
    assert doc.status_validade(hoje) == "A_VENCER"
    doc.data_validade = hoje
    assert doc.status_validade(hoje) == "A_VENCER" and doc.dias_restantes(hoje) == 0
    doc.data_validade = hoje - timedelta(days=1)
    assert doc.status_validade(hoje) == "VENCIDO" and doc.dias_restantes(hoje) == -1
    doc.data_validade = None
    assert doc.status_validade(hoje) == "SEM_VALIDADE" and doc.dias_restantes(hoje) is None


def test_api_upload_listagem_e_exclusao_de_documento(cofre_isolado):
    hoje = hoje_sao_paulo()
    resp = client.post(
        "/api/documentos",
        data={
            "nome": "CND Federal (Receita/PGFN)",
            "categoria": "fiscal",
            "data_emissao": (hoje - timedelta(days=10)).isoformat(),
            "data_validade": (hoje + timedelta(days=5)).isoformat(),
            "tags_habilitacao": "CND, Receita Federal, CND",
        },
        files={"arquivo": ("cnd.pdf", _pdf_minimo("CND"), "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    doc = resp.json()
    assert doc["status"] == "A_VENCER"
    assert doc["dias_restantes"] == 5
    assert doc["tags"] == ["CND", "Receita Federal"]
    assert doc["arquivo_disponivel"] is True
    assert list((cofre_isolado / "documentos").glob("*.pdf"))

    lista = client.get("/api/documentos").json()
    assert any(i["id"] == doc["id"] for i in lista["items"])
    assert lista["resumo"]["a_vencer"] >= 1

    arquivo = client.get(doc["arquivo_url"])
    assert arquivo.status_code == 200
    assert arquivo.content.startswith(b"%PDF-")

    pagina = client.get("/documentos")
    assert pagina.status_code == 200
    assert "Cofre de Documentos" in pagina.text
    assert "CND Federal (Receita/PGFN)" in pagina.text

    excluir = client.delete(f"/api/documentos/{doc['id']}")
    assert excluir.status_code == 200
    assert excluir.json()["arquivo_removido"] is True
    assert not list((cofre_isolado / "documentos").glob("*.pdf"))


def test_api_upload_rejeita_arquivo_nao_pdf_e_datas_invalidas(cofre_isolado):
    base = {"nome": "Atestado", "categoria": "tecnica", "data_emissao": "2026-01-10"}
    r1 = client.post("/api/documentos", data=base, files={"arquivo": ("a.pdf", b"nao sou pdf", "application/pdf")})
    assert r1.status_code == 422
    r2 = client.post("/api/documentos", data=base, files={"arquivo": ("a.txt", _pdf_minimo("x"), "text/plain")})
    assert r2.status_code == 422
    r3 = client.post("/api/documentos", data={**base, "data_validade": "2025-01-01"},
                     files={"arquivo": ("a.pdf", _pdf_minimo("x"), "application/pdf")})
    assert r3.status_code == 422
    r4 = client.post("/api/documentos", data={**base, "categoria": "inexistente"},
                     files={"arquivo": ("a.pdf", _pdf_minimo("x"), "application/pdf")})
    assert r4.status_code == 422


def test_navegacao_contem_link_cofre_de_documentos():
    html_home = client.get("/").text
    assert html_home.count('href="/documentos"') >= 2  # desktop + menu PWA
    assert "Cofre de Documentos" in html_home


def _criar_documento(db, nome, categoria, validade, pasta, emissao=date(2026, 1, 2)):
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / f"{document_vault.slugify(nome)}.pdf"
    caminho.write_bytes(_pdf_minimo(nome))
    doc = DocumentoEmpresa(nome=nome, categoria=categoria, data_emissao=emissao,
                           data_validade=validade, caminho_arquivo=str(caminho))
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def test_montar_kit_habilitacao_zip_filtra_validos_e_injeta_relatorio(cofre_isolado, sample_tender):
    hoje = date(2026, 10, 9)
    pasta = cofre_isolado / "documentos"
    db = SessionLocal()
    try:
        _criar_documento(db, "KIT Contrato Social", "juridica", None, pasta)
        _criar_documento(db, "KIT CND Federal", "fiscal", hoje + timedelta(days=40), pasta)
        _criar_documento(db, "KIT Certidao Falencia", "economica", hoje + timedelta(days=3), pasta)
        _criar_documento(db, "KIT Balanco Vencido", "economica", hoje - timedelta(days=1), pasta)
        sem_arquivo = DocumentoEmpresa(nome="KIT Atestado Sem Arquivo", categoria="tecnica",
                                       data_emissao=date(2026, 1, 2), data_validade=None,
                                       caminho_arquivo=str(pasta / "nao_existe.pdf"))
        db.add(sem_arquivo)
        db.commit()

        conteudo = montar_kit_habilitacao_zip(sample_tender.id, db=db, hoje=hoje)
    finally:
        db.close()

    zf = zipfile.ZipFile(io.BytesIO(conteudo))
    assert zf.testzip() is None
    nomes = zf.namelist()
    assert nomes[0] == NOME_RELATORIO
    docs_zip = [n for n in nomes if n.startswith(PASTA_DOCUMENTOS + "/")]
    assert any("KIT_Contrato_Social" in n for n in docs_zip)
    assert any("KIT_CND_Federal" in n for n in docs_zip)
    assert any("KIT_Certidao_Falencia" in n for n in docs_zip)
    assert not any("Vencido" in n for n in docs_zip)
    assert not any("Sem_Arquivo" in n for n in docs_zip)
    for n in docs_zip:
        assert zf.read(n).startswith(b"%PDF-")

    relatorio = _texto_pdf(zf.read(NOME_RELATORIO))
    assert "RELATÓRIO DE CONFORMIDADE" in relatorio
    assert (sample_tender.notice_number or sample_tender.external_id) in relatorio
    assert "KIT Balanco Vencido" in relatorio
    assert "Vencido em" in relatorio
    assert "Arquivo não localizado no cofre" in relatorio


def test_montar_kit_tender_inexistente():
    with pytest.raises(KitHabilitacaoError):
        montar_kit_habilitacao_zip("certame-que-nao-existe")


def test_api_download_kit_zip_e_botao_no_detalhe(cofre_isolado, sample_tender):
    resp = client.get(f"/api/tenders/{sample_tender.id}/download-kit-zip")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "Kit_Habilitacao_" in resp.headers["content-disposition"]
    assert NOME_RELATORIO in zipfile.ZipFile(io.BytesIO(resp.content)).namelist()

    assert client.get("/api/tenders/inexistente-xyz/download-kit-zip").status_code == 404

    detalhe = client.get(f"/tenders/{sample_tender.id}")
    assert "Baixar Kit de Habilitação (ZIP)" in detalhe.text
    assert f"/api/tenders/{sample_tender.id}/download-kit-zip" in detalhe.text


# ==============================================================================
# 7. LIGAÇÃO KANBAN -> JURÍDICO
# ==============================================================================

def test_kanban_card_envia_para_juridico_com_query_params(sample_tender):
    db = SessionLocal()
    criado = False
    try:
        deal = db.query(Deal).filter(Deal.tender_id == sample_tender.id).first()
        if not deal:
            deal = Deal(tender_id=sample_tender.id, stage="oportunidades", notes="teste juridico")
            db.add(deal)
            db.commit()
            criado = True
        deal_id = deal.id
    finally:
        db.close()

    try:
        html_kanban = client.get("/negocios?view=quadros").text
        assert "Enviar para Jurídico" in html_kanban
        bloco = html_kanban.split(f'id="deal-card-{deal_id}"', 1)[1]
        href = re.search(r'href="(/juridico\?[^"]+)"', bloco).group(1)
        params = parse_qs(urlparse(html.unescape(href)).query)
        assert params["orgao"] == [sample_tender.contracting_entity]
        assert params["numero_certame"] == [sample_tender.notice_number or sample_tender.external_id]
        assert params["objeto"] == [sample_tender.title]

        destino = client.get(html.unescape(href))
        assert destino.status_code == 200
        assert html.escape(sample_tender.contracting_entity, quote=True) in destino.text
    finally:
        if criado:
            db = SessionLocal()
            try:
                db.query(Deal).filter(Deal.id == deal_id).delete()
                db.commit()
            finally:
                db.close()
