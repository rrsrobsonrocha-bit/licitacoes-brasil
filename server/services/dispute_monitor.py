import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    SP_TZ = ZoneInfo("America/Sao_Paulo")
except Exception:
    SP_TZ = timezone(timedelta(hours=-3), name="America/Sao_Paulo")


def get_sp_now() -> datetime:
    return datetime.now(SP_TZ)


DEGRAU_PADRAO = 0.01


def normalizar_degrau(valor: Any) -> Optional[float]:
    """Converte o degrau informado em float positivo com 2 casas (None se ausente ou inválido)."""
    try:
        degrau = float(valor)
    except (TypeError, ValueError):
        return None
    if degrau != degrau or degrau <= 0:  # NaN ou não positivo
        return None
    return max(DEGRAU_PADRAO, round(degrau, 2))


def _chave_interna(owner_id: Optional[str], item_key: str) -> str:
    return f"{owner_id or ''}|{item_key}"


@dataclass
class PregaoItem:
    id_pregao: str
    numero_item: int
    descricao: str
    valor_referencia: float
    melhor_lance: float = 0.0
    meu_lance: float = 0.0
    fase: str = "ABERTO"  # "AGUARDANDO", "ABERTO", "IMINENCIA", "ENCERRADO"
    degrau_minimo: float = DEGRAU_PADRAO
    degrau_origem: str = "padrao"  # "edital" (informado pela API do PNCP/Compras.gov), "manual" ou "padrao"
    valor_minimo_limite: float = 0.0
    posicao_alvo: int = 1
    auto_lance_ativo: bool = False
    status_automacao: str = "Aguardando"
    fonte_portal: str = "PNCP"
    historico_concorrentes: List[dict] = field(default_factory=list)
    updated_at: datetime = field(default_factory=get_sp_now)
    owner_id: Optional[str] = None  # conta dona do lote (multitenancy); cada usuário vê só os seus

    @property
    def item_key(self) -> str:
        return f"{self.id_pregao}:{self.numero_item}"

    @property
    def storage_key(self) -> str:
        return _chave_interna(self.owner_id, self.item_key)

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
            "degrau_origem": self.degrau_origem,
            "valor_minimo_limite": float(self.valor_minimo_limite),
            "posicao_alvo": int(self.posicao_alvo),
            "auto_lance_ativo": bool(self.auto_lance_ativo),
            "status_automacao": self.status_automacao,
            "fonte_portal": self.fonte_portal,
            "posicao_atual": posicao_atual,
            "historico_concorrentes": list(self.historico_concorrentes),
            "is_losing": self.is_losing,
            "owner_id": self.owner_id,
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
        # fila do assinante SSE -> conta dona (eventos de lotes só chegam ao dono)
        self._subscribers: Dict[asyncio.Queue, Optional[str]] = {}
        self._running: bool = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._global_autobid_enabled: Dict[Optional[str], bool] = {}

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
                # Heartbeat leve: cada assinante recebe apenas os números da própria conta
                for q, owner_id in list(self._subscribers.items()):
                    self._entregar(q, {
                        "type": "HEARTBEAT",
                        "total_items": len(self._itens_do_dono(owner_id)),
                        "global_autobid": self._global_autobid_enabled.get(owner_id, False),
                        "timestamp": get_sp_now().isoformat()
                    })
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Erro no ciclo do DisputeMonitorService: {e}")
                await asyncio.sleep(5)

    def _itens_do_dono(self, owner_id: Optional[str]) -> List[PregaoItem]:
        return [it for it in self._items.values() if it.owner_id == owner_id]

    def track_item(self, item_data: Any, owner_id: Optional[str] = None) -> PregaoItem:
        """Adiciona ou atualiza certame/lote na lista de monitoramento ativo da conta ``owner_id``."""
        if isinstance(item_data, PregaoItem):
            item = item_data
            item.owner_id = owner_id if owner_id is not None else item.owner_id
        elif isinstance(item_data, dict):
            id_p = str(item_data.get("id_pregao", "")).strip()
            num_it = int(item_data.get("numero_item", 1))
            desc = str(item_data.get("descricao", "")).strip()
            val_ref = float(item_data.get("valor_referencia", 0.0))
            best_bid = float(item_data.get("melhor_lance", val_ref))
            my_bid = float(item_data.get("meu_lance", best_bid))
            phase = str(item_data.get("fase", "ABERTO")).upper().strip()

            degrau = normalizar_degrau(item_data.get("degrau_minimo"))
            degrau_origem = str(item_data.get("degrau_origem") or ("manual" if degrau else "padrao"))
            if degrau is None:
                degrau, degrau_origem = DEGRAU_PADRAO, "padrao"
            val_piso = float(item_data.get("valor_minimo_limite", round(val_ref * 0.70, 2) if val_ref > 0 else 0.0))
            pos_alvo = int(item_data.get("posicao_alvo", 1))
            auto_bid = bool(item_data.get("auto_lance_ativo", False))
            status_auto = str(item_data.get("status_automacao", "Robô Ativo" if auto_bid else "Aguardando"))
            fonte = str(item_data.get("fonte_portal", "PNCP")).strip()

            # Somente lances reais informados; nenhum concorrente é inventado
            hist = list(item_data.get("historico_concorrentes", []))

            item = PregaoItem(
                id_pregao=id_p,
                numero_item=num_it,
                descricao=desc,
                valor_referencia=val_ref,
                melhor_lance=best_bid,
                meu_lance=my_bid,
                fase=phase,
                degrau_minimo=degrau,
                degrau_origem=degrau_origem,
                valor_minimo_limite=val_piso,
                posicao_alvo=pos_alvo,
                auto_lance_ativo=auto_bid,
                status_automacao=status_auto,
                fonte_portal=fonte,
                historico_concorrentes=hist,
                updated_at=get_sp_now(),
                owner_id=owner_id
            )
        else:
            raise ValueError("Parâmetro item_data deve ser instância de PregaoItem ou dict válido.")

        self._items[item.storage_key] = item

        event = {
            "type": "ITEM_TRACKED",
            "item": item.to_dict(),
            "timestamp": get_sp_now().isoformat()
        }
        self.broadcast_sync(event)
        return item

    def get_active_snapshot(self, owner_id: Optional[str] = None) -> List[dict]:
        """Snapshot dos lotes em acompanhamento da conta ``owner_id``."""
        return [it.to_dict() for it in self._itens_do_dono(owner_id)]

    def get_item(self, id_pregao: str, numero_item: int, owner_id: Optional[str] = None) -> Optional[PregaoItem]:
        return self._items.get(_chave_interna(owner_id, f"{id_pregao}:{numero_item}"))

    def _find_item(self, item_id: str, owner_id: Optional[str] = None) -> Optional[PregaoItem]:
        """Localiza um lote da conta ``owner_id`` (nunca devolve lotes de outra conta)."""
        exato = self._items.get(_chave_interna(owner_id, item_id))
        if exato:
            return exato
        for it in self._itens_do_dono(owner_id):
            if str(it.numero_item) == str(item_id) or it.id_pregao == item_id or it.item_key == item_id:
                return it
        return None

    def remove_item(self, item_id: str, owner_id: Optional[str] = None) -> bool:
        """Remove um lote da conta ``owner_id``."""
        item = self._find_item(item_id, owner_id)
        if not item:
            return False
        self._items.pop(item.storage_key, None)
        self.broadcast_sync({
            "type": "ITEM_REMOVED",
            "item": item.to_dict(),
            "timestamp": get_sp_now().isoformat()
        })
        return True

    def configure_item(
        self,
        item_id: str,
        degrau_minimo: Optional[float] = None,
        valor_minimo_limite: Optional[float] = None,
        posicao_alvo: Optional[int] = None,
        auto_lance_ativo: Optional[bool] = None,
        owner_id: Optional[str] = None
    ) -> Optional[PregaoItem]:
        """Persiste parâmetros individuais de automação e margem para o lote especificado."""
        item = self._find_item(item_id, owner_id)
        if not item:
            return None

        if degrau_minimo is not None:
            novo_degrau = normalizar_degrau(degrau_minimo) or DEGRAU_PADRAO
            if novo_degrau != item.degrau_minimo:
                item.degrau_minimo = novo_degrau
                item.degrau_origem = "manual"
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
            self.executar_lance_automatico(item.item_key, owner_id)

        event = {
            "type": "ITEM_CONFIGURED",
            "item": item.to_dict(),
            "timestamp": get_sp_now().isoformat()
        }
        self.broadcast_sync(event)
        return item

    def executar_lance_automatico(self, item_id: str, owner_id: Optional[str] = None) -> Optional[dict]:
        """
        Executa cálculo e cobertura de lances com estrita preservação de margem e stop-loss:
        - Verifica melhor_lance concorrente atualizado em tempo real.
        - Se posicao_alvo == 1: novo_lance = melhor_lance - degrau_minimo.
        - Validação do piso: se novo_lance >= valor_minimo_limite e auto_lance_ativo is True:
          meu_lance = round(novo_lance, 2).
        - Caso o cálculo ultrapasse o piso para baixo: suspende novos lances do robô
          e emite estado de 'Piso Atingido / Margem Esgotada'.
        """
        item = self._find_item(item_id, owner_id)
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

    def toggle_all_autobid(self, status: bool, owner_id: Optional[str] = None) -> List[dict]:
        """Liga ou desliga todos os interruptores de automação da conta ``owner_id``."""
        ligado = bool(status)
        self._global_autobid_enabled[owner_id] = ligado
        updated = []
        for it in self._itens_do_dono(owner_id):
            it.auto_lance_ativo = ligado
            if it.auto_lance_ativo:
                it.status_automacao = "Robô Ativo"
                # Se estiver perdendo, dispara tentativa de lance
                if it.is_losing:
                    self.executar_lance_automatico(it.item_key, owner_id)
            else:
                it.status_automacao = "Desativado"
            it.updated_at = get_sp_now()
            updated.append(it.to_dict())

        event = {
            "type": "GLOBAL_AUTOBID_TOGGLED",
            "global_status": ligado,
            "total_items": len(updated),
            "owner_id": owner_id,
            "timestamp": get_sp_now().isoformat()
        }
        self.broadcast_sync(event)
        return updated

    def registrar_melhor_lance(self, item_id: str, valor: Any, owner_id: Optional[str] = None) -> Optional[dict]:
        """
        Registra o melhor lance atual da disputa, informado pelo usuário a partir do portal
        (Compras.gov.br, BLL, BNC...). Atualiza o histórico e, se o robô do lote estiver
        ativo e o usuário passar a perder, tenta cobrir respeitando degrau e piso.
        """
        item = self._find_item(item_id, owner_id)
        if not item:
            return None
        try:
            novo = round(float(valor), 2)
        except (TypeError, ValueError):
            raise ValueError("Informe um valor numérico para o melhor lance.")
        if novo <= 0:
            raise ValueError("O melhor lance deve ser maior que zero.")

        item.melhor_lance = novo
        item.updated_at = get_sp_now()
        item.historico_concorrentes.insert(0, {
            "posicao": 1,
            "valor": novo,
            "timestamp": get_sp_now().isoformat(),
            "autor": "Melhor lance informado"
        })
        item.historico_concorrentes = item.historico_concorrentes[:20]

        self.broadcast_sync({
            "type": "BID_UPDATE",
            "item": item.to_dict(),
            "is_losing": item.is_losing,
            "timestamp": get_sp_now().isoformat()
        })
        if item.auto_lance_ativo and item.is_losing:
            self.executar_lance_automatico(item.item_key, owner_id)
        return item.to_dict()

    def subscribe(self, owner_id: Optional[str] = None) -> asyncio.Queue:
        """Cria e registra uma fila assíncrona dedicada para um assinante SSE da conta ``owner_id``."""
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers[q] = owner_id
        return q

    def unsubscribe(self, q: asyncio.Queue):
        """Remove fila de assinante desconectado."""
        self._subscribers.pop(q, None)

    def _entregar(self, q: asyncio.Queue, event: dict) -> bool:
        try:
            q.put_nowait(event)
            return True
        except Exception:
            self._subscribers.pop(q, None)
            return False

    @staticmethod
    def _dono_do_evento(event: dict) -> Optional[str]:
        if "owner_id" in event:
            return event.get("owner_id")
        item = event.get("item")
        return item.get("owner_id") if isinstance(item, dict) else None

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

        # Cada evento só chega aos assinantes da conta dona do lote
        dono = self._dono_do_evento(event)
        for q, owner_id in list(self._subscribers.items()):
            if owner_id == dono:
                self._entregar(q, event)

    def _persist_dispute_history(
        self,
        id_pregao: str,
        numero_item: int,
        tipo_evento: str,
        valor: float,
        posicao: int = 1,
        autor: str = "Robô Licitações Brasil",
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
