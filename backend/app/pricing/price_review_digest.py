"""Resumen diario de la bandeja de revisión para Pao (mail de texto plano, 08:30 hora de Argentina).

Sale por el SMTP de siempre (`ALERT_SMTP_*`) a los destinatarios de `PM_REVIEW_EMAILS` (vacío = no se manda).
Lleva lo NUEVO desde el último envío (fichas nuevas, reabiertas por empeorar y lo que hay que mirar a mano: errores de
Vendure, reactivaciones frenadas, cambios que quedaron a medias) y, una sola vez por ficha, las que llevan más de
`pm_review_sla_days` días esperando decisión (propuestas y en revisión). Sin novedades no se manda. Cada evento lleva
`notified_at`, así que un reintento (o un segundo disparo el mismo día) no repite nada: los eventos se marcan recién
DESPUÉS de que el SMTP aceptó el mail.

Hugo propone; inactiva o reactiva en Vendure solo lo que decide una persona de la lista (PRICE_REVIEW_DECIDERS). La única
excepción es la reactivación automática del recálculo (pricing/price_recalc.py): lo que apagó Hugo, se corrigió en Vendure y
ya no es rojo firme. Esas salen una vez en «REACTIVADOS SOLOS».
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlmodel import Session, func, select

from app import runtime
from app.clock import utcnow
from app.config import get_settings
from app.db.models import PriceReviewEvent, PriceReviewItem
from app.db.session import engine
from app.notifier import email as email_chan
from app.pricing import price_review

log = logging.getLogger(__name__)

MAX_NEW_LISTED = 15
MAX_WAITING_LISTED = 30
AVISO_SLA = "aviso_sla"
WAITING_STATES = (price_review.PROPUESTO, price_review.EN_REVISION)
# Eventos que salen una vez en «PARA MIRAR A MANO», con una frase fija (nunca el código de error ni el actor).
ATTENTION = {
    "vendure_error": "Vendure no aceptó el cambio: la ficha quedó como estaba",
    "reactivar_a_mano": "hay que reactivarlo a mano en Vendure",
    "intent_descartado": "un cambio en Vendure quedó a medias y se descartó",
    "escritura_huerfana": "Vendure tomó un cambio cuando la ficha ya había cambiado: revisalo a mano",
    "vendure_incierto": "no sabemos si Vendure tomó el cambio; Hugo lo revisa en la próxima acción o esta noche",
    "sin_vuelta_atras": "no se pudo volver atrás porque el producto ya tiene otra ficha abierta: esta se cerró",
    "rojo_tras_reactivar": "Hugo lo había reactivado solo y esta noche volvió a dar rojo firme",
}
CAUSE_TEXT = {
    "a": "caja fuera de rango", "b": "tramos mal armados", "c": "certificado alto",
    "d": "valor bajo por m³", "e": "ML bajo el costo de fábrica", "ninguna": "sin causa clara",
    "sin_dato": "diagnóstico incompleto",
}
# Control (C0, DEL, C1), separadores de línea y párrafo, los de dirección bidi (U+202A-202E, U+2066-2069, LRM/RLM
# U+200E-200F, ALM U+061C) y los invisibles de ancho cero (U+200B-200D, BOM U+FEFF): con estos un texto se puede mostrar
# en otro orden del que dice («gpj.exe» que se lee «exe.jpg») o esconder algo.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u061c\u200b-\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]+")


def clean(text: str | None, limit: int = 80) -> str:
    """Un texto de la base (nombre de producto, que viene de Vendure) listo para una línea del mail: sin
    saltos ni caracteres de control, y acotado."""
    value = " ".join(_CONTROL.sub(" ", text or "").split())
    return value[: limit - 1] + "…" if len(value) > limit else value


@dataclass(slots=True)
class DigestData:
    new: list[PriceReviewItem] = field(default_factory=list)
    new_events: list[PriceReviewEvent] = field(default_factory=list)
    waiting: list[PriceReviewItem] = field(default_factory=list)
    closed_events: list[PriceReviewEvent] = field(default_factory=list)
    reopened: list[tuple[PriceReviewEvent, PriceReviewItem]] = field(default_factory=list)
    attention: list[tuple[PriceReviewEvent, PriceReviewItem]] = field(default_factory=list)
    reactivated: list[tuple[PriceReviewEvent, PriceReviewItem]] = field(default_factory=list)   # las que reactivó Hugo solo
    pending_total: int = 0
    sla_days: int = 7
    now: datetime = field(default_factory=utcnow)

    @property
    def empty(self) -> bool:
        return not (self.new or self.waiting or self.reopened or self.attention or self.reactivated)


def collect(session: Session, now: datetime | None = None) -> DigestData:
    """Lo que hay para contar desde el último envío. No escribe nada."""
    now = now or utcnow()
    sla = int(runtime.get("pm_review_sla_days"))
    data = DigestData(sla_days=sla, now=now)
    data.new_events = list(session.exec(
        select(PriceReviewEvent).where(PriceReviewEvent.kind == "propuesto", PriceReviewEvent.notified_at.is_(None))  # type: ignore[union-attr]
        .order_by(PriceReviewEvent.at, PriceReviewEvent.id)).all())
    data.closed_events = list(session.exec(
        select(PriceReviewEvent).where(PriceReviewEvent.kind == "cerrado_auto", PriceReviewEvent.notified_at.is_(None))  # type: ignore[union-attr]
        .order_by(PriceReviewEvent.at, PriceReviewEvent.id)).all())
    others = list(session.exec(
        select(PriceReviewEvent).where(PriceReviewEvent.kind.in_(("reabierto", *ATTENTION)),  # type: ignore[attr-defined]
                                       PriceReviewEvent.notified_at.is_(None))  # type: ignore[union-attr]
        .order_by(PriceReviewEvent.at, PriceReviewEvent.id)).all())
    auto = list(session.exec(
        select(PriceReviewEvent).where(PriceReviewEvent.kind == "reactivado", PriceReviewEvent.actor == price_review.ACTOR_AUTO,
                                       PriceReviewEvent.notified_at.is_(None))  # type: ignore[union-attr]
        .order_by(PriceReviewEvent.at, PriceReviewEvent.id)).all())
    item_ids = [e.item_id for e in (*data.new_events, *others, *auto)]
    items = {i.id: i for i in session.exec(select(PriceReviewItem).where(PriceReviewItem.id.in_(item_ids))).all()} if item_ids else {}  # type: ignore[attr-defined]
    # Una ficha nueva que ya se cerró sola antes del mail no se lista (queda contada en «cerradas solas»).
    data.new = [items[e.item_id] for e in data.new_events if e.item_id in items and items[e.item_id].state == price_review.PROPUESTO]
    data.reopened = [(e, items[e.item_id]) for e in others if e.kind == "reabierto" and e.item_id in items]
    data.attention = [(e, items[e.item_id]) for e in others if e.kind in ATTENTION and e.item_id in items]
    data.reactivated = [(e, items[e.item_id]) for e in auto if e.item_id in items]
    open_items = list(session.exec(
        select(PriceReviewItem).where(PriceReviewItem.state.in_(WAITING_STATES))  # type: ignore[attr-defined]
        .order_by(PriceReviewItem.state_since, PriceReviewItem.id)).all())
    data.pending_total = len(open_items)
    # El aviso vale para el estado actual: una ficha que pasó de «propuesto» a «en revisión» puede volver a salir.
    last_warned = dict(session.exec(
        select(PriceReviewEvent.item_id, func.max(PriceReviewEvent.at))  # type: ignore[arg-type]
        .where(PriceReviewEvent.kind == AVISO_SLA).group_by(PriceReviewEvent.item_id)).all())
    new_ids = {i.id for i in data.new}
    data.waiting = [i for i in open_items
                    if i.state_since <= now - timedelta(days=sla) and i.id not in new_ids
                    and not (i.id in last_warned and last_warned[i.id] >= i.state_since)]
    return data


def _cause(item: PriceReviewItem) -> str:
    if item.probable_cause is None:
        return "diagnóstico pendiente"
    return CAUSE_TEXT.get(item.probable_cause, item.probable_cause)


def _line(item: PriceReviewItem, extra: str = "") -> str:
    margin = "" if item.last_margin_pct is None else f" · {item.last_margin_pct:+.0f} %".replace(".", ",")
    code = clean(item.product_code or f"#{item.product_id}", 24)
    return f" - {code} · {clean(item.product_name)}{margin} · {_cause(item)}{extra}"


def render(data: DigestData) -> tuple[str, str]:
    """(asunto, cuerpo) del mail. Cada ficha es UNA línea que empieza con « - »."""
    n_new = len(data.new)
    subject = f"[Hugo] Revisión de precios: {n_new} nuevos, {data.pending_total} esperando decisión"
    lines = [
        "Hola,",
        "",
        "Resumen de la bandeja de revisión de precios (Mercado Libre contra nuestros precios).",
        "Hugo propone; solo inactiva o reactiva lo que decide una persona de la lista.",
        "",
    ]
    if data.reactivated:
        lines[-1:-1] = ["La excepción: reactiva solo lo que había apagado él cuando se corrigió en Vendure y ya no está en rojo."]
    if data.new:
        lines.append(f"NUEVOS ({n_new})")
        lines += [_line(i) for i in data.new[:MAX_NEW_LISTED]]
        if n_new > MAX_NEW_LISTED:
            lines.append(f"   … y {n_new - MAX_NEW_LISTED} más en la bandeja.")
        lines.append("")
    if data.waiting:
        lines.append(f"ESPERANDO DECISIÓN HACE MÁS DE {data.sla_days} DÍAS ({len(data.waiting)})")
        for item in data.waiting[:MAX_WAITING_LISTED]:
            days = int((data.now - item.state_since).total_seconds() // 86400)
            lines.append(_line(item, f" · hace {days} días"))
        if len(data.waiting) > MAX_WAITING_LISTED:
            lines.append(f"   … y {len(data.waiting) - MAX_WAITING_LISTED} más.")
        lines.append("")
    if data.reopened:
        lines.append(f"REABIERTOS POR EMPEORAR ({len(data.reopened)})")
        for ev, item in data.reopened:
            worse = (price_review._loads(ev.payload) or {}).get("empeoro_pct")
            ok = isinstance(worse, (int, float))
            lines.append(_line(item, f" · empeoró {worse:g} %".replace(".", ",") if ok else ""))
        lines.append("")
    if data.attention:
        lines.append(f"PARA MIRAR A MANO ({len(data.attention)})")
        lines += [_line(item, f" · {ATTENTION[ev.kind]}") for ev, item in data.attention]
        lines.append("")
    if data.reactivated:
        lines.append(f"REACTIVADOS SOLOS ({len(data.reactivated)})")
        lines += [_line(item, " · se corrigió en Vendure y ya no es rojo: Hugo lo reactivó") for _, item in data.reactivated]
        lines.append("")
    if data.closed_events:
        lines.append(f"Se cerraron solas desde el último mail: {len(data.closed_events)} (el producto dejó de ser rojo).")
    lines.append(f"Total esperando decisión: {data.pending_total}.")
    base = (get_settings().hugo_public_url or "").strip()
    if base.lower().startswith("https://"):
        lines += ["", f"Abrí la bandeja: {base.rstrip('/')}/"]
    return subject, "\n".join(lines) + "\n"


def mark_sent(session: Session, data: DigestData, now: datetime | None = None) -> None:
    """Después de que el SMTP aceptó el mail: los eventos incluidos pasan a `notified_at` y cada ficha que salió
    como «esperando» deja su aviso (así no vuelve a salir). Lo mismo para una ficha que ya pasaba el plazo cuando salió por
    primera vez como «nueva» (p. ej. se cargó PM_REVIEW_EMAILS tarde): no tiene que volver a salir como «esperando» en el
    próximo mail. No hace commit."""
    now = now or utcnow()
    for ev in (*data.new_events, *data.closed_events, *(e for e, _ in (*data.reopened, *data.attention, *data.reactivated))):
        ev.notified_at = now
        session.add(ev)
    overdue_new = [i for i in data.new if i.state_since <= now - timedelta(days=data.sla_days)]
    for item in (*data.waiting, *overdue_new):
        ev = PriceReviewEvent(item_id=int(item.id), product_id=item.product_id, at=now, actor=price_review.ACTOR_AUTO,  # type: ignore[arg-type]
                              kind=AVISO_SLA, notified_at=now,
                              payload=json.dumps({"dias": int((now - item.state_since).total_seconds() // 86400)}))
        session.add(ev)


async def send_digest(now: datetime | None = None) -> dict[str, Any]:
    """El job de las 08:30 ART. Devuelve qué hizo: `sent` o el motivo por el que no mandó."""
    now = now or utcnow()
    recipients = email_chan.parse_recipients(get_settings().pm_review_emails)
    if not recipients:
        return {"sent": False, "reason": "PM_REVIEW_EMAILS vacío"}
    with Session(engine, expire_on_commit=False) as s:
        data = collect(s, now)
        if data.empty:
            # Lo que se cerró solo sin nada más que contar no justifica un mail; igual se deja anotado.
            if data.closed_events:
                mark_sent(s, data, now)
                s.commit()
            return {"sent": False, "reason": "sin novedades"}
        subject, body = render(data)
        ok = await email_chan.send(subject, body, to=recipients)
        if not ok:
            log.warning("price_review_digest: el mail no salió (¿SMTP sin configurar?); se reintenta en la próxima pasada")
            return {"sent": False, "reason": "el SMTP no aceptó el mail", "new": len(data.new), "waiting": len(data.waiting)}
        mark_sent(s, data, now)
        s.commit()
    return {"sent": True, "to": recipients, "new": len(data.new), "waiting": len(data.waiting),
            "pending": data.pending_total, "lines": len(data.new[:MAX_NEW_LISTED]) + len(data.waiting[:MAX_WAITING_LISTED])}
