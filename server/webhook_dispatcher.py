import json
import time
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Union
import httpx
from sqlalchemy.orm import Session
from server.models import WebhookDispatchLog

logger = logging.getLogger(__name__)

SUPPORTED_CHANNELS = ["N8N", "EVOLUTION_API", "ZAPI", "GENERIC"]

def format_webhook_payload(
    canal: str,
    event: str,
    data: Dict[str, Any],
    rule_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Formata o payload de acordo com os padrões específicos de cada canal.
    """
    canal_upper = canal.upper() if canal else "GENERIC"
    timestamp_iso = datetime.utcnow().isoformat()

    if canal_upper == "EVOLUTION_API":
        return {
            "event": event,
            "rule_id": rule_id,
            "timestamp": timestamp_iso,
            "data": data
        }
    elif canal_upper == "ZAPI":
        return {
            "eventType": event,
            "ruleId": rule_id,
            "timestamp": timestamp_iso,
            "payload": data
        }
    elif canal_upper == "N8N":
        return {
            "source": "licitacoes-brasil",
            "event": event,
            "rule_id": rule_id,
            "timestamp": timestamp_iso,
            "body": data
        }
    else:  # GENERIC
        return {
            "event": event,
            "rule_id": rule_id,
            "timestamp": timestamp_iso,
            "data": data
        }

def dispatch_webhook_sync(
    db: Session,
    target_url: str,
    payload: Dict[str, Any],
    canal: str = "GENERIC",
    rule_id: Optional[str] = None,
    tender_id: Optional[str] = None,
    tentativa_atual: int = 1,
    timeout: float = 10.0,
    headers: Optional[Dict[str, str]] = None
) -> WebhookDispatchLog:
    """
    Dispara um webhook síncrono e registra o log em WebhookDispatchLog com latência e status.
    """
    canal_norm = canal.upper() if canal and canal.upper() in SUPPORTED_CHANNELS else "GENERIC"
    req_headers = {"Content-Type": "application/json", "User-Agent": "LicitacoesBrasil-Webhook/1.0"}
    if headers:
        req_headers.update(headers)

    start_time = time.perf_counter()
    status_code: Optional[int] = None
    resposta_servidor: Optional[str] = None
    sucesso = False

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(target_url, json=payload, headers=req_headers)
            status_code = response.status_code
            resposta_servidor = response.text
            sucesso = 200 <= status_code < 300
    except httpx.TimeoutException as e:
        status_code = None
        resposta_servidor = f"Erro de Timeout ({timeout}s): {str(e)}"
        sucesso = False
    except httpx.RequestError as e:
        status_code = None
        resposta_servidor = f"Erro de Conexão/Rede: {str(e)}"
        sucesso = False
    except Exception as e:
        status_code = None
        resposta_servidor = f"Erro Inesperado: {str(e)}"
        sucesso = False
    finally:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)

    log_entry = WebhookDispatchLog(
        rule_id=rule_id,
        tender_id=tender_id,
        target_url=target_url,
        canal=canal_norm,
        status_code=status_code,
        tentativa_atual=tentativa_atual,
        sucesso=sucesso,
        tempo_resposta_ms=latency_ms,
        payload_enviado=payload,
        resposta_servidor=resposta_servidor,
        criado_em=datetime.utcnow()
    )

    db.add(log_entry)
    db.commit()
    db.refresh(log_entry)
    return log_entry


async def dispatch_webhook_async(
    db: Session,
    target_url: str,
    payload: Dict[str, Any],
    canal: str = "GENERIC",
    rule_id: Optional[str] = None,
    tender_id: Optional[str] = None,
    tentativa_atual: int = 1,
    timeout: float = 10.0,
    headers: Optional[Dict[str, str]] = None
) -> WebhookDispatchLog:
    """
    Dispara um webhook assíncrono e registra o log em WebhookDispatchLog com latência e status.
    """
    canal_norm = canal.upper() if canal and canal.upper() in SUPPORTED_CHANNELS else "GENERIC"
    req_headers = {"Content-Type": "application/json", "User-Agent": "LicitacoesBrasil-Webhook/1.0"}
    if headers:
        req_headers.update(headers)

    start_time = time.perf_counter()
    status_code: Optional[int] = None
    resposta_servidor: Optional[str] = None
    sucesso = False

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(target_url, json=payload, headers=req_headers)
            status_code = response.status_code
            resposta_servidor = response.text
            sucesso = 200 <= status_code < 300
    except httpx.TimeoutException as e:
        status_code = None
        resposta_servidor = f"Erro de Timeout ({timeout}s): {str(e)}"
        sucesso = False
    except httpx.RequestError as e:
        status_code = None
        resposta_servidor = f"Erro de Conexão/Rede: {str(e)}"
        sucesso = False
    except Exception as e:
        status_code = None
        resposta_servidor = f"Erro Inesperado: {str(e)}"
        sucesso = False
    finally:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)

    log_entry = WebhookDispatchLog(
        rule_id=rule_id,
        tender_id=tender_id,
        target_url=target_url,
        canal=canal_norm,
        status_code=status_code,
        tentativa_atual=tentativa_atual,
        sucesso=sucesso,
        tempo_resposta_ms=latency_ms,
        payload_enviado=payload,
        resposta_servidor=resposta_servidor,
        criado_em=datetime.utcnow()
    )

    db.add(log_entry)
    db.commit()
    db.refresh(log_entry)
    return log_entry


def retry_webhook_dispatch(
    db: Session,
    log_id: str,
    timeout: float = 10.0
) -> Optional[WebhookDispatchLog]:
    """
    Reexecuta manualmente um disparo a partir de um log existente,
    incrementando a contagem de tentativas e gerando um novo log.
    """
    old_log = db.query(WebhookDispatchLog).filter(WebhookDispatchLog.id == log_id).first()
    if not old_log:
        return None

    new_tentativa = (old_log.tentativa_atual or 1) + 1
    new_log = dispatch_webhook_sync(
        db=db,
        target_url=old_log.target_url,
        payload=old_log.payload_enviado or {},
        canal=old_log.canal,
        rule_id=old_log.rule_id,
        tender_id=old_log.tender_id,
        tentativa_atual=new_tentativa,
        timeout=timeout
    )
    return new_log
