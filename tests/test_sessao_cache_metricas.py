"""Cabeçalho com a sessão do servidor (Fornecedores e demais páginas), cache de métricas do catálogo e prefetch."""
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from server.database import SessionLocal
from server.main import app, get_dashboard_summary_data, get_fornecedores_data
from server.models import Tender
from server.search_engine import agora_sao_paulo_naive
from server.services import cache_metricas

client = TestClient(app)


def _botao_auth(html):
    trecho = html[html.index('id="auth-btn"'):]
    return trecho[:trecho.index("</button>")]


def test_fornecedores_reconhece_a_sessao_e_mostra_sair(usuario_auth):
    html = client.get("/fornecedores", cookies=usuario_auth.cookies).text
    botao = _botao_auth(html)
    assert "Sair" in botao and "Entrar" not in botao and "bg-rose-600" in botao
    assert 'data-usuario-nome="Conta de Teste"' in html and 'data-usuario-papel="USER"' in html
    assert re.search(r'id="user-display" class="hidden sm:inline-flex[^"]*"', html)
    assert '<span id="user-name" class="max-w-[7rem] truncate">Conta</span>' in html
    assert '<span id="mobile-auth-btn-text">Sair</span>' in html and ">Conta de Teste</div>" in html
    # O JS confia na conta do servidor quando o token não está no armazenamento local
    assert "AppSession.getUser() || AppSession.usuarioServidor()" in html
    assert "if (AppSession.getToken() || AppSession.usuarioServidor())" in html

    # Visitante continua vendo "Entrar"
    anonimo = client.get("/fornecedores").text
    assert "Entrar" in _botao_auth(anonimo) and "data-usuario-nome" not in anonimo
    assert '<span id="mobile-auth-btn-text">Entrar</span>' in anonimo


def test_todas_as_paginas_trazem_a_sessao_no_cabecalho(usuario_auth):
    for rota in ("/dashboard", "/disputa", "/alerts", "/search", "/agenda", "/documentos", "/fornecedores"):
        html = client.get(rota, cookies=usuario_auth.cookies).text
        assert "Sair" in _botao_auth(html), rota


def test_paginas_compactas_no_celular(usuario_auth):
    h = usuario_auth.cookies
    forn = client.get("/fornecedores", cookies=h).text
    assert re.search(r'>Fornecedores <span class="font-mono text-sm text-purple-400">\(<span id="fornecedores-total-mobile">\d+</span>\)', forn)
    assert re.search(r'class="hidden lg:(block|flex) bg-gradient-to-r[^"]*" id="gemini-(fallback|active)-banner"', forn)
    disputa = client.get("/disputa", cookies=h).text
    assert '<div class="hidden lg:block">' in disputa and "Cockpit Multi-Pregão" in disputa
    alertas = client.get("/alerts", cookies=h).text
    assert "overflow-x-auto scrollbar-none" in alertas and alertas.count("shrink-0 whitespace-nowrap px-4 py-2.5") == 6
    painel = client.get("/dashboard", cookies=h).text
    assert painel.count('<p class="hidden lg:block text-') == 4


@pytest.fixture
def cache_ligado(monkeypatch):
    monkeypatch.setenv("METRICAS_CACHE_TTL", "900")
    cache_metricas.invalidar()
    yield
    cache_metricas.invalidar()


def _novo_edital(db, orgao):
    t = Tender(source_id="pncp-source-01", external_id=f"CACHE-{uuid.uuid4().hex[:8]}", title="Edital cache",
               object_description="x", contracting_entity=orgao, state="AL", status="Divulgada no PNCP",
               source_url="https://pncp.gov.br/app/editais", publication_date=agora_sao_paulo_naive())
    db.add(t)
    db.commit()
    return t.id


def test_metricas_ficam_em_cache_ate_a_proxima_coleta(cache_ligado):
    orgao = f"Órgão Cache {uuid.uuid4().hex[:6]}"
    db = SessionLocal()
    criados = []
    try:
        antes_forn = get_fornecedores_data(db)
        antes_painel = get_dashboard_summary_data(db, None)
        criados.append(_novo_edital(db, orgao))

        # Dentro do TTL: mesmas contagens, sem varrer o catálogo de novo
        assert get_fornecedores_data(db)["total_tenders"] == antes_forn["total_tenders"]
        assert get_dashboard_summary_data(db, None)["total_tenders_count"] == antes_painel["total_tenders_count"]
        assert orgao not in {c["name"] for c in get_fornecedores_data(db)["companies"]}

        # A coleta invalida o cache: o edital novo entra nas métricas
        cache_metricas.invalidar()
        depois = get_fornecedores_data(db)
        assert depois["total_tenders"] == antes_forn["total_tenders"] + 1
        assert orgao in {c["name"] for c in depois["companies"]}
        assert get_dashboard_summary_data(db, None)["total_tenders_count"] == antes_painel["total_tenders_count"] + 1
    finally:
        db.query(Tender).filter(Tender.id.in_(criados)).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_cache_expira_pelo_ttl_e_ttl_zero_desliga(monkeypatch):
    chamadas = []
    calcular = lambda: chamadas.append(1) or len(chamadas)
    cache_metricas.invalidar("teste:")
    assert cache_metricas.obter("teste:x", calcular, ttl=900) == 1
    assert cache_metricas.obter("teste:x", calcular, ttl=900) == 1
    relogio = [cache_metricas.time.monotonic() + 901]
    monkeypatch.setattr(cache_metricas.time, "monotonic", lambda: relogio[0])
    assert cache_metricas.obter("teste:x", calcular, ttl=900) == 2
    assert cache_metricas.obter("teste:y", calcular, ttl=0) == 3 and cache_metricas.obter("teste:y", calcular, ttl=0) == 4
    cache_metricas.invalidar("teste:")


def test_coleta_e_rotina_invalidam_o_cache():
    import inspect

    from server.integrations import opportunity_collector
    from server.services import auto_sync
    assert "cache_metricas.invalidar()" in inspect.getsource(opportunity_collector.sync_procurement_opportunities)
    assert "cache_metricas.invalidar()" in inspect.getsource(auto_sync._executar)


def test_prefetch_de_navegacao_e_preconnect():
    html = client.get("/").text
    assert '<link rel="preconnect" href="https://cdn.tailwindcss.com">' in html
    assert "dica.rel = 'prefetch'" in html and "fornecedores" in html[html.index("const ROTAS"):html.index("const feitos")]
    assert "saveData" in html


def test_fornecedores_abre_com_os_maiores_orgaos_e_busca_no_servidor(usuario_auth):
    from server.main import ORGAOS_POR_PAGINA
    orgao = f"Prefeitura Busca {uuid.uuid4().hex[:6]}"
    db = SessionLocal()
    tid = _novo_edital(db, orgao)
    db.close()
    try:
        html = client.get("/fornecedores", cookies=usuario_auth.cookies).text
        assert html.count('company-row"') <= ORGAOS_POR_PAGINA and 'oninput="filterCompaniesTable()"' in html
        assert "fetch('/fornecedores/linhas?q='" in html

        achou = client.get("/fornecedores/linhas", params={"q": orgao.upper()}, cookies=usuario_auth.cookies)
        assert achou.status_code == 200 and achou.text.count('company-row"') == 1 and orgao in achou.text
        vazio = client.get("/fornecedores/linhas", params={"q": "zzz-nenhum-orgao"}, cookies=usuario_auth.cookies)
        assert "Nenhum órgão encontrado" in vazio.text
    finally:
        db = SessionLocal()
        db.query(Tender).filter(Tender.id == tid).delete(synchronize_session=False)
        db.commit()
        db.close()
