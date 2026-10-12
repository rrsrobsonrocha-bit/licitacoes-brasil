"""Multi-CNPJ (empresa ativa), white-label dos documentos gerados e simulador de índices contábeis."""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from server.database import SessionLocal
from server.main import app
from server.models import Deal, DocumentoEmpresa, EmpresaUsuario, Tender, hoje_sao_paulo
from server.services import document_ai, dossie, indices_contabeis

client = TestClient(app)
CNPJ_A = "11222333000181"
CNPJ_B = "11444777000161"


def _receita(cnpj, razao, cidade, socio):
    return {
        "cnpj": cnpj, "razao_social": razao, "nome_fantasia": "", "descricao_situacao_cadastral": "ATIVA",
        "data_inicio_atividade": "2015-03-10", "capital_social": 100000, "porte": "MICRO EMPRESA",
        "descricao_tipo_de_logradouro": "RUA", "logradouro": "DAS FLORES", "numero": "100", "complemento": "SALA 2",
        "bairro": "CENTRO", "municipio": cidade, "uf": "AL", "cep": "57020000", "ddd_telefone_1": "8233334444",
        "email": "contato@exemplo.com.br", "cnae_fiscal": 4645101, "cnae_fiscal_descricao": "Comércio atacadista de produtos médicos",
        "qsa": [{"nome_socio": "JOÃO SÓCIO", "qualificacao_socio": "Sócio"},
                {"nome_socio": socio, "qualificacao_socio": "Sócio-Administrador"}],
    }


RECEITA = {
    CNPJ_A: _receita(CNPJ_A, "ALFA DISTRIBUIDORA HOSPITALAR LTDA", "MACEIO", "MARIA ADMINISTRADORA"),
    CNPJ_B: _receita(CNPJ_B, "BETA SERVICOS DE TI LTDA", "ARAPIRACA", "PEDRO DIRETOR"),
}


@pytest.fixture(autouse=True)
def receita_simulada(monkeypatch):
    monkeypatch.setattr(dossie, "requisitar_json", lambda url, params=None, headers=None: RECEITA.get(url.rsplit("/", 1)[-1]))


@pytest.fixture
def contas(criar_usuario):
    a, b = criar_usuario("Conta A"), criar_usuario("Conta B")
    yield a, b
    db = SessionLocal()
    for modelo in (EmpresaUsuario, DocumentoEmpresa):
        db.query(modelo).filter(modelo.user_id.in_([a.id, b.id])).delete(synchronize_session=False)
    db.query(Deal).filter(Deal.user_id.in_([a.id, b.id])).delete(synchronize_session=False)
    db.commit()
    db.close()


def _cadastrar(conta, cnpj):
    resp = client.post("/api/minhas-empresas", headers=conta.headers, json={"cnpj": cnpj})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _docx(conteudo):
    zf = zipfile.ZipFile(io.BytesIO(conteudo))
    return {nome: zf.read(nome).decode("utf-8") for nome in zf.namelist()}


# ------------------------------------------------------------------------------
# 1. Cadastro e empresa ativa
# ------------------------------------------------------------------------------
def test_uma_empresa_e_ativada_automaticamente(contas):
    a, _ = contas
    dados = _cadastrar(a, "11.222.333/0001-81")
    empresa = dados["empresa"]
    assert empresa["is_primary"] is True and dados["ativa"]["cnpj"] == CNPJ_A
    assert empresa["razao_social"] == "ALFA DISTRIBUIDORA HOSPITALAR LTDA"
    assert empresa["endereco_completo"] == "RUA DAS FLORES, 100, SALA 2, CENTRO – MACEIO/AL – CEP 57020-000"
    assert empresa["telefone"] == "(82) 3333-4444" and empresa["email"] == "contato@exemplo.com.br"
    assert empresa["representante_nome"] == "MARIA ADMINISTRADORA"  # administrador do QSA, não o 1º sócio
    assert empresa["cnae_principal"].startswith("4645101")

    html = client.get("/dashboard", headers=a.headers).text
    assert 'id="empresa-ativa-chip"' in html and 'id="seletor-empresa"' not in html  # vai direto, sem perguntar
    assert client.post("/api/minhas-empresas", headers=a.headers, json={"cnpj": "123"}).status_code == 422


def test_varias_empresas_mostram_seletor_e_alternam(contas):
    a, _ = contas
    primeira = _cadastrar(a, CNPJ_A)["empresa"]
    segunda = _cadastrar(a, CNPJ_B)
    assert segunda["ativa"]["cnpj"] == CNPJ_A and len(segunda["lista"]) == 2  # a primeira continua ativa

    html = client.get("/dashboard", headers=a.headers).text
    assert 'id="seletor-empresa"' in html and "BETA SERVICOS DE TI LTDA – 11.444.777/0001-61" in html

    id_b = next(e["id"] for e in segunda["lista"] if e["cnpj"] == CNPJ_B)
    ativa = client.post(f"/api/minhas-empresas/{id_b}/ativar", headers=a.headers).json()
    assert ativa["ativa"]["cnpj"] == CNPJ_B
    assert sum(e["is_primary"] for e in ativa["lista"]) == 1

    # Remover a ativa: a outra assume
    depois = client.delete(f"/api/minhas-empresas/{id_b}", headers=a.headers).json()
    assert depois["ativa"]["id"] == primeira["id"] and len(depois["lista"]) == 1


def test_importacao_de_certidoes_no_cofre_cadastra_a_empresa(contas, monkeypatch):
    a, _ = contas
    monkeypatch.delenv("PORTAL_TRANSPARENCIA_API_KEY", raising=False)
    assert client.post(f"/api/documents/auto-import/{CNPJ_B}", headers=a.headers).status_code == 200
    assert client.get("/api/minhas-empresas", headers=a.headers).json()["ativa"]["cnpj"] == CNPJ_B


def test_empresas_isoladas_por_conta(contas):
    a, b = contas
    id_a = _cadastrar(a, CNPJ_A)["empresa"]["id"]
    assert client.get("/api/minhas-empresas", headers=b.headers).json() == {"lista": [], "ativa": None}
    assert client.post(f"/api/minhas-empresas/{id_a}/ativar", headers=b.headers).status_code == 404
    assert client.delete(f"/api/minhas-empresas/{id_a}", headers=b.headers).status_code == 404
    assert client.get("/api/minhas-empresas").status_code == 401
    # A minuta de B não traz os dados da empresa de A
    docx = _docx(client.get("/api/minutas/enquadramento-me-epp/download", headers=b.headers).content)
    assert "ALFA DISTRIBUIDORA" not in "".join(docx.values())
    # Mesmo CNPJ em contas diferentes é permitido (cada conta tem o seu cadastro)
    assert _cadastrar(b, CNPJ_A)["ativa"]["cnpj"] == CNPJ_A


# ------------------------------------------------------------------------------
# 2. White-label: minutas, petições e relatórios com o timbre da empresa ativa
# ------------------------------------------------------------------------------
def test_minuta_timbrada_com_dados_da_empresa_ativa_e_sem_marca(contas):
    a, _ = contas
    _cadastrar(a, CNPJ_A)
    for slug in ("enquadramento-me-epp", "proposta-comercial-readequada"):
        resp = client.get(f"/api/minutas/{slug}/download", headers=a.headers)
        assert resp.status_code == 200
        partes = _docx(resp.content)
        cabecalho = partes["word/header1.xml"]
        assert "ALFA DISTRIBUIDORA HOSPITALAR LTDA" in cabecalho and "11.222.333/0001-81" in cabecalho
        assert "RUA DAS FLORES" in cabecalho and "contato@exemplo.com.br" in cabecalho
        assert 'r:id="rIdTimbre"' in partes["word/document.xml"]
        corpo = partes["word/document.xml"]
        assert "ALFA DISTRIBUIDORA HOSPITALAR LTDA" in corpo and "MARIA ADMINISTRADORA" in corpo
        assert "[RAZÃO SOCIAL]" not in corpo and "[CNPJ]" not in corpo and "[ENDEREÇO COMPLETO]" not in corpo
        assert "[CPF]" in corpo  # CPF do representante não é armazenado: segue para preenchimento
        assert all("Licitações Brasil" not in conteudo for conteudo in partes.values())


def test_minuta_sem_empresa_fica_neutra():
    partes = _docx(client.get("/api/minutas/enquadramento-me-epp/download").content)
    assert "word/header1.xml" not in partes
    assert "[RAZÃO SOCIAL]" in partes["word/document.xml"]
    assert all("Licitações Brasil" not in c for c in partes.values())


def test_peticao_e_kit_saem_no_timbre_da_empresa(contas):
    from pypdf import PdfReader
    a, _ = contas
    _cadastrar(a, CNPJ_A)
    pdf = client.post("/api/juridico/gerar-pdf", headers=a.headers, json={
        "corpo": "A licitante impugna o edital.", "orgao": "Prefeitura de Exemplo", "numero_certame": "PE 1/2026",
        "objeto": "Medicamentos", "titulo": "Impugnação ao Edital"})
    assert pdf.status_code == 200, pdf.text
    texto = "".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf.content)).pages)
    assert "ALFA DISTRIBUIDORA HOSPITALAR LTDA" in texto and "11.222.333/0001-81" in texto
    assert "Licitações Brasil" not in texto

    db = SessionLocal()
    tender_id = db.query(Tender.id).first()[0]
    db.close()
    kit = client.get(f"/api/tenders/{tender_id}/download-kit-zip", headers=a.headers)
    relatorio = zipfile.ZipFile(io.BytesIO(kit.content)).read("Relatorio_Conformidade.pdf")
    texto_kit = "".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(relatorio)).pages)
    assert "ALFA DISTRIBUIDORA HOSPITALAR LTDA" in texto_kit and "Licitações Brasil" not in texto_kit


# ------------------------------------------------------------------------------
# 3. Índices contábeis e memorial
# ------------------------------------------------------------------------------
BALANCO = {"ativo_circulante": "500.000,00", "realizavel_longo_prazo": 100000, "ativo_total": 1000000,
           "passivo_circulante": 250000, "passivo_nao_circulante": 150000, "patrimonio_liquido": 600000}


def test_formulas_lg_sg_lc():
    r = indices_contabeis.calcular(BALANCO)
    valores = {i["sigla"]: i["valor"] for i in r["indices"]}
    assert valores == {"LG": 1.5, "SG": 2.5, "LC": 2.0}  # (500+100)/400, 1000/400, 500/250
    assert r["qualificado"] is True and r["diagnostico"] == "Qualificado para Licitar" and r["alertas"] == []


def test_indice_truncado_e_orientacao_abaixo_de_um():
    r = indices_contabeis.calcular({**BALANCO, "ativo_circulante": 999, "passivo_circulante": 1000,
                                    "realizavel_longo_prazo": 0, "passivo_nao_circulante": 0,
                                    "ativo_total": 999, "patrimonio_liquido": -1})
    lc = next(i for i in r["indices"] if i["sigla"] == "LC")
    assert lc["valor"] == 0.99 and lc["atende"] is False  # 0,999 nunca vira 1,00
    assert r["qualificado"] is False and lc["orientacao"]
    assert any("negativo" in a for a in r["alertas"])
    sem_passivo = indices_contabeis.calcular({**BALANCO, "passivo_circulante": 0, "passivo_nao_circulante": 0,
                                              "ativo_total": 600000, "patrimonio_liquido": 600000})
    assert sem_passivo["qualificado"] is True and all(i["valor"] is None for i in sem_passivo["indices"])


def test_api_de_indices_valida_e_alerta_inconsistencia():
    r = client.post("/api/juridico/indices-contabeis", json={**BALANCO, "ativo_total": 900000}).json()
    assert any("Passivo + Patrimônio Líquido" in a for a in r["alertas"])
    assert client.post("/api/juridico/indices-contabeis", json={**BALANCO, "ativo_circulante": "abc"}).status_code == 422
    assert client.post("/api/juridico/indices-contabeis", json={**BALANCO, "passivo_circulante": -5}).status_code == 422


def test_memorial_contabil_timbrado(contas):
    a, _ = contas
    _cadastrar(a, CNPJ_A)
    resp = client.post("/api/juridico/indices-contabeis/memorial", headers=a.headers, json={
        **BALANCO, "exercicio": "2025", "contador_nome": "Carla Contadora", "contador_crc": "AL-012345/O"})
    assert resp.status_code == 200
    assert resp.headers["content-disposition"].endswith('memorial-indices-contabeis-2025.docx"')
    partes = _docx(resp.content)
    corpo = partes["word/document.xml"]
    assert "DECLARAÇÃO E MEMORIAL DE CÁLCULO DE ÍNDICES CONTÁBEIS" in corpo
    assert "ALFA DISTRIBUIDORA HOSPITALAR LTDA" in partes["word/header1.xml"]
    assert "1,50" in corpo and "2,50" in corpo and "2,00" in corpo and "500.000,00" in corpo
    assert "Carla Contadora" in corpo and "CRC nº AL-012345/O" in corpo
    assert "Junta Comercial" in corpo and "SPED" in corpo
    assert all("Licitações Brasil" not in c for c in partes.values())


def test_referencia_de_faturamento_homologado(contas):
    a, b = contas
    db = SessionLocal()
    tender_id = db.query(Tender.id).first()[0]
    db.add(Deal(tender_id=tender_id, user_id=a.id, stage="homologado", valor_negocio=45200.0))
    db.add(Deal(tender_id=tender_id, user_id=b.id, stage="homologado", valor_negocio=999.0))
    db.commit()
    db.close()
    ref = client.get("/api/juridico/indices-contabeis/referencia", headers=a.headers).json()
    assert ref["faturamento_homologado"] == 45200.0 and ref["negocios_homologados"] == 1
    html = client.get("/juridico", headers=a.headers).text
    assert "Balanço Patrimonial &amp; Análise de Índices Contábeis" in html and 'id="ic-exportar"' in html


# ------------------------------------------------------------------------------
# 4. Cofre no contexto da empresa ativa
# ------------------------------------------------------------------------------
def test_cofre_segue_a_empresa_ativa(contas, monkeypatch):
    a, b = contas
    _cadastrar(a, CNPJ_A)
    db = SessionLocal()
    hoje = hoje_sao_paulo()
    for nome, cnpj in (("Doc da Alfa", CNPJ_A), ("Doc da Beta", CNPJ_B), ("Doc sem CNPJ", None)):
        db.add(DocumentoEmpresa(user_id=a.id, nome=nome, categoria="outros", data_emissao=hoje, caminho_arquivo="", cnpj=cnpj))
    db.commit()
    db.close()

    nomes = lambda: {d["nome"] for d in client.get("/api/documentos", headers=a.headers).json()["items"]}
    assert nomes() == {"Doc da Alfa", "Doc da Beta", "Doc sem CNPJ"}  # uma empresa: tudo visível
    lista = _cadastrar(a, CNPJ_B)["lista"]
    assert nomes() == {"Doc da Alfa", "Doc sem CNPJ"}  # ativa: Alfa
    client.post(f"/api/minhas-empresas/{next(e['id'] for e in lista if e['cnpj'] == CNPJ_B)}/ativar", headers=a.headers)
    assert nomes() == {"Doc da Beta", "Doc sem CNPJ"}
    assert client.get("/api/documentos", headers=b.headers).json()["items"] == []

    # Documento enviado com o CNPJ da outra empresa da conta: alerta específico
    db = SessionLocal()
    analise = document_ai.normalizar({"tipo_especifico": "act", "cnpj_cpf_identificado": CNPJ_A})
    doc, _ = document_ai.gravar_no_cofre(db, a.id, b"%PDF-1.4 teste", "application/pdf", "act.pdf", analise)
    db.commit()
    assert doc.metadados_ia["alertas"] == ["Documento de outra empresa da conta: ALFA DISTRIBUIDORA HOSPITALAR LTDA"]
    assert doc.cnpj == CNPJ_A
    db.close()


def test_navbar_em_tres_zonas_com_pilula_da_empresa_truncada(contas):
    import re as _re

    a, _ = contas
    _cadastrar(a, CNPJ_A)
    html = client.get("/dashboard", headers=a.headers).text
    cabecalho = html[html.index("<header"):html.index("</header>")]
    # Centro: links essenciais; secundários no menu "Mais"
    nav = cabecalho[cabecalho.index('aria-label="Navegação principal"'):]
    for rota in ("/search", "/dashboard", "/negocios", "/agenda", "/disputa", "/documentos", "/juridico"):
        assert f'href="{rota}' in nav, rota
    mais = nav[nav.index('class="nav-dropdown'):]
    assert all(f'href="{r}"' in mais for r in ("/fornecedores", "/alerts", "/favorites"))
    # Direita: zona isolada, perfil e "Sair" sem encolher
    assert "shrink-0 border-l border-slate-700/60 pl-3" in cabecalho
    assert _re.search(r'id="user-display" class="[^"]*shrink-0', cabecalho)
    assert _re.search(r'id="auth-btn"[^>]*class="shrink-0', cabecalho)
    # Pílula: largura máxima, truncamento e tooltip com CNPJ e razão social completa
    chip = _re.search(r'<span id="empresa-ativa-chip"[^>]*>', cabecalho).group(0)
    assert "max-w-[190px]" in chip and "2xl:max-w-[220px]" in chip
    assert 'title="11.222.333/0001-81 - ALFA DISTRIBUIDORA HOSPITALAR LTDA"' in chip
    assert 'class="truncate whitespace-nowrap">ALFA DISTRIBUIDORA HOSPITALAR LTDA</span>' in cabecalho
    assert "▾" not in cabecalho[cabecalho.index('id="empresa-ativa-chip"'):cabecalho.index('id="user-display"')]

    _cadastrar(a, CNPJ_B)
    cabecalho = client.get("/dashboard", headers=a.headers).text.split("</header>")[0]
    pilula = cabecalho[cabecalho.index('class="empresa-pill'):cabecalho.index('id="seletor-empresa"')]
    assert "max-w-[190px]" in pilula and "▾" in pilula and "fa-building" in pilula
