"""Recálculo del semáforo y de la bandeja de Pao cuando un producto cambia en Vendure (decisiones del usuario del 29-oct).

`refresh_catalog` (cada pocos minutos) anota en vendure/catalog.py los productos que volvieron con otro `updatedAt`;
`enqueue` los encola y `run_pending` los recalcula:

    1. nunca con la corrida de la noche en curso: si arranca, lo que falta queda en la cola para la próxima vuelta;
    2. solo productos cuyo último snapshot tiene dato de ML `ok` de menos de `pm_review_data_max_age_h` horas (si no, nada:
       queda para la noche y el motivo va al log);
    3. UNA lectura por producto (`get_product_priced`), con tope diario propio (`pm_recalc_daily_max`, día ART) y
       concurrencia 2; nuestro precio sale de `semaforo.pick_our_price` con la `pm_tier_policy` vigente, como la noche;
    4. si el precio y el tramo son los del último snapshot no se hace nada (ni snapshot ni evento): un cambio de fotos,
       de descripción o el propio `enabled` que escribe Hugo no hacen ruido ni bucles;
    5. si cambió, el snapshot se recalcula EN EL LUGAR contra los MISMOS idénticos (misma mediana, comisión y envío):
       ganancia, color, marca de la bandeja y contadores de la corrida. `recalc_at` dice cuándo y `recalc_before` guarda
       lo que había medido la corrida. La corrida siguiente lo reemplaza: vuelve a ser la fuente;
    6. con `pm_mode >= 1`, la ficha viva del producto (si no tiene una escritura en curso): evento `recalculado` con el
       antes y el después, el `propuesto` que dejó de ser rojo se cierra solo, el `mantenido` que empeoró se reabre (regla
       del 10 %), el diagnóstico a-e se rehace (con el cupo de lecturas de la bandeja) y lo que apagó Hugo y ya no es rojo
       FIRME se reactiva solo, por `price_review_actions` (mismo protocolo y guardas que «Ya lo corregí», actor
       `hugo:auto`) y con tope diario (`pm_recalc_reactivations_daily_max`).

Lo que NO hace: no busca de nuevo en Mercado Libre, no abre fichas (un producto que empeora sin ficha se ve en el semáforo
y sigue con la racha de la noche) y no apaga nada. Lo único que escribe en Vendure es la reactivación, y la escribe
`price_review_actions`, como siempre.

La cola vive en memoria: si el proceso se reinicia, lo que estaba en la cola lo toma la corrida de la noche.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from sqlmodel import Session, select

from app import runtime
from app.clock import utcnow
from app.db.models import MarketPriceSnapshot, PriceMonitorRun, PriceReviewItem
from app.db.session import engine
from app.pricing import daily_budget, price_monitor, semaforo, store_match
from app.pricing import price_review as pr
from app.pricing.safe_error import error_code
from app.vendure.client import VendureClient, VendureProduct

log = logging.getLogger(__name__)

# Filas del día (ART) en `settings`, con reserva atómica (daily_budget).
READS_KEY = "pm_recalc_reads"
REACTIVATIONS_KEY = "pm_recalc_reactivations"
# Lecturas a Vendure a la vez: cada una es un N+1 de variantList del lado de Vendure (como el full refresh).
CONCURRENCY = 2
# La marca que deja en la ficha una reactivación que no se hizo por el tope del día.
FLAG_NOT_RED = "ya_no_rojo"

_pending: set[str] = set()


def enqueue(product_ids: Iterable[str]) -> None:
    _pending.update(product_ids)


@dataclass(slots=True)
class _Ctx:
    client: Any
    now: datetime
    day: str
    params: pr.ReviewParams
    mode: int
    tier_policy: int
    max_reads: int
    max_reactivations: int
    catalog: dict[str, VendureProduct] = field(default_factory=dict)
    to_diagnose: list[int] = field(default_factory=list)
    capped: bool = False                 # se llegó al tope del día: el resto (un cambio masivo) va directo a la noche


@dataclass(slots=True)
class _Change:
    """Lo que hay que hacer después de guardar: reactivar (ficha y estado esperado) o rehacer el diagnóstico."""
    reactivate: tuple[int, str] | None = None
    diagnose: int | None = None


async def run_pending(client: Any = None, now: datetime | None = None) -> dict[str, int]:
    """Recalcula lo que está en la cola. Devuelve cuántos productos terminaron en cada caso (también va al log). Lo que no
    se pudo por la corrida de la noche queda en la cola; lo demás sale (incluido lo que pasó el tope: eso es de la noche)."""
    if not _pending:
        return {}
    if price_monitor.price_monitor_lock.locked():
        return {"corrida": len(_pending)}
    ids = sorted(_pending)
    _pending.clear()
    now = now or utcnow()
    ctx = _Ctx(client=client or VendureClient(), now=now, day=daily_budget.today_art(now),
               params=pr.ReviewParams.from_runtime(), mode=int(runtime.get("pm_mode") or 0),
               tier_policy=int(runtime.get("pm_tier_policy")), max_reads=int(runtime.get("pm_recalc_daily_max")),
               max_reactivations=int(runtime.get("pm_recalc_reactivations_daily_max")))
    counts: dict[str, int] = {}
    sem = asyncio.Semaphore(CONCURRENCY)

    async def _one(pid: str) -> None:
        async with sem:
            try:
                outcome = await _recalc_one(ctx, pid)
            except Exception as exc:  # noqa: BLE001  (un producto no frena a los demás)
                log.warning("recálculo de %s falló: %s", pid, error_code(exc))
                outcome = "error"
            counts[outcome] = counts.get(outcome, 0) + 1

    await asyncio.gather(*(_one(pid) for pid in ids))
    if ctx.to_diagnose and not price_monitor.price_monitor_lock.locked():
        try:
            if done := await pr.rediagnose(ctx.to_diagnose, catalog=ctx.catalog, now=now):
                counts["diagnosticos"] = done
        except Exception as exc:  # noqa: BLE001
            log.warning("recálculo: el diagnóstico de las fichas falló: %s", error_code(exc))
    log.info("recálculo por cambios en Vendure: %d productos → %s", len(ids), counts)
    return counts


async def _recalc_one(ctx: _Ctx, pid: str) -> str:
    if price_monitor.price_monitor_lock.locked():
        _pending.add(pid)                       # arrancó la corrida de la noche: lo que falta queda en la cola
        return "corrida"
    if ctx.capped:
        return "tope"
    snap_id, reason = await asyncio.to_thread(_last_measured, pid, ctx)
    if reason == "corrida":
        _pending.add(pid)
    if snap_id is None:
        return reason
    if await asyncio.to_thread(daily_budget.reserve, READS_KEY, ctx.max_reads, None, None, ctx.day) is None:
        ctx.capped = True
        return "tope"
    product = await ctx.client.get_product_priced(pid)
    if product is None:
        return "no_existe"
    our = semaforo.pick_our_price(product.priced_variants or [], ctx.tier_policy)
    if our is None:
        return "sin_precio"
    if our.currency and our.currency.upper() != price_monitor.CURRENCY:
        return "moneda"
    ctx.catalog[pid] = product
    change = await asyncio.to_thread(_apply, ctx, snap_id, product, our)
    if change is None:
        return "sin_cambio"
    if change.reactivate is not None:
        await _reactivate(ctx, *change.reactivate)
    if change.diagnose is not None:
        ctx.to_diagnose.append(change.diagnose)
    return "recalculado"


def _last_measured(pid: str, ctx: _Ctx) -> tuple[int | None, str]:
    """El último snapshot del producto, si sirve para recalcular: ML `ok` y dato fresco (sin leer Vendure)."""
    with Session(engine) as s:
        snap = s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.product_id == pid)
                      .order_by(MarketPriceSnapshot.captured_at.desc(), MarketPriceSnapshot.id.desc())  # type: ignore[union-attr]
                      .limit(1)).first()
        if snap is None:
            return None, "sin_snapshot"
        run = s.get(PriceMonitorRun, snap.run_id)
        if run is not None and run.status == price_monitor.RUN_RUNNING:
            return None, "corrida"
        if snap.ml_status != price_monitor.OK:
            return None, "sin_dato_ml"
        data_at = pr._data_time(snap)
        if data_at is None or (ctx.now - data_at).total_seconds() / 3600.0 >= ctx.params.data_max_age_h:
            return None, "dato_viejo"
        return int(snap.id), ""  # type: ignore[arg-type]


def _apply(ctx: _Ctx, snap_id: int, product: VendureProduct, our: semaforo.OurPrice) -> _Change | None:
    """El snapshot recalculado, los contadores de la corrida y la ficha, en UNA transacción. None si no cambió nada."""
    with Session(engine, expire_on_commit=False) as s:
        snap = s.get(MarketPriceSnapshot, snap_id)
        if snap is None or (snap.our_price_cents, snap.tier_used) == (our.price_cents, our.tier_used):
            return None
        # Dos productos de la misma corrida se recalculan a la vez: sin este lock, en Postgres cada uno recontaría la
        # corrida sin ver el cambio del otro (READ COMMITTED) y el último commit dejaría los contadores mal.
        s.exec(select(PriceMonitorRun).where(PriceMonitorRun.id == snap.run_id).with_for_update()).first()
        old_price, old_color = snap.our_price_cents, snap.color
        ratio_before = pr.resale_ratio(snap, old_price)
        if snap.recalc_before is None:
            snap.recalc_before = json.dumps({
                "our_price_cents": old_price, "tier_used": snap.tier_used, "color": old_color,
                "est_margin_pct": snap.est_margin_pct, "review_flag": snap.review_flag})
        snap.our_price_cents, snap.tier_used, snap.variant_id = our.price_cents, our.tier_used, our.variant_id
        snap.product_enabled = bool(product.enabled)
        snap.recalc_at = ctx.now
        margin = semaforo.estimated_margin_pct(snap.ml_median_cents, our.price_cents, snap.commission_pct or 0.0,
                                               snap.shipping_cents or 0)
        snap.color = semaforo.color(margin, our.price_cents, snap.ml_median_cents, ctx.params.green_min,
                                    ctx.params.yellow_min)
        snap.est_margin_pct = None if margin is None else round(margin, 2)
        store_match.reapply_color(s, snap)          # con las tiendas contando, el color las incluye (como la noche)
        verdict = pr.evaluate_firm(snap, ctx.params, ctx.now)
        snap.review_flag = verdict.flag
        s.add(snap)
        price_monitor.recount_run(s, snap.run_id)
        change = _Change()
        if ctx.mode >= 1:
            _update_item(s, ctx, snap, verdict, change, old_price, old_color, ratio_before)
        s.commit()
    return change


def _update_item(s: Session, ctx: _Ctx, snap: MarketPriceSnapshot, verdict: pr.FirmVerdict, change: _Change,
                 old_price: int | None, old_color: str, ratio_before: float | None) -> None:
    """La ficha viva (abierta o `mantenido`) del producto. Sin ficha no se abre ninguna: eso es de la noche (R8)."""
    item = pr.open_item_for(s, snap.product_id)
    if item is None or pr.pending_intent(s, int(item.id)) is not None:   # type: ignore[arg-type]
        return                                   # una escritura en Vendure en curso: la ficha no se toca
    new_price = snap.our_price_cents or 0
    item.last_run_id, item.last_snapshot_id = snap.run_id, snap.id
    item.last_color, item.last_margin_pct = snap.color, snap.est_margin_pct
    pr.add_event(s, item, "recalculado", run_id=snap.run_id, snapshot_id=snap.id, now=ctx.now, payload={
        "precio_antes": old_price, "precio_despues": new_price, "color_antes": old_color, "color_despues": snap.color,
        "ratio_antes": ratio_before, "ratio_despues": pr.resale_ratio(snap, new_price),
        "sentido": "mejoro" if new_price < (old_price or 0) else "empeoro" if new_price > (old_price or 0) else "igual"})
    if item.state == pr.PROPUESTO:
        if snap.color != semaforo.ROJO:
            pr.close_item(s, item, note=f"Se corrigió en Vendure: ahora está en {snap.color}.",
                          code="corregido_en_vendure", run_id=snap.run_id, snapshot_id=snap.id, now=ctx.now)
            return
        item.flags = json.dumps(pr._flags_for(snap, verdict, ctx.params, item.diag_status))
    elif item.state == pr.MANTENIDO:
        # Regla del 10 % en el momento: la racha ya se cumplió cuando se abrió la ficha, no se vuelve a pedir.
        pr._watch_kept(s, item, snap, verdict, max(item.streak_nights, ctx.params.confirm_nights), ctx.params,
                       snap.run_id, ctx.now)
    elif item.state in (pr.EN_REVISION, pr.CORRIGIENDO) and item.hugo_disabled is True:
        flags = [f for f in (pr._loads(item.flags) or []) if f != FLAG_NOT_RED]
        item.flags = json.dumps(flags)
        if pr.evaluate_firm(snap, ctx.params, ctx.now, colors=pr.NOT_RED).firm:
            change.reactivate = (int(item.id), item.state)  # type: ignore[arg-type]
    s.add(item)
    if item.state in pr.OPEN_STATES:
        change.diagnose = int(item.id)  # type: ignore[arg-type]


AVISO_TOPE = {
    pr.CORRIGIENDO: "Ya no está en rojo: reactivala con «Ya lo corregí».",
    pr.EN_REVISION: "Ya no está en rojo: pasala a «Corregir» y reactivala con «Ya lo corregí».",
}


async def _reactivate(ctx: _Ctx, item_id: int, state: str) -> None:
    """La reactivación automática, con su tope del día. Las guardas, la escritura y lo que queda anotado (evento, AuditLog,
    «reactivar a mano» si algo frena) son de `price_review_actions`: este módulo no escribe en Vendure."""
    from app.pricing import price_review_actions as actions   # lazy: actions importa price_review y el cliente

    if await asyncio.to_thread(daily_budget.reserve, REACTIVATIONS_KEY, ctx.max_reactivations, None, None,
                               ctx.day) is None:
        await asyncio.to_thread(_warn_cap, item_id)
        return
    try:
        await actions.auto_reactivate(item_id, state)
    except actions.ActionError as exc:
        log.info("recálculo: la ficha #%s no se reactivó sola (%s)", item_id, exc.code)
    else:
        await asyncio.to_thread(_mark_enabled, item_id)


def _warn_cap(item_id: int) -> None:
    """Pasado el tope del día, la ficha queda con el aviso para reactivarla a mano."""
    with Session(engine) as s:
        item = s.get(PriceReviewItem, item_id)
        if item is None or item.state not in AVISO_TOPE:
            return
        item.flags = json.dumps([f for f in (pr._loads(item.flags) or []) if f != FLAG_NOT_RED] + [FLAG_NOT_RED])
        s.add(item)
        pr.add_event(s, item, "aviso_reactivar", motive_code="tope_del_dia", motive_text=AVISO_TOPE[item.state])
        s.commit()


def _mark_enabled(item_id: int) -> None:
    """El snapshot de la ficha reactivada pasa a habilitado (lo que ya dice Vendure), hasta que lo mida la noche."""
    with Session(engine) as s:
        item = s.get(PriceReviewItem, item_id)
        snap = s.get(MarketPriceSnapshot, item.last_snapshot_id) if item and item.last_snapshot_id else None
        if item is not None and item.state == pr.CORREGIDO and snap is not None:
            snap.product_enabled = True
            s.add(snap)
            s.commit()
