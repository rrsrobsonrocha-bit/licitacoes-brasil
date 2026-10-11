"""
Cofre de Documentos: importação automática por CNPJ e anexação do PDF às certidões pendentes.
As demais rotas do cofre (listar, enviar, baixar, excluir) ficam em server/main.py.
"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from server.database import get_db
from server.models import DocumentoEmpresa
from server.services import certidoes, document_vault, dossie

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
    doc.caminho_arquivo = document_vault.salvar_pdf_no_cofre(conteudo, doc.nome)
    doc.data_emissao = emissao
    doc.data_validade = validade
    doc.pendente = False
    doc.updated_at = datetime.utcnow()
    db.commit()
    if anterior and anterior != doc.caminho_arquivo:
        document_vault.remover_arquivo_documento(anterior)
    db.refresh(doc)
    return document_vault.serializar_documento(doc)
