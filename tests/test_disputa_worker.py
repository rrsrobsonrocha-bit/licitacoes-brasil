"""Robô de Lances Desktop: API do worker (config, heartbeat, Kill-Switch, auditoria) e travas do motor local."""
import importlib.util
import os
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from server.database import SessionLocal
from server.main import app
from server.models import AdminBid, HistoricoDisputa
from server.services.dispute_monitor import dispute_monitor

client = TestClient(app)

_CAMINHO_ROBO = os.path.join(os.path.dirname(__file__), "..", "scripts", "robo_desktop", "main.py")
_spec = importlib.util.spec_from_file_location("robo_desktop_main", _CAMINHO_ROBO)
robo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(robo)


def _rota(item_key, sufixo):
    return f"/api/disputa/{item_key}/{sufixo}"


@pytest.fixture
def lote(admin_auth):
    """Lote no cockpit do ADMIN: melhor lance 1000, meu lance 1000 (liderando), piso 900, degrau 10."""
    id_pregao = f"WRK{uuid.uuid4().hex[:8]}"
    resp = client.post("/api/disputes/track", headers=admin_auth.headers, json={
        "id_pregao": id_pregao, "numero_item": 1, "descricao": "Notebooks", "valor_referencia": 1200,
        "melhor_lance": 1000, "meu_lance": 1000, "valor_minimo_limite": 900, "degrau_minimo": 10,
        "fonte_portal": "Compras.gov.br", "auto_lance_ativo": False,
    })
    assert resp.status_code == 200, resp.text
    item_key = resp.json()["item"]["item_key"]
    yield {"key": item_key, "id_pregao": id_pregao, "headers": admin_auth.headers, "admin": admin_auth}
    dispute_monitor.remove_item(item_key, owner_id=admin_auth.id)
    db = SessionLocal()
    db.query(HistoricoDisputa).filter(HistoricoDisputa.id_pregao == id_pregao).delete(synchronize_session=False)
    db.query(AdminBid).filter(AdminBid.tender_id == id_pregao).delete(synchronize_session=False)
    db.commit()
    db.close()


def _item(lote):
    return dispute_monitor._find_item(lote["key"], owner_id=lote["admin"].id)


# ------------------------------------------------------------------ API do worker
def test_config_do_worker_exclusiva_de_admin(lote, usuario_auth):
    cfg = client.get(_rota(lote["key"], "worker-config"), headers=lote["headers"]).json()
    assert cfg == {
        "item_id": lote["key"], "id_pregao": lote["id_pregao"], "numero_item": 1, "objeto": "Notebooks",
        "preco_minimo": 900.0, "decremento_minimo": 10.0, "status_robo": "DESLIGADO", "kill_switch": False,
        "portal": "comprasnet", "fase": "ABERTO", "melhor_lance": 1000.0, "meu_lance": 1000.0,
    }
    for sufixo, metodo in (("worker-config", "get"), ("worker-heartbeat", "post"), ("worker-log", "post"),
                           ("kill-switch", "post")):
        kwargs = {} if metodo == "get" else {"json": {"valor": "950"}}
        assert getattr(client, metodo)(_rota(lote["key"], sufixo), headers=usuario_auth.headers, **kwargs).status_code == 403
        assert getattr(client, metodo)(_rota(lote["key"], sufixo), **kwargs).status_code == 401
    # Lote de outra conta (ou inexistente) não vaza
    assert client.get(_rota("NAOEXISTE:1", "worker-config"), headers=lote["headers"]).status_code == 404


def test_heartbeat_registra_sinal_de_vida_e_kill_switch_manda_abortar(lote):
    ok = client.post(_rota(lote["key"], "worker-heartbeat"), headers=lote["headers"],
                     json={"status": "OPERANDO", "latencia_ms": 42, "versao": "1.0.0"}).json()
    assert ok["abortar"] is False and ok["config"]["preco_minimo"] == 900.0
    item = _item(lote)
    assert item.worker["latencia_ms"] == 42 and item.worker["status"] == "OPERANDO" and item.worker_online

    client.post("/api/disputes/configure/" + lote["key"], headers=lote["headers"], json={"auto_lance_ativo": True})
    kill = client.post(_rota(lote["key"], "kill-switch"), headers=lote["headers"], json={"ativo": True}).json()
    assert kill["kill_switch"] is True and kill["item"]["auto_lance_ativo"] is False

    resp = client.post(_rota(lote["key"], "worker-heartbeat"), headers=lote["headers"], json={"latencia_ms": 10})
    assert resp.json()["abortar"] is True and "Kill-Switch" in resp.json()["motivo"]
    assert client.get(_rota(lote["key"], "worker-config"), headers=lote["headers"]).json()["kill_switch"] is True

    client.post(_rota(lote["key"], "kill-switch"), headers=lote["headers"], json={"ativo": False})
    assert client.post(_rota(lote["key"], "worker-heartbeat"), headers=lote["headers"], json={}).json()["abortar"] is False


def test_worker_log_audita_lance_e_trava_lance_abaixo_do_piso(lote):
    resp = client.post(_rota(lote["key"], "worker-log"), headers=lote["headers"], json={
        "valor": "990.00", "timestamp_ms": 1760000000123, "aceito": True, "resposta_portal": "HTTP 200: lance aceito",
        "latencia_ms": 87})
    assert resp.status_code == 201, resp.text
    corpo = resp.json()
    assert corpo["aceito"] is True and corpo["abortar"] is False and corpo["lance"]["valor_lance"] == 990.0
    assert _item(lote).meu_lance == 990.0

    recusado = client.post(_rota(lote["key"], "worker-log"), headers=lote["headers"], json={
        "valor": "980", "timestamp_ms": 1760000001000, "aceito": False, "resposta_portal": "HTTP 409: fora do intervalo"})
    assert recusado.status_code == 201 and recusado.json()["lance"] is None

    db = SessionLocal()
    try:
        eventos = db.query(HistoricoDisputa).filter(HistoricoDisputa.id_pregao == lote["id_pregao"]).order_by(HistoricoDisputa.id).all()
        assert [e.tipo_evento for e in eventos] == ["WORKER_BID", "WORKER_BID_REJEITADO"]
        assert eventos[0].detalhes["timestamp_ms"] == 1760000000123 and eventos[0].detalhes["resposta_portal"] == "HTTP 200: lance aceito"
        lances = db.query(AdminBid).filter(AdminBid.tender_id == lote["id_pregao"]).all()
        assert len(lances) == 1 and lances[0].valor_lance == Decimal("990.00") and lances[0].preco_minimo_limite == Decimal("900.00")
        assert lances[0].observacao.startswith("Robô desktop: HTTP 200")
    finally:
        db.close()

    # Segunda trava: lance abaixo do piso chegando ao servidor aciona o Kill-Switch
    abaixo = client.post(_rota(lote["key"], "worker-log"), headers=lote["headers"], json={"valor": "899.99", "timestamp_ms": 1})
    assert abaixo.status_code == 422 and abaixo.json()["detail"]["abortar"] is True
    assert _item(lote).kill_switch is True
    assert client.post(_rota(lote["key"], "worker-heartbeat"), headers=lote["headers"], json={}).json()["abortar"] is True


def test_servidor_nao_simula_lance_com_robo_desktop_online(lote):
    client.post(_rota(lote["key"], "worker-heartbeat"), headers=lote["headers"], json={})
    client.post("/api/disputes/configure/" + lote["key"], headers=lote["headers"], json={"auto_lance_ativo": True})
    client.post("/api/disputes/melhor-lance/" + lote["key"], headers=lote["headers"], json={"valor": 950})
    item = _item(lote)
    assert item.melhor_lance == 950.0 and item.meu_lance == 1000.0  # o lance real é do robô desktop


def test_cockpit_tem_kill_switch_e_status_do_robo(admin_auth):
    html = client.get("/disputa", cookies=admin_auth.cookies).text
    assert "acionarKillSwitch(" in html and "statusWorker(" in html and "WORKER_HEARTBEAT" in html


# ------------------------------------------------------------------ Motor local (scripts/robo_desktop/main.py)
def test_trava_de_stop_loss_aborta_antes_de_qualquer_envio():
    assert robo.calcular_proximo_lance(1000, 1010, 10, 900) == Decimal("990.00")
    assert robo.calcular_proximo_lance(1000, 1000, 10, 900) is None  # já liderando
    assert robo.calcular_proximo_lance(0, 0, 10, 900) is None
    with pytest.raises(robo.StopLossViolado):
        robo.calcular_proximo_lance(905, 1000, 10, 900)
    with pytest.raises(robo.StopLossViolado):
        robo.trava_stop_loss(950, 0)  # sem piso, não opera

    class SessaoQueNaoPodeSerUsada:
        headers, cookies = {}, type("C", (), {"set": lambda *a: None})()

        def request(self, *a, **k):
            raise AssertionError("Nenhum byte pode sair abaixo do piso")

    adaptador = robo.AdaptadorComprasNet({"url_lance": "https://portal.invalid/lance", "payload_modelo": {"v": "{valor}"}},
                                         sessao=SessaoQueNaoPodeSerUsada())
    with pytest.raises(robo.StopLossViolado):
        adaptador.enviar_lance(Decimal("899.99"), Decimal("900"))
    assert adaptador.montar_payload(Decimal("950.5")) == {"v": "950.50"}
    assert robo.AdaptadorBNC({"url_lance": "x", "payload_modelo": {"v": "{valor}"}},
                             sessao=SessaoQueNaoPodeSerUsada()).montar_payload(Decimal("950.5")) == {"v": "950,50"}
    with pytest.raises(ValueError):
        robo.criar_adaptador({"modo": "bnc"}, "comprasnet")
    assert isinstance(robo.criar_adaptador({}, "comprasnet"), robo.AdaptadorSimulacao)


def _robo_contra_o_app(lote, relogio):
    token = lote["admin"].token
    backend = robo.ClienteBackend("http://testserver", token, lote["key"], sessao=client)
    saida = []
    r = robo.Robo(backend, robo.AdaptadorSimulacao(), intervalo_minimo=20, relogio=lambda: relogio[0], saida=saida.append)
    return r, saida


def test_motor_local_disputa_em_simulacao_e_suspende_sem_conexao(lote):
    relogio = [1000.0]
    r, saida = _robo_contra_o_app(lote, relogio)
    cfg = r.handshake()
    assert cfg["preco_minimo"] == 900.0 and "Notebooks" in robo.tela_inicial(cfg, r.adaptador, r.intervalo_minimo)

    assert r.ciclo() == "AGUARDANDO_LIGAR"  # robô desligado no painel
    client.post("/api/disputes/configure/" + lote["key"], headers=lote["headers"], json={"auto_lance_ativo": True})
    r.pulso()
    assert r.ciclo() == "LIDERANDO"
    r.pulso()  # robô online: o servidor deixa de simular lances
    client.post("/api/disputes/melhor-lance/" + lote["key"], headers=lote["headers"], json={"valor": 950})
    r.pulso()
    assert r.ciclo() == "LANCE_ENVIADO"
    assert _item(lote).meu_lance == 940.0 and r.lances_enviados == 1  # auditado e refletido no cockpit
    assert any("LANCE R$ 940.00 -> ACEITO" in linha for linha in saida)

    # Concorrente cobre de novo: respeita o intervalo mínimo entre lances próprios
    client.post("/api/disputes/melhor-lance/" + lote["key"], headers=lote["headers"], json={"valor": 930})
    r.pulso()
    assert r.ciclo() == "INTERVALO_MINIMO"

    # Mais de 6 s sem contato com o servidor: lances suspensos
    relogio[0] += 21
    r.ultimo_contato = relogio[0] - 6.5
    assert r.ciclo() == "SUSPENSO_SEM_CONEXAO"
    r.pulso()
    assert r.ciclo() == "LANCE_ENVIADO" and _item(lote).meu_lance == 920.0

    # Próxima cobertura furaria o piso (905 - 10 < 900): aborta sem enviar
    client.post("/api/disputes/melhor-lance/" + lote["key"], headers=lote["headers"], json={"valor": 905})
    relogio[0] += 21
    r.pulso()
    with pytest.raises(robo.StopLossViolado):
        r.ciclo()
    assert _item(lote).meu_lance == 920.0


def test_motor_local_para_com_kill_switch_do_painel(lote):
    relogio = [0.0]
    r, _ = _robo_contra_o_app(lote, relogio)
    r.handshake()
    client.post(_rota(lote["key"], "kill-switch"), headers=lote["headers"], json={"ativo": True})
    r.pulso()
    assert r.parar.is_set() and "Kill-Switch" in r.motivo_parada
    with pytest.raises(robo.RoboAbortado):
        r.ciclo()
    # Loop com threads: a parada é atendida no próximo tick (~50 ms) e devolve o código de Kill-Switch
    assert r.executar() == robo.SAIDA_KILL
