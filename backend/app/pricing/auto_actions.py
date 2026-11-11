"""«¿Qué prendió y qué apagó Hugo solo?»: el listado de las escrituras automáticas en Vendure.

Toda escritura de `enabled` que hace Hugo sin una persona pasa por `price_review_actions` con el actor `hugo:auto` y deja,
en `price_review_event` (que es de solo agregar: no se edita ni se borra):

  * un evento `decision_activar_auto` / `decision_inactivar_auto` / `decision_reactivar_auto` (la decisión), y
  * un evento `reactivado` (quedó PRENDIDO en Vendure) o `inactivado` (quedó APAGADO), que es el que confirma que Vendure
    tomó el cambio. Lleva en `payload` la acción, la regla (`regla`) y la corrida del semáforo (`corrida`, también en
    `run_id`).

Además queda una fila en `audit_log` (`price_review_enabled` / `price_review_disabled`, con «de hugo:auto» en el detalle),
que es la que se ve en la historia del producto.

`list_auto_actions(days)` junta los eventos confirmados de los últimos N días con el producto, la regla y la corrida, y dice
si una persona lo deshizo después. Para las escrituras anteriores a que la regla y la corrida viajaran en el evento, las
deduce del evento `porton` más cercano de la misma ficha (y lo marca `regla_deducida`).

Consulta equivalente en SQL (por si no hay dashboard a mano):

    SELECT e.at, e.product_id, i.product_name, e.kind, e.payload, e.run_id
      FROM price_review_event e JOIN price_review_item i ON i.id = e.item_id
     WHERE e.actor = 'hugo:auto' AND e.kind IN ('reactivado', 'inactivado')
       AND e.at >= now() - interval '7 days'
     ORDER BY e.at DESC;
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from app.clock import utcnow
from app.db.models import PriceReviewEvent, PriceReviewItem, Setting
from app.db.session import engine
from app.pricing import price_review as pr

WRITE_KINDS = ("reactivado", "inactivado")
MAX_ROWS = 2000
# Los marcadores del piloto automático (scheduler/jobs.py).
_AUTOPILOT_MODE_MARKER = "_meta:autopilot:mode_set"
_AUTOPILOT_RUN_PREFIX = "_meta:autopilot:run:"

ACTION_TEXT = {
    "activar_auto": "Hugo lo activó solo (portón de precio)",
    "inactivar_auto": "Hugo lo apagó solo (portón de precio)",
    "reactivar_auto": "Hugo lo reactivó solo (se corrigió en Vendure y ya no es rojo)",
}
RULE_TEXT = {
    "verde_firme_subido_por_paco": "Verde firme de un producto recién subido",
    "verde_firme_catalogo_apagado": "Verde firme de un producto apagado del catálogo",
    "rojo_firme_publicado": "Rojo firme publicado",
    "recalculo_ya_no_rojo": "Lo había apagado Hugo, se corrigió en Vendure y ya no es rojo firme",
    "verde_firme": "Verde firme",
    "rojo_firme": "Rojo firme",
}


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() + "Z" if moment is not None else None


def _deduced(s: Session, ev: PriceReviewEvent) -> tuple[str | None, int | None]:
    """Regla y corrida de una escritura vieja (sin `regla` en el evento): las del `porton` anterior más cercano de la
    misma ficha; si no hay, el motivo de la decisión."""
    prior = s.exec(select(PriceReviewEvent).where(
        PriceReviewEvent.item_id == ev.item_id, PriceReviewEvent.id < ev.id,
        PriceReviewEvent.kind.in_(("porton", "decision_activar_auto", "decision_inactivar_auto",  # type: ignore[attr-defined]
                                   "decision_reactivar_auto")))
        .order_by(PriceReviewEvent.id.desc()).limit(6)).all()  # type: ignore[union-attr]
    rule = next((p.motive_code for p in prior if p.kind == "porton" and p.motive_code), None)
    run = next((p.run_id for p in prior if p.run_id is not None), None)
    return rule, run


def _undone_by(s: Session, ev: PriceReviewEvent) -> PriceReviewEvent | None:
    """¿Una persona deshizo esta escritura después? (el primer `deshacer` humano posterior, si antes no hubo otra
    escritura automática sobre la misma ficha)."""
    later = s.exec(select(PriceReviewEvent).where(
        PriceReviewEvent.item_id == ev.item_id, PriceReviewEvent.id > ev.id,
        PriceReviewEvent.kind.in_(("deshacer", *WRITE_KINDS)))  # type: ignore[attr-defined]
        .order_by(PriceReviewEvent.id).limit(1)).first()  # type: ignore[arg-type]
    return later if later is not None and later.kind == "deshacer" and later.actor != pr.ACTOR_AUTO else None


def list_auto_actions(days: int = 7, *, limit: int = 500, now: datetime | None = None) -> dict[str, Any]:
    """Las escrituras automáticas confirmadas de los últimos `days` días, la más nueva primero, con totales."""
    now = now or utcnow()
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), MAX_ROWS))
    since = now - timedelta(days=days)
    out: list[dict[str, Any]] = []
    totals = {"activados": 0, "apagados": 0, "deshechos_por_una_persona": 0}
    with Session(engine) as s:
        base = (PriceReviewEvent.actor == pr.ACTOR_AUTO, PriceReviewEvent.kind.in_(WRITE_KINDS),  # type: ignore[attr-defined]
                PriceReviewEvent.at >= since)
        events = s.exec(select(PriceReviewEvent).where(*base)
                        .order_by(PriceReviewEvent.at.desc(), PriceReviewEvent.id.desc())  # type: ignore[union-attr]
                        .limit(MAX_ROWS + 1)).all()
        truncated = len(events) > MAX_ROWS
        events = events[:MAX_ROWS]
        items = {i.id: i for i in s.exec(select(PriceReviewItem).where(
            PriceReviewItem.id.in_({e.item_id for e in events}))).all()} if events else {}  # type: ignore[union-attr]
        for ev in events:
            data = pr._loads(ev.payload) or {}
            action = str(data.get("accion") or "")
            rule, run = data.get("regla"), ev.run_id if ev.run_id is not None else data.get("corrida")
            deduced = False
            if not rule or run is None:
                old_rule, old_run = _deduced(s, ev)
                deduced = bool(not rule and old_rule)
                rule, run = rule or old_rule, run if run is not None else old_run
            undone = _undone_by(s, ev)
            turned_on = bool(ev.enabled_after)
            totals["activados" if turned_on else "apagados"] += 1
            totals["deshechos_por_una_persona"] += undone is not None
            if len(out) >= limit:
                continue
            item = items.get(ev.item_id)
            out.append({
                "at": _iso(ev.at),
                "product_id": ev.product_id,
                "product_name": item.product_name if item is not None else None,
                "product_code": item.product_code if item is not None else None,
                "item_id": ev.item_id,
                "quedo": "activo" if turned_on else "inactivo",
                "accion": action or None,
                "accion_texto": ACTION_TEXT.get(action),
                "regla": rule,
                "regla_texto": RULE_TEXT.get(str(rule)) if rule else None,
                "regla_deducida": deduced,
                "corrida": run,
                "conciliado": bool(data.get("conciliado")),
                "deshecho_por": undone.actor if undone is not None else None,
                "deshecho_at": _iso(undone.at) if undone is not None else None,
            })
        autopilot = _autopilot(s)
    return {"days": days, "since": _iso(since), "totals": totals, "truncated": truncated, "shown": len(out),
            "piloto_automatico": autopilot, "items": out}


def _autopilot(s: Session) -> dict[str, Any]:
    """Qué dejó anotado el piloto automático: cuándo pasó a modo automático (y desde qué modo) y con qué corridas corrió
    el portón al arrancar."""
    mode = s.get(Setting, _AUTOPILOT_MODE_MARKER)
    runs = s.exec(select(Setting).where(Setting.key.startswith(_AUTOPILOT_RUN_PREFIX))  # type: ignore[attr-defined]
                  .order_by(Setting.updated_at.desc()).limit(20)).all()  # type: ignore[union-attr]
    return {
        "modo_cambiado": (pr._loads(mode.value) if mode is not None else None),
        "corridas": [{"corrida": r.key[len(_AUTOPILOT_RUN_PREFIX):], "at": r.value} for r in runs],
    }
