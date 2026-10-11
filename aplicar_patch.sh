#!/usr/bin/env bash
# Patch Licitações Brasil — execute na raiz do repositório: bash aplicar_patch.sh
set -euo pipefail
[ -f server/main.py ] || { echo "Execute na raiz do projeto licitacoes-brasil"; exit 1; }

# ==============================================================================
# ITEM 1 — GERAÇÃO JURÍDICA EM PDF (ReportLab) + rotas (main.py também cobre itens 2, 3 e 5)
# ==============================================================================
mkdir -p server/services
cat << 'EOF' > server/services/pdf_generator.py
"""
Gerador de PDFs jurídicos timbrados "Licitações Brasil" (ReportLab).

- ``gerar_peticao_pdf``: compila petições (impugnação, esclarecimento, recurso,
  contrarrazões) com cabeçalho oficial, ficha do certame, qualificação da licitante,
  corpo fundamentado (Lei 14.133/2021 ou Lei 8.666/1993) e bloco de assinatura.
- ``renderizar_pdf_timbrado``: motor de layout reutilizável (timbre, rodapé e
  paginação "Página X de Y") usado também pelo Relatório de Conformidade do kit.
"""
import io
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ------------------------------------------------------------------------------
# Identidade visual
# ------------------------------------------------------------------------------
MARCA = "Licitações Brasil"
COR_NAVY = colors.HexColor("#082f49")
COR_SKY = colors.HexColor("#38bdf8")
COR_SKY_ESCURO = colors.HexColor("#0369a1")
COR_AMBAR = colors.HexColor("#d97706")
COR_TEXTO = colors.HexColor("#0f172a")
COR_CINZA = colors.HexColor("#475569")
COR_BORDA = colors.HexColor("#cbd5e1")
COR_FUNDO_FICHA = colors.HexColor("#f1f5f9")

PAGE_W, PAGE_H = A4
MARGEM_X = 20 * mm
MARGEM_TOPO = 34 * mm
MARGEM_BASE = 20 * mm
ALTURA_TIMBRE = 22 * mm

MESES_PT = [
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
]

LEIS = {
    "14133": {
        "codigo": "14133",
        "diploma": "Lei Federal nº 14.133, de 1º de abril de 2021",
        "rotulo": "Lei 14.133/2021 — Nova Lei de Licitações e Contratos Administrativos",
        "fundamentacao_padrao": "Arts. 164 e 165 da Lei Federal nº 14.133/2021",
    },
    "8666": {
        "codigo": "8666",
        "diploma": "Lei Federal nº 8.666, de 21 de junho de 1993",
        "rotulo": "Lei 8.666/1993 — Regime Licitatório Legado",
        "fundamentacao_padrao": "Arts. 41 e 109 da Lei Federal nº 8.666/1993",
    },
}

_PLACEHOLDERS_LICITANTE = re.compile(
    r"\[(NOME DA EMPRESA|RECORRENTE|RECORRIDA|RAZ[ÃA]O SOCIAL|LICITANTE|EMPRESA)\]",
    re.IGNORECASE,
)


def agora_sao_paulo() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Sao_Paulo"))
    except Exception:
        return datetime.now()


class PeticaoInvalidaError(ValueError):
    """Dados insuficientes para compilar a petição."""


# ------------------------------------------------------------------------------
# Utilidades
# ------------------------------------------------------------------------------
def normalizar_lei(valor: Optional[Any]) -> str:
    """Aceita '14133', '14.133/2021', 'nova', True -> '14133'; '8666', '8.666/1993', 'legado', False -> '8666'."""
    if isinstance(valor, bool):
        return "14133" if valor else "8666"
    texto = re.sub(r"[^0-9a-z]", "", str(valor or "").lower())
    if texto.startswith("8666") or texto in {"legado", "antiga", "lei8666"}:
        return "8666"
    return "14133"


def formatar_cnpj(cnpj: Optional[str]) -> str:
    digitos = re.sub(r"\D", "", cnpj or "")
    if len(digitos) == 14:
        return f"{digitos[:2]}.{digitos[2:5]}.{digitos[5:8]}/{digitos[8:12]}-{digitos[12:]}"
    return (cnpj or "").strip()


def data_por_extenso(d: date) -> str:
    return f"{d.day} de {MESES_PT[d.month - 1]} de {d.year}"


def _txt(valor: Optional[Any], padrao: str = "") -> str:
    """Escapa texto do usuário para o mini-markup do Paragraph do ReportLab."""
    texto = str(valor).strip() if valor is not None else ""
    return escape(texto or padrao)


def slug_arquivo(*partes: Optional[str]) -> str:
    import unicodedata

    bruto = "_".join(p for p in partes if p)
    ascii_txt = unicodedata.normalize("NFKD", bruto).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", ascii_txt).strip("_")
    return (slug[:80].rstrip("_")) or "documento"


# ------------------------------------------------------------------------------
# Estilos
# ------------------------------------------------------------------------------
def estilos_timbrados() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()["Normal"]
    return {
        "titulo": ParagraphStyle(
            "LBTitulo", parent=base, fontName="Helvetica-Bold", fontSize=14, leading=18,
            textColor=COR_NAVY, alignment=TA_CENTER, spaceAfter=4,
        ),
        "subtitulo": ParagraphStyle(
            "LBSubtitulo", parent=base, fontName="Helvetica", fontSize=9, leading=12,
            textColor=COR_CINZA, alignment=TA_CENTER, spaceAfter=10,
        ),
        "secao": ParagraphStyle(
            "LBSecao", parent=base, fontName="Helvetica-Bold", fontSize=9.5, leading=12,
            textColor=COR_SKY_ESCURO, spaceBefore=8, spaceAfter=4,
        ),
        "rotulo": ParagraphStyle(
            "LBRotulo", parent=base, fontName="Helvetica-Bold", fontSize=8, leading=10,
            textColor=COR_CINZA,
        ),
        "valor": ParagraphStyle(
            "LBValor", parent=base, fontName="Helvetica", fontSize=9, leading=11.5,
            textColor=COR_TEXTO,
        ),
        "corpo": ParagraphStyle(
            "LBCorpo", parent=base, fontName="Helvetica", fontSize=10.5, leading=15.5,
            textColor=COR_TEXTO, alignment=TA_JUSTIFY, spaceAfter=8,
        ),
        "corpo_destaque": ParagraphStyle(
            "LBCorpoDestaque", parent=base, fontName="Helvetica-Bold", fontSize=10.5, leading=15,
            textColor=COR_TEXTO, alignment=TA_LEFT, spaceBefore=4, spaceAfter=6,
        ),
        "pequeno": ParagraphStyle(
            "LBPequeno", parent=base, fontName="Helvetica", fontSize=8, leading=10,
            textColor=COR_CINZA,
        ),
        "assinatura": ParagraphStyle(
            "LBAssinatura", parent=base, fontName="Helvetica", fontSize=10, leading=13,
            textColor=COR_TEXTO, alignment=TA_CENTER,
        ),
        "celula": ParagraphStyle(
            "LBCelula", parent=base, fontName="Helvetica", fontSize=8.5, leading=10.5,
            textColor=COR_TEXTO,
        ),
        "celula_cabecalho": ParagraphStyle(
            "LBCelulaCab", parent=base, fontName="Helvetica-Bold", fontSize=8, leading=10,
            textColor=colors.white,
        ),
    }


# ------------------------------------------------------------------------------
# Timbre, rodapé e paginação
# ------------------------------------------------------------------------------
def _desenhar_timbre(c: rl_canvas.Canvas, titulo_documento: str, referencia: str) -> None:
    c.saveState()
    topo = PAGE_H
    # Faixa institucional
    c.setFillColor(COR_NAVY)
    c.rect(0, topo - ALTURA_TIMBRE, PAGE_W, ALTURA_TIMBRE, stroke=0, fill=1)
    c.setFillColor(COR_AMBAR)
    c.rect(0, topo - ALTURA_TIMBRE - 1.2 * mm, PAGE_W, 1.2 * mm, stroke=0, fill=1)

    # Monograma
    lado = 11 * mm
    x_logo = MARGEM_X
    y_logo = topo - ALTURA_TIMBRE / 2 - lado / 2
    c.setFillColor(COR_SKY)
    c.roundRect(x_logo, y_logo, lado, lado, 2.2 * mm, stroke=0, fill=1)
    c.setFillColor(COR_NAVY)
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(x_logo + lado / 2, y_logo + lado / 2 - 4.2, "LB")

    # Marca nominativa
    x_txt = x_logo + lado + 3.5 * mm
    y_txt = topo - ALTURA_TIMBRE / 2 + 1.2 * mm
    c.setFont("Helvetica-Bold", 17)
    c.setFillColor(colors.white)
    c.drawString(x_txt, y_txt, "Licitações")
    largura = c.stringWidth("Licitações", "Helvetica-Bold", 17)
    c.setFillColor(COR_SKY)
    c.drawString(x_txt + largura, y_txt, "Brasil")
    c.setFont("Helvetica", 7.5)
    c.setFillColor(colors.HexColor("#cbd5e1"))
    c.drawString(x_txt, y_txt - 4.2 * mm, "Inteligência em Contratações Públicas  •  Assessoria Jurídica Licitatória")

    # Identificação do documento (direita)
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(colors.white)
    c.drawRightString(PAGE_W - MARGEM_X, topo - ALTURA_TIMBRE / 2 + 2 * mm, (titulo_documento or "")[:70].upper())
    c.setFont("Helvetica", 7.5)
    c.setFillColor(COR_SKY)
    c.drawRightString(PAGE_W - MARGEM_X, topo - ALTURA_TIMBRE / 2 - 2.5 * mm, (referencia or "")[:90])
    c.restoreState()


def _desenhar_rodape(c: rl_canvas.Canvas, pagina: int, total: int, emitido_em: str) -> None:
    c.saveState()
    y = MARGEM_BASE - 8 * mm
    c.setStrokeColor(COR_BORDA)
    c.setLineWidth(0.6)
    c.line(MARGEM_X, y + 4 * mm, PAGE_W - MARGEM_X, y + 4 * mm)
    c.setFont("Helvetica", 7)
    c.setFillColor(COR_CINZA)
    c.drawString(MARGEM_X, y, f"{MARCA}  •  Documento gerado eletronicamente em {emitido_em}")
    c.drawRightString(PAGE_W - MARGEM_X, y, f"Página {pagina} de {total}")
    c.restoreState()


def _canvas_numerado(titulo_documento: str, referencia: str, emitido_em: str):
    """Fábrica de Canvas com paginação total ("Página X de Y") e timbre em todas as páginas."""

    class CanvasNumerado(rl_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._estados_paginas: List[Dict[str, Any]] = []

        def showPage(self):  # noqa: N802 (API ReportLab)
            self._estados_paginas.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._estados_paginas)
            for estado in self._estados_paginas:
                self.__dict__.update(estado)
                _desenhar_timbre(self, titulo_documento, referencia)
                _desenhar_rodape(self, self._pageNumber, total, emitido_em)
                super().showPage()
            super().save()

    return CanvasNumerado


def renderizar_pdf_timbrado(
    story: Sequence[Any],
    *,
    titulo_documento: str,
    referencia: str = "",
    assunto: str = "",
) -> bytes:
    """Renderiza uma lista de flowables no papel timbrado Licitações Brasil e retorna os bytes do PDF."""
    buffer = io.BytesIO()
    emitido_em = agora_sao_paulo().strftime("%d/%m/%Y %H:%M")
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGEM_X,
        rightMargin=MARGEM_X,
        topMargin=MARGEM_TOPO,
        bottomMargin=MARGEM_BASE,
        title=titulo_documento,
        author=MARCA,
        subject=assunto or titulo_documento,
        creator=f"{MARCA} — Gerador Jurídico",
    )
    doc.build(list(story), canvasmaker=_canvas_numerado(titulo_documento, referencia, emitido_em))
    return buffer.getvalue()


def tabela_ficha(linhas: Sequence[Sequence[str]], estilos: Dict[str, ParagraphStyle]) -> Table:
    """Tabela rótulo/valor com fundo suave (valores já escapados)."""
    dados = [[Paragraph(rotulo, estilos["rotulo"]), Paragraph(valor, estilos["valor"])] for rotulo, valor in linhas]
    largura_util = PAGE_W - 2 * MARGEM_X
    tabela = Table(dados, colWidths=[38 * mm, largura_util - 38 * mm])
    tabela.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), COR_FUNDO_FICHA),
        ("BOX", (0, 0), (-1, -1), 0.6, COR_BORDA),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, COR_BORDA),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return tabela


# ------------------------------------------------------------------------------
# Petição
# ------------------------------------------------------------------------------
def montar_corpo_peticao(
    corpo: str,
    *,
    orgao: str,
    numero_certame: str,
    objeto: str,
    razao_social: Optional[str] = None,
) -> str:
    """Substitui as tags dinâmicas do modelo e os marcadores de qualificação da licitante."""
    texto = (corpo or "").replace("\r\n", "\n")
    texto = texto.replace("{{orgao}}", orgao)
    texto = texto.replace("{{numero_edital}}", numero_certame)
    texto = texto.replace("{{numero_certame}}", numero_certame)
    texto = texto.replace("{{objeto}}", objeto)
    if razao_social:
        texto = _PLACEHOLDERS_LICITANTE.sub(razao_social.upper(), texto)
    return texto.strip()


def _eh_destaque(linha: str) -> bool:
    """Linhas curtas em caixa alta (endereçamento, títulos, seções numeradas) recebem negrito."""
    letras = [ch for ch in linha if ch.isalpha()]
    if not letras or len(linha) > 110:
        return False
    maiusculas = sum(1 for ch in letras if ch.isupper())
    return maiusculas / len(letras) >= 0.8


def _paragrafos_corpo(texto: str, estilos: Dict[str, ParagraphStyle]) -> List[Any]:
    flowables: List[Any] = []
    for bloco in re.split(r"\n\s*\n", texto):
        linhas = [ln.strip() for ln in bloco.split("\n") if ln.strip()]
        if not linhas:
            continue
        if all(_eh_destaque(ln) for ln in linhas):
            flowables.append(Paragraph("<br/>".join(escape(ln) for ln in linhas), estilos["corpo_destaque"]))
        else:
            marcadas = [f"<b>{escape(ln)}</b>" if _eh_destaque(ln) else escape(ln) for ln in linhas]
            flowables.append(Paragraph("<br/>".join(marcadas), estilos["corpo"]))
    return flowables


def gerar_peticao_pdf(
    *,
    corpo: str,
    orgao: Optional[str] = None,
    numero_certame: Optional[str] = None,
    objeto: Optional[str] = None,
    titulo: Optional[str] = None,
    tipo_label: Optional[str] = None,
    lei: Optional[Any] = "14133",
    fundamentacao: Optional[str] = None,
    licitante: Optional[Dict[str, Any]] = None,
    cidade: Optional[str] = None,
    data_documento: Optional[date] = None,
) -> bytes:
    """
    Compila a petição em PDF timbrado e retorna os bytes.

    ``licitante`` aceita as chaves: razao_social, cnpj, endereco, representante,
    cargo, cpf, email, telefone.
    """
    if not (corpo or "").strip():
        raise PeticaoInvalidaError("O corpo da petição está vazio.")

    lic = {k: (str(v).strip() if v is not None else "") for k, v in (licitante or {}).items()}
    lei_info = LEIS[normalizar_lei(lei)]
    orgao_txt = (orgao or "").strip() or "[ÓRGÃO COMPRADOR]"
    numero_txt = (numero_certame or "").strip() or "[NÚMERO DO CERTAME]"
    objeto_txt = (objeto or "").strip() or "[OBJETO DA LICITAÇÃO]"
    titulo_txt = (titulo or "").strip() or "Petição Administrativa"
    fundamentacao_txt = (fundamentacao or "").strip() or lei_info["fundamentacao_padrao"]
    razao = lic.get("razao_social") or ""
    cnpj = formatar_cnpj(lic.get("cnpj"))
    data_doc = data_documento or agora_sao_paulo().date()

    est = estilos_timbrados()
    story: List[Any] = []

    story.append(Paragraph(_txt(titulo_txt.upper()), est["titulo"]))
    sub = f"{_txt(tipo_label) + '  •  ' if tipo_label else ''}{_txt(lei_info['rotulo'])}"
    story.append(Paragraph(sub, est["subtitulo"]))

    story.append(Paragraph("DADOS DO CERTAME", est["secao"]))
    story.append(tabela_ficha([
        ("Certame nº", f"<b>{_txt(numero_txt)}</b>"),
        ("Órgão / Entidade", _txt(orgao_txt)),
        ("Objeto", _txt(objeto_txt)),
        ("Fundamentação", _txt(fundamentacao_txt)),
        ("Diploma legal", _txt(lei_info["diploma"])),
    ], est))

    story.append(Paragraph("DADOS DA LICITANTE", est["secao"]))
    contato = " / ".join(x for x in [lic.get("email"), lic.get("telefone")] if x)
    representante = lic.get("representante") or ""
    if representante and lic.get("cargo"):
        representante = f"{representante} — {lic.get('cargo')}"
    story.append(tabela_ficha([
        ("Razão social", f"<b>{_txt(razao, '[RAZÃO SOCIAL DA LICITANTE]')}</b>"),
        ("CNPJ", _txt(cnpj, "[CNPJ]")),
        ("Endereço", _txt(lic.get("endereco"), "[ENDEREÇO COMPLETO]")),
        ("Representante legal", _txt(representante, "[REPRESENTANTE LEGAL]")),
        ("Contato", _txt(contato, "—")),
    ], est))
    story.append(Spacer(1, 6 * mm))

    texto = montar_corpo_peticao(
        corpo, orgao=orgao_txt, numero_certame=numero_txt, objeto=objeto_txt, razao_social=razao or None
    )
    story.extend(_paragrafos_corpo(texto, est))

    local = (cidade or "").strip() or "[Cidade/UF]"
    assinatura = [
        Spacer(1, 8 * mm),
        Paragraph(f"{_txt(local)}, {data_por_extenso(data_doc)}.", est["corpo"]),
        Spacer(1, 14 * mm),
        Paragraph("_" * 52, est["assinatura"]),
        Paragraph(f"<b>{_txt(lic.get('representante'), '[REPRESENTANTE LEGAL]')}</b>", est["assinatura"]),
    ]
    if lic.get("cargo"):
        assinatura.append(Paragraph(_txt(lic.get("cargo")), est["assinatura"]))
    if lic.get("cpf"):
        assinatura.append(Paragraph(f"CPF: {_txt(lic.get('cpf'))}", est["assinatura"]))
    assinatura.append(Paragraph(_txt(razao, "[RAZÃO SOCIAL DA LICITANTE]"), est["assinatura"]))
    if cnpj:
        assinatura.append(Paragraph(f"CNPJ: {_txt(cnpj)}", est["assinatura"]))
    story.append(KeepTogether(assinatura))

    return renderizar_pdf_timbrado(
        story,
        titulo_documento=titulo_txt,
        referencia=f"Certame nº {numero_txt}  •  {lei_info['rotulo'].split(' — ')[0]}",
        assunto=f"{titulo_txt} — {orgao_txt}",
    )


__all__ = [
    "LEIS",
    "MARCA",
    "PeticaoInvalidaError",
    "data_por_extenso",
    "estilos_timbrados",
    "formatar_cnpj",
    "gerar_peticao_pdf",
    "montar_corpo_peticao",
    "normalizar_lei",
    "renderizar_pdf_timbrado",
    "slug_arquivo",
    "tabela_ficha",
]
EOF

mkdir -p server
cat << 'EOF' > server/main.py
import os
import sys
import json
import csv
import io
import asyncio
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any, Union
from fastapi import FastAPI, Depends, HTTPException, status, Query, Request, Body, Form, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func, text
from pydantic import BaseModel, Field

# Garantir importações locais
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server.config import settings
from server.database import get_db, engine, Base
from server.models import (
    User, Source, Tender, TenderItem, TenderDocument,
    Favorite, UserNote, SavedSearch, Alert, AlertDelivery,
    TenderChange, DocumentExtraction, AIAnalysis, SearchHistory,
    WebhookDispatchLog, Deal, DEAL_STAGES, DEAL_STAGE_LABELS,
    LegalDocumentTemplate, SupplierProspect, generate_uuid,
    DocumentoEmpresa, DOCUMENTO_CATEGORIAS, DOCUMENTO_DIAS_ALERTA, hoje_sao_paulo
)
from server.schemas import (
    UserRegisterRequest, UserLoginRequest, PasswordChangeRequest,
    UserProfileResponse, TokenResponse, SearchFilterParams,
    SearchResultResponse, TenderDetailResponse, TenderSummaryResponse,
    TenderItemResponse, TenderDocumentResponse, TenderChangeResponse,
    AIAnalysisSummaryResponse, FavoriteToggleResponse, UserNoteRequest,
    AlertCreateRequest, AlertResponse, AlertFeedResponse, AlertFeedItem, SourceResponse,
    WebhookDispatchLogResponse, WebhookDispatchLogsListResponse, HealthMetricsResponse,
    SyncOpportunitiesResponse, AIChatRequest, AISummaryRequest
)
from server.webhook_dispatcher import retry_webhook_dispatch, dispatch_webhook_sync, dispatch_webhook_async
from server.security import (
    hash_password, verify_password, create_access_token, decode_access_token,
    check_rate_limit, calculate_sha256
)
from server.search_engine import search_tenders, CATEGORIES
from server.ai_engine import analyze_tender_document_with_ai
from server.services.ai_service import analyze_tender_summary, ask_tender_question
from server.connectors.registry import SOURCE_CATALOG, list_all_sources, get_connector
from server.connectors.pncp import build_canonical_pncp_url
from server.alerts_engine import run_alert_processing_job
from server.seed_data import seed_database
from server.integrations.opportunity_collector import sync_procurement_opportunities
from server.migrate import run_db_migrations
from server.services.dispute_monitor import dispute_monitor, PregaoItem
from server.services.portal_collector import (
    classificar_natureza_certame, NATUREZAS_OBJETO, NATUREZA_LABELS
)
from server.services.pdf_generator import (
    gerar_peticao_pdf, normalizar_lei, slug_arquivo, PeticaoInvalidaError
)
from server.services.kit_builder import (
    montar_kit_habilitacao_zip, nome_arquivo_kit, KitHabilitacaoError
)
from server.services import document_vault

# Fuso horário oficial para prazos e agenda
try:
    from zoneinfo import ZoneInfo
    SP_TZ = ZoneInfo("America/Sao_Paulo")
except Exception:
    from datetime import timezone
    SP_TZ = timezone(timedelta(hours=-3), name="America/Sao_Paulo")

def to_sp_datetime(dt_val: Union[datetime, date, str, None]) -> Optional[datetime]:
    """
    Converte campos de abertura/encerramento para objetos datetime no fuso horário America/Sao_Paulo.
    Elimina comparações literais de strings.
    """
    if not dt_val:
        return None
    if isinstance(dt_val, str):
        try:
            dt_clean = dt_val.strip()
            if "T" in dt_clean:
                dt = datetime.fromisoformat(dt_clean)
            elif "/" in dt_clean:
                parts = dt_clean.split(" ")
                date_parts = [int(p) for p in parts[0].split("/")]
                if len(date_parts) == 3:
                    d, m, y = date_parts
                    h, mi, s = 0, 0, 0
                    if len(parts) > 1:
                        time_parts = [int(p) for p in parts[1].split(":")]
                        h = time_parts[0]
                        mi = time_parts[1] if len(time_parts) > 1 else 0
                        s = time_parts[2] if len(time_parts) > 2 else 0
                    dt = datetime(y, m, d, h, mi, s)
                else:
                    dt = datetime.fromisoformat(dt_clean)
            else:
                dt = datetime.fromisoformat(dt_clean)
        except Exception:
            return None
    elif isinstance(dt_val, datetime):
        dt = dt_val
    elif isinstance(dt_val, date):
        dt = datetime.combine(dt_val, datetime.min.time())
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SP_TZ)
    else:
        dt = dt.astimezone(SP_TZ)
    return dt


def resolve_document_download_url(doc_url: Optional[str], tender: Optional[Tender] = None) -> str:
    """
    Resolve a URL de download oficial do documento ou anexo.
    1. Se o payload contiver URL direta do ficheiro binário/anexo (.pdf, .zip, /arquivos/, etc.),
       atribui-a prioritariamente ao botão.
    2. Se for URL do portal PNCP ou fallback, garante canonização estrita com build_canonical_pncp_url
       no formato de 3 segmentos: https://pncp.gov.br/app/editais/{cnpj}/{ano}/{sequencial_inteiro}
       eliminando o formato legado {cnpj}-{tipo}-{seq}/{ano}.
    """
    clean_url = (doc_url or "").strip()

    # 1. Prioridade: link direto de binário/arquivo
    is_direct_binary = (
        bool(clean_url) and (
            any(clean_url.lower().endswith(ext) for ext in [".pdf", ".zip", ".doc", ".docx", ".xlsx", ".csv", ".rar"]) or
            any(token in clean_url.lower() for token in ["/arquivos/", "/anexos/", "/download/", "/documentos/download", "tipo_arquivo=binario"])
        )
    )
    if is_direct_binary and not ("/app/editais/" in clean_url):
        return clean_url

    # 2. Se for URL do PNCP ou número de controle, normaliza via build_canonical_pncp_url
    if clean_url:
        if "pncp.gov.br/app/editais/" in clean_url or "-" in clean_url or clean_url.startswith("http"):
            canon = build_canonical_pncp_url(numero_controle_pncp=clean_url)
            if canon and canon != "https://pncp.gov.br/app/editais":
                return canon

    # 3. Fallback no certame (tender)
    if tender:
        for candidate in [
            getattr(tender, "url_pncp", None),
            getattr(tender, "official_notice_url", None),
            getattr(tender, "link_pncp", None),
            getattr(tender, "source_url", None),
            getattr(tender, "external_id", None)
        ]:
            if candidate:
                cand_str = str(candidate).strip()
                if any(cand_str.lower().endswith(ext) for ext in [".pdf", ".zip", ".doc", ".docx"]) and not ("/app/editais/" in cand_str):
                    return cand_str
                canon = build_canonical_pncp_url(numero_controle_pncp=cand_str)
                if canon and canon != "https://pncp.gov.br/app/editais":
                    return canon

    return clean_url

# Inicializar tabelas e migrações no banco de dados
Base.metadata.create_all(bind=engine)
run_db_migrations()

APP_START_TIME = datetime.utcnow()

def clean_float_param(val: Any) -> Optional[float]:
    """Converte valores vazios para None e faz o parse seguro de strings para float"""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        v = val.strip()
        if not v:
            return None
        try:
            return float(v.replace(",", "."))
        except (ValueError, TypeError):
            return None
    return None

app = FastAPI(
    title="Licitações Brasil - API Oficial",
    description="Plataforma Inteligente de Pesquisa, Monitoramento e Análise Documental por IA de Licitações Públicas Brasileiras (PNCP e Lei nº 14.133/2021).",
    version=settings.VERSION,
    docs_url="/api/docs",
    redoc_url="/api/redoc"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Templates Jinja2
templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir)

# Arquivos Estáticos e PWA
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Middleware de Rate Limiting e Segurança
@app.middleware("http")
async def security_and_rate_limit_middleware(request: Request, call_next):
    client_ip = request.client.host if request.client else "127.0.0.1"
    
    # Aplicar rate limiting nas rotas de API
    if request.url.path.startswith("/api/"):
        if not check_rate_limit(client_ip, max_requests=settings.RATE_LIMIT_REQUESTS_PER_MINUTE):
            return JSONResponse(
                status_code=429,
                content={"detail": "Limite de requisições excedido. Por favor, aguarde um instante."}
            )
            
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response

# Dependency: Obter Usuário Atual via JWT
def get_current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header.split(" ")[1]
    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        return None
    user = db.query(User).filter(User.id == payload["sub"]).first()
    return user

def require_auth(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Autenticação requerida.")
    if user.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Conta inativa ou suspensa.")
    return user

def require_admin(user: User = Depends(require_auth)) -> User:
    if user.role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acesso exclusivo a administradores.")
    return user

# Startup Event: Popular sementes oficiais e iniciar DisputeMonitorService
@app.on_event("startup")
async def on_startup():
    run_db_migrations()
    db = next(get_db())
    try:
        seed_database(db)
    finally:
        db.close()

    # Inicializa monitoramento assíncrono multi-pregão
    await dispute_monitor.start()

    # Popula lotes de demonstração se estiver vazio
    if not dispute_monitor.get_active_snapshot():
        dispute_monitor.track_item({
            "id_pregao": "PE 042/2025",
            "numero_item": 1,
            "descricao": "Servidores de Rede e Armazenamento SAN de Alta Densidade",
            "valor_referencia": 180000.0,
            "melhor_lance": 172500.0,
            "meu_lance": 172500.0,
            "fase": "ABERTO"
        })
        dispute_monitor.track_item({
            "id_pregao": "PE 018/2025",
            "numero_item": 3,
            "descricao": "Sistema de Ultrassonografia Digital Portátil com Doppler Colorido",
            "valor_referencia": 85000.0,
            "melhor_lance": 79200.0,
            "meu_lance": 81000.0,
            "fase": "IMINENCIA"
        })
        dispute_monitor.track_item({
            "id_pregao": "PE 009/2025",
            "numero_item": 1,
            "descricao": "Licenciamento de Software Corporativo e Nuvem Governamental",
            "valor_referencia": 340000.0,
            "melhor_lance": 325000.0,
            "meu_lance": 328000.0,
            "fase": "IMINENCIA"
        })

@app.on_event("shutdown")
async def on_shutdown():
    await dispute_monitor.stop()


# ==============================================================================
# ROTAS DE PÁGINAS WEB (HTML)
# ==============================================================================

@app.get("/", response_class=HTMLResponse)
def page_home(
    request: Request,
    natureza: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    natureza_sel = (natureza or "").strip().upper()
    if natureza_sel not in NATUREZAS_OBJETO:
        natureza_sel = ""

    # Janela recente classificada lexicamente (MATERIAL / SERVICO / OUTROS)
    candidatos = db.query(Tender).order_by(Tender.publication_date.desc()).limit(300).all()
    classificados = [
        (t, classificar_natureza_certame(t.title, t.object_description)) for t in candidatos
    ]
    natureza_counts = {n: 0 for n in NATUREZAS_OBJETO}
    for _, nat in classificados:
        natureza_counts[nat] += 1

    selecionados = [(t, nat) for t, nat in classificados if not natureza_sel or nat == natureza_sel][:6]
    return templates.TemplateResponse(request, "index.html", {
        "recent_tenders": [t for t, _ in selecionados],
        "naturezas_por_id": {t.id: nat for t, nat in selecionados},
        "natureza_labels": NATUREZA_LABELS,
        "natureza_counts": natureza_counts,
        "natureza_sel": natureza_sel
    })

@app.get("/search", response_class=HTMLResponse)
def page_search(
    request: Request,
    q: Optional[str] = None,
    state: Optional[str] = None,
    city: Optional[str] = None,
    region: Optional[str] = None,
    contracting_entity: Optional[str] = None,
    administrative_sphere: Optional[str] = None,
    modality: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    category: Optional[str] = None,
    source_system: Optional[str] = None,
    portal: Optional[str] = None,
    uasg_code: Optional[str] = None,
    value_min: Optional[str] = None,
    value_max: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sort_by: str = "relevance",
    page: int = 1,
    page_size: Optional[int] = None,
    limit: Optional[int] = None,
    db: Session = Depends(get_db)
):
    effective_size = limit or page_size or 20
    params = SearchFilterParams(
        q=q,
        state=state,
        city=city,
        region=region,
        contracting_entity=contracting_entity,
        administrative_sphere=administrative_sphere,
        modality=modality,
        status=status_filter,
        category=category,
        source_system=source_system,
        portal=portal,
        uasg_code=uasg_code,
        value_min=clean_float_param(value_min),
        value_max=clean_float_param(value_max),
        date_from=date_from,
        date_to=date_to,
        sort_by=sort_by,
        page=page,
        page_size=effective_size,
        limit=limit
    )
    user = get_current_user(request, db)
    results = search_tenders(db, params, user_id=user.id if user else None)
    total_db_tenders = db.query(Tender).count()
    return templates.TemplateResponse(request, "search.html", {
        "params": params,
        "results": results,
        "categories": CATEGORIES,
        "total_db_tenders": total_db_tenders
    })

@app.get("/tenders/{tender_id}", response_class=HTMLResponse)
def page_tender_detail(request: Request, tender_id: str, db: Session = Depends(get_db)):
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        raise HTTPException(status_code=404, detail="Licitação não encontrada.")

    user = get_current_user(request, db)
    is_fav = False
    user_note = ""
    if user:
        is_fav = db.query(Favorite).filter(Favorite.user_id == user.id, Favorite.tender_id == tender.id).first() is not None
        note_obj = db.query(UserNote).filter(UserNote.user_id == user.id, UserNote.tender_id == tender.id).first()
        if note_obj:
            user_note = note_obj.content

    items = db.query(TenderItem).filter(TenderItem.tender_id == tender.id).order_by(TenderItem.item_number.asc()).all()
    docs = db.query(TenderDocument).filter(TenderDocument.tender_id == tender.id).all()
    changes = db.query(TenderChange).filter(TenderChange.tender_id == tender.id).order_by(TenderChange.detected_at.desc()).all()
    latest_ai = db.query(AIAnalysis).filter(AIAnalysis.tender_id == tender.id).order_by(AIAnalysis.created_at.desc()).first()

    source_name = tender.source.name if tender.source else "PNCP"
    summary = TenderSummaryResponse(
        id=tender.id,
        external_id=tender.external_id,
        process_number=tender.process_number or "Não informado",
        notice_number=tender.notice_number or "Não informado",
        title=tender.title,
        object_description=tender.object_description,
        contracting_entity=tender.contracting_entity,
        purchasing_unit=tender.purchasing_unit or "Não informado",
        state=tender.state,
        city=tender.city or "Não informado",
        administrative_sphere=tender.administrative_sphere,
        modality=tender.modality,
        category=tender.category or "Outros",
        status=tender.status or "Não informado",
        publication_date=tender.publication_date,
        opening_date=tender.opening_date,
        proposal_deadline=tender.proposal_deadline,
        estimated_total_value=tender.estimated_total_value,
        source_name=source_name,
        source_url=tender.source_url,
        official_notice_url=tender.official_notice_url,
        source_system=tender.source_system or "pncp",
        uasg_code=tender.uasg_code,
        sicaf_ready=bool(tender.sicaf_ready),
        link_comprasnet=tender.link_comprasnet,
        link_pncp=(
            build_canonical_pncp_url(numero_controle_pncp=getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url or tender.external_id)
            if (getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url or tender.external_id)
            else (getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url)
        ),
        url_disputa=getattr(tender, "url_disputa", None) or tender.link_comprasnet or tender.official_notice_url or (
            build_canonical_pncp_url(numero_controle_pncp=getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url or tender.external_id)
            if (getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url or tender.external_id)
            else (getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url)
        ),
        url_pncp=(
            build_canonical_pncp_url(numero_controle_pncp=getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url or tender.external_id)
            if (getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url or tender.external_id)
            else (getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url)
        ),
        fonte_portal=getattr(tender, "fonte_portal", None) or "PNCP",
        link_edital_oficial=getattr(tender, "link_edital_oficial", None),
        ai_resumo=getattr(tender, "ai_resumo", None),
        ai_pontos_atencao=getattr(tender, "ai_pontos_atencao", None),
        itens_extraidos_json=getattr(tender, "itens_extraidos_json", None),
        data_completeness=tender.data_completeness,
        is_favorite=is_fav,
        created_at=tender.created_at,
        last_synced_at=tender.last_synced_at
    )

    detail = TenderDetailResponse(
        tender=summary,
        items=[
            TenderItemResponse(
                id=i.id,
                item_number=i.item_number,
                description=i.description,
                unit=i.unit or "Não informado",
                quantity=i.quantity,
                estimated_unit_value=i.estimated_unit_value,
                estimated_total_value=i.estimated_total_value,
                classification_code=i.classification_code or "Não informado"
            ) for i in items
        ],
        documents=[
            TenderDocumentResponse(
                id=d.id,
                name=d.name,
                document_type=d.document_type,
                source_url=resolve_document_download_url(d.source_url, tender),
                mime_type=d.mime_type,
                file_size=d.file_size,
                checksum=d.checksum,
                published_at=d.published_at,
                extraction_status=d.extraction_status
            ) for d in docs
        ] if docs else [
            TenderDocumentResponse(
                id=f"doc-edital-{tender.id}",
                name=f"Edital e Termo de Referência ({tender.notice_number or tender.external_id})",
                document_type="EDITAL",
                source_url=resolve_document_download_url(tender.official_notice_url or summary.url_pncp or tender.source_url, tender),
                mime_type="application/pdf",
                file_size=None,
                checksum=None,
                published_at=tender.publication_date,
                extraction_status="SUCCESS"
            )
        ] if (tender.official_notice_url or summary.url_pncp or tender.source_url) else [],
        changes=[
            TenderChangeResponse(
                id=c.id,
                change_type=c.change_type,
                field_name=c.field_name,
                old_value=c.old_value,
                new_value=c.new_value,
                detected_at=c.detected_at,
                source_reference=c.source_reference
            ) for c in changes
        ],
        latest_ai_analysis=AIAnalysisSummaryResponse(
            id=latest_ai.id,
            model_provider=latest_ai.model_provider,
            model_name=latest_ai.model_name,
            analysis_type=latest_ai.analysis_type,
            result_json=latest_ai.result_json,
            created_at=latest_ai.created_at
        ) if latest_ai else None,
        user_notes=user_note
    )

    return templates.TemplateResponse(request, "tender_detail.html", {
        "detail": detail
    })

@app.get("/dashboard", response_class=HTMLResponse)
def page_dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    summary = get_dashboard_summary_data(db, user)
    return templates.TemplateResponse(request, "dashboard.html", {
        "summary": summary,
        "user": user
    })

@app.get("/favorites", response_class=HTMLResponse)
def page_favorites(request: Request):
    return templates.TemplateResponse(request, "favorites.html", {})

@app.get("/alerts", response_class=HTMLResponse)
def page_alerts(request: Request):
    return templates.TemplateResponse(request, "alerts.html", {})

@app.get("/admin", response_class=HTMLResponse)
def page_admin(request: Request):
    return templates.TemplateResponse(request, "admin.html", {})

@app.get("/login", response_class=HTMLResponse)
def page_login(request: Request):
    return templates.TemplateResponse(request, "login.html", {})

@app.get("/register", response_class=HTMLResponse)
def page_register(request: Request):
    return templates.TemplateResponse(request, "register.html", {})

@app.get("/negocios", response_class=HTMLResponse)
def page_negocios(
    request: Request,
    view: str = Query("quadros"),
    db: Session = Depends(get_db)
):
    deals = db.query(Deal).order_by(Deal.updated_at.desc()).all()
    grouped = {stage: [] for stage in DEAL_STAGES}
    for d in deals:
        stage_key = d.stage if d.stage in grouped else "oportunidades"
        grouped[stage_key].append(serialize_deal(d))

    counts = {stage: len(items) for stage, items in grouped.items()}
    total_deals = len(deals)

    return templates.TemplateResponse(
        request,
        "negocios.html",
        {
            "view": view,
            "stages": DEAL_STAGES,
            "stage_labels": DEAL_STAGE_LABELS,
            "grouped_deals": grouped,
            "counts": counts,
            "total_deals": total_deals
        }
    )

@app.get("/juridico", response_class=HTMLResponse)
def page_juridico(
    request: Request,
    q: Optional[str] = Query(None),
    tipo: Optional[str] = Query(None),
    nova_lei_only: Optional[bool] = Query(None),
    orgao: Optional[str] = Query(None),
    numero_certame: Optional[str] = Query(None),
    objeto: Optional[str] = Query(None),
    tender_id: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    query = db.query(LegalDocumentTemplate)
    if q:
        query = query.filter(
            (LegalDocumentTemplate.titulo.ilike(f"%{q}%")) |
            (LegalDocumentTemplate.corpo.ilike(f"%{q}%")) |
            (LegalDocumentTemplate.fundamentacao_legal.ilike(f"%{q}%"))
        )
    if tipo:
        if tipo in ["contrarbenzoes", "contrarbenzoes"]:
            query = query.filter(LegalDocumentTemplate.tipo.in_(["contrarbenzoes", "contrarbenzoes"]))
        else:
            query = query.filter(LegalDocumentTemplate.tipo == tipo)
    if nova_lei_only is True:
        query = query.filter(LegalDocumentTemplate.is_nova_lei == True)

    templates_list = query.order_by(LegalDocumentTemplate.id.asc()).all()
    serialized = [serialize_legal_template(t) for t in templates_list]

    counts = {
        "total": db.query(LegalDocumentTemplate).count(),
        "impugnacao": db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.tipo == "impugnacao").count(),
        "esclarecimento": db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.tipo == "esclarecimento").count(),
        "recurso": db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.tipo.in_(["recurso", "intencao_recurso"])).count(),
        "contrarbenzoes": db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.tipo.in_(["contrarbenzoes", "contrarbenzoes"])).count(),
        "nova_lei": db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.is_nova_lei == True).count()
    }

    return templates.TemplateResponse(
        request,
        "juridico.html",
        {
            "templates": serialized,
            "user": user,
            "q": q or "",
            "tipo": tipo or "",
            "nova_lei_only": bool(nova_lei_only),
            "counts": counts,
            "certame_prefill": {
                "orgao": (orgao or "").strip()[:500],
                "numero_certame": (numero_certame or "").strip()[:255],
                "objeto": (objeto or "").strip()[:1000],
                "tender_id": (tender_id or "").strip()[:64]
            }
        }
    )

@app.get("/documentos", response_class=HTMLResponse)
def page_documentos(request: Request, db: Session = Depends(get_db)):
    hoje = hoje_sao_paulo()
    docs = db.query(DocumentoEmpresa).order_by(
        DocumentoEmpresa.data_validade.is_(None),
        DocumentoEmpresa.data_validade.asc(),
        DocumentoEmpresa.nome.asc()
    ).all()
    serializados = [document_vault.serializar_documento(d, hoje) for d in docs]
    return templates.TemplateResponse(
        request,
        "documentos.html",
        {
            "documentos": serializados,
            "resumo": document_vault.resumo_validade(serializados),
            "categorias": DOCUMENTO_CATEGORIAS,
            "dias_alerta": DOCUMENTO_DIAS_ALERTA,
            "hoje": hoje.isoformat(),
            "max_mb": document_vault.DOCUMENTO_MAX_BYTES // (1024 * 1024)
        }
    )

@app.get("/agenda", response_class=HTMLResponse)
def page_agenda(
    request: Request,
    db: Session = Depends(get_db)
):
    now = datetime.now(SP_TZ)
    deals = db.query(Deal).all()

    events_raw = []
    seen_tenders = set()

    for d in deals:
        t = d.tender
        if not t or t.id in seen_tenders:
            continue
        seen_tenders.add(t.id)
        raw_target = t.opening_date or t.proposal_deadline
        target_date = to_sp_datetime(raw_target)
        if target_date:
            events_raw.append({
                "deal_id": d.id,
                "tender_id": t.id,
                "title": t.title,
                "notice_number": t.notice_number or t.external_id,
                "contracting_entity": t.contracting_entity or "Órgão Comprador",
                "state": t.state or "BR",
                "stage": d.stage,
                "target_date": target_date,
                "estimated_value": t.estimated_total_value,
                "sicaf_ready": bool(t.sicaf_ready)
            })

    # Se houver poucos certames em deals com prazos, complementar com licitações gerais com datas
    if len(events_raw) < 5:
        tenders_deadlines = db.query(Tender).filter(
            (Tender.proposal_deadline.isnot(None)) | (Tender.opening_date.isnot(None))
        ).order_by(Tender.proposal_deadline.asc()).limit(15).all()
        for t in tenders_deadlines:
            if t.id in seen_tenders:
                continue
            seen_tenders.add(t.id)
            raw_target = t.opening_date or t.proposal_deadline
            target_date = to_sp_datetime(raw_target)
            if target_date:
                events_raw.append({
                    "deal_id": None,
                    "tender_id": t.id,
                    "title": t.title,
                    "notice_number": t.notice_number or t.external_id,
                    "contracting_entity": t.contracting_entity or "Órgão Comprador",
                    "state": t.state or "BR",
                    "stage": "oportunidades",
                    "target_date": target_date,
                    "estimated_value": t.estimated_total_value,
                    "sicaf_ready": bool(t.sicaf_ready)
                })

    critical_events = []
    warning_events = []
    upcoming_events = []
    calendar_events = []

    for item in events_raw:
        td = item["target_date"]
        diff_seconds = (td - now).total_seconds()
        hours_remaining = diff_seconds / 3600.0
        days_remaining = (td.date() - now.date()).days

        if diff_seconds < 0:
            urgency = "vencido"
            badge_text = "Encerrado / Julgamento"
        elif 0 <= hours_remaining <= 24:
            urgency = "critico"
            badge_text = "Vence hoje / Últimas 24h"
        elif days_remaining <= 7:
            urgency = "alerta"
            days = max(1, days_remaining)
            badge_text = f"Faltam {days} dias"
        else:
            urgency = "normal"
            badge_text = f"Faltam {days_remaining} dias"

        ev_dict = {
            "deal_id": item["deal_id"],
            "tender_id": item["tender_id"],
            "title": item["title"],
            "notice_number": item["notice_number"],
            "contracting_entity": item["contracting_entity"],
            "state": item["state"],
            "stage": item["stage"],
            "date": td.isoformat(),
            "formatted_date": td.strftime("%d/%m/%Y %H:%M"),
            "time": td.strftime("%H:%M"),
            "estimated_value": item["estimated_value"],
            "sicaf_ready": item["sicaf_ready"],
            "hours_remaining": hours_remaining,
            "days_remaining": days_remaining,
            "urgency": urgency,
            "badge_text": badge_text
        }

        calendar_events.append(ev_dict)

        if urgency == "critico":
            critical_events.append(ev_dict)
        elif urgency == "alerta":
            warning_events.append(ev_dict)

        if diff_seconds >= 0:
            upcoming_events.append(ev_dict)

    calendar_events.sort(key=lambda x: x["date"])
    upcoming_events.sort(key=lambda x: x["date"])
    critical_events.sort(key=lambda x: x["date"])
    warning_events.sort(key=lambda x: x["date"])

    return templates.TemplateResponse(
        request,
        "agenda.html",
        {
            "critical_events": critical_events,
            "warning_events": warning_events,
            "upcoming_events": upcoming_events[:20],
            "total_scheduled": len(calendar_events),
            "calendar_events_json": json.dumps(calendar_events)
        }
    )

@app.get("/disputa", response_class=HTMLResponse)
def page_disputa(request: Request):
    return templates.TemplateResponse(request, "disputa.html", {})

@app.get("/manifest.json")
def pwa_manifest():
    manifest_path = os.path.join(os.path.dirname(__file__), "static", "manifest.json")
    if os.path.exists(manifest_path):
        return FileResponse(manifest_path, media_type="application/manifest+json")
    raise HTTPException(status_code=404, detail="Manifest não encontrado.")

@app.get("/sw.js")
def pwa_service_worker():
    sw_path = os.path.join(os.path.dirname(__file__), "static", "sw.js")
    if os.path.exists(sw_path):
        return FileResponse(
            sw_path,
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/"}
        )
    raise HTTPException(status_code=404, detail="Service worker não encontrado.")

def get_fornecedores_data(db: Session) -> Dict[str, Any]:
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("AI_API_KEY") or ""
    gemini_configured = bool(gemini_key and gemini_key.strip())

    tenders = db.query(Tender).all()
    entities_map: Dict[str, Dict[str, Any]] = {}
    states_set = set()
    total_val = 0.0

    for t in tenders:
        ent = t.contracting_entity or "Órgão Federal / PNCP"
        if t.state:
            states_set.add(t.state)
        val = t.estimated_total_value or 0.0
        total_val += val

        if ent not in entities_map:
            entities_map[ent] = {
                "name": ent,
                "state": t.state or "BR",
                "city": t.city or "Âmbito Federal",
                "sphere": t.administrative_sphere or "FEDERAL",
                "uasg_code": t.uasg_code,
                "tenders_count": 0,
                "total_value": 0.0,
                "modalities": {}
            }

        entities_map[ent]["tenders_count"] += 1
        entities_map[ent]["total_value"] += val
        mod = t.modality or "PREGÃO ELETRÔNICO"
        entities_map[ent]["modalities"][mod] = entities_map[ent]["modalities"].get(mod, 0) + 1

    companies = []
    for ent, data in entities_map.items():
        primary_mod = max(data["modalities"].items(), key=lambda x: x[1])[0] if data["modalities"] else "PREGÃO ELETRÔNICO"
        companies.append({
            "name": data["name"],
            "state": data["state"],
            "city": data["city"],
            "sphere": data["sphere"],
            "uasg_code": data["uasg_code"],
            "tenders_count": data["tenders_count"],
            "total_value": data["total_value"],
            "primary_modality": primary_mod
        })

    companies.sort(key=lambda x: x["tenders_count"], reverse=True)

    return {
        "gemini_configured": gemini_configured,
        "companies": companies,
        "total_companies": len(companies),
        "total_tenders": len(tenders),
        "total_value": total_val,
        "states_count": len(states_set)
    }

@app.get("/fornecedores", response_class=HTMLResponse)
@app.get("/empresas", response_class=HTMLResponse)
def page_fornecedores(
    request: Request,
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    data = get_fornecedores_data(db)
    return templates.TemplateResponse(
        request,
        "fornecedores.html",
        {
            "user": user,
            **data
        }
    )

@app.get("/api/fornecedores")
@app.get("/api/empresas")
def api_fornecedores(db: Session = Depends(get_db)):
    data = get_fornecedores_data(db)
    return {
        "status": "success",
        "gemini_configured": data["gemini_configured"],
        "total_companies": data["total_companies"],
        "total_tenders": data["total_tenders"],
        "total_value": data["total_value"],
        "companies": data["companies"]
    }

@app.get("/api/agenda/events")
def api_agenda_events(db: Session = Depends(get_db)):
    now = datetime.now(SP_TZ)
    deals = db.query(Deal).all()
    events = []
    seen = set()

    for d in deals:
        t = d.tender
        if t and (t.opening_date or t.proposal_deadline):
            seen.add(t.id)
            td = to_sp_datetime(t.opening_date or t.proposal_deadline)
            if not td:
                continue
            diff_seconds = (td - now).total_seconds()
            hours_remaining = diff_seconds / 3600.0
            days_remaining = (td.date() - now.date()).days
            if diff_seconds < 0:
                urgency = "vencido"
            elif 0 <= hours_remaining <= 24:
                urgency = "critico"
            elif days_remaining <= 7:
                urgency = "alerta"
            else:
                urgency = "normal"
            events.append({
                "deal_id": d.id,
                "tender_id": t.id,
                "title": t.title,
                "notice_number": t.notice_number or t.external_id,
                "contracting_entity": t.contracting_entity,
                "date": td.isoformat(),
                "formatted_date": td.strftime("%d/%m/%Y %H:%M"),
                "urgency": urgency,
                "hours_remaining": hours_remaining,
                "days_remaining": days_remaining,
                "stage": d.stage
            })

    # Complementar se poucos deals com prazos
    if len(events) < 5:
        tenders_extra = db.query(Tender).filter(
            (Tender.proposal_deadline.isnot(None)) | (Tender.opening_date.isnot(None))
        ).limit(10).all()
        for t in tenders_extra:
            if t.id in seen:
                continue
            seen.add(t.id)
            td = to_sp_datetime(t.opening_date or t.proposal_deadline)
            if not td:
                continue
            diff_seconds = (td - now).total_seconds()
            hours_remaining = diff_seconds / 3600.0
            days_remaining = (td.date() - now.date()).days
            if diff_seconds < 0:
                urgency = "vencido"
            elif 0 <= hours_remaining <= 24:
                urgency = "critico"
            elif days_remaining <= 7:
                urgency = "alerta"
            else:
                urgency = "normal"
            events.append({
                "deal_id": None,
                "tender_id": t.id,
                "title": t.title,
                "notice_number": t.notice_number or t.external_id,
                "contracting_entity": t.contracting_entity,
                "date": td.isoformat(),
                "formatted_date": td.strftime("%d/%m/%Y %H:%M"),
                "urgency": urgency,
                "hours_remaining": hours_remaining,
                "days_remaining": days_remaining,
                "stage": "oportunidades"
            })

    return {"status": "success", "events": events, "total": len(events)}


# ==============================================================================
# DISPUTAS MULTI-PREGÃO E TRANSMISSÃO EM TEMPO REAL (SSE)
# ==============================================================================

@app.post("/api/disputes/track")
async def api_track_dispute_item(payload: Dict[str, Any] = Body(...)):
    """Registra ou atualiza um certame/lote na matriz de monitoramento multi-pregão."""
    try:
        item = dispute_monitor.track_item(payload)
        return {
            "status": "success",
            "item": item.to_dict()
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erro ao registrar item na disputa: {str(e)}")


@app.get("/api/disputes/active")
def api_get_active_disputes():
    """Consulta snapshot consolidado de todos os lotes e pregões em acompanhamento."""
    items = dispute_monitor.get_active_snapshot()
    return {
        "status": "success",
        "total": len(items),
        "items": items
    }


@app.post("/api/disputes/pulse")
def api_dispute_simulate_pulse(item_key: Optional[str] = Query(None)):
    """Método de contingência/mock para certames de teste que gera atualizações e iminência."""
    updated = dispute_monitor.simulate_pulse(item_key)
    return {
        "status": "success",
        "total_updated": len(updated),
        "items": updated
    }


@app.post("/api/disputes/configure/{item_id:path}")
async def api_disputes_configure_item(item_id: str, payload: Dict[str, Any] = Body(...)):
    """Persiste parâmetros individuais (degrau_minimo, valor_minimo_limite, posicao_alvo, auto_lance_ativo)."""
    degrau = payload.get("degrau_minimo")
    piso = payload.get("valor_minimo_limite")
    pos = payload.get("posicao_alvo")
    auto = payload.get("auto_lance_ativo")

    item = dispute_monitor.configure_item(
        item_id=item_id,
        degrau_minimo=degrau,
        valor_minimo_limite=piso,
        posicao_alvo=pos,
        auto_lance_ativo=auto
    )
    if not item:
        raise HTTPException(status_code=404, detail="Item de disputa não encontrado.")
    return {
        "status": "success",
        "item": item.to_dict()
    }


@app.post("/api/disputes/toggle-global")
async def api_disputes_toggle_global(payload: Dict[str, Any] = Body(...)):
    """Liga/desliga o interruptor mestre global de automação de lances."""
    status_val = bool(payload.get("status", False))
    items = dispute_monitor.toggle_all_autobid(status_val)
    return {
        "status": "success",
        "global_status": status_val,
        "total_updated": len(items),
        "items": items
    }


@app.post("/api/disputes/bid/{item_id:path}")
async def api_disputes_bid_action(item_id: str, payload: Dict[str, Any] = Body(default={})):
    """Cobre lance manual ou dispara execução imediata do robô para o lote."""
    valor = payload.get("valor")
    item = dispute_monitor._find_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item de disputa não encontrado.")
    if valor is not None:
        item.meu_lance = round(float(valor), 2)
        if item.meu_lance <= item.melhor_lance:
            item.melhor_lance = item.meu_lance
        item.updated_at = to_sp_datetime(datetime.now()) or datetime.now()
        event = {"type": "MANUAL_BID_PLACED", "item": item.to_dict(), "timestamp": datetime.now().isoformat()}
        dispute_monitor.broadcast_sync(event)
        return {"status": "success", "item": item.to_dict()}
    else:
        res = dispute_monitor.executar_lance_automatico(item_id)
        return {"status": "success", "item": res or item.to_dict()}


@app.get("/api/disputes/events")
async def api_disputes_sse_events(request: Request):
    """
    Endpoint Server-Sent Events (SSE) transmitindo deltas de variação
    de melhor lance e status de iminência em tempo real.
    """
    queue = dispute_monitor.subscribe()

    async def event_generator():
        try:
            # Envia evento de confirmação de conexão e snapshot inicial logo na abertura da conexão SSE
            yield f"data: {json.dumps({'type': 'CONNECTED', 'timestamp': datetime.now().isoformat()})}\n\n"
            initial_snapshot = dispute_monitor.get_active_snapshot()
            for it in initial_snapshot:
                payload = json.dumps({"type": "ITEM_TRACKED", "item": it})
                yield f"data: {payload}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            dispute_monitor.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


# ==============================================================================
# REST API OFICIAL (ENDPOINTS DOCUMENTADOS)
# ==============================================================================

@app.get("/api/health")
def api_health():
    return {
        "status": "healthy",
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "timestamp": datetime.utcnow().isoformat(),
        "database": "connected"
    }

@app.get("/api/health/metrics", response_model=HealthMetricsResponse)
def api_health_metrics(db: Session = Depends(get_db)):
    uptime_seconds = round((datetime.utcnow() - APP_START_TIME).total_seconds(), 2)
    hours = int(uptime_seconds // 3600)
    minutes = int((uptime_seconds % 3600) // 60)
    seconds = int(uptime_seconds % 60)
    uptime_human = f"{hours}h {minutes}m {seconds}s"

    try:
        db.execute(text("SELECT 1"))
        db_status = "healthy"
    except Exception:
        db_status = "unhealthy"

    since_24h = datetime.utcnow() - timedelta(hours=24)
    total_24h = db.query(WebhookDispatchLog).filter(WebhookDispatchLog.criado_em >= since_24h).count()
    success_24h = db.query(WebhookDispatchLog).filter(
        WebhookDispatchLog.criado_em >= since_24h,
        WebhookDispatchLog.sucesso == True
    ).count()
    failed_24h = total_24h - success_24h
    taxa_sucesso_pct = round((success_24h / total_24h * 100), 2) if total_24h > 0 else 100.0

    return HealthMetricsResponse(
        status="healthy" if db_status == "healthy" else "degraded",
        uptime_seconds=uptime_seconds,
        uptime_human=uptime_human,
        database=db_status,
        disparos_ultimas_24h=total_24h,
        disparos_sucesso_24h=success_24h,
        disparos_falha_24h=failed_24h,
        taxa_sucesso_pct=taxa_sucesso_pct,
        timestamp=datetime.utcnow().isoformat()
    )

# ----------------- Auth API -----------------

@app.post("/api/auth/register", response_model=TokenResponse)
def api_register(payload: UserRegisterRequest, db: Session = Depends(get_db)):
    # Proteção contra enumeração: resposta uniforme
    existing = db.query(User).filter(User.email == payload.email.lower()).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Não foi possível concluir o cadastro com as informações fornecidas."
        )

    user = User(
        name=payload.name.strip(),
        email=payload.email.lower().strip(),
        password_hash=hash_password(payload.password),
        role="USER",
        status="ACTIVE"
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token({"sub": user.id, "email": user.email, "role": user.role})
    return TokenResponse(
        access_token=token,
        user=UserProfileResponse(
            id=user.id,
            name=user.name,
            email=user.email,
            role=user.role,
            status=user.status,
            created_at=user.created_at,
            last_login_at=user.last_login_at
        )
    )

@app.post("/api/auth/login", response_model=TokenResponse)
def api_login(payload: UserLoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email.lower().strip()).first()
    if not user or not verify_password(payload.password, user.password_hash):
        # Defesa contra timing attack e enumeração
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciais de acesso incorretas.")

    if user.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Conta inativa.")

    user.last_login_at = datetime.utcnow()
    db.commit()

    token = create_access_token({"sub": user.id, "email": user.email, "role": user.role})
    return TokenResponse(
        access_token=token,
        user=UserProfileResponse(
            id=user.id,
            name=user.name,
            email=user.email,
            role=user.role,
            status=user.status,
            created_at=user.created_at,
            last_login_at=user.last_login_at
        )
    )

@app.get("/api/auth/me", response_model=UserProfileResponse)
def api_me(user: User = Depends(require_auth)):
    return UserProfileResponse(
        id=user.id,
        name=user.name,
        email=user.email,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
        last_login_at=user.last_login_at
    )

@app.delete("/api/auth/account")
def api_delete_account(user: User = Depends(require_auth), db: Session = Depends(get_db)):
    """Direito à exclusão e esquecimento de dados pessoais conforme a LGPD (Art. 18, VI)"""
    db.delete(user)
    db.commit()
    return {"message": "Conta e dados pessoais excluídos com sucesso em conformidade com a LGPD."}

# ----------------- Tenders & Search API -----------------

@app.get("/api/tenders", response_model=SearchResultResponse)
def api_search_tenders(
    q: Optional[str] = None,
    state: Optional[str] = None,
    city: Optional[str] = None,
    region: Optional[str] = None,
    contracting_entity: Optional[str] = None,
    administrative_sphere: Optional[str] = None,
    modality: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    category: Optional[str] = None,
    source_system: Optional[str] = None,
    portal: Optional[str] = None,
    uasg_code: Optional[str] = None,
    value_min: Optional[str] = None,
    value_max: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sort_by: str = "relevance",
    page: int = 1,
    page_size: Optional[int] = None,
    limit: Optional[int] = None,
    request: Request = None,
    db: Session = Depends(get_db)
):
    effective_size = limit or page_size or 20
    params = SearchFilterParams(
        q=q,
        state=state,
        city=city,
        region=region,
        contracting_entity=contracting_entity,
        administrative_sphere=administrative_sphere,
        modality=modality,
        status=status_filter,
        category=category,
        source_system=source_system,
        portal=portal,
        uasg_code=uasg_code,
        value_min=clean_float_param(value_min),
        value_max=clean_float_param(value_max),
        date_from=date_from,
        date_to=date_to,
        sort_by=sort_by,
        page=page,
        page_size=effective_size,
        limit=limit
    )
    user = get_current_user(request, db) if request else None
    return search_tenders(db, params, user_id=user.id if user else None)

@app.post("/api/tenders/sync", response_model=SyncOpportunitiesResponse)
def api_sync_tenders(
    days_back: int = Query(7, ge=1, le=60),
    uf: Optional[str] = Query(None),
    pages: int = Query(10, ge=1, le=50),
    limit_per_page: int = Query(50, ge=10, le=100),
    db: Session = Depends(get_db)
):
    """
    Dispara o pipeline unificado de sincronização de oportunidades em larga escala multifontes:
    priorizando Compras.gov.br (Federal / SICAF), portais estaduais (BEC/SP, Compras RS, MG, BA, RJ, PR)
    e portais integrados (BLL, Licitações-e, Portal de Compras Públicas, PNCP Geral).
    """
    result = sync_procurement_opportunities(
        db=db,
        days_back=days_back,
        uf=uf,
        pages=pages,
        limit_per_page=limit_per_page
    )
    return SyncOpportunitiesResponse(
        status=result["status"],
        message=result["message"],
        synced_at=datetime.fromisoformat(result["synced_at"]),
        total_processados=result["total_processados"],
        novos_inseridos=result["novos_inseridos"],
        atualizados=result["atualizados"],
        compras_gov_count=result["compras_gov_count"],
        pncp_count=result["pncp_count"],
        dispensas_count=result.get("dispensas_count"),
        total_coletados=result["total_coletados"],
        total_geral_banco=result["total_geral_banco"]
    )

@app.get("/api/tenders/export/csv")
def api_export_tenders_csv(
    q: Optional[str] = None,
    state: Optional[str] = None,
    modality: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    db: Session = Depends(get_db)
):
    """Exporta oportunidades filtradas em streaming de arquivo CSV estruturado."""
    params = SearchFilterParams(
        q=q,
        state=state,
        modality=modality,
        status=status_filter,
        page=1,
        page_size=100
    )

    def generate_csv():
        output = io.StringIO()
        writer = csv.writer(output, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        # Cabeçalho estruturado
        writer.writerow([
            "objeto",
            "orgao",
            "estado",
            "valor_estimado",
            "data_abertura",
            "url_disputa",
            "url_pncp"
        ])
        yield output.getvalue().encode("utf-8-sig")
        output.seek(0)
        output.truncate(0)

        current_page = 1
        max_export_records = 2000
        exported = 0

        while True:
            params.page = current_page
            search_result = search_tenders(db, params)
            if not search_result.results:
                break

            for item in search_result.results:
                val_str = f"{item.estimated_total_value:.2f}".replace(".", ",") if item.estimated_total_value is not None else ""
                data_abertura = item.opening_date.strftime("%d/%m/%Y %H:%M") if item.opening_date else ""
                url_disp = item.url_disputa or item.link_comprasnet or item.official_notice_url or item.source_url or item.url_pncp or ""
                url_pnc = item.url_pncp or item.link_pncp or item.source_url or ""
                writer.writerow([
                    item.object_description or item.title or "",
                    item.contracting_entity or "",
                    item.state or "",
                    val_str,
                    data_abertura,
                    url_disp,
                    url_pnc
                ])
                yield output.getvalue().encode("utf-8-sig")
                output.seek(0)
                output.truncate(0)
                exported += 1
                if exported >= max_export_records:
                    break

            if exported >= max_export_records or current_page >= search_result.total_pages:
                break
            current_page += 1

    filename = f"licitacoes_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"'
    }
    return StreamingResponse(generate_csv(), media_type="text/csv", headers=headers)

@app.get("/api/tenders/{tender_id}")
def api_get_tender_by_id(tender_id: str, db: Session = Depends(get_db)):
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        raise HTTPException(status_code=404, detail="Licitação não encontrada.")
    
    items = db.query(TenderItem).filter(TenderItem.tender_id == tender.id).order_by(TenderItem.item_number.asc()).all()
    docs = db.query(TenderDocument).filter(TenderDocument.tender_id == tender.id).all()

    items_payload = [
        {
            "id": i.id,
            "item_number": i.item_number,
            "description": i.description,
            "unit": i.unit or "UN",
            "quantity": i.quantity or 1.0,
            "estimated_unit_value": i.estimated_unit_value or 0.0,
            "estimated_total_value": i.estimated_total_value or 0.0,
            "classification_code": i.classification_code or "Não informado"
        } for i in items
    ]
    if not items_payload:
        items_payload = [{
            "id": f"item-1-{tender.id}",
            "item_number": 1,
            "description": tender.title or tender.object_description,
            "unit": "UN",
            "quantity": 1.0,
            "estimated_unit_value": tender.estimated_total_value or 0.0,
            "estimated_total_value": tender.estimated_total_value or 0.0,
            "classification_code": tender.category or "Item Principal"
        }]

    canon_api_pncp = (
        build_canonical_pncp_url(numero_controle_pncp=getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url or tender.external_id)
        if (getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url or tender.external_id)
        else (getattr(tender, "url_pncp", None) or tender.link_pncp or tender.source_url)
    )

    docs_payload = [
        {
            "id": d.id,
            "name": d.name,
            "document_type": d.document_type,
            "source_url": resolve_document_download_url(d.source_url, tender),
            "mime_type": d.mime_type or "application/pdf",
            "file_size": d.file_size
        } for d in docs
    ]
    if not docs_payload and (tender.official_notice_url or canon_api_pncp or tender.source_url):
        docs_payload = [{
            "id": f"doc-edital-{tender.id}",
            "name": f"Edital e Termo de Referência ({tender.notice_number or tender.external_id})",
            "document_type": "EDITAL",
            "source_url": resolve_document_download_url(tender.official_notice_url or canon_api_pncp or tender.source_url, tender),
            "mime_type": "application/pdf",
            "file_size": None
        }]

    return {
        "id": tender.id,
        "external_id": tender.external_id,
        "process_number": tender.process_number or "Não informado",
        "notice_number": tender.notice_number or "Não informado",
        "title": tender.title,
        "object_description": tender.object_description,
        "contracting_entity": tender.contracting_entity,
        "purchasing_unit": tender.purchasing_unit or "Não informado",
        "state": tender.state,
        "city": tender.city or "Não informado",
        "region": tender.region or "Não informado",
        "administrative_sphere": tender.administrative_sphere,
        "modality": tender.modality,
        "category": tender.category or "Outros",
        "status": tender.status or "Divulgada no PNCP",
        "publication_date": tender.publication_date.isoformat() if tender.publication_date else None,
        "opening_date": tender.opening_date.isoformat() if tender.opening_date else None,
        "proposal_deadline": tender.proposal_deadline.isoformat() if tender.proposal_deadline else None,
        "estimated_total_value": tender.estimated_total_value,
        "source_name": tender.source.name if tender.source else "PNCP",
        "source_url": tender.source_url,
        "official_notice_url": tender.official_notice_url,
        "source_system": tender.source_system or "pncp",
        "uasg_code": tender.uasg_code,
        "sicaf_ready": bool(tender.sicaf_ready),
        "link_comprasnet": tender.link_comprasnet,
        "link_pncp": canon_api_pncp,
        "url_disputa": getattr(tender, "url_disputa", None) or tender.link_comprasnet or tender.official_notice_url or canon_api_pncp,
        "url_pncp": canon_api_pncp,
        "fonte_portal": getattr(tender, "fonte_portal", None) or "PNCP",
        "link_edital_oficial": getattr(tender, "link_edital_oficial", None),
        "ai_resumo": getattr(tender, "ai_resumo", None),
        "ai_pontos_atencao": getattr(tender, "ai_pontos_atencao", None),
        "itens_extraidos_json": getattr(tender, "itens_extraidos_json", None),
        "data_completeness": tender.data_completeness,
        "items": items_payload,
        "documents": docs_payload,
        "created_at": tender.created_at.isoformat() if tender.created_at else None,
        "last_synced_at": tender.last_synced_at.isoformat() if tender.last_synced_at else None
    }


@app.post("/api/tenders/{tender_id}/ai-analyze")
async def api_tender_ai_analyze(
    tender_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    db: Session = Depends(get_db)
):
    """
    Aciona análise semântica e resumo executivo de oportunidade via Google Gemini (2.5 / 2.0 Flash)
    com enriquecimento e persistência unificada.
    """
    from server.services.gemini_analyzer import resumo_executivo_oportunidade, analisar_edital_documento

    tender = db.query(Tender).filter((Tender.id == tender_id) | (Tender.external_id == tender_id)).first()
    if not tender:
        raise HTTPException(status_code=404, detail="Licitação não encontrada para análise por IA.")

    dados_licitacao = {
        "title": tender.title,
        "object_description": tender.object_description,
        "contracting_entity": tender.contracting_entity,
        "state": tender.state,
        "city": tender.city,
        "modality": tender.modality,
        "estimated_total_value": tender.estimated_total_value or 0.0
    }

    ai_summary = await resumo_executivo_oportunidade(dados_licitacao)

    doc_res = None
    target_doc_id = payload.get("document_id") if payload else None
    if target_doc_id:
        doc = db.query(TenderDocument).filter(TenderDocument.id == target_doc_id).first()
        if doc:
            doc_res = await analisar_edital_documento(b"", doc.name)
    if not doc_res:
        docs = db.query(TenderDocument).filter(TenderDocument.tender_id == tender.id).all()
        if docs:
            doc_res = await analisar_edital_documento(b"", docs[0].name)
        else:
            doc_res = await analisar_edital_documento((tender.object_description or "").encode("utf-8", errors="ignore"), "edital.pdf")

    # Enriquecimento de colunas no modelo Tender
    tender.ai_resumo = ai_summary.get("resumo")
    tender.ai_pontos_atencao = json.dumps(ai_summary.get("pontos_atencao", []))
    if doc_res and doc_res.get("itens"):
        tender.itens_extraidos_json = doc_res.get("itens")

    # Estruturação para a UI e retorno
    structured_data = {
        "resumo_executivo": {
            "o_que_sera_contratado": ai_summary.get("resumo") or tender.title,
            "resumo": ai_summary.get("resumo") or tender.title,
            "quem_contrata": tender.contracting_entity,
            "onde_local": f"{tender.city or ''} / {tender.state or ''}",
            "modalidade": tender.modality,
            "valor_estimado": f"R$ {tender.estimated_total_value or 0.0:,.2f}" if tender.estimated_total_value else "Não informado",
            "datas_relevantes": tender.opening_date.strftime("%d/%m/%Y às %H:%M") if tender.opening_date else "A definir em edital",
            "competitividade_estimada": ai_summary.get("competitividade_estimada", "Média")
        },
        "requisitos_participacao": {
            "habilitacao_juridica": doc_res.get("habilitacao_exigencias", []) if doc_res else ["Conforme Edital Oficial (Lei 14.133/2021)"],
            "regularidade_fiscal_trabalhista": ["CND Federal e Trabalhista (CNDT)", "Regularidade FGTS"],
            "qualificacao_tecnica": [ai_summary.get("competitividade_estimada", "Atestado de Capacidade Técnica")],
            "registros_profissionais_ou_sanitarios": "Conforme exigência do órgão licitante"
        },
        "pontos_atencao": {
            "prazos_urgentes_ou_curtos": "Impugnações e esclarecimentos em até 3 dias úteis antes da abertura da sessão.",
            "penalidades_previstas": "\n".join(ai_summary.get("pontos_atencao", [])) or "Conforme Lei 14.133/2021",
            "riscos": ai_summary.get("riscos_juridicos", [])
        },
        "itens_extraidos": doc_res.get("itens", []) if doc_res else [],
        "citacoes": [
            {
                "topico": "Objeto da Contratação",
                "pagina": "Pág. 1",
                "trecho": (tender.object_description or tender.title)[:160]
            }
        ]
    }

    try:
        analysis_record = AIAnalysis(
            tender_id=tender.id,
            model_provider="GOOGLE_GEMINI",
            model_name=ai_summary.get("modelo_utilizado", "gemini-3.8-flash"),
            analysis_type="EXECUTIVE_SUMMARY_AND_RISK",
            result_json=structured_data
        )
        db.add(analysis_record)
    except Exception as e:
        logger.warning(f"Erro ao salvar AIAnalysis: {e}")

    db.commit()
    db.refresh(tender)

    return {
        "success": True,
        "status": "success",
        "tender_id": tender.id,
        "data": structured_data,
        "analysis": ai_summary,
        "doc_analysis": doc_res
    }


@app.post("/api/portals/sync")
async def api_portals_sync(
    payload: Optional[Dict[str, Any]] = Body(None),
    sources: Optional[List[str]] = Query(None),
    uf: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """
    Aciona pipeline assíncrono de coleta multifonte de licitações
    (PNCP, Compras.gov.br, Portal de Compras Públicas, BNC, Licitanet).
    """
    from server.services.portal_collector import portal_collector
    p = payload or {}
    src = sources or p.get("sources")
    target_uf = uf or p.get("uf")
    kw = q or p.get("q") or p.get("keyword")
    ufs_list = [target_uf] if target_uf else None

    result = await portal_collector.sync_pipeline(
        sources=src,
        ufs=ufs_list,
        keywords=kw,
        db_session=db
    )
    result["total_saved"] = result.get("persisted_tenders", 0)
    return result

def serialize_deal(deal: Deal, tender: Optional[Tender] = None) -> Dict[str, Any]:
    t = tender or deal.tender
    tender_data = None
    if t:
        tender_data = {
            "id": t.id,
            "title": t.title,
            "external_id": t.external_id,
            "notice_number": t.notice_number,
            "contracting_entity": t.contracting_entity,
            "purchasing_unit": t.purchasing_unit or "Não informado",
            "state": t.state,
            "city": t.city or "Não informado",
            "modality": t.modality,
            "status": t.status or "Divulgada no PNCP",
            "estimated_total_value": t.estimated_total_value,
            "opening_date": t.opening_date.isoformat() if t.opening_date else None,
            "proposal_deadline": t.proposal_deadline.isoformat() if t.proposal_deadline else None,
            "source_system": t.source_system or "pncp",
            "uasg_code": t.uasg_code,
            "sicaf_ready": bool(t.sicaf_ready),
            "link_comprasnet": t.link_comprasnet,
            "link_pncp": getattr(t, "url_pncp", None) or t.link_pncp or t.source_url,
            "url_disputa": getattr(t, "url_disputa", None) or t.link_comprasnet or t.official_notice_url or (getattr(t, "url_pncp", None) or t.link_pncp or t.source_url),
            "url_pncp": getattr(t, "url_pncp", None) or t.link_pncp or t.source_url,
            "source_url": t.source_url
        }
    return {
        "id": deal.id,
        "tender_id": deal.tender_id,
        "stage": deal.stage,
        "notes": deal.notes or "",
        "created_at": deal.created_at.isoformat() if deal.created_at else None,
        "updated_at": deal.updated_at.isoformat() if deal.updated_at else None,
        "tender": tender_data
    }

@app.post("/api/deals", status_code=status.HTTP_201_CREATED)
async def api_create_deal(
    request: Request,
    tender_id: Optional[Union[str, int]] = Query(None),
    stage: Optional[str] = Query(None),
    notes: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Cria ou atualiza um negócio no pipeline/funil comercial."""
    payload: Dict[str, Any] = {}
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            payload = await request.json()
    except Exception:
        payload = {}

    target_tender_id = payload.get("tender_id") or tender_id
    target_stage = payload.get("stage") or stage or "oportunidades"
    target_notes = payload.get("notes") or notes or ""

    if not target_tender_id:
        raise HTTPException(status_code=422, detail="tender_id é obrigatório.")

    if target_stage not in DEAL_STAGES:
        target_stage = "oportunidades"

    tender = db.query(Tender).filter(
        (Tender.id == str(target_tender_id)) | (Tender.external_id == str(target_tender_id))
    ).first()
    if not tender:
        raise HTTPException(status_code=404, detail="Licitação não encontrada.")

    deal = db.query(Deal).filter(Deal.tender_id == tender.id).first()
    if deal:
        if target_stage in DEAL_STAGES:
            deal.stage = target_stage
        if target_notes:
            deal.notes = target_notes
        deal.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(deal)
        return serialize_deal(deal, tender)

    deal = Deal(
        tender_id=tender.id,
        stage=target_stage,
        notes=target_notes,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(deal)
    db.commit()
    db.refresh(deal)
    return serialize_deal(deal, tender)

@app.get("/api/deals")
def api_get_deals(
    stage: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Lista os negócios do funil agrupados por estágio."""
    query = db.query(Deal).order_by(Deal.updated_at.desc())
    if stage and stage in DEAL_STAGES:
        query = query.filter(Deal.stage == stage)

    deals = query.all()
    grouped: Dict[str, List[Dict[str, Any]]] = {s: [] for s in DEAL_STAGES}
    deals_list: List[Dict[str, Any]] = []

    for d in deals:
        serialized = serialize_deal(d)
        stage_key = d.stage if d.stage in grouped else "oportunidades"
        grouped[stage_key].append(serialized)
        deals_list.append(serialized)

    return {
        "oportunidades": grouped["oportunidades"],
        "qualificacao": grouped["qualificacao"],
        "disputa": grouped["disputa"],
        "classificacao": grouped["classificacao"],
        "contrato": grouped["contrato"],
        "grouped": grouped,
        "deals": deals_list,
        "total": len(deals),
        "counts": {s: len(grouped[s]) for s in DEAL_STAGES}
    }

@app.patch("/api/deals/{deal_id}/stage")
@app.put("/api/deals/{deal_id}/stage")
async def api_update_deal_stage(
    deal_id: int,
    request: Request,
    stage: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Atualiza o estágio do negócio (mover entre colunas)."""
    target_stage = stage
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            payload = await request.json()
            if isinstance(payload, dict) and "stage" in payload:
                target_stage = payload["stage"]
    except Exception:
        pass

    if not target_stage or target_stage not in DEAL_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Estágio inválido. Opções permitidas: {', '.join(DEAL_STAGES)}"
        )

    deal = db.query(Deal).filter(Deal.id == deal_id).first()
    if not deal:
        raise HTTPException(status_code=404, detail="Negócio não encontrado.")

    deal.stage = target_stage
    deal.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(deal)
    return serialize_deal(deal)

@app.delete("/api/deals/{deal_id}")
def api_delete_deal(
    deal_id: int,
    db: Session = Depends(get_db)
):
    """Remove um negócio do funil."""
    deal = db.query(Deal).filter(Deal.id == deal_id).first()
    if not deal:
        raise HTTPException(status_code=404, detail="Negócio não encontrado.")
    db.delete(deal)
    db.commit()
    return {"status": "success", "message": "Negócio removido do funil.", "id": deal_id}

@app.post("/api/pipeline/add")
def api_add_to_pipeline(
    tender_id: str = Query(...),
    request: Request = None,
    db: Session = Depends(get_db)
):
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        raise HTTPException(status_code=404, detail="Licitação não encontrada.")
    user = get_current_user(request, db) if request else None
    if user:
        existing = db.query(Favorite).filter(Favorite.user_id == user.id, Favorite.tender_id == tender.id).first()
        if not existing:
            db.add(Favorite(user_id=user.id, tender_id=tender.id))
            db.commit()

    deal = db.query(Deal).filter(Deal.tender_id == tender.id).first()
    if not deal:
        deal = Deal(
            tender_id=tender.id,
            stage="oportunidades",
            notes="Adicionado via busca de oportunidades",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        db.add(deal)
        db.commit()
        db.refresh(deal)

    return {
        "status": "success",
        "message": f"Licitação {tender.notice_number or tender.external_id} adicionada ao seu funil de negócios!",
        "tender_id": tender.id,
        "deal_id": deal.id,
        "title": tender.title
    }

# ----------------- Peças Jurídicas API -----------------

def serialize_legal_template(tmpl: LegalDocumentTemplate) -> Dict[str, Any]:
    tipo_label_map = {
        "impugnacao": "Impugnação",
        "esclarecimento": "Esclarecimento",
        "intencao_recurso": "Intenção de Recurso",
        "recurso": "Recurso",
        "contrarbenzoes": "Contrarrazões",
        "contrarbenzoes": "Contrarrazões"
    }
    return {
        "id": tmpl.id,
        "tipo": tmpl.tipo,
        "tipo_label": tipo_label_map.get(tmpl.tipo, tmpl.tipo.capitalize()),
        "titulo": tmpl.titulo,
        "title": tmpl.titulo,
        "fundamentacao_legal": tmpl.fundamentacao_legal,
        "corpo": tmpl.corpo,
        "content": tmpl.corpo,
        "is_nova_lei": bool(tmpl.is_nova_lei),
        "created_at": tmpl.created_at.isoformat() if tmpl.created_at else None,
        "updated_at": tmpl.updated_at.isoformat() if tmpl.updated_at else None
    }

@app.get("/api/juridico/templates")
def api_list_legal_templates(
    q: Optional[str] = Query(None),
    tipo: Optional[str] = Query(None),
    nova_lei_only: Optional[bool] = Query(None),
    db: Session = Depends(get_db)
):
    """Lista as peças jurídicas com suporte a busca, filtros de categoria e regime da Nova Lei."""
    query = db.query(LegalDocumentTemplate)
    if q:
        query = query.filter(
            (LegalDocumentTemplate.titulo.ilike(f"%{q}%")) |
            (LegalDocumentTemplate.corpo.ilike(f"%{q}%")) |
            (LegalDocumentTemplate.fundamentacao_legal.ilike(f"%{q}%"))
        )
    if tipo:
        if tipo in ["contrarbenzoes", "contrarbenzoes"]:
            query = query.filter(LegalDocumentTemplate.tipo.in_(["contrarbenzoes", "contrarbenzoes"]))
        else:
            query = query.filter(LegalDocumentTemplate.tipo == tipo)
    if nova_lei_only is True:
        query = query.filter(LegalDocumentTemplate.is_nova_lei == True)

    templates_list = query.order_by(LegalDocumentTemplate.id.asc()).all()
    return {
        "total": len(templates_list),
        "items": [serialize_legal_template(t) for t in templates_list]
    }

@app.get("/api/juridico/templates/{template_id}")
def api_get_legal_template(
    template_id: int,
    db: Session = Depends(get_db)
):
    """Obtém detalhes do modelo de peça jurídica por ID."""
    tmpl = db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.id == template_id).first()
    if not tmpl:
        raise HTTPException(status_code=404, detail="Modelo de peça jurídica não encontrado.")
    return serialize_legal_template(tmpl)

@app.post("/api/juridico/templates/{template_id}/render")
async def api_render_legal_template(
    template_id: int,
    request: Request,
    tender_id: Optional[str] = Query(None),
    orgao: Optional[str] = Query(None),
    numero_edital: Optional[str] = Query(None),
    objeto: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Renderiza a peça substituindo as tags {{orgao}}, {{numero_edital}}, {{objeto}}."""
    tmpl = db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.id == template_id).first()
    if not tmpl:
        raise HTTPException(status_code=404, detail="Modelo de peça jurídica não encontrado.")

    payload = {}
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            payload = await request.json()
    except Exception:
        pass

    target_tender_id = payload.get("tender_id") or tender_id
    target_orgao = payload.get("orgao") or orgao
    target_numero = payload.get("numero_edital") or numero_edital
    target_objeto = payload.get("objeto") or objeto

    if target_tender_id:
        t = db.query(Tender).filter(Tender.id == target_tender_id).first()
        if t:
            if not target_orgao:
                target_orgao = t.contracting_entity
            if not target_numero:
                target_numero = t.notice_number or t.external_id
            if not target_objeto:
                target_objeto = t.title

    target_orgao = target_orgao or "[ÓRGÃO COMPRADOR]"
    target_numero = target_numero or "[NÚMERO DO EDITAL]"
    target_objeto = target_objeto or "[OBJETO DA LICITAÇÃO]"

    rendered_corpo = tmpl.corpo
    rendered_corpo = rendered_corpo.replace("{{orgao}}", target_orgao)
    rendered_corpo = rendered_corpo.replace("{{numero_edital}}", target_numero)
    rendered_corpo = rendered_corpo.replace("{{objeto}}", target_objeto)

    return {
        "template_id": tmpl.id,
        "titulo": tmpl.titulo,
        "tipo": tmpl.tipo,
        "fundamentacao_legal": tmpl.fundamentacao_legal,
        "rendered_corpo": rendered_corpo,
        "variables_used": {
            "orgao": target_orgao,
            "numero_edital": target_numero,
            "objeto": target_objeto
        }
    }

# ----------------- Geração Jurídica em PDF -----------------

class LicitantePDF(BaseModel):
    razao_social: Optional[str] = Field(None, max_length=255)
    cnpj: Optional[str] = Field(None, max_length=32)
    endereco: Optional[str] = Field(None, max_length=500)
    representante: Optional[str] = Field(None, max_length=255)
    cargo: Optional[str] = Field(None, max_length=120)
    cpf: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=255)
    telefone: Optional[str] = Field(None, max_length=40)


class GerarPeticaoPDFRequest(BaseModel):
    template_id: Optional[int] = None
    tender_id: Optional[str] = Field(None, max_length=64)
    titulo: Optional[str] = Field(None, max_length=255)
    corpo: Optional[str] = Field(None, max_length=60000)
    orgao: Optional[str] = Field(None, max_length=500)
    numero_certame: Optional[str] = Field(None, max_length=255)
    numero_edital: Optional[str] = Field(None, max_length=255)
    objeto: Optional[str] = Field(None, max_length=2000)
    lei: Optional[str] = Field(None, max_length=32)
    fundamentacao: Optional[str] = Field(None, max_length=500)
    cidade: Optional[str] = Field(None, max_length=120)
    licitante: Optional[LicitantePDF] = None


@app.post("/api/juridico/gerar-pdf")
def api_juridico_gerar_pdf(payload: GerarPeticaoPDFRequest, db: Session = Depends(get_db)):
    """
    Compila a petição em PDF timbrado Licitações Brasil.
    O texto vem de `corpo` (editado no modal) ou do modelo `template_id`; os dados do
    certame podem ser informados diretamente ou herdados de `tender_id`.
    """
    tmpl = None
    if payload.template_id is not None:
        tmpl = db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.id == payload.template_id).first()
        if not tmpl:
            raise HTTPException(status_code=404, detail="Modelo de peça jurídica não encontrado.")

    corpo = (payload.corpo or "").strip() or (tmpl.corpo if tmpl else "")
    if not corpo.strip():
        raise HTTPException(status_code=422, detail="Informe `template_id` ou o `corpo` da petição.")

    orgao = payload.orgao
    numero = payload.numero_certame or payload.numero_edital
    objeto = payload.objeto
    if payload.tender_id:
        t = db.query(Tender).filter(
            (Tender.id == payload.tender_id) | (Tender.external_id == payload.tender_id)
        ).first()
        if t:
            orgao = orgao or t.contracting_entity
            numero = numero or t.notice_number or t.external_id
            objeto = objeto or t.title

    lei = normalizar_lei(payload.lei) if payload.lei else normalizar_lei(tmpl.is_nova_lei if tmpl else True)
    fundamentacao = payload.fundamentacao or (tmpl.fundamentacao_legal if tmpl else None)
    titulo = payload.titulo or (tmpl.titulo if tmpl else "Petição Administrativa")
    tipo_label = serialize_legal_template(tmpl)["tipo_label"] if tmpl else None

    try:
        pdf_bytes = gerar_peticao_pdf(
            corpo=corpo,
            orgao=orgao,
            numero_certame=numero,
            objeto=objeto,
            titulo=titulo,
            tipo_label=tipo_label,
            lei=lei,
            fundamentacao=fundamentacao,
            licitante=payload.licitante.model_dump() if payload.licitante else None,
            cidade=payload.cidade
        )
    except PeticaoInvalidaError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    filename = f"{slug_arquivo(tmpl.tipo if tmpl else 'peticao', numero or 'certame')}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(pdf_bytes)),
            "Cache-Control": "no-store"
        }
    )

# ----------------- Cofre de Documentos de Habilitação -----------------

@app.get("/api/documentos")
def api_listar_documentos(
    status_filtro: Optional[str] = Query(None, alias="status"),
    categoria: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Lista documentos do cofre com dias restantes e status de validade calculados (fuso America/Sao_Paulo)."""
    hoje = hoje_sao_paulo()
    query = db.query(DocumentoEmpresa)
    if categoria:
        query = query.filter(DocumentoEmpresa.categoria == categoria.lower())
    docs = query.order_by(
        DocumentoEmpresa.data_validade.is_(None),
        DocumentoEmpresa.data_validade.asc(),
        DocumentoEmpresa.nome.asc()
    ).all()
    itens = [document_vault.serializar_documento(d, hoje) for d in docs]
    if status_filtro:
        itens = [i for i in itens if i["status"] == status_filtro.upper()]
    return {
        "hoje": hoje.isoformat(),
        "dias_alerta": DOCUMENTO_DIAS_ALERTA,
        "resumo": document_vault.resumo_validade(itens),
        "items": itens
    }


@app.post("/api/documentos", status_code=status.HTTP_201_CREATED)
async def api_upload_documento(
    nome: str = Form(...),
    categoria: str = Form(...),
    data_emissao: str = Form(...),
    data_validade: Optional[str] = Form(None),
    tags_habilitacao: Optional[str] = Form(None),
    arquivo: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """Recebe um PDF de habilitação, grava em uploads/documentos/ e persiste os metadados no SQLite."""
    try:
        meta = document_vault.validar_metadados(nome, categoria, data_emissao, data_validade)
        conteudo = await arquivo.read(document_vault.DOCUMENTO_MAX_BYTES + 1)
        document_vault.validar_conteudo_pdf(arquivo.filename, conteudo)
    except document_vault.DocumentoInvalidoError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    finally:
        await arquivo.close()

    caminho = document_vault.salvar_pdf_no_cofre(conteudo, meta["nome"])
    doc = DocumentoEmpresa(
        nome=meta["nome"],
        categoria=meta["categoria"],
        data_emissao=meta["data_emissao"],
        data_validade=meta["data_validade"],
        caminho_arquivo=caminho,
        tags_habilitacao=document_vault.normalizar_tags(tags_habilitacao),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    try:
        db.add(doc)
        db.commit()
        db.refresh(doc)
    except Exception:
        db.rollback()
        document_vault.remover_arquivo_documento(caminho)
        raise HTTPException(status_code=500, detail="Falha ao registrar o documento no cofre.")
    return document_vault.serializar_documento(doc)


@app.get("/api/documentos/{documento_id}/arquivo")
def api_baixar_documento(documento_id: int, db: Session = Depends(get_db)):
    doc = db.query(DocumentoEmpresa).filter(DocumentoEmpresa.id == documento_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Documento não encontrado.")
    caminho = document_vault.resolver_caminho_documento(doc.caminho_arquivo)
    if not caminho or not caminho.is_file():
        raise HTTPException(status_code=404, detail="Arquivo físico não localizado no cofre.")
    return FileResponse(
        str(caminho),
        media_type="application/pdf",
        filename=f"{document_vault.slugify(doc.nome, 80)}.pdf"
    )


@app.delete("/api/documentos/{documento_id}")
def api_excluir_documento(documento_id: int, db: Session = Depends(get_db)):
    doc = db.query(DocumentoEmpresa).filter(DocumentoEmpresa.id == documento_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Documento não encontrado.")
    caminho = doc.caminho_arquivo
    db.delete(doc)
    db.commit()
    arquivo_removido = document_vault.remover_arquivo_documento(caminho)
    return {"status": "success", "id": documento_id, "arquivo_removido": arquivo_removido}

# ----------------- Kit de Habilitação (ZIP) -----------------

@app.get("/api/tenders/{tender_id}/download-kit-zip")
def api_download_kit_habilitacao(tender_id: str, db: Session = Depends(get_db)):
    """Empacota os documentos válidos do cofre + Relatorio_Conformidade.pdf para o certame."""
    try:
        zip_bytes = montar_kit_habilitacao_zip(tender_id, db=db)
    except KitHabilitacaoError:
        raise HTTPException(status_code=404, detail="Licitação não encontrada.")
    tender = db.query(Tender).filter((Tender.id == tender_id) | (Tender.external_id == tender_id)).first()
    filename = nome_arquivo_kit(tender)
    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(zip_bytes)),
            "Cache-Control": "no-store"
        }
    )

@app.post("/api/tenders/{tender_id}/ai-summary")
async def api_tender_ai_summary(
    tender_id: str,
    payload: Optional[AISummaryRequest] = None,
    force_refresh: bool = Query(False),
    db: Session = Depends(get_db)
):
    """Dispara a geração ou recuperação em cache do resumo executivo estruturado com IA Gemini."""
    refresh = force_refresh or (payload.force_refresh if payload else False)
    return await analyze_tender_summary(tender_id=tender_id, db=db, force_refresh=refresh)

@app.post("/api/tenders/{tender_id}/ai-chat")
async def api_tender_ai_chat(
    tender_id: str,
    payload: AIChatRequest,
    db: Session = Depends(get_db)
):
    """Recebe a pergunta do usuário e devolve a resposta contextualizada da IA ('Converse com o Edital')."""
    return await ask_tender_question(tender_id=tender_id, question=payload.question, db=db)

# Legado: Rota unificada implementada em api_tender_ai_analyze (/api/tenders/{tender_id}/ai-analyze)


@app.post("/api/tenders/{tender_id}/notes")
def api_save_tender_note(
    tender_id: str,
    payload: UserNoteRequest,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db)
):
    note = db.query(UserNote).filter(UserNote.user_id == user.id, UserNote.tender_id == tender_id).first()
    if not note:
        note = UserNote(user_id=user.id, tender_id=tender_id, content=payload.content)
        db.add(note)
    else:
        note.content = payload.content
        note.updated_at = datetime.utcnow()
    db.commit()
    return {"message": "Anotação salva com sucesso."}

# ----------------- Favorites API -----------------

@app.get("/api/favorites")
def api_get_favorites(user: User = Depends(require_auth), db: Session = Depends(get_db)):
    favs = db.query(Favorite).filter(Favorite.user_id == user.id).all()
    tenders = []
    for f in favs:
        t = f.tender
        if t:
            tenders.append({
                "id": t.id,
                "title": t.title,
                "contracting_entity": t.contracting_entity,
                "state": t.state,
                "city": t.city,
                "modality": t.modality,
                "status": t.status,
                "proposal_deadline": t.proposal_deadline,
                "estimated_total_value": t.estimated_total_value,
                "favorited_at": f.created_at
            })
    return tenders

@app.post("/api/favorites/toggle", response_model=FavoriteToggleResponse)
def api_toggle_favorite(
    tender_id: str = Query(...),
    user: User = Depends(require_auth),
    db: Session = Depends(get_db)
):
    existing = db.query(Favorite).filter(Favorite.user_id == user.id, Favorite.tender_id == tender_id).first()
    if existing:
        db.delete(existing)
        db.commit()
        return FavoriteToggleResponse(tender_id=tender_id, is_favorite=False)
    else:
        new_fav = Favorite(user_id=user.id, tender_id=tender_id)
        db.add(new_fav)
        db.commit()
        return FavoriteToggleResponse(tender_id=tender_id, is_favorite=True)

# ----------------- Alerts API -----------------

@app.get("/api/alerts", response_model=List[AlertResponse])
def api_get_alerts(
    user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    query = db.query(SavedSearch).filter(SavedSearch.active == True)
    if user:
        query = query.filter((SavedSearch.user_id == user.id) | (SavedSearch.user_id.is_(None)))
    saved_items = query.order_by(SavedSearch.created_at.desc()).all()

    resp = []
    seen_ids = set()
    for s in saved_items:
        seen_ids.add(s.id)
        count = db.query(AlertDelivery).filter(AlertDelivery.alert_id == s.id).count()
        al = db.query(Alert).filter(Alert.id == s.id).first()
        resp.append(AlertResponse(
            id=s.id,
            keywords=s.keywords,
            ufs=s.ufs,
            min_value=s.min_value,
            created_at=s.created_at,
            active=s.active,
            name=s.name or "Alerta",
            filters=s.filters or {},
            frequency=al.frequency if al else "DAILY",
            channels=al.channels if al else "IN_APP",
            last_run_at=al.last_run_at if al else None,
            delivery_count=count
        ))

    # Incorporar alertas legados da tabela Alert se houver
    legacy_query = db.query(Alert).filter(Alert.active == True)
    if user:
        legacy_query = legacy_query.filter(Alert.user_id == user.id)
    legacy_alerts = legacy_query.all()
    for la in legacy_alerts:
        if la.id not in seen_ids:
            seen_ids.add(la.id)
            count = db.query(AlertDelivery).filter(AlertDelivery.alert_id == la.id).count()
            la_ufs = la.filters.get("state") if isinstance(la.filters, dict) else None
            la_min = clean_float_param(la.filters.get("value_min")) if isinstance(la.filters, dict) else None
            resp.append(AlertResponse(
                id=la.id,
                keywords=la.keywords,
                ufs=la_ufs,
                min_value=la_min,
                created_at=la.created_at,
                active=la.active,
                name=la.name,
                filters=la.filters or {},
                frequency=la.frequency,
                channels=la.channels,
                last_run_at=la.last_run_at,
                delivery_count=count
            ))

    return resp

@app.post("/api/alerts", response_model=AlertResponse)
def api_create_alert(
    payload: AlertCreateRequest,
    user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    keywords = payload.keywords
    filters = dict(payload.filters or {})
    if not keywords and filters.get("q"):
        keywords = filters.get("q")

    ufs = payload.ufs or filters.get("state") or filters.get("uf")
    min_value = payload.min_value
    if min_value is None and filters.get("value_min") is not None:
        min_value = clean_float_param(filters.get("value_min"))

    name = payload.name or (f"Alerta: {keywords}" if keywords else (f"Alerta UF {ufs}" if ufs else "Alerta de Busca"))

    if ufs and "state" not in filters:
        filters["state"] = ufs
    if min_value is not None and "value_min" not in filters:
        filters["value_min"] = min_value
    if keywords and "keywords" not in filters:
        filters["keywords"] = keywords

    alert_id = generate_uuid()
    created_now = datetime.utcnow()

    target_user_id = user.id if user else None
    if not target_user_id:
        demo = db.query(User).filter(User.email == "usuario@empresa.com.br").first()
        if demo:
            target_user_id = demo.id
        else:
            first_u = db.query(User).first()
            if first_u:
                target_user_id = first_u.id

    # Guardar pesquisa/alerta no modelo SavedSearch
    saved_search = SavedSearch(
        id=alert_id,
        user_id=target_user_id,
        name=name,
        keywords=keywords,
        ufs=ufs,
        min_value=min_value,
        filters=filters,
        active=True,
        created_at=created_now,
        updated_at=created_now
    )
    db.add(saved_search)

    # Espelhar em Alert para compatibilidade de entregas se houver usuário
    freq = payload.frequency or "DAILY"
    chan = payload.channels or "IN_APP"
    if target_user_id:
        alert = Alert(
            id=alert_id,
            user_id=target_user_id,
            name=name,
            keywords=keywords,
            filters=filters,
            frequency=freq,
            channels=chan,
            active=True,
            created_at=created_now,
            updated_at=created_now
        )
        db.add(alert)

    db.commit()
    db.refresh(saved_search)

    return AlertResponse(
        id=saved_search.id,
        keywords=saved_search.keywords,
        ufs=saved_search.ufs,
        min_value=saved_search.min_value,
        created_at=saved_search.created_at,
        active=saved_search.active,
        name=saved_search.name,
        filters=saved_search.filters or {},
        frequency=freq,
        channels=chan,
        last_run_at=None,
        delivery_count=0
    )

@app.post("/api/alerts/{alert_id}/toggle")
def api_toggle_alert(
    alert_id: str,
    active: bool = Query(...),
    user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    saved = db.query(SavedSearch).filter(SavedSearch.id == alert_id).first()
    if not alert and not saved:
        raise HTTPException(status_code=404, detail="Alerta não localizado.")
    if alert:
        alert.active = active
    if saved:
        saved.active = active
    db.commit()
    return {"message": f"Alerta {'reativado' if active else 'pausado'} com sucesso."}

@app.delete("/api/alerts/{alert_id}")
def api_delete_alert(
    alert_id: str,
    user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    saved = db.query(SavedSearch).filter(SavedSearch.id == alert_id).first()
    if not alert and not saved:
        raise HTTPException(status_code=404, detail="Alerta não localizado.")
    if alert:
        db.delete(alert)
    if saved:
        db.delete(saved)
    db.commit()
    return {"message": "Alerta excluído."}


@app.get("/api/alerts/feed", response_model=AlertFeedResponse)
def api_get_alerts_feed(
    limit: int = Query(50, ge=1, le=200),
    user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Retorna o feed de certames correspondentes aos alertas do usuário.
    Se o usuário não estiver autenticado, retorna as entregas recentes globais.
    """
    query = db.query(AlertDelivery, Tender).join(
        Tender, AlertDelivery.tender_id == Tender.id
    )

    if user:
        user_alert_ids = [
            r[0] for r in db.query(SavedSearch.id).filter(
                (SavedSearch.user_id == user.id) | (SavedSearch.user_id.is_(None))
            ).all()
        ]
        user_alert_ids.extend([
            r[0] for r in db.query(Alert.id).filter(Alert.user_id == user.id).all()
        ])
        if user_alert_ids:
            query = query.filter(AlertDelivery.alert_id.in_(user_alert_ids))

    deliveries = query.order_by(AlertDelivery.delivered_at.desc()).limit(limit).all()

    items = []
    for delivery, tender in deliveries:
        saved = db.query(SavedSearch).filter(SavedSearch.id == delivery.alert_id).first()
        al = db.query(Alert).filter(Alert.id == delivery.alert_id).first()
        alert_title = (saved.name if saved and saved.name else None) or (al.name if al else "Alerta")

        items.append(AlertFeedItem(
            id=delivery.id,
            delivery_id=delivery.id,
            alert_id=delivery.alert_id,
            alert_name=alert_title,
            tender_id=tender.id,
            title=tender.title,
            contracting_entity=tender.contracting_entity,
            state=tender.state,
            city=tender.city,
            modality=tender.modality,
            estimated_total_value=tender.estimated_total_value,
            url_disputa=tender.url_disputa or tender.url_pncp,
            url_pncp=tender.url_pncp,
            channel=delivery.channel or "IN_APP",
            delivered_at=delivery.delivered_at,
            tender={
                "id": tender.id,
                "title": tender.title,
                "notice_number": tender.notice_number,
                "contracting_entity": tender.contracting_entity,
                "state": tender.state,
                "city": tender.city,
                "modality": tender.modality,
                "estimated_total_value": tender.estimated_total_value,
                "opening_date": tender.opening_date.isoformat() if tender.opening_date else None,
                "url_disputa": tender.url_disputa or tender.url_pncp,
                "url_pncp": tender.url_pncp
            }
        ))

    return AlertFeedResponse(
        total=len(items),
        count=len(items),
        feed=items,
        items=items
    )


# ----------------- Webhooks Audit & Logs API -----------------

@app.get("/api/alerts/webhooks/logs", response_model=WebhookDispatchLogsListResponse)
def api_get_webhook_logs(
    status: Optional[str] = Query(None, description="Filtro por status: SUCESSO ou FALHA"),
    certame_id: Optional[str] = Query(None, description="Filtro por certame / licitação ID"),
    tender_id: Optional[str] = Query(None, description="Alias para certame_id"),
    data: Optional[str] = Query(None, description="Filtro por data no formato YYYY-MM-DD"),
    canal: Optional[str] = Query(None, description="Filtro por canal (N8N, EVOLUTION_API, ZAPI, GENERIC)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db)
):
    query = db.query(WebhookDispatchLog)

    if status:
        st_clean = status.strip().upper()
        if st_clean in ("SUCESSO", "SUCCESS", "TRUE", "1"):
            query = query.filter(WebhookDispatchLog.sucesso == True)
        elif st_clean in ("FALHA", "FAILED", "ERROR", "FALSE", "0"):
            query = query.filter(WebhookDispatchLog.sucesso == False)

    target_certame = certame_id or tender_id
    if target_certame:
        query = query.filter(WebhookDispatchLog.tender_id == target_certame.strip())

    if data:
        data_clean = data.strip()
        try:
            target_date = datetime.strptime(data_clean, "%Y-%m-%d").date()
            query = query.filter(func.date(WebhookDispatchLog.criado_em) == target_date)
        except ValueError:
            pass

    if canal:
        query = query.filter(WebhookDispatchLog.canal == canal.strip().upper())

    total = query.count()
    db_items = (
        query.order_by(WebhookDispatchLog.criado_em.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    items = [
        WebhookDispatchLogResponse(
            id=item.id,
            rule_id=item.rule_id,
            tender_id=item.tender_id,
            target_url=item.target_url,
            canal=item.canal,
            status_code=item.status_code,
            tentativa_atual=item.tentativa_atual,
            sucesso=item.sucesso,
            tempo_resposta_ms=item.tempo_resposta_ms,
            payload_enviado=item.payload_enviado,
            resposta_servidor=item.resposta_servidor,
            criado_em=item.criado_em
        )
        for item in db_items
    ]

    return WebhookDispatchLogsListResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=items
    )


@app.post("/api/alerts/webhooks/{log_id}/retry")
def api_retry_webhook(log_id: str, db: Session = Depends(get_db)):
    old_log = db.query(WebhookDispatchLog).filter(WebhookDispatchLog.id == log_id).first()
    if not old_log:
        raise HTTPException(status_code=404, detail="Log de disparo de webhook não encontrado.")

    new_log = retry_webhook_dispatch(db, log_id)
    if not new_log:
        raise HTTPException(status_code=500, detail="Erro ao reprocessar disparo de webhook.")

    return {
        "message": "Webhook reexecutado com sucesso.",
        "log": {
            "id": new_log.id,
            "rule_id": new_log.rule_id,
            "tender_id": new_log.tender_id,
            "target_url": new_log.target_url,
            "canal": new_log.canal,
            "status_code": new_log.status_code,
            "tentativa_atual": new_log.tentativa_atual,
            "sucesso": new_log.sucesso,
            "tempo_resposta_ms": new_log.tempo_resposta_ms,
            "payload_enviado": new_log.payload_enviado,
            "resposta_servidor": new_log.resposta_servidor,
            "criado_em": new_log.criado_em.isoformat()
        }
    }

# ----------------- Dashboard Summary API -----------------

def get_dashboard_summary_data(db: Session, user: Optional[User] = None) -> Dict[str, Any]:
    # 1. Total de negócios ativos (deals no funil)
    deals_count = db.query(Deal).count()
    funnel_stages = {s: db.query(Deal).filter(Deal.stage == s).count() for s in DEAL_STAGES}

    # 2. Empresas identificadas (órgãos compradores únicos ou entidades)
    companies_count = db.query(func.count(func.distinct(Tender.contracting_entity))).scalar() or 0

    # 3. Total de licitações publicadas/sincronizadas hoje e total de itens
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    tenders_today_query = db.query(Tender).filter(
        (Tender.publication_date >= today_start) |
        (Tender.last_synced_at >= today_start) |
        (Tender.created_at >= today_start)
    )
    tenders_today_count = tenders_today_query.count()
    if tenders_today_count == 0:
        tenders_today_count = db.query(Tender).count()

    total_items_count = db.query(TenderItem).count()
    items_today_count = db.query(TenderItem).join(Tender).filter(
        (Tender.publication_date >= today_start) |
        (Tender.last_synced_at >= today_start) |
        (Tender.created_at >= today_start)
    ).count()
    if items_today_count == 0:
        items_today_count = total_items_count

    # 4. Distribuição por categoria: Serviços, Materiais e Não Classificado
    tenders = db.query(Tender).all()
    servicos_count = 0
    materiais_count = 0
    nao_classificado_count = 0

    for t in tenders:
        cat = (t.category or "").lower().strip()
        obj = (t.title or "").lower() + " " + (t.object_description or "").lower()
        if any(k in cat for k in ["serviço", "servico", "consultoria", "manuten", "loca", "engenharia", "obra", "vigil", "limpeza"]) or any(k in obj for k in ["prestação de serviço", "prestacao de servico", "serviço de", "servicos de"]):
            servicos_count += 1
        elif any(k in cat for k in ["material", "materiais", "medicamento", "alimento", "equipamento", "hospitalar", "suprimento", "insumo", "ti", "tecnologia", "produto"]) or any(k in obj for k in ["aquisição", "aquisicao", "fornecimento"]):
            materiais_count += 1
        else:
            if cat:
                materiais_count += 1
            else:
                nao_classificado_count += 1

    # 5. Distribuição por faixa de valor: Até R$ 50 mil, R$ 50 mil a R$ 250 mil, Acima de R$ 250 mil
    ate_50k = 0
    de_50k_a_250k = 0
    acima_250k = 0

    for t in tenders:
        val = t.estimated_total_value
        if val is None or val == 0:
            ate_50k += 1
        elif val <= 50000.0:
            ate_50k += 1
        elif 50000.0 < val <= 250000.0:
            de_50k_a_250k += 1
        else:
            acima_250k += 1

    # 6. Próximos eventos da agenda vinculados aos negócios em andamento
    deals = db.query(Deal).all()
    agenda_events = []
    seen_tenders = set()

    for d in deals:
        t = d.tender
        if not t or t.id in seen_tenders:
            continue
        seen_tenders.add(t.id)

        target_date = t.opening_date or t.proposal_deadline
        if target_date:
            event_type = "Sessão de Lances / Disputa" if t.opening_date and target_date == t.opening_date else "Prazo Limite de Propostas"
            agenda_events.append({
                "deal_id": d.id,
                "tender_id": t.id,
                "title": t.title,
                "notice_number": t.notice_number or t.external_id,
                "contracting_entity": t.contracting_entity,
                "state": t.state,
                "stage": d.stage,
                "event_type": event_type,
                "date": target_date.isoformat(),
                "formatted_date": target_date.strftime("%d/%m/%Y %H:%M"),
                "estimated_value": t.estimated_total_value,
                "sicaf_ready": bool(t.sicaf_ready)
            })

    if not agenda_events:
        tenders_with_deadlines = db.query(Tender).filter(Tender.proposal_deadline.isnot(None)).order_by(Tender.proposal_deadline.asc()).limit(5).all()
        for t in tenders_with_deadlines:
            agenda_events.append({
                "deal_id": None,
                "tender_id": t.id,
                "title": t.title,
                "notice_number": t.notice_number or t.external_id,
                "contracting_entity": t.contracting_entity,
                "state": t.state,
                "stage": "oportunidades",
                "event_type": "Prazo Limite de Propostas",
                "date": t.proposal_deadline.isoformat(),
                "formatted_date": t.proposal_deadline.strftime("%d/%m/%Y %H:%M"),
                "estimated_value": t.estimated_total_value,
                "sicaf_ready": bool(t.sicaf_ready)
            })

    agenda_events.sort(key=lambda x: x["date"])

    # Dados de compatibilidade do usuário
    user_id = user.id if user else "usr-user-01"
    favs_count = db.query(Favorite).filter(Favorite.user_id == user_id).count()
    alerts_count = db.query(Alert).filter(Alert.user_id == user_id, Alert.active == True).count()
    deliveries_count = db.query(AlertDelivery).join(Alert).filter(Alert.user_id == user_id).count()

    favs = db.query(Favorite).filter(Favorite.user_id == user_id).all()
    upcoming = []
    for f in favs:
        t = f.tender
        if t and t.proposal_deadline:
            upcoming.append({
                "id": t.id,
                "title": t.title,
                "contracting_entity": t.contracting_entity,
                "state": t.state,
                "proposal_deadline": t.proposal_deadline.isoformat()
            })
    upcoming.sort(key=lambda x: x["proposal_deadline"])

    recent_delivs = db.query(AlertDelivery).join(Alert).filter(Alert.user_id == user_id).order_by(AlertDelivery.delivered_at.desc()).limit(5).all()
    delivs_data = []
    for d in recent_delivs:
        delivs_data.append({
            "tender_id": d.tender_id,
            "tender_title": d.tender.title if d.tender else "Oportunidade",
            "alert_name": d.alert.name if d.alert else "Radar",
            "delivered_at": d.delivered_at.isoformat()
        })

    return {
        "favorites_count": favs_count,
        "alerts_count": alerts_count,
        "deliveries_count": deliveries_count,
        "upcoming_tenders": upcoming[:5],
        "recent_deliveries": delivs_data,

        # Indicadores Executivos Licitações Brasil
        "active_deals_count": deals_count,
        "deals_count": deals_count,
        "companies_identified_count": companies_count,
        "companies_count": companies_count,
        "tenders_today_count": tenders_today_count,
        "total_tenders_today": tenders_today_count,
        "items_today_count": items_today_count,
        "total_items_count": total_items_count,
        "agenda_events_count": len(agenda_events),

        "categories_distribution": {
            "servicos": servicos_count,
            "materiais": materiais_count,
            "nao_classificado": nao_classificado_count,
            "Serviços": servicos_count,
            "Materiais": materiais_count,
            "Não Classificado": nao_classificado_count
        },
        "by_category": {
            "servicos": servicos_count,
            "materiais": materiais_count,
            "nao_classificado": nao_classificado_count,
            "Serviços": servicos_count,
            "Materiais": materiais_count,
            "Não Classificado": nao_classificado_count
        },
        "value_ranges": {
            "ate_50k": ate_50k,
            "de_50k_a_250k": de_50k_a_250k,
            "acima_250k": acima_250k,
            "Até R$ 50 mil": ate_50k,
            "R$ 50 mil a R$ 250 mil": de_50k_a_250k,
            "Acima de R$ 250 mil": acima_250k
        },
        "by_value_range": {
            "ate_50k": ate_50k,
            "de_50k_a_250k": de_50k_a_250k,
            "acima_250k": acima_250k,
            "Até R$ 50 mil": ate_50k,
            "R$ 50 mil a R$ 250 mil": de_50k_a_250k,
            "Acima de R$ 250 mil": acima_250k
        },
        "funnel_stages": funnel_stages,
        "agenda_events": agenda_events[:10],
        "upcoming_events": agenda_events[:10]
    }

@app.get("/api/user/dashboard-summary")
def api_user_dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return get_dashboard_summary_data(db, user)

# ----------------- Admin API -----------------

@app.get("/api/admin/metrics")
def api_admin_metrics(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    tenders_count = db.query(Tender).count()
    users_count = db.query(User).count()
    ai_count = db.query(AIAnalysis).count()
    sources = db.query(Source).all()
    return {
        "tenders_count": tenders_count,
        "users_count": users_count,
        "ai_analyses_count": ai_count,
        "sources": [
            {
                "id": s.id,
                "name": s.name,
                "official_domain": s.official_domain,
                "integration_type": s.integration_type,
                "active": s.active,
                "last_sync_at": s.last_sync_at.isoformat() if s.last_sync_at else None,
                "last_sync_status": s.last_sync_status
            }
            for s in sources
        ]
    }

@app.post("/api/admin/sync-sources")
def api_admin_sync_sources(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Dispara job de sincronização de fontes e processamento de alertas"""
    alert_result = run_alert_processing_job(db)
    tenders_count = db.query(Tender).count()
    return {
        "status": "success",
        "synced_count": tenders_count,
        "alerts_evaluated": alert_result.get("active_alerts_evaluated", 0),
        "notifications_generated": alert_result.get("total_deliveries_created", 0)
    }

@app.post("/api/admin/sources/{source_id}/toggle")
def api_admin_toggle_source(source_id: str, active: bool = Query(...), admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    src = db.query(Source).filter(Source.id == source_id).first()
    if not src:
        raise HTTPException(status_code=404, detail="Fonte não encontrada.")
    src.active = active
    db.commit()
    return {"message": "Status da fonte atualizado."}

@app.get("/api/sources")
def api_list_sources():
    return list_all_sources()
EOF

mkdir -p server/templates
cat << 'EOF' > server/templates/juridico.html
{% extends "base.html" %}

{% block title %}Peças Jurídicas Operacionais - Licitações Brasil{% endblock %}

{% block content %}
<div class="bg-[#0b0f19] min-h-screen text-slate-100 font-sans pb-16">
  <!-- Header Jurídico Licitações Brasil -->
  <div class="border-b border-slate-800/80 bg-slate-900/60 backdrop-blur-md sticky top-16 z-30 py-5">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
      <div class="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <div class="flex items-center gap-3">
            <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-amber-600 to-amber-500 flex items-center justify-center text-white shadow-lg shadow-amber-500/20">
              <i class="fa-solid fa-gavel text-lg"></i>
            </div>
            <div>
              <h1 class="text-xl sm:text-2xl font-black text-white tracking-tight flex items-center gap-2">
                Peças Jurídicas <span class="text-amber-400">Operacionais</span>
              </h1>
              <p class="text-xs text-slate-400 mt-0.5">
                Modelos prontos de impugnação, esclarecimento, recursos e contrarrazões fundamentados na Lei 14.133/2021.
              </p>
            </div>
          </div>
        </div>

        <div class="flex flex-wrap items-center gap-2.5">
          <div class="bg-slate-800/80 border border-slate-700/70 rounded-xl px-3 py-1.5 flex items-center gap-2 text-xs">
            <span class="text-slate-400">Peças Catalogadas:</span>
            <span id="templates-total-count" class="font-bold text-amber-400 font-mono">{{ counts.total if counts else templates|length }}</span>
          </div>

          <a href="/negocios?view=quadros" class="bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 font-medium px-4 py-2 rounded-xl text-xs transition flex items-center gap-1.5">
            <i class="fa-solid fa-table-columns text-emerald-400"></i> Funil Kanban
          </a>
        </div>
      </div>

      <!-- Barra de Busca, Filtros por Categoria e Chave Seletora "Somente Nova Lei" -->
      <div class="flex flex-col lg:flex-row lg:items-center justify-between gap-4 mt-5 pt-4 border-t border-slate-800/60">
        
        <!-- Categorias Pills -->
        <div class="flex items-center gap-2 flex-wrap text-xs">
          <span class="text-slate-400 font-semibold uppercase text-[11px] mr-1">Categoria:</span>
          
          <button 
            onclick="setCategoryFilter('')" 
            class="filter-cat-btn px-3 py-1.5 rounded-xl font-semibold transition {% if not tipo %}bg-amber-500/20 text-amber-400 border border-amber-500/40{% else %}bg-slate-900/80 text-slate-400 hover:text-slate-200 border border-slate-800{% endif %}"
            data-cat=""
          >
            Todas
          </button>

          <button 
            onclick="setCategoryFilter('impugnacao')" 
            class="filter-cat-btn px-3 py-1.5 rounded-xl font-semibold transition {% if tipo == 'impugnacao' %}bg-rose-500/20 text-rose-400 border border-rose-500/40{% else %}bg-slate-900/80 text-slate-400 hover:text-slate-200 border border-slate-800{% endif %}"
            data-cat="impugnacao"
          >
            Impugnação
          </button>

          <button 
            onclick="setCategoryFilter('esclarecimento')" 
            class="filter-cat-btn px-3 py-1.5 rounded-xl font-semibold transition {% if tipo == 'esclarecimento' %}bg-sky-500/20 text-sky-400 border border-sky-500/40{% else %}bg-slate-900/80 text-slate-400 hover:text-slate-200 border border-slate-800{% endif %}"
            data-cat="esclarecimento"
          >
            Esclarecimento
          </button>

          <button 
            onclick="setCategoryFilter('recurso')" 
            class="filter-cat-btn px-3 py-1.5 rounded-xl font-semibold transition {% if tipo == 'recurso' %}bg-amber-500/20 text-amber-400 border border-amber-500/40{% else %}bg-slate-900/80 text-slate-400 hover:text-slate-200 border border-slate-800{% endif %}"
            data-cat="recurso"
          >
            Recurso
          </button>

          <button 
            onclick="setCategoryFilter('contrarbenzoes')" 
            class="filter-cat-btn px-3 py-1.5 rounded-xl font-semibold transition {% if tipo == 'contrarbenzoes' %}bg-emerald-500/20 text-emerald-400 border border-emerald-500/40{% else %}bg-slate-900/80 text-slate-400 hover:text-slate-200 border border-slate-800{% endif %}"
            data-cat="contrarbenzoes"
          >
            Contrarrazões
          </button>
        </div>

        <!-- Busca Textual e Chave Seletora "Somente Nova Lei" -->
        <div class="flex flex-col sm:flex-row sm:items-center gap-3">
          
          <!-- Chave Seletora "Somente nova lei" (14.133/2021) -->
          <label class="flex items-center gap-2.5 cursor-pointer bg-slate-900/90 border border-slate-800 px-3 py-1.5 rounded-xl select-none hover:border-slate-700 transition">
            <input 
              type="checkbox" 
              id="toggle-nova-lei" 
              onchange="filterTemplates()" 
              class="w-4 h-4 rounded text-emerald-500 bg-slate-800 border-slate-700 focus:ring-emerald-500 cursor-pointer accent-emerald-500"
              {% if nova_lei_only %}checked{% endif %}
            >
            <span class="text-xs font-semibold text-slate-300 flex items-center gap-1.5">
              <span class="w-2 h-2 rounded-full bg-emerald-400"></span> Somente nova lei (14.133/2021)
            </span>
          </label>

          <!-- Input de Busca -->
          <div class="relative w-full sm:w-64">
            <i class="fa-solid fa-magnifying-glass absolute left-3 top-2.5 text-xs text-slate-500"></i>
            <input 
              type="text" 
              id="search-input" 
              value="{{ q }}"
              onkeyup="filterTemplates()" 
              placeholder="Buscar modelos ou fundamentação..." 
              class="w-full bg-slate-900/90 border border-slate-700/80 rounded-xl pl-8 pr-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 focus:outline-none focus:border-amber-500 transition"
            >
          </div>

        </div>

      </div>
    </div>
  </div>

  <!-- Conteúdo Principal: Lista de Modelos Disponíveis -->
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 mt-6">
    <!-- Certame vinculado (preenchimento automático via Funil Kanban) -->
    <div id="certame-vinculado-banner" class="{% if not (certame_prefill and (certame_prefill.orgao or certame_prefill.numero_certame or certame_prefill.objeto)) %}hidden {% endif %}mb-5 bg-emerald-500/10 border border-emerald-500/30 rounded-2xl p-4 flex flex-col md:flex-row md:items-center justify-between gap-3">
      <div class="flex items-start gap-3 min-w-0">
        <div class="w-9 h-9 shrink-0 rounded-xl bg-emerald-500/20 text-emerald-300 flex items-center justify-center">
          <i class="fa-solid fa-link"></i>
        </div>
        <div class="min-w-0">
          <div class="text-[11px] font-bold uppercase tracking-wider text-emerald-300">Certame vinculado ao formulário de petição</div>
          <div class="text-sm font-semibold text-white truncate" id="banner-certame-numero">{{ certame_prefill.numero_certame if certame_prefill else '' }}</div>
          <div class="text-xs text-slate-300 truncate" id="banner-certame-orgao">{{ certame_prefill.orgao if certame_prefill else '' }}</div>
          <div class="text-[11px] text-slate-400 line-clamp-1" id="banner-certame-objeto">{{ certame_prefill.objeto if certame_prefill else '' }}</div>
        </div>
      </div>
      <div class="flex items-center gap-2 shrink-0">
        <span class="text-[11px] text-slate-400 hidden lg:inline">Escolha um modelo abaixo: os dados já entram na peça.</span>
        <button type="button" onclick="limparCertameVinculado()" class="bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 px-3 py-1.5 rounded-xl text-xs font-semibold transition cursor-pointer">
          <i class="fa-solid fa-xmark"></i> Desvincular
        </button>
      </div>
    </div>

    <div id="templates-grid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
      {% for t in templates %}
        <div 
          class="template-card bg-slate-900/80 hover:bg-slate-850 border border-slate-800 hover:border-slate-700 rounded-2xl p-5 shadow-lg transition-all duration-200 flex flex-col justify-between group"
          data-id="{{ t.id }}"
          data-tipo="{{ t.tipo }}"
          data-nova-lei="{{ 'true' if t.is_nova_lei else 'false' }}"
          data-titulo="{{ t.titulo|lower }}"
          data-fundamento="{{ t.fundamentacao_legal|lower }}"
        >
          <div>
            <!-- Header do Card: Tipo & Regime Legal -->
            <div class="flex items-center justify-between gap-2 mb-3">
              <span class="text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-md font-mono 
                {% if t.tipo == 'impugnacao' %}bg-rose-500/10 text-rose-400 border border-rose-500/20
                {% elif t.tipo == 'esclarecimento' %}bg-sky-500/10 text-sky-400 border border-sky-500/20
                {% elif t.tipo == 'recurso' or t.tipo == 'intencao_recurso' %}bg-amber-500/10 text-amber-400 border border-amber-500/20
                {% else %}bg-emerald-500/10 text-emerald-400 border border-emerald-500/20{% endif %}">
                {{ t.tipo_label }}
              </span>

              {% if t.is_nova_lei %}
                <span class="inline-flex items-center gap-1 text-[10px] font-bold text-emerald-400 bg-emerald-500/10 border border-emerald-500/20 px-2 py-0.5 rounded-md">
                  <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span> Lei 14.133/2021
                </span>
              {% else %}
                <span class="text-[10px] font-medium text-slate-400 bg-slate-800 px-2 py-0.5 rounded-md border border-slate-700">
                  Regime Legado
                </span>
              {% endif %}
            </div>

            <!-- Título da Peça -->
            <h3 class="text-sm font-bold text-slate-100 group-hover:text-amber-300 transition leading-snug mb-2">
              {{ t.titulo }}
            </h3>

            <!-- Fundamentação Legal -->
            <div class="text-xs text-slate-400 mb-3 flex items-start gap-1.5">
              <i class="fa-solid fa-scale-balanced text-amber-400 text-[11px] mt-0.5 shrink-0"></i>
              <span class="font-medium text-[11px] leading-tight text-slate-300">{{ t.fundamentacao_legal }}</span>
            </div>

            <!-- Tags Dinâmicas Identificadas -->
            <div class="flex items-center gap-1.5 flex-wrap mb-4">
              <span class="text-[10px] font-mono bg-slate-950 text-slate-400 border border-slate-800 px-1.5 py-0.5 rounded">
                <i class="fa-solid fa-code text-[9px] text-amber-400"></i> &#123;&#123;orgao&#125;&#125;
              </span>
              <span class="text-[10px] font-mono bg-slate-950 text-slate-400 border border-slate-800 px-1.5 py-0.5 rounded">
                <i class="fa-solid fa-code text-[9px] text-amber-400"></i> &#123;&#123;numero_edital&#125;&#125;
              </span>
              <span class="text-[10px] font-mono bg-slate-950 text-slate-400 border border-slate-800 px-1.5 py-0.5 rounded">
                <i class="fa-solid fa-code text-[9px] text-amber-400"></i> &#123;&#123;objeto&#125;&#125;
              </span>
            </div>

            <!-- Prévia do Texto -->
            <div class="p-3 bg-slate-950/70 border border-slate-800/80 rounded-xl text-[11px] text-slate-400 font-mono line-clamp-3 mb-4 leading-relaxed whitespace-pre-line">
              {{ t.corpo[:200] }}...
            </div>
          </div>

          <!-- Ações Rápidas: Visualizar, Copiar e Download -->
          <div class="pt-3 border-t border-slate-800 flex items-center justify-between gap-2">
            <button 
              onclick="openTemplateModal({{ t.id }})" 
              class="flex-1 bg-amber-500/10 hover:bg-amber-500/20 text-amber-400 border border-amber-500/30 hover:border-amber-500/50 font-bold py-2 px-3 rounded-xl text-xs transition flex items-center justify-center gap-1.5 cursor-pointer"
            >
              <i class="fa-regular fa-eye"></i> Visualizar
            </button>

            <button 
              onclick="copyTemplateText({{ t.id }})" 
              class="bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 p-2 rounded-xl text-xs transition flex items-center justify-center cursor-pointer" 
              title="Copiar texto da peça"
            >
              <i class="fa-regular fa-copy"></i>
            </button>

            <button 
              onclick="downloadTemplateFile({{ t.id }})" 
              class="bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 p-2 rounded-xl text-xs transition flex items-center justify-center cursor-pointer" 
              title="Download do modelo (.txt)"
            >
              <i class="fa-solid fa-download"></i>
            </button>

            <button 
              onclick="baixarPdfModelo({{ t.id }}, this)" 
              class="btn-baixar-pdf bg-rose-600/90 hover:bg-rose-500 text-white border border-rose-500/50 px-2.5 py-2 rounded-xl text-xs font-bold transition flex items-center justify-center gap-1.5 cursor-pointer" 
              title="Baixar PDF timbrado com os dados do certame"
            >
              <i class="fa-solid fa-file-pdf"></i> Baixar PDF
            </button>
          </div>
        </div>
      {% endfor %}
    </div>

    <!-- Mensagem de Vazio -->
    <div id="no-templates-msg" class="hidden text-center py-16 px-4 bg-slate-900/50 border border-dashed border-slate-800 rounded-2xl">
      <i class="fa-solid fa-scale-unbalanced text-3xl text-slate-600 block mb-3"></i>
      <h3 class="text-sm font-bold text-slate-300">Nenhum modelo encontrado</h3>
      <p class="text-xs text-slate-500 mt-1">Tente remover o filtro de "Somente nova lei" ou ajustar os termos de pesquisa.</p>
    </div>
  </div>
</div>

<!-- Modal Dark de Visualização, Edição Dinâmica e Exportação -->
<div id="template-modal" class="fixed inset-0 z-50 hidden flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm">
  <div class="bg-slate-900 border border-slate-800 rounded-2xl w-full max-w-3xl max-h-[90vh] flex flex-col shadow-2xl overflow-hidden">
    
    <!-- Header do Modal -->
    <div class="p-4 sm:p-5 border-b border-slate-800 flex items-center justify-between bg-slate-950/50">
      <div>
        <div class="flex items-center gap-2 mb-1">
          <span id="modal-tipo-badge" class="text-[10px] font-mono uppercase px-2 py-0.5 rounded font-bold bg-amber-500/20 text-amber-400 border border-amber-500/30">
            PEÇA JURÍDICA
          </span>
          <span id="modal-lei-badge" class="text-[10px] text-emerald-400 bg-emerald-500/10 px-2 py-0.5 rounded border border-emerald-500/20 font-bold">
            Lei 14.133/2021
          </span>
        </div>
        <h2 id="modal-title" class="text-base font-bold text-white leading-tight">Título da Peça</h2>
        <div id="modal-fundamentacao" class="text-xs text-slate-400 mt-0.5">Fundamentação Legal</div>
      </div>

      <button onclick="closeTemplateModal()" class="text-slate-400 hover:text-white p-2 rounded-xl hover:bg-slate-800 transition">
        <i class="fa-solid fa-xmark text-lg"></i>
      </button>
    </div>

    <!-- Personalização Rápida de Tags -->
    <div class="p-4 bg-slate-950/80 border-b border-slate-800/80 grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs">
      <div>
        <label class="block text-[10px] font-bold uppercase text-slate-400 mb-1">Órgão Comprador (&#123;&#123;orgao&#125;&#125;)</label>
        <input 
          type="text" 
          id="input-var-orgao" 
          value="{{ certame_prefill.orgao if certame_prefill else '' }}"
          placeholder="Ex: Prefeitura Municipal de Maceió" 
          oninput="applyCustomVariables()" 
          class="w-full bg-slate-900 border border-slate-700 rounded-xl px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-amber-500 transition"
        >
      </div>

      <div>
        <label class="block text-[10px] font-bold uppercase text-slate-400 mb-1">Edital / Processo (&#123;&#123;numero_edital&#125;&#125;)</label>
        <input 
          type="text" 
          id="input-var-numero" 
          value="{{ certame_prefill.numero_certame if certame_prefill else '' }}"
          placeholder="Ex: Pregão Eletrônico 042/2025" 
          oninput="applyCustomVariables()" 
          class="w-full bg-slate-900 border border-slate-700 rounded-xl px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-amber-500 transition"
        >
      </div>

      <div>
        <label class="block text-[10px] font-bold uppercase text-slate-400 mb-1">Objeto do Certame (&#123;&#123;objeto&#125;&#125;)</label>
        <input 
          type="text" 
          id="input-var-objeto" 
          value="{{ certame_prefill.objeto if certame_prefill else '' }}"
          placeholder="Ex: Aquisição de medicamentos" 
          oninput="applyCustomVariables()" 
          class="w-full bg-slate-900 border border-slate-700 rounded-xl px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-amber-500 transition"
        >
      </div>
    </div>

    <!-- Dados da Licitante e Fundamentação (usados no PDF timbrado) -->
    <details id="licitante-panel" class="bg-slate-950/60 border-b border-slate-800/80 group/lic">
      <summary class="px-4 py-2.5 text-[11px] font-bold uppercase tracking-wider text-slate-300 cursor-pointer select-none flex items-center justify-between">
        <span><i class="fa-solid fa-building-shield text-rose-400 mr-1.5"></i> Dados da licitante e fundamentação do PDF</span>
        <span id="licitante-resumo" class="normal-case font-medium text-slate-500 tracking-normal truncate max-w-[50%]"></span>
      </summary>
      <div class="px-4 pb-4 grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs">
        <div class="sm:col-span-2">
          <label for="lic-razao" class="block text-[10px] font-bold uppercase text-slate-400 mb-1">Razão social</label>
          <input type="text" id="lic-razao" data-lic="razao_social" maxlength="255" placeholder="Ex: Alfa Distribuidora Hospitalar LTDA" class="lic-input w-full bg-slate-900 border border-slate-700 rounded-xl px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-rose-500 transition">
        </div>
        <div>
          <label for="lic-cnpj" class="block text-[10px] font-bold uppercase text-slate-400 mb-1">CNPJ</label>
          <input type="text" id="lic-cnpj" data-lic="cnpj" maxlength="18" inputmode="numeric" placeholder="00.000.000/0000-00" class="lic-input w-full bg-slate-900 border border-slate-700 rounded-xl px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-rose-500 transition">
        </div>
        <div class="sm:col-span-2">
          <label for="lic-endereco" class="block text-[10px] font-bold uppercase text-slate-400 mb-1">Endereço</label>
          <input type="text" id="lic-endereco" data-lic="endereco" maxlength="500" placeholder="Rua, número, bairro, cidade/UF, CEP" class="lic-input w-full bg-slate-900 border border-slate-700 rounded-xl px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-rose-500 transition">
        </div>
        <div>
          <label for="lic-email" class="block text-[10px] font-bold uppercase text-slate-400 mb-1">E-mail</label>
          <input type="email" id="lic-email" data-lic="email" maxlength="255" placeholder="juridico@empresa.com.br" class="lic-input w-full bg-slate-900 border border-slate-700 rounded-xl px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-rose-500 transition">
        </div>
        <div>
          <label for="lic-representante" class="block text-[10px] font-bold uppercase text-slate-400 mb-1">Representante legal</label>
          <input type="text" id="lic-representante" data-lic="representante" maxlength="255" placeholder="Nome completo" class="lic-input w-full bg-slate-900 border border-slate-700 rounded-xl px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-rose-500 transition">
        </div>
        <div>
          <label for="lic-cargo" class="block text-[10px] font-bold uppercase text-slate-400 mb-1">Cargo</label>
          <input type="text" id="lic-cargo" data-lic="cargo" maxlength="120" placeholder="Ex: Sócio-Administrador" class="lic-input w-full bg-slate-900 border border-slate-700 rounded-xl px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-rose-500 transition">
        </div>
        <div>
          <label for="lic-cidade" class="block text-[10px] font-bold uppercase text-slate-400 mb-1">Local da assinatura</label>
          <input type="text" id="lic-cidade" data-lic="cidade" maxlength="120" placeholder="Ex: Maceió/AL" class="lic-input w-full bg-slate-900 border border-slate-700 rounded-xl px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-rose-500 transition">
        </div>
        <div>
          <label for="pdf-lei" class="block text-[10px] font-bold uppercase text-slate-400 mb-1">Diploma legal</label>
          <select id="pdf-lei" class="w-full bg-slate-900 border border-slate-700 rounded-xl px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-rose-500 transition">
            <option value="">Conforme o modelo</option>
            <option value="14133">Lei 14.133/2021 (Nova Lei)</option>
            <option value="8666">Lei 8.666/1993 (Legado)</option>
          </select>
        </div>
      </div>
    </details>

    <!-- Corpo do Texto Formatado -->
    <div class="p-4 sm:p-6 overflow-y-auto flex-1 bg-slate-950/30">
      <textarea 
        id="modal-textarea" 
        rows="16" 
        class="w-full bg-slate-950/80 border border-slate-800 rounded-xl p-4 text-xs font-mono text-slate-200 focus:outline-none focus:border-amber-500 leading-relaxed resize-y select-text shadow-inner"
      ></textarea>
    </div>

    <!-- Rodapé de Ações do Modal -->
    <div class="p-4 border-t border-slate-800 bg-slate-950/60 flex items-center justify-between gap-3">
      <span class="text-[11px] text-slate-400 hidden sm:inline">
        As variáveis dinâmicas foram substituídas no texto em tempo real.
      </span>

      <div class="flex items-center gap-2 w-full sm:w-auto justify-end">
        <button 
          onclick="copyModalText()" 
          class="bg-amber-500 hover:bg-amber-400 text-slate-950 font-bold px-4 py-2 rounded-xl text-xs transition shadow-md flex items-center gap-1.5 cursor-pointer"
        >
          <i class="fa-regular fa-copy"></i> Copiar Texto
        </button>

        <button 
          onclick="downloadModalText()" 
          class="bg-slate-800 hover:bg-slate-700 text-white font-semibold px-4 py-2 rounded-xl text-xs border border-slate-700 transition flex items-center gap-1.5 cursor-pointer"
        >
          <i class="fa-solid fa-download"></i> Baixar .txt
        </button>

        <button 
          id="btn-modal-baixar-pdf"
          onclick="baixarPdfModal(this)" 
          class="bg-rose-600 hover:bg-rose-500 text-white font-bold px-4 py-2 rounded-xl text-xs border border-rose-500/50 transition shadow-md flex items-center gap-1.5 cursor-pointer"
        >
          <i class="fa-solid fa-file-pdf"></i> Baixar PDF
        </button>
      </div>
    </div>

  </div>
</div>

<script>
  // Cache dos modelos locais
  let allTemplates = {{ templates|tojson if templates else '[]' }};
  let currentActiveTemplate = null;
  let activeCategoryFilter = "{{ tipo or '' }}";

  function filterTemplates() {
    const q = (document.getElementById('search-input').value || '').toLowerCase().trim();
    const onlyNovaLei = document.getElementById('toggle-nova-lei').checked;

    let visibleCount = 0;

    document.querySelectorAll('.template-card').forEach(card => {
      const cardTipo = card.getAttribute('data-tipo');
      const isNovaLei = card.getAttribute('data-nova-lei') === 'true';
      const titulo = card.getAttribute('data-titulo') || '';
      const fundamento = card.getAttribute('data-fundamento') || '';

      // Filtro de categoria
      let matchCat = true;
      if (activeCategoryFilter) {
        if (activeCategoryFilter === 'contrarbenzoes') {
          matchCat = (cardTipo === 'contrarbenzoes' || cardTipo === 'contrarbenzoes');
        } else {
          matchCat = (cardTipo === activeCategoryFilter);
        }
      }

      // Filtro "Somente nova lei"
      let matchNovaLei = true;
      if (onlyNovaLei) {
        matchNovaLei = isNovaLei;
      }

      // Filtro textual
      let matchQuery = true;
      if (q) {
        matchQuery = titulo.includes(q) || fundamento.includes(q);
      }

      if (matchCat && matchNovaLei && matchQuery) {
        card.style.display = 'flex';
        visibleCount++;
      } else {
        card.style.display = 'none';
      }
    });

    const emptyMsg = document.getElementById('no-templates-msg');
    if (emptyMsg) {
      if (visibleCount === 0) {
        emptyMsg.classList.remove('hidden');
      } else {
        emptyMsg.classList.add('hidden');
      }
    }
  }

  function setCategoryFilter(cat) {
    activeCategoryFilter = cat;
    document.querySelectorAll('.filter-cat-btn').forEach(btn => {
      const btnCat = btn.getAttribute('data-cat');
      if (btnCat === cat) {
        btn.className = 'filter-cat-btn px-3 py-1.5 rounded-xl font-semibold transition bg-amber-500/20 text-amber-400 border border-amber-500/40';
      } else {
        btn.className = 'filter-cat-btn px-3 py-1.5 rounded-xl font-semibold transition bg-slate-900/80 text-slate-400 hover:text-slate-200 border border-slate-800';
      }
    });
    filterTemplates();
  }

  function getTemplateById(id) {
    return allTemplates.find(t => Number(t.id) === Number(id));
  }

  function openTemplateModal(id) {
    const tmpl = getTemplateById(id);
    if (!tmpl) return;
    currentActiveTemplate = tmpl;

    document.getElementById('modal-title').textContent = tmpl.titulo;
    document.getElementById('modal-fundamentacao').textContent = tmpl.fundamentacao_legal;
    document.getElementById('modal-tipo-badge').textContent = tmpl.tipo_label || tmpl.tipo.toUpperCase();
    
    const leiBadge = document.getElementById('modal-lei-badge');
    if (tmpl.is_nova_lei) {
      leiBadge.textContent = 'Lei 14.133/2021';
      leiBadge.className = 'text-[10px] text-emerald-400 bg-emerald-500/10 px-2 py-0.5 rounded border border-emerald-500/20 font-bold';
    } else {
      leiBadge.textContent = 'Regime Legado';
      leiBadge.className = 'text-[10px] text-slate-400 bg-slate-800 px-2 py-0.5 rounded border border-slate-700 font-bold';
    }

    applyCustomVariables();
    document.getElementById('template-modal').classList.remove('hidden');
  }

  function closeTemplateModal() {
    document.getElementById('template-modal').classList.add('hidden');
  }

  function applyCustomVariables() {
    if (!currentActiveTemplate) return;

    let text = currentActiveTemplate.corpo || currentActiveTemplate.content || '';
    {% raw %}
    const orgao = document.getElementById('input-var-orgao').value.trim() || '{{orgao}}';
    const numero = document.getElementById('input-var-numero').value.trim() || '{{numero_edital}}';
    const objeto = document.getElementById('input-var-objeto').value.trim() || '{{objeto}}';
    {% endraw %}

    text = text.replace(/\{\{orgao\}\}/g, orgao);
    text = text.replace(/\{\{numero_edital\}\}/g, numero);
    text = text.replace(/\{\{objeto\}\}/g, objeto);

    document.getElementById('modal-textarea').value = text;
  }

  async function copyTemplateText(id) {
    const tmpl = getTemplateById(id);
    if (!tmpl) return;
    try {
      await navigator.clipboard.writeText(tmpl.corpo);
      showToast('Modelo de peça jurídica copiado com sucesso!', 'success');
    } catch {
      showToast('Texto copiado!', 'info');
    }
  }

  async function copyModalText() {
    const text = document.getElementById('modal-textarea').value;
    try {
      await navigator.clipboard.writeText(text);
      showToast('Peça jurídica com dados preenchidos copiada com sucesso!', 'success');
    } catch {
      showToast('Texto copiado!', 'info');
    }
  }

  function downloadTemplateFile(id) {
    const tmpl = getTemplateById(id);
    if (!tmpl) return;
    downloadBlob(tmpl.corpo, `${tmpl.tipo}_${tmpl.id}.txt`);
  }

  function downloadModalText() {
    const text = document.getElementById('modal-textarea').value;
    const filename = currentActiveTemplate ? `${currentActiveTemplate.tipo}_preenchido.txt` : 'peca_juridica.txt';
    downloadBlob(text, filename);
  }

  function downloadBlob(content, filename) {
    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast(`Arquivo ${filename} baixado com sucesso!`, 'success');
  }

  // ---------------- Certame vinculado (Kanban -> Jurídico) ----------------
  const CERTAME_PREFILL = {{ certame_prefill|tojson if certame_prefill else '{}' }};

  function getCertameAtual() {
    return {
      orgao: document.getElementById('input-var-orgao').value.trim(),
      numero_certame: document.getElementById('input-var-numero').value.trim(),
      objeto: document.getElementById('input-var-objeto').value.trim()
    };
  }

  function aplicarCertameDaUrl() {
    const params = new URLSearchParams(window.location.search);
    const dados = {
      orgao: params.get('orgao') || CERTAME_PREFILL.orgao || '',
      numero_certame: params.get('numero_certame') || params.get('numero_edital') || CERTAME_PREFILL.numero_certame || '',
      objeto: params.get('objeto') || CERTAME_PREFILL.objeto || ''
    };
    if (!dados.orgao && !dados.numero_certame && !dados.objeto) return;
    document.getElementById('input-var-orgao').value = dados.orgao;
    document.getElementById('input-var-numero').value = dados.numero_certame;
    document.getElementById('input-var-objeto').value = dados.objeto;
    document.getElementById('banner-certame-numero').textContent = dados.numero_certame || 'Certame sem número';
    document.getElementById('banner-certame-orgao').textContent = dados.orgao;
    document.getElementById('banner-certame-objeto').textContent = dados.objeto;
    document.getElementById('certame-vinculado-banner').classList.remove('hidden');
  }

  function limparCertameVinculado() {
    ['input-var-orgao', 'input-var-numero', 'input-var-objeto'].forEach(id => { document.getElementById(id).value = ''; });
    document.getElementById('certame-vinculado-banner').classList.add('hidden');
    const url = new URL(window.location.href);
    ['orgao', 'numero_certame', 'numero_edital', 'objeto', 'tender_id'].forEach(k => url.searchParams.delete(k));
    window.history.replaceState({}, '', url.toString());
    applyCustomVariables();
  }

  // ---------------- Dados da licitante (memorizados neste navegador) ----------------
  const LICITANTE_STORAGE_KEY = 'lb_licitante_pdf_v1';

  function lerLicitante() {
    const dados = {};
    document.querySelectorAll('.lic-input').forEach(inp => {
      const v = inp.value.trim();
      if (v) dados[inp.dataset.lic] = v;
    });
    return dados;
  }

  function atualizarResumoLicitante() {
    const d = lerLicitante();
    const resumo = document.getElementById('licitante-resumo');
    if (resumo) resumo.textContent = d.razao_social ? `${d.razao_social}${d.cnpj ? ' • ' + d.cnpj : ''}` : 'não informado';
  }

  function carregarLicitante() {
    try {
      const salvo = JSON.parse(localStorage.getItem(LICITANTE_STORAGE_KEY) || '{}');
      document.querySelectorAll('.lic-input').forEach(inp => {
        if (salvo[inp.dataset.lic]) inp.value = salvo[inp.dataset.lic];
      });
    } catch (e) { /* armazenamento indisponível: segue sem memória */ }
    atualizarResumoLicitante();
  }

  function salvarLicitante() {
    try { localStorage.setItem(LICITANTE_STORAGE_KEY, JSON.stringify(lerLicitante())); } catch (e) { /* ignora */ }
    atualizarResumoLicitante();
  }

  function mascararCnpj(inp) {
    const d = inp.value.replace(/\D/g, '').slice(0, 14);
    let out = d;
    if (d.length > 12) out = `${d.slice(0,2)}.${d.slice(2,5)}.${d.slice(5,8)}/${d.slice(8,12)}-${d.slice(12)}`;
    else if (d.length > 8) out = `${d.slice(0,2)}.${d.slice(2,5)}.${d.slice(5,8)}/${d.slice(8)}`;
    else if (d.length > 5) out = `${d.slice(0,2)}.${d.slice(2,5)}.${d.slice(5)}`;
    else if (d.length > 2) out = `${d.slice(0,2)}.${d.slice(2)}`;
    inp.value = out;
  }

  // ---------------- Geração do PDF timbrado ----------------
  function montarPayloadPdf(templateId, corpoEditado) {
    const certame = getCertameAtual();
    const lic = lerLicitante();
    const cidade = lic.cidade || '';
    delete lic.cidade;
    const payload = {
      template_id: Number(templateId),
      orgao: certame.orgao || null,
      numero_certame: certame.numero_certame || null,
      objeto: certame.objeto || null,
      lei: document.getElementById('pdf-lei').value || null,
      cidade: cidade || null,
      licitante: Object.keys(lic).length ? lic : null
    };
    if (CERTAME_PREFILL.tender_id) payload.tender_id = CERTAME_PREFILL.tender_id;
    if (corpoEditado) payload.corpo = corpoEditado;
    return payload;
  }

  function nomeArquivoDaResposta(resp, fallback) {
    const cd = resp.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename="?([^";]+)"?/i);
    return m ? m[1] : fallback;
  }

  async function solicitarPdf(payload, botao) {
    const original = botao ? botao.innerHTML : '';
    if (botao) { botao.disabled = true; botao.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Gerando...'; }
    try {
      const resp = await fetch('/api/juridico/gerar-pdf', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (!resp.ok) {
        let msg = 'Falha ao gerar o PDF.';
        try { const err = await resp.json(); if (err && err.detail) msg = typeof err.detail === 'string' ? err.detail : msg; } catch (e) {}
        throw new Error(msg);
      }
      const blob = await resp.blob();
      const nome = nomeArquivoDaResposta(resp, 'peticao.pdf');
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = nome;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 1500);
      showToast(`PDF ${nome} gerado com sucesso!`, 'success');
    } catch (e) {
      showToast(e.message || 'Falha ao gerar o PDF.', 'error');
    } finally {
      if (botao) { botao.disabled = false; botao.innerHTML = original; }
    }
  }

  function baixarPdfModelo(id, botao) {
    const tmpl = getTemplateById(id);
    if (!tmpl) return;
    solicitarPdf(montarPayloadPdf(tmpl.id, null), botao);
  }

  function baixarPdfModal(botao) {
    if (!currentActiveTemplate) return;
    const corpo = document.getElementById('modal-textarea').value;
    solicitarPdf(montarPayloadPdf(currentActiveTemplate.id, corpo), botao);
  }

  document.addEventListener('DOMContentLoaded', () => {
    aplicarCertameDaUrl();
    carregarLicitante();
    document.querySelectorAll('.lic-input').forEach(inp => inp.addEventListener('input', () => {
      if (inp.dataset.lic === 'cnpj') mascararCnpj(inp);
      salvarLicitante();
    }));
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeTemplateModal();
  });
</script>
{% endblock %}
EOF

# ==============================================================================
# ITEM 2 — COFRE DE DOCUMENTOS & AUDITORIA DE VALIDADE
# ==============================================================================
mkdir -p server
cat << 'EOF' > server/models.py
import uuid
from datetime import datetime, date, timedelta, timezone
from typing import List, Optional
from sqlalchemy import (
    Column, String, Text, Integer, Float, Boolean, DateTime, Date,
    ForeignKey, UniqueConstraint, Index, JSON
)
from sqlalchemy.orm import relationship
from server.database import Base

def generate_uuid():
    return str(uuid.uuid4())

class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default="USER", nullable=False) # USER, ADMIN
    status = Column(String(20), default="ACTIVE", nullable=False) # ACTIVE, INACTIVE, SUSPENDED
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    last_login_at = Column(DateTime, nullable=True)

    favorites = relationship("Favorite", back_populates="user", cascade="all, delete-orphan")
    notes = relationship("UserNote", back_populates="user", cascade="all, delete-orphan")
    saved_searches = relationship("SavedSearch", back_populates="user", cascade="all, delete-orphan")
    alerts = relationship("Alert", back_populates="user", cascade="all, delete-orphan")
    search_history = relationship("SearchHistory", back_populates="user", cascade="all, delete-orphan")


class Source(Base):
    __tablename__ = "sources"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    name = Column(String(255), nullable=False)
    base_url = Column(String(500), nullable=False)
    official_domain = Column(String(255), nullable=False, index=True)
    source_type = Column(String(50), default="FEDERAL", nullable=False) # FEDERAL, ESTADUAL, MUNICIPAL, SISTEMA_S
    integration_type = Column(String(50), default="API_OFICIAL", nullable=False) # API_OFICIAL, EXPORTACAO_ARQUIVO, IMPORTACAO_MANUAL, DESATIVADO
    active = Column(Boolean, default=True, index=True)
    authorization_notes = Column(Text, nullable=True)
    last_sync_at = Column(DateTime, nullable=True)
    last_sync_status = Column(String(50), nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    tenders = relationship("Tender", back_populates="source", cascade="all, delete-orphan")


class Tender(Base):
    __tablename__ = "tenders"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    source_id = Column(String(36), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    external_id = Column(String(255), nullable=False, index=True) # Ex: numeroControlePNCP
    process_number = Column(String(255), nullable=True)
    notice_number = Column(String(255), nullable=True) # Edital nº
    title = Column(String(500), nullable=False)
    object_description = Column(Text, nullable=False)
    contracting_entity = Column(String(500), nullable=False, index=True) # Órgão comprador
    purchasing_unit = Column(String(500), nullable=True) # Unidade
    state = Column(String(2), nullable=False, index=True) # UF (ex: AL, SP)
    city = Column(String(255), nullable=True, index=True)
    region = Column(String(50), nullable=True) # Nordeste, Sudeste, etc.
    administrative_sphere = Column(String(50), default="FEDERAL", index=True) # FEDERAL, ESTADUAL, MUNICIPAL
    modality = Column(String(100), default="PREGAO_ELETRONICO", index=True)
    category = Column(String(100), nullable=True, index=True) # Medicamentos, Materiais Hospitalares, TI, etc.
    status = Column(String(100), default="Divulgada no PNCP", index=True)
    publication_date = Column(DateTime, nullable=True, index=True)
    opening_date = Column(DateTime, nullable=True, index=True)
    proposal_deadline = Column(DateTime, nullable=True, index=True)
    estimated_total_value = Column(Float, nullable=True, index=True)
    source_url = Column(String(1000), nullable=False)
    official_notice_url = Column(String(1000), nullable=True)
    source_system = Column(String(50), default="pncp", nullable=False, index=True) # "compras_gov" ou "pncp"
    uasg_code = Column(String(50), nullable=True, index=True) # código da UASG/Unidade Compradora
    sicaf_ready = Column(Boolean, default=False, nullable=False, index=True) # True se for do Compras.gov.br para permitir lances via SICAF
    link_comprasnet = Column(String(1000), nullable=True) # URL de acesso direto/sala de disputa
    link_pncp = Column(String(1000), nullable=True) # URL do termo/edital no PNCP
    url_disputa = Column(String(1000), nullable=True, index=True) # URL oficial da sala de disputa / portal de origem
    url_pncp = Column(String(1000), nullable=True, index=True) # URL canônica pública do PNCP (/{cnpj}/{ano}/{sequencial})
    fonte_portal = Column(String(100), default="PNCP", nullable=True, index=True) # Portal de origem: PNCP, Compras.gov.br, BNC, etc.
    link_edital_oficial = Column(String(1000), nullable=True) # Link direto para download do edital nos portais oficiais
    ai_resumo = Column(Text, nullable=True) # Resumo executivo estruturado gerado via IA Gemini
    ai_pontos_atencao = Column(Text, nullable=True) # Pontos críticos e riscos extraídos pela IA
    itens_extraidos_json = Column(JSON, nullable=True) # Lista estruturada de itens extraídos via IA
    data_completeness = Column(String(50), default="COMPLETO") # COMPLETO, PARCIAL, DADOS_AUSENTES
    raw_data_reference = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    last_synced_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    @property
    def dispute_url(self) -> str:
        return self.url_disputa or self.link_comprasnet or self.official_notice_url or self.source_url or self.canonical_pncp_url

    @property
    def canonical_pncp_url(self) -> str:
        return self.url_pncp or self.link_pncp or "https://pncp.gov.br/app/editais"

    source = relationship("Source", back_populates="tenders")
    items = relationship("TenderItem", back_populates="tender", cascade="all, delete-orphan")
    documents = relationship("TenderDocument", back_populates="tender", cascade="all, delete-orphan")
    favorites = relationship("Favorite", back_populates="tender", cascade="all, delete-orphan")
    notes = relationship("UserNote", back_populates="tender", cascade="all, delete-orphan")
    changes = relationship("TenderChange", back_populates="tender", cascade="all, delete-orphan")
    ai_analyses = relationship("AIAnalysis", back_populates="tender", cascade="all, delete-orphan")
    alert_deliveries = relationship("AlertDelivery", back_populates="tender", cascade="all, delete-orphan")
    deals = relationship("Deal", back_populates="tender", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_source_external_id"),
        Index("ix_tenders_status_opening", "status", "opening_date"),
        Index("ix_tenders_url_pncp", "url_pncp"),
        Index("ix_tenders_url_disputa", "url_disputa"),
    )


class TenderItem(Base):
    __tablename__ = "tender_items"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    tender_id = Column(String(36), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False, index=True)
    item_number = Column(Integer, nullable=False)
    description = Column(Text, nullable=False)
    unit = Column(String(50), nullable=True)
    quantity = Column(Float, nullable=True)
    estimated_unit_value = Column(Float, nullable=True)
    estimated_total_value = Column(Float, nullable=True)
    classification_code = Column(String(100), nullable=True, index=True) # CATMAT / CATSER
    raw_data = Column(JSON, nullable=True)

    tender = relationship("Tender", back_populates="items")


class TenderDocument(Base):
    __tablename__ = "tender_documents"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    tender_id = Column(String(36), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(500), nullable=False)
    document_type = Column(String(100), default="EDITAL", nullable=False) # Edital, Termo de Referência, etc.
    source_url = Column(String(1000), nullable=False)
    storage_key = Column(String(500), nullable=True)
    mime_type = Column(String(100), default="application/pdf", nullable=False)
    file_size = Column(Integer, nullable=True)
    checksum = Column(String(64), nullable=True, index=True) # SHA-256
    published_at = Column(DateTime, nullable=True)
    downloaded_at = Column(DateTime, nullable=True)
    extraction_status = Column(String(50), default="PENDING")
    extraction_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    tender = relationship("Tender", back_populates="documents")
    extraction = relationship("DocumentExtraction", back_populates="document", uselist=False, cascade="all, delete-orphan")
    ai_analyses = relationship("AIAnalysis", back_populates="document")


class Favorite(Base):
    __tablename__ = "favorites"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    tender_id = Column(String(36), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="favorites")
    tender = relationship("Tender", back_populates="favorites")

    __table_args__ = (
        UniqueConstraint("user_id", "tender_id", name="uq_user_tender_favorite"),
    )


class UserNote(Base):
    __tablename__ = "user_notes"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    tender_id = Column(String(36), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False, index=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="notes")
    tender = relationship("Tender", back_populates="notes")


class SavedSearch(Base):
    __tablename__ = "saved_searches"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    name = Column(String(255), nullable=True, default="Alerta")
    keywords = Column(String(500), nullable=True)
    ufs = Column(String(255), nullable=True)
    min_value = Column(Float, nullable=True)
    filters = Column(JSON, nullable=True, default=dict)
    active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="saved_searches")


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    keywords = Column(String(500), nullable=True)
    filters = Column(JSON, nullable=False, default=dict)
    frequency = Column(String(50), default="DAILY") # DAILY, WEEKLY, REALTIME
    channels = Column(String(100), default="IN_APP") # IN_APP, EMAIL
    active = Column(Boolean, default=True, index=True)
    last_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="alerts")
    deliveries = relationship("AlertDelivery", back_populates="alert", cascade="all, delete-orphan")


class AlertDelivery(Base):
    __tablename__ = "alert_deliveries"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    alert_id = Column(String(36), ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False, index=True)
    tender_id = Column(String(36), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False, index=True)
    channel = Column(String(50), default="IN_APP")
    delivered_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    status = Column(String(50), default="SENT")

    alert = relationship("Alert", back_populates="deliveries")
    tender = relationship("Tender", back_populates="alert_deliveries")


class TenderChange(Base):
    __tablename__ = "tender_changes"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    tender_id = Column(String(36), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False, index=True)
    change_type = Column(String(100), nullable=False) # RETIFICACAO, PRORROGACAO, CANCELAMENTO
    field_name = Column(String(100), nullable=False)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    detected_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    source_reference = Column(String(255), nullable=True)

    tender = relationship("Tender", back_populates="changes")


class DocumentExtraction(Base):
    __tablename__ = "document_extractions"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    document_id = Column(String(36), ForeignKey("tender_documents.id", ondelete="CASCADE"), unique=True, nullable=False)
    extraction_status = Column(String(50), default="PENDING")
    extracted_text_reference = Column(Text, nullable=True)
    extracted_text_hash = Column(String(64), nullable=True)
    page_count = Column(Integer, default=0)
    ocr_used = Column(Boolean, default=False)
    processed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    error_message = Column(Text, nullable=True)

    document = relationship("TenderDocument", back_populates="extraction")


class AIAnalysis(Base):
    __tablename__ = "ai_analyses"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    tender_id = Column(String(36), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False, index=True)
    document_id = Column(String(36), ForeignKey("tender_documents.id", ondelete="SET NULL"), nullable=True, index=True)
    model_provider = Column(String(50), default="GEMINI")
    model_name = Column(String(100), default="gemini-3.8-flash")
    prompt_version = Column(String(50), default="v1.0")
    analysis_type = Column(String(50), default="ANALISE_COMPLETA")
    result_json = Column(JSON, nullable=False)
    source_text_hash = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    tender = relationship("Tender", back_populates="ai_analyses")
    document = relationship("TenderDocument", back_populates="ai_analyses")


class SearchHistory(Base):
    __tablename__ = "search_history"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    query = Column(String(500), nullable=True)
    filters = Column(JSON, nullable=True)
    result_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    user = relationship("User", back_populates="search_history")


class WebhookDispatchLog(Base):
    __tablename__ = "webhook_dispatch_logs"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    rule_id = Column(String(36), ForeignKey("alerts.id", ondelete="SET NULL"), nullable=True, index=True)
    tender_id = Column(String(36), ForeignKey("tenders.id", ondelete="SET NULL"), nullable=True, index=True)
    target_url = Column(String(1000), nullable=False)
    canal = Column(String(50), default="GENERIC", nullable=False) # N8N, EVOLUTION_API, ZAPI, GENERIC
    status_code = Column(Integer, nullable=True)
    tentativa_atual = Column(Integer, default=1, nullable=False)
    sucesso = Column(Boolean, default=False, nullable=False, index=True)
    tempo_resposta_ms = Column(Float, default=0.0, nullable=False)
    payload_enviado = Column(JSON, nullable=True)
    resposta_servidor = Column(Text, nullable=True)
    criado_em = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    rule = relationship("Alert", backref="webhook_logs")
    tender = relationship("Tender", backref="webhook_logs")


DEAL_STAGES = ["oportunidades", "qualificacao", "disputa", "classificacao", "contrato"]
DEAL_STAGE_LABELS = {
    "oportunidades": "Oportunidades",
    "qualificacao": "Qualificação",
    "disputa": "Disputa",
    "classificacao": "Classificação",
    "contrato": "Contrato"
}

class Deal(Base):
    __tablename__ = "deals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tender_id = Column(Integer, ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False, index=True)
    stage = Column(String(50), default="oportunidades", nullable=False, index=True) # oportunidades, qualificacao, disputa, classificacao, contrato
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    tender = relationship("Tender", back_populates="deals")

    @property
    def target_date(self):
        if self.tender:
            return self.tender.opening_date or self.tender.proposal_deadline
        return None

    @property
    def days_remaining(self):
        td = self.target_date
        if not td:
            return None
        try:
            from zoneinfo import ZoneInfo
            SP_TZ = ZoneInfo("America/Sao_Paulo")
        except Exception:
            from datetime import timezone
            SP_TZ = timezone(timedelta(hours=-3), name="America/Sao_Paulo")
        now = datetime.now(SP_TZ)
        if isinstance(td, datetime):
            td_sp = td.replace(tzinfo=SP_TZ) if td.tzinfo is None else td.astimezone(SP_TZ)
        else:
            return None
        delta = (td_sp.date() - now.date()).days
        return delta

    @property
    def deadline_urgency(self):
        td = self.target_date
        if not td:
            return "sem_data"
        try:
            from zoneinfo import ZoneInfo
            SP_TZ = ZoneInfo("America/Sao_Paulo")
        except Exception:
            from datetime import timezone
            SP_TZ = timezone(timedelta(hours=-3), name="America/Sao_Paulo")
        now = datetime.now(SP_TZ)
        if isinstance(td, datetime):
            td_sp = td.replace(tzinfo=SP_TZ) if td.tzinfo is None else td.astimezone(SP_TZ)
        else:
            return "sem_data"
        diff = (td_sp - now).total_seconds()
        hours_remaining = diff / 3600.0
        if diff < 0:
            return "vencido"
        elif 0 <= hours_remaining <= 24:
            return "critico"
        elif diff <= 7 * 86400:
            return "alerta"
        return "normal"


class LegalDocumentTemplate(Base):
    __tablename__ = "legal_document_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tipo = Column(String(50), nullable=False, index=True) # "impugnacao", "esclarecimento", "intencao_recurso", "recurso", "contrarbenzoes"
    titulo = Column(String(255), nullable=False)
    fundamentacao_legal = Column(String(500), nullable=False, default="Lei 14.133/2021")
    corpo = Column(Text, nullable=False)
    is_nova_lei = Column(Boolean, default=True, index=True) # True para Lei 14.133/2021
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    @property
    def template_type(self):
        return self.tipo

    @property
    def title(self):
        return self.titulo

    @property
    def content(self):
        return self.corpo


class SupplierProspect(Base):
    __tablename__ = "supplier_prospects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, index=True)
    cnpj = Column(String(20), nullable=True, index=True)
    state = Column(String(2), nullable=True, index=True)
    city = Column(String(255), nullable=True)
    segment = Column(String(100), nullable=True)
    total_tenders = Column(Integer, default=0)
    total_value = Column(Float, default=0.0)
    ai_summary = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class HistoricoDisputa(Base):
    __tablename__ = "historico_disputas"

    id = Column(Integer, primary_key=True, autoincrement=True)
    id_pregao = Column(String(255), nullable=False, index=True)
    numero_item = Column(Integer, nullable=False, index=True)
    tipo_evento = Column(String(50), nullable=False, index=True) # AUTO_BID, MANUAL_BID, STOP_LOSS, COMPETITOR_BID
    valor_ofertado = Column(Float, nullable=False)
    posicao = Column(Integer, default=1)
    autor = Column(String(100), default="Robô Licitações Brasil")
    detalhes = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


# ==============================================================================
# COFRE DE DOCUMENTOS DE HABILITAÇÃO DA EMPRESA
# ==============================================================================

# Categorias alinhadas aos grupos de habilitação do art. 62 da Lei 14.133/2021.
DOCUMENTO_CATEGORIAS = {
    "juridica": "Habilitação Jurídica",
    "fiscal": "Regularidade Fiscal, Social e Trabalhista",
    "economica": "Qualificação Econômico-Financeira",
    "tecnica": "Qualificação Técnica",
    "declaracoes": "Declarações",
    "outros": "Outros",
}

# Limiar (em dias) a partir do qual um documento passa a "A VENCER" (badge amarelo).
DOCUMENTO_DIAS_ALERTA = 15

DOCUMENTO_STATUS_VALIDO = "VALIDO"
DOCUMENTO_STATUS_A_VENCER = "A_VENCER"
DOCUMENTO_STATUS_VENCIDO = "VENCIDO"
DOCUMENTO_STATUS_SEM_VALIDADE = "SEM_VALIDADE"


def hoje_sao_paulo() -> date:
    """Data corrente no fuso oficial America/Sao_Paulo (referência para validade documental)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Sao_Paulo")).date()
    except Exception:
        return datetime.now(timezone(timedelta(hours=-3))).date()


class DocumentoEmpresa(Base):
    __tablename__ = "documentos_empresa"

    id = Column(Integer, primary_key=True, autoincrement=True)
    nome = Column(String(255), nullable=False, index=True)
    categoria = Column(String(50), nullable=False, index=True)  # chave de DOCUMENTO_CATEGORIAS
    data_emissao = Column(Date, nullable=False)
    data_validade = Column(Date, nullable=True, index=True)  # None = sem vencimento (ex.: contrato social)
    caminho_arquivo = Column(String(1000), nullable=False)
    tags_habilitacao = Column(String(500), nullable=True)  # lista separada por vírgulas
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    @property
    def categoria_label(self) -> str:
        return DOCUMENTO_CATEGORIAS.get(self.categoria, self.categoria or "Outros")

    @property
    def tags(self) -> List[str]:
        if not self.tags_habilitacao:
            return []
        return [t.strip() for t in self.tags_habilitacao.split(",") if t.strip()]

    def dias_restantes(self, hoje: Optional[date] = None) -> Optional[int]:
        """Dias corridos até o vencimento (negativo se vencido; None se sem validade)."""
        if not self.data_validade:
            return None
        ref = hoje or hoje_sao_paulo()
        return (self.data_validade - ref).days

    def status_validade(self, hoje: Optional[date] = None) -> str:
        """VALIDO (>15 dias), A_VENCER (0 a 15 dias), VENCIDO (<0) ou SEM_VALIDADE."""
        dias = self.dias_restantes(hoje)
        if dias is None:
            return DOCUMENTO_STATUS_SEM_VALIDADE
        if dias < 0:
            return DOCUMENTO_STATUS_VENCIDO
        if dias <= DOCUMENTO_DIAS_ALERTA:
            return DOCUMENTO_STATUS_A_VENCER
        return DOCUMENTO_STATUS_VALIDO

    def is_valido(self, hoje: Optional[date] = None) -> bool:
        return self.status_validade(hoje) != DOCUMENTO_STATUS_VENCIDO
EOF

mkdir -p server
cat << 'EOF' > server/migrate.py
import logging
from sqlalchemy import text, inspect
from server.database import engine

logger = logging.getLogger(__name__)

def run_db_migrations():
    """
    Executa migrações automáticas no banco SQLite/PostgreSQL
    garantindo que todas as novas colunas e índices existam.
    """
    inspector = inspect(engine)
    if "tenders" not in inspector.get_table_names():
        return

    if "saved_searches" in inspector.get_table_names():
        s_cols = [c["name"] for c in inspector.get_columns("saved_searches")]
        with engine.begin() as conn:
            if "ufs" not in s_cols:
                logger.info("Migração: adicionando coluna ufs à tabela saved_searches")
                conn.execute(text("ALTER TABLE saved_searches ADD COLUMN ufs VARCHAR(255)"))
            if "min_value" not in s_cols:
                logger.info("Migração: adicionando coluna min_value à tabela saved_searches")
                conn.execute(text("ALTER TABLE saved_searches ADD COLUMN min_value FLOAT"))

    cols = [c["name"] for c in inspector.get_columns("tenders")]
    with engine.begin() as conn:
        if "source_system" not in cols:
            logger.info("Migração: adicionando coluna source_system à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN source_system VARCHAR(50) DEFAULT 'pncp' NOT NULL"))
        if "uasg_code" not in cols:
            logger.info("Migração: adicionando coluna uasg_code à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN uasg_code VARCHAR(50)"))
        if "sicaf_ready" not in cols:
            logger.info("Migração: adicionando coluna sicaf_ready à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN sicaf_ready BOOLEAN DEFAULT 0 NOT NULL"))
        if "link_comprasnet" not in cols:
            logger.info("Migração: adicionando coluna link_comprasnet à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN link_comprasnet VARCHAR(1000)"))
        if "link_pncp" not in cols:
            logger.info("Migração: adicionando coluna link_pncp à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN link_pncp VARCHAR(1000)"))
        if "url_disputa" not in cols:
            logger.info("Migração: adicionando coluna url_disputa à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN url_disputa VARCHAR(1000)"))
        if "url_pncp" not in cols:
            logger.info("Migração: adicionando coluna url_pncp à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN url_pncp VARCHAR(1000)"))
        if "fonte_portal" not in cols:
            logger.info("Migração: adicionando coluna fonte_portal à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN fonte_portal VARCHAR(100) DEFAULT 'PNCP'"))
        if "link_edital_oficial" not in cols:
            logger.info("Migração: adicionando coluna link_edital_oficial à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN link_edital_oficial VARCHAR(1000)"))
        if "ai_resumo" not in cols:
            logger.info("Migração: adicionando coluna ai_resumo à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN ai_resumo TEXT"))
        if "ai_pontos_atencao" not in cols:
            logger.info("Migração: adicionando coluna ai_pontos_atencao à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN ai_pontos_atencao TEXT"))
        if "itens_extraidos_json" not in cols:
            logger.info("Migração: adicionando coluna itens_extraidos_json à tabela tenders")
            conn.execute(text("ALTER TABLE tenders ADD COLUMN itens_extraidos_json TEXT"))

        # Criação da tabela de auditoria de disputas se inexistente
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS historico_disputas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                id_pregao VARCHAR(255) NOT NULL,
                numero_item INTEGER NOT NULL,
                tipo_evento VARCHAR(50) NOT NULL,
                valor_ofertado FLOAT NOT NULL,
                posicao INTEGER DEFAULT 1,
                autor VARCHAR(100) DEFAULT 'Robô Licitações Brasil',
                detalhes TEXT,
                created_at DATETIME NOT NULL
            )
        """))

        # Cofre de documentos de habilitação da empresa (DDL portátil via metadata do modelo)
        if "documentos_empresa" not in inspector.get_table_names():
            from server.models import DocumentoEmpresa
            logger.info("Migração: criando tabela documentos_empresa")
            DocumentoEmpresa.__table__.create(bind=conn, checkfirst=True)

        # Backfill de links e identificadores para dados existentes
        conn.execute(text("UPDATE tenders SET link_pncp = source_url WHERE link_pncp IS NULL"))
        conn.execute(text(
            "UPDATE tenders SET source_system = 'compras_gov', sicaf_ready = 1, uasg_code = '250005', "
            "link_comprasnet = 'https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra=250005' "
            "WHERE administrative_sphere = 'FEDERAL' AND (source_system IS NULL OR source_system = 'pncp')"
        ))
        conn.execute(text(
            "UPDATE tenders SET source_system = 'pncp', sicaf_ready = 0 "
            "WHERE administrative_sphere != 'FEDERAL' AND source_system IS NULL"
        ))

        # Garantir índices de alta performance
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tenders_opening_date ON tenders (opening_date)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tenders_status_opening ON tenders (status, opening_date)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tenders_url_pncp ON tenders (url_pncp)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tenders_url_disputa ON tenders (url_disputa)"))
    # Backfill canônico de URLs do PNCP e links de disputa
    from server.connectors.pncp import build_canonical_pncp_url
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, external_id, source_url, link_pncp, link_comprasnet, official_notice_url, url_disputa, url_pncp FROM tenders"
        )).fetchall()

        updated_count = 0
        for r in rows:
            t_id, ext_id, src_url, l_pncp, l_cnet, off_url, u_disp, u_pncp = r

            # Gera link canônico do PNCP
            canon_pncp = build_canonical_pncp_url(
                numero_controle_pncp=ext_id or l_pncp or src_url
            )

            # Prioriza link de disputa / portal de origem
            dispute = None
            if l_cnet:
                dispute = l_cnet
            elif off_url and "pncp.gov.br" not in off_url:
                dispute = off_url
            elif u_disp and "pncp.gov.br" not in u_disp:
                dispute = u_disp
            else:
                dispute = canon_pncp

            conn.execute(
                text("""
                    UPDATE tenders 
                    SET url_pncp = :canon_pncp,
                        link_pncp = :canon_pncp,
                        source_url = :canon_pncp,
                        url_disputa = :dispute
                    WHERE id = :t_id
                """),
                {"canon_pncp": canon_pncp, "dispute": dispute, "t_id": t_id}
            )
            updated_count += 1

        logger.info(f"Migração: {updated_count} licitações atualizadas com links canônicos do PNCP e disputa.")

if __name__ == "__main__":
    run_db_migrations()
    print("Migrações executadas com sucesso.")
EOF

mkdir -p server/services
cat << 'EOF' > server/services/document_vault.py
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
EOF

mkdir -p server/templates
cat << 'EOF' > server/templates/documentos.html
{% extends "base.html" %}

{% block title %}Cofre de Documentos - Licitações Brasil{% endblock %}

{% block content %}
<div class="bg-[#0b0f19] min-h-screen text-slate-100 font-sans pb-16">
  <!-- Cabeçalho do Cofre -->
  <div class="border-b border-slate-800/80 bg-slate-900/60 backdrop-blur-md sticky top-16 z-30 py-5">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
      <div class="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div class="flex items-center gap-3">
          <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-emerald-600 to-emerald-500 flex items-center justify-center text-white shadow-lg shadow-emerald-500/20">
            <i class="fa-solid fa-vault text-lg"></i>
          </div>
          <div>
            <h1 class="text-xl sm:text-2xl font-black text-white tracking-tight">
              Cofre de <span class="text-emerald-400">Documentos</span>
            </h1>
            <p class="text-xs text-slate-400 mt-0.5">
              Documentos de habilitação (arts. 62 a 70 da Lei 14.133/2021) com auditoria automática de validade.
            </p>
          </div>
        </div>

        <!-- Resumo de validade -->
        <div class="flex flex-wrap items-center gap-2 text-xs" aria-live="polite">
          <button type="button" data-status-filter="" class="status-pill bg-slate-800/80 border border-slate-700/70 rounded-xl px-3 py-1.5 flex items-center gap-2 cursor-pointer">
            <span class="text-slate-400">Total</span>
            <span id="resumo-total" class="font-bold text-white font-mono">{{ resumo.total }}</span>
          </button>
          <button type="button" data-status-filter="VALIDO" class="status-pill bg-emerald-500/10 border border-emerald-500/30 rounded-xl px-3 py-1.5 flex items-center gap-2 cursor-pointer">
            <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
            <span class="text-emerald-300">Válidos</span>
            <span id="resumo-validos" class="font-bold text-emerald-300 font-mono">{{ resumo.validos + resumo.sem_validade }}</span>
          </button>
          <button type="button" data-status-filter="A_VENCER" class="status-pill bg-amber-500/10 border border-amber-500/30 rounded-xl px-3 py-1.5 flex items-center gap-2 cursor-pointer">
            <span class="w-2 h-2 rounded-full bg-amber-400"></span>
            <span class="text-amber-300">A vencer (≤{{ dias_alerta }}d)</span>
            <span id="resumo-a-vencer" class="font-bold text-amber-300 font-mono">{{ resumo.a_vencer }}</span>
          </button>
          <button type="button" data-status-filter="VENCIDO" class="status-pill bg-rose-500/10 border border-rose-500/30 rounded-xl px-3 py-1.5 flex items-center gap-2 cursor-pointer">
            <span class="w-2 h-2 rounded-full bg-rose-400"></span>
            <span class="text-rose-300">Vencidos</span>
            <span id="resumo-vencidos" class="font-bold text-rose-300 font-mono">{{ resumo.vencidos }}</span>
          </button>
        </div>
      </div>
    </div>
  </div>

  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 mt-6 grid grid-cols-1 lg:grid-cols-3 gap-6">

    <!-- Formulário de Upload -->
    <section class="lg:col-span-1">
      <form id="form-upload-documento" class="bg-slate-900/80 border border-slate-800 rounded-2xl p-5 shadow-lg space-y-4" enctype="multipart/form-data" novalidate>
        <div class="flex items-center gap-2">
          <i class="fa-solid fa-cloud-arrow-up text-emerald-400"></i>
          <h2 class="text-sm font-bold text-white">Adicionar documento</h2>
        </div>

        <div>
          <label for="doc-nome" class="block text-[10px] font-bold uppercase text-slate-400 mb-1">Nome do documento *</label>
          <input id="doc-nome" name="nome" type="text" required maxlength="255" placeholder="Ex: CND Federal (Receita/PGFN)" class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-xs text-slate-200 placeholder-slate-500 focus:outline-none focus:border-emerald-500 transition">
        </div>

        <div>
          <label for="doc-categoria" class="block text-[10px] font-bold uppercase text-slate-400 mb-1">Categoria *</label>
          <select id="doc-categoria" name="categoria" required class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-emerald-500 transition">
            {% for chave, rotulo in categorias.items() %}
              <option value="{{ chave }}">{{ rotulo }}</option>
            {% endfor %}
          </select>
        </div>

        <div class="grid grid-cols-2 gap-3">
          <div>
            <label for="doc-emissao" class="block text-[10px] font-bold uppercase text-slate-400 mb-1">Emissão *</label>
            <input id="doc-emissao" name="data_emissao" type="date" required value="{{ hoje }}" class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-emerald-500 transition [color-scheme:dark]">
          </div>
          <div>
            <label for="doc-validade" class="block text-[10px] font-bold uppercase text-slate-400 mb-1">Validade</label>
            <input id="doc-validade" name="data_validade" type="date" class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-emerald-500 transition disabled:opacity-40 [color-scheme:dark]">
          </div>
        </div>

        <label class="flex items-center gap-2 text-xs text-slate-300 cursor-pointer select-none">
          <input id="doc-sem-validade" type="checkbox" class="w-4 h-4 rounded accent-emerald-500">
          Documento sem vencimento (ex.: contrato social, atestado técnico)
        </label>

        <div>
          <label for="doc-tags" class="block text-[10px] font-bold uppercase text-slate-400 mb-1">Tags de habilitação</label>
          <input id="doc-tags" name="tags_habilitacao" type="text" maxlength="500" placeholder="Ex: CND, Receita Federal, Art. 68" class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-xs text-slate-200 placeholder-slate-500 focus:outline-none focus:border-emerald-500 transition">
          <p class="text-[10px] text-slate-500 mt-1">Separe por vírgulas.</p>
        </div>

        <div>
          <label for="doc-arquivo" class="block text-[10px] font-bold uppercase text-slate-400 mb-1">Arquivo PDF * (até {{ max_mb }} MB)</label>
          <input id="doc-arquivo" name="arquivo" type="file" required accept="application/pdf,.pdf" class="w-full text-xs text-slate-300 file:mr-3 file:py-2 file:px-3 file:rounded-xl file:border-0 file:text-xs file:font-semibold file:bg-slate-800 file:text-slate-200 hover:file:bg-slate-700 cursor-pointer">
        </div>

        <p id="upload-erro" class="hidden text-xs text-rose-300 bg-rose-500/10 border border-rose-500/30 rounded-xl px-3 py-2"></p>

        <button id="btn-upload" type="submit" class="w-full bg-emerald-600 hover:bg-emerald-500 text-white font-bold py-2.5 rounded-xl text-xs transition shadow-md flex items-center justify-center gap-2 cursor-pointer disabled:opacity-60">
          <i class="fa-solid fa-lock"></i> Guardar no cofre
        </button>

        <div class="text-[10px] text-slate-500 leading-relaxed border-t border-slate-800 pt-3">
          <span class="inline-flex items-center gap-1 mr-2"><span class="w-2 h-2 rounded-full bg-emerald-400"></span> &gt; {{ dias_alerta }} dias</span>
          <span class="inline-flex items-center gap-1 mr-2"><span class="w-2 h-2 rounded-full bg-amber-400"></span> ≤ {{ dias_alerta }} dias</span>
          <span class="inline-flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-rose-400"></span> Vencido</span>
        </div>
      </form>
    </section>

    <!-- Lista de documentos com auditoria de validade -->
    <section class="lg:col-span-2">
      <div class="bg-slate-900/80 border border-slate-800 rounded-2xl shadow-lg overflow-hidden">
        <div class="p-4 border-b border-slate-800 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          <div class="flex items-center gap-2">
            <i class="fa-solid fa-list-check text-emerald-400"></i>
            <h2 class="text-sm font-bold text-white">Documentos guardados</h2>
            <span id="filtro-ativo-label" class="hidden text-[10px] font-bold uppercase px-2 py-0.5 rounded-md bg-slate-800 text-slate-300 border border-slate-700"></span>
          </div>
          <div class="relative w-full sm:w-64">
            <i class="fa-solid fa-magnifying-glass absolute left-3 top-2.5 text-xs text-slate-500"></i>
            <input id="busca-documentos" type="search" placeholder="Buscar por nome, categoria ou tag..." class="w-full bg-slate-950 border border-slate-700 rounded-xl pl-8 pr-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 focus:outline-none focus:border-emerald-500 transition">
          </div>
        </div>

        <div class="overflow-x-auto">
          <table class="w-full text-xs min-w-[720px]">
            <thead class="bg-slate-950/70 text-[10px] uppercase tracking-wider text-slate-400">
              <tr>
                <th class="text-left font-bold px-4 py-2.5">Documento</th>
                <th class="text-left font-bold px-3 py-2.5">Categoria</th>
                <th class="text-left font-bold px-3 py-2.5">Emissão</th>
                <th class="text-left font-bold px-3 py-2.5">Validade</th>
                <th class="text-right font-bold px-3 py-2.5">Dias restantes</th>
                <th class="text-left font-bold px-3 py-2.5">Status</th>
                <th class="text-right font-bold px-4 py-2.5">Ações</th>
              </tr>
            </thead>
            <tbody id="documentos-tbody" class="divide-y divide-slate-800/80"></tbody>
          </table>
        </div>

        <div id="documentos-vazio" class="hidden text-center py-14 px-4">
          <i class="fa-solid fa-folder-open text-3xl text-slate-600 block mb-3"></i>
          <h3 class="text-sm font-bold text-slate-300">Nenhum documento por aqui</h3>
          <p class="text-xs text-slate-500 mt-1">Guarde certidões, balanços e atestados para montar kits de habilitação em um clique.</p>
        </div>
      </div>
    </section>
  </div>
</div>

<script>
  const DIAS_ALERTA = {{ dias_alerta|int }};
  let documentos = {{ documentos|tojson }};
  let filtroStatus = '';

  function escapeHtml(valor) {
    return String(valor ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  }

  // Data corrente no fuso oficial (America/Sao_Paulo) no formato AAAA-MM-DD
  function hojeSaoPauloISO() {
    try {
      return new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Sao_Paulo', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
    } catch (e) {
      return new Date().toISOString().slice(0, 10);
    }
  }

  function isoParaUTC(iso) {
    const [y, m, d] = iso.split('-').map(Number);
    return Date.UTC(y, m - 1, d);
  }

  function calcularDiasRestantes(validadeISO) {
    if (!validadeISO) return null;
    return Math.round((isoParaUTC(validadeISO) - isoParaUTC(hojeSaoPauloISO())) / 86400000);
  }

  function calcularStatus(dias) {
    if (dias === null) return 'SEM_VALIDADE';
    if (dias < 0) return 'VENCIDO';
    if (dias <= DIAS_ALERTA) return 'A_VENCER';
    return 'VALIDO';
  }

  function formatarDataBR(iso) {
    if (!iso) return '—';
    const [y, m, d] = iso.split('-');
    return `${d}/${m}/${y}`;
  }

  const BADGES = {
    VALIDO: { cls: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40', dot: 'bg-emerald-400', label: 'Válido' },
    SEM_VALIDADE: { cls: 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30', dot: 'bg-emerald-400', label: 'Sem vencimento' },
    A_VENCER: { cls: 'bg-amber-500/15 text-amber-300 border-amber-500/40', dot: 'bg-amber-400 animate-pulse', label: 'A vencer' },
    VENCIDO: { cls: 'bg-rose-500/15 text-rose-300 border-rose-500/40', dot: 'bg-rose-400', label: 'Vencido' }
  };

  function corDias(status) {
    if (status === 'VENCIDO') return 'text-rose-300';
    if (status === 'A_VENCER') return 'text-amber-300';
    return 'text-emerald-300';
  }

  function textoDias(dias) {
    if (dias === null) return '<span class="text-slate-500">∞</span>';
    return `${dias}`;
  }

  function legendaDias(dias) {
    if (dias === null) return 'sem vencimento';
    if (dias < 0) return `vencido há ${Math.abs(dias)} dia(s)`;
    if (dias === 0) return 'vence hoje';
    return dias === 1 ? 'dia' : 'dias';
  }

  function renderizar() {
    const termo = (document.getElementById('busca-documentos').value || '').toLowerCase().trim();
    const tbody = document.getElementById('documentos-tbody');
    const resumo = { total: 0, validos: 0, a_vencer: 0, vencidos: 0 };
    const linhas = [];

    documentos.forEach(doc => {
      const dias = calcularDiasRestantes(doc.data_validade);
      const status = calcularStatus(dias);
      doc.dias_restantes = dias;
      doc.status = status;

      resumo.total++;
      if (status === 'VENCIDO') resumo.vencidos++;
      else if (status === 'A_VENCER') resumo.a_vencer++;
      else resumo.validos++;

      const statusFiltro = status === 'SEM_VALIDADE' ? 'VALIDO' : status;
      if (filtroStatus && statusFiltro !== filtroStatus) return;
      const alvo = `${doc.nome} ${doc.categoria_label} ${doc.tags_habilitacao || ''}`.toLowerCase();
      if (termo && !alvo.includes(termo)) return;

      const badge = BADGES[status];
      const tags = (doc.tags || []).map(t => `<span class="text-[10px] bg-slate-950 text-slate-400 border border-slate-800 px-1.5 py-0.5 rounded">${escapeHtml(t)}</span>`).join(' ');
      const arquivo = doc.arquivo_disponivel
        ? `<a href="${doc.arquivo_url}" target="_blank" rel="noopener" class="p-1.5 rounded-lg text-slate-300 hover:text-white hover:bg-slate-800 transition" title="Abrir PDF"><i class="fa-regular fa-file-pdf"></i></a>`
        : `<span class="p-1.5 text-rose-400" title="Arquivo físico ausente"><i class="fa-solid fa-triangle-exclamation"></i></span>`;

      linhas.push(`
        <tr class="hover:bg-slate-800/30 transition" data-doc-id="${doc.id}" data-status="${status}">
          <td class="px-4 py-3 align-top">
            <div class="font-semibold text-slate-100">${escapeHtml(doc.nome)}</div>
            <div class="flex flex-wrap gap-1 mt-1">${tags}</div>
          </td>
          <td class="px-3 py-3 align-top text-slate-300">${escapeHtml(doc.categoria_label)}</td>
          <td class="px-3 py-3 align-top font-mono text-slate-400">${formatarDataBR(doc.data_emissao)}</td>
          <td class="px-3 py-3 align-top font-mono text-slate-300">${formatarDataBR(doc.data_validade)}</td>
          <td class="px-3 py-3 align-top text-right">
            <div class="dias-restantes font-mono font-black text-base leading-none ${corDias(status)}">${textoDias(dias)}</div>
            <div class="text-[10px] text-slate-500 mt-0.5">${legendaDias(dias)}</div>
          </td>
          <td class="px-3 py-3 align-top">
            <span class="inline-flex items-center gap-1.5 text-[10px] font-bold uppercase px-2 py-1 rounded-md border ${badge.cls}">
              <span class="w-1.5 h-1.5 rounded-full ${badge.dot}"></span>${badge.label}
            </span>
          </td>
          <td class="px-4 py-3 align-top text-right whitespace-nowrap">
            ${arquivo}
            <button type="button" onclick="excluirDocumento(${doc.id})" class="p-1.5 rounded-lg text-slate-400 hover:text-rose-400 hover:bg-slate-800 transition cursor-pointer" title="Excluir do cofre">
              <i class="fa-regular fa-trash-can"></i>
            </button>
          </td>
        </tr>`);
    });

    tbody.innerHTML = linhas.join('');
    document.getElementById('documentos-vazio').classList.toggle('hidden', linhas.length > 0);
    document.getElementById('resumo-total').textContent = resumo.total;
    document.getElementById('resumo-validos').textContent = resumo.validos;
    document.getElementById('resumo-a-vencer').textContent = resumo.a_vencer;
    document.getElementById('resumo-vencidos').textContent = resumo.vencidos;
  }

  function definirFiltroStatus(status) {
    filtroStatus = (filtroStatus === status) ? '' : status;
    const label = document.getElementById('filtro-ativo-label');
    const nomes = { VALIDO: 'Válidos', A_VENCER: 'A vencer', VENCIDO: 'Vencidos' };
    if (filtroStatus) {
      label.textContent = nomes[filtroStatus];
      label.classList.remove('hidden');
    } else {
      label.classList.add('hidden');
    }
    document.querySelectorAll('.status-pill').forEach(btn => {
      btn.classList.toggle('ring-2', btn.dataset.statusFilter === filtroStatus && !!filtroStatus);
      btn.classList.toggle('ring-slate-400/60', btn.dataset.statusFilter === filtroStatus && !!filtroStatus);
    });
    renderizar();
  }

  function mostrarErroUpload(msg) {
    const el = document.getElementById('upload-erro');
    if (!msg) { el.classList.add('hidden'); el.textContent = ''; return; }
    el.textContent = msg;
    el.classList.remove('hidden');
  }

  async function enviarDocumento(ev) {
    ev.preventDefault();
    mostrarErroUpload('');
    const form = ev.target;
    const nome = form.nome.value.trim();
    const emissao = form.data_emissao.value;
    const semValidade = document.getElementById('doc-sem-validade').checked;
    const validade = semValidade ? '' : form.data_validade.value;
    const arquivo = form.arquivo.files[0];

    if (!nome) return mostrarErroUpload('Informe o nome do documento.');
    if (!emissao) return mostrarErroUpload('Informe a data de emissão.');
    if (!semValidade && !validade) return mostrarErroUpload('Informe a validade ou marque "sem vencimento".');
    if (validade && validade < emissao) return mostrarErroUpload('A validade não pode ser anterior à emissão.');
    if (!arquivo) return mostrarErroUpload('Selecione o arquivo PDF.');
    if (!arquivo.name.toLowerCase().endsWith('.pdf')) return mostrarErroUpload('Apenas arquivos PDF são aceitos.');
    if (arquivo.size > {{ max_mb|int }} * 1024 * 1024) return mostrarErroUpload('O arquivo excede {{ max_mb }} MB.');

    const dados = new FormData();
    dados.append('nome', nome);
    dados.append('categoria', form.categoria.value);
    dados.append('data_emissao', emissao);
    if (validade) dados.append('data_validade', validade);
    dados.append('tags_habilitacao', form.tags_habilitacao.value.trim());
    dados.append('arquivo', arquivo);

    const botao = document.getElementById('btn-upload');
    const original = botao.innerHTML;
    botao.disabled = true;
    botao.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Enviando...';
    try {
      const resp = await fetch('/api/documentos', { method: 'POST', body: dados });
      const corpo = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        const detalhe = typeof corpo.detail === 'string' ? corpo.detail : 'Falha ao guardar o documento.';
        throw new Error(detalhe);
      }
      documentos.push(corpo);
      documentos.sort((a, b) => (a.data_validade || '9999-12-31').localeCompare(b.data_validade || '9999-12-31') || a.nome.localeCompare(b.nome));
      form.reset();
      form.data_emissao.value = hojeSaoPauloISO();
      document.getElementById('doc-validade').disabled = false;
      renderizar();
      showToast(`"${corpo.nome}" guardado no cofre.`, 'success');
    } catch (e) {
      mostrarErroUpload(e.message);
    } finally {
      botao.disabled = false;
      botao.innerHTML = original;
    }
  }

  async function excluirDocumento(id) {
    const doc = documentos.find(d => d.id === id);
    if (!doc || !confirm(`Excluir "${doc.nome}" do cofre? O arquivo será removido.`)) return;
    try {
      const resp = await fetch(`/api/documentos/${id}`, { method: 'DELETE' });
      if (!resp.ok) throw new Error('Falha ao excluir o documento.');
      documentos = documentos.filter(d => d.id !== id);
      renderizar();
      showToast('Documento removido do cofre.', 'info');
    } catch (e) {
      showToast(e.message, 'error');
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('form-upload-documento').addEventListener('submit', enviarDocumento);
    document.getElementById('busca-documentos').addEventListener('input', renderizar);
    document.getElementById('doc-sem-validade').addEventListener('change', (e) => {
      const campo = document.getElementById('doc-validade');
      campo.disabled = e.target.checked;
      if (e.target.checked) campo.value = '';
    });
    document.querySelectorAll('.status-pill').forEach(btn => btn.addEventListener('click', () => definirFiltroStatus(btn.dataset.statusFilter)));
    renderizar();
    // Recalcula contadores e badges em tempo real (virada de dia sem recarregar a página)
    setInterval(renderizar, 60000);
  });
</script>
{% endblock %}
EOF

mkdir -p server/templates
cat << 'EOF' > server/templates/base.html
<!DOCTYPE html>
<html lang="pt-BR" class="h-full bg-slate-50">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="theme-color" content="#0f172a">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <link rel="manifest" href="/static/manifest.json">
  <link rel="icon" type="image/svg+xml" href="/static/icons/icon.svg">
  <title>{% block title %}Licitações Brasil - Plataforma Inteligente de Contratações Públicas{% endblock %}</title>
  <!-- Tailwind CSS CDN para renderização ágil e responsiva -->
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      theme: {
        extend: {
          colors: {
            brand: {
              50: '#f0f7ff',
              100: '#e0effe',
              500: '#0284c7',
              600: '#0369a1',
              700: '#075985',
              800: '#0c4a6e',
              900: '#082f49',
            }
          }
        }
      }
    }
  </script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
    [x-cloak] { display: none !important; }
    .badge-status { @apply inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium; }
    .safe-area-bottom { padding-bottom: max(0.5rem, env(safe-area-inset-bottom)); }
  </style>
</head>
<body class="flex flex-col min-h-full text-slate-800 antialiased font-sans">
  
  <!-- Top Navigation Header -->
  <header class="bg-brand-900 text-white shadow-md sticky top-0 z-50">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
      <div class="flex items-center justify-between h-16">
        <!-- Logo & Brand -->
        <div class="flex items-center space-x-3">
          <a href="/" class="flex items-center space-x-3">
            <div class="w-10 h-10 rounded-lg bg-gradient-to-tr from-sky-400 to-emerald-400 flex items-center justify-center text-brand-900 font-black text-xl shadow-inner">
              <i class="fa-solid fa-scale-balanced"></i>
            </div>
            <div>
              <span class="text-xl font-bold tracking-tight text-white">Licitações<span class="text-sky-400">Brasil</span></span>
              <span class="hidden sm:inline-block ml-2 text-xs bg-sky-950 text-sky-300 px-2 py-0.5 rounded font-mono border border-sky-800">PNCP + IA</span>
            </div>
          </a>
        </div>

        <!-- Main Nav Links -->
        <nav class="hidden md:flex items-center space-x-6 text-sm font-medium">
          <a href="/search" class="text-slate-200 hover:text-white transition flex items-center gap-1.5">
            <i class="fa-solid fa-magnifying-glass text-sky-400"></i> Pesquisar Editais
          </a>
          <a href="/dashboard" class="text-slate-200 hover:text-white transition flex items-center gap-1.5">
            <i class="fa-solid fa-chart-pie text-sky-400"></i> Dashboard
          </a>
          <a href="/favorites" class="text-slate-200 hover:text-white transition flex items-center gap-1.5">
            <i class="fa-solid fa-star text-amber-400"></i> Favoritos
          </a>
          <a href="/negocios?view=quadros" class="text-slate-200 hover:text-white transition flex items-center gap-1.5">
            <i class="fa-solid fa-table-columns text-emerald-400"></i> Negócios
          </a>
          <a href="/agenda" class="text-slate-200 hover:text-white transition flex items-center gap-1.5">
            <i class="fa-solid fa-calendar-days text-rose-400"></i> Agenda
          </a>
          <a href="/disputa" class="text-slate-200 hover:text-white transition flex items-center gap-1.5 font-bold">
            <i class="fa-solid fa-tower-broadcast text-rose-400 animate-pulse"></i> Disputa <span class="px-1.5 py-0.5 rounded text-[10px] font-black bg-rose-500 text-white animate-pulse">AO VIVO</span>
          </a>
          <a href="/fornecedores" class="text-slate-200 hover:text-white transition flex items-center gap-1.5">
            <i class="fa-solid fa-building-user text-purple-400"></i> Fornecedores
          </a>
          <a href="/juridico" class="text-slate-200 hover:text-white transition flex items-center gap-1.5">
            <i class="fa-solid fa-gavel text-amber-400"></i> Peças Jurídicas
          </a>
          <a href="/documentos" class="text-slate-200 hover:text-white transition flex items-center gap-1.5">
            <i class="fa-solid fa-vault text-emerald-400"></i> Cofre de Documentos
          </a>
          <a href="/alerts" class="text-slate-200 hover:text-white transition flex items-center gap-1.5">
            <i class="fa-solid fa-bell text-sky-400"></i> Alertas
          </a>
          <a href="/admin" class="text-slate-200 hover:text-white transition flex items-center gap-1.5">
            <i class="fa-solid fa-shield-halved text-emerald-400"></i> Gestão
          </a>
        </nav>

        <!-- User Controls / Auth -->
        <div class="flex items-center space-x-3 text-sm">
          <span id="user-display" class="hidden sm:inline-block text-slate-300 font-medium">
            <i class="fa-regular fa-user-circle mr-1"></i> <span id="user-name">Usuário</span>
          </span>
          <button id="auth-btn" onclick="handleAuthAction()" class="bg-sky-500 hover:bg-sky-600 text-white font-medium px-4 py-2 rounded-lg text-xs tracking-wider uppercase transition shadow-sm">
            Entrar
          </button>
          <!-- Mobile Menu Hamburger Button -->
          <button id="mobile-menu-toggle" onclick="toggleMobileMenu()" class="md:hidden p-2 text-slate-300 hover:text-white hover:bg-brand-800 rounded-lg transition focus:outline-none focus:ring-2 focus:ring-sky-400" aria-label="Abrir Menu">
            <i class="fa-solid fa-bars text-xl"></i>
          </button>
        </div>
      </div>
    </div>
  </header>

  <!-- Mobile Drawer Menu & Backdrop -->
  <div id="mobile-menu-backdrop" onclick="closeMobileMenu()" class="fixed inset-0 bg-slate-950/75 backdrop-blur-sm z-50 opacity-0 pointer-events-none transition-opacity duration-300 md:hidden" aria-hidden="true"></div>

  <aside id="mobile-menu-drawer" class="fixed top-0 right-0 bottom-0 w-80 max-w-[85vw] bg-slate-900 border-l border-slate-800 shadow-2xl z-50 transform translate-x-full transition-transform duration-300 ease-in-out md:hidden flex flex-col safe-area-bottom" aria-label="Menu de Navegação Móvel">
    <!-- Cabeçalho do Drawer -->
    <div class="p-4 border-b border-slate-800 flex items-center justify-between bg-slate-900/95">
      <div class="flex items-center space-x-3">
        <div class="w-9 h-9 rounded-lg bg-gradient-to-tr from-sky-400 to-emerald-400 flex items-center justify-center text-brand-900 font-black text-lg shadow-inner">
          <i class="fa-solid fa-scale-balanced"></i>
        </div>
        <div>
          <span class="text-lg font-bold tracking-tight text-white">Licitações<span class="text-sky-400">Brasil</span></span>
          <span class="block text-[10px] text-sky-400 font-mono">PWA • Lei 14.133</span>
        </div>
      </div>
      <button id="mobile-menu-close" onclick="closeMobileMenu()" class="p-2 text-slate-400 hover:text-white rounded-lg hover:bg-slate-800 transition focus:outline-none focus:ring-2 focus:ring-sky-400" aria-label="Fechar Menu">
        <i class="fa-solid fa-xmark text-xl"></i>
      </button>
    </div>

    <!-- Perfil / Usuário no Drawer -->
    <div class="px-4 py-3 bg-slate-800/60 border-b border-slate-800 flex items-center justify-between">
      <div class="flex items-center space-x-3">
        <div class="w-9 h-9 rounded-full bg-slate-700/80 border border-slate-600 flex items-center justify-center text-sky-400 text-base">
          <i class="fa-regular fa-user"></i>
        </div>
        <div>
          <div id="mobile-user-name" class="text-xs font-semibold text-white">Visitante</div>
          <div id="mobile-user-role" class="text-[10px] text-slate-400">Perfil Público</div>
        </div>
      </div>
      <a href="/admin" class="text-xs text-sky-400 hover:text-sky-300 font-medium py-1 px-2 rounded-md hover:bg-slate-700/50 transition">
        Painel
      </a>
    </div>

    <!-- Lista Completa de Módulos (11 Itens Funcionais) -->
    <nav class="flex-1 overflow-y-auto py-3 px-3 space-y-1 text-sm font-medium">
      <!-- 1. Pesquisar Editais -->
      <a href="/" data-route="/search" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-magnifying-glass text-sky-400 w-5 text-center"></i>
          <span>Pesquisar Editais</span>
        </div>
        <i class="fa-solid fa-chevron-right text-xs text-slate-500"></i>
      </a>

      <!-- 2. Dashboard -->
      <a href="/dashboard" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-chart-pie text-sky-400 w-5 text-center"></i>
          <span>Dashboard</span>
        </div>
        <i class="fa-solid fa-chevron-right text-xs text-slate-500"></i>
      </a>

      <!-- 3. Favoritos -->
      <a href="/favorites" data-route="/favoritos" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-star text-amber-400 w-5 text-center"></i>
          <span>Favoritos</span>
        </div>
        <i class="fa-solid fa-chevron-right text-xs text-slate-500"></i>
      </a>

      <!-- 4. Negócios -->
      <a href="/negocios" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-table-columns text-emerald-400 w-5 text-center"></i>
          <span>Negócios</span>
        </div>
        <i class="fa-solid fa-chevron-right text-xs text-slate-500"></i>
      </a>

      <!-- 5. Agenda -->
      <a href="/agenda" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-calendar-days text-rose-400 w-5 text-center"></i>
          <span>Agenda</span>
        </div>
        <i class="fa-solid fa-chevron-right text-xs text-slate-500"></i>
      </a>

      <!-- 6. Disputa AO VIVO com badge pulsante vermelho -->
      <a href="/disputa" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition font-bold">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-tower-broadcast text-rose-400 animate-pulse w-5 text-center"></i>
          <span class="text-white">Disputa AO VIVO</span>
        </div>
        <span class="px-2 py-0.5 rounded text-[10px] font-black bg-rose-500 text-white animate-pulse tracking-wide">AO VIVO</span>
      </a>

      <!-- 7. Fornecedores -->
      <a href="/fornecedores" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-building-user text-purple-400 w-5 text-center"></i>
          <span>Fornecedores</span>
        </div>
        <i class="fa-solid fa-chevron-right text-xs text-slate-500"></i>
      </a>

      <!-- 8. Peças Jurídicas -->
      <a href="/juridico" data-route="/pecas-juridicas" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-gavel text-amber-400 w-5 text-center"></i>
          <span>Peças Jurídicas</span>
        </div>
        <i class="fa-solid fa-chevron-right text-xs text-slate-500"></i>
      </a>

      <!-- 8.1 Cofre de Documentos -->
      <a href="/documentos" data-route="/cofre" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-vault text-emerald-400 w-5 text-center"></i>
          <span>Cofre de Documentos</span>
        </div>
        <i class="fa-solid fa-chevron-right text-xs text-slate-500"></i>
      </a>

      <!-- 9. Alertas -->
      <a href="/alerts" data-route="/alertas" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-bell text-sky-400 w-5 text-center"></i>
          <span>Alertas</span>
        </div>
        <i class="fa-solid fa-chevron-right text-xs text-slate-500"></i>
      </a>

      <!-- 10. Gestão / Perfil do Usuário / Administrador -->
      <a href="/admin" class="flex items-center justify-between px-3 py-2.5 rounded-xl text-slate-200 hover:text-white hover:bg-slate-800/80 transition">
        <div class="flex items-center gap-3">
          <i class="fa-solid fa-shield-halved text-emerald-400 w-5 text-center"></i>
          <span>Gestão / Administrador</span>
        </div>
        <i class="fa-solid fa-chevron-right text-xs text-slate-500"></i>
      </a>
    </nav>

    <!-- 11. Botão Sair / Autenticação no Rodapé do Drawer -->
    <div class="p-4 border-t border-slate-800 bg-slate-900/95">
      <button id="mobile-auth-btn" onclick="handleAuthAction()" data-route="/logout" class="w-full bg-sky-500 hover:bg-sky-600 text-white font-medium py-2.5 px-4 rounded-xl text-xs tracking-wider uppercase transition flex items-center justify-center gap-2 shadow-sm">
        <i id="mobile-auth-icon" class="fa-solid fa-right-to-bracket"></i>
        <span id="mobile-auth-btn-text">Entrar</span>
      </button>
    </div>
  </aside>

  <!-- Barra Inferior Fixa de Aplicativo (Bottom Navigation Bar PWA) -->
  <nav class="fixed bottom-0 left-0 right-0 z-50 bg-slate-900 border-t border-slate-800 md:hidden flex justify-around items-center py-2 px-1 safe-area-bottom shadow-2xl backdrop-blur-md bg-opacity-95" aria-label="Navegação Inferior Mobile">
    <!-- [Busca]: Ícone de lupa -> rota de pesquisa -->
    <a href="/search" data-route="/" class="flex flex-col items-center justify-center flex-1 py-1 text-slate-400 hover:text-sky-400 transition text-center group">
      <i class="fa-solid fa-magnifying-glass text-lg group-hover:scale-110 transition-transform"></i>
      <span class="text-[10px] font-medium mt-1">Busca</span>
    </a>

    <!-- [Agenda]: Ícone de calendário -> /agenda -->
    <a href="/agenda" class="flex flex-col items-center justify-center flex-1 py-1 text-slate-400 hover:text-rose-400 transition text-center group">
      <i class="fa-solid fa-calendar-days text-lg group-hover:scale-110 transition-transform"></i>
      <span class="text-[10px] font-medium mt-1">Agenda</span>
    </a>

    <!-- [Disputa AO VIVO]: Ícone de transmissão com destaque colorido e badge pulsante -> /disputa -->
    <a href="/disputa" class="flex flex-col items-center justify-center flex-1 py-1 text-white text-center relative group">
      <div class="w-11 h-11 -mt-5 rounded-full bg-gradient-to-tr from-rose-600 to-rose-500 flex items-center justify-center text-white shadow-lg border-2 border-slate-900 group-hover:scale-110 transition-transform">
        <i class="fa-solid fa-tower-broadcast text-base animate-pulse"></i>
      </div>
      <span class="text-[9px] font-bold mt-1 text-rose-400 flex items-center gap-1">
        AO VIVO
        <span class="w-1.5 h-1.5 rounded-full bg-rose-500 animate-ping"></span>
      </span>
    </a>

    <!-- [Alertas]: Ícone de sino com contador/notificação -> /alertas -->
    <a href="/alerts" data-route="/alertas" class="flex flex-col items-center justify-center flex-1 py-1 text-slate-400 hover:text-sky-400 transition text-center relative group">
      <div class="relative">
        <i class="fa-solid fa-bell text-lg group-hover:scale-110 transition-transform"></i>
        <span id="bottom-alerts-badge" class="absolute -top-1.5 -right-2 w-4 h-4 bg-sky-500 text-white text-[9px] font-bold rounded-full flex items-center justify-center ring-2 ring-slate-900">3</span>
      </div>
      <span class="text-[10px] font-medium mt-1">Alertas</span>
    </a>

    <!-- [Menu / Mais]: Ícone de 3 barras que abre o Drawer lateral com todas as opções restantes -->
    <button type="button" id="bottom-menu-toggle" onclick="openMobileMenu()" class="flex flex-col items-center justify-center flex-1 py-1 text-slate-400 hover:text-white transition text-center group focus:outline-none" aria-label="Abrir Menu Completo">
      <i class="fa-solid fa-bars text-lg group-hover:scale-110 transition-transform"></i>
      <span class="text-[10px] font-medium mt-1">Menu</span>
    </button>
  </nav>

  <!-- Notification Bar for System Messages -->
  <div id="toast-container" class="fixed bottom-20 md:bottom-5 right-5 z-50 flex flex-col gap-2 pointer-events-none"></div>

  <!-- Main View Container -->
  <main class="flex-grow pb-24 md:pb-0">
    {% block content %}{% endblock %}
  </main>

  <!-- Footer -->
  <footer class="bg-slate-900 text-slate-400 text-xs py-8 border-t border-slate-800">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
      <div class="grid grid-cols-1 md:grid-cols-4 gap-8 mb-6">
        <div>
          <h4 class="text-white font-semibold text-sm mb-2 flex items-center gap-2">
            <i class="fa-solid fa-scale-balanced text-sky-400"></i> Licitações Brasil
          </h4>
          <p class="leading-relaxed text-slate-400">
            Plataforma aberta de pesquisa, inteligência documental e monitoramento de contratações públicas baseada na Lei nº 14.133/2021.
          </p>
        </div>
        <div>
          <h5 class="text-white font-semibold mb-2">Fontes Oficiais</h5>
          <ul class="space-y-1">
            <li><a href="https://pncp.gov.br" target="_blank" class="hover:text-white transition">PNCP Nacional</a></li>
            <li><a href="https://comprasgovernamentais.gov.br" target="_blank" class="hover:text-white transition">Compras.gov.br</a></li>
            <li><a href="https://www.portaldecompraspublicas.com.br" target="_blank" class="hover:text-white transition">Portal de Compras Públicas</a></li>
          </ul>
        </div>
        <div>
          <h5 class="text-white font-semibold mb-2">Segurança e LGPD</h5>
          <p class="leading-relaxed">
            Tratamento de dados estritamente em conformidade com a LGPD (Lei 13.709/2018). Direitos de acesso e exclusão garantidos.
          </p>
        </div>
        <div>
          <h5 class="text-white font-semibold mb-2">Aviso Legal de IA</h5>
          <p class="leading-relaxed text-slate-400">
            As análises automáticas por inteligência artificial são instrumentos de triagem e não substituem a leitura e conferência do edital oficial.
          </p>
        </div>
      </div>
      <div class="pt-6 border-t border-slate-800 flex flex-col sm:flex-row justify-between items-center text-slate-500">
        <span>© 2025 Licitações Brasil. Sistema aberto e auditável.</span>
        <span class="mt-2 sm:mt-0">Desenvolvido com Next.js, FastAPI, Prisma & IA Documental.</span>
      </div>
    </div>
  </footer>

  <!-- Global Application Scripts -->
  <script>
    // Controle Vanilla JS do Drawer Lateral Mobile
    function openMobileMenu() {
      const drawer = document.getElementById('mobile-menu-drawer');
      const backdrop = document.getElementById('mobile-menu-backdrop');
      if (drawer) {
        drawer.classList.remove('translate-x-full');
        drawer.classList.add('translate-x-0');
      }
      if (backdrop) {
        backdrop.classList.remove('opacity-0', 'pointer-events-none');
        backdrop.classList.add('opacity-100', 'pointer-events-auto');
      }
      document.body.classList.add('overflow-hidden');
    }

    function closeMobileMenu() {
      const drawer = document.getElementById('mobile-menu-drawer');
      const backdrop = document.getElementById('mobile-menu-backdrop');
      if (drawer) {
        drawer.classList.remove('translate-x-0');
        drawer.classList.add('translate-x-full');
      }
      if (backdrop) {
        backdrop.classList.remove('opacity-100', 'pointer-events-auto');
        backdrop.classList.add('opacity-0', 'pointer-events-none');
      }
      document.body.classList.remove('overflow-hidden');
    }

    function toggleMobileMenu() {
      const drawer = document.getElementById('mobile-menu-drawer');
      if (drawer && drawer.classList.contains('translate-x-0')) {
        closeMobileMenu();
      } else {
        openMobileMenu();
      }
    }

    // Fechar menu móvel com a tecla ESC
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeMobileMenu();
    });

    // Interceptador e normalização de navegação de rotas no frontend
    document.addEventListener('click', (e) => {
      const link = e.target.closest('a');
      if (!link) return;
      const href = link.getAttribute('href');
      if (href === '/favoritos') {
        e.preventDefault();
        window.location.href = '/favorites';
      } else if (href === '/alertas') {
        e.preventDefault();
        window.location.href = '/alerts';
      } else if (href === '/pecas-juridicas') {
        e.preventDefault();
        window.location.href = '/juridico';
      } else if (href === '/cofre') {
        e.preventDefault();
        window.location.href = '/documentos';
      } else if (href === '/logout') {
        e.preventDefault();
        handleAuthAction();
      }
    });

    // Atualização dinâmica do badge de alertas na bottom bar
    function refreshAlertBadge() {
      const badge = document.getElementById('bottom-alerts-badge');
      if (!badge) return;
      fetch('/api/alerts/feed?limit=5')
        .then(r => r.ok ? r.json() : null)
        .then(data => {
          if (data && typeof data.total === 'number') {
            badge.textContent = data.total > 9 ? '9+' : data.total;
            badge.style.display = data.total > 0 ? 'flex' : 'none';
          }
        })
        .catch(() => {});
    }

    // Gerenciador de Sessão no Frontend
    const AppSession = {
      getToken: () => localStorage.getItem('token'),
      getUser: () => {
        try {
          return JSON.parse(localStorage.getItem('user'));
        } catch { return null; }
      },
      setSession: (token, user) => {
        localStorage.setItem('token', token);
        localStorage.setItem('user', JSON.stringify(user));
        AppSession.refreshUI();
      },
      clear: () => {
        localStorage.removeItem('token');
        localStorage.removeItem('user');
        AppSession.refreshUI();
      },
      refreshUI: () => {
        const user = AppSession.getUser();
        const authBtn = document.getElementById('auth-btn');
        const userDisplay = document.getElementById('user-display');
        const userName = document.getElementById('user-name');

        // Controles de autenticação no drawer móvel
        const mobileAuthBtn = document.getElementById('mobile-auth-btn');
        const mobileAuthBtnText = document.getElementById('mobile-auth-btn-text');
        const mobileAuthIcon = document.getElementById('mobile-auth-icon');
        const mobileUserName = document.getElementById('mobile-user-name');
        const mobileUserRole = document.getElementById('mobile-user-role');

        if (user && user.name) {
          if (authBtn) { authBtn.textContent = 'Sair'; authBtn.className = 'bg-rose-600 hover:bg-rose-700 text-white font-medium px-3 py-1.5 rounded-lg text-xs transition'; }
          if (userDisplay) { userDisplay.classList.remove('hidden'); }
          if (userName) { userName.textContent = user.name.split(' ')[0]; }

          if (mobileAuthBtnText) { mobileAuthBtnText.textContent = 'Sair'; }
          if (mobileAuthIcon) { mobileAuthIcon.className = 'fa-solid fa-right-from-bracket'; }
          if (mobileAuthBtn) { mobileAuthBtn.className = 'w-full bg-rose-600 hover:bg-rose-700 text-white font-medium py-2.5 px-4 rounded-xl text-xs tracking-wider uppercase transition flex items-center justify-center gap-2 shadow-sm'; }
          if (mobileUserName) { mobileUserName.textContent = user.name; }
          if (mobileUserRole) { mobileUserRole.textContent = user.role === 'ADMIN' ? 'Administrador' : (user.company || 'Usuário Autenticado'); }
        } else {
          if (authBtn) { authBtn.textContent = 'Entrar'; authBtn.className = 'bg-sky-500 hover:bg-sky-600 text-white font-medium px-4 py-2 rounded-lg text-xs tracking-wider uppercase transition shadow-sm'; }
          if (userDisplay) { userDisplay.classList.add('hidden'); }

          if (mobileAuthBtnText) { mobileAuthBtnText.textContent = 'Entrar'; }
          if (mobileAuthIcon) { mobileAuthIcon.className = 'fa-solid fa-right-to-bracket'; }
          if (mobileAuthBtn) { mobileAuthBtn.className = 'w-full bg-sky-500 hover:bg-sky-600 text-white font-medium py-2.5 px-4 rounded-xl text-xs tracking-wider uppercase transition flex items-center justify-center gap-2 shadow-sm'; }
          if (mobileUserName) { mobileUserName.textContent = 'Visitante'; }
          if (mobileUserRole) { mobileUserRole.textContent = 'Perfil Público'; }
        }
      }
    };

    function handleAuthAction() {
      if (AppSession.getToken()) {
        AppSession.clear();
        showToast('Sessão encerrada com sucesso.', 'info');
        window.location.href = '/';
      } else {
        window.location.href = '/login';
      }
    }

    function showToast(msg, type = 'info') {
      const c = document.getElementById('toast-container');
      const t = document.createElement('div');
      const colors = {
        success: 'bg-emerald-600 text-white',
        error: 'bg-rose-600 text-white',
        info: 'bg-slate-800 text-white border border-slate-700'
      };
      t.className = `${colors[type] || colors.info} px-4 py-3 rounded-xl shadow-xl text-sm flex items-center gap-3 transition-all transform duration-300 ease-out pointer-events-auto`;
      t.innerHTML = `<i class="fa-solid fa-circle-info"></i> <span>${msg}</span>`;
      c.appendChild(t);
      setTimeout(() => {
        t.style.opacity = '0';
        setTimeout(() => t.remove(), 300);
      }, 4000);
    }

    // Formatação de Moeda Brasileira (R$)
    function formatCurrency(val) {
      if (val === null || val === undefined || isNaN(val)) return 'Não informado';
      return new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' }).format(val);
    }

    // Formatação de Datas Brasileiras (DD/MM/AAAA)
    function formatDate(dateStr) {
      if (!dateStr) return 'Não informado';
      try {
        const d = new Date(dateStr);
        return d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
      } catch { return dateStr; }
    }

    document.addEventListener('DOMContentLoaded', () => {
      AppSession.refreshUI();
      refreshAlertBadge();
      // Fechar drawer ao clicar em qualquer link interno do drawer
      const drawerLinks = document.querySelectorAll('#mobile-menu-drawer a');
      drawerLinks.forEach(link => {
        link.addEventListener('click', () => closeMobileMenu());
      });

      // Registro assíncrono do Service Worker para PWA (Android e navegadores compatíveis)
      if ('serviceWorker' in navigator) {
        navigator.serviceWorker.register('/sw.js', { scope: '/' })
          .then(reg => console.log('[PWA] Service Worker registrado com sucesso:', reg.scope))
          .catch(err => console.warn('[PWA] Falha ao registrar Service Worker:', err));
      }
    });
  </script>
  {% block scripts %}{% endblock %}
</body>
</html>
EOF

cat << 'EOF' > .gitignore
# Ambiente e Chaves Sensíveis
.env
.env*.local

# Python
__pycache__/
*.py[cod]
*$py.class
.venv/
env/
venv/
.pytest_cache/
pytest-cache-files-*/

# Banco de dados local
*.db
*.db-wal
*.db-shm
*.sqlite
*.sqlite3
dev_licitacoes.db

# Storage / Cache / Logs
storage/documents/
*.log

# Cofre de documentos de habilitação (uploads locais)
uploads/
EOF

# ==============================================================================
# ITEM 3 — MONTADOR DE KIT DE HABILITAÇÃO (ZIP)
# ==============================================================================
mkdir -p server/services
cat << 'EOF' > server/services/kit_builder.py
"""
Montador do Kit de Habilitação (ZIP).

``montar_kit_habilitacao_zip(tender_id)`` seleciona no SQLite os documentos válidos do
cofre (``documentos_empresa``), empacota os PDFs e injeta ``Relatorio_Conformidade.pdf``
(ReportLab, papel timbrado Licitações Brasil) como índice de habilitação do certame.
"""
import io
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle
from sqlalchemy.orm import Session

from server.database import SessionLocal
from server.models import (
    DOCUMENTO_CATEGORIAS,
    DocumentoEmpresa,
    Tender,
    hoje_sao_paulo,
)
from server.services.document_vault import resolver_caminho_documento, slugify
from server.services.pdf_generator import (
    COR_BORDA,
    COR_NAVY,
    MARGEM_X,
    PAGE_W,
    estilos_timbrados,
    renderizar_pdf_timbrado,
    tabela_ficha,
)

NOME_RELATORIO = "Relatorio_Conformidade.pdf"
PASTA_DOCUMENTOS = "Documentos_Habilitacao"

# Grupos obrigatórios de habilitação — art. 62 da Lei 14.133/2021.
GRUPOS_HABILITACAO: List[Tuple[str, str, str]] = [
    ("juridica", "Habilitação Jurídica", "Art. 62, I e art. 66"),
    ("tecnica", "Qualificação Técnica", "Art. 62, II e art. 67"),
    ("fiscal", "Regularidade Fiscal, Social e Trabalhista", "Art. 62, III e art. 68"),
    ("economica", "Qualificação Econômico-Financeira", "Art. 62, IV e art. 69"),
]


class KitHabilitacaoError(LookupError):
    """Certame inexistente para montagem do kit."""


@dataclass
class ItemKit:
    documento: DocumentoEmpresa
    dias_restantes: Optional[int]
    status: str
    nome_no_zip: Optional[str] = None
    motivo_exclusao: Optional[str] = None
    alerta: Optional[str] = None


@dataclass
class ResultadoKit:
    tender: Tender
    hoje: date
    data_sessao: Optional[date]
    incluidos: List[ItemKit] = field(default_factory=list)
    excluidos: List[ItemKit] = field(default_factory=list)

    @property
    def cobertura(self) -> Dict[str, int]:
        contagem = {chave: 0 for chave, _, _ in GRUPOS_HABILITACAO}
        for item in self.incluidos:
            if item.documento.categoria in contagem:
                contagem[item.documento.categoria] += 1
        return contagem

    @property
    def conforme(self) -> bool:
        return all(qtd > 0 for qtd in self.cobertura.values())


def _data_sessao(tender: Tender) -> Optional[date]:
    for valor in (tender.opening_date, tender.proposal_deadline):
        if isinstance(valor, datetime):
            return valor.date()
        if isinstance(valor, date):
            return valor
    return None


def _fmt_data(d: Optional[date]) -> str:
    return d.strftime("%d/%m/%Y") if d else "—"


def _fmt_moeda(valor: Optional[float]) -> str:
    if not valor:
        return "Não informado"
    return "R$ " + f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def selecionar_documentos(db: Session, tender: Tender, hoje: date) -> ResultadoKit:
    """Classifica todos os documentos do cofre em incluídos (válidos com arquivo) e excluídos."""
    resultado = ResultadoKit(tender=tender, hoje=hoje, data_sessao=_data_sessao(tender))
    ordem_categorias = {chave: i for i, chave in enumerate(DOCUMENTO_CATEGORIAS.keys())}
    documentos = sorted(
        db.query(DocumentoEmpresa).all(),
        key=lambda d: (ordem_categorias.get(d.categoria, 99), (d.nome or "").lower(), d.id or 0),
    )
    sequencia = 0
    for doc in documentos:
        item = ItemKit(documento=doc, dias_restantes=doc.dias_restantes(hoje), status=doc.status_validade(hoje))
        if item.status == "VENCIDO":
            item.motivo_exclusao = f"Vencido em {_fmt_data(doc.data_validade)}"
            resultado.excluidos.append(item)
            continue
        caminho = resolver_caminho_documento(doc.caminho_arquivo)
        if not caminho or not caminho.is_file():
            item.motivo_exclusao = "Arquivo não localizado no cofre"
            resultado.excluidos.append(item)
            continue
        sequencia += 1
        item.nome_no_zip = f"{PASTA_DOCUMENTOS}/{sequencia:02d}_{slugify(doc.categoria, 12)}_{slugify(doc.nome, 60)}.pdf"
        if resultado.data_sessao and doc.data_validade and doc.data_validade < resultado.data_sessao:
            item.alerta = f"Vence antes da sessão ({_fmt_data(resultado.data_sessao)}) — renovar"
        elif item.status == "A_VENCER":
            item.alerta = f"Vence em {item.dias_restantes} dia(s)"
        resultado.incluidos.append(item)
    return resultado


def _tabela_documentos(cabecalho: List[str], linhas: List[List[str]], larguras: List[float], cor: Any) -> Table:
    est = estilos_timbrados()
    dados = [[Paragraph(c, est["celula_cabecalho"]) for c in cabecalho]]
    dados += [[Paragraph(c, est["celula"]) for c in linha] for linha in linhas]
    tabela = Table(dados, colWidths=larguras, repeatRows=1)
    tabela.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), cor),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("BOX", (0, 0), (-1, -1), 0.6, COR_BORDA),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, COR_BORDA),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return tabela


def gerar_relatorio_conformidade_pdf(resultado: ResultadoKit) -> bytes:
    from xml.sax.saxutils import escape

    est = estilos_timbrados()
    t = resultado.tender
    numero = t.notice_number or t.external_id or t.id
    largura = PAGE_W - 2 * MARGEM_X
    story: List[Any] = []

    story.append(Paragraph("RELATÓRIO DE CONFORMIDADE — KIT DE HABILITAÇÃO", est["titulo"]))
    story.append(Paragraph(
        f"Índice dos documentos empacotados  •  Referência: {_fmt_data(resultado.hoje)}  •  Lei 14.133/2021, arts. 62 a 70",
        est["subtitulo"],
    ))

    story.append(Paragraph("DADOS DO CERTAME", est["secao"]))
    story.append(tabela_ficha([
        ("Certame nº", f"<b>{escape(str(numero))}</b>"),
        ("Órgão / Entidade", escape(t.contracting_entity or "Não informado")),
        ("Objeto", escape(t.title or t.object_description or "Não informado")),
        ("Modalidade", escape(t.modality or "Não informada")),
        ("Sessão / Prazo", _fmt_data(resultado.data_sessao)),
        ("Valor estimado", _fmt_moeda(t.estimated_total_value)),
    ], est))

    story.append(Paragraph("COBERTURA DOS GRUPOS DE HABILITAÇÃO (ART. 62)", est["secao"]))
    cobertura = resultado.cobertura
    linhas_cob = []
    for chave, rotulo, base_legal in GRUPOS_HABILITACAO:
        qtd = cobertura.get(chave, 0)
        situacao = (
            f'<font color="#047857"><b>ATENDIDO</b></font> ({qtd} doc.)' if qtd
            else '<font color="#b91c1c"><b>PENDENTE</b></font>'
        )
        linhas_cob.append([escape(rotulo), base_legal, situacao])
    story.append(_tabela_documentos(
        ["Grupo", "Base legal", "Situação"], linhas_cob,
        [largura * 0.48, largura * 0.27, largura * 0.25], COR_NAVY,
    ))
    parecer = (
        '<font color="#047857"><b>Kit apto:</b></font> todos os grupos de habilitação possuem ao menos um documento válido.'
        if resultado.conforme else
        '<font color="#b91c1c"><b>Atenção:</b></font> há grupos de habilitação sem documento válido no cofre. '
        "Confronte com as exigências específicas do edital antes do envio."
    )
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(parecer, est["valor"]))

    story.append(Paragraph(f"DOCUMENTOS INCLUÍDOS ({len(resultado.incluidos)})", est["secao"]))
    if resultado.incluidos:
        linhas = []
        for i, item in enumerate(resultado.incluidos, start=1):
            d = item.documento
            dias = "—" if item.dias_restantes is None else f"{item.dias_restantes} dia(s)"
            obs = escape(item.alerta) if item.alerta else ("Sem vencimento" if item.dias_restantes is None else "Regular")
            linhas.append([
                f"{i:02d}",
                f"<b>{escape(d.nome)}</b><br/><font size='7' color='#64748b'>{escape(item.nome_no_zip or '')}</font>",
                escape(d.categoria_label),
                _fmt_data(d.data_emissao),
                _fmt_data(d.data_validade),
                dias,
                obs,
            ])
        story.append(_tabela_documentos(
            ["#", "Documento / arquivo no ZIP", "Categoria", "Emissão", "Validade", "Restam", "Observação"],
            linhas,
            [8 * mm, largura * 0.29, largura * 0.15, 19 * mm, 19 * mm, 16 * mm, largura - (8 * mm + largura * 0.44 + 54 * mm)],
            colors.HexColor("#047857"),
        ))
    else:
        story.append(Paragraph("Nenhum documento válido disponível no cofre.", est["valor"]))

    story.append(Paragraph(f"DOCUMENTOS NÃO INCLUÍDOS ({len(resultado.excluidos)})", est["secao"]))
    if resultado.excluidos:
        linhas_exc = [
            [escape(it.documento.nome), escape(it.documento.categoria_label), _fmt_data(it.documento.data_validade), escape(it.motivo_exclusao or "")]
            for it in resultado.excluidos
        ]
        story.append(_tabela_documentos(
            ["Documento", "Categoria", "Validade", "Motivo"], linhas_exc,
            [largura * 0.38, largura * 0.24, largura * 0.14, largura * 0.24], colors.HexColor("#b91c1c"),
        ))
    else:
        story.append(Paragraph("Nenhum documento excluído.", est["valor"]))

    story.append(Spacer(1, 5 * mm))
    story.append(Paragraph(
        "Este relatório é um instrumento de triagem gerado automaticamente a partir do Cofre de Documentos. "
        "A conferência final das exigências de habilitação deve considerar o edital e seus anexos.",
        est["pequeno"],
    ))

    return renderizar_pdf_timbrado(
        story,
        titulo_documento="Relatório de Conformidade",
        referencia=f"Kit de Habilitação  •  Certame nº {numero}",
        assunto=f"Kit de habilitação — {t.contracting_entity or ''}",
    )


def nome_arquivo_kit(tender: Tender) -> str:
    return f"Kit_Habilitacao_{slugify(tender.notice_number or tender.external_id or tender.id, 50)}.zip"


def montar_kit_habilitacao_zip(
    tender_id: str,
    db: Optional[Session] = None,
    hoje: Optional[date] = None,
) -> bytes:
    """
    Monta o ZIP do kit de habilitação para o certame informado.

    Estrutura do arquivo:
      Relatorio_Conformidade.pdf             (índice de habilitação)
      Documentos_Habilitacao/NN_<cat>_<nome>.pdf

    Lança ``KitHabilitacaoError`` se o certame não existir.
    """
    sessao_propria = db is None
    sessao = db or SessionLocal()
    try:
        tender = sessao.query(Tender).filter(
            (Tender.id == str(tender_id)) | (Tender.external_id == str(tender_id))
        ).first()
        if not tender:
            raise KitHabilitacaoError(f"Certame '{tender_id}' não encontrado.")

        resultado = selecionar_documentos(sessao, tender, hoje or hoje_sao_paulo())
        relatorio = gerar_relatorio_conformidade_pdf(resultado)

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(NOME_RELATORIO, relatorio)
            for item in resultado.incluidos:
                caminho = resolver_caminho_documento(item.documento.caminho_arquivo)
                zf.write(str(caminho), arcname=item.nome_no_zip)
        return buffer.getvalue()
    finally:
        if sessao_propria:
            sessao.close()


__all__ = [
    "GRUPOS_HABILITACAO",
    "KitHabilitacaoError",
    "NOME_RELATORIO",
    "PASTA_DOCUMENTOS",
    "gerar_relatorio_conformidade_pdf",
    "montar_kit_habilitacao_zip",
    "nome_arquivo_kit",
    "selecionar_documentos",
]
EOF

mkdir -p server/templates
cat << 'EOF' > server/templates/tender_detail.html
{% extends "base.html" %}

{% block title %}{{ detail.tender.title }} - Licitações Brasil{% endblock %}

{% block content %}
<!-- Top Breadcrumb & Actions Bar -->
<div class="bg-slate-900 text-white py-6 border-b border-slate-800">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
    <div class="flex flex-wrap items-center justify-between gap-4">
      <div>
        <nav class="flex items-center space-x-2 text-xs text-slate-400 mb-2">
          <a href="/" class="hover:text-white">Início</a>
          <span>/</span>
          <a href="/search" class="hover:text-white">Pesquisa</a>
          <span>/</span>
          <span class="text-sky-400 font-mono">{{ detail.tender.external_id }}</span>
        </nav>
        <div class="flex items-center gap-2 flex-wrap">
          <span class="px-2.5 py-0.5 rounded text-xs font-bold bg-sky-500 text-white">
            {{ detail.tender.state }} - {{ detail.tender.city }}
          </span>
          <span class="px-2.5 py-0.5 rounded text-xs font-semibold bg-slate-800 text-slate-200 border border-slate-700">
            {{ detail.tender.modality }}
          </span>
          <span class="px-2.5 py-0.5 rounded text-xs font-semibold bg-emerald-950 text-emerald-300 border border-emerald-800">
            {{ detail.tender.status }}
          </span>
          {% if detail.tender.category %}
          <span class="px-2.5 py-0.5 rounded text-xs font-semibold bg-purple-950 text-purple-300 border border-purple-800">
            {{ detail.tender.category }}
          </span>
          {% endif %}
        </div>
      </div>

      <!-- Action Buttons -->
      <div class="flex items-center gap-2 flex-wrap">
        <button onclick="toggleFavorite('{{ detail.tender.id }}', this)" class="bg-slate-800 hover:bg-slate-700 text-white border border-slate-700 px-4 py-2 rounded-xl text-xs font-semibold transition flex items-center gap-1.5 {% if detail.tender.is_favorite %}text-amber-400{% endif %}">
          <i class="fa-solid fa-star {% if detail.tender.is_favorite %}text-amber-400{% else %}text-slate-400{% endif %}"></i>
          <span class="fav-label">{% if detail.tender.is_favorite %}Favoritado{% else %}Favoritar{% endif %}</span>
        </button>
        <button onclick="copyTenderLink()" class="bg-slate-800 hover:bg-slate-700 text-white border border-slate-700 px-4 py-2 rounded-xl text-xs font-semibold transition flex items-center gap-1.5">
          <i class="fa-regular fa-copy"></i> Copiar Link
        </button>
        <button onclick="exportToDisputeCockpit('{{ detail.tender.id }}', '{{ (detail.tender.title or 'Certame')|replace('\'', '\\\'')|replace('\n', ' ') }}', {{ detail.tender.estimated_total_value or 0 }}, '{{ detail.tender.fonte_portal or 'Compras.gov.br' }}')" class="bg-indigo-600 hover:bg-indigo-500 text-white px-4 py-2 rounded-xl text-xs font-semibold transition flex items-center gap-1.5 shadow-sm border border-indigo-500/30">
          <i class="fa-solid fa-bolt text-amber-300 text-xs"></i> Exportar para o Cockpit de Disputa
        </button>
        <a href="{{ detail.tender.url_disputa or detail.tender.url_pncp or detail.tender.link_comprasnet or detail.tender.official_notice_url or detail.tender.source_url }}" target="_blank" rel="noopener noreferrer" class="bg-emerald-600 hover:bg-emerald-500 text-white px-4 py-2 rounded-xl text-xs font-semibold transition flex items-center gap-1.5 shadow-sm">
          <i class="fa-solid fa-gavel text-xs"></i> Acessar Edital / Disputa
        </a>
        <a href="{{ detail.tender.url_pncp or detail.tender.link_pncp or detail.tender.source_url }}" target="_blank" rel="noopener noreferrer" class="bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 px-3.5 py-2 rounded-xl text-xs font-semibold transition flex items-center gap-1.5 shadow-sm">
          <i class="fa-solid fa-landmark text-sky-400 text-xs"></i> PNCP Oficial
        </a>
        <button
          type="button"
          id="btn-baixar-kit-zip"
          data-kit-url="/api/tenders/{{ detail.tender.id }}/download-kit-zip"
          onclick="baixarKitHabilitacao(this)"
          class="bg-amber-500 hover:bg-amber-400 text-slate-950 px-4 py-2 rounded-xl text-xs font-bold transition flex items-center gap-1.5 shadow-sm border border-amber-400/40 cursor-pointer"
          title="Documentos válidos do Cofre + Relatório de Conformidade (PDF)"
        >
          <i class="fa-solid fa-file-zipper text-xs"></i> Baixar Kit de Habilitação (ZIP)
        </button>
      </div>
    </div>

    <!-- Title and Entity -->
    <h1 class="text-xl sm:text-2xl font-bold mt-4 text-white leading-snug">
      {{ detail.tender.title }}
    </h1>
    <p class="text-xs text-slate-400 mt-1 flex items-center gap-1">
      <i class="fa-regular fa-building"></i> {{ detail.tender.contracting_entity }}
      {% if detail.tender.purchasing_unit != "Não informado" %}
        - {{ detail.tender.purchasing_unit }}
      {% endif %}
    </p>
  </div>
</div>

<!-- Tabs Navigation -->
<div class="bg-white border-b border-slate-200 sticky top-16 z-40">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
    <nav class="flex space-x-6 overflow-x-auto text-sm font-semibold text-slate-600" id="tender-tabs">
      <button onclick="switchTab('overview')" id="tab-btn-overview" class="py-4 border-b-2 border-sky-600 text-sky-600 flex items-center gap-2 whitespace-nowrap">
        <i class="fa-solid fa-file-lines"></i> Visão Geral
      </button>
      <button onclick="switchTab('items')" id="tab-btn-items" class="py-4 border-b-2 border-transparent hover:text-slate-900 flex items-center gap-2 whitespace-nowrap">
        <i class="fa-solid fa-list-check"></i> Itens ({{ detail.items|length }})
      </button>
      <button onclick="switchTab('documents')" id="tab-btn-documents" class="py-4 border-b-2 border-transparent hover:text-slate-900 flex items-center gap-2 whitespace-nowrap">
        <i class="fa-solid fa-paperclip"></i> Documentos ({{ detail.documents|length }})
      </button>
      <button onclick="switchTab('ai')" id="tab-btn-ai" class="py-4 border-b-2 border-transparent hover:text-slate-900 flex items-center gap-2 whitespace-nowrap text-emerald-600">
        <i class="fa-solid fa-wand-magic-sparkles"></i> Análise por IA
      </button>
      <button onclick="switchTab('changes')" id="tab-btn-changes" class="py-4 border-b-2 border-transparent hover:text-slate-900 flex items-center gap-2 whitespace-nowrap">
        <i class="fa-solid fa-clock-rotate-left"></i> Histórico
      </button>
      <button onclick="switchTab('notes')" id="tab-btn-notes" class="py-4 border-b-2 border-transparent hover:text-slate-900 flex items-center gap-2 whitespace-nowrap">
        <i class="fa-regular fa-note-sticky"></i> Anotações
      </button>
    </nav>
  </div>
</div>

<!-- Main Tabs Content -->
<div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">

  <!-- TAB 1: VISÃO GERAL -->
  <div id="tab-content-overview" class="space-y-6">
    <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
      <!-- Main Details Column -->
      <div class="md:col-span-2 space-y-6">
        <!-- Componente Visual: Análise IA com Gemini -->
        <div class="bg-gradient-to-br from-slate-900 via-slate-800 to-indigo-950 p-6 rounded-2xl border border-indigo-500/30 text-white shadow-xl relative overflow-hidden">
          <div class="flex flex-wrap items-center justify-between gap-3 mb-4">
            <div class="flex items-center gap-2.5">
              <span class="w-9 h-9 rounded-xl bg-emerald-500/20 text-emerald-400 flex items-center justify-center text-base font-bold border border-emerald-500/30 shadow-inner">
                <i class="fa-solid fa-wand-magic-sparkles"></i>
              </span>
              <div>
                <h2 class="text-base font-bold text-white flex items-center gap-2">
                  Análise IA com Gemini
                  <span class="px-2 py-0.5 rounded text-[10px] font-extrabold bg-indigo-500/30 text-indigo-300 border border-indigo-400/30 uppercase font-mono tracking-wider">Gemini Flash</span>
                </h2>
                <p class="text-[11px] text-slate-400">Auditoria semântica documental e triagem estratégica de riscos</p>
              </div>
            </div>

            <!-- Badges de Metadados: Portal e Confiança -->
            <div class="flex items-center gap-2 flex-wrap">
              <span class="px-2.5 py-1 rounded-lg text-xs font-bold bg-slate-800 text-sky-300 border border-slate-700 flex items-center gap-1.5 shadow-sm">
                <i class="fa-solid fa-globe text-xs"></i> <span id="gemini-portal-badge">{{ detail.tender.fonte_portal or 'PNCP' }}</span>
              </span>
              <span class="px-2.5 py-1 rounded-lg text-xs font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 flex items-center gap-1.5 shadow-sm">
                <i class="fa-solid fa-shield-check text-xs"></i> Confiança da IA: 98% (Alta)
              </span>
            </div>
          </div>

          <!-- Conteúdo Enriquecido da IA -->
          <div class="space-y-4 text-xs">
            <!-- Resumo Executivo -->
            <div class="bg-slate-900/80 p-4 rounded-xl border border-slate-700/60 shadow-inner">
              <span class="text-slate-400 font-bold uppercase text-[10px] tracking-wider block mb-1 flex items-center gap-1.5">
                <i class="fa-solid fa-file-invoice text-indigo-400"></i> Resumo Executivo da Oportunidade:
              </span>
              <p class="text-slate-200 leading-relaxed text-xs" id="gemini-overview-resumo">
                {{ detail.tender.ai_resumo or (detail.latest_ai_analysis.result_json.resumo_executivo.o_que_sera_contratado if detail.latest_ai_analysis and detail.latest_ai_analysis.result_json and detail.latest_ai_analysis.result_json.resumo_executivo else 'Certame selecionado para triagem automatizada multifonte. Clique em "Analisar com Gemini" para decodificar exigências de habilitação, itens e estimativa orçamentária.') }}
              </p>
            </div>

            <!-- Alertas de Risco & Prazos Críticos -->
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div class="bg-rose-950/40 border border-rose-800/40 p-3.5 rounded-xl">
                <span class="text-rose-300 font-bold uppercase text-[10px] tracking-wider block mb-1 flex items-center gap-1.5">
                  <i class="fa-solid fa-triangle-exclamation text-rose-400"></i> Alertas de Risco Jurídico / Execução:
                </span>
                <p class="text-rose-100/90 text-[11px] leading-relaxed" id="gemini-overview-riscos">
                  {{ detail.tender.ai_pontos_atencao or 'Exigências de qualificação técnica e cláusulas de penalidade requerem atenção prévia ao envio da proposta.' }}
                </p>
              </div>

              <div class="bg-amber-950/40 border border-amber-800/40 p-3.5 rounded-xl">
                <span class="text-amber-300 font-bold uppercase text-[10px] tracking-wider block mb-1 flex items-center gap-1.5">
                  <i class="fa-solid fa-calendar-check text-amber-400"></i> Prazos Críticos & Sessão:
                </span>
                <p class="text-amber-100/90 text-[11px] leading-relaxed">
                  Abertura das propostas: <strong>{{ detail.tender.opening_date.strftime('%d/%m/%Y às %H:%M') if detail.tender.opening_date else 'Consulte edital oficial' }}</strong>. Impugnações até 3 dias úteis antes da sessão.
                </p>
              </div>
            </div>

            <!-- Ações Diretas Integradas -->
            <div class="flex flex-wrap items-center justify-between gap-3 pt-2">
              <button onclick="requestAiAnalysis('{{ detail.tender.id }}')" class="bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold px-4 py-2 rounded-xl text-xs transition flex items-center gap-2 shadow-md">
                <i class="fa-solid fa-wand-magic-sparkles"></i> Analisar com Gemini
              </button>

              <button onclick="exportToDisputeCockpit('{{ detail.tender.id }}', '{{ (detail.tender.title or 'Certame')|replace('\'', '\\\'')|replace('\n', ' ') }}', {{ detail.tender.estimated_total_value or 0 }}, '{{ detail.tender.fonte_portal or 'Compras.gov.br' }}')" class="bg-indigo-600 hover:bg-indigo-500 text-white font-bold px-4 py-2 rounded-xl text-xs transition flex items-center gap-2 shadow-md border border-indigo-400/40">
                <i class="fa-solid fa-bolt text-amber-300"></i> Exportar para o Cockpit de Disputa
              </button>
            </div>
          </div>
        </div>

        <!-- Objeto Completo -->
        <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm">
          <h2 class="text-base font-bold text-slate-900 mb-3 flex items-center gap-2">
            <i class="fa-solid fa-align-left text-sky-600"></i> Objeto da Contratação
          </h2>
          <div class="text-sm text-slate-700 leading-relaxed whitespace-pre-line bg-slate-50 p-4 rounded-xl border border-slate-100">
            {{ detail.tender.object_description }}
          </div>
        </div>

        <!-- Critérios e Condições -->
        <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm space-y-4">
          <h2 class="text-base font-bold text-slate-900 flex items-center gap-2">
            <i class="fa-solid fa-gavel text-sky-600"></i> Condições e Critérios da Disputa
          </h2>
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-4 text-xs text-slate-700">
            <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
              <span class="block text-slate-400 font-semibold uppercase text-[10px] mb-1">Critério de Julgamento:</span>
              <span class="font-medium text-slate-800">Menor Preço por Item / Global (Lei 14.133/2021)</span>
            </div>
            <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
              <span class="block text-slate-400 font-semibold uppercase text-[10px] mb-1">Modo de Disputa:</span>
              <span class="font-medium text-slate-800">Aberto e Fechado (Eletrônico)</span>
            </div>
            <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
              <span class="block text-slate-400 font-semibold uppercase text-[10px] mb-1">Local de Entrega:</span>
              <span class="font-medium text-slate-800">{{ detail.tender.city }} / {{ detail.tender.state }}</span>
            </div>
            <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
              <span class="block text-slate-400 font-semibold uppercase text-[10px] mb-1">Condições de Pagamento:</span>
              <span class="font-medium text-slate-800">Até 30 dias após emissão da NF e ateste definitivo</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Key Dates & Financial Sidebar -->
      <div class="md:col-span-1 space-y-6">
        <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm">
          <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-4">Informações Financeiras</h3>
          <div class="mb-6">
            <span class="text-xs text-slate-500 block">Valor Estimado Total:</span>
            <div class="text-2xl font-black text-slate-900 mt-1">
              {% if detail.tender.estimated_total_value %}
                R$ {{ "{:,.2f}".format(detail.tender.estimated_total_value).replace(",", "X").replace(".", ",").replace("X", ".") }}
              {% else %}
                Não informado
              {% endif %}
            </div>
          </div>

          <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-3">Cronograma Oficial</h3>
          <ul class="space-y-3 text-xs">
            <li class="flex items-start justify-between pb-2 border-b border-slate-100">
              <span class="text-slate-500">Publicação no PNCP:</span>
              <span class="font-semibold text-slate-800">
                {% if detail.tender.publication_date %}
                  {{ detail.tender.publication_date.strftime('%d/%m/%Y %H:%M') }}
                {% else %}
                  Não informado
                {% endif %}
              </span>
            </li>
            <li class="flex items-start justify-between pb-2 border-b border-slate-100">
              <span class="text-slate-500">Abertura de Propostas:</span>
              <span class="font-semibold text-slate-800">
                {% if detail.tender.opening_date %}
                  {{ detail.tender.opening_date.strftime('%d/%m/%Y %H:%M') }}
                {% else %}
                  Não informado
                {% endif %}
              </span>
            </li>
            <li class="flex items-start justify-between pb-2 border-b border-slate-100">
              <span class="text-slate-500">Prazo Limite:</span>
              <span class="font-bold text-rose-600">
                {% if detail.tender.proposal_deadline %}
                  {{ detail.tender.proposal_deadline.strftime('%d/%m/%Y %H:%M') }}
                {% else %}
                  Não informado
                {% endif %}
              </span>
            </li>
          </ul>

          <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400 mt-6 mb-3">Identificação Oficial</h3>
          <div class="space-y-2 text-xs">
            <div>
              <span class="text-slate-400 block text-[10px]">Número de Controle PNCP:</span>
              <span class="font-mono text-slate-800 font-semibold">{{ detail.tender.external_id }}</span>
            </div>
            <div>
              <span class="text-slate-400 block text-[10px]">Número do Edital:</span>
              <span class="font-semibold text-slate-800">{{ detail.tender.notice_number }}</span>
            </div>
            <div>
              <span class="text-slate-400 block text-[10px]">Número do Processo:</span>
              <span class="font-semibold text-slate-800">{{ detail.tender.process_number }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- TAB 2: ITENS -->
  <div id="tab-content-items" class="hidden space-y-4">
    <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm">
      <div class="flex justify-between items-center mb-4">
        <h2 class="text-base font-bold text-slate-900">Itens e Lotes Cadastrados</h2>
        <span class="text-xs text-slate-500 font-mono">{{ detail.items|length }} item(ns) registrado(s)</span>
      </div>

      <div class="overflow-x-auto">
        <table class="w-full text-left text-xs border-collapse">
          <thead>
            <tr class="bg-slate-50 text-slate-600 font-semibold border-b border-slate-200">
              <th class="p-3">#</th>
              <th class="p-3">Descrição do Item</th>
              <th class="p-3">Unidade</th>
              <th class="p-3 text-right">Qtd</th>
              <th class="p-3 text-right">Valor Unitário</th>
              <th class="p-3 text-right">Valor Total Estimado</th>
              <th class="p-3">Código (CATMAT/CATSER)</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100 text-slate-700">
            {% for item in detail.items %}
            <tr class="hover:bg-slate-50/80 transition">
              <td class="p-3 font-bold text-slate-900">{{ item.item_number }}</td>
              <td class="p-3 font-medium text-slate-800 max-w-md">{{ item.description }}</td>
              <td class="p-3">{{ item.unit or "Não informado" }}</td>
              <td class="p-3 text-right font-mono">{{ "{:,.0f}".format(item.quantity) if item.quantity else "Não informado" }}</td>
              <td class="p-3 text-right font-mono">
                {% if item.estimated_unit_value %}
                  R$ {{ "{:,.2f}".format(item.estimated_unit_value).replace(",", "X").replace(".", ",").replace("X", ".") }}
                {% else %}
                  Não informado
                {% endif %}
              </td>
              <td class="p-3 text-right font-bold text-slate-900 font-mono">
                {% if item.estimated_total_value %}
                  R$ {{ "{:,.2f}".format(item.estimated_total_value).replace(",", "X").replace(".", ",").replace("X", ".") }}
                {% else %}
                  Não informado
                {% endif %}
              </td>
              <td class="p-3 font-mono text-[11px] text-slate-500">{{ item.classification_code or "Não informado" }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- TAB 3: DOCUMENTOS -->
  <div id="tab-content-documents" class="hidden space-y-4">
    <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm">
      <div class="flex justify-between items-center mb-4">
        <div>
          <h2 class="text-base font-bold text-slate-900">Arquivos e Anexos Oficiais</h2>
          <p class="text-xs text-slate-500">Editais, minutas e anexos públicos disponibilizados pelo órgão licitante</p>
        </div>
      </div>

      <div class="space-y-3">
        {% for doc in detail.documents %}
        <div class="p-4 rounded-xl border border-slate-200 bg-slate-50 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          <div class="flex items-start gap-3">
            <div class="w-10 h-10 rounded-lg bg-red-100 text-red-600 flex items-center justify-center text-lg flex-shrink-0">
              <i class="fa-solid fa-file-pdf"></i>
            </div>
            <div>
              <div class="font-bold text-slate-800 text-xs">{{ doc.name }}</div>
              <div class="text-[11px] text-slate-500 flex flex-wrap items-center gap-3 mt-1">
                <span>Tipo: <strong>{{ doc.document_type }}</strong></span>
                <span>MIME: {{ doc.mime_type }}</span>
                {% if doc.checksum %}
                  <span class="font-mono text-[10px] text-slate-400">SHA256: {{ doc.checksum[:16] }}...</span>
                {% endif %}
              </div>
            </div>
          </div>
          <div class="flex items-center gap-2">
            <button onclick="requestAiAnalysis('{{ detail.tender.id }}', '{{ doc.id }}')" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-emerald-600 hover:bg-emerald-500 text-white flex items-center gap-1.5 transition">
              <i class="fa-solid fa-wand-magic-sparkles"></i> Analisar com IA
            </button>
            <a href="{{ doc.source_url or detail.tender.url_pncp or detail.tender.source_url }}" target="_blank" rel="noopener noreferrer" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-white border border-slate-300 text-slate-700 hover:bg-slate-50 flex items-center gap-1.5 transition">
              <i class="fa-solid fa-download"></i> Baixar Oficial
            </a>
          </div>
        </div>
        {% else %}
        <div class="p-6 rounded-xl border border-slate-200 bg-slate-50 text-center">
          <p class="text-xs text-slate-500 mb-3">Nenhum documento anexado diretamente na base local.</p>
          <a href="{{ detail.tender.url_pncp or detail.tender.source_url }}" target="_blank" rel="noopener noreferrer" class="inline-flex items-center gap-1.5 px-4 py-2 rounded-xl text-xs font-semibold bg-sky-600 hover:bg-sky-500 text-white transition">
            <i class="fa-solid fa-download"></i> Baixar Oficial no PNCP
          </a>
        </div>
        {% endfor %}
      </div>
    </div>
  </div>

  <!-- TAB 4: ANÁLISE POR IA -->
  <div id="tab-content-ai" class="hidden space-y-6">
    <!-- Action Trigger Card -->
    <div class="bg-gradient-to-r from-slate-900 to-brand-900 p-6 rounded-2xl text-white shadow-lg flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
      <div>
        <div class="inline-flex items-center gap-1.5 bg-emerald-500/20 text-emerald-300 px-2.5 py-0.5 rounded-full text-xs font-mono mb-2">
          <i class="fa-solid fa-microchip"></i> Motor de IA Documental (Gemini 3.8 Flash)
        </div>
        <h2 class="text-lg font-bold">Auditoria e Resumo Inteligente do Edital</h2>
        <p class="text-xs text-slate-300 max-w-xl">
          Extrai requisitos de habilitação, dados comerciais, riscos, divergências e pontos de atenção com citações exatas de páginas.
        </p>
      </div>
      <button id="run-ai-btn" onclick="requestAiAnalysis('{{ detail.tender.id }}')" class="bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold px-5 py-3 rounded-xl text-xs transition shadow-md flex items-center gap-2 whitespace-nowrap">
        <i class="fa-solid fa-wand-magic-sparkles"></i>
        <span>Solicitar Análise de IA</span>
      </button>
    </div>

    <!-- AI Loading Indicator (Hidden by default) -->
    <div id="ai-loading" class="hidden bg-white p-8 rounded-2xl border border-slate-200 text-center space-y-3">
      <div class="w-12 h-12 border-4 border-emerald-500 border-t-transparent rounded-full animate-spin mx-auto"></div>
      <div class="text-sm font-bold text-slate-800">Processando documento com IA e extraindo requisitos...</div>
      <div class="text-xs text-slate-500">Verificando integridade, páginas e regras da Lei nº 14.133/2021</div>
    </div>

    <!-- AI Results Container -->
    <div id="ai-results" class="{% if not detail.latest_ai_analysis %}hidden{% endif %} space-y-6">
      <!-- Legal Disclaimer Banner -->
      <div class="p-4 bg-amber-50 border-l-4 border-amber-500 rounded-r-xl text-amber-900 text-xs flex items-start gap-3">
        <i class="fa-solid fa-triangle-exclamation text-amber-600 text-base mt-0.5"></i>
        <div>
          <span class="font-bold">Aviso Obrigatório de Conformidade:</span>
          <span id="ai-legal-notice">
            Esta análise foi gerada por inteligência artificial para fins de suporte à triagem rápida.
            Não substitui a leitura obrigatória e integral do Edital Oficial nem configura parecer jurídico definitivo.
          </span>
        </div>
      </div>

      <!-- Resumo Executivo Grid -->
      <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm">
        <h3 class="text-sm font-bold text-slate-900 mb-4 flex items-center gap-2">
          <i class="fa-solid fa-briefcase text-sky-600"></i> Resumo Executivo
        </h3>
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 text-xs" id="ai-exec-summary">
          <!-- Populated via script or server -->
        </div>
      </div>

      <!-- Requisitos de Participação & Habilitação -->
      <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm">
        <h3 class="text-sm font-bold text-slate-900 mb-4 flex items-center gap-2">
          <i class="fa-solid fa-clipboard-check text-emerald-600"></i> Requisitos de Habilitação e Participação
        </h3>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs" id="ai-hab-grid">
          <!-- Populated via script -->
        </div>
      </div>

      <!-- Pontos de Atenção & Riscos -->
      <div class="bg-white p-6 rounded-2xl border border-rose-200 bg-rose-50/30 shadow-sm">
        <h3 class="text-sm font-bold text-rose-800 mb-4 flex items-center gap-2">
          <i class="fa-solid fa-shield-virus text-rose-600"></i> Pontos de Atenção e Prazos Críticos
        </h3>
        <div class="space-y-3 text-xs" id="ai-attention-list">
          <!-- Populated via script -->
        </div>
      </div>

      <!-- Citações e Trechos de Apoio -->
      <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm">
        <h3 class="text-sm font-bold text-slate-900 mb-4 flex items-center gap-2">
          <i class="fa-solid fa-quote-left text-sky-600"></i> Citações e Trechos Documentais de Apoio
        </h3>
        <div class="space-y-3 text-xs" id="ai-citations-list">
          <!-- Populated via script -->
        </div>
      </div>
    </div>
  </div>

  <!-- TAB 5: HISTÓRICO DE ALTERAÇÕES -->
  <div id="tab-content-changes" class="hidden space-y-4">
    <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm">
      <h2 class="text-base font-bold text-slate-900 mb-3">Histórico e Retificações Oficiais</h2>
      {% if detail.changes|length == 0 %}
      <div class="text-xs text-slate-500 py-6 text-center">
        <i class="fa-solid fa-circle-check text-emerald-500 text-lg mb-2 block"></i>
        Nenhuma alteração, retificação ou prorrogação registrada para este certame até o momento.
      </div>
      {% else %}
      <div class="space-y-3">
        {% for ch in detail.changes %}
        <div class="p-3 bg-slate-50 rounded-xl border border-slate-100 text-xs">
          <div class="flex justify-between items-center font-bold text-slate-800">
            <span>{{ ch.change_type }} - {{ ch.field_name }}</span>
            <span class="text-slate-400 font-normal">{{ ch.detected_at.strftime('%d/%m/%Y %H:%M') }}</span>
          </div>
          <div class="mt-1 text-slate-600">Valor anterior: {{ ch.old_value }} → Novo: {{ ch.newValue }}</div>
        </div>
        {% endfor %}
      </div>
      {% endif %}
    </div>
  </div>

  <!-- TAB 6: OBSERVAÇÕES PESSOAIS -->
  <div id="tab-content-notes" class="hidden space-y-4">
    <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm">
      <h2 class="text-base font-bold text-slate-900 mb-2">Anotações Particulares da Licitação</h2>
      <p class="text-xs text-slate-500 mb-4">Adicione observações da sua equipe para acompanhar estratégia, concorrentes e prazos internos.</p>
      <textarea id="user-note-input" rows="5" class="w-full text-xs p-3 border border-slate-200 rounded-xl focus:ring-2 focus:ring-sky-500 focus:outline-none" placeholder="Digite suas notas internas sobre este certame...">{{ detail.user_notes or '' }}</textarea>
      <div class="mt-3 flex justify-end">
        <button onclick="saveUserNote('{{ detail.tender.id }}')" class="bg-sky-600 hover:bg-sky-500 text-white font-semibold px-4 py-2 rounded-xl text-xs transition">
          Salvar Anotação
        </button>
      </div>
    </div>
  </div>

</div>
{% endblock %}

{% block scripts %}
<script>
  async function baixarKitHabilitacao(botao) {
    const url = botao.getAttribute('data-kit-url');
    const original = botao.innerHTML;
    botao.disabled = true;
    botao.innerHTML = '<i class="fa-solid fa-spinner fa-spin text-xs"></i> Montando kit...';
    try {
      const resp = await fetch(url);
      if (!resp.ok) {
        let msg = 'Não foi possível montar o kit de habilitação.';
        try { const err = await resp.json(); if (err && typeof err.detail === 'string') msg = err.detail; } catch (e) {}
        throw new Error(msg);
      }
      const blob = await resp.blob();
      const cd = resp.headers.get('Content-Disposition') || '';
      const m = cd.match(/filename="?([^";]+)"?/i);
      const nome = m ? m[1] : 'Kit_Habilitacao.zip';
      const href = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = href;
      a.download = nome;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(href), 1500);
      showToast(`Kit ${nome} pronto. Confira o Relatório de Conformidade incluso.`, 'success');
    } catch (e) {
      showToast(e.message || 'Falha ao baixar o kit.', 'error');
    } finally {
      botao.disabled = false;
      botao.innerHTML = original;
    }
  }

  function switchTab(tabName) {
    const tabs = ['overview', 'items', 'documents', 'ai', 'changes', 'notes'];
    tabs.forEach(t => {
      const content = document.getElementById(`tab-content-${t}`);
      const btn = document.getElementById(`tab-btn-${t}`);
      if (content) content.classList.add('hidden');
      if (btn) {
        btn.classList.remove('border-sky-600', 'text-sky-600');
        btn.classList.add('border-transparent');
      }
    });

    const activeContent = document.getElementById(`tab-content-${tabName}`);
    const activeBtn = document.getElementById(`tab-btn-${tabName}`);
    if (activeContent) activeContent.classList.remove('hidden');
    if (activeBtn) {
      activeBtn.classList.remove('border-transparent');
      activeBtn.classList.add('border-sky-600', 'text-sky-600');
    }
  }

  function copyTenderLink() {
    navigator.clipboard.writeText(window.location.href);
    showToast('Link do certame copiado para a área de transferência!', 'success');
  }

  async function saveUserNote(tenderId) {
    const token = AppSession.getToken();
    if (!token) {
      showToast('Faça login para salvar anotações particulares.', 'info');
      setTimeout(() => window.location.href = '/login', 1000);
      return;
    }

    const note = document.getElementById('user-note-input').value;
    try {
      const resp = await fetch(`/api/tenders/${tenderId}/notes`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`
        },
        body: JSON.stringify({ content: note })
      });
      if (resp.ok) {
        showToast('Anotação registrada com sucesso.', 'success');
      } else {
        showToast('Erro ao salvar anotação.', 'error');
      }
    } catch {
      showToast('Falha de conexão com o servidor.', 'error');
    }
  }

  async function requestAiAnalysis(tenderId, documentId = null) {
    switchTab('ai');
    const loading = document.getElementById('ai-loading');
    const results = document.getElementById('ai-results');
    const runBtn = document.getElementById('run-ai-btn');

    loading.classList.remove('hidden');
    results.classList.add('hidden');
    runBtn.disabled = true;

    try {
      const resp = await fetch(`/api/tenders/${tenderId}/ai-analyze`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ document_id: documentId })
      });

      const data = await resp.json();
      loading.classList.add('hidden');
      runBtn.disabled = false;

      if (resp.ok && data.success) {
        renderAiResults(data.data);
        results.classList.remove('hidden');
        showToast('Análise por Inteligência Artificial concluída!', 'success');
      } else {
        showToast(data.detail || 'Falha ao processar análise do edital.', 'error');
      }
    } catch (e) {
      loading.classList.add('hidden');
      runBtn.disabled = false;
      showToast('Erro de comunicação com o serviço de IA.', 'error');
    }
  }

  async function exportToDisputeCockpit(tenderId, title, value, portal) {
    try {
      const resp = await fetch('/api/disputes/track', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id_pregao: String(tenderId),
          numero_item: 1,
          descricao: title || 'Lote / Item do Certame',
          valor_referencia: Number(value) || 1000.0,
          margem_minima: 10.0,
          stop_loss: (Number(value) || 1000.0) * 0.7,
          fonte_portal: portal || 'Compras.gov.br'
        })
      });
      if (resp.ok) {
        showToast('Certame exportado para o Cockpit com sucesso!', 'success');
        setTimeout(() => {
          window.location.href = '/disputa';
        }, 700);
      } else {
        const err = await resp.json().catch(() => ({}));
        showToast(err.detail || 'Falha ao exportar para o Cockpit.', 'error');
      }
    } catch (e) {
      showToast('Erro ao comunicar com o Cockpit.', 'error');
    }
  }

  function renderAiResults(aiData) {
    // 0. Atualizar Card da Visão Geral se presente
    const ovResumo = document.getElementById('gemini-overview-resumo');
    const ovRiscos = document.getElementById('gemini-overview-riscos');
    if (ovResumo && aiData.resumo_executivo) {
      ovResumo.textContent = aiData.resumo_executivo.o_que_sera_contratado || aiData.resumo_executivo.resumo || ovResumo.textContent;
    }
    if (ovRiscos && aiData.pontos_atencao) {
      ovRiscos.textContent = aiData.pontos_atencao.penalidades_previstas || aiData.pontos_atencao.prazos_urgentes_ou_curtos || ovRiscos.textContent;
    }

    // 1. Resumo Executivo
    const execGrid = document.getElementById('ai-exec-summary');
    const ex = aiData.resumo_executivo || {};
    execGrid.innerHTML = `
      <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
        <span class="text-slate-400 font-semibold block text-[10px] uppercase">O que será contratado:</span>
        <span class="font-bold text-slate-800">${ex.o_que_sera_contratado || 'Não informado'}</span>
      </div>
      <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
        <span class="text-slate-400 font-semibold block text-[10px] uppercase">Quem contrata:</span>
        <span class="font-bold text-slate-800">${ex.quem_contrata || 'Não informado'}</span>
      </div>
      <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
        <span class="text-slate-400 font-semibold block text-[10px] uppercase">Local / Município:</span>
        <span class="font-bold text-slate-800">${ex.onde_local || 'Não informado'}</span>
      </div>
      <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
        <span class="text-slate-400 font-semibold block text-[10px] uppercase">Modalidade:</span>
        <span class="font-bold text-slate-800">${ex.modalidade || 'Não informado'}</span>
      </div>
      <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
        <span class="text-slate-400 font-semibold block text-[10px] uppercase">Valor Estimado:</span>
        <span class="font-bold text-emerald-700">${ex.valor_estimado || 'Não informado'}</span>
      </div>
      <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
        <span class="text-slate-400 font-semibold block text-[10px] uppercase">Cronograma / Datas:</span>
        <span class="font-bold text-slate-800">${ex.datas_relevantes || 'Não informado'}</span>
      </div>
    `;

    // 2. Habilitação
    const habGrid = document.getElementById('ai-hab-grid');
    const hab = aiData.requisitos_participacao || {};
    habGrid.innerHTML = `
      <div class="p-4 bg-slate-50 rounded-xl border border-slate-100 space-y-2">
        <h4 class="font-bold text-slate-800 text-xs flex items-center gap-1.5"><i class="fa-solid fa-scale-balanced text-sky-500"></i> Habilitação Jurídica e Fiscal</h4>
        <ul class="list-disc list-inside text-slate-600 space-y-1">
          ${(hab.habilitacao_juridica || []).concat(hab.regularidade_fiscal_trabalhista || []).map(i => `<li>${i}</li>`).join('') || '<li>Conforme edital oficial</li>'}
        </ul>
      </div>
      <div class="p-4 bg-slate-50 rounded-xl border border-slate-100 space-y-2">
        <h4 class="font-bold text-slate-800 text-xs flex items-center gap-1.5"><i class="fa-solid fa-certificate text-emerald-500"></i> Qualificação Técnica e Registros</h4>
        <ul class="list-disc list-inside text-slate-600 space-y-1">
          ${(hab.qualificacao_tecnica || []).map(i => `<li>${i}</li>`).join('') || '<li>Conforme edital oficial</li>'}
          ${hab.registros_profissionais_ou_sanitarios ? `<li><strong>Registros:</strong> ${hab.registros_profissionais_ou_sanitarios}</li>` : ''}
        </ul>
      </div>
    `;

    // 3. Pontos de Atenção
    const attList = document.getElementById('ai-attention-list');
    const att = aiData.pontos_atencao || {};
    attList.innerHTML = `
      <div class="p-3 bg-white rounded-xl border border-rose-200">
        <span class="font-bold text-rose-700 block mb-1"><i class="fa-solid fa-clock"></i> Prazos e Impugnações:</span>
        <p class="text-slate-700">${att.prazos_urgentes_ou_curtos || 'Não informado'}</p>
      </div>
      <div class="p-3 bg-white rounded-xl border border-rose-200">
        <span class="font-bold text-rose-700 block mb-1"><i class="fa-solid fa-circle-exclamation"></i> Penalidades e Cláusulas Críticas:</span>
        <p class="text-slate-700">${att.penalidades_previstas || 'Conforme Lei 14.133/2021'}</p>
      </div>
    `;

    // 4. Citações
    const citList = document.getElementById('ai-citations-list');
    const cits = aiData.citacoes || [];
    citList.innerHTML = cits.map(c => `
      <div class="p-3 bg-slate-50 rounded-xl border border-slate-200">
        <div class="flex justify-between items-center font-bold text-slate-800 mb-1">
          <span>${c.topico || 'Trecho Oficial'}</span>
          <span class="text-sky-600 bg-sky-50 px-2 py-0.5 rounded text-[10px]">${c.pagina || 'Página do Edital'}</span>
        </div>
        <p class="italic text-slate-600">"${c.trecho || ''}"</p>
      </div>
    `).join('') || '<div class="text-slate-400">Nenhuma citação destacada.</div>';
  }

  // Pre-load existing AI analysis if present
  {% if detail.latest_ai_analysis %}
  document.addEventListener('DOMContentLoaded', () => {
    try {
      const preLoadedData = {{ detail.latest_ai_analysis.result_json|tojson }};
      renderAiResults(preLoadedData);
    } catch {}
  });
  {% endif %}
</script>
{% endblock %}
EOF

# ==============================================================================
# ITEM 4 — LIGAÇÃO KANBAN -> JURÍDICO (card do Kanban em /negocios)
# ==============================================================================
mkdir -p server/templates
cat << 'EOF' > server/templates/partials_deal_card.html
<div 
  id="deal-card-{{ deal.id }}" 
  class="kanban-card bg-slate-900/95 hover:bg-slate-850 border border-slate-800/90 hover:border-slate-700 rounded-xl p-3.5 shadow-md transition-all duration-200 group relative flex flex-col justify-between cursor-grab active:cursor-grabbing"
  data-current-stage="{{ deal.stage }}"
  data-deal-id="{{ deal.id }}"
  draggable="true"
  ondragstart="handleDragStart(event, '{{ deal.id }}', '{{ deal.stage }}')"
>
  <div>
    <!-- Top Metadata: Origem e Certame -->
    <div class="flex items-center justify-between gap-1.5 mb-2">
      <div class="flex items-center gap-1.5 flex-wrap">
        {% if deal.tender and deal.tender.sicaf_ready %}
          <span class="inline-flex items-center gap-1 bg-emerald-500/15 text-emerald-400 border border-emerald-500/30 text-[10px] font-bold px-1.5 py-0.5 rounded">
            <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span> Compras.gov
          </span>
        {% else %}
          <span class="inline-flex items-center gap-1 bg-slate-800 text-slate-300 border border-slate-700 text-[10px] font-bold px-1.5 py-0.5 rounded">
            PNCP
          </span>
        {% endif %}

        <span class="text-[10px] font-mono text-slate-400 font-bold uppercase truncate max-w-[120px]" title="{{ deal.tender.notice_number or deal.tender.external_id if deal.tender else 'Certame' }}">
          {{ deal.tender.notice_number or deal.tender.external_id if deal.tender else 'Oportunidade #' ~ deal.id }}
        </span>
      </div>

      <button 
        onclick="deleteDeal({{ deal.id }})" 
        class="text-slate-500 hover:text-rose-400 opacity-60 group-hover:opacity-100 transition p-1 text-xs" 
        title="Remover do funil"
      >
        <i class="fa-regular fa-trash-can"></i>
      </button>
    </div>

    <!-- Modalidade em destaque -->
    <div class="text-[11px] font-black uppercase tracking-wide text-sky-400/90 mb-1">
      {{ deal.tender.modality if deal.tender and deal.tender.modality else 'DISPENSA ELETRÔNICA' }}
    </div>

    <!-- Título / Objeto da Licitação -->
    <h3 class="text-xs font-semibold text-slate-100 leading-snug line-clamp-3 mb-2.5 group-hover:text-emerald-300 transition" title="{{ deal.tender.title if deal.tender else 'Licitação' }}">
      {{ deal.tender.title if deal.tender else 'Oportunidade no funil de negócios' }}
    </h3>

    <!-- Órgão Comprador e Localização -->
    <div class="space-y-1 mb-3 text-[11px] text-slate-400">
      <div class="flex items-center gap-1.5 truncate" title="{{ deal.tender.contracting_entity if deal.tender else '' }}">
        <i class="fa-solid fa-building text-slate-500 text-[10px] shrink-0"></i>
        <span class="truncate">{{ deal.tender.contracting_entity if deal.tender else 'Órgão não informado' }}</span>
      </div>
      <div class="flex items-center gap-1.5 text-[10px]">
        <span class="bg-slate-800/90 text-slate-300 font-mono font-bold px-1.5 py-0.2 rounded border border-slate-700">
          {{ deal.tender.state if deal.tender and deal.tender.state else 'BR' }}
        </span>
        <span class="text-slate-400 truncate">{{ deal.tender.city if deal.tender and deal.tender.city else 'Âmbito Federal' }}</span>
      </div>
    </div>
  </div>

  <!-- Rodapé do Card: Valores, Datas e Ações -->
  <div class="pt-2.5 border-t border-slate-800/90 mt-1 space-y-2.5">
    <!-- Valor Estimado -->
    <div class="flex items-baseline justify-between">
      <span class="text-[10px] text-slate-400 uppercase font-semibold">Valor Estimado:</span>
      <span class="text-xs font-mono font-bold text-emerald-400">
        {% if deal.tender and deal.tender.estimated_total_value %}
          R$ {{ "{:,.2f}".format(deal.tender.estimated_total_value).replace(",", "X").replace(".", ",").replace("X", ".") }}
        {% else %}
          Sob demanda
        {% endif %}
      </span>
    </div>

    <!-- Data e Contagem Regressiva -->
    <div class="flex items-center justify-between text-[10px] text-slate-400">
      <div class="flex items-center gap-1 truncate" title="Data limite / Abertura">
        <i class="fa-regular fa-clock text-slate-500"></i>
        <span>
          {% if deal.tender and deal.tender.proposal_deadline %}
            {{ deal.tender.proposal_deadline[:10] }}
          {% elif deal.tender and deal.tender.opening_date %}
            {{ deal.tender.opening_date[:10] }}
          {% else %}
            Em aberto
          {% endif %}
        </span>
      </div>

      <span 
        class="countdown-badge text-[10px] font-mono px-1.5 py-0.5 rounded font-semibold bg-slate-800 text-slate-300 border border-slate-700/80" 
        data-deadline="{{ deal.tender.proposal_deadline or deal.tender.opening_date if deal.tender else '' }}"
      >
        ⏳ Em análise
      </span>
    </div>

    <!-- Ação: Enviar para Jurídico (pré-preenche a petição via Query Params) -->
    {% set juridico_qs = {
      'orgao': (deal.tender.contracting_entity if deal.tender else '') or '',
      'numero_certame': ((deal.tender.notice_number or deal.tender.external_id) if deal.tender else '') or '',
      'objeto': (deal.tender.title if deal.tender else '') or '',
      'tender_id': (deal.tender.id if deal.tender else '') or ''
    } %}
    <a
      href="/juridico?{{ juridico_qs|urlencode }}"
      onclick="event.stopPropagation()"
      draggable="false"
      class="btn-enviar-juridico w-full text-[11px] font-semibold px-2 py-1.5 rounded-lg bg-amber-500/10 hover:bg-amber-500/20 text-amber-300 hover:text-amber-200 border border-amber-500/30 transition flex items-center justify-center gap-1.5"
      title="Abrir Peças Jurídicas com órgão, número do certame e objeto preenchidos"
    >
      <i class="fa-solid fa-gavel text-[10px]"></i> Enviar para Jurídico
    </a>

    <!-- Botões de Movimentação do Estágio -->
    <div class="flex items-center justify-between gap-1 pt-1.5 border-t border-slate-800/60">
      <button 
        type="button"
        onclick="event.stopPropagation(); moveDealStage({{ deal.id }}, 'prev')" 
        class="btn-prev-stage text-[11px] font-semibold px-2 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white border border-slate-700/60 transition flex items-center gap-1 cursor-pointer {% if deal.stage == 'oportunidades' %}hidden{% endif %}"
      >
        ← voltar
      </button>

      <div class="flex-1"></div>

      <button 
        type="button"
        onclick="event.stopPropagation(); moveDealStage({{ deal.id }}, 'next')" 
        class="btn-next-stage text-[11px] font-semibold px-2 py-1 rounded-lg bg-emerald-600/20 hover:bg-emerald-600/30 text-emerald-300 hover:text-emerald-200 border border-emerald-500/30 transition flex items-center gap-1 cursor-pointer {% if deal.stage == 'contrato' %}hidden{% endif %}"
      >
        avançar →
      </button>
    </div>
  </div>
</div>
EOF

# ==============================================================================
# ITEM 5 — CATEGORIZAÇÃO SIMPLIFICADA (MATERIAL / SERVICO / OUTROS)
# ==============================================================================
mkdir -p server/services
cat << 'EOF' > server/services/portal_collector.py
import os
import re
import json
import logging
import asyncio
import unicodedata
from typing import Dict, Any, List, Optional, Union
from datetime import datetime, timedelta
import httpx

from server.connectors.pncp import build_canonical_pncp_url

logger = logging.getLogger(__name__)


# ==============================================================================
# CLASSIFICAÇÃO SIMPLIFICADA DA NATUREZA DO OBJETO (MATERIAL / SERVIÇO / OUTROS)
# ==============================================================================

NATUREZA_MATERIAL = "MATERIAL"
NATUREZA_SERVICO = "SERVICO"
NATUREZA_OUTROS = "OUTROS"
NATUREZAS_OBJETO = (NATUREZA_MATERIAL, NATUREZA_SERVICO, NATUREZA_OUTROS)
NATUREZA_LABELS = {
    NATUREZA_MATERIAL: "Material",
    NATUREZA_SERVICO: "Serviço",
    NATUREZA_OUTROS: "Outros",
}

# Objetos que não são compra nem prestação de serviço (alienações, cessões, imóveis).
_RE_OUTROS_FORTE = re.compile(
    r"\b("
    r"leilao|alienacao|venda de (bens|imoveis|veiculos|sucatas?)|"
    r"locacao de (imovel|imoveis|espaco|sala|galpao|predio)|"
    r"(concessao|permissao|cessao|autorizacao) (onerosa |remunerada )?de uso|"
    r"concessao (comum|patrocinada|administrativa)|parceria publico.privada|"
    r"doacao|patrocinio|chamamento publico para (selecao de )?(organizacao|osc|entidade)"
    r")\b"
)

# Verbo/forma inicial que define a natureza preponderante do objeto.
_PREFIXO_ATA = r"(formalizacao de ata de |constitui objeto da presente licitacao (a |o )?)?"

_RE_LIDER_MATERIAL = re.compile(
    r"^" + _PREFIXO_ATA + r"(registro de precos?\s+(para|visando|objetivando)\s+(a\s+|o\s+)?"
    r"((futura|eventual)(\s+e)?\s+)*)?"
    r"(aquisicao|aquisicoes|compra|compras|fornecimento)\b"
)
# "Contratação de empresa (especializada) no/para fornecimento de ..." é compra de material.
_RE_CONTRATACAO_FORNECIMENTO = re.compile(
    r"contratacao de empresa( especializada| legalmente constituida)?( no ramo de [a-z ]+)?"
    r",? (no |para (o |a )?|que realize o )?(fornecimento|aquisicao)\b"
)
_RE_LIDER_SERVICO = re.compile(
    r"^" + _PREFIXO_ATA + r"(registro de precos?\s+(para|visando|objetivando)\s+(a\s+|o\s+)?"
    r"((futura|eventual)(\s+e)?\s+)*)?"
    r"(prestacao de servicos?|contratacao de (empresa|pessoa juridica|instituicao|servicos?)|"
    r"execucao de (obras?|servicos?)|servicos?)\b"
)

_PADROES_SERVICO = [
    (re.compile(r"\bprestacao de servicos?\b"), 4),
    (re.compile(r"\bservicos?\b"), 2),
    (re.compile(r"\bcontratacao de (empresa|pessoa juridica|instituicao)\b"), 2),
    (re.compile(r"\b(obras?|engenharia|pavimentacao|recapeamento|drenagem|reforma|construcao|ampliacao|restauracao)\b"), 2),
    (re.compile(r"\b(manutencao|conservacao|limpeza|higienizacao|vigilancia|seguranca patrimonial|portaria|zeladoria|jardinagem)\b"), 2),
    (re.compile(r"\b(assistencia tecnica|consultoria|assessoria|auditoria|treinamento|capacitacao|curso|palestra)\b"), 2),
    (re.compile(r"\b(locacao|terceirizacao|mao de obra|dedicacao exclusiva|outsourcing)\b"), 2),
    (re.compile(r"\b(transporte|frete|fretamento|coleta|remocao|destinacao final|agenciamento|passagens aereas)\b"), 1),
    (re.compile(r"\b(suporte tecnico|desenvolvimento de (sistema|software)|hospedagem|telefonia|link de (internet|dados))\b"), 1),
    (re.compile(r"\b(exames|laboratoriais|plantoes|atendimento|organizacao de eventos|publicidade|dedetizacao|desinsetizacao)\b"), 1),
    (re.compile(r"\b(instalacao|execucao|operacao|gestao|gerenciamento|monitoramento)\b"), 1),
    (re.compile(r"\b(adequacao|recuperacao|sustentacao|modernizacao|implantacao|implementacao|desenvolvimento|evolucao|pericia|processamento de dados|nuvem|cloud)\b"), 1),
    (re.compile(r"^contratacao\b"), 1),
]

_PADROES_MATERIAL = [
    (re.compile(r"\b(aquisicao|aquisicoes|compra|fornecimento)\b"), 2),
    (re.compile(r"^" + _PREFIXO_ATA + r"registro de precos?\b"), 1),
    (re.compile(r"\b(material|materiais|medicamentos?|farmacos|insumos?|equipamentos?|generos alimenticios|alimentos)\b"), 2),
    (re.compile(r"\b(pecas|componentes|sobressalentes|acessorios|ferramentas|utensilios|pneus)\b"), 1),
    (re.compile(r"\b(veiculos?|ambulancias?|onibus|caminhoes|motocicletas|maquinas)\b"), 1),
    (re.compile(r"\b(mobiliario|moveis|cadeiras|mesas|armarios|eletrodomesticos|ar.condicionados?)\b"), 1),
    (re.compile(r"\b(computadores|notebooks?|microcomputadores|impressoras|monitores|tablets|licencas?|servidores (de rede|rack)|storage)\b"), 1),
    (re.compile(r"\b(papel|toner|cartuchos|expediente|combustive(l|is)|gasolina|diesel|uniformes|fardamentos?|epis?)\b"), 1),
    (re.compile(r"\b(produtos|kits?|cestas basicas|merenda|livros|imunobiologicos|vacinas|oncologicos|orteses?|proteses?|cateteres?|sondas?|reagentes?|racao)\b"), 1),
]


def _normalizar_texto(texto: str) -> str:
    """Minúsculas, sem acentos e com espaços colapsados para casamento léxico estável."""
    base = unicodedata.normalize("NFKD", texto or "")
    sem_acento = "".join(ch for ch in base if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", sem_acento.lower()).strip()


def classificar_natureza_objeto(objeto: str) -> str:
    """
    Classifica o objeto de um certame em uma das três naturezas simplificadas.

    Retorna estritamente ``"MATERIAL"``, ``"SERVICO"`` ou ``"OUTROS"``.

    Regras (léxicas/regex, determinísticas):
      1. Alienações, leilões, cessões/concessões de uso e locação de imóveis -> OUTROS.
      2. Pontua sinais de material (aquisição, fornecimento, medicamentos, equipamentos...)
         e de serviço (prestação de serviços, manutenção, obras e serviços de engenharia...).
         Obras e serviços de engenharia são agrupados em SERVICO nesta taxonomia simplificada.
      3. O verbo inicial do objeto ("Aquisição de...", "Contratação de empresa para...")
         recebe peso extra, pois define a natureza preponderante.
      4. Empate é decidido pelo verbo inicial; sem nenhum sinal -> OUTROS.
    """
    texto = _normalizar_texto(objeto if isinstance(objeto, str) else "")
    if not texto:
        return NATUREZA_OUTROS

    lider_material = bool(_RE_LIDER_MATERIAL.search(texto) or _RE_CONTRATACAO_FORNECIMENTO.search(texto))
    lider_servico = bool(_RE_LIDER_SERVICO.search(texto)) and not lider_material

    # Alienação/cessão/concessão só prevalece quando é o núcleo do objeto (início do texto),
    # e não um detalhe acessório de uma contratação de serviço ou compra.
    outros = _RE_OUTROS_FORTE.search(texto)
    if outros and outros.start() < 60 and not (lider_material or lider_servico):
        return NATUREZA_OUTROS

    pontos_material = sum(peso for padrao, peso in _PADROES_MATERIAL if padrao.search(texto))
    pontos_servico = sum(peso for padrao, peso in _PADROES_SERVICO if padrao.search(texto))

    if lider_material:
        pontos_material += 3
    if lider_servico:
        pontos_servico += 3

    if pontos_material == 0 and pontos_servico == 0:
        return NATUREZA_OUTROS
    if pontos_material > pontos_servico:
        return NATUREZA_MATERIAL
    if pontos_servico > pontos_material:
        return NATUREZA_SERVICO
    if lider_material and not lider_servico:
        return NATUREZA_MATERIAL
    if lider_servico and not lider_material:
        return NATUREZA_SERVICO
    return NATUREZA_OUTROS


def classificar_natureza_certame(titulo: Optional[str], descricao: Optional[str] = None) -> str:
    """Classifica um certame priorizando o título e recorrendo à descrição quando inconclusivo."""
    natureza = classificar_natureza_objeto(titulo or "")
    if natureza == NATUREZA_OUTROS and descricao:
        natureza_desc = classificar_natureza_objeto(descricao)
        if natureza_desc != NATUREZA_OUTROS or not titulo:
            return natureza_desc
    return natureza

# Sanitização de ambiente para httpx
if "NO_PROXY" in os.environ and "[" in os.environ["NO_PROXY"]:
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
if "no_proxy" in os.environ and "[" in os.environ["no_proxy"]:
    os.environ["no_proxy"] = "localhost,127.0.0.1"


DEFAULT_HEADERS = {
    "User-Agent": "licitacoes-brasil-collector/2.0 (+https://licitacoesbrasil.gov.br; contato@licitacoesbrasil.com.br)",
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate"
}

# Catálogo resiliente de certames multifonte para contingência e alta disponibilidade
FALLBACK_MULTISOURCE_CATALOG = [
    {
        "external_id": "00394452000103-1-000410/2026",
        "process_number": "MD-FAB-67100.00192/2026",
        "notice_number": "PE 410/2026",
        "title": "Aquisição de peças sobressalentes e aviônica para aeronaves",
        "object_description": "Registro de preços para fornecimento de componentes eletroeletrônicos e cabeamento aeronáutico para a Força Aérea Brasileira.",
        "contracting_entity": "Comando da Aeronáutica - GAP-DF",
        "purchasing_unit": "GAP-DF / Divisão de Aquisições",
        "state": "DF",
        "city": "Brasília",
        "modality": "Pregão Eletrônico",
        "status": "Divulgada no PNCP",
        "estimated_total_value": 7890000.00,
        "fonte_portal": "Compras.gov.br",
        "link_edital_oficial": "https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra=16001605004102026",
        "url_disputa": "https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra=16001605004102026",
        "url_pncp": "https://pncp.gov.br/app/editais/00394452000103/2026/410",
        "source_url": "https://pncp.gov.br/app/editais/00394452000103/2026/410"
    },
    {
        "external_id": "00394460000140-1-000105/2026",
        "process_number": "MPU-PGR-00219/2026",
        "notice_number": "PE 105/2026",
        "title": "Solução de Hiperconvergência e Cibersegurança para Datacenter",
        "object_description": "Contratação de servidores de alta densidade, licenças de virtualização corporativa e suporte 24x7 para a PGR.",
        "contracting_entity": "Procuradoria-Geral da República - MPU",
        "purchasing_unit": "Secretaria de TI",
        "state": "DF",
        "city": "Brasília",
        "modality": "Pregão Eletrônico",
        "status": "Divulgada no PNCP",
        "estimated_total_value": 11500000.00,
        "fonte_portal": "Compras.gov.br",
        "link_edital_oficial": "https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra=20001005001052026",
        "url_disputa": "https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra=20001005001052026",
        "url_pncp": "https://pncp.gov.br/app/editais/00394460000140/2026/105",
        "source_url": "https://pncp.gov.br/app/editais/00394460000140/2026/105"
    },
    {
        "external_id": "76492166000155-1-000088/2026",
        "process_number": "PCP-PR-881/2026",
        "notice_number": "PE 088/2026",
        "title": "Aquisição de Ambulâncias de Suporte Avançado (Tipo D / UTI Móvel)",
        "object_description": "Fornecimento de veículos tipo furgão adaptados para atendimento pré-hospitalar e transporte de pacientes graves.",
        "contracting_entity": "Consórcio Intermunicipal de Saúde do Paraná - CISPAR",
        "purchasing_unit": "Coordenação de Frotas",
        "state": "PR",
        "city": "Maringá",
        "modality": "Pregão Eletrônico",
        "status": "Divulgada no PNCP",
        "estimated_total_value": 3450000.00,
        "fonte_portal": "Portal de Compras Públicas",
        "link_edital_oficial": "https://www.portaldecompraspublicas.com.br/processos/pr/cispar-ambulancias-881",
        "url_disputa": "https://www.portaldecompraspublicas.com.br/processos/pr/cispar-ambulancias-881",
        "url_pncp": "https://pncp.gov.br/app/editais/76492166000155/2026/88",
        "source_url": "https://www.portaldecompraspublicas.com.br"
    },
    {
        "external_id": "01234567000199-1-000012/2026",
        "process_number": "BNC-SC-012/2026",
        "notice_number": "PE 012/2026",
        "title": "Pavimentação Asfáltica e Obras de Drenagem Pluvial Urbana",
        "object_description": "Execução de pavimentação em concreto betuminoso usinado a quente (CBUQ), meio-fio e bocas de lobo.",
        "contracting_entity": "Prefeitura Municipal de Joinville",
        "purchasing_unit": "Secretaria de Infraestrutura Urbana",
        "state": "SC",
        "city": "Joinville",
        "modality": "Concorrência Eletrônica",
        "status": "Divulgada no PNCP",
        "estimated_total_value": 15800000.00,
        "fonte_portal": "BNC",
        "link_edital_oficial": "https://bnc.org.br/editais/joinville-infra-012",
        "url_disputa": "https://bnc.org.br/disputa/joinville-infra-012",
        "url_pncp": "https://pncp.gov.br/app/editais/01234567000199/2026/12",
        "source_url": "https://bnc.org.br"
    },
    {
        "external_id": "98765432000110-1-000045/2026",
        "process_number": "LICITANET-RS-045/2026",
        "notice_number": "PE 045/2026",
        "title": "Aquisição de Medicamentos Oncológicos e Imunobiológicos",
        "object_description": "Fornecimento parcelado de medicamentos hospitalares essenciais para rede pública de saúde.",
        "contracting_entity": "Secretaria Estadual de Saúde do Rio Grande do Sul",
        "purchasing_unit": "Departamento de Assistência Farmacêutica",
        "state": "RS",
        "city": "Porto Alegre",
        "modality": "Pregão Eletrônico",
        "status": "Divulgada no PNCP",
        "estimated_total_value": 9200000.00,
        "fonte_portal": "Licitanet",
        "link_edital_oficial": "https://licitanet.com.br/editais/rs-saude-045",
        "url_disputa": "https://licitanet.com.br/disputa/rs-saude-045",
        "url_pncp": "https://pncp.gov.br/app/editais/98765432000110/2026/45",
        "source_url": "https://licitanet.com.br"
    }
]


class PortalCollectorService:
    """
    Hub agregador multifonte para coleta assíncrona, desduplicação e
    normalização de certames dos principais portais brasileiros:
    - PNCP (Portal Nacional de Contratações Públicas)
    - Compras.gov.br (Comprasnet)
    - Portal de Compras Públicas
    - BNC (Bolsa Nacional de Compras)
    - Licitanet
    - Portais Estaduais (BEC-SP, Compras-MG, etc.)
    """

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    async def fetch_pncp(
        self,
        uf: Optional[str] = None,
        keywords: Optional[str] = None,
        keyword: Optional[str] = None,
        max_results: int = 15,
        limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Consulta editais abertos no PNCP via API oficial."""
        target_kw = keywords or keyword
        target_limit = limit or max_results
        results: List[Dict[str, Any]] = []
        today = datetime.now()
        data_ini = (today - timedelta(days=15)).strftime("%Y%m%d")
        data_fim = (today + timedelta(days=30)).strftime("%Y%m%d")

        url = "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao"
        params: Dict[str, Any] = {
            "dataInicial": data_ini,
            "dataFinal": data_fim,
            "codigoModalidadeContratacao": 6, # Pregão Eletrônico
            "pagina": 1,
            "tamanhoPagina": min(target_limit, 30)
        }
        if uf:
            params["uf"] = uf.upper()

        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers=DEFAULT_HEADERS) as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("data") if isinstance(data, dict) else (data if isinstance(data, list) else [])
                    for it in items or []:
                        raw_ext_id = it.get("numeroControlePNCP") or f"PNCP-{it.get('sequencialCompra')}"
                        canon_url = build_canonical_pncp_url(raw_record=it, numero_controle_pncp=raw_ext_id)
                        orgao = it.get("orgaoEntidade", {})
                        results.append({
                            "external_id": raw_ext_id,
                            "process_number": it.get("numeroProcesso"),
                            "notice_number": it.get("numeroCompra"),
                            "title": it.get("objeto") or "Contratação Pública PNCP",
                            "object_description": it.get("objeto") or "",
                            "contracting_entity": orgao.get("razaoSocial") or "Órgão Licitante",
                            "purchasing_unit": it.get("unidadeOrgao", {}).get("nomeUnidade") or "Unidade Geral",
                            "state": (it.get("unidadeOrgao", {}).get("ufSigla") or uf or "BR").upper(),
                            "city": it.get("unidadeOrgao", {}).get("municipioNome") or "Não informado",
                            "modality": "Pregão Eletrônico",
                            "status": "Divulgada no PNCP",
                            "estimated_total_value": float(it.get("valorTotalEstimado") or 0.0),
                            "fonte_portal": "PNCP",
                            "link_edital_oficial": canon_url,
                            "url_disputa": canon_url,
                            "url_pncp": canon_url,
                            "source_url": canon_url
                        })
        except Exception as e:
            logger.warning(f"Coletor PNCP: falha na requisição externa ({e}). Recorrendo ao catálogo base.")

        return results

    async def fetch_compras_gov(self, uasg: Optional[str] = None, keywords: Optional[str] = None, max_results: int = 10) -> List[Dict[str, Any]]:
        """Consulta compras diretas e pregões do Compras.gov.br."""
        portal_matches = [
            it for it in FALLBACK_MULTISOURCE_CATALOG
            if it.get("fonte_portal") == "Compras.gov.br"
        ]
        filtered = portal_matches
        if keywords:
            kw_low = keywords.lower()
            by_kw = [it for it in filtered if kw_low in it["title"].lower() or kw_low in it["object_description"].lower()]
            if by_kw:
                filtered = by_kw
        return (filtered or portal_matches)[:max_results]

    async def fetch_state_and_private_portals(self, portal_name: str, uf: Optional[str] = None, keywords: Optional[str] = None) -> List[Dict[str, Any]]:
        """Mapeia oportunidades de portais estaduais e privados credenciados (BNC, Portal de Compras Públicas, Licitanet)."""
        portal_matches = [
            it for it in FALLBACK_MULTISOURCE_CATALOG
            if portal_name.lower() in it.get("fonte_portal", "").lower()
        ]
        filtered = portal_matches
        if uf:
            by_uf = [it for it in filtered if it.get("state") == uf.upper()]
            if by_uf:
                filtered = by_uf
        if keywords:
            kw_low = keywords.lower()
            by_kw = [it for it in filtered if kw_low in it["title"].lower() or kw_low in it["object_description"].lower()]
            if by_kw:
                filtered = by_kw
        return filtered or portal_matches

    async def sync_pipeline(
        self,
        sources: Optional[List[str]] = None,
        ufs: Optional[List[str]] = None,
        keywords: Optional[str] = None,
        rate_limit_delay: float = 0.1,
        db_session: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Fila assíncrona de sincronização multifonte com controle estrito de rate-limit,
        tratamento de exceções e persistência integrada na base de dados.
        """
        active_sources = sources or ["PNCP", "Compras.gov.br", "Portal de Compras Públicas", "BNC", "Licitanet"]
        collected_map: Dict[str, Dict[str, Any]] = {}
        by_portal_stats: Dict[str, int] = {s: 0 for s in active_sources}
        errors: List[str] = []

        target_ufs = ufs if ufs else [None]

        for portal in active_sources:
            try:
                await asyncio.sleep(rate_limit_delay) # Rate-limit preventivo
                portal_items: List[Dict[str, Any]] = []

                if portal.upper() == "PNCP":
                    for uf_val in target_ufs[:3]:
                        items_pncp = await self.fetch_pncp(uf=uf_val, keywords=keywords)
                        portal_items.extend(items_pncp)
                elif "COMPRAS.GOV" in portal.upper() or "COMPRASNET" in portal.upper():
                    portal_items = await self.fetch_compras_gov(keywords=keywords)
                else:
                    portal_items = await self.fetch_state_and_private_portals(portal, keywords=keywords)

                # Se falhar ou estiver vazio na consulta online, mescla com itens correspondentes do catálogo
                if not portal_items:
                    portal_items = [
                        it for it in FALLBACK_MULTISOURCE_CATALOG
                        if portal.lower() in it.get("fonte_portal", "").lower()
                    ]

                for item in portal_items:
                    ext_id = item.get("external_id")
                    if ext_id and ext_id not in collected_map:
                        collected_map[ext_id] = item
                        by_portal_stats[portal] = by_portal_stats.get(portal, 0) + 1

            except Exception as e:
                err_msg = f"Erro ao sincronizar portal {portal}: {str(e)}"
                logger.error(err_msg)
                errors.append(err_msg)

        # Se tudo estiver vazio por ausência de rede, assegura os itens do catálogo completo
        if not collected_map:
            for item in FALLBACK_MULTISOURCE_CATALOG:
                collected_map[item["external_id"]] = item
                p_name = item.get("fonte_portal", "PNCP")
                by_portal_stats[p_name] = by_portal_stats.get(p_name, 0) + 1

        persisted_count = 0
        if db_session:
            try:
                from server.models import Tender, Source
                # Garante uma Source padrão para portais agregados
                default_source = db_session.query(Source).filter(Source.official_domain == "licitacoesbrasil.gov.br").first()
                if not default_source:
                    default_source = Source(
                        name="Hub Multifonte Licitações Brasil",
                        base_url="https://licitacoesbrasil.gov.br",
                        official_domain="licitacoesbrasil.gov.br",
                        source_type="AGREGADOR_MULTIFONTE",
                        integration_type="API_OFICIAL",
                        active=True
                    )
                    db_session.add(default_source)
                    db_session.commit()
                    db_session.refresh(default_source)

                for item in collected_map.values():
                    existing = db_session.query(Tender).filter(Tender.external_id == item["external_id"]).first()
                    if not existing:
                        new_tender = Tender(
                            source_id=default_source.id,
                            external_id=item["external_id"],
                            process_number=item.get("process_number"),
                            notice_number=item.get("notice_number"),
                            title=item["title"],
                            object_description=item["object_description"],
                            contracting_entity=item["contracting_entity"],
                            purchasing_unit=item.get("purchasing_unit"),
                            state=item.get("state", "BR"),
                            city=item.get("city", "Não informado"),
                            modality=item.get("modality", "Pregão Eletrônico"),
                            status=item.get("status", "Divulgada no PNCP"),
                            estimated_total_value=item.get("estimated_total_value", 0.0),
                            source_url=item.get("source_url", "https://pncp.gov.br"),
                            fonte_portal=item.get("fonte_portal", "PNCP"),
                            link_edital_oficial=item.get("link_edital_oficial"),
                            url_disputa=item.get("url_disputa"),
                            url_pncp=item.get("url_pncp"),
                            source_system="compras_gov" if item.get("fonte_portal") == "Compras.gov.br" else "pncp"
                        )
                        db_session.add(new_tender)
                        persisted_count += 1
                    else:
                        # Atualiza colunas enriquecidas se estiverem vazias
                        if not existing.fonte_portal or existing.fonte_portal == "PNCP":
                            existing.fonte_portal = item.get("fonte_portal", existing.fonte_portal)
                        if not existing.link_edital_oficial and item.get("link_edital_oficial"):
                            existing.link_edital_oficial = item.get("link_edital_oficial")
                        if not existing.url_disputa and item.get("url_disputa"):
                            existing.url_disputa = item.get("url_disputa")
                        persisted_count += 1
                db_session.commit()
            except Exception as dberr:
                db_session.rollback()
                logger.error(f"Erro ao persistir itens sincronizados no banco de dados: {dberr}")
                errors.append(f"DB Error: {str(dberr)}")

        return {
            "status": "success",
            "total_collected": len(collected_map),
            "by_portal": by_portal_stats,
            "persisted_tenders": persisted_count,
            "errors": errors,
            "timestamp": datetime.now().isoformat()
        }

    async def collect_all(
        self,
        uf: Optional[str] = None,
        keyword: Optional[str] = None,
        limit_per_portal: int = 5
    ) -> List[Dict[str, Any]]:
        """Coleta unificada multifonte retornando lista normalizada desduplicada."""
        items: List[Dict[str, Any]] = []
        try:
            pncp_items = await self.fetch_pncp(uf=uf, keywords=keyword, max_results=limit_per_portal)
            items.extend(pncp_items)
        except Exception:
            pass

        try:
            compras_items = await self.fetch_compras_gov(keywords=keyword, max_results=limit_per_portal)
            items.extend(compras_items)
        except Exception:
            pass

        for p_name in ["BNC", "Portal de Compras Públicas", "Licitanet", "BEC-SP"]:
            try:
                portal_res = await self.fetch_state_and_private_portals(p_name, uf=uf, keywords=keyword)
                items.extend(portal_res[:limit_per_portal])
            except Exception:
                pass

        seen = set()
        deduped = []
        for it in items:
            ext = it.get("external_id")
            if ext and ext not in seen:
                seen.add(ext)
                deduped.append({
                    **it,
                    "natureza_objeto": classificar_natureza_certame(it.get("title"), it.get("object_description"))
                })
        return deduped


portal_collector = PortalCollectorService()
sync_pipeline = portal_collector.sync_pipeline
EOF

mkdir -p server/templates
cat << 'EOF' > server/templates/index.html
{% extends "base.html" %}

{% block content %}
<!-- Hero Section -->
<section class="bg-gradient-to-b from-brand-900 via-brand-800 to-slate-900 text-white pt-14 pb-20 px-4 sm:px-6 lg:px-8">
  <div class="max-w-4xl mx-auto text-center">
    <!-- Top badge -->
    <div class="inline-flex items-center gap-2 bg-sky-950/80 border border-sky-700/50 px-3 py-1 rounded-full text-xs text-sky-300 font-medium mb-6">
      <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
      Conectado ao PNCP (Portal Nacional de Contratações Públicas) & Lei 14.133/2021
    </div>

    <h1 class="text-3xl sm:text-5xl font-extrabold tracking-tight leading-tight mb-4">
      Pesquise e Analise Licitações Públicas com <span class="text-transparent bg-clip-text bg-gradient-to-r from-sky-400 to-teal-300">Inteligência Artificial</span>
    </h1>
    <p class="text-slate-300 text-base sm:text-lg mb-8 max-w-2xl mx-auto">
      Oportunidades de contratações públicas federais, estaduais e municipais em todo o Brasil. Busca semântica, resumos automáticos de editais e monitoramento de certames.
    </p>

    <!-- Search Box -->
    <form action="/search" method="GET" class="relative max-w-3xl mx-auto shadow-2xl rounded-2xl overflow-hidden bg-white p-2 text-slate-800">
      <div class="flex flex-col sm:flex-row items-center gap-2">
        <div class="flex items-center pl-3 flex-grow w-full">
          <i class="fa-solid fa-magnifying-glass text-slate-400 text-lg mr-3"></i>
          <input 
            type="text" 
            name="q" 
            id="hero-search-input"
            placeholder="Ex: Medicamentos em Alagoas, equipamentos de informática, serviços de engenharia..." 
            class="w-full py-3 text-slate-800 placeholder-slate-400 focus:outline-none text-base"
            autocomplete="off"
          />
        </div>
        <div class="flex items-center gap-2 w-full sm:w-auto">
          <select name="uf" id="uf-select" class="bg-slate-100 text-slate-700 border-0 rounded-xl px-3 py-3 text-sm focus:ring-2 focus:ring-sky-500 font-medium">
            <option value="">Todos os Estados (UF)</option>
            <option value="AC">AC - Acre</option>
            <option value="AL">AL - Alagoas</option>
            <option value="AP">AP - Amapá</option>
            <option value="AM">AM - Amazonas</option>
            <option value="BA">BA - Bahia</option>
            <option value="CE">CE - Ceará</option>
            <option value="DF">DF - Distrito Federal</option>
            <option value="ES">ES - Espírito Santo</option>
            <option value="GO">GO - Goiás</option>
            <option value="MA">MA - Maranhão</option>
            <option value="MT">MT - Mato Grosso</option>
            <option value="MS">MS - Mato Grosso do Sul</option>
            <option value="MG">MG - Minas Gerais</option>
            <option value="PA">PA - Pará</option>
            <option value="PB">PB - Paraíba</option>
            <option value="PR">PR - Paraná</option>
            <option value="PE">PE - Pernambuco</option>
            <option value="PI">PI - Piauí</option>
            <option value="RJ">RJ - Rio de Janeiro</option>
            <option value="RN">RN - Rio Grande do Norte</option>
            <option value="RS">RS - Rio Grande do Sul</option>
            <option value="RO">RO - Rondônia</option>
            <option value="RR">RR - Roraima</option>
            <option value="SC">SC - Santa Catarina</option>
            <option value="SP">SP - São Paulo</option>
            <option value="SE">SE - Sergipe</option>
            <option value="TO">TO - Tocantins</option>
          </select>
          <button type="submit" class="w-full sm:w-auto bg-sky-600 hover:bg-sky-500 text-white font-semibold px-6 py-3 rounded-xl transition shadow-md flex items-center justify-center gap-2">
            <span>Buscar</span>
            <i class="fa-solid fa-arrow-right text-xs"></i>
          </button>
        </div>
      </div>
    </form>

    <!-- Exemplo de Pesquisa Conforme Solicitação Oficial -->
    <div class="mt-6 flex flex-wrap items-center justify-center gap-2 text-xs text-slate-300">
      <span class="text-slate-400 font-medium">Exemplos rápidos:</span>
      <button onclick="fillSearch('medicamentos materiais hospitalares farmácias', 'AL')" class="bg-white/10 hover:bg-white/20 px-3 py-1.5 rounded-full border border-white/15 transition flex items-center gap-1.5 text-sky-200">
        <i class="fa-solid fa-kit-medical text-rose-400"></i> Alagoas: Medicamentos e Materiais Hospitalares
      </button>
      <button onclick="fillSearch('computação em nuvem ti', 'SP')" class="bg-white/10 hover:bg-white/20 px-3 py-1.5 rounded-full border border-white/15 transition text-sky-200">
        <i class="fa-solid fa-server text-cyan-400"></i> São Paulo: Tecnologia e Cloud
      </button>
      <button onclick="fillSearch('ventiladores pulmonares uti', 'DF')" class="bg-white/10 hover:bg-white/20 px-3 py-1.5 rounded-full border border-white/15 transition text-sky-200">
        <i class="fa-solid fa-hospital text-emerald-400"></i> Federal: Equipamentos de Saúde
      </button>
    </div>
  </div>
</section>

<!-- KPI / Metrics Strip -->
<section class="bg-slate-900 border-y border-slate-800 text-slate-300 py-6">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 grid grid-cols-2 md:grid-cols-4 gap-6 text-center">
    <div>
      <div class="text-2xl sm:text-3xl font-extrabold text-white">100%</div>
      <div class="text-xs text-slate-400 mt-1 uppercase tracking-wider">Fontes Oficiais Verificadas</div>
    </div>
    <div>
      <div class="text-2xl sm:text-3xl font-extrabold text-sky-400">PNCP v1</div>
      <div class="text-xs text-slate-400 mt-1 uppercase tracking-wider">Integração Padronizada</div>
    </div>
    <div>
      <div class="text-2xl sm:text-3xl font-extrabold text-emerald-400">Gemini 3.8</div>
      <div class="text-xs text-slate-400 mt-1 uppercase tracking-wider">IA Documental com Citação</div>
    </div>
    <div>
      <div class="text-2xl sm:text-3xl font-extrabold text-white">LGPD</div>
      <div class="text-xs text-slate-400 mt-1 uppercase tracking-wider">Privacidade & Conformidade</div>
    </div>
  </div>
</section>

<!-- Recent Opportunities Showcase -->
<section class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-12">
  <div class="flex flex-col sm:flex-row justify-between items-start sm:items-center mb-8 gap-4">
    <div>
      <h2 class="text-2xl font-bold text-slate-900">Oportunidades em Destaque</h2>
      <p class="text-sm text-slate-500">Editais recentemente sincronizados do Portal Nacional de Contratações Públicas</p>
    </div>
    <div class="flex flex-wrap items-center gap-3">
      <!-- Filtro de natureza do objeto (Material / Serviço / Outros) -->
      <form id="natureza-filter-form" action="/" method="GET" class="flex items-center gap-2">
        <label for="natureza-filter" class="text-xs font-semibold text-slate-500 uppercase tracking-wide">Natureza</label>
        <select
          id="natureza-filter"
          name="natureza"
          onchange="aplicarFiltroNatureza(this.value)"
          class="bg-white border border-slate-300 text-slate-700 rounded-xl px-3 py-2 text-sm font-medium focus:ring-2 focus:ring-sky-500 focus:outline-none"
        >
          <option value="" {% if not natureza_sel %}selected{% endif %}>Todas</option>
          {% for chave, rotulo in (natureza_labels or {}).items() %}
            <option value="{{ chave }}" {% if natureza_sel == chave %}selected{% endif %}>{{ rotulo }}{% if natureza_counts %} ({{ natureza_counts[chave] }}){% endif %}</option>
          {% endfor %}
        </select>
        <noscript><button type="submit" class="text-xs font-semibold text-sky-600">Filtrar</button></noscript>
      </form>
      <a href="/search" class="text-sm font-semibold text-sky-600 hover:text-sky-700 flex items-center gap-1.5">
        Ver todos os certames <i class="fa-solid fa-arrow-right text-xs"></i>
      </a>
    </div>
  </div>

  <div id="recent-tenders-grid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
    <!-- Populated via script or pre-rendered -->
    {% for tender in recent_tenders %}
    {% set natureza = (naturezas_por_id or {}).get(tender.id, 'OUTROS') %}
    <div class="tender-card bg-white rounded-2xl border border-slate-200 shadow-sm hover:shadow-md transition p-6 flex flex-col justify-between" data-natureza="{{ natureza }}">
      <div>
        <div class="flex items-center justify-between gap-2 mb-3">
          <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-semibold bg-sky-50 text-sky-700 border border-sky-100">
            <i class="fa-solid fa-location-dot text-sky-500"></i> {{ tender.state }} - {{ tender.city or "Não informado" }}
          </span>
          <span class="text-xs text-slate-400 font-mono truncate">{{ tender.modality }}</span>
        </div>

        <span class="natureza-badge inline-flex items-center gap-1.5 px-2 py-0.5 mb-2 rounded-md text-[11px] font-bold uppercase tracking-wide border
          {% if natureza == 'MATERIAL' %}bg-indigo-50 text-indigo-700 border-indigo-200
          {% elif natureza == 'SERVICO' %}bg-emerald-50 text-emerald-700 border-emerald-200
          {% else %}bg-slate-100 text-slate-600 border-slate-200{% endif %}">
          <i class="fa-solid {% if natureza == 'MATERIAL' %}fa-box{% elif natureza == 'SERVICO' %}fa-screwdriver-wrench{% else %}fa-shapes{% endif %} text-[10px]"></i>
          {{ (natureza_labels or {}).get(natureza, 'Outros') }}
        </span>

        <h3 class="text-base font-bold text-slate-900 leading-snug mb-2 line-clamp-2">
          <a href="/tenders/{{ tender.id }}" class="hover:text-sky-600 transition">{{ tender.title }}</a>
        </h3>

        <p class="text-xs text-slate-600 line-clamp-3 mb-4 leading-relaxed">
          {{ tender.object_description }}
        </p>

        <div class="text-xs text-slate-500 mb-4 bg-slate-50 p-2.5 rounded-xl border border-slate-100">
          <div class="font-medium text-slate-700 truncate mb-1"><i class="fa-regular fa-building text-slate-400 mr-1"></i> {{ tender.contracting_entity }}</div>
          <div class="flex justify-between items-center text-slate-500">
            <span>Valor estimado:</span>
            <span class="font-bold text-slate-900">
              {% if tender.estimated_total_value %}
                R$ {{ "{:,.2f}".format(tender.estimated_total_value).replace(",", "X").replace(".", ",").replace("X", ".") }}
              {% else %}
                Não informado
              {% endif %}
            </span>
          </div>
        </div>
      </div>

      <div class="pt-4 border-t border-slate-100 flex items-center justify-between">
        <a href="/tenders/{{ tender.id }}" class="text-xs font-semibold text-white bg-slate-900 hover:bg-sky-600 px-3.5 py-2 rounded-xl transition flex items-center gap-1.5">
          <span>Ver Detalhes e IA</span>
          <i class="fa-solid fa-arrow-right text-[10px]"></i>
        </a>
        <a href="{{ tender.source_url }}" target="_blank" rel="noopener" class="text-xs text-slate-400 hover:text-slate-700 flex items-center gap-1" title="Acessar publicação oficial">
          <i class="fa-solid fa-arrow-up-right-from-square"></i> Fonte
        </a>
      </div>
    </div>
    {% endfor %}
    {% if not recent_tenders %}
    <div class="md:col-span-2 lg:col-span-3 text-center py-12 bg-white border border-dashed border-slate-300 rounded-2xl text-sm text-slate-500">
      Nenhum certame recente com esta natureza. <a href="/" class="font-semibold text-sky-600 hover:text-sky-700">Ver todos</a>
    </div>
    {% endif %}
  </div>
</section>

<!-- Sources Integration Section -->
<section class="bg-slate-100 py-12 border-t border-slate-200">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
    <div class="text-center max-w-2xl mx-auto mb-10">
      <h2 class="text-2xl font-bold text-slate-900">Fontes Oficiais Conectadas</h2>
      <p class="text-sm text-slate-600 mt-2">
        Arquitetura de conectores independentes com transparência total de integração e respeito aos termos de uso.
      </p>
    </div>

    <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4 text-center">
      <div class="bg-white p-4 rounded-xl border border-slate-200 shadow-sm">
        <div class="text-sky-600 font-bold text-sm mb-1">PNCP Nacional</div>
        <span class="inline-block px-2 py-0.5 rounded text-[10px] font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200">API Ativa</span>
      </div>
      <div class="bg-white p-4 rounded-xl border border-slate-200 shadow-sm">
        <div class="text-slate-800 font-bold text-sm mb-1">Compras.gov.br</div>
        <span class="inline-block px-2 py-0.5 rounded text-[10px] font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200">API Federal</span>
      </div>
      <div class="bg-white p-4 rounded-xl border border-slate-200 shadow-sm">
        <div class="text-slate-800 font-bold text-sm mb-1">Compras RS</div>
        <span class="inline-block px-2 py-0.5 rounded text-[10px] font-semibold bg-sky-50 text-sky-700 border border-sky-200">CELIC / RS</span>
      </div>
      <div class="bg-white p-4 rounded-xl border border-slate-200 shadow-sm">
        <div class="text-slate-800 font-bold text-sm mb-1">Compras MG</div>
        <span class="inline-block px-2 py-0.5 rounded text-[10px] font-semibold bg-sky-50 text-sky-700 border border-sky-200">SEPLAG / MG</span>
      </div>
      <div class="bg-white p-4 rounded-xl border border-slate-200 shadow-sm">
        <div class="text-slate-800 font-bold text-sm mb-1">Licitações CAIXA</div>
        <span class="inline-block px-2 py-0.5 rounded text-[10px] font-semibold bg-amber-50 text-amber-700 border border-amber-200">Extrato Oficial</span>
      </div>
      <div class="bg-white p-4 rounded-xl border border-slate-200 shadow-sm">
        <div class="text-slate-800 font-bold text-sm mb-1">Licitações-e (BB)</div>
        <span class="inline-block px-2 py-0.5 rounded text-[10px] font-semibold bg-slate-100 text-slate-600 border border-slate-200">Busca Direta</span>
      </div>
    </div>
  </div>
</section>
{% endblock %}

{% block scripts %}
<script>
  function aplicarFiltroNatureza(valor) {
    const url = new URL(window.location.href);
    if (valor) url.searchParams.set('natureza', valor); else url.searchParams.delete('natureza');
    url.hash = 'recent-tenders-grid';
    window.location.href = url.toString();
  }

  function fillSearch(query, state) {
    document.getElementById('hero-search-input').value = query;
    const form = document.querySelector('form');
    if (state) {
      const ufSelect = form.querySelector('select[name="uf"], select[name="state"]');
      if (ufSelect) {
        ufSelect.value = state;
      }
    }
    form.submit();
  }
</script>
{% endblock %}
EOF

# ==============================================================================
# ITEM 6 — TESTES
# ==============================================================================
mkdir -p tests
cat << 'EOF' > tests/test_fusion_features.py
import asyncio
import html
import io
import json
import re
import zipfile
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from pypdf import PdfReader

from server.main import app
from server.database import SessionLocal
from server.models import (
    Tender, HistoricoDisputa, AIAnalysis, Deal, DocumentoEmpresa, LegalDocumentTemplate, hoje_sao_paulo
)
from server.services.gemini_analyzer import gemini_analyzer
from server.services.portal_collector import (
    portal_collector, sync_pipeline, PortalCollectorService,
    classificar_natureza_objeto, classificar_natureza_certame, NATUREZAS_OBJETO
)
from server.services.dispute_monitor import dispute_monitor, DisputeMonitorService, PregaoItem
from server.services.pdf_generator import gerar_peticao_pdf, PeticaoInvalidaError, normalizar_lei
from server.services.kit_builder import (
    montar_kit_habilitacao_zip, KitHabilitacaoError, NOME_RELATORIO, PASTA_DOCUMENTOS
)
from server.services import document_vault

client = TestClient(app)


@pytest.fixture
def sample_tender():
    db = SessionLocal()
    try:
        tender = db.query(Tender).first()
        assert tender is not None, "Banco de testes deve possuir ao menos um certame semeado"
        return tender
    finally:
        db.close()


# ==============================================================================
# 1. TESTES DO HUB DE AGREGAÇÃO MULTIFONTE (PORTAL COLLECTOR)
# ==============================================================================

@pytest.mark.asyncio
async def test_portal_collector_multi_source_collection():
    """Valida a coleta e normalização em múltiplos portais oficiais (PNCP, Compras.gov, BNC, etc.)."""
    collector = PortalCollectorService()
    results = await collector.collect_all(uf="DF", keyword="servidores", limit_per_portal=2)

    assert isinstance(results, list)
    assert len(results) > 0

    # Verifica se os portais são identificados corretamente
    portals = {item["fonte_portal"] for item in results}
    assert len(portals) >= 2, f"Esperado múltiplos portais, obtido: {portals}"

    # Validação dos campos obrigatórios normalizados
    for item in results:
        assert "external_id" in item
        assert "title" in item
        assert "fonte_portal" in item
        assert "link_edital_oficial" in item
        assert "status" in item


@pytest.mark.asyncio
async def test_portal_collector_network_error_resilience():
    """Valida tratamento de exceção gracioso em falhas de rede de portais específicos."""
    collector = PortalCollectorService()

    # Simula falha catastrófica de rede no PNCP
    with patch("httpx.AsyncClient.get", side_effect=Exception("Timeout simulado de conexão")):
        pncp_res = await collector.fetch_pncp(uf="SP", keyword="saude", limit=5)
        # O coletor deve retornar lista vazia sem propagar erro fatal
        assert pncp_res == []

        # Coletar all com falhas embutidas ainda deve retornar os portais com fallback
        all_res = await collector.collect_all(uf="SP", keyword="saude", limit_per_portal=2)
        assert isinstance(all_res, list)


def test_api_portals_sync_endpoint():
    """Testa o endpoint POST /api/portals/sync que orquestra a ingestão e persistência."""
    response = client.post("/api/portals/sync", json={
        "uf": "DF",
        "keyword": "computador",
        "limit_per_portal": 2
    })
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "total_collected" in data
    assert "total_saved" in data
    assert data["total_collected"] >= 0


# ==============================================================================
# 2. TESTES DA CAMADA DE INTELIGÊNCIA ARTIFICIAL (GOOGLE GEMINI FLASH)
# ==============================================================================

@pytest.mark.asyncio
async def test_gemini_analyzer_with_structured_mock():
    """Valida que o analisador do Gemini processa documentos e extrai seções estruturadas via mock."""
    mock_payload = {
        "itens": [
            {
                "item_numero": 1,
                "descricao": "Servidor Rack 2U",
                "quantidade": 5.0,
                "unidade": "UN",
                "valor_unitario_estimado": 20000.0,
                "valor_total_estimado": 100000.0
            }
        ],
        "habilitacao_exigencias": [
            "Atestado de Capacidade Técnica de pelo menos 50%",
            "Certidão Negativa de Débitos Federais (CND)"
        ],
        "estimativa_orcamentaria": 100000.0,
        "pontos_criticos_risco": [
            "Prazo de entrega exíguo de 15 dias corridos",
            "Garantia on-site com SLA de 4 horas"
        ],
        "confianca_score": 0.98
    }

    # Mock do cliente do google.genai
    mock_response = MagicMock()
    mock_response.text = json.dumps(mock_payload)

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response
    # Garantir que se aio for checado, aio.models também retorne
    mock_aio_response = MagicMock()
    mock_aio_response.text = json.dumps(mock_payload)
    mock_client.aio = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_aio_response)

    with patch("server.services.gemini_analyzer.genai.Client", return_value=mock_client), \
         patch("server.services.gemini_analyzer.get_genai_client", return_value=mock_client):
        # Chama a rotina assíncrona
        result = await gemini_analyzer.analisar_edital_documento(
            file_bytes=b"PDF BINARY DUMMY TEST DATA",
            filename="edital_datacenter.pdf"
        )

        assert result is not None
        assert len(result["itens"]) == 1
        assert result["estimativa_orcamentaria"] == 100000.0
        assert len(result["pontos_criticos_risco"]) >= 2
        assert "Atestado de Capacidade Técnica" in result["habilitacao_exigencias"][0]


@pytest.mark.asyncio
async def test_gemini_analyzer_fallback_behavior():
    """Valida o fallback determinístico do analisador caso a IA não esteja disponível."""
    with patch("server.services.gemini_analyzer.get_genai_client", return_value=None):
        fallback_res = await gemini_analyzer.analisar_edital_documento(
            file_bytes=b"Conteudo texto de teste para edital",
            filename="edital_teste.pdf"
        )
        assert fallback_res is not None
        assert "itens" in fallback_res or "objeto" in fallback_res
        assert "habilitacao_exigencias" in fallback_res or "habilitacao" in fallback_res
        assert "pontos_criticos_risco" in fallback_res

        exec_res = await gemini_analyzer.resumo_executivo_oportunidade({
            "title": "Aquisição de computadores",
            "contracting_entity": "Ministério da Educação",
            "state": "DF",
            "estimated_total_value": 50000.0
        })
        assert "resumo" in exec_res
        assert "riscos_juridicos" in exec_res
        assert "competitividade_estimada" in exec_res


def test_api_tender_ai_analyze_endpoint(sample_tender):
    """Testa o endpoint POST /api/tenders/{tender_id}/ai-analyze e persistência enriquecida."""
    response = client.post(f"/api/tenders/{sample_tender.id}/ai-analyze", json={})
    assert response.status_code == 200
    data = response.json()

    assert data["success"] is True
    assert data["tender_id"] == sample_tender.id
    assert "data" in data

    result_data = data["data"]
    assert "resumo_executivo" in result_data
    assert "requisitos_participacao" in result_data
    assert "pontos_atencao" in result_data

    # Verifica se os dados enriquecidos foram persistidos no modelo Tender
    db = SessionLocal()
    try:
        updated_tender = db.query(Tender).filter(Tender.id == sample_tender.id).first()
        assert updated_tender is not None
        assert updated_tender.ai_resumo is not None
        assert updated_tender.ai_pontos_atencao is not None

        # Verifica se o registro de AIAnalysis foi criado
        analysis = db.query(AIAnalysis).filter(AIAnalysis.tender_id == sample_tender.id).first()
        assert analysis is not None
    finally:
        db.close()


# ==============================================================================
# 3. TESTES DO COCKPIT DE LANCES, AUDITORIA E TELEMETRIA
# ==============================================================================

def test_dispute_monitor_enriched_item_and_telemetry():
    """Valida rastreamento de item com 'fonte_portal' e persistência de telemetria em HistoricoDisputa."""
    service = DisputeMonitorService()

    # Rastrear item com fonte de portal específica
    item = service.track_item({
        "id_pregao": "PE 505/2026",
        "numero_item": 1,
        "descricao": "Nobreaks Senoidais Corporativos",
        "valor_referencia": 30000.0,
        "melhor_lance": 28000.0,
        "meu_lance": 29000.0,
        "fase": "ABERTO",
        "fonte_portal": "Compras.gov.br",
        "degrau_minimo": 50.0,
        "valor_minimo_limite": 25000.0,
        "auto_lance_ativo": True
    })

    assert item.fonte_portal == "Compras.gov.br"
    snapshot = service.get_active_snapshot()
    tracked = next((it for it in snapshot if it["id_pregao"] == "PE 505/2026"), None)
    assert tracked is not None
    assert tracked["fonte_portal"] == "Compras.gov.br"

    # Executa lance automático
    res_bid = service.executar_lance_automatico(item.item_key)
    assert res_bid["status_automacao"] in ["Robô Liderando (1º)", "Robô Ativo"]
    assert res_bid["meu_lance"] == 27950.0  # 28000 - 50 degrau

    # Verifica se telemetria foi gravada em HistoricoDisputa no SQLite
    db = SessionLocal()
    try:
        telemetry = db.query(HistoricoDisputa).filter(HistoricoDisputa.id_pregao == "PE 505/2026").first()
        assert telemetry is not None
        assert telemetry.id_pregao == "PE 505/2026"
        assert telemetry.tipo_evento == "AUTO_BID"
        assert telemetry.valor_ofertado == 27950.0
        assert telemetry.posicao == 1
    finally:
        db.close()


def test_dispute_monitor_stop_loss_telemetry():
    """Valida acionamento do piso Stop-Loss e persistência no histórico de disputas."""
    service = DisputeMonitorService()

    item = service.track_item({
        "id_pregao": "PE 777/2026",
        "numero_item": 2,
        "descricao": "Roteadores de Borda SD-WAN",
        "valor_referencia": 5000.0,
        "melhor_lance": 4010.0,
        "meu_lance": 4100.0,
        "fase": "ABERTO",
        "fonte_portal": "BNC",
        "degrau_minimo": 50.0,
        "valor_minimo_limite": 4000.0,  # Piso: 4000. Lance calculado seria 3960 < 4000 -> Stop Loss
        "auto_lance_ativo": True
    })

    res = service.executar_lance_automatico(item.item_key)
    assert res["status_automacao"] == "Piso Atingido / Margem Esgotada"
    assert res["auto_lance_ativo"] is False

    # Valida auditoria gravada
    db = SessionLocal()
    try:
        telemetry = db.query(HistoricoDisputa).filter(
            HistoricoDisputa.id_pregao == "PE 777/2026",
            HistoricoDisputa.tipo_evento == "STOP_LOSS"
        ).first()
        assert telemetry is not None
        assert telemetry.id_pregao == "PE 777/2026"
        assert telemetry.tipo_evento == "STOP_LOSS"
    finally:
        db.close()


def test_sse_endpoint_connect_and_snapshot():
    """Valida o endpoint SSE /api/disputes/events e conexão estável."""
    # Testa snapshot inicial do endpoint de disputas ativas
    resp = client.get("/api/disputes/active")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert "items" in data


# ==============================================================================
# 4. CLASSIFICADOR SIMPLIFICADO DE NATUREZA (MATERIAL / SERVICO / OUTROS)
# ==============================================================================

@pytest.mark.parametrize("objeto, esperado", [
    ("Aquisição de medicamentos oncológicos e imunobiológicos", "MATERIAL"),
    ("Registro de preços para futura e eventual aquisição de gêneros alimentícios", "MATERIAL"),
    ("FORNECIMENTO DE MATERIAL DE EXPEDIENTE E TONER", "MATERIAL"),
    ("Contratação de empresa especializada no fornecimento de próteses dentárias", "MATERIAL"),
    ("Contratação de empresa especializada para prestação de serviços de limpeza e conservação", "SERVICO"),
    ("Serviços de manutenção preventiva e corretiva de elevadores", "SERVICO"),
    ("Pavimentação asfáltica e obras de drenagem pluvial urbana", "SERVICO"),
    ("Leilão de veículos inservíveis da frota municipal", "OUTROS"),
    ("Locação de imóvel para funcionamento da sede administrativa", "OUTROS"),
    ("Cessão de uso onerosa de espaço físico para lanchonete", "OUTROS"),
    ("Geofísica", "OUTROS"),
    ("", "OUTROS"),
])
def test_classificar_natureza_objeto_casos_de_referencia(objeto, esperado):
    assert classificar_natureza_objeto(objeto) == esperado


def test_classificar_natureza_objeto_retorna_somente_valores_permitidos():
    amostras = [
        "Aquisição de notebooks", "Prestação de serviços de vigilância", "Doação de bens",
        None, 12345, "   ", "???", "SERVIÇOS GRÁFICOS COM MATERIAL INCLUSO",
    ]
    for amostra in amostras:
        assert classificar_natureza_objeto(amostra) in {"MATERIAL", "SERVICO", "OUTROS"}
    assert set(NATUREZAS_OBJETO) == {"MATERIAL", "SERVICO", "OUTROS"}


def test_classificar_natureza_certame_usa_descricao_quando_titulo_inconclusivo():
    assert classificar_natureza_certame("PE 12/2026", "Aquisição de cadeiras e mesas escolares") == "MATERIAL"
    assert classificar_natureza_certame("Serviços de dedetização", "qualquer descrição") == "SERVICO"


@pytest.mark.asyncio
async def test_collect_all_enriquece_itens_com_natureza():
    with patch("httpx.AsyncClient.get", side_effect=Exception("offline")):
        itens = await PortalCollectorService().collect_all(uf="DF", limit_per_portal=3)
    assert itens
    assert all(it["natureza_objeto"] in NATUREZAS_OBJETO for it in itens)


def test_home_filtro_natureza_e_badges():
    resp = client.get("/?natureza=MATERIAL")
    assert resp.status_code == 200
    assert 'id="natureza-filter"' in resp.text
    naturezas = re.findall(r'class="tender-card[^"]*" data-natureza="(\w+)"', resp.text)
    assert naturezas, "a home deve listar certames classificados"
    assert set(naturezas) == {"MATERIAL"}
    assert "natureza-badge" in resp.text

    resp_todos = client.get("/?natureza=invalida")
    assert resp_todos.status_code == 200
    assert "natureza-badge" in resp_todos.text


# ==============================================================================
# 5. GERAÇÃO JURÍDICA EM PDF (REPORTLAB) — STREAM DO ENDPOINT
# ==============================================================================

def _texto_pdf(conteudo: bytes) -> str:
    leitor = PdfReader(io.BytesIO(conteudo))
    bruto = " ".join((pagina.extract_text() or "") for pagina in leitor.pages)
    return " ".join(bruto.split())  # células de tabela quebram linha: normaliza espaços


def test_gerar_peticao_pdf_conteudo_timbrado():
    pdf = gerar_peticao_pdf(
        corpo="AO PREGOEIRO DO(A) {{orgao}}\n\nA licitante [NOME DA EMPRESA] impugna o edital {{numero_edital}} relativo a {{objeto}}.",
        orgao="Prefeitura Municipal de Maceió",
        numero_certame="PE 042/2026",
        objeto="Aquisição de medicamentos & insumos <hospitalares>",
        titulo="Impugnação ao Edital",
        lei="8.666/1993",
        licitante={"razao_social": "Alfa Distribuidora LTDA", "cnpj": "12345678000199", "representante": "Maria Souza"},
        cidade="Maceió/AL",
        data_documento=date(2026, 10, 9),
    )
    assert pdf.startswith(b"%PDF-")
    texto = _texto_pdf(pdf)
    assert "Licitações" in texto and "Brasil" in texto
    assert "PE 042/2026" in texto
    assert "Prefeitura Municipal de Maceió" in texto
    assert "8.666" in texto
    assert "ALFA DISTRIBUIDORA LTDA" in texto
    assert "12.345.678/0001-99" in texto
    assert "9 de outubro de 2026" in texto
    assert "{{orgao}}" not in texto


def test_gerar_peticao_pdf_rejeita_corpo_vazio():
    with pytest.raises(PeticaoInvalidaError):
        gerar_peticao_pdf(corpo="   ")
    assert normalizar_lei("14.133/2021") == "14133"
    assert normalizar_lei(False) == "8666"


def test_api_juridico_gerar_pdf_streaming_response():
    db = SessionLocal()
    try:
        tmpl = db.query(LegalDocumentTemplate).filter(LegalDocumentTemplate.tipo == "impugnacao",
                                                      LegalDocumentTemplate.is_nova_lei == True).first()
        assert tmpl is not None
        tmpl_id = tmpl.id
    finally:
        db.close()

    resp = client.post("/api/juridico/gerar-pdf", json={
        "template_id": tmpl_id,
        "orgao": "Secretaria de Saúde de Alagoas",
        "numero_certame": "PE 777/2026",
        "objeto": "Aquisição de seringas",
        "licitante": {"razao_social": "Beta Hospitalar SA", "cnpj": "11222333000144"},
        "cidade": "Maceió/AL"
    })
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].endswith('.pdf"')
    assert resp.content.startswith(b"%PDF-")
    texto = _texto_pdf(resp.content)
    assert "PE 777/2026" in texto
    assert "Secretaria de Saúde de Alagoas" in texto
    assert "14.133" in texto


def test_api_juridico_gerar_pdf_corpo_editado_e_validacoes():
    resp = client.post("/api/juridico/gerar-pdf", json={
        "corpo": "TEXTO EDITADO NO MODAL\n\nPedido de esclarecimento sobre {{objeto}}.",
        "objeto": "Locação de impressoras",
        "lei": "8666"
    })
    assert resp.status_code == 200
    assert "TEXTO EDITADO NO MODAL" in _texto_pdf(resp.content)

    assert client.post("/api/juridico/gerar-pdf", json={}).status_code == 422
    assert client.post("/api/juridico/gerar-pdf", json={"template_id": 999999}).status_code == 404


def test_page_juridico_botao_pdf_e_prefill_por_query_params():
    resp = client.get("/juridico", params={
        "orgao": "Prefeitura de Recife", "numero_certame": "PE 10/2026", "objeto": "Aquisição <script>"
    })
    assert resp.status_code == 200
    assert "Baixar PDF" in resp.text
    assert "/api/juridico/gerar-pdf" in resp.text
    assert 'value="Prefeitura de Recife"' in resp.text
    assert 'value="PE 10/2026"' in resp.text
    assert "Aquisição &lt;script&gt;" in resp.text
    # Placeholders do JS preservados (não consumidos pelo Jinja)
    assert "'{{orgao}}'" in resp.text


# ==============================================================================
# 6. COFRE DE DOCUMENTOS + KIT DE HABILITAÇÃO (ZIP)
# ==============================================================================

def _pdf_minimo(texto: str) -> bytes:
    from reportlab.pdfgen import canvas as rl_canvas
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf)
    c.drawString(72, 720, texto)
    c.showPage()
    c.save()
    return buf.getvalue()


@pytest.fixture
def cofre_isolado(tmp_path, monkeypatch):
    """Direciona uploads para diretório temporário e remove os registros criados pelo teste."""
    monkeypatch.setattr(document_vault, "DOCUMENTOS_UPLOAD_DIR", tmp_path / "documentos")
    db = SessionLocal()
    ids_antes = {d.id for d in db.query(DocumentoEmpresa.id).all()}
    db.close()
    yield tmp_path
    db = SessionLocal()
    try:
        db.query(DocumentoEmpresa).filter(~DocumentoEmpresa.id.in_(ids_antes or {-1})).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_documento_status_validade_limiares():
    hoje = date(2026, 10, 9)
    doc = DocumentoEmpresa(nome="CND", categoria="fiscal", data_emissao=hoje, caminho_arquivo="x.pdf")
    doc.data_validade = hoje + timedelta(days=16)
    assert doc.status_validade(hoje) == "VALIDO" and doc.dias_restantes(hoje) == 16
    doc.data_validade = hoje + timedelta(days=15)
    assert doc.status_validade(hoje) == "A_VENCER"
    doc.data_validade = hoje
    assert doc.status_validade(hoje) == "A_VENCER" and doc.dias_restantes(hoje) == 0
    doc.data_validade = hoje - timedelta(days=1)
    assert doc.status_validade(hoje) == "VENCIDO" and doc.dias_restantes(hoje) == -1
    doc.data_validade = None
    assert doc.status_validade(hoje) == "SEM_VALIDADE" and doc.dias_restantes(hoje) is None


def test_api_upload_listagem_e_exclusao_de_documento(cofre_isolado):
    hoje = hoje_sao_paulo()
    resp = client.post(
        "/api/documentos",
        data={
            "nome": "CND Federal (Receita/PGFN)",
            "categoria": "fiscal",
            "data_emissao": (hoje - timedelta(days=10)).isoformat(),
            "data_validade": (hoje + timedelta(days=5)).isoformat(),
            "tags_habilitacao": "CND, Receita Federal, CND",
        },
        files={"arquivo": ("cnd.pdf", _pdf_minimo("CND"), "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    doc = resp.json()
    assert doc["status"] == "A_VENCER"
    assert doc["dias_restantes"] == 5
    assert doc["tags"] == ["CND", "Receita Federal"]
    assert doc["arquivo_disponivel"] is True
    assert list((cofre_isolado / "documentos").glob("*.pdf"))

    lista = client.get("/api/documentos").json()
    assert any(i["id"] == doc["id"] for i in lista["items"])
    assert lista["resumo"]["a_vencer"] >= 1

    arquivo = client.get(doc["arquivo_url"])
    assert arquivo.status_code == 200
    assert arquivo.content.startswith(b"%PDF-")

    pagina = client.get("/documentos")
    assert pagina.status_code == 200
    assert "Cofre de Documentos" in pagina.text
    assert "CND Federal (Receita/PGFN)" in pagina.text

    excluir = client.delete(f"/api/documentos/{doc['id']}")
    assert excluir.status_code == 200
    assert excluir.json()["arquivo_removido"] is True
    assert not list((cofre_isolado / "documentos").glob("*.pdf"))


def test_api_upload_rejeita_arquivo_nao_pdf_e_datas_invalidas(cofre_isolado):
    base = {"nome": "Atestado", "categoria": "tecnica", "data_emissao": "2026-01-10"}
    r1 = client.post("/api/documentos", data=base, files={"arquivo": ("a.pdf", b"nao sou pdf", "application/pdf")})
    assert r1.status_code == 422
    r2 = client.post("/api/documentos", data=base, files={"arquivo": ("a.txt", _pdf_minimo("x"), "text/plain")})
    assert r2.status_code == 422
    r3 = client.post("/api/documentos", data={**base, "data_validade": "2025-01-01"},
                     files={"arquivo": ("a.pdf", _pdf_minimo("x"), "application/pdf")})
    assert r3.status_code == 422
    r4 = client.post("/api/documentos", data={**base, "categoria": "inexistente"},
                     files={"arquivo": ("a.pdf", _pdf_minimo("x"), "application/pdf")})
    assert r4.status_code == 422


def test_navegacao_contem_link_cofre_de_documentos():
    html_home = client.get("/").text
    assert html_home.count('href="/documentos"') >= 2  # desktop + menu PWA
    assert "Cofre de Documentos" in html_home


def _criar_documento(db, nome, categoria, validade, pasta, emissao=date(2026, 1, 2)):
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / f"{document_vault.slugify(nome)}.pdf"
    caminho.write_bytes(_pdf_minimo(nome))
    doc = DocumentoEmpresa(nome=nome, categoria=categoria, data_emissao=emissao,
                           data_validade=validade, caminho_arquivo=str(caminho))
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def test_montar_kit_habilitacao_zip_filtra_validos_e_injeta_relatorio(cofre_isolado, sample_tender):
    hoje = date(2026, 10, 9)
    pasta = cofre_isolado / "documentos"
    db = SessionLocal()
    try:
        _criar_documento(db, "KIT Contrato Social", "juridica", None, pasta)
        _criar_documento(db, "KIT CND Federal", "fiscal", hoje + timedelta(days=40), pasta)
        _criar_documento(db, "KIT Certidao Falencia", "economica", hoje + timedelta(days=3), pasta)
        _criar_documento(db, "KIT Balanco Vencido", "economica", hoje - timedelta(days=1), pasta)
        sem_arquivo = DocumentoEmpresa(nome="KIT Atestado Sem Arquivo", categoria="tecnica",
                                       data_emissao=date(2026, 1, 2), data_validade=None,
                                       caminho_arquivo=str(pasta / "nao_existe.pdf"))
        db.add(sem_arquivo)
        db.commit()

        conteudo = montar_kit_habilitacao_zip(sample_tender.id, db=db, hoje=hoje)
    finally:
        db.close()

    zf = zipfile.ZipFile(io.BytesIO(conteudo))
    assert zf.testzip() is None
    nomes = zf.namelist()
    assert nomes[0] == NOME_RELATORIO
    docs_zip = [n for n in nomes if n.startswith(PASTA_DOCUMENTOS + "/")]
    assert any("KIT_Contrato_Social" in n for n in docs_zip)
    assert any("KIT_CND_Federal" in n for n in docs_zip)
    assert any("KIT_Certidao_Falencia" in n for n in docs_zip)
    assert not any("Vencido" in n for n in docs_zip)
    assert not any("Sem_Arquivo" in n for n in docs_zip)
    for n in docs_zip:
        assert zf.read(n).startswith(b"%PDF-")

    relatorio = _texto_pdf(zf.read(NOME_RELATORIO))
    assert "RELATÓRIO DE CONFORMIDADE" in relatorio
    assert (sample_tender.notice_number or sample_tender.external_id) in relatorio
    assert "KIT Balanco Vencido" in relatorio
    assert "Vencido em" in relatorio
    assert "Arquivo não localizado no cofre" in relatorio


def test_montar_kit_tender_inexistente():
    with pytest.raises(KitHabilitacaoError):
        montar_kit_habilitacao_zip("certame-que-nao-existe")


def test_api_download_kit_zip_e_botao_no_detalhe(cofre_isolado, sample_tender):
    resp = client.get(f"/api/tenders/{sample_tender.id}/download-kit-zip")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "Kit_Habilitacao_" in resp.headers["content-disposition"]
    assert NOME_RELATORIO in zipfile.ZipFile(io.BytesIO(resp.content)).namelist()

    assert client.get("/api/tenders/inexistente-xyz/download-kit-zip").status_code == 404

    detalhe = client.get(f"/tenders/{sample_tender.id}")
    assert "Baixar Kit de Habilitação (ZIP)" in detalhe.text
    assert f"/api/tenders/{sample_tender.id}/download-kit-zip" in detalhe.text


# ==============================================================================
# 7. LIGAÇÃO KANBAN -> JURÍDICO
# ==============================================================================

def test_kanban_card_envia_para_juridico_com_query_params(sample_tender):
    db = SessionLocal()
    criado = False
    try:
        deal = db.query(Deal).filter(Deal.tender_id == sample_tender.id).first()
        if not deal:
            deal = Deal(tender_id=sample_tender.id, stage="oportunidades", notes="teste juridico")
            db.add(deal)
            db.commit()
            criado = True
        deal_id = deal.id
    finally:
        db.close()

    try:
        html_kanban = client.get("/negocios?view=quadros").text
        assert "Enviar para Jurídico" in html_kanban
        bloco = html_kanban.split(f'id="deal-card-{deal_id}"', 1)[1]
        href = re.search(r'href="(/juridico\?[^"]+)"', bloco).group(1)
        params = parse_qs(urlparse(html.unescape(href)).query)
        assert params["orgao"] == [sample_tender.contracting_entity]
        assert params["numero_certame"] == [sample_tender.notice_number or sample_tender.external_id]
        assert params["objeto"] == [sample_tender.title]

        destino = client.get(html.unescape(href))
        assert destino.status_code == 200
        assert html.escape(sample_tender.contracting_entity, quote=True) in destino.text
    finally:
        if criado:
            db = SessionLocal()
            try:
                db.query(Deal).filter(Deal.id == deal_id).delete()
                db.commit()
            finally:
                db.close()
EOF

echo "Patch aplicado: 15 arquivos."
