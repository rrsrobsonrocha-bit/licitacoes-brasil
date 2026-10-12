"""
Empresas da conta (multi-CNPJ) e empresa ativa (Licitações Brasil).

- Cadastro a partir da Receita Federal (BrasilAPI): razão social, endereço, contato, CNAE, porte e
  representante legal (administrador do QSA). Nada é inventado: campo ausente na Receita fica vazio.
- Empresa ativa: com uma empresa só, ela é a ativa automaticamente; com várias, a escolhida no seletor.
- ``timbre`` e ``dados_licitante`` alimentam o papel timbrado e a qualificação dos documentos gerados.
"""
import re
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from server.models import EmpresaUsuario
from server.services import dossie

CARGOS_REPRESENTANTE = ("administrador", "diretor", "presidente", "titular", "socio-administrador", "sócio-administrador")


class EmpresaNaoEncontrada(LookupError):
    pass


def formatar_cnpj(cnpj: Optional[str]) -> str:
    return dossie.formatar_cnpj(cnpj or "")


def formatar_cep(cep: Optional[str]) -> str:
    d = re.sub(r"\D", "", cep or "")
    return f"{d[:5]}-{d[5:]}" if len(d) == 8 else d


def _telefone(bruto: Optional[str]) -> Optional[str]:
    d = re.sub(r"\D", "", bruto or "")
    if len(d) in (10, 11):
        return f"({d[:2]}) {d[2:-4]}-{d[-4:]}"
    return d or None


def _representante(qsa: List[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """Representante legal: o primeiro administrador/diretor do QSA; sem um deles, o primeiro sócio."""
    for socio in qsa or []:
        qualificacao = (socio.get("qualificacao") or "").lower()
        if any(c in qualificacao for c in CARGOS_REPRESENTANTE):
            return {"nome": socio.get("nome") or None, "cargo": socio.get("qualificacao") or None}
    if qsa:
        return {"nome": qsa[0].get("nome") or None, "cargo": qsa[0].get("qualificacao") or None}
    return {"nome": None, "cargo": None}


def endereco_da_receita(receita: Dict[str, Any]) -> str:
    end = receita.get("endereco") or {}
    rua = ", ".join(p for p in (end.get("logradouro"), end.get("numero"), end.get("complemento"), end.get("bairro")) if p)
    cidade = "/".join(p for p in (end.get("municipio"), end.get("uf")) if p)
    partes = [p for p in (rua, cidade, f"CEP {formatar_cep(end.get('cep'))}" if end.get("cep") else "") if p]
    return " – ".join(partes)


def listar(db: Session, user_id: str) -> List[EmpresaUsuario]:
    return (db.query(EmpresaUsuario).filter(EmpresaUsuario.user_id == user_id)
            .order_by(EmpresaUsuario.is_primary.desc(), EmpresaUsuario.razao_social.asc()).all())


def empresa_ativa(db: Session, user_id: Optional[str]) -> Optional[EmpresaUsuario]:
    """A empresa ativa da conta; com uma única empresa, ela é marcada como ativa automaticamente."""
    if not user_id:
        return None
    empresas = listar(db, user_id)
    if not empresas:
        return None
    ativa = next((e for e in empresas if e.is_primary), None)
    if ativa is None:
        ativa = max(empresas, key=lambda e: e.created_at)  # garante sempre uma ativa
        ativa.is_primary = True
        db.commit()
    return ativa


def registrar_da_receita(db: Session, user_id: str, receita: Dict[str, Any]) -> EmpresaUsuario:
    """Cria ou atualiza a empresa da conta com os dados da Receita. A primeira empresa vira a ativa."""
    cnpj = receita["cnpj"]
    empresa = db.query(EmpresaUsuario).filter(EmpresaUsuario.user_id == user_id, EmpresaUsuario.cnpj == cnpj).first()
    if empresa is None:
        empresa = EmpresaUsuario(user_id=user_id, cnpj=cnpj)
        empresa.is_primary = db.query(EmpresaUsuario).filter(EmpresaUsuario.user_id == user_id).count() == 0
        db.add(empresa)
    end = receita.get("endereco") or {}
    representante = _representante(receita.get("qsa") or [])
    cnae = receita.get("cnae_principal") or {}
    empresa.razao_social = (receita.get("razao_social") or cnpj)[:255]
    empresa.nome_fantasia = (receita.get("nome_fantasia") or None)
    empresa.endereco_completo = endereco_da_receita(receita)[:500] or None
    empresa.cidade = end.get("municipio") or None
    empresa.uf = (end.get("uf") or None)
    empresa.cep = re.sub(r"\D", "", end.get("cep") or "")[:8] or None
    empresa.telefone = _telefone(receita.get("telefone"))
    empresa.email = (receita.get("email") or None)
    empresa.cnae_principal = " – ".join(p for p in (cnae.get("codigo"), cnae.get("descricao")) if p)[:255] or None
    empresa.porte = receita.get("porte") or None
    empresa.representante_nome = representante["nome"]
    empresa.representante_cargo = representante["cargo"]
    db.flush()
    return empresa


def cadastrar_por_cnpj(db: Session, user_id: str, cnpj: str) -> EmpresaUsuario:
    """Consulta a Receita (BrasilAPI) e grava a empresa na conta. Erros de CNPJ/fonte sobem (dossie.*Error)."""
    empresa = registrar_da_receita(db, user_id, dossie.consultar_receita(cnpj))
    db.commit()
    db.refresh(empresa)
    return empresa


def _da_conta(db: Session, user_id: str, empresa_id: int) -> EmpresaUsuario:
    empresa = db.query(EmpresaUsuario).filter(EmpresaUsuario.id == empresa_id, EmpresaUsuario.user_id == user_id).first()
    if not empresa:
        raise EmpresaNaoEncontrada("Empresa não encontrada nesta conta.")
    return empresa


def ativar(db: Session, user_id: str, empresa_id: int) -> EmpresaUsuario:
    empresa = _da_conta(db, user_id, empresa_id)
    db.query(EmpresaUsuario).filter(EmpresaUsuario.user_id == user_id, EmpresaUsuario.id != empresa.id).update(
        {EmpresaUsuario.is_primary: False}, synchronize_session=False)
    empresa.is_primary = True
    db.commit()
    db.refresh(empresa)
    return empresa


def remover(db: Session, user_id: str, empresa_id: int) -> None:
    empresa = _da_conta(db, user_id, empresa_id)
    era_ativa = empresa.is_primary
    db.delete(empresa)
    db.commit()
    if era_ativa:
        empresa_ativa(db, user_id)  # outra empresa da conta assume


def serializar(empresa: EmpresaUsuario) -> Dict[str, Any]:
    return {
        "id": empresa.id,
        "cnpj": empresa.cnpj,
        "cnpj_formatado": formatar_cnpj(empresa.cnpj),
        "razao_social": empresa.razao_social,
        "nome_fantasia": empresa.nome_fantasia,
        "endereco_completo": empresa.endereco_completo,
        "cidade": empresa.cidade,
        "uf": empresa.uf,
        "telefone": empresa.telefone,
        "email": empresa.email,
        "cnae_principal": empresa.cnae_principal,
        "porte": empresa.porte,
        "representante_nome": empresa.representante_nome,
        "representante_cargo": empresa.representante_cargo,
        "is_primary": bool(empresa.is_primary),
    }


def timbre(empresa: Optional[EmpresaUsuario]) -> Optional[Dict[str, str]]:
    """Papel timbrado: nome, linha de CNPJ/endereço e linha de contato (só com o que a Receita informou)."""
    if empresa is None:
        return None
    nome = empresa.razao_social
    if empresa.nome_fantasia and empresa.nome_fantasia.lower() not in nome.lower():
        nome = f"{nome} ({empresa.nome_fantasia})"
    linha_cnpj = " · ".join(p for p in (f"CNPJ: {formatar_cnpj(empresa.cnpj)}",
                                         f"Endereço: {empresa.endereco_completo}" if empresa.endereco_completo else "") if p)
    contato = " · ".join(p for p in (f"E-mail: {empresa.email}" if empresa.email else "",
                                      f"Telefone: {empresa.telefone}" if empresa.telefone else "") if p)
    return {"nome": nome, "linha_cnpj": linha_cnpj, "contato": contato}


def dados_licitante(empresa: Optional[EmpresaUsuario]) -> Dict[str, str]:
    """Qualificação do licitante para petições e minutas (CPF/RG do representante não são armazenados)."""
    if empresa is None:
        return {}
    return {
        "razao_social": empresa.razao_social,
        "cnpj": empresa.cnpj,
        "endereco": empresa.endereco_completo or "",
        "representante": empresa.representante_nome or "",
        "cargo": empresa.representante_cargo or "",
        "email": empresa.email or "",
        "telefone": empresa.telefone or "",
        "cidade_uf": "/".join(p for p in (empresa.cidade, empresa.uf) if p),
    }


def da_sessao(db: Session, user_id: Optional[str]) -> Dict[str, Any]:
    """Empresas da conta e a ativa, para o seletor do cabeçalho."""
    if not user_id:
        return {"lista": [], "ativa": None}
    ativa = empresa_ativa(db, user_id)
    return {"lista": [serializar(e) for e in listar(db, user_id)], "ativa": serializar(ativa) if ativa else None}
