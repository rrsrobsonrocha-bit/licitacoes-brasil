"""
Gerador de PDFs jurídicos no papel timbrado da empresa licitante (white-label, ReportLab).

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
def _caber(c: rl_canvas.Canvas, texto: str, fonte: str, tamanho: float, largura: float) -> str:
    """Corta o texto (com reticências) para caber na largura disponível."""
    texto = texto or ""
    if c.stringWidth(texto, fonte, tamanho) <= largura:
        return texto
    while texto and c.stringWidth(texto + "…", fonte, tamanho) > largura:
        texto = texto[:-1]
    return texto.rstrip() + "…"


def _desenhar_timbre(c: rl_canvas.Canvas, titulo_documento: str, referencia: str,
                     empresa: Optional[Dict[str, str]] = None) -> None:
    """
    Papel timbrado da empresa licitante (white-label): razão social, CNPJ/endereço e contato.
    Sem empresa cadastrada, cabeçalho neutro com o título do documento (nenhuma marca da plataforma).
    """
    c.saveState()
    topo = PAGE_H
    c.setFillColor(COR_NAVY)
    c.rect(0, topo - ALTURA_TIMBRE, PAGE_W, ALTURA_TIMBRE, stroke=0, fill=1)
    c.setFillColor(COR_AMBAR)
    c.rect(0, topo - ALTURA_TIMBRE - 1.2 * mm, PAGE_W, 1.2 * mm, stroke=0, fill=1)

    largura_util = PAGE_W - 2 * MARGEM_X
    largura_esquerda = largura_util * 0.66
    y_base = topo - ALTURA_TIMBRE / 2
    if empresa:
        c.setFont("Helvetica-Bold", 12.5)
        c.setFillColor(colors.white)
        c.drawString(MARGEM_X, y_base + 4 * mm, _caber(c, empresa.get("nome", ""), "Helvetica-Bold", 12.5, largura_esquerda))
        c.setFont("Helvetica", 7.2)
        c.setFillColor(colors.HexColor("#cbd5e1"))
        c.drawString(MARGEM_X, y_base - 0.6 * mm, _caber(c, empresa.get("linha_cnpj", ""), "Helvetica", 7.2, largura_esquerda))
        if empresa.get("contato"):
            c.drawString(MARGEM_X, y_base - 4.6 * mm, _caber(c, empresa["contato"], "Helvetica", 7.2, largura_esquerda))
        largura_direita = largura_util - largura_esquerda - 4 * mm
        c.setFont("Helvetica-Bold", 8)
        c.setFillColor(colors.white)
        c.drawRightString(PAGE_W - MARGEM_X, y_base + 2 * mm,
                          _caber(c, (titulo_documento or "").upper(), "Helvetica-Bold", 8, largura_direita))
        c.setFont("Helvetica", 7.5)
        c.setFillColor(COR_SKY)
        c.drawRightString(PAGE_W - MARGEM_X, y_base - 2.5 * mm,
                          _caber(c, referencia or "", "Helvetica", 7.5, largura_direita))
    else:
        c.setFont("Helvetica-Bold", 12)
        c.setFillColor(colors.white)
        c.drawString(MARGEM_X, y_base + 1 * mm, _caber(c, (titulo_documento or "").upper(), "Helvetica-Bold", 12, largura_util))
        c.setFont("Helvetica", 7.5)
        c.setFillColor(COR_SKY)
        c.drawString(MARGEM_X, y_base - 4 * mm, _caber(c, referencia or "", "Helvetica", 7.5, largura_util))
    c.restoreState()


def _desenhar_rodape(c: rl_canvas.Canvas, pagina: int, total: int, emitido_em: str,
                     empresa: Optional[Dict[str, str]] = None) -> None:
    c.saveState()
    y = MARGEM_BASE - 8 * mm
    c.setStrokeColor(COR_BORDA)
    c.setLineWidth(0.6)
    c.line(MARGEM_X, y + 4 * mm, PAGE_W - MARGEM_X, y + 4 * mm)
    c.setFont("Helvetica", 7)
    c.setFillColor(COR_CINZA)
    prefixo = f"{empresa['nome']}  •  " if empresa and empresa.get("nome") else ""
    c.drawString(MARGEM_X, y, _caber(c, f"{prefixo}Documento gerado eletronicamente em {emitido_em}", "Helvetica", 7,
                                     PAGE_W - 2 * MARGEM_X - 25 * mm))
    c.drawRightString(PAGE_W - MARGEM_X, y, f"Página {pagina} de {total}")
    c.restoreState()


def _canvas_numerado(titulo_documento: str, referencia: str, emitido_em: str, empresa: Optional[Dict[str, str]] = None):
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
                _desenhar_timbre(self, titulo_documento, referencia, empresa)
                _desenhar_rodape(self, self._pageNumber, total, emitido_em, empresa)
                super().showPage()
            super().save()

    return CanvasNumerado


def renderizar_pdf_timbrado(
    story: Sequence[Any],
    *,
    titulo_documento: str,
    referencia: str = "",
    assunto: str = "",
    empresa: Optional[Dict[str, str]] = None,
) -> bytes:
    """Renderiza os flowables no papel timbrado da empresa licitante (``empresa``) e retorna o PDF."""
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
        author=(empresa or {}).get("nome", ""),
        subject=assunto or titulo_documento,
        creator=(empresa or {}).get("nome", ""),
    )
    doc.build(list(story), canvasmaker=_canvas_numerado(titulo_documento, referencia, emitido_em, empresa))
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
# Marcadores dos modelos de peça -> campo da licitante (empresa ativa) ou do certame.
# [RECORRENTE] (recurso) e [RECORRIDA] (contrarrazões) são sempre a peticionária, isto é, a empresa da conta.
MARCADORES_LICITANTE = {
    "razao_social": r"NOME DA EMPRESA(?: REQUERENTE)?|RAZ[ÃA]O SOCIAL(?: DA LICITANTE)?|RECORRENTE|RECORRIDA|LICITANTE|EMPRESA",
    "cnpj": r"CNPJ",
    "endereco": r"ENDERE[ÇC]O(?: COMPLETO)?",
    "representante": r"(?:NOME DO )?REPRESENTANTE LEGAL",
    "cargo": r"CARGO",
    "email": r"E-?MAIL",
    "telefone": r"TELEFONE",
}
MARCADORES_CERTAME = {
    "numero_certame": r"N[ÚU]MERO DO CERTAME|N[ÚU]MERO DO EDITAL",
    "orgao": r"[ÓO]RG[ÃA]O COMPRADOR|[ÓO]RG[ÃA]O",
    "objeto": r"OBJETO DA LICITA[ÇC][ÃA]O|OBJETO",
}
_FECHO_LOCAL_DATA = re.compile(r"^\s*\[CIDADE[^\]]*\]\s*,\s*\[DATA\]\.?\s*$", re.IGNORECASE | re.MULTILINE)
_CIDADE = re.compile(r"\[CIDADE(?:\s*[-/]\s*UF)?\]", re.IGNORECASE)
_DATA = re.compile(r"\[DATA\]", re.IGNORECASE)


def _marcador(padrao: str) -> "re.Pattern[str]":
    return re.compile(rf"\[(?:{padrao})\]", re.IGNORECASE)


def preencher_peca(
    corpo: str,
    *,
    licitante: Optional[Dict[str, Any]] = None,
    orgao: Optional[str] = None,
    numero_certame: Optional[str] = None,
    objeto: Optional[str] = None,
    cidade: Optional[str] = None,
    data_documento: Optional[date] = None,
    remover_fecho: bool = False,
) -> str:
    """
    Preenche o texto de uma peça jurídica: tags {{orgao}}/{{numero_edital}}/{{objeto}}, marcadores da
    licitante ([RAZÃO SOCIAL DA LICITANTE], [NOME DA EMPRESA REQUERENTE], [CNPJ], [ENDEREÇO COMPLETO],
    [REPRESENTANTE LEGAL]...) e do certame ([NÚMERO DO CERTAME], [ÓRGÃO COMPRADOR], [OBJETO DA LICITAÇÃO]).
    Só substitui o que tem valor; o resto continua marcado para preenchimento.
    ``remover_fecho``: tira a linha "[CIDADE - UF], [DATA]." (o PDF já assina com local e data).
    """
    texto = (corpo or "").replace("\r\n", "\n")
    lic = {k: (str(v).strip() if v is not None else "") for k, v in (licitante or {}).items()}
    if lic.get("cnpj"):
        lic["cnpj"] = formatar_cnpj(lic["cnpj"])
    if lic.get("razao_social"):
        lic["razao_social"] = lic["razao_social"].upper()
    certame = {"orgao": (orgao or "").strip(), "numero_certame": (numero_certame or "").strip(),
               "objeto": (objeto or "").strip()}
    for tag, chave in (("{{orgao}}", "orgao"), ("{{numero_edital}}", "numero_certame"),
                       ("{{numero_certame}}", "numero_certame"), ("{{objeto}}", "objeto")):
        if certame[chave]:
            texto = texto.replace(tag, certame[chave])
    for chave, padrao in MARCADORES_CERTAME.items():
        if certame[chave]:
            texto = _marcador(padrao).sub(lambda _m, v=certame[chave]: v, texto)
    for chave, padrao in MARCADORES_LICITANTE.items():
        if lic.get(chave):
            texto = _marcador(padrao).sub(lambda _m, v=lic[chave]: v, texto)
    if remover_fecho:
        texto = _FECHO_LOCAL_DATA.sub("", texto)
    else:
        local = (cidade or lic.get("cidade_uf") or "").strip()
        if local:
            texto = _CIDADE.sub(lambda _m: local, texto)
        texto = _DATA.sub(lambda _m: data_por_extenso(data_documento or agora_sao_paulo().date()), texto)
    return texto.strip()


def montar_corpo_peticao(
    corpo: str,
    *,
    orgao: str,
    numero_certame: str,
    objeto: str,
    razao_social: Optional[str] = None,
    licitante: Optional[Dict[str, Any]] = None,
) -> str:
    """Corpo do PDF: tags e marcadores preenchidos (local e data vêm na assinatura do PDF)."""
    dados = dict(licitante or {})
    if razao_social and not dados.get("razao_social"):
        dados["razao_social"] = razao_social
    return preencher_peca(corpo, licitante=dados, orgao=orgao, numero_certame=numero_certame, objeto=objeto,
                          remover_fecho=True)


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
    empresa: Optional[Dict[str, str]] = None,
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
        corpo, orgao=orgao_txt, numero_certame=numero_txt, objeto=objeto_txt, razao_social=razao or None,
        licitante=lic,
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
        empresa=empresa,
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
    "preencher_peca",
    "normalizar_lei",
    "renderizar_pdf_timbrado",
    "slug_arquivo",
    "tabela_ficha",
]
