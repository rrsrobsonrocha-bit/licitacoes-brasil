import pytest
from fastapi.testclient import TestClient
from server.main import app
from server.services.dispute_monitor import DisputeMonitorService, PregaoItem, dispute_monitor

client = TestClient(app)


def test_dispute_monitor_service_tracking_multiple_items():
    """Valida registro e consulta de múltiplos itens simultâneos no serviço de monitoramento."""
    service = DisputeMonitorService()

    item1 = service.track_item({
        "id_pregao": "PE 101/2026",
        "numero_item": 1,
        "descricao": "Monitores de Alta Resolução 4K",
        "valor_referencia": 45000.0,
        "melhor_lance": 42000.0,
        "meu_lance": 43500.0,
        "fase": "ABERTO"
    })

    item2 = service.track_item({
        "id_pregao": "PE 101/2026",
        "numero_item": 2,
        "descricao": "Estações de Trabalho Gráficas",
        "valor_referencia": 120000.0,
        "melhor_lance": 115000.0,
        "meu_lance": 115000.0,
        "fase": "IMINENCIA"
    })

    item3 = service.track_item({
        "id_pregao": "PE 202/2026",
        "numero_item": 1,
        "descricao": "Switch Gerenciável 48 Portas PoE",
        "valor_referencia": 68000.0,
        "melhor_lance": 65000.0,
        "meu_lance": 64000.0,
        "fase": "ABERTO"
    })

    snapshot = service.get_active_snapshot()
    assert len(snapshot) == 3

    # Verifica integridade das chaves e estado
    assert item1.is_losing is True  # meu_lance (43500) > melhor_lance (42000)
    assert item2.is_losing is False # meu_lance (115000) == melhor_lance
    assert item3.is_losing is False # meu_lance (64000) < melhor_lance (65000)
    assert item2.fase == "IMINENCIA"

    found = service.get_item("PE 101/2026", 1)
    assert found is not None
    assert found.descricao == "Monitores de Alta Resolução 4K"


def test_dispute_monitor_event_queue_and_subscribers():
    """Valida consistência da fila de eventos e distribuição assíncrona para assinantes."""
    service = DisputeMonitorService()
    sub_queue = service.subscribe()

    try:
        # 1. Rastrear item deve emitir evento ITEM_TRACKED
        service.track_item({
            "id_pregao": "PE 303/2026",
            "numero_item": 5,
            "descricao": "No-Break Senoidal 10kVA",
            "valor_referencia": 32000.0,
            "melhor_lance": 31000.0,
            "meu_lance": 31000.0,
            "fase": "ABERTO"
        })

        assert not sub_queue.empty()
        ev1 = sub_queue.get_nowait()
        assert ev1["type"] == "ITEM_TRACKED"
        assert ev1["item"]["id_pregao"] == "PE 303/2026"
        assert ev1["item"]["numero_item"] == 5

        # 2. Simular pulso concorrente deve gerar atualização de lance e transição para IMINENCIA
        updated = service.simulate_pulse("PE 303/2026:5")
        assert len(updated) == 1
        assert updated[0]["fase"] == "IMINENCIA"
        assert updated[0]["melhor_lance"] < 31000.0

        assert not sub_queue.empty()
        ev2 = sub_queue.get_nowait()
        assert ev2["type"] == "IMMINENCE_ALERT"
        assert ev2["item"]["fase"] == "IMINENCIA"

    finally:
        service.unsubscribe(sub_queue)


def test_api_disputes_track_and_active_endpoints():
    """Valida retornos HTTP 200 e payload JSON íntegro nas rotas da API."""
    payload = {
        "id_pregao": "PE 999/2026",
        "numero_item": 1,
        "descricao": "Software de Análise de Vulnerabilidades",
        "valor_referencia": 90000.0,
        "melhor_lance": 88000.0,
        "meu_lance": 88000.0,
        "fase": "ABERTO"
    }

    # 1. POST /api/disputes/track
    resp_track = client.post("/api/disputes/track", json=payload)
    assert resp_track.status_code == 200
    track_data = resp_track.json()
    assert track_data["status"] == "success"
    assert "item" in track_data
    assert track_data["item"]["id_pregao"] == "PE 999/2026"
    assert track_data["item"]["numero_item"] == 1

    # 2. GET /api/disputes/active
    resp_active = client.get("/api/disputes/active")
    assert resp_active.status_code == 200
    active_data = resp_active.json()
    assert active_data["status"] == "success"
    assert "total" in active_data
    assert active_data["total"] >= 1
    item_in_list = next((it for it in active_data["items"] if it["id_pregao"] == "PE 999/2026"), None)
    assert item_in_list is not None
    assert item_in_list["descricao"] == "Software de Análise de Vulnerabilidades"

    # 3. POST /api/disputes/pulse
    resp_pulse = client.post("/api/disputes/pulse?item_key=PE 999/2026:1")
    assert resp_pulse.status_code == 200
    pulse_data = resp_pulse.json()
    assert pulse_data["status"] == "success"
    assert pulse_data["total_updated"] >= 1


def test_pwa_manifest_and_sw_endpoints():
    """Valida disponibilidade e conformidade dos assets de PWA para Android."""
    # 1. Manifest
    res_manifest = client.get("/manifest.json")
    assert res_manifest.status_code == 200
    manifest = res_manifest.json()
    assert manifest["name"] == "LicitaçõesBrasil - Gestão e Inteligência em Licitações"
    assert manifest["short_name"] == "LicitaçõesBrasil"
    assert manifest["display"] == "standalone"
    assert manifest["theme_color"] == "#0f172a"

    # 2. Service Worker
    res_sw = client.get("/sw.js")
    assert res_sw.status_code == 200
    assert "javascript" in res_sw.headers.get("content-type", "")
    assert res_sw.headers.get("Service-Worker-Allowed") == "/"

    # 3. Página HTML da Disputa
    res_disputa = client.get("/disputa")
    assert res_disputa.status_code == 200
    html = res_disputa.text
    assert "Cockpit Multi-Pregão" in html
    assert "DisputeAudioEngine" in html
    assert "playCompetitorBeep" in html
    assert "playImminenceAlert" in html
    assert "vibrate" in html
