import asyncio
import json
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
        "fase": "ABERTO",
        "degrau_minimo": 0.01,
        "valor_minimo_limite": 35000.0
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
    assert found.degrau_minimo == 0.01
    assert found.valor_minimo_limite == 35000.0


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

        # 2. Melhor lance real informado pelo usuário atualiza o lote e emite BID_UPDATE
        updated = service.registrar_melhor_lance("PE 303/2026:5", 30500.0)
        assert updated["melhor_lance"] == 30500.0
        assert updated["is_losing"] is True
        assert updated["historico_concorrentes"][0]["valor"] == 30500.0

        assert not sub_queue.empty()
        ev2 = sub_queue.get_nowait()
        assert ev2["type"] == "BID_UPDATE"
        assert ev2["item"]["melhor_lance"] == 30500.0

        # Nenhum concorrente fictício é criado ao cadastrar o lote
        assert not hasattr(service, "simulate_pulse")
        with pytest.raises(ValueError):
            service.registrar_melhor_lance("PE 303/2026:5", 0)

    finally:
        service.unsubscribe(sub_queue)


def test_autobid_minimal_decrement_and_stop_loss_protection():
    """
    Testa o motor de lances automáticos:
    - Decremento exato por degrau_minimo (ex: R$ 0.01 ou R$ 5.00).
    - Preservação da margem e suspensão imediata com STOP_LOSS_REACHED ao atingir o piso.
    """
    service = DisputeMonitorService()
    sub_queue = service.subscribe()

    try:
        item = service.track_item({
            "id_pregao": "PE 505/2026",
            "numero_item": 1,
            "descricao": "Lote de Laptops Corporativos",
            "valor_referencia": 50000.0,
            "melhor_lance": 48000.0,
            "meu_lance": 48500.0,
            "fase": "ABERTO",
            "degrau_minimo": 10.0,
            "valor_minimo_limite": 47500.0,
            "posicao_alvo": 1,
            "auto_lance_ativo": True
        })

        # Lance automático 1: melhor_lance é 48000.0 -> novo lance deve ser 47990.0 (48000 - 10)
        res1 = service.executar_lance_automatico(item.item_key)
        assert res1 is not None
        assert res1["meu_lance"] == 47990.0
        assert res1["melhor_lance"] == 47990.0
        assert res1["auto_lance_ativo"] is True

        # Verifica evento AUTO_BID_PLACED
        events = []
        while not sub_queue.empty():
            events.append(sub_queue.get_nowait())
        auto_bid_events = [e for e in events if e.get("type") == "AUTO_BID_PLACED"]
        assert len(auto_bid_events) >= 1
        assert auto_bid_events[-1]["lance"] == 47990.0

        # Concorrente cobre agressivamente abaixo do nosso piso (piso é 47500.0)
        item.melhor_lance = 47495.0
        # Robô tenta cobrir: 47495 - 10 = 47485 < 47500 (STOP LOSS)
        res2 = service.executar_lance_automatico(item.item_key)
        assert res2 is not None
        assert res2["auto_lance_ativo"] is False
        assert "Piso Atingido / Margem Esgotada" in res2["status_automacao"]

        # Verifica evento STOP_LOSS_REACHED
        events2 = []
        while not sub_queue.empty():
            events2.append(sub_queue.get_nowait())
        stop_loss_events = [e for e in events2 if e.get("type") == "STOP_LOSS_REACHED"]
        assert len(stop_loss_events) >= 1
        assert stop_loss_events[-1]["piso"] == 47500.0
    finally:
        service.unsubscribe(sub_queue)


def test_individual_and_global_autobid_toggles():
    """Valida comutação dos interruptores individuais de lote e do interruptor mestre global."""
    service = DisputeMonitorService()

    i1 = service.track_item({
        "id_pregao": "PE 701/2026",
        "numero_item": 1,
        "descricao": "Item 1",
        "valor_referencia": 1000.0,
        "melhor_lance": 950.0,
        "meu_lance": 950.0,
        "auto_lance_ativo": False
    })
    i2 = service.track_item({
        "id_pregao": "PE 701/2026",
        "numero_item": 2,
        "descricao": "Item 2",
        "valor_referencia": 2000.0,
        "melhor_lance": 1900.0,
        "meu_lance": 1900.0,
        "auto_lance_ativo": False
    })

    # 1. Configuração individual
    updated_i1 = service.configure_item(
        item_id=i1.item_key,
        degrau_minimo=0.05,
        valor_minimo_limite=800.0,
        posicao_alvo=1,
        auto_lance_ativo=True
    )
    assert updated_i1 is not None
    assert updated_i1.auto_lance_ativo is True
    assert updated_i1.degrau_minimo == 0.05
    assert updated_i1.valor_minimo_limite == 800.0
    assert i2.auto_lance_ativo is False

    # 2. Toggle global LIGADO (ativa todos)
    all_on = service.toggle_all_autobid(True)
    assert len(all_on) == 2
    assert all(it["auto_lance_ativo"] is True for it in all_on)

    # 3. Toggle global DESLIGADO (desativa todos)
    all_off = service.toggle_all_autobid(False)
    assert len(all_off) == 2
    assert all(it["auto_lance_ativo"] is False for it in all_off)


def test_api_disputes_endpoints_and_sse_integrity(admin_auth):
    """Valida rotas HTTP de controle de robô (exclusivo de ADMIN) e integridade do endpoint SSE."""
    h = admin_auth.headers
    assert client.post("/api/disputes/track", json={"id_pregao": "X", "numero_item": 1}).status_code == 401
    # 1. Cadastra item via API
    resp_track = client.post("/api/disputes/track", headers=h, json={
        "id_pregao": "PE 808/2026",
        "numero_item": 1,
        "descricao": "Solução de Backup em Nuvem",
        "valor_referencia": 25000.0,
        "melhor_lance": 24000.0,
        "meu_lance": 24000.0,
        "fase": "ABERTO",
        "degrau_minimo": 0.01,
        "valor_minimo_limite": 18000.0,
        "posicao_alvo": 1,
        "auto_lance_ativo": False
    })
    assert resp_track.status_code == 200
    key = "PE 808/2026:1"

    # 2. POST /api/disputes/configure/{item_id}
    resp_cfg = client.post(f"/api/disputes/configure/{key}", headers=h, json={
        "degrau_minimo": 0.02,
        "valor_minimo_limite": 19500.0,
        "posicao_alvo": 1,
        "auto_lance_ativo": True
    })
    assert resp_cfg.status_code == 200
    cfg_data = resp_cfg.json()
    assert cfg_data["status"] == "success"
    assert cfg_data["item"]["degrau_minimo"] == 0.02
    assert cfg_data["item"]["valor_minimo_limite"] == 19500.0
    assert cfg_data["item"]["auto_lance_ativo"] is True

    # 3. POST /api/disputes/toggle-global
    resp_tg = client.post("/api/disputes/toggle-global", headers=h, json={"status": True})
    assert resp_tg.status_code == 200
    assert resp_tg.json()["global_status"] is True

    # 4. POST /api/disputes/bid/{item_id} (manual bid)
    resp_bid = client.post(f"/api/disputes/bid/{key}", headers=h, json={"valor": 23950.0})
    assert resp_bid.status_code == 200
    assert resp_bid.json()["item"]["meu_lance"] == 23950.0

    # 5. Melhor lance informado do portal
    resp_ml = client.post(f"/api/disputes/melhor-lance/{key}", headers=h, json={"valor": 23900.0})
    assert resp_ml.status_code == 200
    hist = resp_ml.json()["item"]["historico_concorrentes"]
    assert any(h["valor"] == 23900.0 and h["autor"] == "Melhor lance informado" for h in hist)
    # Robô ativo (toggle-global): cobre o lance informado com o degrau configurado
    assert resp_ml.json()["item"]["meu_lance"] == 23899.98
    assert client.post(f"/api/disputes/melhor-lance/{key}", headers=h, json={"valor": "abc"}).status_code == 422

    # 6. Remoção do lote
    assert client.delete(f"/api/disputes/{key}", headers=h).status_code == 200
    assert client.get("/api/disputes/active", headers=h).json()["total"] == 0

@pytest.mark.asyncio
async def test_sse_endpoint_stream_integrity():
    """Valida resolução de dependências assíncronas do SSE (asyncio) e captura limpa de CancelledError."""
    import server.main as main_mod
    assert hasattr(main_mod, "asyncio")
    assert hasattr(main_mod.asyncio, "CancelledError")

    from starlette.requests import Request
    from starlette.responses import StreamingResponse
    from server.main import api_disputes_sse_events

    scope = {"type": "http", "method": "GET", "path": "/api/disputes/events", "headers": []}
    req = Request(scope)
    res = await api_disputes_sse_events(req)
    assert isinstance(res, StreamingResponse)
    assert res.media_type == "text/event-stream"
    assert res.headers["Cache-Control"] == "no-cache"

    gen = res.body_iterator
    first_chunk = await gen.__anext__()
    assert "data:" in first_chunk
    assert "CONNECTED" in first_chunk

    # Fechamento gracioso dispara CancelledError internamente sem exceção não capturada
    await gen.aclose()


def test_ui_templates_elements_disputa_and_base(admin_auth):
    """Valida elementos de navegação mobile e accordion na renderização HTML (chave geral: ADMIN)."""
    # 1. Página de Disputa com Accordion e Chave Geral
    res_disputa = client.get("/disputa", headers=admin_auth.headers)
    assert res_disputa.status_code == 200
    html_disputa = res_disputa.text
    assert "Cockpit Multi-Pregão" in html_disputa
    assert "btn-global-autobid" in html_disputa
    assert "dispute-accordion" in html_disputa
    assert "Degrau Mínimo" in html_disputa
    assert "Piso Stop-Loss" in html_disputa
    assert "Margem de Segurança" in html_disputa
    assert "toggleGlobalAutobid" in html_disputa
    assert "toggleItemAutobid" in html_disputa

    # 2. Template Base com Mobile Drawer e Bottom Navigation Bar
    res_home = client.get("/")
    assert res_home.status_code == 200
    html_home = res_home.text
    assert "mobile-menu-toggle" in html_home
    assert "mobile-menu-drawer" in html_home
    assert "mobile-menu-backdrop" in html_home
    assert "safe-area-bottom" in html_home
    assert "pb-24 md:pb-0" in html_home
    assert "Disputa AO VIVO" in html_home


def test_sala_de_disputa_inicia_vazia_e_isolada_por_usuario(criar_usuario):
    """Sem lotes de exemplo: cada conta vê, configura e recebe eventos apenas dos próprios lotes."""
    a, b = criar_usuario("Empresa A", role="ADMIN"), criar_usuario("Empresa B", role="ADMIN")
    assert client.get("/api/disputes/active", headers=a.headers).json()["total"] == 0
    assert client.get("/api/disputes/active").json()["items"] == []  # visitante

    lote = {"id_pregao": "PE 909/2026", "numero_item": 1, "descricao": "Lote da Empresa A",
            "valor_referencia": 1000.0, "melhor_lance": 950.0, "meu_lance": 960.0}
    assert client.post("/api/disputes/track", headers=a.headers, json=lote).status_code == 200
    key = "PE 909/2026:1"
    try:
        assert client.get("/api/disputes/active", headers=a.headers).json()["total"] == 1
        assert client.get("/api/disputes/active", headers=b.headers).json()["total"] == 0
        item_a = client.get("/api/disputes/active", headers=a.headers).json()["items"][0]
        assert item_a["historico_concorrentes"] == []  # sem concorrentes inventados

        # B não configura, não lança, não informa melhor lance e não remove o lote de A
        assert client.post(f"/api/disputes/configure/{key}", headers=b.headers, json={"auto_lance_ativo": True}).status_code == 404
        assert client.post(f"/api/disputes/bid/{key}", headers=b.headers, json={"valor": 900}).status_code == 404
        assert client.post(f"/api/disputes/melhor-lance/{key}", headers=b.headers, json={"valor": 900}).status_code == 404
        assert client.delete(f"/api/disputes/{key}", headers=b.headers).status_code == 404
        assert client.post("/api/disputes/toggle-global", headers=b.headers, json={"status": True}).json()["total_updated"] == 0

        # Eventos SSE: a fila de B não recebe eventos do lote de A
        fila_a, fila_b = dispute_monitor.subscribe(a.id), dispute_monitor.subscribe(b.id)
        try:
            client.post(f"/api/disputes/melhor-lance/{key}", headers=a.headers, json={"valor": 940})
            assert not fila_a.empty()
            assert fila_b.empty()
        finally:
            dispute_monitor.unsubscribe(fila_a)
            dispute_monitor.unsubscribe(fila_b)
    finally:
        client.delete(f"/api/disputes/{key}", headers=a.headers)


def test_robo_de_lances_antigo_e_exclusivo_de_admin(usuario_auth):
    """Conta comum acompanha o lote (melhor lance, ranking), mas não liga o robô nem dá lances."""
    h = usuario_auth.headers
    lote = {"id_pregao": "PE 4040/2026", "numero_item": 1, "descricao": "Lote", "valor_referencia": 1000.0,
            "melhor_lance": 950.0, "meu_lance": 960.0, "auto_lance_ativo": True}
    resp = client.post("/api/disputes/track", headers=h, json=lote)
    assert resp.status_code == 200 and resp.json()["item"]["auto_lance_ativo"] is False
    key = "PE 4040/2026:1"
    try:
        assert client.post(f"/api/disputes/configure/{key}", headers=h, json={"auto_lance_ativo": True}).status_code == 403
        assert client.post(f"/api/disputes/configure/{key}", headers=h, json={"valor_minimo_limite": 800}).status_code == 200
        assert client.post(f"/api/disputes/bid/{key}", headers=h, json={"valor": 940}).status_code == 403
        assert client.post("/api/disputes/toggle-global", headers=h, json={"status": True}).status_code == 403
        assert client.post(f"/api/disputes/melhor-lance/{key}", headers=h, json={"valor": 930}).status_code == 200
        html = client.get("/disputa", headers=h).text
        assert 'id="btn-global-autobid"' not in html and "const IS_ADMIN = false;" in html
    finally:
        client.delete(f"/api/disputes/{key}", headers=h)
