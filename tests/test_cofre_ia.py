"""Cofre com inteligência documental: Gemini Flash (simulado), regras, lote, slots e isolamento por conta."""
import asyncio
import io
import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas as rl_canvas

from server.database import SessionLocal
from server.main import app
from server.models import ArquivoCofre, DocumentoEmpresa, hoje_sao_paulo
from server.services import document_ai

client = TestClient(app)
CNPJ_EMPRESA = "11222333000181"
HOJE = hoje_sao_paulo()


def _pdf(*linhas):
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf)
    y = 780
    for linha in linhas:
        c.drawString(40, y, linha)
        y -= 20
    c.showPage()
    c.save()
    return buf.getvalue()


PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")

RESPOSTAS = {
    "cnd": {
        "categoria": "fiscal_trabalhista", "tipo_especifico": "cnd_federal",
        "titulo_documento": "Certidão Negativa de Débitos Relativos aos Tributos Federais e à Dívida Ativa da União",
        "orgao_emissor": "Receita Federal do Brasil / PGFN", "cnpj_cpf_identificado": "11.222.333/0001-81",
        "razao_social_identificada": "EMPRESA EXEMPLO LTDA", "data_emissao": (HOJE - timedelta(days=5)).isoformat(),
        "data_validade": (HOJE + timedelta(days=175)).isoformat(), "dias_para_vencer": 999,
        "codigo_autenticidade": "A1B2.C3D4.E5F6.7890", "situacao": "vencido",
        "dados_especificos": {"indices_contabeis": {"lg": None, "sg": None, "lc": None}, "objeto_atestado": None,
                              "exercicio_ano": None},
        "resumo_ia": "CND federal válida."},
    "balanco": {
        "categoria": "juridica", "tipo_especifico": "balanco_patrimonial", "titulo_documento": "Balanço Patrimonial 2025",
        "orgao_emissor": "Junta Comercial (SPED)", "cnpj_cpf_identificado": CNPJ_EMPRESA,
        "razao_social_identificada": "EMPRESA EXEMPLO LTDA", "data_emissao": "2026-04-30", "data_validade": None,
        "dias_para_vencer": None, "codigo_autenticidade": None, "situacao": "indeterminado",
        "dados_especificos": {"indices_contabeis": {"lg": 1.85, "sg": "2,4", "lc": 1.32}, "objeto_atestado": None,
                              "exercicio_ano": 2025},
        "resumo_ia": "Balanço do exercício de 2025 com índices acima de 1."},
    "atestado": {
        "categoria": "tecnica", "tipo_especifico": "act", "titulo_documento": "Atestado de Capacidade Técnica",
        "orgao_emissor": "Prefeitura de Exemplo", "cnpj_cpf_identificado": "99888777000166",
        "razao_social_identificada": "OUTRA EMPRESA LTDA", "data_emissao": "2025-11-10", "data_validade": None,
        "dias_para_vencer": None, "codigo_autenticidade": None, "situacao": "indeterminado",
        "dados_especificos": {"indices_contabeis": {"lg": None, "sg": None, "lc": None},
                              "objeto_atestado": "Fornecimento de 500 notebooks com garantia de 36 meses",
                              "exercicio_ano": None},
        "resumo_ia": "Atestado de fornecimento de notebooks."},
}


class _Resposta:
    def __init__(self, texto):
        self.text = texto


class _GeminiFalso:
    """Simula client.aio.models.generate_content e guarda o que recebeu."""

    def __init__(self, falhar=False):
        self.chamadas = []
        self.falhar = falhar
        self.aio = self
        self.models = self

    async def generate_content(self, model, contents, config):
        self.chamadas.append({"model": model, "contents": contents, "config": config})
        if self.falhar:
            raise RuntimeError("503 UNAVAILABLE")
        texto = next(str(c) for c in contents if isinstance(c, str))
        chave = next(k for k in RESPOSTAS if k in texto.split("Nome do arquivo enviado:")[-1].lower())
        return _Resposta(json.dumps(RESPOSTAS[chave]))


@pytest.fixture
def gemini(monkeypatch):
    falso = _GeminiFalso()
    monkeypatch.setattr(document_ai, "_cliente_gemini", lambda: falso)
    return falso


@pytest.fixture
def contas(criar_usuario):
    a, b = criar_usuario("Empresa A"), criar_usuario("Empresa B")
    yield a, b
    db = SessionLocal()
    for modelo in (DocumentoEmpresa, ArquivoCofre):
        db.query(modelo).filter(modelo.user_id.in_([a.id, b.id])).delete(synchronize_session=False)
    db.commit()
    db.close()


# ------------------------------------------------------------------------------
# Extração (Gemini simulado) e normalização
# ------------------------------------------------------------------------------
def test_extrai_certidao_com_prazo_e_recalcula_situacao(gemini):
    r = asyncio.run(document_ai.analisar_documento(_pdf("CND"), "application/pdf", "cnd.pdf"))
    assert r["metodo"] == "gemini" and r["categoria"] == "fiscal_trabalhista" and r["tipo_especifico"] == "cnd_federal"
    assert r["cnpj_cpf_identificado"] == CNPJ_EMPRESA and r["codigo_autenticidade"] == "A1B2.C3D4.E5F6.7890"
    assert r["data_validade"] == (HOJE + timedelta(days=175)).isoformat()
    # dias e situação vêm da data de hoje, não da IA (que disse 999 e "vencido")
    assert r["dias_para_vencer"] == 175 and r["situacao"] == "valido"
    chamada = gemini.chamadas[0]
    assert chamada["model"] == document_ai.modelos_candidatos()[0]
    assert chamada["config"].response_mime_type == "application/json"
    assert chamada["config"].response_json_schema == document_ai.SCHEMA


def test_extrai_balanco_com_indices_e_corrige_categoria(gemini):
    r = asyncio.run(document_ai.analisar_documento(_pdf("Balanco"), "application/pdf", "balanco_2025.pdf"))
    assert r["tipo_especifico"] == "balanco_patrimonial"
    assert r["categoria"] == "financeira"  # a IA disse "juridica": o tipo define a categoria legal
    assert r["dados_especificos"]["indices_contabeis"] == {"lg": 1.85, "sg": 2.4, "lc": 1.32}
    assert r["dados_especificos"]["exercicio_ano"] == 2025


def test_extrai_atestado_sem_validade_definida(gemini):
    r = asyncio.run(document_ai.analisar_documento(_pdf("Atestado"), "application/pdf", "atestado_prefeitura.pdf"))
    assert r["categoria"] == "tecnica" and r["tipo_especifico"] == "act"
    assert r["data_validade"] is None and r["dias_para_vencer"] is None and r["situacao"] == "indeterminado"
    assert r["dados_especificos"]["objeto_atestado"].startswith("Fornecimento de 500 notebooks")


def test_normalizacao_descarta_dados_incoerentes():
    r = document_ai.normalizar({"categoria": "inventada", "tipo_especifico": "nao_existe",
                                "data_emissao": (HOJE + timedelta(days=3)).isoformat(),
                                "data_validade": "31/02/2027", "cnpj_cpf_identificado": "123"}, HOJE)
    assert r["tipo_especifico"] == "outro" and r["categoria"] == "outro"
    assert r["data_emissao"] is None and r["data_validade"] is None and r["cnpj_cpf_identificado"] is None
    assert any("futuro" in a for a in r["alertas"])
    vencida = document_ai.normalizar({"tipo_especifico": "cndt", "data_validade": (HOJE - timedelta(days=1)).isoformat()}, HOJE)
    assert vencida["situacao"] == "vencido" and vencida["categoria"] == "fiscal_trabalhista"
    atencao = document_ai.normalizar({"tipo_especifico": "crf_fgts", "data_validade": (HOJE + timedelta(days=10)).isoformat()}, HOJE)
    assert atencao["situacao"] == "atencao"


def test_sem_gemini_usa_regras_sobre_o_texto_do_pdf(monkeypatch):
    monkeypatch.setattr(document_ai, "_cliente_gemini", lambda: None)
    validade = (HOJE + timedelta(days=90)).strftime("%d/%m/%Y")
    pdf = _pdf("CERTIDAO NEGATIVA DE DEBITOS RELATIVOS AOS TRIBUTOS FEDERAIS E A DIVIDA ATIVA DA UNIAO",
               "Procuradoria-Geral da Fazenda Nacional", "CNPJ: 11.222.333/0001-81",
               f"Emitida as 10:00:00 do dia {(HOJE - timedelta(days=1)).strftime('%d/%m/%Y')}.",
               f"Valida ate {validade}.", "Codigo de controle da certidao: A1B2.C3D4.E5F6.7890")
    r = asyncio.run(document_ai.analisar_documento(pdf, "application/pdf", "documento.pdf"))
    assert r["metodo"] == "regras" and r["tipo_especifico"] == "cnd_federal"
    assert r["cnpj_cpf_identificado"] == CNPJ_EMPRESA and r["dias_para_vencer"] == 90
    assert r["codigo_autenticidade"] == "A1B2.C3D4.E5F6.7890"
    assert any("sem IA" in a for a in r["alertas"])


def test_gemini_fora_do_ar_cai_nas_regras(monkeypatch):
    falso = _GeminiFalso(falhar=True)
    monkeypatch.setattr(document_ai, "_cliente_gemini", lambda: falso)
    monkeypatch.setattr(document_ai, "ESPERA_RETENTATIVA_SEGUNDOS", 0)
    r = asyncio.run(document_ai.analisar_documento(_pdf("Atestado de capacidade tecnica"), "application/pdf", "x.pdf"))
    assert r["metodo"] == "regras" and r["tipo_especifico"] == "act"
    # 503 é transitório: cada modelo é tentado de novo antes de passar ao próximo
    assert len(falso.chamadas) == len(document_ai.modelos_candidatos()) * document_ai.TENTATIVAS_POR_MODELO


# ------------------------------------------------------------------------------
# Lote, slots e validação cruzada de CNPJ
# ------------------------------------------------------------------------------
def _slot_pendente(user_id, tipo="cnd_federal"):
    db = SessionLocal()
    doc = DocumentoEmpresa(user_id=user_id, nome="Certidão Negativa de Débitos Federais e Dívida Ativa (PGFN/Receita)",
                           categoria="fiscal", data_emissao=HOJE, caminho_arquivo="", pendente=True, origem="CNPJ",
                           cnpj=CNPJ_EMPRESA, tipo_documento=tipo, validade_padrao_dias=180)
    db.add(doc)
    db.commit()
    doc_id = doc.id
    db.close()
    return doc_id


def _enviar(conta, *arquivos):
    return client.post("/api/documents/processar-lote", headers=conta.headers,
                       files=[("arquivos", a) for a in arquivos])


def test_lote_classifica_atualiza_slot_e_cria_documentos(gemini, contas):
    a, _ = contas
    slot = _slot_pendente(a.id)
    resp = _enviar(a, ("cnd.pdf", _pdf("CND"), "application/pdf"),
                   ("balanco_2025.pdf", _pdf("Balanco"), "application/pdf"),
                   ("atestado.png", PNG_1PX, "image/png"),
                   ("leia-me.txt", b"texto qualquer", "text/plain"))
    assert resp.status_code == 200, resp.text
    corpo = resp.json()
    assert corpo["total"] == 4 and corpo["concluidos"] == 3
    por_arquivo = {r["arquivo"]: r for r in corpo["resultados"]}
    assert por_arquivo["leia-me.txt"]["status"] == "erro"

    cnd = por_arquivo["cnd.pdf"]
    assert cnd["acao"] == "atualizado" and cnd["documento"]["id"] == slot  # slot pendente da importação por CNPJ
    assert cnd["documento"]["status"] == "VALIDO" and cnd["documento"]["pendente"] is False
    assert cnd["documento"]["nome"] == "Certidão Negativa de Débitos Federais e Dívida Ativa (PGFN/Receita)"
    assert cnd["metadados"]["codigo_autenticidade"] == "A1B2.C3D4.E5F6.7890" and cnd["alertas"] == []

    balanco = por_arquivo["balanco_2025.pdf"]["documento"]
    assert balanco["categoria"] == "economica" and balanco["status"] == "SEM_VALIDADE"
    assert balanco["metadados_ia"]["dados_especificos"]["indices_contabeis"]["lg"] == 1.85

    atestado = por_arquivo["atestado.png"]
    assert atestado["acao"] == "criado" and atestado["documento"]["categoria"] == "tecnica"
    assert "CNPJ divergente do cadastro" in atestado["alertas"]
    imagem = client.get(f"{atestado['documento']['arquivo_url']}?inline=1", headers=a.headers)
    assert imagem.status_code == 200 and imagem.headers["content-type"] == "image/png"
    assert imagem.headers["content-disposition"].startswith("inline")

    # Arquivos persistidos no banco (sobrevivem a deploys)
    db = SessionLocal()
    docs = db.query(DocumentoEmpresa).filter(DocumentoEmpresa.user_id == a.id).all()
    assert all(d.caminho_arquivo.startswith("db:") for d in docs) and len(docs) == 3
    assert {d.tipo_documento for d in docs} == {"cnd_federal", "balanco_patrimonial", "act"}
    db.close()

    # Atestados se acumulam; a CND renovada continua no mesmo slot
    segundo = _enviar(a, ("atestado_2.pdf", _pdf("Atestado 2"), "application/pdf"),
                      ("cnd_renovada.pdf", _pdf("CND"), "application/pdf")).json()
    acoes = {r["arquivo"]: r["acao"] for r in segundo["resultados"]}
    assert acoes == {"atestado_2.pdf": "criado", "cnd_renovada.pdf": "atualizado"}
    db = SessionLocal()
    assert db.query(DocumentoEmpresa).filter(DocumentoEmpresa.user_id == a.id, DocumentoEmpresa.tipo_documento == "act").count() == 2
    assert db.query(DocumentoEmpresa).filter(DocumentoEmpresa.user_id == a.id, DocumentoEmpresa.tipo_documento == "cnd_federal").count() == 1
    assert db.query(ArquivoCofre).filter(ArquivoCofre.user_id == a.id).count() == 4  # arquivo anterior da CND removido
    db.close()


def test_isolamento_entre_contas(gemini, contas):
    a, b = contas
    slot_a = _slot_pendente(a.id)
    doc_a = _enviar(a, ("balanco.pdf", _pdf("Balanco"), "application/pdf")).json()["resultados"][0]["documento"]

    # B envia uma CND: nunca atualiza o slot pendente de A
    r_b = _enviar(b, ("cnd.pdf", _pdf("CND"), "application/pdf")).json()["resultados"][0]
    assert r_b["acao"] == "criado" and r_b["documento"]["id"] != slot_a
    db = SessionLocal()
    assert db.get(DocumentoEmpresa, slot_a).pendente is True
    db.close()

    ids_b = {d["id"] for d in client.get("/api/documentos", headers=b.headers).json()["items"]}
    assert doc_a["id"] not in ids_b and slot_a not in ids_b and r_b["documento"]["id"] in ids_b
    assert client.get(doc_a["arquivo_url"], headers=b.headers).status_code == 404
    assert client.delete(f"/api/documentos/{doc_a['id']}", headers=b.headers).status_code == 404
    titulo_json = json.dumps("Balanço Patrimonial 2025")[1:-1]  # forma embutida no JSON da página
    assert titulo_json not in client.get("/documentos", headers=b.headers).text
    assert titulo_json in client.get("/documentos", headers=a.headers).text

    # Sem conta não há lote
    assert client.post("/api/documents/processar-lote",
                       files=[("arquivos", ("x.pdf", _pdf("x"), "application/pdf"))]).status_code == 401


def test_lote_limita_quantidade(gemini, contas):
    a, _ = contas
    arquivos = [(f"a{i}.pdf", _pdf("Atestado"), "application/pdf") for i in range(21)]
    assert _enviar(a, *arquivos).status_code == 422


def test_pagina_do_cofre_tem_zona_de_lote_filtros_e_detalhes():
    html = client.get("/documentos").text
    for marcador in ("Arraste suas Certidões, Balanços, Atestados e Contratos em PDF (Classificação Automática por IA)",
                     'id="lote-arquivos"', "/api/documents/processar-lote", "Analisando com Gemini...",
                     'data-categoria="fiscal"', "Fiscal/Trabalhista", "Financeira", "Sanções",
                     "Vence em breve", "Indeterminado", 'id="modal-detalhes"', "Código de autenticidade",
                     "Índices contábeis", "Objeto atestado"):
        assert marcador in html, marcador
