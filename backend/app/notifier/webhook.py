"""Notificaciones vía webhook genérico (Slack/Discord/n8n/Telegram/CallMeBot-WhatsApp).

Lee:
  ALERT_WEBHOOK_URL       — URL completa del webhook
  ALERT_WEBHOOK_METHOD    — POST por defecto (GET sirve para CallMeBot)
  ALERT_WEBHOOK_TEMPLATE  — JSON template; vacío → {"text": "subject\\nbody"}

Si el template tiene `{subject}` o `{body}`, los reemplaza. Para CallMeBot que
usa GET con `text` en query string, dejá la URL con `text={subject}` y poné
método GET — vamos a expandir la URL también con esos placeholders.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import quote_plus

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def _expand(template: str, subject: str, body: str, *, url_safe: bool = False) -> str:
    s = quote_plus(subject) if url_safe else subject
    b = quote_plus(body) if url_safe else body
    return template.replace("{subject}", s).replace("{body}", b)


async def send(subject: str, body: str) -> bool:
    """Manda al webhook. Devuelve True si fue 2xx, False si no estaba configurado o falló."""
    s = get_settings()
    if not s.alert_webhook_url:
        log.debug("Webhook no configurado, salteando.")
        return False

    method = s.alert_webhook_method.upper()
    url = _expand(s.alert_webhook_url, subject, body, url_safe=True)

    payload: dict | str | None = None
    if s.alert_webhook_template:
        rendered = _expand(s.alert_webhook_template, subject, body)
        try:
            payload = json.loads(rendered)
        except json.JSONDecodeError:
            payload = rendered  # se manda como texto crudo
    else:
        payload = {"text": f"*{subject}*\n{body}"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            if method == "GET":
                r = await client.get(url)
            else:
                if isinstance(payload, dict):
                    r = await client.request(method, url, json=payload)
                else:
                    r = await client.request(
                        method, url, content=payload,
                        headers={"Content-Type": "text/plain"},
                    )
            r.raise_for_status()
            return True
    except httpx.HTTPError as exc:
        log.warning("Webhook falló (%s %s): %s", method, url, exc)
        return False
