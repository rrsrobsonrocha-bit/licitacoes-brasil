import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Set

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    SP_TZ = ZoneInfo("America/Sao_Paulo")
except Exception:
    SP_TZ = timezone(timedelta(hours=-3), name="America/Sao_Paulo")


def get_sp_now() -> datetime:
    return datetime.now(SP_TZ)


@dataclass
class PregaoItem:
    id_pregao: str
    numero_item: int
    descricao: str
    valor_referencia: float
    melhor_lance: float = 0.0
    meu_lance: float = 0.0
    fase: str = "ABERTO"  # "AGUARDANDO", "ABERTO", "IMINENCIA", "ENCERRADO"
    updated_at: datetime = field(default_factory=get_sp_now)

    @property
    def item_key(self) -> str:
        return f"{self.id_pregao}:{self.numero_item}"

    @property
    def is_losing(self) -> bool:
        return bool(self.meu_lance > 0 and self.melhor_lance > 0 and self.meu_lance > self.melhor_lance)

    def to_dict(self) -> dict:
        return {
            "id_pregao": self.id_pregao,
            "numero_item": self.numero_item,
            "item_key": self.item_key,
            "descricao": self.descricao,
            "valor_referencia": float(self.valor_referencia),
            "melhor_lance": float(self.melhor_lance),
            "meu_lance": float(self.meu_lance),
            "fase": self.fase.upper(),
            "is_losing": self.is_losing,
            "updated_at": self.updated_at.isoformat() if isinstance(self.updated_at, datetime) else str(self.updated_at)
        }


class DisputeMonitorService:
    """
    Serviço assíncrono de alta performance para monitoramento de multi-pregão
    em tempo real (estilo SIGA / ComprasNet). Opera com filas assíncronas
    e eventos push SSE sem bloqueio de I/O.
    """

    def __init__(self):
        self._items: Dict[str, PregaoItem] = {}
        self._event_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subscribers: Set[asyncio.Queue] = set()
        self._running: bool = False
        self._monitor_task: Optional[asyncio.Task] = None

    async def start(self):
        if self._running:
            return
        self._running = True
        try:
            loop = asyncio.get_running_loop()
            self._monitor_task = loop.create_task(self._background_loop())
            logger.info("DisputeMonitorService iniciado com sucesso.")
        except RuntimeError:
            logger.warning("DisputeMonitorService: nenhum event loop ativo detectado no momento do start.")

    async def stop(self):
        self._running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("DisputeMonitorService finalizado.")

    async def _background_loop(self):
        """Loop sem bloqueio para heartbeat e verificação de integridade."""
        while self._running:
            try:
                await asyncio.sleep(20)
                # Heartbeat leve para assinantes conectados
                ping_event = {
                    "type": "HEARTBEAT",
                    "total_items": len(self._items),
                    "timestamp": get_sp_now().isoformat()
                }
                self.broadcast_sync(ping_event)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Erro no ciclo do DisputeMonitorService: {e}")
                await asyncio.sleep(5)

    def track_item(self, item_data: Any) -> PregaoItem:
        """Adiciona ou atualiza certame/lote na lista de monitoramento ativo."""
        if isinstance(item_data, PregaoItem):
            item = item_data
        elif isinstance(item_data, dict):
            id_p = str(item_data.get("id_pregao", "")).strip()
            num_it = int(item_data.get("numero_item", 1))
            desc = str(item_data.get("descricao", "")).strip()
            val_ref = float(item_data.get("valor_referencia", 0.0))
            best_bid = float(item_data.get("melhor_lance", val_ref))
            my_bid = float(item_data.get("meu_lance", best_bid))
            phase = str(item_data.get("fase", "ABERTO")).upper().strip()

            item = PregaoItem(
                id_pregao=id_p,
                numero_item=num_it,
                descricao=desc,
                valor_referencia=val_ref,
                melhor_lance=best_bid,
                meu_lance=my_bid,
                fase=phase,
                updated_at=get_sp_now()
            )
        else:
            raise ValueError("Parâmetro item_data deve ser instância de PregaoItem ou dict válido.")

        self._items[item.item_key] = item

        event = {
            "type": "ITEM_TRACKED",
            "item": item.to_dict(),
            "timestamp": get_sp_now().isoformat()
        }
        self.broadcast_sync(event)
        return item

    def get_active_snapshot(self) -> List[dict]:
        """Devolve o snapshot consolidado de todos os lotes e pregões em acompanhamento."""
        return [it.to_dict() for it in self._items.values()]

    def get_item(self, id_pregao: str, numero_item: int) -> Optional[PregaoItem]:
        return self._items.get(f"{id_pregao}:{numero_item}")

    def simulate_pulse(self, item_key: Optional[str] = None) -> List[dict]:
        """
        Método de contingência/mock para certames de teste.
        Gera atualizações automáticas de lances e transição para fase de 'IMINENCIA'.
        """
        updated = []
        targets = [self._items[item_key]] if (item_key and item_key in self._items) else list(self._items.values())

        for it in targets:
            if it.fase == "ENCERRADO":
                continue

            base = it.melhor_lance if it.melhor_lance > 0 else it.valor_referencia
            decrement = max(10.0, round(base * 0.015, 2))
            novo_melhor_lance = max(50.0, round(base - decrement, 2))

            it.melhor_lance = novo_melhor_lance
            it.updated_at = get_sp_now()

            # Transição para IMINENCIA se ainda estiver ABERTO
            if it.fase == "ABERTO":
                it.fase = "IMINENCIA"
                event_type = "IMMINENCE_ALERT"
            else:
                event_type = "BID_UPDATE"

            event = {
                "type": event_type,
                "item": it.to_dict(),
                "delta": decrement,
                "is_losing": it.is_losing,
                "timestamp": get_sp_now().isoformat()
            }
            self.broadcast_sync(event)
            updated.append(it.to_dict())

        return updated

    def subscribe(self) -> asyncio.Queue:
        """Cria e registra uma fila assíncrona dedicada para um assinante SSE."""
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        """Remove fila de assinante desconectado."""
        self._subscribers.discard(q)

    def broadcast_sync(self, event: dict):
        """Envia evento para a fila interna e para todos os ouvintes conectados."""
        try:
            self._event_queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self._event_queue.get_nowait()
                self._event_queue.put_nowait(event)
            except Exception:
                pass

        dead = set()
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.add(q)
            except Exception:
                dead.add(q)

        for d in dead:
            self._subscribers.discard(d)


# Instância singleton global do monitor
dispute_monitor = DisputeMonitorService()
