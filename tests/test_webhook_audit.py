import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
import httpx

from server.main import app
from server.database import SessionLocal
from server.models import WebhookDispatchLog, Alert, Tender
from server.webhook_dispatcher import (
    format_webhook_payload,
    dispatch_webhook_sync,
    dispatch_webhook_async,
    retry_webhook_dispatch,
    SUPPORTED_CHANNELS
)

client = TestClient(app)


# 1. Canais e Formatação de Payload (4 testes)
@pytest.mark.parametrize("canal", ["N8N", "EVOLUTION_API", "ZAPI", "GENERIC"])
def test_webhook_channels_formatting(canal):
    data = {"tender_id": "test-123", "title": "Pregão Eletrônico"}
    payload = format_webhook_payload(canal=canal, event="TENDER_MATCH", data=data, rule_id="rule-999")
    assert payload is not None
    assert payload["event"] == "TENDER_MATCH" if canal != "ZAPI" else payload["eventType"] == "TENDER_MATCH"
    assert "timestamp" in payload


# 2. Disparo Síncrono e Status Codes (5 testes)
@pytest.mark.parametrize("status_code", [200, 201, 400, 404, 500])
def test_webhook_sync_dispatch_status_codes(status_code):
    db = SessionLocal()
    try:
        with patch("httpx.Client") as mock_client_cls:
            mock_inst = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_inst
            mock_resp = MagicMock(status_code=status_code, text=f'{{"code": {status_code}}}')
            mock_inst.post.return_value = mock_resp

            log = dispatch_webhook_sync(
                db=db,
                target_url="https://webhook.site/test",
                payload={"test": True},
                canal="GENERIC"
            )

            assert log.id is not None
            assert log.status_code == status_code
            assert log.sucesso == (200 <= status_code < 300)
            assert log.tempo_resposta_ms >= 0.0
    finally:
        db.close()


# 3. Disparo Assíncrono e Status Codes (5 testes)
@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [200, 204, 401, 403, 502])
async def test_webhook_async_dispatch_status_codes(status_code):
    db = SessionLocal()
    try:
        with patch("httpx.AsyncClient") as mock_async_cls:
            mock_inst = MagicMock()
            mock_async_cls.return_value.__aenter__.return_value = mock_inst
            mock_resp = MagicMock(status_code=status_code, text=f'{{"code": {status_code}}}')
            mock_inst.post = AsyncMock(return_value=mock_resp)

            log = await dispatch_webhook_async(
                db=db,
                target_url="https://webhook.site/async-test",
                payload={"async": True},
                canal="N8N"
            )

            assert log.id is not None
            assert log.status_code == status_code
            assert log.sucesso == (200 <= status_code < 300)
            assert log.canal == "N8N"
    finally:
        db.close()


# 4. Persistência de Canais Síncrono (4 testes)
@pytest.mark.parametrize("canal", ["N8N", "EVOLUTION_API", "ZAPI", "GENERIC"])
def test_webhook_dispatch_sync_channel_persistence(canal):
    db = SessionLocal()
    try:
        with patch("httpx.Client") as mock_client_cls:
            mock_inst = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_inst
            mock_inst.post.return_value = MagicMock(status_code=200, text='{"ok": true}')

            log = dispatch_webhook_sync(
                db=db,
                target_url="https://api.empresa.com/webhook",
                payload={"data": "test"},
                canal=canal
            )

            persisted = db.query(WebhookDispatchLog).filter(WebhookDispatchLog.id == log.id).first()
            assert persisted is not None
            assert persisted.canal == canal
            assert persisted.sucesso is True
    finally:
        db.close()


# 5. Persistência de Canais Assíncrono (4 testes)
@pytest.mark.asyncio
@pytest.mark.parametrize("canal", ["N8N", "EVOLUTION_API", "ZAPI", "GENERIC"])
async def test_webhook_dispatch_async_channel_persistence(canal):
    db = SessionLocal()
    try:
        with patch("httpx.AsyncClient") as mock_async_cls:
            mock_inst = MagicMock()
            mock_async_cls.return_value.__aenter__.return_value = mock_inst
            mock_inst.post = AsyncMock(return_value=MagicMock(status_code=200, text='{"ok": true}'))

            log = await dispatch_webhook_async(
                db=db,
                target_url="https://api.empresa.com/async-webhook",
                payload={"async_channel": canal},
                canal=canal
            )

            persisted = db.query(WebhookDispatchLog).filter(WebhookDispatchLog.id == log.id).first()
            assert persisted is not None
            assert persisted.canal == canal
            assert persisted.sucesso is True
    finally:
        db.close()


# 6. Exceções de Rede Síncronas (2 testes)
@pytest.mark.parametrize("exc", [httpx.TimeoutException("Timeout"), httpx.ConnectError("Connection refused")])
def test_webhook_sync_network_exceptions(exc):
    db = SessionLocal()
    try:
        with patch("httpx.Client") as mock_client_cls:
            mock_inst = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_inst
            mock_inst.post.side_effect = exc

            log = dispatch_webhook_sync(
                db=db,
                target_url="https://unreachable.domain/webhook",
                payload={"error": True}
            )

            assert log.sucesso is False
            assert log.status_code is None
            assert "Erro" in log.resposta_servidor
    finally:
        db.close()


# 7. Exceções de Rede Assíncronas (2 testes)
@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [httpx.TimeoutException("Async Timeout"), httpx.ConnectError("Async Connect Error")])
async def test_webhook_async_network_exceptions(exc):
    db = SessionLocal()
    try:
        with patch("httpx.AsyncClient") as mock_async_cls:
            mock_inst = MagicMock()
            mock_async_cls.return_value.__aenter__.return_value = mock_inst
            mock_inst.post = AsyncMock(side_effect=exc)

            log = await dispatch_webhook_async(
                db=db,
                target_url="https://unreachable.domain/webhook-async",
                payload={"async_error": True}
            )

            assert log.sucesso is False
            assert log.status_code is None
            assert "Erro" in log.resposta_servidor
    finally:
        db.close()


# 8. Retry Increments Attempts (3 testes)
@pytest.mark.parametrize("init_attempt", [1, 2, 3])
def test_webhook_retry_dispatch_increments_attempts(init_attempt):
    db = SessionLocal()
    try:
        base_log = WebhookDispatchLog(
            target_url="https://retry.test/webhook",
            canal="EVOLUTION_API",
            tentativa_atual=init_attempt,
            sucesso=False,
            payload_enviado={"attempt": init_attempt}
        )
        db.add(base_log)
        db.commit()
        db.refresh(base_log)

        with patch("httpx.Client") as mock_client_cls:
            mock_inst = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_inst
            mock_inst.post.return_value = MagicMock(status_code=200, text='{"retry": "ok"}')

            new_log = retry_webhook_dispatch(db, base_log.id)
            assert new_log is not None
            assert new_log.tentativa_atual == init_attempt + 1
            assert new_log.sucesso is True
    finally:
        db.close()


# 9. API Webhook Logs - Filtro de Status (4 testes)
@pytest.mark.parametrize("status_val", ["SUCESSO", "FALHA", "success", "failed"])
def test_webhook_logs_api_status_filter(status_val):
    resp = client.get(f"/api/alerts/webhooks/logs?status={status_val}")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    expected_bool = status_val.lower() in ("sucesso", "success")
    for item in data["items"]:
        assert item["sucesso"] == expected_bool


# 10. API Webhook Logs - Filtro de Canal (4 testes)
@pytest.mark.parametrize("canal_filter", ["N8N", "EVOLUTION_API", "ZAPI", "GENERIC"])
def test_webhook_logs_api_canal_filter(canal_filter):
    resp = client.get(f"/api/alerts/webhooks/logs?canal={canal_filter}")
    assert resp.status_code == 200
    data = resp.json()
    for item in data["items"]:
        assert item["canal"] == canal_filter


# 11. API Webhook Logs - Paginação (3 testes)
@pytest.mark.parametrize("p, ps", [(1, 2), (2, 2), (1, 5)])
def test_webhook_logs_api_pagination(p, ps):
    resp = client.get(f"/api/alerts/webhooks/logs?page={p}&page_size={ps}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["page"] == p
    assert data["page_size"] == ps
    assert len(data["items"]) <= ps


# 12. Busca com Valores Vazios no HTML Search (3 testes)
@pytest.mark.parametrize("v_min", ["", "   ", None])
def test_search_empty_value_filters_page(v_min):
    url = "/search"
    if v_min is not None:
        url += f"?value_min={v_min}&value_max="
    response = client.get(url)
    assert response.status_code == 200
    assert "Licitações" in response.text


# 13. Busca com Valores Vazios no API Search (3 testes)
@pytest.mark.parametrize("v_min", ["", "   ", None])
def test_search_empty_value_filters_api(v_min):
    url = "/api/tenders"
    if v_min is not None:
        url += f"?value_min={v_min}&value_max="
    response = client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert "results" in data


# 14. Medição de Latência (1 teste)
@pytest.mark.parametrize("delay", [0.01])
def test_webhook_latency_recorded(delay):
    db = SessionLocal()
    try:
        with patch("httpx.Client") as mock_client_cls:
            mock_inst = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_inst
            mock_inst.post.return_value = MagicMock(status_code=200, text='ok')

            log = dispatch_webhook_sync(
                db=db,
                target_url="https://latency.test/webhook",
                payload={"latency": True}
            )
            assert log.tempo_resposta_ms >= 0.0
    finally:
        db.close()


# 15. Formatos Variados de Payload (2 testes)
@pytest.mark.parametrize("payload", [
    {"simples": 123},
    {"aninhado": {"chave": "valor"}}
])
def test_webhook_payload_structures(payload):
    db = SessionLocal()
    try:
        with patch("httpx.Client") as mock_client_cls:
            mock_inst = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_inst
            mock_inst.post.return_value = MagicMock(status_code=200, text='{"received": true}')

            log = dispatch_webhook_sync(
                db=db,
                target_url="https://payload.test/webhook",
                payload=payload
            )
            assert log.payload_enviado == payload
    finally:
        db.close()


# 16. Integridade de Campos do Modelo WebhookDispatchLog (1 teste)
def test_webhook_model_fields_integrity():
    db = SessionLocal()
    try:
        log = WebhookDispatchLog(
            target_url="https://model-fields.test/webhook",
            canal="GENERIC",
            status_code=200,
            tentativa_atual=1,
            sucesso=True,
            tempo_resposta_ms=45.2,
            payload_enviado={"status": "ok"},
            resposta_servidor="Criado",
            criado_em=datetime.utcnow()
        )
        db.add(log)
        db.commit()
        db.refresh(log)

        assert log.id is not None
        assert log.target_url == "https://model-fields.test/webhook"
        assert log.canal == "GENERIC"
        assert log.status_code == 200
        assert log.sucesso is True
        assert log.tempo_resposta_ms == 45.2
        assert log.criado_em is not None
    finally:
        db.close()


# 17. Filtro por Certame ID no Endpoint de Logs (1 teste)
def test_webhook_logs_api_certame_filter():
    db = SessionLocal()
    try:
        specific_tender_id = "certame-special-xyz"
        log = WebhookDispatchLog(
            tender_id=specific_tender_id,
            target_url="https://certame.test/webhook",
            canal="ZAPI",
            sucesso=True
        )
        db.add(log)
        db.commit()

        resp = client.get(f"/api/alerts/webhooks/logs?certame_id={specific_tender_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert any(item["tender_id"] == specific_tender_id for item in data["items"])
    finally:
        db.close()


# 18. Filtro por Data no Endpoint de Logs (1 teste)
def test_webhook_logs_api_date_filter():
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    resp = client.get(f"/api/alerts/webhooks/logs?data={today_str}")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data


# 19. Endpoint de Retry com Sucesso (1 teste)
def test_webhook_retry_endpoint_success():
    db = SessionLocal()
    try:
        log = WebhookDispatchLog(
            target_url="https://endpoint-retry.test/webhook",
            canal="N8N",
            tentativa_atual=1,
            sucesso=False,
            payload_enviado={"retry_test": True}
        )
        db.add(log)
        db.commit()
        db.refresh(log)

        with patch("httpx.Client") as mock_client_cls:
            mock_inst = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_inst
            mock_inst.post.return_value = MagicMock(status_code=200, text='{"retried": true}')

            resp = client.post(f"/api/alerts/webhooks/{log.id}/retry")
            assert resp.status_code == 200
            data = resp.json()
            assert "log" in data
            assert data["log"]["tentativa_atual"] == 2
            assert data["log"]["sucesso"] is True
    finally:
        db.close()


# 20. Endpoint de Retry com ID Inexistente (1 teste)
def test_webhook_retry_endpoint_not_found():
    resp = client.post("/api/alerts/webhooks/id-totalmente-inexistente-404/retry")
    assert resp.status_code == 404
    assert "detail" in resp.json()


# 21. Estrutura das Métricas de Saúde (1 teste)
def test_health_metrics_endpoint_structure():
    resp = client.get("/api/health/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert "uptime_seconds" in data
    assert "uptime_human" in data
    assert "database" in data
    assert "disparos_ultimas_24h" in data
    assert "disparos_sucesso_24h" in data
    assert "disparos_falha_24h" in data
    assert "taxa_sucesso_pct" in data
    assert data["database"] == "healthy"


# 22. Cálculo da Taxa de Sucesso nas Métricas (1 teste)
def test_health_metrics_success_rate_calculation():
    resp = client.get("/api/health/metrics")
    assert resp.status_code == 200
    data = resp.json()
    total = data["disparos_ultimas_24h"]
    success = data["disparos_sucesso_24h"]
    rate = data["taxa_sucesso_pct"]
    if total > 0:
        expected = round((success / total) * 100, 2)
        assert rate == expected
    else:
        assert rate == 100.0
