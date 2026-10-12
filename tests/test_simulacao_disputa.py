"""Sandbox de disputa: 4 salas simuladas (sim-01 a sim-04), concorrentes virtuais e integração com o robô desktop."""
import random

import pytest
from fastapi.testclient import TestClient

from server.database import SessionLocal
from server.main import app
from server.models import AdminBid, HistoricoDisputa
from server.services import simulacao_disputa
from server.services.dispute_monitor import dispute_monitor
from tests.test_disputa_worker import robo  # motor local carregado de scripts/robo_desktop/main.py

client = TestClient(app)
IDS = ["sim-01", "sim-02", "sim-03", "sim-04"]


@pytest.fixture
def sandbox(admin_auth):
    resp = client.post("/api/disputa/criar-cenarios-simulacao", headers=admin_auth.headers)
    assert resp.status_code == 200, resp.text
    yield {"admin": admin_auth, "headers": admin_auth.headers, "resposta": resp.json()}
    for item in [it for it in dispute_monitor._itens_do_dono(admin_auth.id) if it.simulado]:
        dispute_monitor.remove_item(item.item_key, owner_id=admin_auth.id)
    simulacao_disputa._salas.clear()


def _item(sandbox, sala_id):
    return dispute_monitor._find_item(sala_id, owner_id=sandbox["admin"].id)


def _registros_no_banco():
    db = SessionLocal()
    try:
        return (db.query(HistoricoDisputa).filter(HistoricoDisputa.id_pregao.in_(IDS)).count()
                + db.query(AdminBid).filter(AdminBid.tender_id.in_(IDS)).count())
    finally:
        db.close()


def test_cria_as_4_salas_so_para_admin_e_reset_nao_duplica(sandbox, usuario_auth):
    itens = sandbox["resposta"]["itens"]
    assert [i["id_pregao"] for i in itens] == IDS and all(i["simulado"] for i in itens)
    esperado = {"sim-01": (120000, 92000, 100), "sim-02": (45000, 34500, 50),
                "sim-03": (30000, 26000, 50), "sim-04": (80000, 60000, 100)}
    for i in itens:
        assert (i["valor_referencia"], i["valor_minimo_limite"], i["degrau_minimo"]) == esperado[i["id_pregao"]]
        assert i["meu_lance"] == 0 and i["fase"] == "ABERTO" and "SIMULAÇÃO" in i["descricao"]

    client.post("/api/disputa/criar-cenarios-simulacao", headers=sandbox["headers"])
    simulados = [it for it in dispute_monitor._itens_do_dono(sandbox["admin"].id) if it.simulado]
    assert sorted(it.id_pregao for it in simulados) == IDS

    assert client.post("/api/disputa/criar-cenarios-simulacao", headers=usuario_auth.headers).status_code == 403
    assert client.post("/api/disputa/criar-cenarios-simulacao").status_code == 401
    assert not [it for it in dispute_monitor._itens_do_dono(usuario_auth.id) if it.simulado]


def test_salas_respondem_ao_worker_config_e_aceitam_heartbeat(sandbox):
    for sala_id in IDS:
        cfg = client.get(f"/api/disputa/{sala_id}/worker-config", headers=sandbox["headers"]).json()
        assert cfg["item_id"] == f"{sala_id}:1" and cfg["portal"] == "simulacao" and cfg["simulado"] is True
        batida = client.post(f"/api/disputa/{sala_id}/worker-heartbeat", headers=sandbox["headers"],
                             json={"status": "OPERANDO", "latencia_ms": 15})
        assert batida.status_code == 200 and batida.json()["abortar"] is False
        assert _item(sandbox, sala_id).worker_online


def test_concorrentes_virtuais_dao_lances_ate_o_proprio_limite(sandbox):
    rng = random.Random(7)
    agora = 1_000_000.0
    simulacao_disputa.criar_cenarios(sandbox["admin"].id, agora=agora, rng=rng)
    for _ in range(400):
        agora += 5
        simulacao_disputa.passo(agora=agora, rng=rng)

    notebooks = _item(sandbox, "sim-01")
    assert 95000 <= notebooks.melhor_lance < 120000  # para no limite do concorrente mais barato
    assert {r["fornecedor"] for r in notebooks.ranking} <= {"Empresa Alfa", "Beta Distribuidora"}
    assert notebooks.historico_concorrentes[0]["autor"] in ("Empresa Alfa", "Beta Distribuidora")
    # Sem o robô na sala, a briga para no limite do concorrente menos agressivo (sim-03: R$ 27.500)
    assert _item(sandbox, "sim-03").melhor_lance >= 27450
    # sim-04: sem lances novos, a prorrogação acaba e a sala encerra
    assert _item(sandbox, "sim-04").fase == "ENCERRADO"
    assert _registros_no_banco() == 0


def test_prorrogacao_rapida_adia_o_fim_a_cada_lance_na_reta_final(sandbox):
    rng = random.Random(1)
    agora = 500.0
    simulacao_disputa.criar_cenarios(sandbox["admin"].id, agora=agora, rng=rng)
    sala = simulacao_disputa._salas[(sandbox["admin"].id, "sim-04")]
    sala.proximo_lance_em = agora + 10_000  # concorrentes calados: só o lance do operador
    simulacao_disputa.passo(agora=agora + 160, rng=rng)
    assert _item(sandbox, "sim-04").fase == "IMINENCIA"
    dispute_monitor.registrar_meu_lance("sim-04", 79000, owner_id=sandbox["admin"].id)
    simulacao_disputa.passo(agora=agora + 175, rng=rng)
    assert sala.encerra_em == agora + 175 + 30
    simulacao_disputa.passo(agora=agora + 206, rng=rng)
    assert _item(sandbox, "sim-04").fase == "ENCERRADO"


def test_lances_do_robo_no_sandbox_nao_vao_para_o_banco(sandbox):
    log = client.post("/api/disputa/sim-01/worker-log", headers=sandbox["headers"],
                      json={"valor": "119900", "timestamp_ms": 1, "aceito": True, "resposta_portal": "SIMULACAO"})
    assert log.status_code == 201 and log.json()["simulado"] is True
    assert _item(sandbox, "sim-01").meu_lance == 119900.0
    abaixo = client.post("/api/disputa/sim-03/worker-log", headers=sandbox["headers"], json={"valor": "25000"})
    assert abaixo.status_code == 422 and _item(sandbox, "sim-03").kill_switch is True

    # Robô do servidor (sem o desktop conectado) também não grava histórico de sala simulada
    client.post("/api/disputes/configure/sim-02", headers=sandbox["headers"], json={"auto_lance_ativo": True})
    dispute_monitor.registrar_meu_lance("sim-02", 44000, owner_id=sandbox["admin"].id)
    dispute_monitor.registrar_melhor_lance("sim-02", 43900, owner_id=sandbox["admin"].id, autor="Empresa Alfa")
    assert _item(sandbox, "sim-02").meu_lance == 43850.0
    assert _registros_no_banco() == 0


def test_robo_desktop_disputa_a_sala_sim_03_e_para_no_stop_loss(sandbox):
    # Mesmo com config.json em modo portal real, sala simulada usa o adaptador de simulação
    assert isinstance(robo.criar_adaptador({"modo": "portal", "url_lance": "https://portal.invalid/x",
                                             "payload_modelo": {"v": "{valor}"}}, "simulacao"), robo.AdaptadorSimulacao)

    rng = random.Random(3)
    agora = [10_000.0]
    simulacao_disputa.criar_cenarios(sandbox["admin"].id, agora=agora[0], rng=rng)
    client.post("/api/disputes/configure/sim-03", headers=sandbox["headers"], json={"auto_lance_ativo": True})
    backend = robo.ClienteBackend("http://testserver", sandbox["admin"].token, "sim-03", sessao=client)
    r = robo.Robo(backend, robo.AdaptadorSimulacao(), intervalo_minimo=3, relogio=lambda: agora[0], saida=lambda _: None)
    r.handshake()

    with pytest.raises(robo.StopLossViolado):
        for _ in range(500):
            agora[0] += 4
            simulacao_disputa.passo(agora=agora[0], rng=rng)
            r.pulso()
            r.ciclo()
    item = _item(sandbox, "sim-03")
    assert r.lances_enviados > 0 and item.meu_lance >= 26000  # nenhum lance do robô abaixo do piso
    assert item.melhor_lance < 26050  # o concorrente agressivo levou o preço para baixo do piso + degrau
    assert item.historico_concorrentes[0]["autor"] == "Empresa Alfa"
    assert _registros_no_banco() == 0


def test_cockpit_tem_botao_do_sandbox_e_id_copiavel(admin_auth, usuario_auth):
    html = client.get("/disputa", cookies=admin_auth.cookies).text
    assert "Criar/Resetar 4 Salas de Simulação" in html and "criarSalasSimulacao()" in html
    assert "copiarIdRobo(" in html and "const idRobo = item.simulado ? item.id_pregao : item.item_key;" in html
    assert 'id="btn-criar-simulacao"' not in client.get("/disputa", cookies=usuario_auth.cookies).text
