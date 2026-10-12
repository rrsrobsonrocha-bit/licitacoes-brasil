"""
Sala de Disputa — Ranking Top 10 e Mesa de Lances do Operador.

- Ranking: qualquer conta registra no próprio lote os lances vistos no portal (monitoramento).
- Mesa de Lances (cálculo de menor perda, registro e histórico de lances): EXCLUSIVA de ADMIN.
  Os lances ficam em ``admin_bids`` como registro de auditoria; o servidor não envia lances ao
  portal de compras — o operador dá o lance no portal e o registra aqui.
- Robô de Lances Desktop (ADMIN): o executável local (scripts/robo_desktop) é quem envia lances ao
  portal; aqui ficam a configuração, o Kill-Switch, o sinal de vida e a auditoria de cada lance.
"""
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from server.database import get_db
from server.models import AdminBid, HistoricoDisputa, User
from server.services import estrategia_lances, simulacao_disputa
from server.services.dispute_monitor import dispute_monitor

router = APIRouter()
MAX_OBSERVACAO = 255


def _usuario(request: Request) -> User:
    usuario = getattr(request.state, "usuario", None)
    if usuario is None or usuario.motivo_bloqueio():
        raise HTTPException(status_code=401, detail="Autenticação requerida.")
    return usuario


def exigir_admin(request: Request) -> User:
    """Trava da Mesa de Lances: só administradores calculam, registram e consultam lances."""
    usuario = _usuario(request)
    if usuario.role != "ADMIN":
        raise HTTPException(status_code=403, detail="A Mesa de Lances é exclusiva de administradores.")
    return usuario


def _chave_lote(tender_id: str, item_id: str) -> str:
    return f"{tender_id}:{item_id}"


def _lote(tender_id: str, item_id: str, user: User) -> Optional[Dict[str, Any]]:
    item = dispute_monitor._find_item(_chave_lote(tender_id, item_id), owner_id=user.id)
    return item.to_dict() if item else None


def _concorrentes(lote: Optional[Dict[str, Any]]):
    return [r["valor"] for r in (lote or {}).get("ranking", [])]


def _status_frente_ao_ranking(valor: Decimal, concorrentes) -> str:
    if not concorrentes:
        return "enviado"
    return "lider" if estrategia_lances.posicao_do_lance(valor, concorrentes) == 1 else "superado"


def serializar_lance(lance: AdminBid) -> Dict[str, Any]:
    criado = lance.created_at
    if criado is not None and criado.tzinfo is None:
        criado = criado.replace(tzinfo=timezone.utc)  # gravado em UTC
    return {
        "id": lance.id,
        "tender_id": lance.tender_id,
        "item_id": lance.item_id,
        "valor_lance": float(lance.valor_lance),
        "preco_minimo_limite": float(lance.preco_minimo_limite) if lance.preco_minimo_limite is not None else None,
        "colocacao_alvo": lance.colocacao_alvo,
        "status": lance.status,
        "observacao": lance.observacao,
        "created_at": criado.isoformat(timespec="seconds") if criado else None,
    }


def _ultimo_lance(db: Session, user_id: str, tender_id: str, item_id: str) -> Optional[AdminBid]:
    return (db.query(AdminBid)
            .filter(AdminBid.user_id == user_id, AdminBid.tender_id == tender_id,
                    AdminBid.item_id == item_id, AdminBid.status != "cancelado")
            .order_by(AdminBid.created_at.desc(), AdminBid.id.desc()).first())


# ------------------------------------------------------------------------------
# Ranking Top 10 (monitoramento, qualquer conta, no próprio lote)
# ------------------------------------------------------------------------------
@router.post("/api/disputes/ranking/{item_id:path}")
def api_definir_ranking(item_id: str, request: Request, payload: Dict[str, Any] = Body(...),
                        db: Session = Depends(get_db)):
    """Registra o ranking (até 10 concorrentes) visto no portal: [{"fornecedor", "valor"}, ...]."""
    user = _usuario(request)
    try:
        lote = dispute_monitor.definir_ranking(item_id, payload.get("ranking"), owner_id=user.id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not lote:
        raise HTTPException(status_code=404, detail="Item de disputa não encontrado.")
    # O último lance do operador passa a líder ou superado conforme o novo ranking
    if user.role == "ADMIN":
        ultimo = _ultimo_lance(db, user.id, lote["id_pregao"], str(lote["numero_item"]))
        if ultimo:
            ultimo.status = _status_frente_ao_ranking(ultimo.valor_lance, _concorrentes(lote))
            db.commit()
    return {"status": "success", "item": lote}


# ------------------------------------------------------------------------------
# Mesa de Lances do Operador (ADMIN)
# ------------------------------------------------------------------------------
@router.post("/api/disputa/{tender_id:path}/items/{item_id}/calcular")
def api_calcular_lance(tender_id: str, item_id: str, request: Request,
                       payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    """'Cobrir com Menor Perda': lance mínimo necessário para a estratégia, nunca abaixo do piso."""
    user = exigir_admin(request)
    lote = _lote(tender_id, item_id, user)
    ultimo = _ultimo_lance(db, user.id, tender_id, item_id)
    concorrentes = payload.get("concorrentes")
    if concorrentes is None:
        concorrentes = _concorrentes(lote)
    meu = payload.get("meu_ultimo_lance")
    if meu is None and ultimo is not None:
        meu = ultimo.valor_lance
    try:
        return estrategia_lances.calcular_lance(
            concorrentes,
            payload.get("preco_minimo_limite"),
            estrategia=payload.get("estrategia") or "lider",
            tipo_degrau=payload.get("tipo_degrau") or "valor",
            degrau=payload.get("degrau") or "0.01",
            meu_ultimo_lance=meu,
            degrau_minimo_edital=(lote or {}).get("degrau_minimo") if (lote or {}).get("degrau_origem") == "edital" else None,
        )
    except estrategia_lances.EstrategiaInvalidaError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/api/disputa/{tender_id:path}/items/{item_id}/lance", status_code=201)
def api_registrar_lance(tender_id: str, item_id: str, request: Request,
                        payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    """Registra o lance do operador; rejeita qualquer valor abaixo do preço mínimo (piso)."""
    user = exigir_admin(request)
    tender_id, item_id = tender_id.strip(), str(item_id).strip()
    if not tender_id or len(tender_id) > 64 or not item_id or len(item_id) > 64:
        raise HTTPException(status_code=422, detail="Identificação do pregão ou do item inválida.")
    try:
        valor = estrategia_lances.dinheiro(payload.get("valor_lance"), "valor_lance")
        if payload.get("preco_minimo_limite") in (None, ""):
            raise estrategia_lances.EstrategiaInvalidaError(
                "Informe o seu preço mínimo (piso): a trava de segurança não envia lance sem piso.")
        piso = estrategia_lances.dinheiro(payload.get("preco_minimo_limite"), "preco_minimo_limite")
    except estrategia_lances.EstrategiaInvalidaError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if valor <= 0 or piso <= 0:
        raise HTTPException(status_code=422, detail="Lance e piso devem ser maiores que zero.")
    if valor < piso:
        raise HTTPException(status_code=422, detail=(
            f"Lance bloqueado pela trava de segurança: R$ {valor} está abaixo do seu piso (R$ {piso})."))
    try:
        colocacao = int(payload.get("colocacao_alvo") or 1)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Colocação alvo inválida.")
    if not 1 <= colocacao <= estrategia_lances.MAX_POSICOES:
        raise HTTPException(status_code=422, detail="A colocação alvo vai do 1º ao 10º lugar.")
    observacao = (str(payload.get("observacao") or "").strip() or None)
    if observacao and len(observacao) > MAX_OBSERVACAO:
        raise HTTPException(status_code=422, detail=f"Observação com no máximo {MAX_OBSERVACAO} caracteres.")

    lote = _lote(tender_id, item_id, user)
    lance = AdminBid(
        tender_id=tender_id, item_id=item_id, user_id=user.id,
        valor_lance=valor, preco_minimo_limite=piso, colocacao_alvo=colocacao,
        status=_status_frente_ao_ranking(valor, _concorrentes(lote)), observacao=observacao,
    )
    db.add(lance)
    db.commit()
    db.refresh(lance)
    if lote:
        dispute_monitor.registrar_meu_lance(_chave_lote(tender_id, item_id), float(valor), owner_id=user.id)
    return {"status": "success", "lance": serializar_lance(lance),
            "posicao": estrategia_lances.posicao_do_lance(valor, _concorrentes(lote)) if lote and lote["ranking"] else None}


@router.get("/api/disputa/{tender_id:path}/meus-lances")
def api_meus_lances(tender_id: str, request: Request, item_id: Optional[str] = Query(None),
                    limite: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    """Histórico dos lances do operador no certame (mais recentes primeiro)."""
    user = exigir_admin(request)
    consulta = db.query(AdminBid).filter(AdminBid.user_id == user.id, AdminBid.tender_id == tender_id)
    if item_id:
        consulta = consulta.filter(AdminBid.item_id == item_id)
    lances = consulta.order_by(AdminBid.created_at.desc(), AdminBid.id.desc()).limit(limite).all()
    return {"tender_id": tender_id, "total": len(lances), "lances": [serializar_lance(l) for l in lances]}


# ------------------------------------------------------------------------------
# Robô de Lances Desktop (worker local do ADMIN, scripts/robo_desktop)
# O executável roda na máquina do administrador e é quem fala com o portal; aqui ficam a
# configuração (piso/degrau), o Kill-Switch, o sinal de vida e a auditoria de cada lance.
# ------------------------------------------------------------------------------
MAX_RESPOSTA_PORTAL = 500


def _lote_do_worker(item_id: str, user: User):
    item = dispute_monitor._find_item(item_id, owner_id=user.id)
    if not item:
        raise HTTPException(status_code=404, detail="Lote de disputa não encontrado para esta conta.")
    return item


def _portal_do_lote(item) -> str:
    if item.simulado:
        return "simulacao"  # sandbox: o robô desktop nunca envia lance real nessas salas
    fonte = (item.fonte_portal or "").lower()
    if "bnc" in fonte:
        return "bnc"
    if "compras" in fonte or "siasg" in fonte:
        return "comprasnet"
    return "outro"


def _config_do_worker(item) -> Dict[str, Any]:
    return {
        "item_id": item.item_key,
        "id_pregao": item.id_pregao,
        "numero_item": item.numero_item,
        "objeto": item.descricao,
        "preco_minimo": round(float(item.valor_minimo_limite or 0), 2),
        "decremento_minimo": round(float(item.degrau_minimo or 0), 2),
        "status_robo": "LIGADO" if item.auto_lance_ativo and not item.kill_switch else "DESLIGADO",
        "kill_switch": bool(item.kill_switch),
        "portal": _portal_do_lote(item),
        "fase": item.fase.upper(),
        "melhor_lance": round(float(item.melhor_lance or 0), 2),
        "meu_lance": round(float(item.meu_lance or 0), 2),
        "simulado": bool(item.simulado),
    }


def _agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@router.post("/api/disputa/criar-cenarios-simulacao")
async def api_criar_cenarios_simulacao(request: Request):
    """Cria (ou reinicia) as 4 salas do sandbox (sim-01 a sim-04) na conta do ADMIN, só em memória."""
    user = exigir_admin(request)
    itens = simulacao_disputa.criar_cenarios(user.id)
    return {"status": "success", "itens": itens, "motor_ativo": simulacao_disputa.motor_ativo(),
            "cenarios": [{"id": c["id"], "descricao": c["descricao"], "perfil": c["perfil"]}
                         for c in simulacao_disputa.CENARIOS]}


@router.get("/api/disputa/{item_id:path}/worker-config")
def api_worker_config(item_id: str, request: Request):
    """Parâmetros do lote para o robô desktop (piso, degrau, estado do robô, Kill-Switch, portal)."""
    user = exigir_admin(request)
    return _config_do_worker(_lote_do_worker(item_id, user))


@router.post("/api/disputa/{item_id:path}/worker-heartbeat")
def api_worker_heartbeat(item_id: str, request: Request, payload: Dict[str, Any] = Body(default={})):
    """Sinal de vida do robô (a cada ~1,5-2 s). Com o Kill-Switch acionado, devolve a ordem de abortar."""
    user = exigir_admin(request)
    item = _lote_do_worker(item_id, user)
    if item.kill_switch:
        return {"abortar": True, "motivo": "Kill-Switch acionado no painel web.", "config": _config_do_worker(item)}
    try:
        latencia = max(0, int(float(payload.get("latencia_ms") or 0)))
    except (TypeError, ValueError):
        latencia = 0
    item.worker_visto_em = time.time()
    item.worker = {
        "ultimo_heartbeat": _agora_iso(),
        "latencia_ms": latencia,
        "status": str(payload.get("status") or "OPERANDO")[:40],
        "versao": str(payload.get("versao") or "")[:20],
    }
    # Só o indicador do cockpit da própria conta é atualizado (sem redesenhar o lote)
    dispute_monitor.broadcast_sync({"type": "WORKER_HEARTBEAT", "owner_id": user.id, "item_key": item.item_key,
                                    "worker": dict(item.worker)})
    return {"abortar": False, "config": _config_do_worker(item)}


@router.post("/api/disputa/{item_id:path}/kill-switch")
def api_worker_kill_switch(item_id: str, request: Request, payload: Dict[str, Any] = Body(default={})):
    """Botão de emergência do painel: aciona (padrão) ou libera o Kill-Switch do lote; acionar desliga o robô."""
    user = exigir_admin(request)
    item = _lote_do_worker(item_id, user)
    ativo = bool(payload.get("ativo", True))
    item.kill_switch = ativo
    if ativo:
        item.auto_lance_ativo = False
        item.status_automacao = "Kill-Switch acionado"
    dispute_monitor.broadcast_sync({"type": "KILL_SWITCH", "ativo": ativo, "item": item.to_dict(),
                                    "timestamp": _agora_iso()})
    return {"status": "success", "kill_switch": ativo, "item": item.to_dict()}


@router.post("/api/disputa/{item_id:path}/worker-log", status_code=201)
def api_worker_log(item_id: str, request: Request, payload: Dict[str, Any] = Body(...),
                   db: Session = Depends(get_db)):
    """
    Auditoria de cada lance disparado pelo robô desktop (horário em ms, valor e resposta do portal).
    Segunda trava de Stop Loss: um lance abaixo do piso aciona o Kill-Switch e é recusado (422).
    """
    user = exigir_admin(request)
    item = _lote_do_worker(item_id, user)
    try:
        valor = estrategia_lances.dinheiro(payload.get("valor"), "valor")
    except estrategia_lances.EstrategiaInvalidaError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if valor <= 0:
        raise HTTPException(status_code=422, detail="O valor do lance deve ser maior que zero.")
    try:
        momento_ms = int(payload.get("timestamp_ms") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="timestamp_ms inválido.")
    if momento_ms <= 0:
        momento_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    resposta = str(payload.get("resposta_portal") or "")[:MAX_RESPOSTA_PORTAL]
    aceito = bool(payload.get("aceito", True))
    piso = Decimal(str(item.valor_minimo_limite or 0)).quantize(Decimal("0.01"))
    autor = f"Robô desktop ({user.name})"[:100]
    detalhes = {"timestamp_ms": momento_ms, "resposta_portal": resposta, "aceito": aceito,
                "latencia_ms": payload.get("latencia_ms"), "origem": "robo_desktop"}

    if piso <= 0 or valor < piso:
        # Nunca deveria chegar aqui (o robô trava antes de enviar): trata como incidente e para tudo
        item.kill_switch = True
        item.auto_lance_ativo = False
        item.status_automacao = "Kill-Switch: lance abaixo do piso"
        if not item.simulado:
            db.add(HistoricoDisputa(id_pregao=item.id_pregao, numero_item=item.numero_item, tipo_evento="STOP_LOSS",
                                    valor_ofertado=float(valor), posicao=0, autor=autor,
                                    detalhes={**detalhes, "piso": float(piso)}))
            db.commit()
        dispute_monitor.broadcast_sync({"type": "STOP_LOSS_REACHED", "item": item.to_dict(), "piso": float(piso),
                                        "timestamp": _agora_iso()})
        raise HTTPException(status_code=422, detail={
            "mensagem": f"Lance de R$ {valor} abaixo do piso (R$ {piso}): Kill-Switch acionado.", "abortar": True})

    lance = None
    if item.simulado:
        # Sandbox: só a memória e o cockpit são atualizados (nenhum dado fictício no banco)
        if aceito:
            dispute_monitor.registrar_meu_lance(item.item_key, float(valor), owner_id=user.id)
        dispute_monitor.broadcast_sync({"type": "WORKER_BID", "aceito": aceito, "lance": float(valor),
                                        "timestamp_ms": momento_ms, "resposta_portal": resposta,
                                        "item": item.to_dict(), "timestamp": _agora_iso()})
        return {"status": "success", "abortar": item.kill_switch, "aceito": aceito, "simulado": True,
                "lance": {"valor_lance": float(valor), "status": "simulado"} if aceito else None}

    db.add(HistoricoDisputa(id_pregao=item.id_pregao, numero_item=item.numero_item,
                            tipo_evento="WORKER_BID" if aceito else "WORKER_BID_REJEITADO",
                            valor_ofertado=float(valor), posicao=1, autor=autor, detalhes=detalhes))
    if aceito:
        lance = AdminBid(tender_id=item.id_pregao[:64], item_id=str(item.numero_item), user_id=user.id,
                         valor_lance=valor, preco_minimo_limite=piso, colocacao_alvo=item.posicao_alvo or 1,
                         status=_status_frente_ao_ranking(valor, [r["valor"] for r in item.ranking]),
                         observacao=f"Robô desktop: {resposta}"[:MAX_OBSERVACAO])
        db.add(lance)
    db.commit()
    if lance is not None:
        db.refresh(lance)
        dispute_monitor.registrar_meu_lance(item.item_key, float(valor), owner_id=user.id)
    dispute_monitor.broadcast_sync({"type": "WORKER_BID", "aceito": aceito, "lance": float(valor),
                                    "timestamp_ms": momento_ms, "resposta_portal": resposta,
                                    "item": item.to_dict(), "timestamp": _agora_iso()})
    return {"status": "success", "abortar": item.kill_switch, "aceito": aceito,
            "lance": serializar_lance(lance) if lance is not None else None}
