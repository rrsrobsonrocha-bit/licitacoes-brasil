"""Padrão mobile das páginas secundárias (cabeçalho compacto com seta de voltar) e gestão de clientes só em /gestao."""
import re
import shutil
import subprocess
import uuid

import pytest
from fastapi.testclient import TestClient

from server.database import SessionLocal
from server.main import app
from server.models import Tender
from server.search_engine import agora_sao_paulo_naive

client = TestClient(app)

PAGINAS = {
    "/dashboard": "Painel Executivo",
    "/documentos": "Cofre de Documentos",
    "/juridico": "Peças Jurídicas",
    "/favorites": "Favoritos",
    "/agenda": "Agenda",
    "/gestao": "Gestão",
    "/fornecedores": "Fornecedores",
    "/disputa": "Disputa ao Vivo",
    "/alerts": "Alertas",
}


@pytest.fixture
def edital():
    db = SessionLocal()
    t = Tender(source_id="pncp-source-01", external_id=f"MOBILE-{uuid.uuid4().hex[:8]}", title="Edital mobile",
               object_description="x", contracting_entity="Órgão", state="AL", status="Divulgada no PNCP",
               source_url="https://pncp.gov.br/app/editais", publication_date=agora_sao_paulo_naive())
    db.add(t)
    db.commit()
    tid = t.id
    db.close()
    yield tid
    db = SessionLocal()
    db.query(Tender).filter(Tender.id == tid).delete(synchronize_session=False)
    db.commit()
    db.close()


def _linha_voltar(html):
    trecho = html[html.index('class="cabecalho-mobile-linha'):]
    return trecho[:trecho.index("</h1>") + 5]


def _js_valido(html, rotulo):
    if not shutil.which("node"):
        return
    for i, bloco in enumerate(re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.S)):
        r = subprocess.run(["node", "--check", "-"], input=bloco, capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, f"{rotulo} script {i}: {r.stderr[:300]}"


def test_paginas_secundarias_tem_seta_de_voltar_e_titulo_compacto(admin_auth, edital):
    paginas = dict(PAGINAS)
    paginas[f"/tenders/{edital}"] = "Detalhes do Edital"
    for rota, titulo in paginas.items():
        resp = client.get(rota, cookies=admin_auth.cookies)
        assert resp.status_code == 200, rota
        html = resp.text
        assert html.count('class="cabecalho-mobile-linha lg:hidden') == 1, rota
        linha = _linha_voltar(html)
        assert 'onclick="voltarPagina()"' in linha and "fa-arrow-left" in linha and 'aria-label="Voltar"' in linha
        assert "btn-voltar h-9 w-9 shrink-0 inline-flex items-center justify-center" in linha and "active:scale-90" in linha
        assert f">{titulo}" in linha, rota
        # Cabeçalho fixo e compacto no celular (o global tem 4 rem; a barra da página fica logo abaixo)
        assert re.search(r'class="[^"]*sticky top-16 z-20[^"]*py-2\.5[^"]*"|class="cabecalho-mobile lg:hidden sticky top-16 z-20 bg-slate-900/95 backdrop-blur border-b border-slate-800 py-2\.5 px-3 flex items-center gap-2"', html), rota
        _js_valido(html, rota)

    base = client.get("/dashboard", cookies=admin_auth.cookies).text
    assert 'window.history.length > 1 ? window.history.back() : window.location.href = "/dashboard"' in base
    assert ".scrollbar-none::-webkit-scrollbar { display: none; }" in base


def test_contagens_na_linha_do_titulo_e_pilulas_rolaveis(usuario_auth):
    h = usuario_auth.cookies
    cofre = client.get("/documentos", cookies=h).text
    assert re.search(r'>Cofre de Documentos <span class="font-mono text-sm text-emerald-400">\(<span id="resumo-total-mobile">\d+</span>\)', cofre)
    pilulas = cofre[cofre.index('aria-live="polite">') - 200:cofre.index('aria-live="polite">')]
    assert "flex items-center overflow-x-auto space-x-2 py-1 scrollbar-none" in pilulas
    assert cofre.count("status-pill shrink-0 whitespace-nowrap") == 5
    assert "<div class=\"hidden lg:flex items-center gap-3\">" in cofre  # ícone grande e subtítulo da lei só no desktop

    juridico = client.get("/juridico", cookies=h).text
    assert re.search(r'>Peças Jurídicas <span class="font-mono text-sm text-amber-400">\(<span id="templates-total-count-mobile">\d+</span>\)', juridico)
    assert 'flex items-center overflow-x-auto space-x-2 py-1 scrollbar-none text-xs' in juridico
    assert "filter-cat-btn shrink-0 whitespace-nowrap px-3" in juridico
    assert 'hidden lg:flex flex-row items-center justify-between gap-4' in juridico

    agenda = client.get("/agenda", cookies=h).text
    assert 'id="agenda-total-count-mobile"' in agenda
    favoritos = client.get("/favorites", cookies=h).text
    assert 'id="favoritos-total"' in favoritos and 'class="hidden lg:block bg-slate-900' in favoritos


def test_gestao_de_clientes_somente_em_gestao(admin_auth, usuario_auth):
    painel = client.get("/dashboard", cookies=admin_auth.cookies).text
    assert "Gestão de Clientes" not in painel and 'id="admin-assinaturas"' not in painel
    assert "/api/admin/users" not in painel
    # O painel segue com atalhos, funil e editais vigentes
    assert 'href="/negocios?view=quadros"' in painel and 'href="/search' in painel and "Editais vigentes" in painel

    gestao = client.get("/gestao", cookies=admin_auth.cookies).text
    assert 'id="admin-assinaturas"' in gestao and "/api/admin/users" in gestao and "carregarClientes()" in gestao
    assert client.get("/gestao", cookies=usuario_auth.cookies, follow_redirects=False).headers["location"] == "/dashboard"
