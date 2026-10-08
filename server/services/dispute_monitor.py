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
    degrau_minimo: float = 0.01
    valor_minimo_limite: float = 0.0
    posicao_alvo: int = 1
    auto_lance_ativo: bool = False
    status_automacao: str = "Aguardando"
    fonte_portal: str = "PNCP"
    historico_concorrentes: List[dict] = field(default_factory=list)
    updated_at: datetime = field(default_factory=get_sp_now)

    @property
    def item_key(self) -> str:
        return f"{self.id_pregao}:{self.numero_item}"

    @property
    def is_losing(self) -> bool:
        return bool(self.meu_lance > 0 and self.melhor_lance > 0 and self.meu_lance > self.melhor_lance)

    def to_dict(self) -> dict:
        posicao_atual = 1 if (self.meu_lance > 0 and (self.melhor_lance == 0 or self.meu_lance <= self.melhor_lance)) else 2
        return {
            "id_pregao": self.id_pregao,
            "numero_item": self.numero_item,
            "item_key": self.item_key,
            "descricao": self.descricao,
            "valor_referencia": float(self.valor_referencia),
            "melhor_lance": float(self.melhor_lance),
            "meu_lance": float(self.meu_lance),
            "fase": self.fase.upper(),
            "degrau_minimo": float(self.degrau_minimo),
            "valor_minimo_limite": float(self.valor_minimo_limite),
            "posicao_alvo": int(self.posicao_alvo),
            "auto_lance_ativo": bool(self.auto_lance_ativo),
            "status_automacao": self.status_automacao,
            "fonte_portal": self.fonte_portal,
            "posicao_atual": posicao_atual,
            "historico_concorrentes": list(self.historico_concorrentes),
            "is_losing": self.is_losing,
            "updated_at": self.updated_at.isoformat() if isinstance(self.updated_at, datetime) else str(self.updated_at)
        }


class DisputeMonitorService:
    """
    Serviço assíncrono de alta performance para monitoramento de multi-pregão
    em tempo real (estilo SIGA / ComprasNet). Opera com filas assíncronas,
    motor de robô de lances com decremento mínimo e stop-loss, e eventos push SSE.
    """

    def __init__(self):
        self._items: Dict[str, PregaoItem] = {}
        self._event_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subscribers: Set[asyncio.Queue] = set()
        self._running: bool = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._global_autobid_enabled: bool = False

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
                    "global_autobid": self._global_autobid_enabled,
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

            degrau = float(item_data.get("degrau_minimo", 0.01))
            val_piso = float(item_data.get("valor_minimo_limite", round(val_ref * 0.70, 2) if val_ref > 0 else 0.0))
            pos_alvo = int(item_data.get("posicao_alvo", 1))
            auto_bid = bool(item_data.get("auto_lance_ativo", False))
            status_auto = str(item_data.get("status_automacao", "Robô Ativo" if auto_bid else "Aguardando"))
            fonte = str(item_data.get("fonte_portal", "PNCP")).strip()

            hist = list(item_data.get("historico_concorrentes", []))
            if not hist and best_bid > 0:
                hist = [
                    {"posicao": 1, "valor": best_bid, "timestamp": get_sp_now().isoformat(), "autor": "Melhor Oferta"},
                    {"posicao": 2, "valor": round(best_bid * 1.015, 2), "timestamp": get_sp_now().isoformat(), "autor": "Concorrente #2"}
                ]

            item = PregaoItem(
                id_pregao=id_p,
                numero_item=num_it,
                descricao=desc,
                valor_referencia=val_ref,
                melhor_lance=best_bid,
                meu_lance=my_bid,
                fase=phase,
                degrau_minimo=degrau,
                valor_minimo_limite=val_piso,
                posicao_alvo=pos_alvo,
                auto_lance_ativo=auto_bid,
                status_automacao=status_auto,
                fonte_portal=fonte,
                historico_concorrentes=hist,
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

    def _find_item(self, item_id: str) -> Optional[PregaoItem]:
        if item_id in self._items:
            return self._items[item_id]
        for key, it in self._items.items():
            if str(it.numero_item) == str(item_id) or it.id_pregao == item_id or key == item_id:
                return it
        return None

    def configure_item(
        self,
        item_id: str,
        degrau_minimo: Optional[float] = None,
        valor_minimo_limite: Optional[float] = None,
        posicao_alvo: Optional[int] = None,
        auto_lance_ativo: Optional[bool] = None
    ) -> Optional[PregaoItem]:
        """Persiste parâmetros individuais de automação e margem para o lote especificado."""
        item = self._find_item(item_id)
        if not item:
            return None

        if degrau_minimo is not None:
            item.degrau_minimo = max(0.01, round(float(degrau_minimo), 2))
        if valor_minimo_limite is not None:
            item.valor_minimo_limite = round(float(valor_minimo_limite), 2)
        if posicao_alvo is not None:
            item.posicao_alvo = max(1, int(posicao_alvo))
        if auto_lance_ativo is not None:
            item.auto_lance_ativo = bool(auto_lance_ativo)
            item.status_automacao = "Robô Ativo" if item.auto_lance_ativo else "Desativado"

        item.updated_at = get_sp_now()

        # Se foi ativado e está perdendo, tenta lance automático imediatamente
        if item.auto_lance_ativo and item.is_losing:
            self.executar_lance_automatico(item.item_key)

        event = {
            "type": "ITEM_CONFIGURED",
            "item": item.to_dict(),
            "timestamp": get_sp_now().isoformat()
        }
        self.broadcast_sync(event)
        return item

    def executar_lance_automatico(self, item_id: str) -> Optional[dict]:
        """
        Executa cálculo e cobertura de lances com estrita preservação de margem e stop-loss:
        - Verifica melhor_lance concorrente atualizado em tempo real.
        - Se posicao_alvo == 1: novo_lance = melhor_lance - degrau_minimo.
        - Validação do piso: se novo_lance >= valor_minimo_limite e auto_lance_ativo is True:
          meu_lance = round(novo_lance, 2).
        - Caso o cálculo ultrapasse o piso para baixo: suspende novos lances do robô
          e emite estado de 'Piso Atingido / Margem Esgotada'.
        """
        item = self._find_item(item_id)
        if not item:
            return None

        if item.fase == "ENCERRADO":
            return item.to_dict()

        if not item.auto_lance_ativo:
            return item.to_dict()

        base_lance = item.melhor_lance if item.melhor_lance > 0 else item.valor_referencia

        if item.posicao_alvo == 1:
            novo_lance = base_lance - item.degrau_minimo
        elif item.posicao_alvo == 2:
            novo_lance = base_lance + item.degrau_minimo
        else:
            novo_lance = base_lance + (item.degrau_minimo * (item.posicao_alvo - 1))

        novo_lance = round(novo_lance, 2)

        if novo_lance >= item.valor_minimo_limite:
            item.meu_lance = novo_lance
            if item.posicao_alvo == 1:
                item.melhor_lance = novo_lance
            item.status_automacao = "Robô Liderando (1º)"
            item.updated_at = get_sp_now()

            # Registra no histórico de lances
            item.historico_concorrentes.insert(0, {
                "posicao": item.posicao_alvo,
                "valor": item.meu_lance,
                "timestamp": get_sp_now().isoformat(),
                "autor": "Robô Automático (Você)"
            })
            if len(item.historico_concorrentes) > 20:
                item.historico_concorrentes = item.historico_concorrentes[:20]

            event = {
                "type": "AUTO_BID_PLACED",
                "item": item.to_dict(),
                "lance": item.meu_lance,
                "timestamp": get_sp_now().isoformat()
            }
            self.broadcast_sync(event)
            self._persist_dispute_history(
                id_pregao=item.id_pregao,
                numero_item=item.numero_item,
                tipo_evento="AUTO_BID",
                valor=item.meu_lance,
                posicao=item.posicao_alvo,
                autor="Robô Automático (Você)",
                detalhes={"melhor_lance": item.melhor_lance, "fonte_portal": item.fonte_portal}
            )
        else:
            # Piso ultrapassado: desativa robô para evitar prejuízo
            item.auto_lance_ativo = False
            item.status_automacao = "Piso Atingido / Margem Esgotada"
            item.updated_at = get_sp_now()

            event = {
                "type": "STOP_LOSS_REACHED",
                "item": item.to_dict(),
                "motivo": "Piso Atingido / Margem Esgotada",
                "valor_tentativa": novo_lance,
                "piso": item.valor_minimo_limite,
                "timestamp": get_sp_now().isoformat()
            }
            self.broadcast_sync(event)
            self._persist_dispute_history(
                id_pregao=item.id_pregao,
                numero_item=item.numero_item,
                tipo_evento="STOP_LOSS",
                valor=novo_lance,
                posicao=item.posicao_alvo,
                autor="Robô Automático (Você)",
                detalhes={"piso": item.valor_minimo_limite, "fonte_portal": item.fonte_portal}
            )

        return item.to_dict()

    def toggle_all_autobid(self, status: bool) -> List[dict]:
        """Liga ou desliga todos os interruptores de automação em simultâneo."""
        self._global_autobid_enabled = bool(status)
        updated = []
        for it in self._items.values():
            it.auto_lance_ativo = self._global_autobid_enabled
            if it.auto_lance_ativo:
                it.status_automacao = "Robô Ativo"
                # Se estiver perdendo, dispara tentativa de lance
                if it.is_losing:
                    self.executar_lance_automatico(it.item_key)
            else:
                it.status_automacao = "Desativado"
            it.updated_at = get_sp_now()
            updated.append(it.to_dict())

        event = {
            "type": "GLOBAL_AUTOBID_TOGGLED",
            "global_status": self._global_autobid_enabled,
            "total_items": len(updated),
            "timestamp": get_sp_now().isoformat()
        }
        self.broadcast_sync(event)
        return updated

    def simulate_pulse(self, item_key: Optional[str] = None) -> List[dict]:
        """
        Método de contingência/mock para certames de teste.
        Gera atualizações automáticas de lances e aciona o robô se ativo.
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

            # Adiciona ao histórico do concorrente
            it.historico_concorrentes.insert(0, {
                "posicao": 1,
                "valor": novo_melhor_lance,
                "timestamp": get_sp_now().isoformat(),
                "autor": "Concorrente Externo"
            })
            if len(it.historico_concorrentes) > 20:
                it.historico_concorrentes = it.historico_concorrentes[:20]

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

            # Se robô do item estiver ativo, tenta cobrir automaticamente
            if it.auto_lance_ativo:
                self.executar_lance_automatico(it.item_key)

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

    def _persist_dispute_history(
        self,
        id_pregao: str,
        numero_item: int,
        tipo_evento: str,
        valor: float,
        posicao: int = 1,
        autor: str = "Robô LicitaçõesBrasil",
        detalhes: Optional[dict] = None
    ):
        """Grava telemetria e auditoria de lances no banco SQLite/Postgres de forma resiliente."""
        try:
            from server.database import SessionLocal
            from server.models import HistoricoDisputa
            db = SessionLocal()
            try:
                rec = HistoricoDisputa(
                    id_pregao=id_pregao,
                    numero_item=numero_item,
                    tipo_evento=tipo_evento,
                    valor_ofertado=round(float(valor), 2),
                    posicao=int(posicao),
                    autor=autor,
                    detalhes=detalhes or {}
                )
                db.add(rec)
                db.commit()
            finally:
                db.close()
        except Exception as err:
            logger.debug(f"Telemetria de auditoria de disputa ignorada: {err}")


# Instância singleton global do monitor
dispute_monitor = DisputeMonitorService()
