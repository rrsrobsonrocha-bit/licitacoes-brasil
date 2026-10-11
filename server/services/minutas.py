"""
Minutas padronizadas de declarações e proposta (Lei nº 14.133/2021), entregues em DOCX editável.

O arquivo é montado aqui mesmo (WordprocessingML mínimo, só com a biblioteca padrão) e abre no
Word, LibreOffice e Google Docs. Os campos a preencher aparecem entre colchetes e realçados em
amarelo. Com um certame informado, órgão, modalidade, número e itens oficiais já vêm preenchidos;
nenhum dado da empresa é inventado.
"""
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence
from xml.sax.saxutils import escape

MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Bloco de identificação do licitante, comum às declarações
QUALIFICACAO = (
    "A empresa [RAZÃO SOCIAL], inscrita no CNPJ sob o nº [CNPJ], com sede em [ENDEREÇO COMPLETO], "
    "por intermédio de seu representante legal, o(a) Sr.(a) [NOME DO REPRESENTANTE LEGAL], portador(a) "
    "do documento de identidade nº [RG] e do CPF nº [CPF],"
)


@dataclass(frozen=True)
class Minuta:
    slug: str
    titulo: str
    descricao: str
    fundamentacao: str
    paragrafos: Sequence[str] = ()


MINUTAS: Dict[str, Minuta] = {
    m.slug: m
    for m in [
        Minuta(
            slug="enquadramento-me-epp",
            titulo="Declaração de Enquadramento como ME/EPP",
            descricao="Comprova o porte para usufruir do tratamento favorecido (preferência, empate ficto, itens exclusivos e cota reservada).",
            fundamentacao="LC nº 123/2006, arts. 3º e 42 a 49; Lei nº 14.133/2021, art. 4º, § 2º",
            paragrafos=(
                f"{QUALIFICACAO} DECLARA, sob as penas da lei, para fins de participação no certame acima referenciado, que:",
                "a) está enquadrada como [MICROEMPRESA / EMPRESA DE PEQUENO PORTE], nos termos do art. 3º da Lei "
                "Complementar nº 123, de 14 de dezembro de 2006;",
                "b) não se enquadra em nenhuma das vedações do § 4º do art. 3º da Lei Complementar nº 123/2006, estando "
                "apta a usufruir do tratamento favorecido estabelecido em seus arts. 42 a 49;",
                "c) no ano-calendário de realização desta licitação, ainda não celebrou contratos com a Administração "
                "Pública cujos valores somados extrapolem a receita bruta máxima admitida para fins de enquadramento "
                "como empresa de pequeno porte, nos termos do § 2º do art. 4º da Lei nº 14.133, de 1º de abril de 2021;",
                "d) compromete-se a comunicar imediatamente qualquer alteração que implique o seu desenquadramento.",
                "Declara, ainda, estar ciente de que a falsidade desta declaração sujeita o declarante às sanções do "
                "art. 299 do Código Penal e do art. 155, inciso VIII, da Lei nº 14.133/2021, sem prejuízo das demais "
                "cominações legais.",
            ),
        ),
        Minuta(
            slug="inexistencia-fato-impeditivo",
            titulo="Declaração de Inexistência de Fato Impeditivo",
            descricao="Atesta que não há sanção, vínculo ou condenação que impeça a participação e a contratação.",
            fundamentacao="Lei nº 14.133/2021, arts. 14 e 156, incisos III e IV",
            paragrafos=(
                f"{QUALIFICACAO} DECLARA, sob as penas da lei, que até a presente data inexistem fatos impeditivos "
                "para a sua habilitação e participação no certame acima referenciado, ciente da obrigatoriedade de "
                "declarar ocorrências posteriores, e, em especial, que:",
                "a) não foi declarada inidônea para licitar ou contratar com a Administração Pública, nem está impedida "
                "de licitar e contratar no âmbito do ente federativo promotor desta licitação (art. 156, incisos III e "
                "IV, da Lei nº 14.133/2021);",
                "b) não incorre em nenhuma das vedações de participação previstas no art. 14 da Lei nº 14.133/2021;",
                "c) não mantém vínculo de natureza técnica, comercial, econômica, financeira, trabalhista ou civil com "
                "dirigente do órgão ou entidade contratante ou com agente público que desempenhe função na licitação ou "
                "atue na fiscalização ou na gestão do contrato, nem é cônjuge, companheiro ou parente deles em linha "
                "reta, colateral ou por afinidade, até o terceiro grau (art. 14, inciso IV, da Lei nº 14.133/2021);",
                "d) não foi condenada judicialmente, com trânsito em julgado, nos 5 (cinco) anos anteriores à divulgação "
                "do edital, por exploração de trabalho infantil, por submissão de trabalhadores a condições análogas às "
                "de escravo ou por contratação de adolescentes nos casos vedados pela legislação trabalhista (art. 14, "
                "inciso VI, da Lei nº 14.133/2021).",
            ),
        ),
        Minuta(
            slug="cumprimento-requisitos-habilitacao",
            titulo="Declaração de Cumprimento dos Requisitos de Habilitação",
            descricao="Declaração exigida no envio da proposta, com as declarações trabalhistas que costumam acompanhá-la.",
            fundamentacao="Lei nº 14.133/2021, arts. 63, incisos I, II e IV, e § 1º, e 68, inciso VI; CF, art. 7º, XXXIII",
            paragrafos=(
                f"{QUALIFICACAO} DECLARA, sob as penas da lei, para os fins do art. 63, inciso I, da Lei nº "
                "14.133/2021, que atende plenamente aos requisitos de habilitação exigidos no edital do certame acima "
                "referenciado, respondendo pela veracidade das informações prestadas, na forma da lei. Declara, ainda, que:",
                "a) não emprega menor de 18 (dezoito) anos em trabalho noturno, perigoso ou insalubre, nem menor de 16 "
                "(dezesseis) anos em qualquer trabalho, salvo na condição de aprendiz, a partir de 14 (quatorze) anos, "
                "nos termos do art. 7º, inciso XXXIII, da Constituição Federal e do art. 68, inciso VI, da Lei nº 14.133/2021;",
                "b) cumpre as exigências de reserva de cargos para pessoa com deficiência e para reabilitado da "
                "Previdência Social previstas em lei e em outras normas específicas (art. 63, inciso IV, da Lei nº 14.133/2021);",
                "c) a sua proposta econômica compreende a integralidade dos custos para atendimento dos direitos "
                "trabalhistas assegurados na Constituição Federal, nas leis trabalhistas, nas normas infralegais, nas "
                "convenções coletivas de trabalho e nos termos de ajustamento de conduta vigentes na data de entrega das "
                "propostas (art. 63, § 1º, da Lei nº 14.133/2021);",
                "d) apresentará, quando convocada, os documentos comprobatórios de habilitação no prazo fixado no edital "
                "(art. 63, inciso II, da Lei nº 14.133/2021).",
            ),
        ),
        Minuta(
            slug="proposta-comercial-readequada",
            titulo="Modelo de Proposta Comercial Readequada",
            descricao="Proposta ajustada ao lance final ou negociado, com planilha de itens, validade e declarações de custos.",
            fundamentacao="Lei nº 14.133/2021, arts. 59 e 63, § 1º",
        ),
    ]
}


# ------------------------------------------------------------------------------
# Contexto do certame (opcional)
# ------------------------------------------------------------------------------
def contexto_do_certame(tender: Any = None, itens: Sequence[Any] = ()) -> Dict[str, Any]:
    if tender is None:
        return {"orgao": None, "referencia": None, "processo": None, "objeto": None, "itens": []}
    numero = tender.notice_number if tender.notice_number and tender.notice_number != "Não informado" else None
    referencia = " nº ".join(p for p in (tender.modality, numero) if p) or None
    return {
        "orgao": tender.contracting_entity,
        "referencia": referencia,
        "processo": tender.process_number if tender.process_number and tender.process_number != "Não informado" else None,
        "objeto": tender.title,
        "itens": [
            {"numero": i.item_number, "descricao": i.description, "unidade": i.unit, "quantidade": i.quantity}
            for i in itens
        ],
    }


def nome_do_arquivo(minuta: Minuta, ctx: Dict[str, Any]) -> str:
    base = minuta.slug
    if ctx.get("referencia"):
        ref = unicodedata.normalize("NFKD", ctx["referencia"]).encode("ascii", "ignore").decode("ascii")
        base += "_" + re.sub(r"[^A-Za-z0-9]+", "-", ref).strip("-").lower()[:60]
    return f"{base}.docx"


# ------------------------------------------------------------------------------
# WordprocessingML
# ------------------------------------------------------------------------------
_PLACEHOLDER = re.compile(r"(\[[^\]]+\])")


def _runs(texto: str, negrito: bool = False, tamanho: int = 22) -> str:
    """Texto em runs; trechos entre colchetes ficam realçados em amarelo para preenchimento."""
    partes = []
    for trecho in _PLACEHOLDER.split(texto):
        if not trecho:
            continue
        # Ordem dos elementos exigida pelo esquema do Word: rFonts, b, sz, highlight
        props = '<w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/>'
        if negrito:
            props += "<w:b/>"
        props += f'<w:sz w:val="{tamanho}"/>'
        if _PLACEHOLDER.fullmatch(trecho):
            props += '<w:highlight w:val="yellow"/>'
        partes.append(f'<w:r><w:rPr>{props}</w:rPr><w:t xml:space="preserve">{escape(trecho)}</w:t></w:r>')
    return "".join(partes)


def _p(texto: str = "", negrito: bool = False, alinhamento: str = "both", tamanho: int = 22, depois: int = 160) -> str:
    return (
        f'<w:p><w:pPr><w:spacing w:after="{depois}" w:line="300" w:lineRule="auto"/><w:jc w:val="{alinhamento}"/></w:pPr>'
        f"{_runs(texto, negrito, tamanho)}</w:p>"
    )


def _tabela(cabecalho: List[str], linhas: List[List[str]], larguras: List[int]) -> str:
    borda = '<w:{0} w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
    bordas = "".join(borda.format(lado) for lado in ("top", "left", "bottom", "right", "insideH", "insideV"))
    grade = "".join(f'<w:gridCol w:w="{w}"/>' for w in larguras)

    def celula(texto: str, largura: int, negrito: bool = False) -> str:
        sombra = '<w:shd w:val="clear" w:color="auto" w:fill="E7EEF7"/>' if negrito else ""
        return (
            f'<w:tc><w:tcPr><w:tcW w:w="{largura}" w:type="dxa"/>{sombra}</w:tcPr>'
            f'<w:p><w:pPr><w:spacing w:after="0"/></w:pPr>{_runs(texto, negrito, 18)}</w:p></w:tc>'
        )

    corpo = "<w:tr>" + "".join(celula(c, larguras[i], True) for i, c in enumerate(cabecalho)) + "</w:tr>"
    for linha in linhas:
        corpo += "<w:tr>" + "".join(celula(c, larguras[i]) for i, c in enumerate(linha)) + "</w:tr>"
    return (
        f'<w:tbl><w:tblPr><w:tblW w:w="{sum(larguras)}" w:type="dxa"/><w:tblBorders>{bordas}</w:tblBorders>'
        f'<w:tblLayout w:type="fixed"/></w:tblPr><w:tblGrid>{grade}</w:tblGrid>{corpo}</w:tbl>'
    )


def _numero(valor: Optional[float]) -> str:
    if valor is None:
        return "[QTD.]"
    texto = f"{valor:,.4f}".rstrip("0").rstrip(".")
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


def _cabecalho(minuta: Minuta, ctx: Dict[str, Any]) -> List[str]:
    blocos = [
        _p(f"Ao(À) {ctx['orgao'] or '[ÓRGÃO OU ENTIDADE CONTRATANTE]'}", negrito=True, alinhamento="left", depois=40),
        _p(f"Ref.: {ctx['referencia'] or '[MODALIDADE] nº [NÚMERO/ANO]'}", alinhamento="left", depois=40),
    ]
    if ctx.get("processo"):
        blocos.append(_p(f"Processo nº {ctx['processo']}", alinhamento="left", depois=40))
    if ctx.get("objeto"):
        blocos.append(_p(f"Objeto: {ctx['objeto']}", alinhamento="left", depois=40))
    blocos.append(_p("", depois=120))
    blocos.append(_p(minuta.titulo.upper(), negrito=True, alinhamento="center", tamanho=26, depois=240))
    return blocos


def _assinatura() -> List[str]:
    return [
        _p("", depois=120),
        _p("[CIDADE/UF], [DIA] de [MÊS] de [ANO].", alinhamento="right", depois=600),
        _p("______________________________________________", alinhamento="center", depois=40),
        _p("[NOME DO REPRESENTANTE LEGAL]", negrito=True, alinhamento="center", depois=40),
        _p("[CARGO] – CPF nº [CPF]", alinhamento="center", depois=40),
        _p("[RAZÃO SOCIAL] – CNPJ nº [CNPJ]", alinhamento="center", depois=40),
    ]


def _corpo_proposta(ctx: Dict[str, Any]) -> List[str]:
    blocos = [
        _p("1. DADOS DO LICITANTE", negrito=True, alinhamento="left"),
        _p("Razão social: [RAZÃO SOCIAL] – CNPJ nº [CNPJ]", alinhamento="left", depois=40),
        _p("Endereço: [ENDEREÇO COMPLETO] – Telefone: [TELEFONE] – E-mail: [E-MAIL]", alinhamento="left", depois=40),
        _p("Representante legal: [NOME DO REPRESENTANTE LEGAL] – CPF nº [CPF] – Cargo: [CARGO]", alinhamento="left", depois=40),
        _p("Dados bancários: Banco [BANCO] – Agência [AGÊNCIA] – Conta [CONTA]", alinhamento="left"),
        _p("2. PROPOSTA DE PREÇOS (VALORES READEQUADOS AO LANCE FINAL)", negrito=True, alinhamento="left"),
    ]
    itens = ctx.get("itens") or []
    linhas = [
        [str(i["numero"]), i["descricao"] or "[DESCRIÇÃO]", "[MARCA/MODELO]", i["unidade"] or "[UNID.]",
         _numero(i["quantidade"]), "[VALOR UNIT.]", "[VALOR TOTAL]"]
        for i in itens
    ] or [[str(n), "[DESCRIÇÃO DO ITEM]", "[MARCA/MODELO]", "[UNID.]", "[QTD.]", "[VALOR UNIT.]", "[VALOR TOTAL]"] for n in (1, 2, 3)]
    blocos.append(_tabela(
        ["Item", "Descrição", "Marca/Modelo", "Unid.", "Qtd.", "Valor unitário (R$)", "Valor total (R$)"],
        linhas, [600, 3000, 1300, 700, 800, 1300, 1300],
    ))
    blocos += [
        _p("", depois=80),
        _p("VALOR GLOBAL DA PROPOSTA: R$ [VALOR GLOBAL] ([VALOR GLOBAL POR EXTENSO]).", negrito=True, alinhamento="left"),
        _p("3. CONDIÇÕES DA PROPOSTA", negrito=True, alinhamento="left"),
        _p("a) Validade da proposta: [60 (SESSENTA)] dias, contados da data de sua apresentação, ou o prazo fixado no edital, se maior;"),
        _p("b) Prazo de entrega/execução: [PRAZO], contado do recebimento da nota de empenho ou da ordem de serviço, conforme o Termo de Referência;"),
        _p("c) Local de entrega/execução: conforme o edital e o Termo de Referência;"),
        _p("d) os valores unitários e totais acima foram readequados ao valor final obtido na fase de lances ou de "
           "negociação, observados os critérios de aceitabilidade de preços do edital;"),
        _p("e) nos preços propostos estão incluídos todos os custos diretos e indiretos, tributos, encargos sociais, "
           "trabalhistas, previdenciários, fiscais e comerciais, taxas, fretes, seguros e demais despesas incidentes "
           "sobre o objeto;"),
        _p("f) a proposta compreende a integralidade dos custos para atendimento dos direitos trabalhistas assegurados "
           "na Constituição Federal, nas leis trabalhistas, nas normas infralegais, nas convenções coletivas de trabalho "
           "e nos termos de ajustamento de conduta vigentes na data de entrega das propostas (art. 63, § 1º, da Lei nº 14.133/2021)."),
    ]
    return blocos


def gerar_docx(minuta: Minuta, ctx: Optional[Dict[str, Any]] = None) -> bytes:
    ctx = ctx or contexto_do_certame()
    blocos = _cabecalho(minuta, ctx)
    if minuta.slug == "proposta-comercial-readequada":
        blocos += _corpo_proposta(ctx)
    else:
        blocos += [_p(texto) for texto in minuta.paragrafos]
    blocos += _assinatura()
    blocos.append(_p(f"Fundamentação: {minuta.fundamentacao}. Modelo de referência: confira as exigências do edital antes de assinar.",
                     alinhamento="left", tamanho=16, depois=0))

    documento = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(blocos)}"
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1418" w:right="1134" w:bottom="1134" w:left="1418" w:header="709" w:footer="709" w:gutter="0"/>'
        "</w:sectPr></w:body></w:document>"
    )
    tipos = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        "</Types>"
    )
    relacoes = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        "</Relationships>"
    )
    propriedades = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{escape(minuta.titulo)}</dc:title><dc:creator>Licitações Brasil</dc:creator>"
        "</cp:coreProperties>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", tipos)
        zf.writestr("_rels/.rels", relacoes)
        zf.writestr("docProps/core.xml", propriedades)
        zf.writestr("word/document.xml", documento)
    return buffer.getvalue()


def listar() -> List[Dict[str, str]]:
    return [
        {
            "slug": m.slug,
            "titulo": m.titulo,
            "descricao": m.descricao,
            "fundamentacao": m.fundamentacao,
            "download_url": f"/api/minutas/{m.slug}/download",
        }
        for m in MINUTAS.values()
    ]
