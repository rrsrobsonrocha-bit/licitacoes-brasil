"""
Cofre de Documentos: importação automática por CNPJ e anexação do PDF às certidões pendentes.
As demais rotas do cofre (listar, enviar, baixar, excluir) ficam em server/main.py.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from server.database import get_db
from server.models import DocumentoEmpresa
from server.services import certidoes, document_ai, document_vault, dossie

logger = logging.getLogger(__name__)
MAX_ARQUIVOS_POR_LOTE = 20
ANALISES_SIMULTANEAS = 3

router = APIRouter()


def _dono(request: Request) -> Optional[str]:
    """Conta da sessão (resolvida pelo middleware de acesso); o cofre é privado por conta."""
    usuario = getattr(request.state, "usuario", None)
    if usuario is None or usuario.motivo_bloqueio():
        return None
    return usuario.id


def erro_de_consulta(exc: Exception) -> HTTPException:
    if isinstance(exc, dossie.CnpjInvalidoError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, dossie.CnpjNaoEncontradoError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=503, detail=f"Consulta pública indisponível no momento: {exc}")


@router.post("/api/documents/auto-import/{cnpj}")
@router.post("/api/documentos/auto-import/{cnpj}")
def api_importar_certidoes_por_cnpj(cnpj: str, request: Request, db: Session = Depends(get_db)):
    """
    Monta no cofre o pacote de habilitação do CNPJ (arts. 62 a 70 da Lei 14.133/2021): o que foi
    consultado em fonte oficial entra válido, com PDF; as certidões que exigem emissão no site
    oficial entram pendentes, com o link de emissão.
    """
    try:
        return certidoes.importar_por_cnpj(db, cnpj, _dono(request))
    except (dossie.CnpjInvalidoError, dossie.CnpjNaoEncontradoError, dossie.FonteIndisponivelError) as exc:
        raise erro_de_consulta(exc)


@router.post("/api/documentos/{documento_id}/anexar")
async def api_anexar_pdf_documento(
    documento_id: int,
    request: Request,
    arquivo: UploadFile = File(...),
    data_emissao: str = Form(...),
    data_validade: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Anexa o PDF emitido a um documento do cofre (ex.: certidão pendente) e atualiza a validade."""
    doc = document_vault.consulta_do_cofre(db, _dono(request)).filter(DocumentoEmpresa.id == documento_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Documento não encontrado.")
    try:
        emissao = document_vault.parse_data_iso(data_emissao, "data_emissao", obrigatorio=True)
        validade = document_vault.parse_data_iso(data_validade, "data_validade")
        if validade is None and doc.validade_padrao_dias:
            validade = emissao + timedelta(days=doc.validade_padrao_dias)
        if validade and validade < emissao:
            raise document_vault.DocumentoInvalidoError("A data de validade não pode ser anterior à data de emissão.")
        conteudo = await arquivo.read(document_vault.DOCUMENTO_MAX_BYTES + 1)
        document_vault.validar_conteudo_pdf(arquivo.filename, conteudo)
    except document_vault.DocumentoInvalidoError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    finally:
        await arquivo.close()

    anterior = doc.caminho_arquivo
    doc.caminho_arquivo = document_vault.salvar_arquivo_no_banco(db, conteudo, doc.nome, doc.user_id)
    doc.data_emissao = emissao
    doc.data_validade = validade
    doc.pendente = False
    doc.updated_at = datetime.utcnow()
    if anterior and anterior != doc.caminho_arquivo:
        document_vault.remover_arquivo_documento(anterior, db)
    db.commit()
    db.refresh(doc)
    return document_vault.serializar_documento(doc)


@router.post("/api/documents/processar-lote")
@router.post("/api/documentos/processar-lote")
async def api_processar_lote(request: Request, arquivos: List[UploadFile] = File(...),
                             db: Session = Depends(get_db)):
    """
    Upload em lote com classificação automática (Gemini Flash; sem ele, regras): cada arquivo é
    analisado, conferido contra o CNPJ da conta e gravado no Cofre da conta logada, atualizando o slot
    do mesmo tipo (pendente/vencido/versão anterior) ou criando um documento novo. Falha em um arquivo
    não interrompe os demais.
    """
    user_id = _dono(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Entre na sua conta para enviar documentos ao Cofre.")
    if not arquivos:
        raise HTTPException(status_code=422, detail="Envie ao menos um arquivo.")
    if len(arquivos) > MAX_ARQUIVOS_POR_LOTE:
        raise HTTPException(status_code=422, detail=f"Envie no máximo {MAX_ARQUIVOS_POR_LOTE} arquivos por lote.")

    lidos: List[Dict[str, Any]] = []
    for arquivo in arquivos:
        nome = (arquivo.filename or "documento")[:255]
        try:
            conteudo = await arquivo.read(document_vault.DOCUMENTO_MAX_BYTES + 1)
            mime = document_vault.detectar_mime(nome, conteudo)
            lidos.append({"arquivo": nome, "conteudo": conteudo, "mime": mime})
        except document_vault.DocumentoInvalidoError as exc:
            lidos.append({"arquivo": nome, "erro": str(exc)})
        finally:
            await arquivo.close()

    # Análise em paralelo (sem banco); gravação em seguida, um arquivo por vez
    limite = asyncio.Semaphore(ANALISES_SIMULTANEAS)

    async def analisar(item: Dict[str, Any]) -> None:
        if "erro" in item:
            return
        async with limite:
            try:
                item["analise"] = await document_ai.analisar_documento(item["conteudo"], item["mime"], item["arquivo"])
            except Exception as exc:  # nunca derruba o lote
                logger.warning("Cofre: falha ao analisar %s: %r", item["arquivo"], exc)
                item["erro"] = "Não foi possível analisar o arquivo."

    await asyncio.gather(*(analisar(item) for item in lidos))

    resultados: List[Dict[str, Any]] = []
    for item in lidos:
        if "erro" in item:
            resultados.append({"arquivo": item["arquivo"], "status": "erro", "erro": item["erro"]})
            continue
        try:
            doc, acao = document_ai.gravar_no_cofre(db, user_id, item["conteudo"], item["mime"], item["arquivo"], item["analise"])
            db.commit()
            db.refresh(doc)
            resultados.append({
                "arquivo": item["arquivo"], "status": "concluido", "acao": acao,
                "documento": document_vault.serializar_documento(doc),
                "metadados": doc.metadados_ia, "alertas": (doc.metadados_ia or {}).get("alertas", []),
            })
        except Exception as exc:
            db.rollback()
            logger.warning("Cofre: falha ao gravar %s: %r", item["arquivo"], exc)
            resultados.append({"arquivo": item["arquivo"], "status": "erro", "erro": "Falha ao gravar no Cofre."})
    return {
        "total": len(resultados),
        "concluidos": sum(1 for r in resultados if r["status"] == "concluido"),
        "resultados": resultados,
    }
