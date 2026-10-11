"""Catálogo global permanente, auto-warmup dos últimos 15 dias do PNCP, painel com volume de vigentes e busca."""
import asyncio
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.config import settings
from server.connectors.pncp import PNCPConnector
from server.database import SessionLocal
from server.main import app
from server.models import Tender, hoje_sao_paulo
from server.search_engine import agora_sao_paulo_naive
from server.services import auto_sync

client = TestClient(app)


class PNCPPorDia:
    """API do PNCP simulada que registra as consultas e devolve certames do dia pedido."""

    def __init__(self, prefixo: str, por_modalidade=None):
        self.prefixo = prefixo
        self.por_modalidade = por_modalidade or {6: 2, 8: 1}
        self.chamadas = []

    def __call__(self, data_inicial, data_final, codigo_modalidade=None, uf=None, pagina=1, tamanho_pagina=50):
        self.chamadas.append((data_inicial, data_final, codigo_modalidade, pagina))
        if pagina > 1:
            return {"success": True, "data": {"data": []}}
        dia = datetime.strptime(data_inicial, "%Y%m%d")
        prazo = (agora_sao_paulo_naive() + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S")
        registros = [
            {
                "numeroControlePNCP": f"{self.prefixo}-{data_inicial}-{codigo_modalidade}-{n}",
                "numeroCompra": f"{n}/2026",
                "objetoCompra": f"Aquisição real simulada {self.prefixo} {data_inicial} {n}",
                "modalidadeNome": "Pregão Eletrônico" if codigo_modalidade == 6 else "Dispensa Eletrônica",
                "situacaoCompraNome": "Divulgada no PNCP",
                "valorTotalEstimado": 1000.0 * (n + 1),
                "dataPublicacaoPncp": dia.replace(hour=10).strftime("%Y-%m-%dT%H:%M:%S"),
                "dataAberturaProposta": prazo,
                "dataEncerramentoProposta": prazo,
                "orgaoEntidade": {"cnpj": "00394452000103", "razaoSocial": f"Órgão {self.prefixo}", "esferaId": "F"},
                "unidadeOrgao": {"ufSigla": "DF", "municipioNome": "Brasília", "nomeUnidade": "Unidade"},
            }
            for n in range(self.por_modalidade.get(codigo_modalidade, 0))
        ]
        return {"success": True, "data": {"data": registros}}


def _apagar_por_prefixo(prefixo: str) -> None:
    db = SessionLocal()
    try:
        db.query(Tender).filter(Tender.external_id.like(f"{prefixo}%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _contar(prefixo: str = None) -> int:
    db = SessionLocal()
    try:
        q = db.query(Tender)
        if prefixo:
            q = q.filter(Tender.external_id.like(f"{prefixo}%"))
        return q.count()
    finally:
        db.close()


# ---------------- 1. Auto-warmup dos últimos 15 dias ----------------
def test_warmup_coleta_15_dias_do_mais_recente_ao_mais_antigo_e_deduplica(monkeypatch):
    prefixo = f"WARMUP-{uuid.uuid4().hex[:8]}"
    api = PNCPPorDia(prefixo)
    monkeypatch.setattr(settings, "AUTO_SYNC_DIAS", 15)
    alertas = MagicMock(return_value=[])
    try:
        with patch.object(PNCPConnector, "fetch_tenders_live", api), \
             patch("server.services.alert_service.check_and_deliver_tender_alerts", alertas), \
             patch("server.integrations.opportunity_collector.time.sleep"):
            resumo = auto_sync.executar_warmup()

            hoje = hoje_sao_paulo()
            esperados = [(hoje - timedelta(days=i)).strftime("%Y%m%d") for i in range(15)]
            dias_consultados = []
            for ini, fim, modalidade, _pagina in api.chamadas:
                assert ini == fim  # um dia por vez
                assert modalidade in (6, 8)  # só modalidades com disputa
                if ini not in dias_consultados:
                    dias_consultados.append(ini)
            assert dias_consultados == esperados  # do mais recente para o mais antigo

            assert resumo["novos"] == 15 * 3 and not resumo["falhas"]
            assert _contar(prefixo) == 45
            alertas.assert_not_called()  # acervo retroativo não dispara alertas em massa

            # Segunda execução: upsert por numeroControlePNCP, sem duplicar
            resumo2 = auto_sync.executar_warmup()
            assert resumo2["novos"] == 0 and resumo2["atualizados"] == 45
            assert _contar(prefixo) == 45
    finally:
        _apagar_por_prefixo(prefixo)


def test_atualizacao_periodica_cobre_hoje_e_ontem_e_entrega_alertas():
    prefixo = f"ATUALIZA-{uuid.uuid4().hex[:8]}"
    api = PNCPPorDia(prefixo, {6: 1, 8: 0})
    alertas = MagicMock(return_value=[])
    try:
        with patch.object(PNCPConnector, "fetch_tenders_live", api), \
             patch("server.services.alert_service.check_and_deliver_tender_alerts", alertas):
            resumo = auto_sync.executar_atualizacao()
        hoje = hoje_sao_paulo()
        assert {c[0] for c in api.chamadas} == {hoje.strftime("%Y%m%d"), (hoje - timedelta(days=1)).strftime("%Y%m%d")}
        assert resumo["novos"] == 2
        assert alertas.call_count == 2
    finally:
        _apagar_por_prefixo(prefixo)


def test_api_indisponivel_nao_insere_nem_apaga_nada():
    antes = _contar()

    def _falha(self, *args, **kwargs):
        return {"success": False, "error": "offline"}

    with patch.object(PNCPConnector, "fetch_tenders_live", _falha):
        resumo = auto_sync.executar_warmup(dias=3)
    assert resumo["novos"] == 0
    assert _contar() == antes  # catálogo permanente: nada é removido


def test_decisao_de_warmup(monkeypatch):
    monkeypatch.setattr(auto_sync, "em_producao", lambda: False)
    monkeypatch.setattr(settings, "AUTO_SYNC_MIN_VIGENTES", 10 ** 9)
    assert auto_sync.precisa_warmup(no_arranque=False) is True  # poucos vigentes

    monkeypatch.setattr(settings, "AUTO_SYNC_MIN_VIGENTES", 0)
    assert auto_sync.precisa_warmup(no_arranque=True) is False  # dev com acervo suficiente

    monkeypatch.setattr(auto_sync, "em_producao", lambda: True)
    assert auto_sync.precisa_warmup(no_arranque=True) is True  # todo arranque em produção
    assert auto_sync.precisa_warmup(no_arranque=False) is False


def test_loop_dispara_warmup_no_arranque_sem_acao_do_usuario(monkeypatch):
    warmup = MagicMock(return_value={"tipo": "warmup"})
    atualizacao = MagicMock(return_value={"tipo": "atualizacao"})
    monkeypatch.setattr(auto_sync, "executar_warmup", warmup)
    monkeypatch.setattr(auto_sync, "executar_atualizacao", atualizacao)
    monkeypatch.setattr(auto_sync, "precisa_warmup", lambda no_arranque: no_arranque)

    async def _rodar():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(auto_sync.loop_sincronizacao_automatica(atraso_inicial=0), timeout=1)

    asyncio.run(_rodar())
    warmup.assert_called_once()
    atualizacao.assert_not_called()
    assert auto_sync.estado["proxima_execucao"]


def test_status_da_sincronizacao_e_publico():
    dados = client.get("/api/sync/status").json()
    for chave in ("ativo", "intervalo_minutos", "dias_warmup", "total_editais", "editais_vigentes", "ultima_execucao"):
        assert chave in dados
    assert dados["editais_vigentes"] <= dados["total_editais"]


# ---------------- 2. Catálogo global com acesso imediato ----------------
def test_conta_nova_ve_todo_o_catalogo_imediatamente(criar_usuario):
    conta = criar_usuario("Conta Recém-Criada")
    anonimo = client.get("/api/tenders?vigentes=true&page_size=5").json()
    logado = client.get("/api/tenders?vigentes=true&page_size=5", headers=conta.headers).json()
    assert logado["total"] == anonimo["total"] == _contar_vigentes()


def _contar_vigentes() -> int:
    return auto_sync.contar_vigentes()


# ---------------- 3. Painel e busca ----------------
def _tender(db, marcador: str, orgao: str, prazo_em_dias: int) -> str:
    agora = agora_sao_paulo_naive()
    t = Tender(
        source_id="pncp-source-01",
        external_id=f"{marcador}-{uuid.uuid4().hex[:10]}",
        title=f"Edital {marcador} {uuid.uuid4().hex[:6]}",
        object_description=f"Objeto {marcador}",
        contracting_entity=orgao,
        state="SP",
        modality="Pregão Eletrônico",
        status="Divulgada no PNCP",
        publication_date=agora - timedelta(days=3),
        proposal_deadline=agora + timedelta(days=prazo_em_dias),
        opening_date=agora + timedelta(days=prazo_em_dias),
        source_url="https://pncp.gov.br/app/editais/teste",
    )
    db.add(t)
    db.commit()
    return t.id


def test_painel_destaca_vigentes_orgaos_ativos_e_funil_pronto():
    marcador = f"PAINEL{uuid.uuid4().hex[:6]}"
    antes = client.get("/api/user/dashboard-summary").json()["companies_identified_count"]
    db = SessionLocal()
    try:
        _tender(db, marcador, f"Órgão Ativo A {marcador}", 10)
        _tender(db, marcador, f"Órgão Ativo B {marcador}", 10)
        _tender(db, marcador, f"Órgão Encerrado {marcador}", -5)
    finally:
        db.close()
    try:
        resumo = client.get("/api/user/dashboard-summary").json()
        assert resumo["companies_identified_count"] == antes + 2  # só órgãos com editais vigentes

        html = client.get("/dashboard").text
        # Quatro cards separados, nesta ordem: publicados hoje, vigentes, órgãos, negócios
        ordem = [html.index(f'id="{c}"') for c in ("card-tenders-today", "card-vigentes", "card-companies", "card-active-deals")]
        assert ordem == sorted(ordem)
        assert html.count("metric-card group cursor-pointer") == 4
        hoje = html.split('id="card-tenders-today"', 1)[1].split("</a>", 1)[0]
        assert "Editais publicados hoje" in hoje and 'id="stat-tenders-today"' in hoje
        vigentes = html.split('id="card-vigentes"', 1)[1].split("</a>", 1)[0]
        assert "Editais vigentes em disputa" in vigentes and 'id="stat-vigentes"' in vigentes
        assert "Órgãos mapeados" in html
        assert 'id="funil-vazio-cta"' in html and "Seu funil está pronto." in html
        # Botão manual ao lado do Funil Kanban, disparando a sincronização em background
        assert 'id="btn-dash-sync"' in html and "Sincronizar agora" in html and "/api/sync/run" in html
        assert html.index('id="btn-dash-sync"') < html.index("Funil Kanban")
    finally:
        _apagar_por_prefixo(marcador)


def test_funil_vazio_em_negocios_convida_a_explorar_editais(criar_usuario):
    conta = criar_usuario("Funil Vazio")
    html = client.get("/negocios", cookies=conta.cookies).text
    assert 'id="funil-vazio-cta"' in html and "/search?vigentes=true" in html


def test_busca_abre_com_vigentes_e_tem_atalhos_de_periodo():
    marcador = f"BUSCA{uuid.uuid4().hex[:6]}"
    db = SessionLocal()
    try:
        aberto = _tender(db, marcador, "Órgão Busca", 10)
        encerrado = _tender(db, marcador, "Órgão Busca", -5)
    finally:
        db.close()
    try:
        html = client.get(f"/search?q={marcador}").text
        assert aberto in html and encerrado not in html  # padrão: somente vigentes
        assert "Somente vigentes" in html

        todos = client.get(f"/search?q={marcador}&vigentes=false").text
        assert aberto in todos and encerrado in todos

        hoje = hoje_sao_paulo()
        pagina = client.get("/search").text
        ordem = [pagina.index(f'id="{i}"') for i in (
            "atalho-todas-vigentes", "atalho-ultimos-15-dias", "atalho-ultimos-7-dias", "atalho-publicadas-hoje")]
        assert ordem == sorted(ordem)
        assert f"date_from={(hoje - timedelta(days=14)).isoformat()}" in pagina
        assert '<input type="hidden" name="vigentes" value="false" />' in pagina

        # Período explícito não força o filtro de vigentes (publicadas no intervalo, abertas ou não)
        periodo = client.get(f"/search?q={marcador}&date_from={(hoje - timedelta(days=14)).isoformat()}&date_to={hoje.isoformat()}").text
        assert aberto in periodo and encerrado in periodo
    finally:
        _apagar_por_prefixo(marcador)


# ---------------- 4. Paginação completa, limite de requisições e progresso ----------------
class PNCPPaginado:
    """PNCP simulado com totalPaginas: Pregão em várias páginas; uma página pode falhar (429 persistente)."""

    def __init__(self, prefixo: str, total_paginas: int = 4, ultima: int = 20, falha_na_pagina: int = 0):
        self.prefixo, self.total_paginas, self.ultima, self.falha_na_pagina = prefixo, total_paginas, ultima, falha_na_pagina
        self.chamadas = []
        self.progresso_visto = []
        self.gravados_ao_pedir = []

    def __call__(self, data_inicial, data_final, codigo_modalidade=None, uf=None, pagina=1, tamanho_pagina=50):
        self.chamadas.append((codigo_modalidade, pagina))
        self.progresso_visto.append(auto_sync.estado.get("progresso"))
        self.gravados_ao_pedir.append(_contar(self.prefixo))
        if codigo_modalidade != 6:
            return {"success": True, "data": {"data": [], "totalPaginas": 0}}
        if pagina == self.falha_na_pagina:
            return {"success": False, "error": "HTTP 429: Limite de Requisições Excedido", "status_code": 429}
        qtd = self.ultima if pagina == self.total_paginas else 50
        base = PNCPPorDia(f"{self.prefixo}-p{pagina}", {6: qtd})
        dados = base(data_inicial, data_final, codigo_modalidade, uf, 1, tamanho_pagina)["data"]
        dados["totalPaginas"] = self.total_paginas
        return {"success": True, "data": dados}


def test_warmup_pagina_ate_total_paginas_gravando_cada_pagina_e_reportando_progresso():
    prefixo = f"PAGINADO-{uuid.uuid4().hex[:8]}"
    api = PNCPPaginado(prefixo, total_paginas=4, ultima=20)
    try:
        with patch.object(PNCPConnector, "fetch_tenders_live", api):
            resumo = auto_sync.executar_warmup(dias=1)
        assert [p for m, p in api.chamadas if m == 6] == [1, 2, 3, 4]  # todas as páginas do dia
        assert resumo["novos"] == 170 and not resumo["falhas"]
        # Cada página já estava gravada (commit) antes de a próxima ser pedida
        assert api.gravados_ao_pedir[:4] == [0, 50, 100, 150]
        # O andamento é publicado em /api/sync/status durante a coleta
        antes_da_pagina_4 = api.progresso_visto[3]
        assert antes_da_pagina_4["modalidade"] == "Pregão Eletrônico"
        assert (antes_da_pagina_4["pagina"], antes_da_pagina_4["total_paginas"]) == (3, 4)
        assert antes_da_pagina_4["coletados"] == 150 and antes_da_pagina_4["dia_indice"] == 1
        assert auto_sync.estado["progresso"] is None  # limpo ao terminar
    finally:
        _apagar_por_prefixo(prefixo)


def test_bloqueio_persistente_da_api_vira_falha_registrada_sem_perder_paginas_gravadas():
    prefixo = f"BLOQUEIO-{uuid.uuid4().hex[:8]}"
    api = PNCPPaginado(prefixo, total_paginas=3, falha_na_pagina=2)
    try:
        with patch.object(PNCPConnector, "fetch_tenders_live", api):
            resumo = auto_sync.executar_warmup(dias=1)
        assert resumo["novos"] == 50 and _contar(prefixo) == 50
        assert any("página 2" in f and "429" in f for f in resumo["falhas"])
    finally:
        _apagar_por_prefixo(prefixo)


def test_conector_tenta_de_novo_com_backoff_quando_pncp_responde_429(monkeypatch):
    from server.connectors import pncp as modulo_pncp

    monkeypatch.setattr(settings, "PNCP_MAX_RETRIES", 3)
    respostas = [MagicMock(status_code=429, text="Limite de Requisições Excedido", headers={}),
                 MagicMock(status_code=429, text="Limite de Requisições Excedido", headers={}),
                 MagicMock(status_code=200, headers={}, json=MagicMock(return_value={"data": [], "totalPaginas": 0}))]
    conector = PNCPConnector()
    esperas = []
    with patch.object(conector.session, "get", side_effect=respostas), \
         patch.object(modulo_pncp.time, "sleep", side_effect=esperas.append):
        r = conector.fetch_tenders_live("20261001", "20261001", codigo_modalidade=6, pagina=2)
    assert r["success"] is True
    assert esperas == [10.0, 20.0]  # backoff exponencial

    # Erro definitivo (4xx diferente de 429) não é repetido
    with patch.object(conector.session, "get", return_value=MagicMock(status_code=400, text="parâmetro inválido", headers={})) as get, \
         patch.object(modulo_pncp.time, "sleep"):
        r = conector.fetch_tenders_live("20261001", "20261001", codigo_modalidade=6)
    assert r["success"] is False and get.call_count == 1


def test_limitador_espaca_as_consultas_ao_pncp(monkeypatch):
    from server.connectors import pncp as modulo_pncp

    monkeypatch.setattr(settings, "PNCP_RATE_LIMIT_PER_MINUTE", 12)
    monkeypatch.setattr(modulo_pncp, "_ultima_requisicao", modulo_pncp.time.monotonic())
    esperas = []
    with patch.object(modulo_pncp.time, "sleep", side_effect=esperas.append):
        modulo_pncp._aguardar_vez()
    assert len(esperas) == 1 and 4.5 < esperas[0] <= 5.0  # 12/min = uma consulta a cada 5s


def test_botao_sincronizar_exige_login_e_dispara_em_background(criar_usuario, monkeypatch):
    assert client.post("/api/sync/run").status_code == 401

    disparo = MagicMock(return_value=True)
    monkeypatch.setattr(auto_sync, "disparar_em_background", disparo)
    conta = criar_usuario("Quem Sincroniza")
    resp = client.post("/api/sync/run", headers=conta.headers)
    assert resp.status_code == 202 and resp.json()["iniciada"] is True
    disparo.assert_called_once()

    disparo.return_value = False
    assert client.post("/api/sync/run", headers=conta.headers).json()["iniciada"] is False
    assert "progresso" in client.get("/api/sync/status").json()
