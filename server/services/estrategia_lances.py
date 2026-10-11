"""
Estratégia de lances da Mesa do Operador (Sala de Disputa) — regra da menor perda possível.

Pregão de menor preço: vence o menor lance. Para ficar à frente de um concorrente basta ofertar o
valor dele menos um degrau; o sistema nunca joga o lance direto no piso (preço mínimo do
operador), o que queimaria margem sem necessidade.

- ``lider``: cobre o 1º colocado (alvo = menor lance concorrente).
- ``melhor_viavel``: se cobrir o 1º exigir um valor abaixo do piso, desce o ranking (2º ao 10º) e
  escolhe a melhor colocação cujo lance de cobertura fica no piso ou acima dele.

Valores em Decimal com 2 casas; o degrau percentual é arredondado para cima (ao centavo), para
que o decremento nunca fique abaixo do percentual configurado.
"""
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional

CENTAVO = Decimal("0.01")
MAX_POSICOES = 10
ESTRATEGIAS = ("lider", "melhor_viavel")
TIPOS_DEGRAU = ("valor", "percentual")
DEGRAUS_RAPIDOS_VALOR = ("0.01", "0.05", "0.10", "1.00")
DEGRAUS_RAPIDOS_PERCENTUAL = ("0.1", "0.5", "1")


class EstrategiaInvalidaError(ValueError):
    """Parâmetro de estratégia inválido (valor negativo, estratégia ou degrau desconhecido)."""


def _texto_decimal(valor: Any) -> str:
    """Aceita 1234.56, "1234,56" e "1.234,56" / "R$ 1.234,56" (formato brasileiro)."""
    texto = str(valor).strip().replace("R$", "").replace(" ", "")
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    return texto


def dinheiro(valor: Any, campo: str = "valor") -> Decimal:
    try:
        numero = valor if isinstance(valor, Decimal) else Decimal(_texto_decimal(valor))
    except (InvalidOperation, ValueError):
        raise EstrategiaInvalidaError(f"Valor inválido em '{campo}'.")
    if not numero.is_finite():
        raise EstrategiaInvalidaError(f"Valor inválido em '{campo}'.")
    return numero.quantize(CENTAVO, rounding=ROUND_HALF_UP)


def valor_do_degrau(alvo: Decimal, tipo: str, degrau: Decimal, degrau_minimo_edital: Optional[Decimal] = None) -> Decimal:
    """Decremento em R$ aplicado sobre o lance alvo (nunca menor que o degrau mínimo do edital)."""
    if tipo == "percentual":
        valor = (alvo * degrau / Decimal(100)).quantize(CENTAVO, rounding=ROUND_CEILING)
    else:
        valor = degrau.quantize(CENTAVO, rounding=ROUND_CEILING)
    piso_do_degrau = max(CENTAVO, degrau_minimo_edital or CENTAVO)
    return max(valor, piso_do_degrau)


def _concorrentes_ordenados(valores: Iterable[Any]) -> List[Decimal]:
    lista = sorted(dinheiro(v, "ranking") for v in valores if v is not None)
    if any(v <= 0 for v in lista):
        raise EstrategiaInvalidaError("Os lances do ranking devem ser maiores que zero.")
    return lista[:MAX_POSICOES]


def calcular_lance(
    concorrentes: Iterable[Any],
    piso: Any,
    *,
    estrategia: str = "lider",
    tipo_degrau: str = "valor",
    degrau: Any = "0.01",
    meu_ultimo_lance: Any = None,
    degrau_minimo_edital: Any = None,
) -> Dict[str, Any]:
    """
    Lance de menor perda para a estratégia. ``concorrentes`` são os lances dos outros fornecedores
    (Top 10, sem o nosso). Devolve ``acao``: "lance" (novo valor), "manter" (nosso último lance já
    garante a colocação, não há por que baixar) ou "inviavel" (nenhuma colocação acima do piso).
    """
    if estrategia not in ESTRATEGIAS:
        raise EstrategiaInvalidaError("Estratégia inválida: use 'lider' ou 'melhor_viavel'.")
    if tipo_degrau not in TIPOS_DEGRAU:
        raise EstrategiaInvalidaError("Degrau inválido: use 'valor' (R$) ou 'percentual' (%).")
    piso_d = dinheiro(piso, "piso")
    if piso_d <= 0:
        raise EstrategiaInvalidaError("Informe o preço mínimo (piso) para calcular o lance.")
    try:
        degrau_d = Decimal(_texto_decimal(degrau))
    except (InvalidOperation, ValueError):
        raise EstrategiaInvalidaError("Degrau inválido.")
    if not degrau_d.is_finite() or degrau_d <= 0:
        raise EstrategiaInvalidaError("O degrau deve ser maior que zero.")
    edital = dinheiro(degrau_minimo_edital, "degrau do edital") if degrau_minimo_edital else None
    meu = dinheiro(meu_ultimo_lance, "meu último lance") if meu_ultimo_lance not in (None, "", 0) else None
    lista = _concorrentes_ordenados(concorrentes)

    if not lista:
        return {"acao": "inviavel", "valor": None, "posicao": None, "alvo": None,
                "motivo": "Informe o ranking da disputa (lances dos concorrentes) para calcular a cobertura."}

    alvos = range(len(lista)) if estrategia == "melhor_viavel" else range(1)
    tentativas: List[Dict[str, Any]] = []
    for indice in alvos:
        alvo = lista[indice]
        posicao = indice + 1  # cobrindo o concorrente nesta posição, ocupamos o lugar dele
        if meu is not None and meu < alvo:
            # Já estamos à frente deste concorrente: baixar o lance seria perder margem à toa
            return {"acao": "manter", "valor": float(meu), "posicao": posicao, "alvo": float(alvo),
                    "degrau": 0.0, "margem_sobre_piso": float(meu - piso_d), "tentativas": tentativas,
                    "motivo": f"Seu último lance já garante o {posicao}º lugar: mantenha, sem queimar margem."}
        passo = valor_do_degrau(alvo, tipo_degrau, degrau_d, edital)
        lance = (alvo - passo).quantize(CENTAVO)
        tentativas.append({"posicao": posicao, "alvo": float(alvo), "lance": float(lance), "viavel": lance >= piso_d})
        if lance >= piso_d:
            motivo = (f"Cobre o 1º colocado por {passo} (R$)." if posicao == 1 else
                      f"1º lugar abaixo do piso; melhor colocação viável: {posicao}º, cobrindo R$ {alvo} por R$ {passo}.")
            return {"acao": "lance", "valor": float(lance), "posicao": posicao, "alvo": float(alvo),
                    "degrau": float(passo), "margem_sobre_piso": float(lance - piso_d),
                    "tentativas": tentativas, "motivo": motivo}

    if estrategia == "lider":
        motivo = (f"Cobrir o 1º colocado exigiria R$ {tentativas[0]['lance']:.2f}, abaixo do seu piso "
                  f"(R$ {piso_d}). Use 'Melhor Colocação Viável' ou revise o piso.")
    else:
        motivo = f"Nenhuma colocação do 1º ao {len(lista)}º fica acima do seu piso (R$ {piso_d})."
    return {"acao": "inviavel", "valor": None, "posicao": None, "alvo": None, "tentativas": tentativas, "motivo": motivo}


def posicao_do_lance(valor: Any, concorrentes: Iterable[Any]) -> int:
    """Colocação de um lance frente aos concorrentes (menor preço vence; empate fica atrás de quem já ofertou)."""
    v = dinheiro(valor)
    return 1 + sum(1 for c in _concorrentes_ordenados(concorrentes) if c <= v)
