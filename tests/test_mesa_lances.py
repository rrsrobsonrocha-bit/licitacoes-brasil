"""Mesa de Lances do Operador (ADMIN): trava de acesso, piso, ranking Top 10 e estratégia de menor perda."""
import uuid
from datetime import datetime
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from server.database import SessionLocal
from server.main import app
from server.models import AdminBid
from server.services import estrategia_lances as est
from server.services.dispute_monitor import dispute_monitor

client = TestClient(app)


# ------------------------------------------------------------------------------
# Estratégia de menor perda (funções puras)
# ------------------------------------------------------------------------------
def test_disputar_primeiro_lugar_cobre_pelo_degrau_minimo_sem_ir_ao_piso():
    r = est.calcular_lance([1010, 1000], piso=500, estrategia="lider", degrau="0.01")
    assert r["acao"] == "lance" and r["posicao"] == 1
    assert r["valor"] == 999.99  # nunca R$ 500 (o piso): só o necessário para superar o líder
    assert r["margem_sobre_piso"] == 499.99


def test_degrau_percentual_arredonda_para_cima_e_respeita_degrau_do_edital():
    assert est.calcular_lance([1000], 500, tipo_degrau="percentual", degrau="1")["valor"] == 990.00
    # 0,1% de 1.234,56 = 1,23456 -> 1,24 (o decremento nunca fica abaixo do percentual)
    assert est.calcular_lance([1234.56], 500, tipo_degrau="percentual", degrau="0.1")["valor"] == 1233.32
    assert est.calcular_lance([1000], 500, degrau="0.01", degrau_minimo_edital="0.50")["valor"] == 999.50
    for degrau in ("0.05", "0.10", "1.00"):
        assert est.calcular_lance([1000], 500, degrau=degrau)["valor"] == round(1000 - float(degrau), 2)


def test_primeiro_lugar_abaixo_do_piso_e_inviavel_na_estrategia_lider():
    r = est.calcular_lance([999.00, 1200], piso=1000, estrategia="lider")
    assert r["acao"] == "inviavel" and r["valor"] is None
    assert "abaixo do seu piso" in r["motivo"]


def test_melhor_colocacao_viavel_desce_o_ranking_ate_ficar_acima_do_piso():
    concorrentes = [950, 800, 1000, 900]  # ordem qualquer: o ranking é ordenado por menor preço
    r = est.calcular_lance(concorrentes, piso=905, estrategia="melhor_viavel")
    # 1º exigiria 799,99 e 2º exigiria 899,99 (abaixo de 905): melhor viável é o 3º, cobrindo 950
    assert r["acao"] == "lance" and r["posicao"] == 3 and r["alvo"] == 950.0
    assert r["valor"] == 949.99
    assert [t["viavel"] for t in r["tentativas"]] == [False, False, True]


def test_melhor_colocacao_viavel_considera_ate_o_decimo_lugar():
    concorrentes = [100 + i for i in range(12)]  # só os 10 primeiros contam
    r = est.calcular_lance(concorrentes, piso=109.50, estrategia="melhor_viavel")
    assert r["acao"] == "inviavel" and len(r["tentativas"]) == 10
    r = est.calcular_lance(concorrentes, piso=108.50, estrategia="melhor_viavel")
    assert r["posicao"] == 10 and r["valor"] == 108.99


def test_menor_perda_mantem_lance_que_ja_garante_a_colocacao():
    lider = est.calcular_lance([900, 950], piso=500, meu_ultimo_lance=899)
    assert lider["acao"] == "manter" and lider["valor"] == 899.0 and lider["posicao"] == 1
    # Líder inviável, mas o nosso lance atual já é o 2º: não baixa à toa
    viavel = est.calcular_lance([800, 900], piso=850, estrategia="melhor_viavel", meu_ultimo_lance=880)
    assert viavel["acao"] == "manter" and viavel["posicao"] == 2


def test_validacoes_da_estrategia():
    assert est.calcular_lance([], 100)["acao"] == "inviavel"
    with pytest.raises(est.EstrategiaInvalidaError):
        est.calcular_lance([100], 0)
    with pytest.raises(est.EstrategiaInvalidaError):
        est.calcular_lance([100], 50, estrategia="tudo")
    with pytest.raises(est.EstrategiaInvalidaError):
        est.calcular_lance([100], 50, tipo_degrau="fracao")
    with pytest.raises(est.EstrategiaInvalidaError):
        est.calcular_lance([100, -1], 50)
    assert est.posicao_do_lance(899.99, [900, 950]) == 1
    assert est.posicao_do_lance(900, [900, 950]) == 2  # empate: quem ofertou antes fica à frente


# ------------------------------------------------------------------------------
# API
# ------------------------------------------------------------------------------
@pytest.fixture
def pregao():
    """Identificador único de pregão (com barra, como os números do PNCP) e limpeza dos lances."""
    id_pregao = f"UASG 160001/{uuid.uuid4().hex[:8]}"
    yield id_pregao
    db = SessionLocal()
    db.query(AdminBid).filter(AdminBid.tender_id == id_pregao).delete(synchronize_session=False)
    db.commit()
    db.close()


def _base(id_pregao, item="1"):
    return f"/api/disputa/{quote(id_pregao, safe='')}/items/{item}"


def _acompanhar(conta, id_pregao):
    resp = client.post("/api/disputes/track", headers=conta.headers, json={
        "id_pregao": id_pregao, "numero_item": 1, "descricao": "Notebooks", "valor_referencia": 5000.0,
        "melhor_lance": 4800.0, "meu_lance": 0, "valor_minimo_limite": 3500.0, "degrau_minimo": 0.01,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["item"]["item_key"]


def test_mesa_de_lances_bloqueia_quem_nao_e_admin(pregao, usuario_auth):
    corpo = {"valor_lance": 1000, "preco_minimo_limite": 900}
    assert client.post(f"{_base(pregao)}/lance", headers=usuario_auth.headers, json=corpo).status_code == 403
    assert client.get(f"/api/disputa/{quote(pregao, safe='')}/meus-lances", headers=usuario_auth.headers).status_code == 403
    assert client.post(f"{_base(pregao)}/calcular", headers=usuario_auth.headers,
                       json={"preco_minimo_limite": 900}).status_code == 403
    assert client.post(f"{_base(pregao)}/lance", json=corpo).status_code == 401  # visitante
    db = SessionLocal()
    assert db.query(AdminBid).filter(AdminBid.tender_id == pregao).count() == 0
    db.close()


def test_lance_abaixo_do_piso_ou_sem_piso_e_rejeitado(pregao, admin_auth):
    url = f"{_base(pregao)}/lance"
    abaixo = client.post(url, headers=admin_auth.headers, json={"valor_lance": 899.99, "preco_minimo_limite": 900})
    assert abaixo.status_code == 422 and "trava de segurança" in abaixo.json()["detail"]
    sem_piso = client.post(url, headers=admin_auth.headers, json={"valor_lance": 1000})
    assert sem_piso.status_code == 422
    assert client.post(url, headers=admin_auth.headers,
                       json={"valor_lance": 1000, "preco_minimo_limite": 900, "colocacao_alvo": 11}).status_code == 422
    db = SessionLocal()
    assert db.query(AdminBid).filter(AdminBid.tender_id == pregao).count() == 0
    db.close()


def test_fluxo_completo_ranking_calculo_lance_e_historico(pregao, admin_auth):
    item_key = _acompanhar(admin_auth, pregao)
    ranking = [{"fornecedor": "Fornecedor B", "valor": 4100}, {"fornecedor": "Fornecedor A", "valor": 3400},
               {"fornecedor": "", "valor": "4.250,00"}]
    resp = client.post(f"/api/disputes/ranking/{quote(item_key, safe='')}", headers=admin_auth.headers,
                       json={"ranking": ranking})
    assert resp.status_code == 200, resp.text
    lote = resp.json()["item"]
    assert [r["valor"] for r in lote["ranking"]] == [3400.0, 4100.0, 4250.0]
    assert [r["posicao"] for r in lote["ranking"]] == [1, 2, 3]
    assert lote["ranking"][2]["fornecedor"] == "Fornecedor 3"

    # 1º lugar (3.399,99) fica abaixo do piso de 3.500: a melhor colocação viável é o 2º
    calc = client.post(f"{_base(pregao)}/calcular", headers=admin_auth.headers, json={
        "preco_minimo_limite": 3500, "estrategia": "melhor_viavel", "tipo_degrau": "valor", "degrau": "0.10"})
    assert calc.status_code == 200, calc.text
    assert calc.json()["acao"] == "lance" and calc.json()["posicao"] == 2 and calc.json()["valor"] == 4099.90
    lider = client.post(f"{_base(pregao)}/calcular", headers=admin_auth.headers,
                        json={"preco_minimo_limite": 3500, "estrategia": "lider"}).json()
    assert lider["acao"] == "inviavel"

    enviado = client.post(f"{_base(pregao)}/lance", headers=admin_auth.headers, json={
        "valor_lance": 4099.90, "preco_minimo_limite": 3500, "colocacao_alvo": 2, "observacao": "Melhor viável"})
    assert enviado.status_code == 201, enviado.text
    lance = enviado.json()["lance"]
    assert lance["valor_lance"] == 4099.90 and lance["preco_minimo_limite"] == 3500.0
    assert lance["colocacao_alvo"] == 2 and lance["status"] == "superado" and enviado.json()["posicao"] == 2
    assert dispute_monitor._find_item(item_key, owner_id=admin_auth.id).meu_lance == 4099.90

    # Concorrente líder sai do ranking: o último lance passa a líder
    client.post(f"/api/disputes/ranking/{quote(item_key, safe='')}", headers=admin_auth.headers,
                json={"ranking": [{"valor": 4100}, {"valor": 4250}]})
    historico = client.get(f"/api/disputa/{quote(pregao, safe='')}/meus-lances", headers=admin_auth.headers,
                           params={"item_id": "1"})
    assert historico.status_code == 200
    lances = historico.json()["lances"]
    assert len(lances) == 1 and lances[0]["status"] == "lider"
    assert datetime.fromisoformat(lances[0]["created_at"]).second is not None
    assert len(lances[0]["created_at"]) >= 19  # data e hora com segundos

    # Com o nosso lance de 4.099,90 já à frente de todos, a cobertura sugere manter (menor perda)
    manter = client.post(f"{_base(pregao)}/calcular", headers=admin_auth.headers,
                         json={"preco_minimo_limite": 3500}).json()
    assert manter["acao"] == "manter" and manter["valor"] == 4099.90


def test_historico_de_lances_e_privado_por_operador(pregao, admin_auth, criar_usuario):
    outro_admin = criar_usuario(role="ADMIN")
    client.post(f"{_base(pregao)}/lance", headers=admin_auth.headers, json={"valor_lance": 1000, "preco_minimo_limite": 900})
    meus = client.get(f"/api/disputa/{quote(pregao, safe='')}/meus-lances", headers=admin_auth.headers).json()
    assert meus["total"] == 1 and meus["lances"][0]["status"] == "enviado"  # sem ranking informado
    alheios = client.get(f"/api/disputa/{quote(pregao, safe='')}/meus-lances", headers=outro_admin.headers).json()
    assert alheios["total"] == 0


def test_ranking_valida_entradas_e_dono(pregao, usuario_auth, criar_usuario):
    item_key = _acompanhar(usuario_auth, pregao)
    url = f"/api/disputes/ranking/{quote(item_key, safe='')}"
    assert client.post(url, headers=usuario_auth.headers, json={"ranking": [{"valor": i + 1} for i in range(11)]}).status_code == 422
    assert client.post(url, headers=usuario_auth.headers, json={"ranking": [{"valor": 0}]}).status_code == 422
    assert client.post(url, headers=usuario_auth.headers, json={"ranking": [{"valor": "abc"}]}).status_code == 422
    # Ranking (monitoramento) é permitido a usuários comuns, no próprio lote
    assert client.post(url, headers=usuario_auth.headers, json={"ranking": [{"valor": 10}]}).status_code == 200
    assert client.post(url, headers=criar_usuario().headers, json={"ranking": [{"valor": 10}]}).status_code == 404


def test_pagina_mostra_mesa_de_lances_so_para_admin(admin_auth, usuario_auth):
    admin_html = client.get("/disputa", headers=admin_auth.headers).text
    comum_html = client.get("/disputa", headers=usuario_auth.headers).text
    assert "const IS_ADMIN = true;" in admin_html
    assert "const IS_ADMIN = false;" in comum_html
    assert "Ranking da Disputa (Top 10)" in comum_html and "Cobrir com Menor Perda" in admin_html


def test_tabela_admin_bids_tem_tipos_estritos_no_postgresql():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    ddl = str(CreateTable(AdminBid.__table__).compile(dialect=postgresql.dialect()))
    assert "valor_lance NUMERIC(14, 2) NOT NULL" in ddl
    assert "preco_minimo_limite NUMERIC(14, 2)," in ddl
    assert "created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in ddl
    assert "status VARCHAR(30) DEFAULT 'enviado' NOT NULL" in ddl
    assert "colocacao_alvo INTEGER DEFAULT '1' NOT NULL" in ddl or "colocacao_alvo INTEGER DEFAULT 1 NOT NULL" in ddl
    assert "id SERIAL NOT NULL" in ddl
