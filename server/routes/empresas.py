"""Empresas da conta (multi-CNPJ): cadastro pela Receita, empresa ativa e remoção."""
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from server.database import get_db
from server.routes.documents import _dono, erro_de_consulta
from server.services import dossie, empresas

router = APIRouter()


def _conta(request: Request) -> str:
    user_id = _dono(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Entre na sua conta para gerenciar as empresas.")
    return user_id


@router.get("/api/minhas-empresas")
def api_listar_empresas(request: Request, db: Session = Depends(get_db)):
    return empresas.da_sessao(db, _conta(request))


@router.post("/api/minhas-empresas", status_code=201)
def api_cadastrar_empresa(request: Request, payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    """Consulta o CNPJ na Receita e grava na conta; a primeira empresa vira a ativa automaticamente."""
    user_id = _conta(request)
    try:
        empresa = empresas.cadastrar_por_cnpj(db, user_id, str(payload.get("cnpj") or ""))
    except (dossie.CnpjInvalidoError, dossie.CnpjNaoEncontradoError, dossie.FonteIndisponivelError) as exc:
        raise erro_de_consulta(exc)
    return {"empresa": empresas.serializar(empresa), **empresas.da_sessao(db, user_id)}


@router.post("/api/minhas-empresas/{empresa_id}/ativar")
def api_ativar_empresa(empresa_id: int, request: Request, db: Session = Depends(get_db)):
    user_id = _conta(request)
    try:
        empresas.ativar(db, user_id, empresa_id)
    except empresas.EmpresaNaoEncontrada as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return empresas.da_sessao(db, user_id)


@router.delete("/api/minhas-empresas/{empresa_id}")
def api_remover_empresa(empresa_id: int, request: Request, db: Session = Depends(get_db)):
    user_id = _conta(request)
    try:
        empresas.remover(db, user_id, empresa_id)
    except empresas.EmpresaNaoEncontrada as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return empresas.da_sessao(db, user_id)
