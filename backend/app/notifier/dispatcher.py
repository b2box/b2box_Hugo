"""Punto único para notificar. Manda por todos los canales configurados.

Hugo no tiene que saber a qué canales se manda — solo `await notify(subject, body)`.
"""

from __future__ import annotations

import logging
from typing import Iterable

from app.db.models import AuditLog
from app.notifier import email as email_chan
from app.notifier import webhook as webhook_chan

log = logging.getLogger(__name__)


async def notify(subject: str, body: str) -> dict[str, bool]:
    """Manda subject/body por todos los canales habilitados.

    Devuelve un dict {canal: ok} para que el caller pueda loguear o reaccionar.
    """
    return {
        "email": await email_chan.send(subject, body),
        "webhook": await webhook_chan.send(subject, body),
    }


async def notify_digest(logs: Iterable[AuditLog]) -> dict[str, bool]:
    """Envía el digest diario por todos los canales."""
    rendered = email_chan.render_daily_digest(logs)
    if rendered is None:
        return {"email": False, "webhook": False}
    subject, body = rendered
    return await notify(subject, body)
