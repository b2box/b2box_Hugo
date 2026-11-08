"""Portón de precio (`pm_mode = 2`): solo queda publicado lo que se vende en Argentina a buen precio.

Paco sube a Vendure SIEMPRE inactivo. Con `pm_mode = 2`, después de la corrida de la noche (y de la pasada de la bandeja),
`process_run(run_id)` mira cada producto que subió Paco por Luis (`luis_intake` con `vendure_product_id`, subido hace
`pm_gate_max_age_days` días o menos) con el snapshot de ESA corrida:

    verde firme  → Hugo lo ACTIVA (ficha `por_activar` → `activado`, actor `hugo:auto`), con tope diario
                   `pm_gate_activations_daily_max`. «Firme» = las mismas reglas que el rojo firme de la bandeja (R1, R3-R7:
                   idénticos confirmados por foto + nombre o por una persona, dato de ML fresco, 3 publicaciones, 2
                   vendedores, precio creíble) con el color al revés, más el piso de precio de la reactivación automática
                   (nuestro precio / mediana >= 1 / `pm_review_max_price_ratio`).
    rojo firme   → queda inactivo (`gate_status = rojo_inactivo`); si tenía una ficha `por_activar`, se cierra sola.
    amarillo, sin dato o dudoso → bandeja de Pao: ficha `por_activar` con el motivo; Pao decide «Activar» o «Despublicar».
                   Si una noche siguiente da verde firme, Hugo la activa por el mismo camino.

Y `on_vendure_events(ids)` (avisos de `POST /api/vendure/events`): si alguien PRENDE A MANO un producto que la última
medición vio apagado y es rojo firme, Hugo lo vuelve a apagar (`inactivar_auto` → ficha `en_revision` con el motivo), con
tope `pm_gate_inactivations_daily_max` y lecturas acotadas por `pm_gate_reads_daily_max`. La noche hace lo mismo con lo que
el portón había dejado inactivo y aparece prendido.

Lo que NO hace: no toca productos que no subió Paco por Luis (salvo el caso «prendido a mano» de arriba), no pelea con una
persona (si alguien decidió Mantener, Activar, Reactivar o Deshacer sobre el producto en los últimos 30 días, no escribe), no
recalcula precios y no escribe en Vendure por su cuenta: toda escritura va por `price_review_actions` (intención, guardas,
un solo intento, verificación, conciliación, evento, AuditLog y Deshacer).

Con `pm_mode` distinto de 2 no hace nada.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import runtime
from app.clock import utcnow
from app.db.models import LuisIntake, MarketPriceSnapshot, PriceReviewEvent, PriceReviewItem
from app.db.session import engine
from app.pricing import daily_budget, semaforo
from app.pricing import price_review as pr
from app.pricing.safe_error import error_code

log = logging.getLogger(__name__)

ACTIVATIONS_KEY = "pm_gate_activations"
INACTIVATIONS_KEY = "pm_gate_inactivations"
READS_KEY = "pm_gate_reads"

# Qué decidió el portón (columna `luis_intake.gate_status`).
ACTIVAR = "activar"
BANDEJA = "bandeja"
ROJO = "rojo_inactivo"
G_ACTIVADO, G_BANDEJA, G_ROJO, G_YA_ACTIVO = "activado", "bandeja", "rojo_inactivo", "ya_activo"

# Una persona decidió algo «a favor» del producto en este tiempo: Hugo no lo vuelve a apagar ni lo prende.
HUMAN_HOLD = timedelta(days=30)
_PRO_ACTIVE_KINDS = ("decision_mantener", "decision_activar", "decision_reactivar", "deshacer")
MAX_PER_RUN = 500

REASON_TEXT = {
    "amarillo": "Amarillo: el revendedor gana entre el corte amarillo y el verde",
    "sin_dato": "Sin dato: no hay idénticos con precio en Mercado Libre",
    "precio_bajo": "Verde firme, pero nuestro precio quedó demasiado bajo contra ML (revisar que no sea un error de carga)",
    "tope_del_dia": "Verde firme, pero se llegó al tope de activaciones del día: se activa mañana o a mano",
    "rojo_prendido_a_mano": "Rojo firme y alguien lo prendió a mano: Hugo lo volvió a apagar",
}


def enabled() -> bool:
    return int(runtime.get("pm_mode") or 0) == 2


@dataclass(slots=True, frozen=True)
class GateDecision:
    action: str                # activar | bandeja | rojo_inactivo
    reason: str                # código corto
    verdict: pr.FirmVerdict | None = None

    @property
    def text(self) -> str:
        if self.reason in REASON_TEXT:
            return REASON_TEXT[self.reason]
        if self.reason.startswith(("verde_dudoso:", "rojo_dudoso:")):
            color, _, why = self.reason.partition("_dudoso:")
            return f"{color.capitalize()} pero dudoso: {pr.REASONS.get(why, why)}"
        return {"verde_firme": "Verde firme", "rojo_firme": "Rojo firme: queda inactivo"}.get(self.reason, self.reason)


def classify(snap: MarketPriceSnapshot | None, params: pr.ReviewParams, now: datetime | None = None) -> GateDecision:
    """Qué hace el portón con este snapshot. PURA (sin red, sin base)."""
    if snap is None or snap.ml_status != "ok" or snap.color == semaforo.SIN_DATO:
        return GateDecision(BANDEJA, "sin_dato")
    now = now or utcnow()
    if snap.color == semaforo.VERDE:
        v = pr.evaluate_firm(snap, params, now, colors=(semaforo.VERDE,))
        if not v.firm:
            return GateDecision(BANDEJA, f"verde_dudoso:{v.reason}", v)
        if v.price_ratio is None or v.price_ratio < 1.0 / params.max_price_ratio:
            return GateDecision(BANDEJA, "precio_bajo", v)
        return GateDecision(ACTIVAR, "verde_firme", v)
    if snap.color == semaforo.ROJO:
        v = pr.evaluate_firm(snap, params, now, ignore_enabled=True)
        if v.firm:
            return GateDecision(ROJO, "rojo_firme", v)
        return GateDecision(BANDEJA, f"rojo_dudoso:{v.reason}", v)
    return GateDecision(BANDEJA, "amarillo")


def red_firm(snap: MarketPriceSnapshot | None, params: pr.ReviewParams, now: datetime | None = None) -> bool:
    """¿Rojo firme aunque esté apagado? (para el «prendido a mano»). PURA."""
    return snap is not None and snap.color == semaforo.ROJO and pr.evaluate_firm(
        snap, params, now or utcnow(), ignore_enabled=True).firm


# ─── Base ──────────────────────────────────────────────────────────


def _latest_snapshot(s: Session, product_id: str, run_id: int | None = None) -> MarketPriceSnapshot | None:
    stmt = select(MarketPriceSnapshot).where(MarketPriceSnapshot.product_id == product_id)
    if run_id is not None:
        stmt = stmt.where(MarketPriceSnapshot.run_id == run_id)
    return s.exec(stmt.order_by(MarketPriceSnapshot.captured_at.desc(), MarketPriceSnapshot.id.desc())  # type: ignore[union-attr]
                  .limit(1)).first()


def _human_hold(s: Session, product_id: str, now: datetime) -> bool:
    """¿Una persona decidió algo a favor del producto hace poco? Entonces Hugo no escribe."""
    return s.exec(select(PriceReviewEvent.id).where(
        PriceReviewEvent.product_id == product_id, PriceReviewEvent.actor != pr.ACTOR_AUTO,
        PriceReviewEvent.kind.in_(_PRO_ACTIVE_KINDS),  # type: ignore[attr-defined]
        PriceReviewEvent.at >= now - HUMAN_HOLD).limit(1)).first() is not None


def _writing(s: Session, product_id: str) -> bool:
    return any(ev.product_id == product_id for ev in pr.pending_intents(s))


def _candidates(now: datetime) -> list[LuisIntake]:
    """Lo que subió Paco por Luis, reciente, que el portón todavía puede decidir."""
    max_age = timedelta(days=max(1, int(runtime.get("pm_gate_max_age_days") or 30)))
    with Session(engine, expire_on_commit=False) as s:
        rows = s.exec(select(LuisIntake).where(
            LuisIntake.vendure_product_id.is_not(None),  # type: ignore[union-attr]
            LuisIntake.uploaded_at >= now - max_age,  # type: ignore[operator]
        ).order_by(LuisIntake.id).limit(MAX_PER_RUN)).all()
    return [r for r in rows if r.gate_status in (None, G_BANDEJA, G_ROJO)]


def _set_gate(intake_id: int | None, status: str, reason: str, now: datetime) -> None:
    if intake_id is None:
        return
    with Session(engine) as s:
        row = s.get(LuisIntake, intake_id)
        if row is None:
            return
        row.gate_status, row.gate_reason, row.gate_at = status, reason[:300], now
        s.add(row)
        s.commit()


def _ensure_item(snap: MarketPriceSnapshot, state: str, decision: GateDecision, now: datetime,
                 run_id: int | None) -> PriceReviewItem | None:
    """La ficha viva del producto si ya hay una; si no, abre una en `state` con su evento `porton`. None si no se pudo."""
    with Session(engine, expire_on_commit=False) as s:
        current = pr.open_item_for(s, snap.product_id)
        if current is not None:
            return current
        v = decision.verdict
        item = PriceReviewItem(
            product_id=snap.product_id, product_name=snap.product_name, product_code=snap.product_code,
            product_slug=snap.product_slug, image_url=snap.product_image_url, open_key=snap.product_id, state=state,
            state_since=now, opened_at=now, mode_at_open=2, opened_run_id=run_id, opened_snapshot_id=snap.id,
            last_run_id=snap.run_id, last_snapshot_id=snap.id, last_color=snap.color, last_margin_pct=snap.est_margin_pct,
            our_price_cents=snap.our_price_cents,
            ml_median_cents=(v.firm_median if v is not None and v.firm_median else snap.ml_median_cents),
            firm_listing_count=v.firm_listings if v is not None else None,
            firm_seller_count=v.firm_sellers if v is not None else None,
            price_ratio=None if v is None or v.price_ratio is None else round(v.price_ratio, 3),
            reff_ratio=None if v is None or v.reff_ratio is None else round(v.reff_ratio, 3),
            flags=json.dumps([f"porton:{decision.reason}"]),
        )
        s.add(item)
        try:
            s.flush()
        except IntegrityError:
            s.rollback()
            return pr.open_item_for(s, snap.product_id)
        pr.add_event(s, item, "porton", to_state=state, run_id=run_id, snapshot_id=snap.id, now=now,
                     motive_code=decision.reason[:24], motive_text=decision.text,
                     payload={"color": snap.color, "margen_pct": snap.est_margin_pct, "modo": 2})
        s.commit()
        return item


def _close_gate_item(item_id: int, note: str, code: str, now: datetime) -> None:
    with Session(engine, expire_on_commit=False) as s:
        item = s.get(PriceReviewItem, item_id)
        if item is not None and item.state == pr.POR_ACTIVAR and pr.pending_intent(s, item_id) is None:
            pr.close_item(s, item, note=note, code=code, now=now)
            s.commit()


# ─── La noche ─────────────────────────────────────────────────────


async def process_run(run_id: int, *, now: datetime | None = None, client: Any = None) -> dict[str, int]:
    """El portón sobre lo que subió Paco, con los snapshots de la corrida `run_id`. Nunca lanza; devuelve contadores."""
    counts: dict[str, int] = {}
    if not enabled():
        return counts
    now = now or utcnow()
    params = pr.ReviewParams.from_runtime()
    try:
        rows = await asyncio.to_thread(_candidates, now)
    except Exception as exc:  # noqa: BLE001
        log.warning("portón #%s: no se pudieron leer los candidatos (%s)", run_id, error_code(exc))
        return counts
    for row in rows:
        try:
            outcome = await _one(row, run_id, params, now, client)
        except Exception as exc:  # noqa: BLE001  (un producto no frena a los demás)
            log.warning("portón #%s: el producto %s falló (%s)", run_id, row.vendure_product_id, error_code(exc))
            outcome = "error"
        counts[outcome] = counts.get(outcome, 0) + 1
        if not enabled():                      # interruptor de emergencia: bajar el modo corta la pasada
            break
    if counts:
        log.info("portón #%s: %s", run_id, counts)
    return counts


async def _one(row: LuisIntake, run_id: int, params: pr.ReviewParams, now: datetime, client: Any) -> str:
    pid = str(row.vendure_product_id)
    with Session(engine, expire_on_commit=False) as s:
        snap = _latest_snapshot(s, pid, run_id)
        if snap is None:
            return "sin_medicion"
        if _writing(s, pid):
            return "escribiendo"
        hold = _human_hold(s, pid, now)
        item = pr.open_item_for(s, pid)
    if item is not None and item.state not in (pr.POR_ACTIVAR,):
        return "ficha_de_una_persona"                    # ya la tiene la bandeja de rojos (o mantenida): no se toca
    decision = classify(snap, params, now)
    if snap.product_enabled:
        if decision.action == ROJO and row.gate_status in (G_ROJO, G_BANDEJA) and not hold:
            done = await enforce_red(snap, "rojo_prendido_a_mano", run_id=run_id, now=now, client=client)
            if done:
                await asyncio.to_thread(_set_gate, row.id, G_ROJO, REASON_TEXT["rojo_prendido_a_mano"], now)
            return "reapagado" if done else "rojo_prendido_sin_escribir"
        if item is not None and item.state == pr.POR_ACTIVAR:
            await asyncio.to_thread(_close_gate_item, int(item.id), "Lo prendió una persona en Vendure.",  # type: ignore[arg-type]
                                    "ya_activo", now)
        if row.gate_status is None or item is not None:
            await asyncio.to_thread(_set_gate, row.id, G_YA_ACTIVO, "Ya estaba activo: lo prendió una persona", now)
        return "ya_activo"
    if decision.action == ROJO:
        if item is not None:
            await asyncio.to_thread(_close_gate_item, int(item.id), "Rojo firme: queda inactivo.", "rojo_firme", now)  # type: ignore[arg-type]
        await asyncio.to_thread(_set_gate, row.id, G_ROJO, decision.text, now)
        return "rojo_inactivo"
    if decision.action == ACTIVAR and not hold:
        item = await asyncio.to_thread(_ensure_item, snap, pr.POR_ACTIVAR, decision, now, run_id)
        if item is None or item.state != pr.POR_ACTIVAR:
            return "sin_ficha"
        cap = int(runtime.get("pm_gate_activations_daily_max") or 0)
        if await asyncio.to_thread(daily_budget.reserve, ACTIVATIONS_KEY, cap, None, None,
                                   daily_budget.today_art(now)) is None:
            await asyncio.to_thread(_set_gate, row.id, G_BANDEJA, REASON_TEXT["tope_del_dia"], now)
            return "tope"
        from app.pricing import (
            price_review_actions as actions,  # lazy: actions importa el cliente de Vendure
        )

        try:
            await actions.auto_gate(int(item.id), actions.ACTIVAR_AUTO, pr.POR_ACTIVAR, client=client)  # type: ignore[arg-type]
        except actions.ActionError as exc:
            log.info("portón: %s no se activó solo (%s)", pid, exc.code)
            await asyncio.to_thread(_set_gate, row.id, G_BANDEJA, f"Verde firme, pero no se activó: {exc.message}", now)
            return "activar_frenado"
        await asyncio.to_thread(_set_gate, row.id, G_ACTIVADO, decision.text, now)
        await asyncio.to_thread(_mark_snapshot_enabled, int(snap.id))  # type: ignore[arg-type]
        return "activado"
    # amarillo, sin dato, dudoso (o verde con una persona en el medio): a la bandeja de Pao.
    await asyncio.to_thread(_ensure_item, snap, pr.POR_ACTIVAR, decision, now, run_id)
    await asyncio.to_thread(_set_gate, row.id, G_BANDEJA, decision.text, now)
    return "bandeja"


def _mark_snapshot_enabled(snap_id: int) -> None:
    with Session(engine) as s:
        snap = s.get(MarketPriceSnapshot, snap_id)
        if snap is not None:
            snap.product_enabled = True
            s.add(snap)
            s.commit()


# ─── Rojo firme prendido a mano ───────────────────────────────────


async def enforce_red(snap: MarketPriceSnapshot, reason: str, *, run_id: int | None = None,
                      now: datetime | None = None, client: Any = None) -> bool:
    """Vuelve a apagar un rojo firme que alguien prendió a mano: ficha (la viva o una nueva `propuesto`) → `en_revision`
    por `inactivar_auto`. True si Vendure quedó apagado por esta llamada. Con tope diario."""
    now = now or utcnow()
    with Session(engine) as s:
        if _human_hold(s, snap.product_id, now) or _writing(s, snap.product_id):
            return False
        current = pr.open_item_for(s, snap.product_id)
    if current is not None and current.state not in (pr.PROPUESTO, pr.POR_ACTIVAR):
        return False                                       # una persona ya tiene la ficha en sus manos
    decision = GateDecision(ROJO, reason, None)
    item = current or await asyncio.to_thread(_ensure_item, snap, pr.PROPUESTO, decision, now, run_id)
    if item is None or item.state not in (pr.PROPUESTO, pr.POR_ACTIVAR):
        return False
    cap = int(runtime.get("pm_gate_inactivations_daily_max") or 0)
    if await asyncio.to_thread(daily_budget.reserve, INACTIVATIONS_KEY, cap, None, None,
                               daily_budget.today_art(now)) is None:
        log.info("portón: tope de inactivaciones del día; %s queda en la bandeja", snap.product_id)
        return False
    from app.pricing import (
        price_review_actions as actions,  # lazy: actions importa el cliente de Vendure
    )

    with Session(engine) as s:
        it = s.get(PriceReviewItem, item.id)
        if it is not None:
            pr.add_event(s, it, "porton", motive_code=reason[:24], motive_text=REASON_TEXT.get(reason, reason), now=now)
            s.commit()
    try:
        out = await actions.auto_gate(int(item.id), actions.INACTIVAR_AUTO, item.state, client=client)  # type: ignore[arg-type]
    except actions.ActionError as exc:
        log.info("portón: %s no se apagó solo (%s)", snap.product_id, exc.code)
        return False
    return bool(out.get("wrote"))


# Referencias a las tareas de fondo de los avisos.
_TASKS: set[asyncio.Task] = set()


def notify(product_ids: Iterable[str]) -> None:
    """Desde `POST /api/vendure/events`: corre `on_vendure_events` en segundo plano (solo con `pm_mode = 2`)."""
    if not enabled():
        return
    ids = list(dict.fromkeys(product_ids))[:200]
    if not ids:
        return
    task = asyncio.get_running_loop().create_task(on_vendure_events(ids))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def _suspects(product_ids: list[str], now: datetime, params: pr.ReviewParams) -> list[MarketPriceSnapshot]:
    """De los avisados: los que la última medición vio APAGADOS y son rojo firme (sin leer Vendure)."""
    out: list[MarketPriceSnapshot] = []
    with Session(engine, expire_on_commit=False) as s:
        for pid in product_ids:
            snap = _latest_snapshot(s, pid)
            if snap is not None and snap.product_enabled is False and red_firm(snap, params, now):
                out.append(snap)
    return out


async def on_vendure_events(product_ids: list[str], *, client: Any = None, now: datetime | None = None) -> dict[str, int]:
    """Un aviso de Vendure sobre un producto que Hugo vio apagado y en rojo firme: se lee si quedó prendido (una lectura por
    producto, con tope diario) y, si sí, se vuelve a apagar. Nunca lanza."""
    counts: dict[str, int] = {}
    if not enabled():
        return counts
    now = now or utcnow()
    try:
        params = pr.ReviewParams.from_runtime()
        suspects = await asyncio.to_thread(_suspects, product_ids, now, params)
        if not suspects:
            return counts
        if client is None:
            from app.vendure.client import VendureClient
            client = VendureClient()
        cap = int(runtime.get("pm_gate_reads_daily_max") or 0)
        for snap in suspects:
            if await asyncio.to_thread(daily_budget.reserve, READS_KEY, cap, None, None,
                                       daily_budget.today_art(now)) is None:
                counts["tope_lecturas"] = counts.get("tope_lecturas", 0) + 1
                continue
            try:
                is_on = await client.get_enabled_status(snap.product_id)
            except Exception as exc:  # noqa: BLE001
                log.info("portón: no se pudo leer %s (%s)", snap.product_id, error_code(exc))
                counts["error"] = counts.get("error", 0) + 1
                continue
            if is_on is not True:
                counts["sigue_apagado"] = counts.get("sigue_apagado", 0) + 1
                continue
            done = await enforce_red(snap, "rojo_prendido_a_mano", now=now, client=client)
            key = "reapagado" if done else "sin_escribir"
            counts[key] = counts.get(key, 0) + 1
    except Exception as exc:  # noqa: BLE001
        log.warning("portón: los avisos de Vendure fallaron (%s)", error_code(exc))
    if counts:
        log.info("portón (avisos): %s", counts)
    return counts
