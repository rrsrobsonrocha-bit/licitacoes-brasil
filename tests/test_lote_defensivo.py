"""Upload em lote do Cofre: arquivos vazios, pastas, formatos não aceitos e falhas isoladas por arquivo."""
import asyncio
import io

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas as rl_canvas
from starlette.datastructures import UploadFile

from server.database import SessionLocal
from server.main import app
from server.models import ArquivoCofre, DocumentoEmpresa
from server.routes import documents as rotas_documentos
from server.services import document_ai

client = TestClient(app)


def _pdf(texto="Atestado de capacidade tecnica"):
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf)
    c.drawString(72, 720, texto)
    c.showPage()
    c.save()
    return buf.getvalue()


@pytest.fixture
def conta(criar_usuario, monkeypatch):
    monkeypatch.setattr(document_ai, "_cliente_gemini", lambda: None)  # sem IA: classificador por regras
    usuario = criar_usuario("Conta Lote")
    yield usuario
    db = SessionLocal()
    for modelo in (DocumentoEmpresa, ArquivoCofre):
        db.query(modelo).filter(modelo.user_id == usuario.id).delete(synchronize_session=False)
    db.commit()
    db.close()


@pytest.fixture
def analises(monkeypatch):
    """Conta quantos arquivos chegaram à análise (Gemini/regras)."""
    chamados = []
    original = document_ai.analisar_documento

    async def espiao(conteudo, mime, nome, hoje=None):
        chamados.append(nome)
        return await original(conteudo, mime, nome, hoje)

    monkeypatch.setattr(document_ai, "analisar_documento", espiao)
    return chamados


def _enviar(conta, *arquivos):
    return client.post("/api/documents/processar-lote", headers=conta.headers,
                       files=[("arquivos", a) for a in arquivos])


def test_vazio_pasta_e_formato_nao_aceito_viram_erro_sem_ir_para_a_analise(conta, analises):
    resp = _enviar(conta,
                   ("certidao_vazia.pdf", b"", "application/pdf"),
                   ("Certidoes 2026", b"", "application/octet-stream"),  # pasta arrastada: sem extensão e 0 bytes
                   ("planilha.xlsx", b"PK\x03\x04conteudo", "application/vnd.ms-excel"),
                   ("falso.pdf", b"isto nao e um pdf", "application/pdf"),
                   ("atestado.pdf", _pdf(), "application/pdf"))
    assert resp.status_code == 200, resp.text
    corpo = resp.json()
    por_arquivo = {r["arquivo"]: r for r in corpo["resultados"]}
    assert corpo["total"] == 5 and corpo["concluidos"] == 1
    assert "vazio" in por_arquivo["certidao_vazia.pdf"]["erro"]
    assert "pastas não podem ser enviadas" in por_arquivo["Certidoes 2026"]["erro"]
    assert "não é um arquivo aceito" in por_arquivo["planilha.xlsx"]["erro"]
    assert "Formato não aceito" in por_arquivo["falso.pdf"]["erro"]
    assert por_arquivo["atestado.pdf"]["status"] == "concluido"
    assert analises == ["atestado.pdf"]  # só o arquivo válido chegou à IA


def test_falha_de_leitura_de_um_arquivo_nao_derruba_o_lote(conta, monkeypatch):
    leitura_original = UploadFile.read

    async def leitura(self, size=-1):
        if self.filename == "quebrado.pdf":
            raise ConnectionResetError("upload interrompido")
        return await leitura_original(self, size)

    monkeypatch.setattr(UploadFile, "read", leitura)
    corpo = _enviar(conta, ("quebrado.pdf", _pdf(), "application/pdf"),
                    ("atestado.pdf", _pdf(), "application/pdf")).json()
    por_arquivo = {r["arquivo"]: r for r in corpo["resultados"]}
    assert por_arquivo["quebrado.pdf"] == {"arquivo": "quebrado.pdf", "status": "erro",
                                           "erro": "Não foi possível ler o arquivo enviado."}
    assert por_arquivo["atestado.pdf"]["status"] == "concluido"


def test_analise_que_estoura_o_tempo_vira_erro_so_daquele_arquivo(conta, monkeypatch):
    original = document_ai.analisar_documento

    async def analise(conteudo, mime, nome, hoje=None):
        if nome == "lento.pdf":
            await asyncio.sleep(5)
        return await original(conteudo, mime, nome, hoje)

    monkeypatch.setattr(document_ai, "analisar_documento", analise)
    monkeypatch.setattr(rotas_documentos, "TEMPO_MAX_ANALISE_SEGUNDOS", 0.2)
    corpo = _enviar(conta, ("lento.pdf", _pdf(), "application/pdf"), ("atestado.pdf", _pdf(), "application/pdf")).json()
    por_arquivo = {r["arquivo"]: r for r in corpo["resultados"]}
    assert por_arquivo["lento.pdf"]["status"] == "erro" and "demorou demais" in por_arquivo["lento.pdf"]["erro"]
    assert por_arquivo["atestado.pdf"]["status"] == "concluido"


def test_zona_de_upload_filtra_pastas_e_formatos_no_navegador():
    html = client.get("/documentos").text
    for marcador in ("Atenção: pastas não podem ser enviadas diretamente. Arraste apenas os arquivos contidos nela.",
                     "webkitGetAsEntry", "entrada.isDirectory", 'id="aviso-lote"', "EXTENSOES_LOTE",
                     "processarLote(arquivosDoDrop(e))", "Falha de conexão ao enviar o arquivo"):
        assert marcador in html, marcador
