"""
Testes das 4 melhorias prioritárias:
  1. Autenticação: mostrar senha, manter conectado e recuperação de senha
  2. Filtros: categoria estritamente MATERIAL / SERVICO / OUTROS e as 27 UFs
  3. Itens individuais do certame via endpoint /itens do PNCP
  4. Listagem e download interno dos anexos (/arquivos) sem redirecionamento externo
"""
import io
import re
import uuid
import zipfile
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from server import security
from server.config import settings
from server.database import SessionLocal
from server.main import app
from server.migrate import run_db_migrations
from server.models import PasswordResetToken, Source, Tender, TenderDocument, TenderItem, User
from server.search_engine import NATUREZAS_FILTRO, UFS_BRASIL
from server.security import decode_access_token, hash_password, hash_reset_token
from server.services import email_service, pncp_compras

client = TestClient(app)

CNPJ = "12265468000197"
PDF_FALSO = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"

# Amostra real do endpoint /itens do PNCP (compra 12265468000197/2026/36)
ITEM_PNCP_REAL = {
    "numeroItem": 1,
    "descricao": "Contratação de empresa para fornecimento de sistema de recorte digital de publicação e intimações",
    "materialOuServico": "M", "materialOuServicoNome": "Material",
    "valorUnitarioEstimado": 3415.2000, "valorTotal": 3415.2000, "quantidade": 1.0000, "unidadeMedida": "UN",
    "orcamentoSigiloso": False, "itemCategoriaId": 3, "itemCategoriaNome": "Não se aplica",
    "criterioJulgamentoId": 1, "criterioJulgamentoNome": "Menor preço",
    "situacaoCompraItem": 1, "situacaoCompraItemNome": "Em andamento",
    "tipoBeneficio": 4, "tipoBeneficioNome": "Sem benefício",
    "dataInclusao": "2026-10-06T20:19:00", "temResultado": False,
    "ncmNbsCodigo": None, "catalogoCodigoItem": None, "informacaoComplementar": None,
}


@pytest.fixture(autouse=True)
def limpar_limites():
    security._rate_limit_records.clear()
    pncp_compras._falhas_recentes.clear()
    yield
    security._rate_limit_records.clear()
    pncp_compras._falhas_recentes.clear()
    pncp_compras._transport_override = None


# ==============================================================================
# 1. AUTENTICAÇÃO
# ==============================================================================

@pytest.fixture
def usuario_temporario():
    email = f"teste.{uuid.uuid4().hex[:10]}@exemplo.com.br"
    senha = "SenhaInicial@123"
    db = SessionLocal()
    try:
        user = User(name="Maria Teste", email=email, password_hash=hash_password(senha), role="USER", status="ACTIVE")
        db.add(user)
        db.commit()
        user_id = user.id
    finally:
        db.close()
    yield {"id": user_id, "email": email, "senha": senha}
    db = SessionLocal()
    try:
        db.query(PasswordResetToken).filter(PasswordResetToken.user_id == user_id).delete()
        db.query(User).filter(User.id == user_id).delete()
        db.commit()
    finally:
        db.close()


def test_login_sessao_padrao_e_manter_conectado(usuario_temporario):
    curto = client.post("/api/auth/login", json={"email": usuario_temporario["email"], "password": usuario_temporario["senha"]})
    assert curto.status_code == 200
    dados = curto.json()
    assert dados["remember_me"] is False
    assert dados["expires_in"] == settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    payload = decode_access_token(dados["access_token"])
    assert payload["pwv"] and payload["rmb"] is False

    longo = client.post("/api/auth/login", json={
        "email": usuario_temporario["email"], "password": usuario_temporario["senha"], "remember_me": True
    }).json()
    assert longo["remember_me"] is True
    assert longo["expires_in"] == settings.REMEMBER_ME_DAYS * 86400
    p = decode_access_token(longo["access_token"])
    assert p["exp"] - p["iat"] >= settings.REMEMBER_ME_DAYS * 86400 - 5


def test_login_bloqueia_apos_10_falhas_e_nao_conta_sucessos(usuario_temporario):
    for _ in range(12):
        assert client.post("/api/auth/login", json={"email": usuario_temporario["email"], "password": usuario_temporario["senha"]}).status_code == 200
    for _ in range(10):
        r = client.post("/api/auth/login", json={"email": usuario_temporario["email"], "password": "errada-123"})
        assert r.status_code == 401
    bloqueado = client.post("/api/auth/login", json={"email": usuario_temporario["email"], "password": usuario_temporario["senha"]})
    assert bloqueado.status_code == 429


def test_paginas_de_login_com_olho_manter_conectado_e_recuperacao():
    login = client.get("/login").text
    assert 'data-toggle-password="password-input"' in login
    assert 'id="remember-input"' in login and "Manter conectado" in login
    assert 'href="/recuperar-senha"' in login
    assert 'autocomplete="current-password"' in login

    registro = client.get("/register").text
    assert 'data-toggle-password="password-input"' in registro
    assert 'autocomplete="new-password"' in registro

    recuperar = client.get("/recuperar-senha")
    assert recuperar.status_code == 200 and "/api/auth/forgot-password" in recuperar.text
    assert client.get("/esqueci-senha").status_code == 200


def test_fluxo_completo_de_recuperacao_de_senha(usuario_temporario):
    email_service.caixa_mock.mensagens.clear()
    sessao_antiga = client.post("/api/auth/login", json={
        "email": usuario_temporario["email"], "password": usuario_temporario["senha"]
    }).json()["access_token"]

    resp = client.post("/api/auth/forgot-password", json={"email": usuario_temporario["email"]})
    assert resp.status_code == 200
    assert "reset_url" not in resp.json()  # nunca exposto por padrão

    mensagem = email_service.caixa_mock.mensagens[-1]
    assert mensagem["to"] == usuario_temporario["email"]
    link = re.search(r"https?://\S+/redefinir-senha\?token=([\w-]+)", mensagem["text"])
    assert link, "o e-mail deve conter o link de redefinição"
    token = link.group(1)

    db = SessionLocal()
    try:
        registro = db.query(PasswordResetToken).filter(PasswordResetToken.user_id == usuario_temporario["id"]).first()
        assert registro.token_hash == hash_reset_token(token)
        assert token not in registro.token_hash  # token em claro nunca é persistido
    finally:
        db.close()

    assert client.get(f"/api/auth/reset-password/validate?token={token}").json()["valid"] is True
    pagina = client.get(f"/redefinir-senha?token={token}")
    assert 'id="form-redefinir"' in pagina.text
    assert pagina.headers["referrer-policy"] == "no-referrer"

    mesma = client.post("/api/auth/reset-password", json={"token": token, "new_password": usuario_temporario["senha"]})
    assert mesma.status_code == 422

    ok = client.post("/api/auth/reset-password", json={"token": token, "new_password": "NovaSenha#2026"})
    assert ok.status_code == 200

    assert client.post("/api/auth/reset-password", json={"token": token, "new_password": "OutraSenha#2026"}).status_code == 400
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {sessao_antiga}"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": usuario_temporario["email"], "password": usuario_temporario["senha"]}).status_code == 401
    assert client.post("/api/auth/login", json={"email": usuario_temporario["email"], "password": "NovaSenha#2026"}).status_code == 200


def test_recuperacao_resposta_uniforme_e_token_expirado(usuario_temporario, monkeypatch):
    inexistente = client.post("/api/auth/forgot-password", json={"email": "ninguem.cadastrado@exemplo.com"})
    existente = client.post("/api/auth/forgot-password", json={"email": usuario_temporario["email"]})
    assert inexistente.status_code == existente.status_code == 200
    assert inexistente.json()["message"] == existente.json()["message"]

    monkeypatch.setattr(settings, "AUTH_EXPOSE_RESET_LINK", True)
    security._rate_limit_records.clear()
    dev = client.post("/api/auth/forgot-password", json={"email": usuario_temporario["email"]}).json()
    token = dev["reset_url"].split("token=")[1]

    db = SessionLocal()
    try:
        ativos = db.query(PasswordResetToken).filter(
            PasswordResetToken.user_id == usuario_temporario["id"], PasswordResetToken.used_at.is_(None)
        ).all()
        assert len(ativos) == 1, "um novo pedido invalida os links anteriores"
        ativos[0].expires_at = datetime.utcnow() - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()

    assert client.post("/api/auth/reset-password", json={"token": token, "new_password": "Qualquer#2026"}).status_code == 400
    assert 'id="token-invalido"' in client.get(f"/redefinir-senha?token={token}").text


def test_recuperacao_limita_pedidos_por_ip():
    codigos = [client.post("/api/auth/forgot-password", json={"email": f"x{i}@exemplo.com"}).status_code for i in range(6)]
    assert codigos[:5] == [200] * 5
    assert codigos[5] == 429


# ==============================================================================
# 2. FILTROS: MATERIAL / SERVIÇO / OUTROS E AS 27 UFs
# ==============================================================================

def test_lista_oficial_tem_27_ufs_unicas():
    siglas = [s for s, _ in UFS_BRASIL]
    assert len(siglas) == 27 and len(set(siglas)) == 27
    assert {"DF", "TO", "AP", "RR", "AC"} <= set(siglas)
    assert [c for c, _ in NATUREZAS_FILTRO] == ["MATERIAL", "SERVICO", "OUTROS"]


@pytest.mark.parametrize("rota", ["/", "/search", "/alerts"])
def test_paginas_exibem_as_27_ufs(rota):
    html = client.get(rota).text
    siglas = set(re.findall(r'<option value="([A-Z]{2})"', html))
    assert {s for s, _ in UFS_BRASIL} <= siglas


def test_select_de_categoria_tem_somente_tres_opcoes():
    html = client.get("/search").text
    bloco = re.search(r'<select name="category" id="categoria-select".*?</select>', html, re.S).group(0)
    valores = re.findall(r'<option value="([^"]*)"', bloco)
    assert valores == ["", "MATERIAL", "SERVICO", "OUTROS"]

    home = client.get("/").text
    bloco_home = re.search(r'<select name="category" id="hero-categoria-select".*?</select>', home, re.S).group(0)
    assert re.findall(r'<option value="([^"]*)"', bloco_home) == ["", "MATERIAL", "SERVICO", "OUTROS"]


@pytest.mark.parametrize("natureza", ["MATERIAL", "SERVICO", "OUTROS"])
def test_api_filtra_por_natureza_e_uf(natureza):
    dados = client.get(f"/api/tenders?category={natureza.lower()}&uf=sp&page_size=100").json()
    assert dados["total"] >= 0
    for item in dados["results"]:
        assert item["natureza_objeto"] == natureza
        assert item["state"] == "SP"
    alias = client.get(f"/api/tenders?natureza={natureza}&page_size=5").json()
    assert all(r["natureza_objeto"] == natureza for r in alias["results"])


def test_pagina_de_busca_filtra_e_mantem_filtros_na_paginacao():
    html = client.get("/search?uf=AL&category=MATERIAL&page_size=1").text
    naturezas = set(re.findall(r'class="natureza-badge[^"]*"[^>]*data-natureza="(\w+)"', html.replace("\n", " ")))
    assert naturezas <= {"MATERIAL"}
    assert re.search(r'<option value="AL" selected>', html)
    proxima = re.search(r'href="\?([^"]*)&page=2"', html)
    if proxima:
        assert "uf=AL" in proxima.group(1) and "category=MATERIAL" in proxima.group(1)


def test_categoria_legada_continua_funcionando():
    dados = client.get("/api/tenders?category=Medicamentos&state=AL").json()
    assert dados["total"] >= 1
    assert all("Medicamentos" in r["category"] for r in dados["results"])


def _source_id(db):
    src = db.query(Source).first()
    assert src is not None
    return src.id


def test_natureza_classificada_automaticamente_ao_gravar_e_na_migracao():
    db = SessionLocal()
    try:
        t = Tender(
            source_id=_source_id(db), external_id=f"TESTE-NAT-{uuid.uuid4().hex[:8]}", title="Aquisição de notebooks",
            object_description="Compra de notebooks", contracting_entity="Órgão Teste", state="DF",
            source_url="https://pncp.gov.br/app/editais",
        )
        db.add(t)
        db.commit()
        assert t.natureza_objeto == "MATERIAL"

        t.title = "Prestação de serviços de vigilância patrimonial"
        t.object_description = "Serviços contínuos de vigilância"
        db.commit()
        assert t.natureza_objeto == "SERVICO"

        db.execute(Tender.__table__.update().where(Tender.id == t.id).values(natureza_objeto=None))
        db.commit()
        run_db_migrations()
        db.expire_all()
        assert db.query(Tender).get(t.id).natureza_objeto == "SERVICO"
    finally:
        db.query(Tender).filter(Tender.external_id.like("TESTE-NAT-%")).delete(synchronize_session=False)
        db.commit()
        db.close()


# ==============================================================================
# 3 e 4. ITENS (/itens) E ARQUIVOS (/arquivos) DO PNCP
# ==============================================================================

@pytest.fixture
def certame_pncp(tmp_path, monkeypatch):
    """Certame isolado com identificador PNCP real e cache de downloads em diretório temporário."""
    monkeypatch.setattr(settings, "STORAGE_LOCAL_PATH", str(tmp_path / "storage"))
    sequencial = str(900000 + uuid.uuid4().int % 99999)
    db = SessionLocal()
    try:
        t = Tender(
            source_id=_source_id(db), external_id=f"{CNPJ}-1-{sequencial.zfill(6)}/2026",
            title="Aquisição de sistema de recorte digital", object_description="Teste PNCP",
            contracting_entity="Município de Teste", state="AL", source_url="https://pncp.gov.br/app/editais",
            notice_number=f"DISP {sequencial}/2026",
        )
        db.add(t)
        db.commit()
        db.add(TenderItem(tender_id=t.id, item_number=999, description="Item local obsoleto", origem="LOCAL"))
        db.commit()
        dados = {"id": t.id, "sequencial": sequencial}
    finally:
        db.close()
    yield dados
    db = SessionLocal()
    try:
        db.query(Tender).filter(Tender.id == dados["id"]).delete()
        db.commit()
    finally:
        db.close()


def _instalar_pncp_falso(sequencial, *, total_itens=150, redirecionar_para="https://pncp.gov.br/pncp-api/v1/storage/edital.pdf",
                         status_itens=200, corpo_arquivo=PDF_FALSO):
    chamadas = []
    base_arquivo = f"https://pncp.gov.br/pncp-api/v1/orgaos/{CNPJ}/compras/2026/{sequencial}/arquivos"
    itens = [dict(ITEM_PNCP_REAL, numeroItem=i, descricao=f"Item {i} - {ITEM_PNCP_REAL['descricao']}",
                  tipoBeneficioNome=("Participação exclusiva para ME/EPP" if i % 2 else "Sem benefício"))
             for i in range(1, total_itens + 1)]

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append(str(request.url))
        caminho = request.url.path
        if caminho.endswith(f"/compras/2026/{sequencial}/itens"):
            if status_itens != 200:
                return httpx.Response(status_itens)
            pagina = int(request.url.params["pagina"])
            tamanho = int(request.url.params["tamanhoPagina"])
            return httpx.Response(200, json=itens[(pagina - 1) * tamanho: pagina * tamanho])
        if caminho.endswith(f"/compras/2026/{sequencial}/arquivos"):
            return httpx.Response(200, json=[
                {"url": f"{base_arquivo}/1", "uri": f"{base_arquivo}/1", "statusAtivo": True, "sequencialDocumento": 1,
                 "titulo": "EDITAL_DE_DISPENSA__SISTEMA", "tipoDocumentoNome": "Aviso de Contratação Direta",
                 "dataPublicacaoPncp": "2026-10-06T20:19:00"},
                {"url": f"{base_arquivo}/2", "statusAtivo": True, "sequencialDocumento": 2,
                 "titulo": "Termo de Referência", "tipoDocumentoNome": "Termo de Referência"},
                {"url": "https://servidor-externo.exemplo/arquivo.pdf", "statusAtivo": True, "sequencialDocumento": 3, "titulo": "Fora do PNCP"},
                {"url": f"{base_arquivo}/4", "statusAtivo": False, "sequencialDocumento": 4, "titulo": "Removido"},
            ])
        if caminho.endswith("/arquivos/1"):
            return httpx.Response(302, headers={"location": redirecionar_para})
        if caminho.endswith("/storage/edital.pdf"):
            return httpx.Response(200, content=corpo_arquivo, headers={
                "content-type": "application/octet-stream",
                "content-disposition": 'attachment; filename="EDITAL.pdf"'})
        if caminho.endswith("/arquivos/2"):
            return httpx.Response(200, content=b"PK\x03\x04conteudo-zip", headers={"content-type": "application/zip"})
        return httpx.Response(404)

    pncp_compras._transport_override = httpx.MockTransport(handler)
    return chamadas


def test_identificar_compra_e_parse_de_item_real():
    t = Tender(external_id=f"{CNPJ}-1-000036/2026", source_url="x")
    ident = pncp_compras.identificar_compra(t)
    assert (ident.cnpj, ident.ano, ident.sequencial) == (CNPJ, "2026", "36")
    assert ident.caminho == f"orgaos/{CNPJ}/compras/2026/36"

    item = pncp_compras.parse_item_pncp(ITEM_PNCP_REAL)
    assert item["item_number"] == 1
    assert item["quantity"] == 1.0 and item["estimated_unit_value"] == 3415.2
    assert item["unit"] == "UN" and item["material_ou_servico"] == "M"
    assert item["criterio_julgamento"] == "Menor preço" and item["situacao"] == "Em andamento"
    assert pncp_compras.parse_item_pncp({"descricao": "sem número"}) is None
    assert pncp_compras.identificar_compra(Tender(external_id="sem-identificador", source_url="x")) is None


def test_sincroniza_itens_com_paginacao_e_cache(certame_pncp):
    chamadas = _instalar_pncp_falso(certame_pncp["sequencial"], total_itens=150)
    resp = client.get(f"/api/tenders/{certame_pncp['id']}/itens")
    assert resp.status_code == 200
    dados = resp.json()
    assert dados["status"] == "sincronizado" and dados["origem"] == "PNCP"
    assert dados["total"] == 150
    assert [i["item_number"] for i in dados["itens"]][:3] == [1, 2, 3]
    assert all(i["origem"] == "PNCP" for i in dados["itens"])
    assert 999 not in [i["item_number"] for i in dados["itens"]], "item local obsoleto deve ser removido"
    assert dados["valor_total_estimado"] == pytest.approx(150 * 3415.2)
    assert sum("/itens" in c for c in chamadas) == 2  # 100 + 50

    antes = len(chamadas)
    assert client.get(f"/api/tenders/{certame_pncp['id']}/itens").json()["status"] == "cache"
    assert len(chamadas) == antes
    assert client.get(f"/api/tenders/{certame_pncp['id']}/itens?atualizar=true").json()["status"] == "sincronizado"

    pagina = client.get(f"/tenders/{certame_pncp['id']}").text
    assert "Participação exclusiva para ME/EPP" in pagina
    assert "Atualizar do PNCP" in pagina


def test_itens_compra_inexistente_ou_pncp_fora_do_ar(certame_pncp):
    _instalar_pncp_falso(certame_pncp["sequencial"], status_itens=404)
    dados = client.get(f"/api/tenders/{certame_pncp['id']}/itens").json()
    assert dados["status"] == "nao_encontrado"
    assert [i["item_number"] for i in dados["itens"]] == [999]  # base local preservada

    pncp_compras._falhas_recentes.clear()

    def fora_do_ar(request):
        raise httpx.ConnectTimeout("timeout", request=request)

    pncp_compras._transport_override = httpx.MockTransport(fora_do_ar)
    dados = client.get(f"/api/tenders/{certame_pncp['id']}/itens").json()
    assert dados["status"] == "indisponivel" and "PNCP" in dados["mensagem"]
    # cache negativo: segunda chamada não consulta o PNCP de novo
    pncp_compras._transport_override = httpx.MockTransport(lambda r: pytest.fail("não deveria consultar o PNCP"))
    assert client.get(f"/api/tenders/{certame_pncp['id']}/itens").json()["status"] == "indisponivel"


def test_lista_arquivos_somente_do_pncp_e_ativos(certame_pncp):
    _instalar_pncp_falso(certame_pncp["sequencial"])
    dados = client.get(f"/api/tenders/{certame_pncp['id']}/arquivos").json()
    assert dados["status"] == "sincronizado"
    nomes = [a["name"] for a in dados["arquivos"]]
    assert nomes == ["EDITAL DE DISPENSA SISTEMA", "Termo de Referência"]
    for arq in dados["arquivos"]:
        assert arq["baixavel"] is True
        assert arq["download_url"].startswith(f"/api/tenders/{certame_pncp['id']}/arquivos/")
        assert "pncp.gov.br" not in arq["download_url"]
    assert dados["zip_url"] == f"/api/tenders/{certame_pncp['id']}/arquivos/zip"


def test_download_interno_com_cache_inline_e_zip(certame_pncp):
    chamadas = _instalar_pncp_falso(certame_pncp["sequencial"])
    arquivos = client.get(f"/api/tenders/{certame_pncp['id']}/arquivos").json()["arquivos"]
    edital = arquivos[0]

    resp = client.get(edital["download_url"])
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"].startswith("attachment")
    assert ".pdf" in resp.headers["content-disposition"]
    assert resp.content == PDF_FALSO
    assert len(resp.headers["x-checksum-sha256"]) == 64

    antes = len(chamadas)
    inline = client.get(edital["visualizar_url"])
    assert inline.headers["content-disposition"].startswith("inline")
    assert len(chamadas) == antes, "segundo acesso deve vir do cache local"

    listagem = client.get(f"/api/tenders/{certame_pncp['id']}/arquivos").json()["arquivos"]
    assert listagem[0]["mime_type"] == "application/pdf" and listagem[0]["file_size"] == len(PDF_FALSO)

    pacote = client.get(f"/api/tenders/{certame_pncp['id']}/arquivos/zip")
    assert pacote.status_code == 200 and pacote.headers["content-type"] == "application/zip"
    nomes = zipfile.ZipFile(io.BytesIO(pacote.content)).namelist()
    assert any(n.endswith(".pdf") for n in nomes) and any(n.endswith(".zip") for n in nomes)


def test_download_bloqueia_redirecionamento_externo_e_arquivo_grande(certame_pncp, monkeypatch):
    _instalar_pncp_falso(certame_pncp["sequencial"], redirecionar_para="https://servidor-malicioso.exemplo/edital.pdf")
    edital = client.get(f"/api/tenders/{certame_pncp['id']}/arquivos").json()["arquivos"][0]
    bloqueado = client.get(edital["download_url"])
    assert bloqueado.status_code == 404
    assert "não autorizado" in bloqueado.json()["detail"]

    monkeypatch.setattr(settings, "PNCP_DOWNLOAD_MAX_MB", 0)
    _instalar_pncp_falso(certame_pncp["sequencial"], corpo_arquivo=PDF_FALSO * 10)
    grande = client.get(edital["download_url"])
    assert grande.status_code == 413


def test_documento_de_outro_certame_ou_sem_arquivo_nao_e_baixado(certame_pncp):
    db = SessionLocal()
    try:
        doc = TenderDocument(tender_id=certame_pncp["id"], name="Página do edital", source_url="https://pncp.gov.br/app/editais/x")
        db.add(doc)
        db.commit()
        doc_id = doc.id
    finally:
        db.close()
    assert client.get(f"/api/tenders/{certame_pncp['id']}/arquivos/{doc_id}/download").status_code == 404
    assert client.get(f"/api/tenders/tnd-sp-ti-002/arquivos/{doc_id}/download").status_code == 404


def test_detalhe_do_certame_nao_redireciona_para_portal_externo(certame_pncp):
    _instalar_pncp_falso(certame_pncp["sequencial"])
    client.get(f"/api/tenders/{certame_pncp['id']}/arquivos")
    html = client.get(f"/tenders/{certame_pncp['id']}").text
    assert "Baixar Oficial" not in html
    assert f"/api/tenders/{certame_pncp['id']}/arquivos/" in html
    assert "baixarArquivoInterno" in html
    aba = html.split('id="tab-content-documents"', 1)[1].split('id="tab-content-ai"', 1)[0]
    assert 'target="_blank" rel="noopener noreferrer"' not in aba
