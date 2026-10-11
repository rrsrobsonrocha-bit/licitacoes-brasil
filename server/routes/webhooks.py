"""
Webhook da Hotmart (Licitações Brasil) — em modo de espera.

O endpoint já recebe, valida e interpreta as notificações, mas ainda NÃO altera nenhuma conta:
apenas registra no log o evento, o comprador e a ação que será aplicada quando a integração for
ligada. Sem HOTMART_HOTTOK configurado, aceita a chamada sem validar (e sem efeito), para que a
Hotmart possa ser apontada para cá antes de a configuração estar completa.
"""
import hmac
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from server.config import settings

router = APIRouter()

# Evento da Hotmart -> ação prevista na assinatura da conta com o e-mail do comprador
ACOES_POR_EVENTO = {
    "PURCHASE_APPROVED": "ativar",
    "PURCHASE_COMPLETE": "ativar",
    "SUBSCRIPTION_REACTIVATION": "ativar",
    "PURCHASE_DELAYED": "suspender",
    "PURCHASE_PROTEST": "suspender",
    "PURCHASE_CANCELED": "cancelar",
    "PURCHASE_REFUNDED": "cancelar",
    "PURCHASE_CHARGEBACK": "cancelar",
    "PURCHASE_EXPIRED": "cancelar",
    "SUBSCRIPTION_CANCELLATION": "cancelar",
    "SUBSCRIPTION_CANCELED": "cancelar",
    "SWITCH_PLAN": "trocar_plano",
}


def _mascarar_email(email: str) -> str:
    """Evita gravar o e-mail completo do comprador nos logs."""
    usuario, _, dominio = email.partition("@")
    return f"{usuario[:1]}***@{dominio}" if dominio else "(sem e-mail)"


def _hottok_recebido(request: Request, payload: Dict[str, Any]) -> str:
    # Webhook v2: cabeçalho X-HOTMART-HOTTOK; versões antigas mandavam "hottok" no corpo
    return request.headers.get("X-HOTMART-HOTTOK") or str(payload.get("hottok") or "")


def interpretar_evento(payload: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Extrai evento, comprador, assinante e plano do formato de webhook v2 da Hotmart."""
    dados = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    comprador = dados.get("buyer") if isinstance(dados.get("buyer"), dict) else {}
    assinatura = dados.get("subscription") if isinstance(dados.get("subscription"), dict) else {}
    assinante = assinatura.get("subscriber") if isinstance(assinatura.get("subscriber"), dict) else {}
    plano = assinatura.get("plan") if isinstance(assinatura.get("plan"), dict) else {}
    evento = str(payload.get("event") or "").upper() or None
    return {
        "evento": evento,
        "acao_prevista": ACOES_POR_EVENTO.get(evento or "", "ignorar"),
        "email": (str(comprador.get("email") or "").strip().lower()) or None,
        "codigo_assinante": assinante.get("code"),
        "plano": plano.get("name"),
    }


@router.post("/api/webhooks/hotmart")
async def webhook_hotmart(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    hottok = settings.HOTMART_HOTTOK
    if hottok:
        if not hmac.compare_digest(_hottok_recebido(request, payload), hottok):
            print("[HOTMART] Webhook recusado: hottok inválido.")
            raise HTTPException(status_code=401, detail="Token da Hotmart inválido.")
    else:
        print("[HOTMART] HOTMART_HOTTOK não configurado: evento registrado sem validação e sem efeito.")

    info = interpretar_evento(payload)
    print(
        f"[HOTMART] Evento {info['evento'] or '(sem evento)'} | comprador "
        f"{_mascarar_email(info['email'] or '')} | assinante {info['codigo_assinante'] or '-'} | "
        f"plano {info['plano'] or '-'} | ação prevista: {info['acao_prevista']} "
        f"(modo de espera: nenhuma conta foi alterada)"
    )
    return {
        "status": "recebido",
        "modo": "espera",
        "evento": info["evento"],
        "acao_prevista": info["acao_prevista"],
        "aplicado": False,
    }
