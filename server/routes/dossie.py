"""Dossiê do Fornecedor: consulta 360° de um CNPJ e busca de fornecedores por nome."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from server.database import get_db
from server.routes.documents import erro_de_consulta
from server.services import dossie

router = APIRouter()


@router.get("/api/fornecedores/dossie/{cnpj}")
def api_dossie_fornecedor(cnpj: str, atualizar: bool = Query(False), db: Session = Depends(get_db)):
    """Cadastro e QSA (Receita), contratos no PNCP, sanções e selo de aptidão; cache de 24 h."""
    try:
        return dossie.obter_dossie(db, cnpj, atualizar=atualizar)
    except (dossie.CnpjInvalidoError, dossie.CnpjNaoEncontradoError, dossie.FonteIndisponivelError) as exc:
        raise erro_de_consulta(exc)


@router.get("/api/fornecedores/busca")
def api_buscar_fornecedor(q: str = Query("", max_length=120)):
    """CNPJ completo devolve o próprio CNPJ; nome devolve empresas fornecedoras em contratos do PNCP."""
    termo = (q or "").strip()
    digitos = dossie.somente_digitos(termo)
    if len(digitos) == 14:
        if not dossie.cnpj_valido(digitos):
            raise HTTPException(status_code=422, detail="CNPJ inválido: confira os 14 dígitos.")
        return {"resultados": [{"cnpj": digitos, "cnpj_formatado": dossie.formatar_cnpj(digitos), "nome": ""}]}
    if len(termo) < 3:
        raise HTTPException(status_code=422, detail="Digite o CNPJ ou pelo menos 3 letras da razão social.")
    try:
        return {"resultados": dossie.buscar_fornecedores_por_nome(termo)}
    except dossie.FonteIndisponivelError as exc:
        raise erro_de_consulta(exc)
