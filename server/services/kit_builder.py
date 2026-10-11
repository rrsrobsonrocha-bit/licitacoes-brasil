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
