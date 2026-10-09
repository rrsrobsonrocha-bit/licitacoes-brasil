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
from typing import Any, Dict, List, Optional

from server.models import (
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


def salvar_pdf_no_cofre(conteudo: bytes, nome_documento: str) -> str:
    """Grava o PDF em uploads/documentos com nome único e retorna o caminho a persistir."""
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
    """Resolve o caminho persistido (relativo à raiz do projeto ou absoluto)."""
    if not caminho_arquivo:
        return None
    caminho = Path(caminho_arquivo)
    if not caminho.is_absolute():
        caminho = PROJECT_ROOT / caminho
    return caminho.resolve()


def remover_arquivo_documento(caminho_arquivo: Optional[str]) -> bool:
    """Remove o arquivo físico apenas se estiver dentro do diretório do cofre."""
    caminho = resolver_caminho_documento(caminho_arquivo)
    if not caminho or not caminho.is_file():
        return False
    raiz = Path(DOCUMENTOS_UPLOAD_DIR).resolve()
    if raiz != caminho.parent and raiz not in caminho.parents:
        return False
    caminho.unlink()
    return True


def serializar_documento(doc: DocumentoEmpresa, hoje: Optional[date] = None) -> Dict[str, Any]:
    ref = hoje or hoje_sao_paulo()
    caminho = resolver_caminho_documento(doc.caminho_arquivo)
    return {
        "id": doc.id,
        "nome": doc.nome,
        "categoria": doc.categoria,
        "categoria_label": doc.categoria_label,
        "data_emissao": doc.data_emissao.isoformat() if doc.data_emissao else None,
        "data_validade": doc.data_validade.isoformat() if doc.data_validade else None,
        "tags_habilitacao": doc.tags_habilitacao or "",
        "tags": doc.tags,
        "dias_restantes": doc.dias_restantes(ref),
        "status": doc.status_validade(ref),
        "arquivo_disponivel": bool(caminho and caminho.is_file()),
        "arquivo_url": f"/api/documentos/{doc.id}/arquivo",
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


def resumo_validade(documentos: List[Dict[str, Any]]) -> Dict[str, int]:
    resumo = {"total": len(documentos), "validos": 0, "a_vencer": 0, "vencidos": 0, "sem_validade": 0}
    for d in documentos:
        if d["status"] == "VALIDO":
            resumo["validos"] += 1
        elif d["status"] == "A_VENCER":
            resumo["a_vencer"] += 1
        elif d["status"] == "VENCIDO":
            resumo["vencidos"] += 1
        else:
            resumo["sem_validade"] += 1
    return resumo


__all__ = [
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
