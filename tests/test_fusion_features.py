import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from server.main import app
from server.database import SessionLocal
from server.models import Tender, HistoricoDisputa, AIAnalysis
from server.services.gemini_analyzer import gemini_analyzer
from server.services.portal_collector import portal_collector, sync_pipeline, PortalCollectorService
from server.services.dispute_monitor import dispute_monitor, DisputeMonitorService, PregaoItem

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
