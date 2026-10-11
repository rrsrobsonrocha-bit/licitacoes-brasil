"""
Itens dos certames com alta disponibilidade (Licitações Brasil).

- ``get_or_sync_tender_items``: banco local (instantâneo) → PNCP (timeout de 6 s) → Compras.gov.br
  (Dados Abertos), com gravação em lote do que vier de fonte oficial. As camadas de rede ficam em
  ``pncp_compras.sincronizar_itens``; aqui está a entrada única para telas e rotinas.
- ``pre_carregar_itens_estrategicos``: fila de fundo do agendador que baixa antes os itens dos
  certames que o operador provavelmente vai abrir (sessão em 72 h, favoritos, AL e dispensas).
"""
import asyncio
import os
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import exists, or_
from sqlalchemy.orm import Session

from server.config import settings
from server.database import SessionLocal
from server.models import Favorite, Tender, TenderItem
from server.search_engine import agora_sao_paulo_naive, filtro_vigentes
from server.services import pncp_compras

UF_PRIORITARIA = os.getenv("AUTO_SYNC_UF_PRIORITARIA", "AL").strip().upper()
HORAS_SESSAO_PROXIMA = 72
ITENS_POR_LOTE = 10
PAUSA_ENTRE_LOTES_SEGUNDOS = float(os.getenv("AUTO_SYNC_PAUSA_LOTE_SEGUNDOS", "3"))


class CertameNaoEncontrado(LookupError):
    pass


async def get_or_sync_tender_items(tender_id: str, db: Optional[Session] = None, forcar: bool = False,
                                   interativo: bool = True) -> Dict[str, Any]:
    """
    Itens do certame (por id interno ou numeroControlePNCP). Com itens oficiais já gravados,
    responde sem rede; senão tenta PNCP e, se ele falhar, o Compras.gov.br. Nunca levanta erro de
    rede: falha das fontes volta como ``status`` = "indisponivel" com a lista local (talvez vazia).
    """
    proprio = db is None
    db = db or SessionLocal()
    inicio = time.perf_counter()
    try:
        tender = db.query(Tender).filter((Tender.id == tender_id) | (Tender.external_id == tender_id)).first()
        if not tender:
            raise CertameNaoEncontrado("Licitação não encontrada.")
        resultado = await pncp_compras.sincronizar_itens(db, tender, forcar=forcar, interativo=interativo)
        return {**resultado, "tender_id": tender.id, "tempo_ms": round((time.perf_counter() - inicio) * 1000, 1)}
    finally:
        if proprio:
            db.close()


def _sem_itens_oficiais():
    return ~exists().where(TenderItem.tender_id == Tender.id, TenderItem.origem.in_(pncp_compras.ORIGENS_OFICIAIS))


def certames_estrategicos(db: Session, limite: int, agora: Optional[datetime] = None) -> List[str]:
    """
    Vigentes ainda sem itens oficiais gravados, em ordem de prioridade: sessão/encerramento nas
    próximas 72 h, favoritos de qualquer conta, estado prioritário (AL) e dispensas.
    """
    agora = agora or agora_sao_paulo_naive()
    limite_sessao = agora + timedelta(hours=HORAS_SESSAO_PROXIMA)
    base = db.query(Tender.id).filter(filtro_vigentes(), _sem_itens_oficiais())
    grupos = [
        base.filter(or_(Tender.proposal_deadline.between(agora, limite_sessao),
                        Tender.opening_date.between(agora, limite_sessao))).order_by(Tender.proposal_deadline.asc()),
        base.filter(exists().where(Favorite.tender_id == Tender.id)).order_by(Tender.proposal_deadline.asc()),
        base.filter(Tender.state == UF_PRIORITARIA).order_by(Tender.proposal_deadline.asc()),
        base.filter(Tender.modality.ilike("%dispensa%")).order_by(Tender.proposal_deadline.asc()),
    ]
    ids: List[str] = []
    vistos = set()
    for consulta in grupos:
        for (tid,) in consulta.limit(limite):
            if tid not in vistos:
                vistos.add(tid)
                ids.append(tid)
                if len(ids) >= limite:
                    return ids
    return ids


def pre_carregar_itens_estrategicos(limite: Optional[int] = None, parar: Optional[Callable[[], bool]] = None,
                                    dormir: Callable[[float], None] = time.sleep) -> Dict[str, int]:
    """
    Baixa e grava os itens dos certames estratégicos (com fallback do Compras.gov.br), no ritmo de
    PNCP_ITENS_RATE_LIMIT_PER_MINUTE e com uma pausa extra a cada ITENS_POR_LOTE certames.
    """
    limite = settings.AUTO_SYNC_ESTRATEGICOS_MAX if limite is None else limite
    taxa = settings.PNCP_ITENS_RATE_LIMIT_PER_MINUTE
    pausa = 60.0 / taxa if taxa > 0 else 0.0
    resumo = {"fila": 0, "sincronizados": 0, "via_compras": 0, "sem_itens": 0, "pendentes": 0}
    db = SessionLocal()
    try:
        ids = certames_estrategicos(db, max(0, limite))
        resumo["fila"] = len(ids)
        for indice, tender_id in enumerate(ids):
            if parar and parar():
                resumo["pendentes"] = len(ids) - indice
                break
            try:
                # Timeouts curtos e disjuntores: com PNCP e Compras fora do ar a fila passa rápido
                r = asyncio.run(get_or_sync_tender_items(tender_id, db=db, interativo=True))
            except Exception as exc:  # um certame com falha não interrompe a fila
                db.rollback()
                print(f"[AUTO-SYNC] Itens estratégicos de {tender_id}: falha ({exc!r}).")
                r = {"status": "erro", "total": 0}
            if r.get("status") == "sincronizado":
                resumo["sincronizados"] += 1
                resumo["via_compras"] += r.get("origem") == "COMPRAS"
            else:
                resumo["sem_itens"] += 1
            if indice < len(ids) - 1:
                if pausa:
                    dormir(pausa)
                if PAUSA_ENTRE_LOTES_SEGUNDOS and (indice + 1) % ITENS_POR_LOTE == 0:
                    dormir(PAUSA_ENTRE_LOTES_SEGUNDOS)
    finally:
        db.close()
    return resumo
