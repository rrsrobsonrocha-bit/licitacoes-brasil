"""
Sandbox de disputa (somente ADMIN): 4 salas de teste com concorrentes virtuais para ensaiar o Cockpit e o
Robô de Lances Desktop de ponta a ponta.

Isolamento (exceção controlada à regra "sem dados fictícios"):
- As salas existem só na memória do servidor, na conta do ADMIN que as criou, marcadas ``simulado=True``
  e identificadas como sim-01 ... sim-04. Nada vai para o banco (nem histórico, nem admin_bids).
- O worker-config devolve ``portal: "simulacao"``: o robô desktop usa o adaptador de simulação nessas
  salas mesmo que o config.json aponte para um portal real.

Os concorrentes ("Empresa Alfa", "Beta Distribuidora") dão lances a cada poucos segundos até o próprio
limite de preço; a sala sim-03 tem um concorrente que desce abaixo do piso (teste de Stop Loss) e a
sim-04 encerra com prorrogação rápida (cada lance na reta final adia o fim).
"""
import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from server.services.dispute_monitor import PregaoItem, dispute_monitor, get_sp_now

logger = logging.getLogger(__name__)

CONCORRENTES = ("Empresa Alfa", "Beta Distribuidora")
TICK_SEGUNDOS = 1.0

CENARIOS: List[Dict[str, Any]] = [
    {"id": "sim-01", "descricao": "Lote Notebooks", "perfil": "Concorrência moderada",
     "referencia": 120000.0, "piso": 92000.0, "degrau": 100.0, "intervalo": (8, 15), "saltos": (1, 3),
     "limites": {"Empresa Alfa": 95000.0, "Beta Distribuidora": 97500.0}},
    {"id": "sim-02", "descricao": "Lote Suprimentos", "perfil": "Concorrência rápida (3-5 s)",
     "referencia": 45000.0, "piso": 34500.0, "degrau": 50.0, "intervalo": (3, 5), "saltos": (1, 2),
     "limites": {"Empresa Alfa": 35500.0, "Beta Distribuidora": 36200.0}},
    {"id": "sim-03", "descricao": "Lote Mobiliário", "perfil": "Concorrente agressivo abaixo do piso (teste de Stop Loss)",
     "referencia": 30000.0, "piso": 26000.0, "degrau": 50.0, "intervalo": (5, 10), "saltos": (2, 6),
     "limites": {"Empresa Alfa": 24000.0, "Beta Distribuidora": 27500.0}},
    {"id": "sim-04", "descricao": "Lote Serviços", "perfil": "Prorrogação rápida (fecha em 3 min; +30 s por lance)",
     "referencia": 80000.0, "piso": 60000.0, "degrau": 100.0, "intervalo": (5, 15), "saltos": (1, 3),
     "limites": {"Empresa Alfa": 63000.0, "Beta Distribuidora": 65000.0},
     "duracao": 180.0, "prorrogacao": 30.0},
]


@dataclass
class Sala:
    cenario: Dict[str, Any]
    owner_id: str
    item_key: str
    proximo_lance_em: float
    encerra_em: Optional[float] = None
    lances: Dict[str, float] = field(default_factory=dict)  # último lance de cada concorrente virtual
    lider: Optional[str] = None
    ultimo_meu_lance: float = 0.0


_salas: Dict[Tuple[str, str], Sala] = {}
_motor: Optional[asyncio.Task] = None


def _agendar(sala: Sala, agora: float, rng) -> None:
    minimo, maximo = sala.cenario["intervalo"]
    sala.proximo_lance_em = agora + rng.uniform(minimo, maximo)


def criar_cenarios(owner_id: str, agora: Optional[float] = None, rng=random) -> List[dict]:
    """Cria (ou recria do zero) as 4 salas na conta ``owner_id``."""
    agora = time.monotonic() if agora is None else agora
    for chave in [c for c in _salas if c[0] == owner_id]:
        _salas.pop(chave)
    for item in [it for it in dispute_monitor._itens_do_dono(owner_id) if it.simulado]:
        dispute_monitor.remove_item(item.item_key, owner_id=owner_id)

    criados = []
    for cenario in CENARIOS:
        item = dispute_monitor.track_item(PregaoItem(
            id_pregao=cenario["id"], numero_item=1,
            descricao=f"{cenario['descricao']} - SIMULAÇÃO ({cenario['perfil']})",
            valor_referencia=cenario["referencia"], melhor_lance=cenario["referencia"], meu_lance=0.0,
            fase="ABERTO", degrau_minimo=cenario["degrau"], degrau_origem="manual",
            valor_minimo_limite=cenario["piso"], status_automacao="Aguardando", fonte_portal="SIMULAÇÃO",
            simulado=True,
        ), owner_id=owner_id)
        sala = Sala(cenario=cenario, owner_id=owner_id, item_key=item.item_key, proximo_lance_em=agora)
        _agendar(sala, agora, rng)
        if cenario.get("duracao"):
            sala.encerra_em = agora + cenario["duracao"]
        _salas[(owner_id, cenario["id"])] = sala
        criados.append(item.to_dict())
    return criados


def _ranking(sala: Sala) -> List[dict]:
    ordem = sorted(sala.lances.items(), key=lambda par: par[1])
    return [{"fornecedor": nome, "valor": valor, "posicao": i} for i, (nome, valor) in enumerate(ordem, start=1)]


def _lance_do_concorrente(sala: Sala, item: PregaoItem, rng) -> Optional[Tuple[str, float]]:
    """Um concorrente (que não seja o líder atual) cobre o melhor lance sem passar do próprio limite."""
    degrau = sala.cenario["degrau"]
    candidatos = []
    for nome, limite in sala.cenario["limites"].items():
        if nome == sala.lider:
            continue
        alvo = round(item.melhor_lance - degrau * rng.randint(*sala.cenario["saltos"]), 2)
        if alvo < limite:
            alvo = round(item.melhor_lance - degrau, 2)  # no limite: cobre só pelo degrau mínimo
        if alvo >= limite and alvo > 0:
            candidatos.append((nome, alvo))
    return rng.choice(candidatos) if candidatos else None


def _atualizar_fase(sala: Sala, item: PregaoItem, agora: float) -> bool:
    """Prorrogação (sim-04): reta final vira IMINÊNCIA; sem lances até o fim, a sala encerra. True = encerrada."""
    if sala.encerra_em is None:
        return False
    if agora >= sala.encerra_em:
        item.fase = "ENCERRADO"
        item.auto_lance_ativo = False
        item.status_automacao = "Disputa encerrada (simulação)"
        dispute_monitor.broadcast_sync({"type": "ITEM_CONFIGURED", "item": item.to_dict(),
                                        "timestamp": get_sp_now().isoformat()})
        return True
    if sala.encerra_em - agora <= sala.cenario["prorrogacao"] and item.fase != "IMINENCIA":
        item.fase = "IMINENCIA"
        dispute_monitor.broadcast_sync({"type": "IMMINENCE_ALERT", "item": item.to_dict(),
                                        "timestamp": get_sp_now().isoformat()})
    return False


def _prorrogar(sala: Sala, agora: float) -> None:
    if sala.encerra_em is not None and sala.encerra_em - agora <= sala.cenario["prorrogacao"]:
        sala.encerra_em = agora + sala.cenario["prorrogacao"]


def passo(agora: Optional[float] = None, rng=random) -> int:
    """Uma volta do motor: fases, prorrogação e lances dos concorrentes que chegaram na hora. Devolve quantos lances."""
    agora = time.monotonic() if agora is None else agora
    lances = 0
    for chave, sala in list(_salas.items()):
        item = dispute_monitor._find_item(sala.item_key, owner_id=sala.owner_id)
        if item is None or not item.simulado:  # sala removida no cockpit
            _salas.pop(chave, None)
            continue
        if item.fase == "ENCERRADO":
            continue
        # Lance do operador/robô na reta final também prorroga
        if item.meu_lance and item.meu_lance != sala.ultimo_meu_lance:
            sala.ultimo_meu_lance = item.meu_lance
            sala.lider = "voce" if item.meu_lance <= item.melhor_lance else sala.lider
            _prorrogar(sala, agora)
        if _atualizar_fase(sala, item, agora) or agora < sala.proximo_lance_em:
            continue
        _agendar(sala, agora, rng)
        escolha = _lance_do_concorrente(sala, item, rng)
        if escolha is None:
            continue
        nome, valor = escolha
        sala.lances[nome] = valor
        sala.lider = nome
        item.ranking = _ranking(sala)
        _prorrogar(sala, agora)
        dispute_monitor.registrar_melhor_lance(sala.item_key, valor, owner_id=sala.owner_id, autor=nome)
        lances += 1
    return lances


async def _laco_motor():
    while True:
        await asyncio.sleep(TICK_SEGUNDOS)
        try:
            passo()
        except Exception as exc:  # o motor do sandbox nunca derruba o servidor
            logger.warning("Sandbox de disputa: falha no passo de simulação: %s", exc)


def iniciar_motor() -> None:
    """Liga o motor dos concorrentes virtuais no event loop do servidor (ocioso enquanto não há salas)."""
    global _motor
    if _motor is not None and not _motor.done():
        return
    try:
        _motor = asyncio.get_running_loop().create_task(_laco_motor())
    except RuntimeError:
        logger.warning("Sandbox de disputa: sem event loop ativo; concorrentes virtuais parados.")


def motor_ativo() -> bool:
    return _motor is not None and not _motor.done()
