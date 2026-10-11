"""
Importação automática de certidões por CNPJ (Cofre) e Dossiê do Fornecedor.
BrasilAPI, PNCP e Portal da Transparência são simulados (rede externa bloqueada na suíte).
"""
import io
import zipfile
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas as rl_canvas

from server.database import SessionLocal
from server.main import app
from server.models import DocumentoEmpresa, DossieFornecedor, Tender, hoje_sao_paulo
from server.services import document_vault, dossie
from server.services.kit_builder import NOME_RELATORIO, PASTA_DOCUMENTOS, montar_kit_habilitacao_zip

CNPJ = "11222333000181"
OUTRO_CNPJ = "11444777000161"


def _receita(situacao="ATIVA", abertura="2015-03-10"):
    return {
        "cnpj": CNPJ, "razao_social": "EMPRESA EXEMPLO DE TESTES LTDA", "nome_fantasia": "EXEMPLO",
        "descricao_situacao_cadastral": situacao, "data_situacao_cadastral": "2015-03-10",
        "descricao_motivo_situacao_cadastral": "" if situacao == "ATIVA" else "EXTINCAO POR ENCERRAMENTO",
        "data_inicio_atividade": abertura, "capital_social": 150000, "porte": "MICRO EMPRESA",
        "natureza_juridica": "Sociedade Empresária Limitada", "opcao_pelo_simples": True,
        "descricao_identificador_matriz_filial": "MATRIZ",
        "descricao_tipo_de_logradouro": "RUA", "logradouro": "DAS FLORES", "numero": "100", "complemento": "",
        "bairro": "CENTRO", "municipio": "BRASILIA", "uf": "DF", "cep": "70000000",
        "cnae_fiscal": 6201501, "cnae_fiscal_descricao": "Desenvolvimento de programas de computador sob encomenda",
        "cnaes_secundarios": [{"codigo": 6204000, "descricao": "Consultoria em tecnologia da informação"}],
        "qsa": [{"nome_socio": "MARIA EXEMPLO", "qualificacao_socio": "Sócio-Administrador",
                 "data_entrada_sociedade": "2015-03-10", "faixa_etaria": "Entre 41 a 50 anos",
                 "cnpj_cpf_do_socio": "***123456**"}],
    }


def _contrato(fornecedor, orgao, orgao_cnpj, valor, assinatura, seq):
    return {"fornecedor_ni": fornecedor, "fornecedor_nome": "EMPRESA EXEMPLO DE TESTES LTDA",
            "orgao_nome": orgao, "orgao_cnpj": orgao_cnpj, "uf": "DF", "valor_global": valor,
            "data_assinatura": assinatura, "title": f"Contrato nº {seq}/2026", "description": "Serviços de TI",
            "item_url": f"/contratos/{orgao_cnpj}/2026/{seq}", "cancelado": False}


CONTRATOS = [
    _contrato(CNPJ, "MINISTERIO DA ECONOMIA", "00394460000141", 100000.0, "2026-09-01", 1),
    _contrato(CNPJ, "MINISTERIO DA ECONOMIA", "00394460000141", 50000.0, "2026-05-01", 2),
    _contrato(CNPJ, "PREFEITURA DE GOIANIA", "01612092000123", 25000.5, "2025-11-20", 3),
    # citação do CNPJ em contrato de outro fornecedor: não conta
    _contrato(OUTRO_CNPJ, "TRIBUNAL X", "00000000000272", 999999.0, "2026-10-01", 4),
]


@pytest.fixture
def fontes(monkeypatch):
    """Substitui as chamadas HTTP do dossiê; ``estado`` controla as respostas e conta as chamadas."""
    estado = {"receita": _receita(), "sancoes": [], "pncp_fora": False, "chamadas": []}

    def falso(url, params=None, headers=None):
        estado["chamadas"].append(url)
        if "brasilapi" in url:
            return estado["receita"]
        if "pncp.gov.br/api/search" in url:
            if estado["pncp_fora"]:
                raise dossie.FonteIndisponivelError("pncp.gov.br respondeu HTTP 503")
            if params["q"] == CNPJ:
                return {"total": len(CONTRATOS), "items": CONTRATOS}
            return {"total": 2, "items": [CONTRATOS[0], {**CONTRATOS[3], "fornecedor_nome": "OUTRA EMPRESA LTDA"}]}
        if "portaldatransparencia" in url:
            assert headers["chave-api-dados"] == "chave-teste"
            return estado["sancoes"] if url.endswith("/ceis") else []
        raise AssertionError(f"URL inesperada: {url}")

    monkeypatch.setattr(dossie, "requisitar_json", falso)
    monkeypatch.delenv("PORTAL_TRANSPARENCIA_API_KEY", raising=False)
    db = SessionLocal()
    db.query(DossieFornecedor).delete()
    db.commit()
    db.close()
    yield estado
    db = SessionLocal()
    db.query(DossieFornecedor).delete()
    db.commit()
    db.close()


@pytest.fixture
def cofre(tmp_path, monkeypatch):
    monkeypatch.setattr(document_vault, "DOCUMENTOS_UPLOAD_DIR", tmp_path / "documentos")
    yield tmp_path / "documentos"
    db = SessionLocal()
    db.query(DocumentoEmpresa).filter(DocumentoEmpresa.cnpj == CNPJ).delete(synchronize_session=False)
    db.commit()
    db.close()


def _cliente(conta=None):
    return TestClient(app, cookies=conta.cookies if conta else None)


def _pdf(texto="Certidao"):
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf)
    c.drawString(72, 720, texto)
    c.showPage()
    c.save()
    return buf.getvalue()


# ------------------------------------------------------------------------------
# CNPJ
# ------------------------------------------------------------------------------
def test_validacao_e_formatacao_de_cnpj():
    assert dossie.cnpj_valido("11.222.333/0001-81") and dossie.cnpj_valido(CNPJ)
    assert not dossie.cnpj_valido("11.222.333/0001-82")
    assert not dossie.cnpj_valido("00000000000000") and not dossie.cnpj_valido("123")
    assert dossie.formatar_cnpj(CNPJ) == "11.222.333/0001-81"
    with pytest.raises(dossie.CnpjInvalidoError):
        dossie.normalizar_cnpj("11222333000180")


# ------------------------------------------------------------------------------
# Parte 1: importação automática no Cofre
# ------------------------------------------------------------------------------
def test_importacao_cria_pacote_de_habilitacao_sem_inventar_certidoes(fontes, cofre, criar_usuario):
    conta = criar_usuario()
    cliente = _cliente(conta)
    resp = cliente.post("/api/documents/auto-import/11.222.333000181")  # pontuação é ignorada
    assert resp.status_code == 200, resp.text
    corpo = resp.json()
    assert corpo["razao_social"] == "EMPRESA EXEMPLO DE TESTES LTDA" and corpo["situacao"] == "ATIVA"
    por_chave = {c["chave"]: c for c in corpo["certidoes"]}
    assert set(por_chave) == {"cartao_cnpj", "cnd_federal", "crf_fgts", "cndt", "sancoes"}
    # Só o Cartão CNPJ foi consultado de fato; sem chave do Portal da Transparência, sanções ficam pendentes
    assert por_chave["cartao_cnpj"]["resultado"] == "valido"
    for chave in ("cnd_federal", "crf_fgts", "cndt", "sancoes"):
        assert por_chave[chave]["resultado"] == "pendente", chave
        assert por_chave[chave]["link_oficial"].startswith("https://")
    assert corpo["resumo"] == {"valido": 1, "pendente": 4, "alerta": 0, "existente": 0}

    lista = cliente.get("/api/documentos").json()
    docs = {d["nome"]: d for d in lista["items"]}
    cartao = docs["Comprovante de Inscrição e Situação Cadastral (Cartão CNPJ)"]
    hoje = hoje_sao_paulo()
    assert cartao["status"] == "VALIDO" and cartao["categoria"] == "juridica"
    assert cartao["data_emissao"] == hoje.isoformat()
    assert cartao["data_validade"] == (hoje + timedelta(days=180)).isoformat()
    assert cartao["arquivo_disponivel"] and cartao["origem"] == "CNPJ"
    pdf = cliente.get(cartao["arquivo_url"])
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")

    fgts = docs["Certificado de Regularidade do FGTS (CRF Caixa)"]
    assert fgts["status"] == "PENDENTE" and fgts["pendente"] is True
    assert fgts["data_emissao"] is None and fgts["data_validade"] is None
    assert fgts["validade_padrao_dias"] == 30 and not fgts["arquivo_disponivel"]
    assert docs["Certidão Negativa de Débitos Trabalhistas (CNDT - TST)"]["categoria"] == "fiscal"
    assert lista["resumo"]["pendentes"] == 4

    # Reimportar não duplica
    de_novo = cliente.post(f"/api/documents/auto-import/{CNPJ}").json()
    assert de_novo["resumo"]["existente"] == 5
    assert len(cliente.get("/api/documentos").json()["items"]) == 5


def test_importacao_com_consulta_de_sancoes_configurada(fontes, cofre, criar_usuario, monkeypatch):
    monkeypatch.setenv("PORTAL_TRANSPARENCIA_API_KEY", "chave-teste")
    cliente = _cliente(criar_usuario())
    corpo = cliente.post(f"/api/documents/auto-import/{CNPJ}").json()
    sancoes = next(c for c in corpo["certidoes"] if c["chave"] == "sancoes")
    assert sancoes["resultado"] == "valido"
    doc = next(d for d in cliente.get("/api/documentos").json()["items"] if "Sanções" in d["nome"])
    assert doc["status"] == "VALIDO"
    assert doc["data_validade"] == (hoje_sao_paulo() + timedelta(days=30)).isoformat()


def test_importacao_com_sancao_ou_cnpj_inativo_nao_gera_selo_verde(fontes, cofre, criar_usuario, monkeypatch):
    monkeypatch.setenv("PORTAL_TRANSPARENCIA_API_KEY", "chave-teste")
    fontes["receita"] = _receita(situacao="BAIXADA")
    fontes["sancoes"] = [
        {"sancionado": {"codigoFormatado": "11.222.333/0001-81"}, "tipoSancao": {"descricaoResumida": "Impedimento"},
         "orgaoSancionador": {"nome": "MINISTERIO X"}, "dataInicioSancao": "01/01/2026", "dataFimSancao": "01/01/2028"},
        # sanção de outra empresa devolvida pela API nunca é atribuída a este CNPJ
        {"sancionado": {"codigoFormatado": "11.444.777/0001-61"}, "tipoSancao": {"descricaoResumida": "Inidoneidade"}},
    ]
    corpo = _cliente(criar_usuario()).post(f"/api/documents/auto-import/{CNPJ}").json()
    por_chave = {c["chave"]: c for c in corpo["certidoes"]}
    assert por_chave["cartao_cnpj"]["resultado"] == "alerta" and "BAIXADA" in por_chave["cartao_cnpj"]["detalhe"]
    assert por_chave["sancoes"]["resultado"] == "alerta" and "1 registro" in por_chave["sancoes"]["detalhe"]
    assert corpo["resumo"]["valido"] == 0


def test_cofre_e_privado_por_conta(fontes, cofre, criar_usuario):
    dona, outra = criar_usuario(), criar_usuario()
    _cliente(dona).post(f"/api/documents/auto-import/{CNPJ}")
    doc_id = _cliente(dona).get("/api/documentos").json()["items"][0]["id"]
    assert _cliente(outra).get("/api/documentos").json()["items"] == []
    assert all(d.get("cnpj") != CNPJ for d in _cliente().get("/api/documentos").json()["items"])
    assert _cliente(outra).get(f"/api/documentos/{doc_id}/arquivo").status_code == 404
    assert _cliente(outra).delete(f"/api/documentos/{doc_id}").status_code == 404
    assert "EMPRESA EXEMPLO" not in _cliente(outra).get("/documentos").text


def test_anexar_pdf_a_certidao_pendente(fontes, cofre, criar_usuario):
    cliente = _cliente(criar_usuario())
    cliente.post(f"/api/documents/auto-import/{CNPJ}")
    fgts = next(d for d in cliente.get("/api/documentos").json()["items"] if "FGTS" in d["nome"])
    emissao = hoje_sao_paulo() - timedelta(days=2)

    invalido = cliente.post(f"/api/documentos/{fgts['id']}/anexar", data={"data_emissao": emissao.isoformat()},
                            files={"arquivo": ("crf.pdf", b"nao e pdf", "application/pdf")})
    assert invalido.status_code == 422

    resp = cliente.post(f"/api/documentos/{fgts['id']}/anexar", data={"data_emissao": emissao.isoformat()},
                        files={"arquivo": ("crf.pdf", _pdf("CRF"), "application/pdf")})
    assert resp.status_code == 200, resp.text
    doc = resp.json()
    assert doc["pendente"] is False and doc["status"] == "VALIDO"
    assert doc["data_emissao"] == emissao.isoformat()
    assert doc["data_validade"] == (emissao + timedelta(days=30)).isoformat()  # validade padrão do CRF
    assert doc["arquivo_disponivel"]

    # validade informada prevalece sobre a padrão
    cndt = next(d for d in cliente.get("/api/documentos").json()["items"] if "CNDT" in d["nome"])
    validade = (emissao + timedelta(days=150)).isoformat()
    doc = cliente.post(f"/api/documentos/{cndt['id']}/anexar",
                       data={"data_emissao": emissao.isoformat(), "data_validade": validade},
                       files={"arquivo": ("cndt.pdf", _pdf("CNDT"), "application/pdf")}).json()
    assert doc["data_validade"] == validade


def test_kit_de_habilitacao_ignora_pendentes_e_documentos_de_outra_conta(fontes, cofre, criar_usuario):
    dona, outra = criar_usuario(), criar_usuario()
    _cliente(dona).post(f"/api/documents/auto-import/{CNPJ}")
    db = SessionLocal()
    try:
        tender_id = db.query(Tender.id).first()[0]
        conteudo = montar_kit_habilitacao_zip(tender_id, db=db, user_id=dona.id)
        vazio = montar_kit_habilitacao_zip(tender_id, db=db, user_id=outra.id)
    finally:
        db.close()
    nomes = [n for n in zipfile.ZipFile(io.BytesIO(conteudo)).namelist() if n.startswith(PASTA_DOCUMENTOS + "/")]
    assert len(nomes) == 1 and "Cartao_CNPJ" in nomes[0]
    assert [n for n in zipfile.ZipFile(io.BytesIO(vazio)).namelist() if n != NOME_RELATORIO] == []


def test_importacao_erros_de_cnpj_e_fonte(fontes, cofre, criar_usuario, monkeypatch):
    cliente = _cliente(criar_usuario())
    assert cliente.post("/api/documents/auto-import/11222333000180").status_code == 422
    fontes["receita"] = None
    assert cliente.post(f"/api/documents/auto-import/{CNPJ}").status_code == 404

    def fora(*_a, **_k):
        raise dossie.FonteIndisponivelError("brasilapi.com.br respondeu HTTP 429")
    monkeypatch.setattr(dossie, "requisitar_json", fora)
    resp = cliente.post(f"/api/documents/auto-import/{CNPJ}")
    assert resp.status_code == 503 and "indisponível" in resp.json()["detail"]
    assert cliente.get("/api/documentos").json()["items"] == []  # nada é gravado sem a consulta


# ------------------------------------------------------------------------------
# Parte 2: Dossiê do Fornecedor
# ------------------------------------------------------------------------------
def test_dossie_reune_receita_contratos_e_risco(fontes):
    resp = _cliente().get(f"/api/fornecedores/dossie/{CNPJ}")
    assert resp.status_code == 200, resp.text
    d = resp.json()
    r = d["receita"]
    assert r["razao_social"] == "EMPRESA EXEMPLO DE TESTES LTDA" and r["capital_social"] == 150000
    assert r["qsa"][0]["nome"] == "MARIA EXEMPLO" and r["qsa"][0]["qualificacao"] == "Sócio-Administrador"
    assert r["cnae_principal"]["codigo"] == "6201501" and r["cnaes_secundarios"][0]["codigo"] == "6204000"
    assert d["fundacao"]["anos"] >= 10

    c = d["contratos"]
    assert c["total_contratos"] == 3  # o contrato de outro fornecedor fica de fora
    assert c["valor_acumulado"] == 175000.5
    assert c["orgaos_distintos"] == 2
    assert c["principais_orgaos"][0]["nome"] == "MINISTERIO DA ECONOMIA"
    assert c["principais_orgaos"][0]["contratos"] == 2 and c["principais_orgaos"][0]["valor"] == 150000.0
    assert c["ultimo_contrato"] == "2026-09-01"
    assert c["recentes"][0]["url"] == "https://pncp.gov.br/app/contratos/00394460000141/2026/1"

    # Sem consulta de sanções configurada nunca sai "Apto": fica em atenção
    assert d["risco"]["nivel"] == "amarelo" and d["risco"]["rotulo"] == "Atenção Cadastral"
    assert d["sancoes"]["verificado"] is False
    assert d["cache"] is False


def test_matriz_de_risco():
    receita = {"situacao": "ATIVA", "data_abertura": "2015-01-01"}
    limpo = {"verificado": True, "registros": []}
    hoje = date(2026, 10, 11)
    assert dossie.avaliar_risco(receita, limpo, hoje)["rotulo"] == "Apto para Licitar"
    assert dossie.avaliar_risco({**receita, "data_abertura": "2026-03-01"}, limpo, hoje)["nivel"] == "amarelo"
    assert dossie.avaliar_risco(receita, {"verificado": True, "registros": [{}]}, hoje)["nivel"] == "vermelho"
    inapto = dossie.avaliar_risco({**receita, "situacao": "BAIXADA"}, limpo, hoje)
    assert inapto["nivel"] == "vermelho" and inapto["rotulo"] == "Inapto para Licitar"


def test_dossie_verde_com_sancoes_verificadas(fontes, monkeypatch):
    monkeypatch.setenv("PORTAL_TRANSPARENCIA_API_KEY", "chave-teste")
    d = _cliente().get(f"/api/fornecedores/dossie/{CNPJ}").json()
    assert d["sancoes"]["verificado"] is True and d["risco"]["nivel"] == "verde"


def test_dossie_usa_cache_de_24_horas(fontes):
    cliente = _cliente()
    cliente.get(f"/api/fornecedores/dossie/{CNPJ}")
    chamadas = len(fontes["chamadas"])
    segundo = cliente.get(f"/api/fornecedores/dossie/{CNPJ}").json()
    assert segundo["cache"] is True and len(fontes["chamadas"]) == chamadas

    assert cliente.get(f"/api/fornecedores/dossie/{CNPJ}?atualizar=true").json()["cache"] is False
    assert len(fontes["chamadas"]) > chamadas

    db = SessionLocal()
    registro = db.get(DossieFornecedor, CNPJ)
    registro.atualizado_em = datetime.utcnow() - timedelta(hours=25)
    db.commit()
    db.close()
    assert cliente.get(f"/api/fornecedores/dossie/{CNPJ}").json()["cache"] is False


def test_dossie_com_pncp_fora_do_ar_nao_fica_em_cache(fontes):
    fontes["pncp_fora"] = True
    d = _cliente().get(f"/api/fornecedores/dossie/{CNPJ}").json()
    assert d["contratos"]["disponivel"] is False and d["receita"]["razao_social"]
    db = SessionLocal()
    assert db.get(DossieFornecedor, CNPJ) is None
    db.close()


def test_dossie_erros(fontes):
    cliente = _cliente()
    assert cliente.get("/api/fornecedores/dossie/123").status_code == 422
    fontes["receita"] = None
    assert cliente.get(f"/api/fornecedores/dossie/{CNPJ}").status_code == 404


def test_busca_de_fornecedor_por_cnpj_ou_nome(fontes):
    cliente = _cliente()
    por_cnpj = cliente.get("/api/fornecedores/busca", params={"q": "11.222.333/0001-81"}).json()
    assert por_cnpj["resultados"][0]["cnpj"] == CNPJ
    assert cliente.get("/api/fornecedores/busca", params={"q": "11.222.333/0001-80"}).status_code == 422
    assert cliente.get("/api/fornecedores/busca", params={"q": "ab"}).status_code == 422
    por_nome = cliente.get("/api/fornecedores/busca", params={"q": "exemplo testes"}).json()["resultados"]
    assert por_nome == [{"cnpj": CNPJ, "cnpj_formatado": "11.222.333/0001-81", "nome": "EMPRESA EXEMPLO DE TESTES LTDA"}]


# ------------------------------------------------------------------------------
# Frontend
# ------------------------------------------------------------------------------
def test_paginas_exibem_importacao_rapida_e_dossie():
    cliente = _cliente()
    cofre_html = cliente.get("/documentos").text
    assert "Preenchimento Rápido por CNPJ" in cofre_html and "Puxar Certidões Públicas" in cofre_html
    assert "/api/documents/auto-import/" in cofre_html and 'id="modal-anexar"' in cofre_html
    fornecedores_html = cliente.get("/fornecedores").text
    assert "Dossiê 360° do Fornecedor" in fornecedores_html
    assert "/api/fornecedores/dossie/" in fornecedores_html and "window.print()" in fornecedores_html


def test_requisicao_repete_falhas_transitorias(monkeypatch):
    import httpx

    respostas = [httpx.RemoteProtocolError("desconectou"), httpx.Response(503),
                 httpx.Response(200, json={"ok": True})]

    def get_falso(url, **_kwargs):
        resposta = respostas.pop(0)
        if isinstance(resposta, Exception):
            raise resposta
        return resposta

    monkeypatch.setattr(dossie.httpx, "get", get_falso)
    monkeypatch.setattr(dossie, "ESPERA_ENTRE_TENTATIVAS", 0)
    assert dossie.requisitar_json("https://pncp.gov.br/api/search/") == {"ok": True}

    respostas[:] = [httpx.ConnectError("x")] * dossie.TENTATIVAS
    with pytest.raises(dossie.FonteIndisponivelError):
        dossie.requisitar_json("https://pncp.gov.br/api/search/")
    respostas[:] = [httpx.Response(404)]
    assert dossie.requisitar_json("https://brasilapi.com.br/api/cnpj/v1/x") is None
