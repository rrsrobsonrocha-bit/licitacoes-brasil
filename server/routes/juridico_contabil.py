"""Jurídico: Balanço Patrimonial & Análise de Índices Contábeis (Lei 14.133/2021, art. 69)."""
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from server.database import get_db
from server.models import Deal
from server.routes.documents import _dono
from server.services import empresas, indices_contabeis
from server.services.minutas import MIME_DOCX

router = APIRouter()


@router.post("/api/juridico/indices-contabeis")
def api_calcular_indices(payload: Dict[str, Any] = Body(...)):
    """LG, SG e LC a partir dos valores do balanço, com diagnóstico e checagens de consistência."""
    try:
        return indices_contabeis.calcular(payload)
    except indices_contabeis.ValoresInvalidosError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/api/juridico/indices-contabeis/memorial")
def api_memorial_indices(request: Request, payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    """Declaração e Memorial de Cálculo de Índices Contábeis em DOCX, timbrado com a empresa ativa."""
    user_id = _dono(request)
    empresa = empresas.empresa_ativa(db, user_id) if user_id else None
    try:
        conteudo = indices_contabeis.gerar_memorial_docx(
            payload, empresa=empresas.dados_licitante(empresa) or None, timbre=empresas.timbre(empresa))
    except indices_contabeis.ValoresInvalidosError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    exercicio = "".join(c for c in str(payload.get("exercicio") or "") if c.isalnum())[:8]
    nome = f"memorial-indices-contabeis{('-' + exercicio) if exercicio else ''}.docx"
    return Response(content=conteudo, media_type=MIME_DOCX,
                    headers={"Content-Disposition": f'attachment; filename="{nome}"', "Cache-Control": "no-store"})


@router.get("/api/juridico/indices-contabeis/referencia")
def api_referencia_faturamento(request: Request, db: Session = Depends(get_db)):
    """
    Referência (não é balanço): valor dos negócios homologados/ganhos no funil da conta. Serve de
    contexto para o operador; os valores do balanço vêm sempre da contabilidade da empresa.
    """
    user_id = _dono(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Entre na sua conta.")
    total, quantidade = db.query(func.coalesce(func.sum(Deal.valor_negocio), 0), func.count(Deal.id)).filter(
        Deal.user_id == user_id, Deal.stage == "homologado").one()
    empresa = empresas.empresa_ativa(db, user_id)
    return {
        "faturamento_homologado": round(float(total or 0), 2),
        "negocios_homologados": int(quantidade or 0),
        "empresa": empresas.serializar(empresa) if empresa else None,
    }
