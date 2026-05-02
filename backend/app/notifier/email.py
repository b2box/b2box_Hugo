"""Notificaciones por email vía SMTP (aiosmtplib).

Usa las vars ALERT_SMTP_* / ALERT_EMAIL_TO del ecosistema B2Box.
"""

from __future__ import annotations

import logging
from email.message import EmailMessage
from typing import Iterable

import aiosmtplib

from app.config import get_settings
from app.db.models import AuditLog

log = logging.getLogger(__name__)


async def send(subject: str, body: str) -> bool:
    """Envía un email. Devuelve True si se envió, False si no estaba configurado."""
    s = get_settings()
    if not s.alert_smtp_user or not s.alert_smtp_pass:
        log.debug("SMTP no configurado, salteando email.")
        return False

    msg = EmailMessage()
    msg["From"] = s.alert_email_from or s.alert_smtp_user
    msg["To"] = str(s.alert_email_to)
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        await aiosmtplib.send(
            msg,
            hostname=s.alert_smtp_host,
            port=s.alert_smtp_port,
            username=s.alert_smtp_user,
            password=s.alert_smtp_pass,
            start_tls=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.exception("Falla SMTP: %s", exc)
        return False


def render_daily_digest(logs: Iterable[AuditLog]) -> tuple[str, str] | None:
    """Devuelve (subject, body) del digest, o None si no hay nada que reportar."""
    logs = list(logs)
    if not logs:
        return None
    counts: dict[str, int] = {}
    lines: list[str] = []
    for entry in logs:
        counts[entry.action] = counts.get(entry.action, 0) + 1
        lines.append(
            f"  - [{entry.created_at:%Y-%m-%d %H:%M}] {entry.action} "
            f"product={entry.product_id} :: {entry.detail}"
        )
    summary = "\n".join(f"  · {k}: {v}" for k, v in sorted(counts.items()))
    body = (
        f"Resumen de actividad de Hugo (últimas 24h)\n"
        f"==========================================\n"
        f"Total acciones: {len(logs)}\n\n"
        f"Por tipo:\n{summary}\n\n"
        f"Detalle:\n" + "\n".join(lines[-200:])
    )
    return ("[Hugo] Resumen diario", body)
