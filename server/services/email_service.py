"""
Envio de e-mails transacionais (recuperação de senha, avisos).

Provedores (variável EMAIL_PROVIDER):
  - MOCK   : não envia; registra o conteúdo no log (desenvolvimento).
  - SMTP   : smtplib com STARTTLS (porta 587) ou SSL (SMTP_USE_SSL=true, porta 465).
  - RESEND : API HTTP https://api.resend.com/emails com EMAIL_API_KEY.
"""
import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import List, Optional

import httpx

from server.config import settings

logger = logging.getLogger(__name__)


@dataclass
class EmailEnviado:
    provedor: str
    destinatario: str
    assunto: str
    sucesso: bool
    erro: Optional[str] = None


@dataclass
class _CaixaMock:
    """Guarda as mensagens do provedor MOCK em memória (útil em testes)."""
    mensagens: List[dict] = field(default_factory=list)

    def registrar(self, destinatario: str, assunto: str, texto: str, html: Optional[str]) -> None:
        self.mensagens.append({"to": destinatario, "subject": assunto, "text": texto, "html": html})
        del self.mensagens[:-50]


caixa_mock = _CaixaMock()


def _remetente() -> str:
    return formataddr(("Licitações Brasil", settings.EMAIL_FROM))


def _enviar_smtp(destinatario: str, assunto: str, texto: str, html: Optional[str]) -> None:
    if not settings.SMTP_HOST:
        raise RuntimeError("SMTP_HOST não configurado.")
    msg = EmailMessage()
    msg["From"] = _remetente()
    msg["To"] = destinatario
    msg["Subject"] = assunto
    msg["Message-ID"] = make_msgid(domain=settings.EMAIL_FROM.split("@")[-1] or None)
    msg.set_content(texto)
    if html:
        msg.add_alternative(html, subtype="html")

    contexto = ssl.create_default_context()
    if settings.SMTP_USE_SSL:
        with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, context=contexto, timeout=20) as smtp:
            if settings.SMTP_USER:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls(context=contexto)
            smtp.ehlo()
            if settings.SMTP_USER:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            smtp.send_message(msg)


def _enviar_resend(destinatario: str, assunto: str, texto: str, html: Optional[str]) -> None:
    if not settings.EMAIL_API_KEY:
        raise RuntimeError("EMAIL_API_KEY não configurada para o provedor RESEND.")
    payload = {"from": _remetente(), "to": [destinatario], "subject": assunto, "text": texto}
    if html:
        payload["html"] = html
    resp = httpx.post(
        "https://api.resend.com/emails",
        json=payload,
        headers={"Authorization": f"Bearer {settings.EMAIL_API_KEY}"},
        timeout=20,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"Resend respondeu HTTP {resp.status_code}: {resp.text[:200]}")


def enviar_email(destinatario: str, assunto: str, texto: str, html: Optional[str] = None) -> EmailEnviado:
    """Envia o e-mail pelo provedor configurado. Nunca propaga exceção para a rota chamadora."""
    provedor = (settings.EMAIL_PROVIDER or "MOCK").strip().upper()
    try:
        if provedor == "SMTP":
            _enviar_smtp(destinatario, assunto, texto, html)
        elif provedor == "RESEND":
            _enviar_resend(destinatario, assunto, texto, html)
        else:
            provedor = "MOCK"
            caixa_mock.registrar(destinatario, assunto, texto, html)
            logger.info("[EMAIL MOCK] Para: %s | Assunto: %s\n%s", destinatario, assunto, texto)
        return EmailEnviado(provedor, destinatario, assunto, True)
    except Exception as exc:  # falha de rede/credencial não pode vazar detalhes ao cliente
        logger.error("Falha ao enviar e-mail via %s para %s: %s", provedor, destinatario, exc)
        return EmailEnviado(provedor, destinatario, assunto, False, str(exc))


def montar_email_redefinicao(nome: str, link: str, minutos: int) -> tuple:
    """Retorna (assunto, texto, html) do e-mail de redefinição de senha."""
    from html import escape

    primeiro_nome = (nome or "").split(" ")[0] or "olá"
    assunto = "Redefinição de senha — Licitações Brasil"
    texto = (
        f"Olá, {primeiro_nome}.\n\n"
        "Recebemos um pedido para redefinir a senha da sua conta na Licitações Brasil.\n"
        f"Para criar uma nova senha, acesse o link abaixo (válido por {minutos} minutos e de uso único):\n\n"
        f"{link}\n\n"
        "Se você não fez este pedido, ignore este e-mail: sua senha atual continua válida.\n\n"
        "Equipe Licitações Brasil"
    )
    html = f"""<!doctype html>
<html lang="pt-BR"><body style="margin:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;color:#0f172a">
  <div style="max-width:520px;margin:24px auto;background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #e2e8f0">
    <div style="background:#082f49;padding:18px 24px;color:#ffffff;font-size:18px;font-weight:bold">
      Licitações<span style="color:#38bdf8">Brasil</span>
    </div>
    <div style="padding:24px">
      <p style="margin:0 0 12px">Olá, {escape(primeiro_nome)}.</p>
      <p style="margin:0 0 16px">Recebemos um pedido para redefinir a senha da sua conta.</p>
      <p style="margin:0 0 20px">
        <a href="{escape(link, quote=True)}" style="display:inline-block;background:#0284c7;color:#ffffff;text-decoration:none;font-weight:bold;padding:12px 20px;border-radius:10px">Criar nova senha</a>
      </p>
      <p style="margin:0 0 8px;font-size:13px;color:#475569">O link é válido por {minutos} minutos e só pode ser usado uma vez.</p>
      <p style="margin:0;font-size:13px;color:#475569">Se você não fez este pedido, ignore este e-mail: sua senha atual continua válida.</p>
    </div>
  </div>
</body></html>"""
    return assunto, texto, html
