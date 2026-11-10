"""Portón de precio (`pm_mode = 2`): automático cuando el semáforo compara firme contra Mercado Libre, y la persona manda.

Después de la corrida de la noche (y de la pasada de la bandeja), `process_run(run_id)` hace, con el snapshot de ESA
corrida y los topes diarios `pm_gate_inactivations_daily_max` / `pm_gate_activations_daily_max`, ROJOS PRIMERO:

  1. rojos firmes PUBLICADOS: cada ficha `propuesto` que abrió la pasada de esta noche (rojo firme con su racha, R1-R8)
     se apaga sola (`inactivar_auto` → `en_revision`, actor `hugo:auto`, con Deshacer), del peor margen al mejor;
  2. lo que subió Paco por Luis (`luis_intake` con `vendure_product_id`, subido hace `pm_gate_max_age_days` días o menos),
     rojos primero:

        verde firme  → Hugo lo ACTIVA (ficha `por_activar` → `activado`). «Firme» = las mismas reglas que el rojo firme
                       de la bandeja (R1, R3-R7) con el color al revés, más el piso de precio de la reactivación automática.
        rojo firme   → queda inactivo (`gate_status = rojo_inactivo`); si tenía una ficha `por_activar`, se cierra sola.
        amarillo, sin dato o dudoso → bandeja de Pao: ficha `por_activar` con el motivo; Pao decide «Activar» o
                       «Despublicar». Si una noche siguiente da verde firme, Hugo la activa por el mismo camino.
  3. el resto del catálogo APAGADO (`pm_gate_catalog_greens = 1`): un producto deshabilitado que esta corrida midió en
     verde firme se activa solo, por el mismo camino y con su propio tope diario (`pm_gate_catalog_activations_daily_max`),
     del mejor margen al peor. No se prende lo que apagó el control de duplicados o de BX sin foto, ni lo que una persona
     despublicó o apagó viéndolo verde con el mismo precio. Un verde que no es firme (dudoso, precio bajo, ajuste
     aprendido) queda en revisión: ficha `por_activar` en la bandeja de Pao.

Antes de cualquier activación automática (2 y 3), con `pm_gate_measures_check = 1`, una IA barata revisa que las medidas
del producto, de la caja individual y de la caja master tengan sentido con el producto (pricing/measures_check.py). Si no
cierran, no hay medidas o no se pudieron chequear, el verde va a la bandeja de Pao con el motivo en vez de activarse.

La persona manda (pricing/semaforo_correcciones.py): si alguien PRENDE A MANO un producto que Hugo ve rojo, o APAGA uno que
ve verde (en Vendure, visto por la noche o por `on_vendure_events`; o desde la bandeja), Hugo NO lo revierte: lo anota en
`semaforo_correcciones` y deja el producto fuera de las acciones automáticas `pm_correction_hold_days` días, salvo que
cambie su precio. Además no pelea con una decisión de la bandeja reciente (Mantener, Activar, Reactivar o Deshacer en los
últimos 30 días).

Los ajustes aprendidos de las correcciones (pricing/semaforo_aprende.py, los «aplicados») solo frenan: un caso que cae en un
segmento ajustado y no pasa el corte de ese segmento va a la bandeja en vez de que Hugo actúe solo.

No recalcula precios y no escribe en Vendure por su cuenta: toda escritura va por `price_review_actions` (intención,
guardas, un solo intento, verificación, conciliación, evento, AuditLog y Deshacer). Con `pm_mode` distinto de 2 no hace
nada.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import runtime
from app.clock import utcnow
from app.db.models import (
    AuditLog,
    LuisIntake,
    MarketPriceSnapshot,
    PriceReviewEvent,
    PriceReviewItem,
)
from app.db.session import engine
from app.pricing import daily_budget, semaforo
from app.pricing import price_review as pr
from app.pricing import semaforo_aprende as learn
from app.pricing import semaforo_correcciones as corr
from app.pricing.safe_error import error_code

log = logging.getLogger(__name__)

ACTIVATIONS_KEY = "pm_gate_activations"
CATALOG_ACTIVATIONS_KEY = "pm_gate_catalog_activations"
INACTIVATIONS_KEY = "pm_gate_inactivations"
READS_KEY = "pm_gate_reads"

# Qué decidió el portón (columna `luis_intake.gate_status`).
ACTIVAR = "activar"
BANDEJA = "bandeja"
ROJO = "rojo_inactivo"
G_ACTIVADO, G_BANDEJA, G_ROJO, G_YA_ACTIVO = "activado", "bandeja", "rojo_inactivo", "ya_activo"
# Una persona lo apagó después de que Hugo lo activara (corrección: Hugo no lo vuelve a prender).
G_APAGADO_A_MANO = "apagado_a_mano"

# Una persona decidió algo «a favor» del producto en este tiempo: Hugo no lo vuelve a apagar ni lo prende.
HUMAN_HOLD = timedelta(days=30)
_PRO_ACTIVE_KINDS = ("decision_mantener", "decision_activar", "decision_reactivar", "deshacer")
MAX_PER_RUN = 500

REASON_TEXT = {
    "amarillo": "Amarillo: el revendedor gana entre el corte amarillo y el verde",
    "sin_dato": "Sin dato: no hay idénticos con precio en Mercado Libre",
    "precio_bajo": "Verde firme, pero nuestro precio quedó demasiado bajo contra ML (revisar que no sea un error de carga)",
    "tope_del_dia": "Verde firme, pero se llegó al tope de activaciones del día: se activa mañana o a mano",
    "rojo_prendido_a_mano": "Rojo firme y una persona lo prendió a mano: Hugo no lo revierte (corrección anotada)",
    "verde_apagado_a_mano": "Verde firme y una persona lo apagó a mano: Hugo no lo vuelve a prender (corrección anotada)",
    "rojo_firme_publicado": "Rojo firme publicado: Hugo lo apagó solo (modo automático)",
    "corregido": "Una persona corrigió al semáforo hace poco: Hugo no actúa solo sobre este producto",
}


def enabled() -> bool:
    return int(runtime.get("pm_mode") or 0) == 2


@dataclass(slots=True, frozen=True)
class GateDecision:
    action: str                # activar | bandeja | rojo_inactivo
    reason: str                # código corto
    verdict: pr.FirmVerdict | None = None
    note: str = ""             # texto propio (la guarda de medidas)

    @property
    def text(self) -> str:
        if self.note:
            return self.note
        if self.reason in REASON_TEXT:
            return REASON_TEXT[self.reason]
        if self.reason.startswith("ajuste:"):
            return "Cae en un segmento ajustado por las correcciones de las personas: lo decide la bandeja"
        if self.reason.startswith(("verde_dudoso:", "rojo_dudoso:")):
            color, _, why = self.reason.partition("_dudoso:")
            return f"{color.capitalize()} pero dudoso: {pr.REASONS.get(why, why)}"
        return {"verde_firme": "Verde firme", "rojo_firme": "Rojo firme: queda inactivo"}.get(self.reason, self.reason)


def classify(snap: MarketPriceSnapshot | None, params: pr.ReviewParams, now: datetime | None = None,
             rules: list[learn.Rule] | None = None, category: str | None = None) -> GateDecision:
    """Qué hace el portón con este snapshot. PURA (sin red, sin base). `rules` = los ajustes aprendidos aplicados: un caso
    firme que cae en un segmento ajustado y no pasa su corte va a la bandeja (`ajuste:<id>`)."""
    if snap is None or snap.ml_status != "ok" or snap.color == semaforo.SIN_DATO:
        return GateDecision(BANDEJA, "sin_dato")
    now = now or utcnow()
    if snap.color == semaforo.VERDE:
        v = pr.evaluate_firm(snap, params, now, colors=(semaforo.VERDE,))
        if not v.firm:
            return GateDecision(BANDEJA, f"verde_dudoso:{v.reason}", v)
        if v.price_ratio is None or v.price_ratio < 1.0 / params.max_price_ratio:
            return GateDecision(BANDEJA, "precio_bajo", v)
        rule = _learned_block(rules, learn.VERDE, snap, params, now, category)
        if rule is not None:
            return GateDecision(BANDEJA, f"ajuste:{rule.id}", v)
        return GateDecision(ACTIVAR, "verde_firme", v)
    if snap.color == semaforo.ROJO:
        v = pr.evaluate_firm(snap, params, now, ignore_enabled=True)
        if v.firm:
            rule = _learned_block(rules, learn.ROJO, snap, params, now, category)
            if rule is not None:
                return GateDecision(BANDEJA, f"ajuste:{rule.id}", v)
            return GateDecision(ROJO, "rojo_firme", v)
        return GateDecision(BANDEJA, f"rojo_dudoso:{v.reason}", v)
    return GateDecision(BANDEJA, "amarillo")


def _learned_block(rules: list[learn.Rule] | None, direction: str, snap: MarketPriceSnapshot, params: pr.ReviewParams,
                   now: datetime, category: str | None) -> learn.Rule | None:
    if not rules:
        return None
    f = corr.figures(snap, params, now)
    f["category"] = category
    return learn.blocking_rule(rules, direction, f, params)


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


def _human_said_no(s: Session, product_id: str, now: datetime) -> bool:
    """¿Una persona lo despublicó desde la bandeja hace poco? Entonces Hugo no lo activa solo."""
    return s.exec(select(PriceReviewEvent.id).where(
        PriceReviewEvent.product_id == product_id, PriceReviewEvent.actor != pr.ACTOR_AUTO,
        PriceReviewEvent.kind == "decision_despublicar", PriceReviewEvent.at >= now - HUMAN_HOLD).limit(1)).first() is not None


def _held(s: Session, snap: MarketPriceSnapshot, now: datetime) -> bool:
    """¿Hugo tiene que quedarse quieto con este producto? Una decisión reciente de la bandeja a su favor, o una corrección
    humana vigente con el mismo precio."""
    return _human_hold(s, snap.product_id, now) or corr.held(s, snap.product_id, snap.our_price_cents, now)


def _writing(s: Session, product_id: str) -> bool:
    return any(ev.product_id == product_id for ev in pr.pending_intents(s))


def _hugo_turned_off(s: Session, product_id: str, now: datetime) -> bool:
    """¿Lo apagó Hugo (la bandeja, o el control de duplicados / BX sin foto en los últimos 2 días)? Entonces un apagado no
    es una persona corrigiendo al semáforo."""
    item = pr.open_item_for(s, product_id)
    if item is not None and item.hugo_disabled is True:
        return True
    if _writing(s, product_id):
        return True
    return s.exec(select(AuditLog.id).where(
        AuditLog.product_id == product_id,
        AuditLog.action.in_(("duplicate_disabled", "bx_no_image_disabled", "price_review_disabled")),  # type: ignore[attr-defined]
        AuditLog.created_at >= now - timedelta(days=2)).limit(1)).first() is not None


def _candidates(now: datetime) -> list[LuisIntake]:
    """Lo que subió Paco por Luis, reciente, que el portón todavía puede decidir (o que activó y hay que vigilar)."""
    max_age = timedelta(days=max(1, int(runtime.get("pm_gate_max_age_days") or 30)))
    with Session(engine, expire_on_commit=False) as s:
        rows = s.exec(select(LuisIntake).where(
            LuisIntake.vendure_product_id.is_not(None),  # type: ignore[union-attr]
            LuisIntake.uploaded_at >= now - max_age,  # type: ignore[operator]
        ).order_by(LuisIntake.id).limit(MAX_PER_RUN)).all()
    return [r for r in rows if r.gate_status in (None, G_BANDEJA, G_ROJO, G_ACTIVADO)]


def _reds_first(rows: list[LuisIntake], run_id: int) -> list[LuisIntake]:
    """Los candidatos con los rojos de esta corrida primero (si se corta la pasada, lo urgente ya se hizo)."""
    ids = [str(r.vendure_product_id) for r in rows]
    if not ids:
        return rows
    with Session(engine) as s:
        reds = set(s.exec(select(MarketPriceSnapshot.product_id).where(
            MarketPriceSnapshot.run_id == run_id, MarketPriceSnapshot.color == semaforo.ROJO,
            MarketPriceSnapshot.product_id.in_(ids))).all())  # type: ignore[attr-defined]
    return sorted(rows, key=lambda r: (str(r.vendure_product_id) not in reds, r.id or 0))


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


def _record(snap: MarketPriceSnapshot, decision: str, now: datetime) -> None:
    """Una corrección vista en Vendure (no sabemos quién fue). Nunca lanza."""
    try:
        corr.record(snap, decision, actor=corr.ORIGIN_VENDURE, origin=corr.ORIGIN_VENDURE, now=now)
    except Exception:
        log.exception("portón: no se pudo anotar la corrección de %s", snap.product_id)


def _bump(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


# ─── La noche ─────────────────────────────────────────────────────


async def process_run(run_id: int, *, now: datetime | None = None, client: Any = None,
                      immediate_reds: bool = False) -> dict[str, int]:
    """El portón con los snapshots de la corrida `run_id`: primero los rojos firmes publicados, después lo que subió Paco
    (rojos primero) y al final el resto del catálogo apagado que da verde firme (`pm_gate_catalog_greens`). Nunca lanza;
    devuelve contadores.

    `immediate_reds` (el botón «Aplicar ahora» del dashboard): también apaga los rojos firmes publicados que todavía no
    cumplieron la racha de 2 noches (R8). La noche nunca lo usa."""
    if not enabled():
        return {}
    async with gate_lock:                   # la noche y «Aplicar ahora» no corren a la vez
        return await _process_run(run_id, now=now, client=client, immediate_reds=immediate_reds)


# Una pasada del portón a la vez (la de la noche o la del botón).
gate_lock = asyncio.Lock()


async def _process_run(run_id: int, *, now: datetime | None, client: Any, immediate_reds: bool) -> dict[str, int]:
    counts: dict[str, int] = {}
    now = now or utcnow()
    params = pr.ReviewParams.from_runtime()
    rules = await asyncio.to_thread(learn.active_rules)
    try:
        reds = await asyncio.to_thread(_published_reds, run_id)
        if immediate_reds:
            reds = await asyncio.to_thread(_add_unstreaked_reds, reds, run_id, params, now)
    except Exception as exc:  # noqa: BLE001
        log.warning("portón #%s: no se pudieron leer los rojos publicados (%s)", run_id, error_code(exc))
        reds = []
    for snap in reds:
        try:
            outcome = await _published_red(snap, run_id, params, rules, now, client)
        except Exception as exc:  # noqa: BLE001  (un producto no frena a los demás)
            log.warning("portón #%s: el rojo publicado %s falló (%s)", run_id, snap.product_id, error_code(exc))
            outcome = "error"
        _bump(counts, outcome)
        if not enabled():                      # interruptor de emergencia: bajar el modo corta la pasada
            return counts
    try:
        rows = await asyncio.to_thread(_candidates, now)
        rows = await asyncio.to_thread(_reds_first, rows, run_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("portón #%s: no se pudieron leer los candidatos (%s)", run_id, error_code(exc))
        return counts
    for row in rows:
        try:
            outcome = await _one(row, run_id, params, rules, now, client)
        except Exception as exc:  # noqa: BLE001  (un producto no frena a los demás)
            log.warning("portón #%s: el producto %s falló (%s)", run_id, row.vendure_product_id, error_code(exc))
            outcome = "error"
        if outcome:
            _bump(counts, outcome)
        if not enabled():
            return counts
    if int(runtime.get("pm_gate_catalog_greens") or 0) == 1:
        try:
            greens = await asyncio.to_thread(_catalog_greens, run_id, {str(r.vendure_product_id) for r in rows})
        except Exception as exc:  # noqa: BLE001
            log.warning("portón #%s: no se pudieron leer los verdes apagados (%s)", run_id, error_code(exc))
            greens = []
        for snap in greens:
            try:
                outcome = await _catalog_green(snap, run_id, params, rules, now, client)
            except Exception as exc:  # noqa: BLE001
                log.warning("portón #%s: el verde apagado %s falló (%s)", run_id, snap.product_id, error_code(exc))
                outcome = "error"
            if outcome:
                _bump(counts, f"catalogo_{outcome}")
            if not enabled() or outcome == "tope":
                break
    if counts:
        log.info("portón #%s: %s", run_id, counts)
    return counts


def _published_reds(run_id: int) -> list[MarketPriceSnapshot]:
    """Los rojos publicados que la pasada de esta noche dejó en `propuesto` (rojo firme con su racha), del peor margen al
    mejor."""
    out: list[MarketPriceSnapshot] = []
    with Session(engine, expire_on_commit=False) as s:
        items = s.exec(select(PriceReviewItem).where(PriceReviewItem.state == pr.PROPUESTO,
                                                     PriceReviewItem.last_run_id == run_id)
                       .order_by(PriceReviewItem.id).limit(MAX_PER_RUN)).all()
        for item in items:
            snap = s.get(MarketPriceSnapshot, item.last_snapshot_id) if item.last_snapshot_id else None
            if (snap is not None and snap.run_id == run_id and snap.product_enabled
                    and snap.color == semaforo.ROJO and snap.review_flag == pr.FIRME):
                out.append(snap)
    return sorted(out, key=lambda sn: (sn.est_margin_pct if sn.est_margin_pct is not None else 0.0, sn.id or 0))


def _add_unstreaked_reds(reds: list[MarketPriceSnapshot], run_id: int, params: pr.ReviewParams,
                         now: datetime) -> list[MarketPriceSnapshot]:
    """Los rojos publicados de la corrida que son firmes (R1, R3-R7) aunque no tengan todavía la racha de 2 noches, sumados
    a los de la racha. Solo para «Aplicar ahora»."""
    have = {sn.product_id for sn in reds}
    extra: list[MarketPriceSnapshot] = []
    with Session(engine, expire_on_commit=False) as s:
        snaps = s.exec(select(MarketPriceSnapshot).where(
            MarketPriceSnapshot.run_id == run_id, MarketPriceSnapshot.color == semaforo.ROJO,
            MarketPriceSnapshot.product_enabled == True)  # noqa: E712
            .order_by(MarketPriceSnapshot.id).limit(MAX_PER_RUN * 4)).all()
    for snap in snaps:
        if snap.product_id in have:
            continue
        if pr.evaluate_firm(snap, params, now).firm:
            have.add(snap.product_id)
            extra.append(snap)
    out = reds + extra
    return sorted(out, key=lambda sn: (sn.est_margin_pct if sn.est_margin_pct is not None else 0.0, sn.id or 0))


async def _published_red(snap: MarketPriceSnapshot, run_id: int, params: pr.ReviewParams, rules: list[learn.Rule],
                         now: datetime, client: Any) -> str:
    with Session(engine) as s:
        category = corr.category_of(s, snap.product_id)
    decision = classify(snap, params, now, rules, category)
    if decision.action != ROJO:
        return "rojo_publicado_a_bandeja"          # un ajuste aprendido lo frena: lo decide Pao
    done = await enforce_red(snap, "rojo_firme_publicado", run_id=run_id, now=now, client=client)
    return "apagado" if done else "rojo_sin_escribir"


async def _one(row: LuisIntake, run_id: int, params: pr.ReviewParams, rules: list[learn.Rule], now: datetime,
               client: Any) -> str:
    pid = str(row.vendure_product_id)
    with Session(engine, expire_on_commit=False) as s:
        snap = _latest_snapshot(s, pid, run_id)
        if snap is None:
            return "sin_medicion"
        if _writing(s, pid):
            return "escribiendo"
        hold = _held(s, snap, now)
        said_no = _human_said_no(s, pid, now)
        item = pr.open_item_for(s, pid)
        category = corr.category_of(s, pid)
        hugo_off = row.gate_status == G_ACTIVADO and not snap.product_enabled and _hugo_turned_off(s, pid, now)
    if row.gate_status == G_ACTIVADO:
        # Lo activó Hugo: solo se vigila. Si hoy está apagado y no lo apagó Hugo, lo apagó una persona: corrección.
        if snap.product_enabled or hugo_off or (row.gate_at is not None and snap.captured_at < row.gate_at):
            return ""
        await asyncio.to_thread(_record, snap, corr.INACTIVAR, now)
        text = REASON_TEXT["verde_apagado_a_mano"] if snap.color == semaforo.VERDE else \
            "Una persona lo apagó después de que Hugo lo activara"
        await asyncio.to_thread(_set_gate, row.id, G_APAGADO_A_MANO, text, now)
        return "apagado_a_mano"
    if item is not None and item.state not in (pr.POR_ACTIVAR,):
        return "ficha_de_una_persona"                    # ya la tiene la bandeja de rojos (o mantenida): no se toca
    decision = classify(snap, params, now, rules, category)
    if snap.product_enabled:
        if snap.color == semaforo.ROJO and row.gate_status in (G_ROJO, G_BANDEJA):
            # La persona manda: alguien lo prendió con rojo. Hugo no lo revierte; lo anota y no lo toca por un tiempo.
            await asyncio.to_thread(_record, snap, corr.ACTIVAR, now)
            if item is not None and item.state == pr.POR_ACTIVAR:
                await asyncio.to_thread(_close_gate_item, int(item.id), "Lo prendió una persona en Vendure.",  # type: ignore[arg-type]
                                        "ya_activo", now)
            await asyncio.to_thread(_set_gate, row.id, G_YA_ACTIVO, REASON_TEXT["rojo_prendido_a_mano"], now)
            return "corregido_a_mano"
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
    if decision.action == ACTIVAR and (hold or said_no):
        decision = GateDecision(BANDEJA, "corregido", decision.verdict)
    if decision.action == ACTIVAR:
        decision = await _measures_guard(snap, decision)
    if decision.action == ACTIVAR:
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
    # amarillo, sin dato, dudoso, ajuste aprendido o una persona en el medio: a la bandeja de Pao.
    await asyncio.to_thread(_ensure_item, snap, pr.POR_ACTIVAR, decision, now, run_id)
    await asyncio.to_thread(_set_gate, row.id, G_BANDEJA, decision.text, now)
    return "bandeja"


async def _measures_guard(snap: MarketPriceSnapshot, decision: GateDecision) -> GateDecision:
    """Un verde firme se activa solo si sus medidas (producto, caja y caja master) tienen sentido
    (pricing/measures_check.py, `pm_gate_measures_check`). Si no, va a la bandeja con el motivo."""
    from app.pricing import measures_check  # lazy: usa el cliente de la IA

    ok, code, text = await measures_check.gate_guard(snap)
    if ok:
        return decision
    return GateDecision(BANDEJA, code[:24], decision.verdict, note=text)


def _mark_snapshot_enabled(snap_id: int) -> None:
    with Session(engine) as s:
        snap = s.get(MarketPriceSnapshot, snap_id)
        if snap is not None:
            snap.product_enabled = True
            s.add(snap)
            s.commit()


# ─── El resto del catálogo apagado ────────────────────────────────


def _catalog_greens(run_id: int, skip: set[str]) -> list[MarketPriceSnapshot]:
    """Los productos APAGADOS que esta corrida midió en verde (sin los que ya decidió el paso de Luis), del mejor margen
    al peor: si se corta por el tope, se activaron los más claros."""
    with Session(engine, expire_on_commit=False) as s:
        snaps = s.exec(select(MarketPriceSnapshot).where(
            MarketPriceSnapshot.run_id == run_id, MarketPriceSnapshot.color == semaforo.VERDE,
            MarketPriceSnapshot.product_enabled == False)  # noqa: E712
            .order_by(MarketPriceSnapshot.id).limit(MAX_PER_RUN * 4)).all()
    out = [sn for sn in snaps if sn.product_id not in skip]
    return sorted(out, key=lambda sn: (-(sn.est_margin_pct or 0.0), sn.id or 0))


def _other_flow_off(s: Session, product_id: str) -> bool:
    """¿Lo apagó (o lo confirmó apagado) el control de duplicados o el de BX sin foto? Eso no lo revierte un verde."""
    return s.exec(select(AuditLog.id).where(or_(
        and_(AuditLog.product_id == product_id,
             AuditLog.action.in_(("duplicate_disabled", "bx_no_image_disabled"))),  # type: ignore[attr-defined]
        and_(AuditLog.disable_target_id == product_id, AuditLog.action == "duplicate_confirmed"))).limit(1)).first() \
        is not None


def _human_ever_said_no(s: Session, product_id: str, our_price_cents: int | None) -> bool:
    """¿Una persona lo despublicó alguna vez desde la bandeja, o le corrigió al semáforo un verde? Un producto viejo
    apagado a propósito no se prende solo."""
    if s.exec(select(PriceReviewEvent.id).where(
            PriceReviewEvent.product_id == product_id, PriceReviewEvent.actor != pr.ACTOR_AUTO,
            PriceReviewEvent.kind == "decision_despublicar").limit(1)).first() is not None:
        return True
    return corr.ever_turned_off(s, product_id, our_price_cents)


async def _catalog_green(snap: MarketPriceSnapshot, run_id: int, params: pr.ReviewParams, rules: list[learn.Rule],
                         now: datetime, client: Any) -> str:
    """Un producto apagado del catálogo con verde: si es verde firme y las medidas cierran, Hugo lo ACTIVA (ficha
    `por_activar` → `activado`, mismo protocolo, tope propio del catálogo). Un verde firme con medidas que no cierran, o un
    verde que no es firme, queda en revisión (bandeja de Pao) con el motivo."""
    pid = snap.product_id
    with Session(engine, expire_on_commit=False) as s:
        if _writing(s, pid):
            return "escribiendo"
        if _held(s, snap, now) or _human_ever_said_no(s, pid, snap.our_price_cents):
            return "corregido"
        if _other_flow_off(s, pid):
            return "duplicado_o_bx"
        item = pr.open_item_for(s, pid)
        category = corr.category_of(s, pid)
    if item is not None and item.state != pr.POR_ACTIVAR:
        return "ficha_de_una_persona"
    decision = classify(snap, params, now, rules, category)
    if decision.action != ACTIVAR:
        if decision.reason.startswith(("verde_dudoso:", "precio_bajo", "ajuste:")):
            # Verde pero no firme: no se sube solo, queda en revisión (bandeja de Pao) con el motivo.
            await asyncio.to_thread(_ensure_item, snap, pr.POR_ACTIVAR, decision, now, run_id)
            return "verde_dudoso_a_bandeja"
        return "no_firme"
    decision = await _measures_guard(snap, decision)
    if decision.action != ACTIVAR:
        await asyncio.to_thread(_ensure_item, snap, pr.POR_ACTIVAR, decision, now, run_id)
        return "medidas_a_bandeja"
    item = await asyncio.to_thread(_ensure_item, snap, pr.POR_ACTIVAR, decision, now, run_id)
    if item is None or item.state != pr.POR_ACTIVAR:
        return "sin_ficha"
    cap = int(runtime.get("pm_gate_catalog_activations_daily_max") or 0)
    if await asyncio.to_thread(daily_budget.reserve, CATALOG_ACTIVATIONS_KEY, cap, None, None,
                               daily_budget.today_art(now)) is None:
        return "tope"
    from app.pricing import (
        price_review_actions as actions,  # lazy: actions importa el cliente de Vendure
    )

    try:
        await actions.auto_gate(int(item.id), actions.ACTIVAR_AUTO, pr.POR_ACTIVAR, client=client)  # type: ignore[arg-type]
    except actions.ActionError as exc:
        log.info("portón: el verde apagado %s no se activó solo (%s)", pid, exc.code)
        return "activar_frenado"
    await asyncio.to_thread(_mark_snapshot_enabled, int(snap.id))  # type: ignore[arg-type]
    return "activado"


# ─── Apagar un rojo firme publicado ───────────────────────────────


async def enforce_red(snap: MarketPriceSnapshot, reason: str, *, run_id: int | None = None,
                      now: datetime | None = None, client: Any = None) -> bool:
    """Apaga un rojo firme publicado: ficha (la viva o una nueva `propuesto`) → `en_revision` por `inactivar_auto`. True si
    Vendure quedó apagado por esta llamada. Con tope diario. No hace nada si una persona corrigió al semáforo sobre este
    producto (con este precio) o decidió algo a su favor en la bandeja hace poco."""
    now = now or utcnow()
    with Session(engine) as s:
        if _held(s, snap, now) or _writing(s, snap.product_id):
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


# ─── Avisos de Vendure: ¿una persona corrigió al semáforo? ────────


# Referencias a las tareas de fondo de los avisos.
_TASKS: set[asyncio.Task] = set()
PRENDIDO, APAGADO = "prendido", "apagado"


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


def _suspects(product_ids: list[str], now: datetime, params: pr.ReviewParams) -> list[tuple[MarketPriceSnapshot, str]]:
    """De los avisados (sin leer Vendure), rojos primero: los que la última medición vio APAGADOS y en rojo firme (¿alguien
    los prendió?) y los que vio PRENDIDOS y en verde firme (¿alguien los apagó?). Se salta lo que ya tiene una corrección
    vigente con el mismo precio."""
    reds: list[tuple[MarketPriceSnapshot, str]] = []
    greens: list[tuple[MarketPriceSnapshot, str]] = []
    with Session(engine, expire_on_commit=False) as s:
        for pid in product_ids:
            snap = _latest_snapshot(s, pid)
            if snap is None or corr.held(s, pid, snap.our_price_cents, now):
                continue
            if snap.product_enabled is False and red_firm(snap, params, now):
                reds.append((snap, PRENDIDO))
            elif snap.product_enabled is True and snap.color == semaforo.VERDE and pr.evaluate_firm(
                    snap, params, now, colors=(semaforo.VERDE,)).firm:
                greens.append((snap, APAGADO))
    return reds + greens


def _turned_off_by_hugo(product_id: str, now: datetime) -> bool:
    with Session(engine) as s:
        return _hugo_turned_off(s, product_id, now)


async def on_vendure_events(product_ids: list[str], *, client: Any = None, now: datetime | None = None) -> dict[str, int]:
    """Un aviso de Vendure sobre un producto en rojo firme que Hugo vio apagado, o en verde firme que vio prendido: se lee
    su estado (una lectura por producto, con tope diario `pm_gate_reads_daily_max`) y, si una persona hizo lo contrario del
    semáforo, se anota la corrección (Hugo NO lo revierte). Nunca lanza."""
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
        for snap, expect in suspects:
            if await asyncio.to_thread(daily_budget.reserve, READS_KEY, cap, None, None,
                                       daily_budget.today_art(now)) is None:
                _bump(counts, "tope_lecturas")
                continue
            try:
                is_on = await client.get_enabled_status(snap.product_id)
            except Exception as exc:  # noqa: BLE001
                log.info("portón: no se pudo leer %s (%s)", snap.product_id, error_code(exc))
                _bump(counts, "error")
                continue
            if expect == PRENDIDO:
                if is_on is not True:
                    _bump(counts, "sigue_apagado")
                    continue
                await asyncio.to_thread(_record, snap, corr.ACTIVAR, now)
                _bump(counts, "corregido_a_mano")
                continue
            if is_on is not False:
                _bump(counts, "sigue_prendido")
                continue
            if await asyncio.to_thread(_turned_off_by_hugo, snap.product_id, now):
                _bump(counts, "apagado_por_hugo")
                continue
            await asyncio.to_thread(_record, snap, corr.INACTIVAR, now)
            _bump(counts, "corregido_a_mano")
    except Exception as exc:  # noqa: BLE001
        log.warning("portón: los avisos de Vendure fallaron (%s)", error_code(exc))
    if counts:
        log.info("portón (avisos): %s", counts)
    return counts
