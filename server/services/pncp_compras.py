"""
Integração com a API pública de integração do PNCP para uma compra específica:

  GET {base}/orgaos/{cnpj}/compras/{ano}/{sequencial}/itens?pagina=N&tamanhoPagina=M
  GET {base}/orgaos/{cnpj}/compras/{ano}/{sequencial}/arquivos
  GET https://pncp.gov.br/pncp-api/v1/orgaos/{cnpj}/compras/{ano}/{sequencial}/arquivos/{seq}  (binário)

- Os itens são normalizados e gravados em ``tender_items`` (origem = PNCP).
- Os arquivos são listados em ``tender_documents`` e baixados pelo servidor
  (proxy com cache local), de modo que o usuário nunca é redirecionado ao portal.
- Downloads só aceitam hosts do PNCP (proteção contra SSRF) e têm limite de tamanho.
"""
import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

import httpx
from sqlalchemy.orm import Session

from server.config import settings
from server.connectors.pncp import build_canonical_pncp_url
from server.models import Tender, TenderDocument, TenderItem

logger = logging.getLogger(__name__)

HOSTS_PERMITIDOS = {"pncp.gov.br", "www.pncp.gov.br", "treina.pncp.gov.br"}
CABECALHOS = {
    "User-Agent": "licitacoes-brasil/2.0 (+https://licitacoesbrasil.gov.br)",
    "Accept": "application/json, */*",
}
TIMEOUT_JSON = httpx.Timeout(20.0, connect=10.0)
TIMEOUT_DOWNLOAD = httpx.Timeout(120.0, connect=15.0)
TAMANHO_PAGINA_ITENS = 100
MAX_PAGINAS_ITENS = 50
MAX_REDIRECIONAMENTOS = 3
CACHE_NEGATIVO_SEGUNDOS = 30 * 60

# Permite injetar um httpx.MockTransport nos testes
_transport_override: Optional[httpx.AsyncBaseTransport] = None
# tender_id -> (timestamp, motivo) para não consultar o PNCP a cada visualização quando a compra não existe
_falhas_recentes: Dict[str, Tuple[float, str]] = {}


class PNCPErro(Exception):
    """Erro genérico de integração (rede, timeout, HTTP 5xx)."""


class CompraNaoEncontrada(PNCPErro):
    """A compra não existe no PNCP (HTTP 404) ou o certame não tem identificador PNCP."""


class DownloadNaoPermitido(PNCPErro):
    """URL de arquivo fora do domínio do PNCP ou documento sem arquivo vinculado."""


class ArquivoGrandeDemais(PNCPErro):
    pass


@dataclass(frozen=True)
class IdentificadorCompra:
    cnpj: str
    ano: str
    sequencial: str

    @property
    def caminho(self) -> str:
        return f"orgaos/{self.cnpj}/compras/{self.ano}/{self.sequencial}"

    @property
    def url_publica(self) -> str:
        return f"https://pncp.gov.br/app/editais/{self.cnpj}/{self.ano}/{self.sequencial}"


_RE_CANONICA = re.compile(r"pncp\.gov\.br/app/editais/(\d{14})/(\d{4})/(\d+)")


def identificar_compra(tender: Tender) -> Optional[IdentificadorCompra]:
    """Extrai CNPJ/ano/sequencial do numeroControlePNCP ou das URLs do PNCP do certame."""
    candidatos = [
        getattr(tender, "url_pncp", None),
        getattr(tender, "link_pncp", None),
        tender.external_id,
        tender.source_url,
    ]
    for candidato in candidatos:
        if not candidato:
            continue
        canonica = build_canonical_pncp_url(numero_controle_pncp=str(candidato))
        m = _RE_CANONICA.search(canonica or "")
        if m:
            return IdentificadorCompra(cnpj=m.group(1), ano=m.group(2), sequencial=str(int(m.group(3))))
    return None


# ------------------------------------------------------------------------------
# HTTP
# ------------------------------------------------------------------------------
def _cliente(timeout: httpx.Timeout) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        headers=CABECALHOS,
        follow_redirects=False,
        transport=_transport_override,
    )


def host_permitido(url: str) -> bool:
    try:
        partes = urlparse(url)
    except ValueError:
        return False
    return partes.scheme == "https" and (partes.hostname or "").lower() in HOSTS_PERMITIDOS


async def _get_json(caminho: str, params: Optional[Dict[str, Any]] = None) -> Any:
    url = f"{settings.PNCP_INTEGRACAO_BASE_URL}/{caminho}"
    try:
        async with _cliente(TIMEOUT_JSON) as cliente:
            resp = await cliente.get(url, params=params)
    except httpx.HTTPError as exc:
        raise PNCPErro(f"PNCP indisponível: {exc.__class__.__name__}") from exc
    if resp.status_code == 404:
        raise CompraNaoEncontrada("Compra não localizada no PNCP.")
    if resp.status_code == 204 or not resp.content:
        return []
    if resp.status_code >= 400:
        raise PNCPErro(f"PNCP respondeu HTTP {resp.status_code}.")
    try:
        return resp.json()
    except ValueError as exc:
        raise PNCPErro("Resposta inválida do PNCP.") from exc


def _como_lista(dados: Any) -> List[Dict[str, Any]]:
    if isinstance(dados, list):
        return [d for d in dados if isinstance(d, dict)]
    if isinstance(dados, dict):
        for chave in ("data", "itens", "items", "content"):
            if isinstance(dados.get(chave), list):
                return [d for d in dados[chave] if isinstance(d, dict)]
    return []


def _registrar_falha(tender_id: str, motivo: str) -> None:
    _falhas_recentes[tender_id] = (time.time(), motivo)


def falha_recente(tender_id: str) -> Optional[str]:
    registro = _falhas_recentes.get(tender_id)
    if not registro:
        return None
    instante, motivo = registro
    if time.time() - instante > CACHE_NEGATIVO_SEGUNDOS:
        _falhas_recentes.pop(tender_id, None)
        return None
    return motivo


# ------------------------------------------------------------------------------
# Itens
# ------------------------------------------------------------------------------
def _float(valor: Any) -> Optional[float]:
    try:
        return float(valor) if valor is not None and valor != "" else None
    except (TypeError, ValueError):
        return None


def _texto(valor: Any, limite: Optional[int] = None) -> Optional[str]:
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto:
        return None
    return texto[:limite] if limite else texto


def parse_item_pncp(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normaliza um item do endpoint /itens do PNCP para as colunas de TenderItem."""
    try:
        numero = int(raw.get("numeroItem"))
    except (TypeError, ValueError):
        return None
    quantidade = _float(raw.get("quantidade"))
    unitario = _float(raw.get("valorUnitarioEstimado"))
    total = _float(raw.get("valorTotal"))
    if total is None and unitario is not None and quantidade is not None:
        total = round(unitario * quantidade, 2)

    mos = _texto(raw.get("materialOuServico"), 1)
    codigo = (
        _texto(raw.get("catalogoCodigoItem"), 100)
        or _texto(raw.get("ncmNbsCodigo"), 100)
        or _texto(raw.get("itemCategoriaNome"), 100)
    )
    return {
        "item_number": numero,
        "description": _texto(raw.get("descricao")) or f"Item {numero}",
        "unit": _texto(raw.get("unidadeMedida"), 50),
        "quantity": quantidade,
        "estimated_unit_value": unitario,
        "estimated_total_value": total,
        "classification_code": codigo,
        "material_ou_servico": mos.upper() if mos else None,
        "criterio_julgamento": _texto(raw.get("criterioJulgamentoNome"), 100),
        "situacao": _texto(raw.get("situacaoCompraItemNome"), 100),
        "beneficio": _texto(raw.get("tipoBeneficioNome"), 150),
        "orcamento_sigiloso": bool(raw.get("orcamentoSigiloso")) if raw.get("orcamentoSigiloso") is not None else None,
        "informacao_complementar": _texto(raw.get("informacaoComplementar")),
        "raw_data": raw,
    }


async def buscar_itens_pncp(ident: IdentificadorCompra) -> List[Dict[str, Any]]:
    """Percorre todas as páginas de /itens e devolve os itens normalizados (ordenados por número)."""
    itens: Dict[int, Dict[str, Any]] = {}
    for pagina in range(1, MAX_PAGINAS_ITENS + 1):
        dados = await _get_json(
            f"{ident.caminho}/itens", params={"pagina": pagina, "tamanhoPagina": TAMANHO_PAGINA_ITENS}
        )
        lote = _como_lista(dados)
        for raw in lote:
            item = parse_item_pncp(raw)
            if item:
                itens[item["item_number"]] = item
        if len(lote) < TAMANHO_PAGINA_ITENS:
            break
    return [itens[k] for k in sorted(itens)]


# Nomes com que PNCP/Compras.gov.br (e seus espelhos) publicam o intervalo mínimo entre lances.
# O endpoint público /itens do PNCP nem sempre traz o campo: sem ele, a disputa usa 0,01 como fallback.
CHAVES_DEGRAU_MINIMO = (
    "degrauMinimo",
    "valorDegrauMinimo",
    "intervaloMinimoEntreLances",
    "valorIntervaloMinimoEntreLances",
    "intervaloMinimoLances",
    "intervaloMinimo",
    "variacaoMinimaEntreLances",
    "valorVariacaoMinimaEntreLances",
    "diferencaMinimaEntreLances",
    "valorMinimoEntreLances",
    "degrau_minimo",
    "intervalo_minimo",
)


def extrair_degrau_minimo(raw: Any) -> Optional[float]:
    """Degrau/intervalo mínimo entre lances informado no item bruto da API (None se ausente)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, dict):
        return None
    for chave in CHAVES_DEGRAU_MINIMO:
        valor = raw.get(chave)
        if isinstance(valor, str):
            texto = valor.strip().replace("R$", "").strip()
            valor = texto.replace(".", "").replace(",", ".") if "," in texto else texto
        degrau = _float(valor)
        if degrau is not None and degrau > 0:
            return round(degrau, 2) if degrau >= 0.01 else 0.01
    return None


def serializar_item(item: TenderItem) -> Dict[str, Any]:
    return {
        "id": item.id,
        "item_number": item.item_number,
        "description": item.description,
        "unit": item.unit or "Não informado",
        "quantity": item.quantity,
        "estimated_unit_value": item.estimated_unit_value,
        "estimated_total_value": item.estimated_total_value,
        "classification_code": item.classification_code or "Não informado",
        "material_ou_servico": item.material_ou_servico,
        "criterio_julgamento": item.criterio_julgamento,
        "situacao": item.situacao,
        "beneficio": item.beneficio,
        "orcamento_sigiloso": item.orcamento_sigiloso,
        "informacao_complementar": item.informacao_complementar,
        "degrau_minimo": extrair_degrau_minimo(item.raw_data),
        "origem": item.origem or "LOCAL",
    }


def _itens_do_banco(db: Session, tender_id: str) -> List[TenderItem]:
    return db.query(TenderItem).filter(TenderItem.tender_id == tender_id).order_by(TenderItem.item_number.asc()).all()


async def sincronizar_itens(db: Session, tender: Tender, forcar: bool = False) -> Dict[str, Any]:
    """
    Atualiza os itens do certame a partir do PNCP.
    Sem ``forcar``, reutiliza itens já sincronizados (origem PNCP) e o cache de falhas recentes.
    Em falha, devolve os itens locais com ``status`` = "indisponivel" ou "nao_encontrado".
    """
    existentes = _itens_do_banco(db, tender.id)
    ja_sincronizado = any(i.origem == "PNCP" for i in existentes)
    ident = identificar_compra(tender)

    def resposta(status: str, mensagem: Optional[str] = None, origem: str = "LOCAL") -> Dict[str, Any]:
        itens = _itens_do_banco(db, tender.id)
        return {
            "status": status,
            "origem": origem,
            "mensagem": mensagem,
            "total": len(itens),
            "valor_total_estimado": round(sum(i.estimated_total_value or 0 for i in itens), 2),
            "pncp_url": ident.url_publica if ident else None,
            "itens": [serializar_item(i) for i in itens],
        }

    if not ident:
        return resposta("sem_identificador", "Certame sem identificador do PNCP; exibindo itens da base local.")
    if ja_sincronizado and not forcar:
        return resposta("cache", None, "PNCP")
    if not forcar and falha_recente(f"itens:{tender.id}"):
        return resposta("indisponivel", falha_recente(f"itens:{tender.id}"))

    try:
        itens_pncp = await buscar_itens_pncp(ident)
    except CompraNaoEncontrada:
        msg = "Compra não localizada no PNCP; exibindo itens da base local."
        _registrar_falha(f"itens:{tender.id}", msg)
        return resposta("nao_encontrado", msg)
    except PNCPErro as exc:
        msg = f"Não foi possível consultar o PNCP agora ({exc}). Exibindo itens da base local."
        _registrar_falha(f"itens:{tender.id}", msg)
        return resposta("indisponivel", msg)

    if not itens_pncp:
        return resposta("vazio", "O PNCP ainda não publicou itens para esta compra.", "PNCP")

    agora = datetime.utcnow()
    por_numero = {i.item_number: i for i in existentes}
    numeros_pncp = set()
    for dados in itens_pncp:
        numeros_pncp.add(dados["item_number"])
        item = por_numero.get(dados["item_number"])
        if item is None:
            item = TenderItem(tender_id=tender.id, item_number=dados["item_number"], description=dados["description"])
            db.add(item)
        for campo, valor in dados.items():
            setattr(item, campo, valor)
        item.origem = "PNCP"
        item.synced_at = agora
    # O PNCP é a fonte oficial: remove itens locais que não existem mais na compra
    for numero, item in por_numero.items():
        if numero not in numeros_pncp:
            db.delete(item)
    tender.last_synced_at = agora
    db.commit()
    _falhas_recentes.pop(f"itens:{tender.id}", None)
    return resposta("sincronizado", None, "PNCP")


# ------------------------------------------------------------------------------
# Arquivos / anexos
# ------------------------------------------------------------------------------
def parse_arquivo_pncp(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = _texto(raw.get("url")) or _texto(raw.get("uri"))
    if not url or not host_permitido(url):
        return None
    if raw.get("statusAtivo") is False:
        return None
    try:
        sequencial = int(raw.get("sequencialDocumento"))
    except (TypeError, ValueError):
        sequencial = None
    publicado = None
    if raw.get("dataPublicacaoPncp"):
        try:
            publicado = datetime.fromisoformat(str(raw["dataPublicacaoPncp"]).replace("Z", "")[:19])
        except ValueError:
            publicado = None
    titulo = _texto(raw.get("titulo"), 500) or f"Documento {sequencial or ''}".strip()
    return {
        "name": re.sub(r"\s+", " ", titulo.replace("_", " ")).strip() or titulo,
        "document_type": (_texto(raw.get("tipoDocumentoNome"), 100) or _texto(raw.get("tipoDocumentoDescricao"), 100) or "Anexo"),
        "source_url": url,
        "sequencial_documento": sequencial,
        "published_at": publicado,
    }


async def buscar_arquivos_pncp(ident: IdentificadorCompra) -> List[Dict[str, Any]]:
    dados = await _get_json(f"{ident.caminho}/arquivos")
    arquivos = [a for a in (parse_arquivo_pncp(r) for r in _como_lista(dados)) if a]
    return sorted(arquivos, key=lambda a: (a["sequencial_documento"] or 0))


def documento_baixavel(doc: TenderDocument) -> bool:
    return bool(doc.source_url) and host_permitido(doc.source_url) and (
        "/arquivos/" in doc.source_url or doc.origem == "PNCP"
    )


def serializar_documento(doc: TenderDocument) -> Dict[str, Any]:
    baixavel = documento_baixavel(doc)
    return {
        "id": doc.id,
        "name": doc.name,
        "document_type": doc.document_type,
        "mime_type": doc.mime_type,
        "file_size": doc.file_size,
        "checksum": doc.checksum,
        "published_at": doc.published_at.isoformat() if doc.published_at else None,
        "downloaded_at": doc.downloaded_at.isoformat() if doc.downloaded_at else None,
        "sequencial_documento": doc.sequencial_documento,
        "origem": doc.origem or "LOCAL",
        "baixavel": baixavel,
        "download_url": f"/api/tenders/{doc.tender_id}/arquivos/{doc.id}/download" if baixavel else None,
        "visualizar_url": f"/api/tenders/{doc.tender_id}/arquivos/{doc.id}/download?inline=1" if baixavel else None,
    }


def _docs_do_banco(db: Session, tender_id: str) -> List[TenderDocument]:
    return (
        db.query(TenderDocument)
        .filter(TenderDocument.tender_id == tender_id)
        .order_by(TenderDocument.sequencial_documento.is_(None), TenderDocument.sequencial_documento.asc(), TenderDocument.created_at.asc())
        .all()
    )


async def sincronizar_arquivos(db: Session, tender: Tender, forcar: bool = False) -> Dict[str, Any]:
    existentes = _docs_do_banco(db, tender.id)
    ja_sincronizado = any(d.origem == "PNCP" for d in existentes)
    ident = identificar_compra(tender)

    def resposta(status: str, mensagem: Optional[str] = None) -> Dict[str, Any]:
        docs = _docs_do_banco(db, tender.id)
        return {
            "status": status,
            "mensagem": mensagem,
            "total": len(docs),
            "pncp_url": ident.url_publica if ident else None,
            "zip_url": f"/api/tenders/{tender.id}/arquivos/zip" if any(documento_baixavel(d) for d in docs) else None,
            "arquivos": [serializar_documento(d) for d in docs],
        }

    if not ident:
        return resposta("sem_identificador", "Certame sem identificador do PNCP.")
    if ja_sincronizado and not forcar:
        return resposta("cache")
    if not forcar and falha_recente(f"arquivos:{tender.id}"):
        return resposta("indisponivel", falha_recente(f"arquivos:{tender.id}"))

    try:
        arquivos = await buscar_arquivos_pncp(ident)
    except CompraNaoEncontrada:
        msg = "Compra não localizada no PNCP."
        _registrar_falha(f"arquivos:{tender.id}", msg)
        return resposta("nao_encontrado", msg)
    except PNCPErro as exc:
        msg = f"Não foi possível consultar o PNCP agora ({exc})."
        _registrar_falha(f"arquivos:{tender.id}", msg)
        return resposta("indisponivel", msg)

    por_url = {d.source_url: d for d in existentes}
    por_seq = {d.sequencial_documento: d for d in existentes if d.sequencial_documento is not None}
    for dados in arquivos:
        doc = por_seq.get(dados["sequencial_documento"]) or por_url.get(dados["source_url"])
        if doc is None:
            doc = TenderDocument(tender_id=tender.id, name=dados["name"], source_url=dados["source_url"])
            db.add(doc)
        url_mudou = doc.source_url != dados["source_url"]
        for campo, valor in dados.items():
            setattr(doc, campo, valor)
        doc.origem = "PNCP"
        if url_mudou:
            doc.storage_key = None
            doc.checksum = None
            doc.downloaded_at = None
    db.commit()
    _falhas_recentes.pop(f"arquivos:{tender.id}", None)
    if not arquivos:
        return resposta("vazio", "O órgão ainda não publicou arquivos para esta compra no PNCP.")
    return resposta("sincronizado")


# ------------------------------------------------------------------------------
# Download (proxy com cache local)
# ------------------------------------------------------------------------------
_EXTENSOES_POR_MIME = {
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "application/x-rar-compressed": ".rar",
    "application/vnd.rar": ".rar",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}


def _diretorio_cache(tender_id: str) -> Path:
    base = Path(settings.STORAGE_LOCAL_PATH)
    if not base.is_absolute():
        base = Path(__file__).resolve().parent.parent.parent / base
    destino = base / "pncp" / re.sub(r"[^A-Za-z0-9_-]", "_", tender_id)
    destino.mkdir(parents=True, exist_ok=True)
    return destino


def _nome_seguro(nome: str, max_len: int = 120) -> str:
    base = unicodedata.normalize("NFKD", nome or "").encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip(" ._") or "documento"
    raiz, ext = os.path.splitext(base)
    return (raiz[: max_len - len(ext)] + ext) if len(base) > max_len else base


def _nome_do_cabecalho(content_disposition: str) -> Optional[str]:
    if not content_disposition:
        return None
    m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", content_disposition, re.IGNORECASE)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', content_disposition, re.IGNORECASE)
    if m:
        bruto = m.group(1).strip()
        try:
            return bruto.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return bruto
    return None


def _mime_por_conteudo(cabeca: bytes, mime_http: Optional[str]) -> str:
    if cabeca.startswith(b"%PDF"):
        return "application/pdf"
    if cabeca.startswith(b"PK\x03\x04"):
        return mime_http if mime_http and ("openxml" in mime_http or "opendocument" in mime_http) else "application/zip"
    if cabeca.startswith(b"Rar!"):
        return "application/vnd.rar"
    if cabeca.startswith(b"\xd0\xcf\x11\xe0"):
        return mime_http if mime_http and mime_http != "application/octet-stream" else "application/msword"
    return mime_http or "application/octet-stream"


def nome_para_download(doc: TenderDocument) -> str:
    nome = doc.name or "documento"
    ext_atual = os.path.splitext(nome)[1].lower()
    ext_mime = _EXTENSOES_POR_MIME.get((doc.mime_type or "").split(";")[0].strip()) or mimetypes.guess_extension(doc.mime_type or "") or ""
    if ext_mime and ext_atual != ext_mime:
        nome = f"{nome}{ext_mime}"
    return _nome_seguro(nome)


def caminho_em_cache(doc: TenderDocument) -> Optional[Path]:
    if not doc.storage_key:
        return None
    caminho = Path(doc.storage_key)
    if not caminho.is_absolute():
        caminho = Path(__file__).resolve().parent.parent.parent / caminho
    return caminho if caminho.is_file() else None


async def baixar_documento(db: Session, doc: TenderDocument, forcar: bool = False) -> Path:
    """
    Garante o arquivo do documento em cache local e devolve o caminho.
    Segue até MAX_REDIRECIONAMENTOS, sempre validando que o destino é do PNCP.
    """
    if not documento_baixavel(doc):
        raise DownloadNaoPermitido("Documento sem arquivo disponível para download interno.")

    em_cache = None if forcar else caminho_em_cache(doc)
    if em_cache:
        return em_cache

    limite = settings.PNCP_DOWNLOAD_MAX_MB * 1024 * 1024
    destino_dir = _diretorio_cache(doc.tender_id)
    url = doc.source_url
    fd, temporario = tempfile.mkstemp(dir=destino_dir, prefix=".download_", suffix=".part")
    os.close(fd)
    sha = hashlib.sha256()
    tamanho = 0
    cabeca = b""
    mime_http = None
    nome_http = None
    try:
        async with _cliente(TIMEOUT_DOWNLOAD) as cliente:
            for _ in range(MAX_REDIRECIONAMENTOS + 1):
                if not host_permitido(url):
                    raise DownloadNaoPermitido("Redirecionamento para domínio não autorizado bloqueado.")
                async with cliente.stream("GET", url, headers={"Accept": "*/*"}) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                        url = urljoin(url, resp.headers["location"])
                        continue
                    if resp.status_code == 404:
                        raise CompraNaoEncontrada("Arquivo não encontrado no PNCP.")
                    if resp.status_code >= 400:
                        raise PNCPErro(f"PNCP respondeu HTTP {resp.status_code} ao baixar o arquivo.")
                    declarado = int(resp.headers.get("content-length") or 0)
                    if declarado and declarado > limite:
                        raise ArquivoGrandeDemais(f"Arquivo excede {settings.PNCP_DOWNLOAD_MAX_MB} MB.")
                    mime_http = (resp.headers.get("content-type") or "").split(";")[0].strip() or None
                    nome_http = _nome_do_cabecalho(resp.headers.get("content-disposition", ""))
                    with open(temporario, "wb") as fh:
                        async for bloco in resp.aiter_bytes(64 * 1024):
                            tamanho += len(bloco)
                            if tamanho > limite:
                                raise ArquivoGrandeDemais(f"Arquivo excede {settings.PNCP_DOWNLOAD_MAX_MB} MB.")
                            if len(cabeca) < 16:
                                cabeca += bloco[: 16 - len(cabeca)]
                            sha.update(bloco)
                            fh.write(bloco)
                    break
            else:
                raise PNCPErro("Excesso de redirecionamentos ao baixar o arquivo.")
    except httpx.HTTPError as exc:
        Path(temporario).unlink(missing_ok=True)
        raise PNCPErro(f"Falha de rede ao baixar do PNCP: {exc.__class__.__name__}") from exc
    except Exception:
        Path(temporario).unlink(missing_ok=True)
        raise

    mime = _mime_por_conteudo(cabeca, mime_http)
    ext = _EXTENSOES_POR_MIME.get(mime) or mimetypes.guess_extension(mime) or ".bin"
    final = destino_dir / f"{doc.id}{ext}"
    os.replace(temporario, final)

    raiz = Path(__file__).resolve().parent.parent.parent
    try:
        doc.storage_key = final.resolve().relative_to(raiz).as_posix()
    except ValueError:
        doc.storage_key = str(final.resolve())
    doc.mime_type = mime
    doc.file_size = tamanho
    doc.checksum = sha.hexdigest()
    doc.downloaded_at = datetime.utcnow()
    if nome_http and (not doc.name or doc.name.startswith("Documento ")):
        doc.name = nome_http[:500]
    db.commit()
    return final


async def baixar_varios(db: Session, docs: List[TenderDocument], limite_total_mb: int = 300) -> List[Tuple[TenderDocument, Optional[Path], Optional[str]]]:
    """Baixa (ou reaproveita do cache) vários documentos, sequencialmente, respeitando um teto total."""
    resultados: List[Tuple[TenderDocument, Optional[Path], Optional[str]]] = []
    acumulado = 0
    for doc in docs:
        if not documento_baixavel(doc):
            continue
        try:
            caminho = await baixar_documento(db, doc)
            acumulado += caminho.stat().st_size
            if acumulado > limite_total_mb * 1024 * 1024:
                resultados.append((doc, None, f"Limite total de {limite_total_mb} MB atingido"))
                break
            resultados.append((doc, caminho, None))
        except PNCPErro as exc:
            resultados.append((doc, None, str(exc)))
        await asyncio.sleep(0)
    return resultados
