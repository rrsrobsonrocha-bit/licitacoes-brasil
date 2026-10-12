"""
Cofre de Documentos de Habilitação — armazenamento em disco, validação de upload
e serialização com auditoria de validade (verde / amarelo / vermelho).
"""
import os
import re
import uuid
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

import hashlib

from server.models import (
    ArquivoCofre,
    DocumentoEmpresa,
    DOCUMENTO_CATEGORIAS,
    DOCUMENTO_DIAS_ALERTA,
    hoje_sao_paulo,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DOCUMENTOS_UPLOAD_DIR = Path(
    os.getenv("DOCUMENTOS_UPLOAD_DIR", str(PROJECT_ROOT / "uploads" / "documentos"))
).resolve()
DOCUMENTO_MAX_BYTES = int(os.getenv("DOCUMENTO_MAX_BYTES", str(15 * 1024 * 1024)))  # 15 MB
PDF_MAGIC = b"%PDF-"
PREFIXO_BANCO = "db:"
# Formatos aceitos no upload em lote (PDF e imagens de documentos digitalizados), pela assinatura do arquivo
ASSINATURAS = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"RIFF", "image/webp"),
)
EXTENSOES = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


class DocumentoInvalidoError(ValueError):
    """Erro de validação de metadados ou do arquivo enviado ao cofre."""


def slugify(valor: str, max_len: int = 60) -> str:
    """Converte texto livre em identificador ASCII seguro para nomes de arquivo."""
    base = unicodedata.normalize("NFKD", valor or "")
    ascii_txt = base.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", ascii_txt).strip("_")
    return (slug[:max_len].rstrip("_")) or "documento"


def parse_data_iso(valor: Optional[str], campo: str, obrigatorio: bool = False) -> Optional[date]:
    """Converte 'AAAA-MM-DD' (ou 'DD/MM/AAAA') em date, com mensagens de erro em português."""
    texto = (valor or "").strip()
    if not texto:
        if obrigatorio:
            raise DocumentoInvalidoError(f"O campo '{campo}' é obrigatório.")
        return None
    for formato in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            continue
    raise DocumentoInvalidoError(f"Data inválida em '{campo}': use o formato AAAA-MM-DD.")


def normalizar_tags(tags: Optional[str]) -> Optional[str]:
    if not tags:
        return None
    itens: List[str] = []
    for bruto in re.split(r"[,;\n]", tags):
        tag = bruto.strip()
        if tag and tag.lower() not in {t.lower() for t in itens}:
            itens.append(tag[:60])
    return ", ".join(itens)[:500] or None


def validar_metadados(
    nome: str,
    categoria: str,
    data_emissao: Optional[str],
    data_validade: Optional[str],
) -> Dict[str, Any]:
    nome_limpo = (nome or "").strip()
    if not nome_limpo:
        raise DocumentoInvalidoError("Informe o nome do documento.")
    if len(nome_limpo) > 255:
        raise DocumentoInvalidoError("O nome do documento deve ter no máximo 255 caracteres.")
    cat = (categoria or "").strip().lower()
    if cat not in DOCUMENTO_CATEGORIAS:
        raise DocumentoInvalidoError(
            "Categoria inválida. Opções: " + ", ".join(DOCUMENTO_CATEGORIAS.keys())
        )
    emissao = parse_data_iso(data_emissao, "data_emissao", obrigatorio=True)
    validade = parse_data_iso(data_validade, "data_validade")
    if validade and emissao and validade < emissao:
        raise DocumentoInvalidoError("A data de validade não pode ser anterior à data de emissão.")
    return {"nome": nome_limpo, "categoria": cat, "data_emissao": emissao, "data_validade": validade}


def validar_conteudo_pdf(nome_arquivo: Optional[str], conteudo: bytes) -> None:
    if not conteudo:
        raise DocumentoInvalidoError("O arquivo enviado está vazio.")
    if len(conteudo) > DOCUMENTO_MAX_BYTES:
        limite_mb = DOCUMENTO_MAX_BYTES // (1024 * 1024)
        raise DocumentoInvalidoError(f"O arquivo excede o limite de {limite_mb} MB.")
    if not (nome_arquivo or "").lower().endswith(".pdf"):
        raise DocumentoInvalidoError("Apenas arquivos PDF (.pdf) são aceitos no cofre.")
    if not conteudo.startswith(PDF_MAGIC):
        raise DocumentoInvalidoError("O arquivo não é um PDF válido (assinatura %PDF ausente).")


def detectar_mime(nome_arquivo: Optional[str], conteudo: bytes) -> str:
    """MIME pela assinatura do arquivo (PDF, PNG, JPEG, WEBP); DocumentoInvalidoError se não for aceito."""
    if not conteudo:
        raise DocumentoInvalidoError("O arquivo enviado está vazio.")
    if len(conteudo) > DOCUMENTO_MAX_BYTES:
        raise DocumentoInvalidoError(f"O arquivo excede o limite de {DOCUMENTO_MAX_BYTES // (1024 * 1024)} MB.")
    for assinatura, mime in ASSINATURAS:
        if conteudo.startswith(assinatura) and (mime != "image/webp" or conteudo[8:12] == b"WEBP"):
            return mime
    raise DocumentoInvalidoError(f"Formato não aceito em '{nome_arquivo or 'arquivo'}': envie PDF, PNG, JPG ou WEBP.")


def salvar_arquivo_no_banco(db: Session, conteudo: bytes, nome_documento: str, user_id: Optional[str],
                            mime_type: str = "application/pdf") -> str:
    """Guarda o arquivo no banco (persistente entre deploys) e devolve a referência "db:<id>"."""
    arquivo = ArquivoCofre(
        user_id=user_id,
        nome_original=f"{slugify(nome_documento, 80)}{EXTENSOES.get(mime_type, '')}",
        mime_type=mime_type,
        tamanho=len(conteudo),
        sha256=hashlib.sha256(conteudo).hexdigest(),
        conteudo=conteudo,
    )
    db.add(arquivo)
    db.flush()
    return f"{PREFIXO_BANCO}{arquivo.id}"


def _id_no_banco(caminho_arquivo: Optional[str]) -> Optional[int]:
    if caminho_arquivo and caminho_arquivo.startswith(PREFIXO_BANCO):
        try:
            return int(caminho_arquivo[len(PREFIXO_BANCO):])
        except ValueError:
            return None
    return None


def ler_arquivo_documento(db: Session, doc: DocumentoEmpresa) -> Optional[Tuple[bytes, str]]:
    """(conteúdo, mime) do arquivo do documento: do banco ou, para documentos antigos, do disco."""
    arquivo_id = _id_no_banco(doc.caminho_arquivo)
    if arquivo_id is not None:
        arquivo = db.get(ArquivoCofre, arquivo_id)
        return (arquivo.conteudo, arquivo.mime_type) if arquivo else None
    caminho = resolver_caminho_documento(doc.caminho_arquivo)
    if caminho and caminho.is_file():
        return caminho.read_bytes(), "application/pdf"
    return None


def salvar_pdf_no_cofre(conteudo: bytes, nome_documento: str) -> str:
    """(Legado) Grava o PDF em disco; o cofre usa ``salvar_arquivo_no_banco``, que sobrevive a deploys."""
    destino_dir = Path(DOCUMENTOS_UPLOAD_DIR)
    destino_dir.mkdir(parents=True, exist_ok=True)
    nome_fisico = f"{datetime.utcnow():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:12]}_{slugify(nome_documento, 40)}.pdf"
    destino = destino_dir / nome_fisico
    with open(destino, "wb") as fh:
        fh.write(conteudo)
    try:
        return destino.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(destino.resolve())


def resolver_caminho_documento(caminho_arquivo: Optional[str]) -> Optional[Path]:
    """Resolve o caminho persistido em disco (relativo à raiz do projeto ou absoluto); None para "db:"."""
    if not caminho_arquivo or caminho_arquivo.startswith(PREFIXO_BANCO):
        return None
    caminho = Path(caminho_arquivo)
    if not caminho.is_absolute():
        caminho = PROJECT_ROOT / caminho
    return caminho.resolve()


def remover_arquivo_documento(caminho_arquivo: Optional[str], db: Optional[Session] = None) -> bool:
    """Remove o arquivo do banco ("db:<id>") ou o arquivo físico (só dentro do diretório do cofre)."""
    arquivo_id = _id_no_banco(caminho_arquivo)
    if arquivo_id is not None:
        if db is None:
            return False
        return db.query(ArquivoCofre).filter(ArquivoCofre.id == arquivo_id).delete(synchronize_session=False) > 0
    caminho = resolver_caminho_documento(caminho_arquivo)
    if not caminho or not caminho.is_file():
        return False
    raiz = Path(DOCUMENTOS_UPLOAD_DIR).resolve()
    if raiz != caminho.parent and raiz not in caminho.parents:
        return False
    caminho.unlink()
    return True


def consulta_do_cofre(db: Session, user_id: Optional[str], cnpj: Optional[str] = None):
    """
    Documentos do cofre da conta (privado por usuário); sem conta, só os legados sem dono.
    Com ``cnpj`` (conta com várias empresas), só os da empresa ativa e os sem CNPJ definido.
    """
    from sqlalchemy import or_
    consulta = db.query(DocumentoEmpresa)
    if user_id is None:
        return consulta.filter(DocumentoEmpresa.user_id.is_(None))
    consulta = consulta.filter(DocumentoEmpresa.user_id == user_id)
    if cnpj:
        consulta = consulta.filter(or_(DocumentoEmpresa.cnpj == cnpj, DocumentoEmpresa.cnpj.is_(None)))
    return consulta


def cnpj_do_contexto(db: Session, user_id: Optional[str]) -> Optional[str]:
    """CNPJ da empresa ativa quando a conta tem 2+ empresas (com uma só, o cofre mostra tudo)."""
    if not user_id:
        return None
    from server.models import EmpresaUsuario
    if db.query(EmpresaUsuario).filter(EmpresaUsuario.user_id == user_id).count() < 2:
        return None
    from server.services.empresas import empresa_ativa
    ativa = empresa_ativa(db, user_id)
    return ativa.cnpj if ativa else None


def documentos_ordenados(db: Session, user_id: Optional[str], categoria: Optional[str] = None) -> List[DocumentoEmpresa]:
    consulta = consulta_do_cofre(db, user_id, cnpj_do_contexto(db, user_id))
    if categoria:
        consulta = consulta.filter(DocumentoEmpresa.categoria == categoria.lower())
    return consulta.order_by(
        DocumentoEmpresa.pendente.desc(),
        DocumentoEmpresa.data_validade.is_(None),
        DocumentoEmpresa.data_validade.asc(),
        DocumentoEmpresa.nome.asc(),
    ).all()


def arquivo_disponivel(doc: DocumentoEmpresa) -> bool:
    if _id_no_banco(doc.caminho_arquivo) is not None:
        return True  # conteúdo no banco (removido junto com o documento)
    caminho = resolver_caminho_documento(doc.caminho_arquivo)
    return bool(caminho and caminho.is_file())


def serializar_documento(doc: DocumentoEmpresa, hoje: Optional[date] = None) -> Dict[str, Any]:
    ref = hoje or hoje_sao_paulo()
    pendente = bool(doc.pendente)
    metadados = doc.metadados_ia if isinstance(doc.metadados_ia, dict) else None
    # Documento lido por IA sem data de emissão identificada: não exibe a data do upload como emissão
    emissao_conhecida = not (metadados and metadados.get("data_emissao") is None)
    return {
        "id": doc.id,
        "nome": doc.nome,
        "categoria": doc.categoria,
        "categoria_label": doc.categoria_label,
        "pendente": pendente,
        "origem": doc.origem or "MANUAL",
        "cnpj": doc.cnpj,
        "link_oficial": doc.link_oficial,
        "validade_padrao_dias": doc.validade_padrao_dias,
        # pendente: ainda não emitido (a data gravada é só a do registro)
        "data_emissao": doc.data_emissao.isoformat() if doc.data_emissao and not pendente and emissao_conhecida else None,
        "data_validade": doc.data_validade.isoformat() if doc.data_validade else None,
        "tags_habilitacao": doc.tags_habilitacao or "",
        "tags": doc.tags,
        "dias_restantes": doc.dias_restantes(ref),
        "status": doc.status_validade(ref),
        "arquivo_disponivel": arquivo_disponivel(doc),
        "arquivo_url": f"/api/documentos/{doc.id}/arquivo",
        "tipo_documento": doc.tipo_documento,
        "metadados_ia": metadados,
        "alertas": (metadados or {}).get("alertas", []),
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


def resumo_validade(documentos: List[Dict[str, Any]]) -> Dict[str, int]:
    resumo = {"total": len(documentos), "validos": 0, "a_vencer": 0, "vencidos": 0, "sem_validade": 0, "pendentes": 0}
    for d in documentos:
        if d["status"] == "PENDENTE":
            resumo["pendentes"] += 1
        elif d["status"] == "VALIDO":
            resumo["validos"] += 1
        elif d["status"] == "A_VENCER":
            resumo["a_vencer"] += 1
        elif d["status"] == "VENCIDO":
            resumo["vencidos"] += 1
        else:
            resumo["sem_validade"] += 1
    return resumo


__all__ = [
    "arquivo_disponivel",
    "detectar_mime",
    "ler_arquivo_documento",
    "salvar_arquivo_no_banco",
    "consulta_do_cofre",
    "documentos_ordenados",
    "DOCUMENTOS_UPLOAD_DIR",
    "DOCUMENTO_DIAS_ALERTA",
    "DocumentoInvalidoError",
    "normalizar_tags",
    "remover_arquivo_documento",
    "resolver_caminho_documento",
    "resumo_validade",
    "salvar_pdf_no_cofre",
    "serializar_documento",
    "slugify",
    "validar_conteudo_pdf",
    "validar_metadados",
]
