"""
Índices contábeis de qualificação econômico-financeira (Lei nº 14.133/2021, art. 69).

- LG = (Ativo Circulante + Realizável a Longo Prazo) / (Passivo Circulante + Passivo Não Circulante)
- SG = Ativo Total / (Passivo Circulante + Passivo Não Circulante)
- LC = Ativo Circulante / Passivo Circulante

Os índices são truncados em 2 casas (0,999 fica 0,99, nunca arredondado para 1,00), critério conservador
para a comparação com o mínimo de 1,00. O memorial em DOCX sai no papel timbrado da empresa ativa, para
conferência e assinatura do contador; o balanço que vale no certame é o registrado na Junta / SPED.
"""
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from server.services.minutas import _p, _tabela, empacotar_docx, preencher_empresa

CENTAVO = Decimal("0.01")
MINIMO = Decimal("1.00")
CAMPOS = (
    ("ativo_circulante", "Ativo Circulante (AC)"),
    ("realizavel_longo_prazo", "Ativo Realizável a Longo Prazo (ARLP)"),
    ("ativo_total", "Ativo Total (AT)"),
    ("passivo_circulante", "Passivo Circulante (PC)"),
    ("passivo_nao_circulante", "Passivo Não Circulante (PNC)"),
    ("patrimonio_liquido", "Patrimônio Líquido (PL)"),
)
ORIENTACOES = {
    "LG": "Liquidez Geral abaixo de 1,00: as dívidas totais superam os bens e direitos realizáveis. O edital pode "
          "admitir, em substituição, capital mínimo ou patrimônio líquido mínimo de até 10% do valor estimado "
          "(art. 69, § 4º, da Lei 14.133/2021); revise também a classificação das contas com o contador.",
    "SG": "Solvência Geral abaixo de 1,00: o ativo total não cobre o passivo exigível (passivo a descoberto). "
          "Confira a escrituração e verifique se o edital aceita patrimônio líquido mínimo como alternativa.",
    "LC": "Liquidez Corrente abaixo de 1,00: as obrigações de curto prazo superam o ativo circulante. Avalie com o "
          "contador a reclassificação de dívidas de longo prazo e a capitalização da empresa.",
}
AVISO_INSTITUCIONAL = (
    "Este memorial é instrumento de conferência dos índices. Para fins de habilitação, vale o balanço patrimonial "
    "e as demonstrações contábeis dos 2 (dois) últimos exercícios sociais, apresentados na forma da lei e "
    "registrados na Junta Comercial ou transmitidos pelo SPED (art. 69, inciso I, da Lei nº 14.133/2021)."
)


class ValoresInvalidosError(ValueError):
    pass


def _valor(bruto: Any, campo: str, permite_negativo: bool = False) -> Decimal:
    texto = str(bruto if bruto is not None else "").strip().replace("R$", "").replace(" ", "")
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    if texto == "":
        raise ValoresInvalidosError(f"Informe o valor de {dict(CAMPOS)[campo]}.")
    try:
        numero = Decimal(texto)
    except InvalidOperation:
        raise ValoresInvalidosError(f"Valor inválido em {dict(CAMPOS)[campo]}.")
    if not numero.is_finite() or (numero < 0 and not permite_negativo):
        raise ValoresInvalidosError(f"Valor inválido em {dict(CAMPOS)[campo]}.")
    return numero.quantize(CENTAVO)


def _indice(numerador: Decimal, denominador: Decimal) -> Optional[Decimal]:
    if denominador <= 0:
        return None
    return (numerador / denominador).quantize(CENTAVO, rounding=ROUND_DOWN)


def _br(valor: Optional[Decimal], casas: int = 2) -> str:
    if valor is None:
        return "—"
    return f"{valor:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def calcular(dados: Dict[str, Any]) -> Dict[str, Any]:
    """Índices LG, SG e LC, diagnóstico (todos >= 1,00) e checagens de consistência do balanço."""
    v = {campo: _valor(dados.get(campo), campo, permite_negativo=campo == "patrimonio_liquido") for campo, _ in CAMPOS}
    ac, arlp, at = v["ativo_circulante"], v["realizavel_longo_prazo"], v["ativo_total"]
    pc, pnc, pl = v["passivo_circulante"], v["passivo_nao_circulante"], v["patrimonio_liquido"]
    exigivel = pc + pnc

    definicoes = [
        ("LG", "Liquidez Geral", "(AC + ARLP) / (PC + PNC)", ac + arlp, exigivel,
         f"({_br(ac)} + {_br(arlp)}) / ({_br(pc)} + {_br(pnc)})"),
        ("SG", "Solvência Geral", "AT / (PC + PNC)", at, exigivel, f"{_br(at)} / ({_br(pc)} + {_br(pnc)})"),
        ("LC", "Liquidez Corrente", "AC / PC", ac, pc, f"{_br(ac)} / {_br(pc)}"),
    ]
    indices: List[Dict[str, Any]] = []
    for sigla, nome, formula, numerador, denominador, calculo in definicoes:
        valor = _indice(numerador, denominador)
        sem_passivo = valor is None
        atende = sem_passivo or valor >= MINIMO
        indices.append({
            "sigla": sigla, "nome": nome, "formula": formula, "calculo": calculo,
            "valor": float(valor) if valor is not None else None,
            "valor_formatado": _br(valor) if valor is not None else "Sem passivo (atende)",
            "atende": atende,
            "orientacao": None if atende else ORIENTACOES[sigla],
        })

    alertas: List[str] = []
    passivo_mais_pl = exigivel + pl
    if at and abs(at - passivo_mais_pl) > max(Decimal("1.00"), at * Decimal("0.005")):
        alertas.append(f"O Ativo Total (R$ {_br(at)}) deveria igualar Passivo + Patrimônio Líquido "
                       f"(R$ {_br(passivo_mais_pl)}): confira os valores com o balanço.")
    if at < ac + arlp:
        alertas.append("O Ativo Total é menor que AC + ARLP: confira os valores (o ativo total inclui ambos).")
    if pl < 0:
        alertas.append("Patrimônio Líquido negativo (passivo a descoberto).")

    qualificado = all(i["atende"] for i in indices)
    return {
        "valores": {campo: float(valor) for campo, valor in v.items()},
        "indices": indices,
        "qualificado": qualificado,
        "diagnostico": "Qualificado para Licitar" if qualificado else "Atenção: índice abaixo de 1,00",
        "alertas": alertas,
    }


def gerar_memorial_docx(dados: Dict[str, Any], empresa: Optional[Dict[str, str]] = None,
                        timbre: Optional[Dict[str, str]] = None) -> bytes:
    """Declaração e Memorial de Cálculo de Índices Contábeis (DOCX, timbrado com a empresa ativa)."""
    resultado = calcular(dados)
    exercicio = str(dados.get("exercicio") or "").strip()[:20] or "[ANO]"
    contador = str(dados.get("contador_nome") or "").strip()[:255] or "[NOME DO CONTADOR]"
    crc = str(dados.get("contador_crc") or "").strip()[:40] or "[UF-000000/O]"

    def texto(t: str) -> str:
        return preencher_empresa(t, empresa)

    blocos = [
        _p("DECLARAÇÃO E MEMORIAL DE CÁLCULO DE ÍNDICES CONTÁBEIS", negrito=True, alinhamento="center", tamanho=26, depois=120),
        _p(f"Exercício social: {exercicio}", alinhamento="center", depois=240),
        _p(texto("A empresa [RAZÃO SOCIAL], inscrita no CNPJ sob o nº [CNPJ], com sede em [ENDEREÇO COMPLETO], por "
                 "intermédio de seu representante legal, o(a) Sr.(a) [NOME DO REPRESENTANTE LEGAL], e de seu "
                 "contador responsável, DECLARA, para fins de qualificação econômico-financeira (art. 69 da Lei nº "
                 f"14.133/2021), que os índices abaixo foram apurados com base no balanço patrimonial do exercício "
                 f"de {exercicio}:")),
        _p("1. VALORES DO BALANÇO PATRIMONIAL", negrito=True, alinhamento="left"),
        _tabela(["Conta", "Valor (R$)"],
                [[rotulo, _br(Decimal(str(resultado["valores"][campo])))] for campo, rotulo in CAMPOS], [6200, 3000]),
        _p("", depois=80),
        _p("2. MEMORIAL DE CÁLCULO", negrito=True, alinhamento="left"),
        _tabela(["Índice", "Fórmula", "Cálculo", "Resultado", "Situação"],
                [[f"{i['nome']} ({i['sigla']})", i["formula"], i["calculo"], i["valor_formatado"],
                  "Atende (≥ 1,00)" if i["atende"] else "Abaixo de 1,00"] for i in resultado["indices"]],
                [1900, 1900, 2900, 1200, 1400]),
        _p("", depois=80),
        _p("3. CONCLUSÃO", negrito=True, alinhamento="left"),
        _p("Todos os índices são iguais ou superiores a 1,00: a empresa atende ao critério usual de qualificação "
           "econômico-financeira." if resultado["qualificado"] else
           "Há índice inferior a 1,00. Verifique se o edital admite a comprovação alternativa de capital mínimo ou "
           "patrimônio líquido mínimo (art. 69, § 4º, da Lei nº 14.133/2021)."),
    ]
    blocos += [_p(f"Observação: {a}", tamanho=18) for a in resultado["alertas"]]
    blocos += [
        _p(AVISO_INSTITUCIONAL, tamanho=18),
        _p("", depois=120),
        _p(texto("[CIDADE/UF], [DIA] de [MÊS] de [ANO]."), alinhamento="right", depois=600),
        _p("______________________________________________", alinhamento="center", depois=40),
        _p(texto("[NOME DO REPRESENTANTE LEGAL]"), negrito=True, alinhamento="center", depois=40),
        _p(texto("[CARGO] – [RAZÃO SOCIAL]"), alinhamento="center", depois=480),
        _p("______________________________________________", alinhamento="center", depois=40),
        _p(contador, negrito=True, alinhamento="center", depois=40),
        _p(f"Contador(a) – CRC nº {crc}", alinhamento="center", depois=40),
        _p("Fórmulas: LG = (AC + ARLP) / (PC + PNC); SG = AT / (PC + PNC); LC = AC / PC. Índices truncados em duas casas decimais.",
           alinhamento="left", tamanho=16, depois=0),
    ]
    return empacotar_docx(blocos, "Declaração e Memorial de Cálculo de Índices Contábeis", timbre)
