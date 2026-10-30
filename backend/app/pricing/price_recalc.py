"""Recálculo del semáforo y de la bandeja de Pao cuando un producto cambia en Vendure (decisiones del usuario del 29-oct).

`refresh_catalog` (cada pocos minutos) anota en vendure/catalog.py los productos que volvieron con otro `updatedAt` o
que tienen una variante con `updatedAt` nuevo; `enqueue` los encola y `run_pending` los recalcula. Los tramos son otra
entidad y guardarlos no toca ni la variante ni el producto: por eso `watch_live_items` (job cada 30 min) relee además el
precio de los productos con ficha viva o `mantenido` (hasta 60 por vuelta, los que se miraron hace más tiempo primero, de
a 2, con su propio cupo diario `pm_recalc_watch_daily_max`; no corre con `pm_mode = 0` ni con `pm_recalc_daily_max = 0`).
Un cambio de tramos de un producto sin ficha lo toma la noche. Los dos caminos recalculan igual:

    1. nunca con la corrida de la noche en curso: si arranca, lo que falta queda en la cola para la próxima vuelta;
    2. solo productos cuyo último snapshot tiene dato de ML `ok` de menos de `pm_review_data_max_age_h` horas (si no, nada:
       queda para la noche y el motivo va al log);
    3. UNA lectura por producto (`get_product_priced`), con tope diario (`pm_recalc_daily_max` la cola, el suyo el vigía,
       día ART) y concurrencia 2; nuestro precio sale de `semaforo.pick_our_price` con la `pm_tier_policy` vigente;
    4. si el precio y el tramo son los del último snapshot (con esa política o con la otra: cambiar `pm_tier_policy` es
       configuración, no un arreglo) el snapshot no se toca y no hay evento; la ficha que apagó Hugo igual se vuelve a mirar
       (paso 6), porque esa lectura puede ser la segunda que confirma el «no rojo»;
    5. si cambió, el snapshot se recalcula EN EL LUGAR contra los MISMOS idénticos (misma mediana, comisión y envío):
       ganancia, color y contadores de la corrida. `recalc_at` dice cuándo, `recalc_before` guarda lo que había medido la
       corrida y `recalc_flag` el veredicto de la bandeja con el precio nuevo. `review_flag` NO se toca: es la medición de
       la noche y de ahí sale la racha (R8). La corrida siguiente lo reemplaza: vuelve a ser la fuente;
    6. con `pm_mode >= 1`, la ficha viva del producto (si no tiene una escritura en curso): evento `recalculado` con el
       antes y el después, el `propuesto` que dejó de ser rojo se cierra solo, el `mantenido` que empeoró se reabre (regla
       del 10 %), el diagnóstico a-e se rehace (con un sub-cupo de las lecturas de la bandeja) y lo que apagó Hugo y ya no
       es rojo FIRME (`_decide`): desde `corrigiendo`, con piso de precio y el mismo precio en dos lecturas separadas por
       30 min, se reactiva solo por `price_review_actions` (mismo protocolo y guardas que «Ya lo corregí», actor
       `hugo:auto`, tope `pm_recalc_reactivations_daily_max`); desde `en_revision` solo queda el aviso.

Lo que NO hace: no busca de nuevo en Mercado Libre, no abre fichas (un producto que empeora sin ficha se ve en el semáforo
y sigue con la racha de la noche) y no apaga nada. Lo único que escribe en Vendure es la reactivación, y la escribe
`price_review_actions`, como siempre.

Además Vendure puede AVISAR al guardar (api/vendure_events_routes.py, `POST /api/vendure/events`): `notify` anota cada
producto para leerlo a los ~20 s (Vendure termina de recalcular el precio de venta después de guardar) y, si un aviso de
tramo o de variante dio «sin cambio», una vez más a los ~2 min. Pasa por la misma cola, con los mismos topes, locks y
reglas; un aviso que llega durante la corrida de la noche se procesa cuando termina. El sondeo de arriba queda de respaldo.

La cola y los avisos viven en memoria: si el proceso se reinicia, lo que estaba ahí lo toma la corrida de la noche.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlmodel import Session, func, select

from app import runtime
from app.clock import utcnow
from app.db.models import AuditLog, MarketPriceSnapshot, PriceMonitorRun, PriceReviewEvent, PriceReviewItem
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
# La marca de la ficha «ya no está en rojo: reactivala a mano» (vive en price_review: la noche también la limpia).
FLAG_NOT_RED = pr.FLAG_NOT_RED
# Lecturas del vigía por día (ART): contador propio, aparte del de la cola.
WATCH_READS_KEY = "pm_recalc_watch_reads"
# Doble lectura (ajustes 30-oct): Hugo reactiva solo si el «no rojo» se vio en dos lecturas de Vendure con el mismo precio
# separadas por al menos esto. La primera es la que trajo ese precio (`recalc_at`, o la corrida); la segunda, en general,
# la del vigía de 30 min.
CONFIRM_AFTER = timedelta(minutes=30)
# Piso de precio para reactivar solo: el precio nuevo no puede haber bajado más que esto contra el de la noche.
MAX_DROP = 0.5
# El vigía de las fichas vivas (R-1b): cada cuánto y cuántos productos por vuelta.
WATCH_JOB_ID = "price_recalc_watch"
WATCH_EVERY_MIN = 30
WATCH_MAX = 60

_pending: set[str] = set()
# La cola y el vigía no recalculan a la vez (el mismo producto podría entrar por los dos caminos).
_busy = asyncio.Lock()
# Cuándo releyó el vigía el precio de cada producto.
# shortcut: en memoria; después de un reinicio la primera vuelta va por la ficha más vieja. Si hubiera más de 60 fichas
# vivas y reinicios seguidos, pasarlo a una columna de la ficha.
_watched_at: dict[str, datetime] = {}


# Tope duro de lo que espera en memoria (cola + avisos): lo que sobra se descarta con un aviso y lo toma la noche.
MAX_QUEUED = 20_000
# Un lote de más de esto es un cambio masivo (una fórmula nueva, una importación): se leen solo las fichas vivas.
MASSIVE = 200
# Con qué `updatedAt` (el más nuevo, del producto o de sus variantes) se leyó cada producto la última vez: el sondeo no
# relee lo que un aviso ya leyó igual o después.
_read_marks: dict[str, str] = {}


def _room() -> int:
    return max(0, MAX_QUEUED - len(_pending) - len(_notices))


def enqueue(product_ids: Iterable[str]) -> int:
    """Encola productos para recalcular, sin pasar del tope duro. Devuelve cuántos entraron (los ya encolados cuentan)."""
    new = [pid for pid in dict.fromkeys(product_ids) if pid not in _pending]
    room = _room()
    if len(new) > room:
        log.warning("recálculo: la cola llegó al tope (%d): %d productos quedan para la noche", MAX_QUEUED, len(new) - room)
        new = new[:room]
    _pending.update(new)
    return len(new)


def enqueue_polled(changes: dict[str, str | None]) -> int:
    """Lo que vio el sondeo (producto → `updatedAt` del cambio, o None). No se encola lo que ya se leyó con ese `updatedAt`
    o uno posterior (un aviso de Vendure llegó antes): así un guardado no gasta dos o tres lecturas."""
    def already_read(pid: str, version: str | None) -> bool:
        mark = _read_marks.get(pid)
        if mark is None:
            return False
        seen, polled = _parse_ts(mark), _parse_ts(version)
        return polled is None or (seen is not None and polled <= seen)

    return enqueue(pid for pid, version in changes.items() if not already_read(pid, version))


def _parse_ts(value: str | None) -> datetime | None:
    from app.vendure.catalog import _parse_iso   # el mismo parser del watermark del catálogo

    return _parse_iso(value) if value else None


def _newest_update(product: VendureProduct) -> str | None:
    """El `updatedAt` más nuevo del producto y de sus variantes, tal como lo devolvió Vendure."""
    stamps = [(at, x) for x in (product.updated_at, *(v.updated_at for v in product.priced_variants or []))
              if (at := _parse_ts(x)) is not None]
    return max(stamps)[1] if stamps else None


# ─── Avisos de Vendure ────────────────────────────────────────────

# Cuándo se lee un producto avisado y cuándo se relee un aviso de tramo o de variante que dio «sin cambio».
NOTICE_READ_AFTER = timedelta(seconds=20)
NOTICE_RECHECK_AFTER = timedelta(minutes=2)


@dataclass(slots=True)
class _Notice:
    due: datetime
    recheck: bool          # tramo / variante: si da «sin cambio», se relee una vez a los ~2 min


_notices: dict[str, _Notice] = {}
_notice_task: asyncio.Task | None = None


def _is_leader() -> bool:
    """El proceso que corre el scheduler: el único que recalcula (los locks son de este proceso)."""
    from app.scheduler import jobs   # lazy: jobs arrastra todo el scheduler

    return bool(jobs.scheduler.running)


def notify(changes: dict[str, bool], now: datetime | None = None) -> int:
    """Anota los productos que avisó Vendure (producto → ¿se relee si da «sin cambio»?) y arranca el procesamiento.
    Devuelve cuántos quedaron anotados. Un producto avisado dos veces se lee una vez, a los ~20 s del último aviso."""
    if not _is_leader():
        # shortcut: con más de una réplica el aviso se pierde y lo toma el sondeo del líder; si se escala, pasarlo a la base.
        log.warning("aviso de Vendure en una instancia que no es la líder: lo toma el sondeo")
        return 0
    from app.vendure import catalog as vendure_catalog

    now = now or utcnow()
    known = vendure_catalog.known_ids()
    if known is not None:                         # solo productos del catálogo (sin catálogo cargado, el tope alcanza)
        changes = {pid: recheck for pid, recheck in changes.items() if pid in known}
    room = _room()
    taken = 0
    for pid, recheck in changes.items():
        old = _notices.get(pid)
        if old is None:
            if taken >= room:
                continue
            taken += 1
        _notices[pid] = _Notice(now + NOTICE_READ_AFTER, recheck or (old is not None and old.recheck))
    dropped = sum(1 for pid in changes if pid not in _notices)
    if dropped:
        log.warning("recálculo: los avisos llegaron al tope (%d): %d productos quedan para la noche", MAX_QUEUED, dropped)
    if _notices:
        _start_notice_worker()
    return len(changes) - dropped


def _start_notice_worker() -> None:
    global _notice_task
    if _notice_task is None or _notice_task.done():
        _notice_task = asyncio.get_running_loop().create_task(_notice_worker())


async def _notice_worker() -> None:
    """Procesa los avisos a medida que vencen, hasta que no quede ninguno. Uno solo por proceso; nunca levanta."""
    while _notices:
        wait = (min(n.due for n in _notices.values()) - utcnow()).total_seconds()
        await asyncio.sleep(min(30.0, max(1.0, wait)))
        try:
            await process_notices()
        except Exception as exc:  # noqa: BLE001
            log.warning("recálculo: los avisos de Vendure fallaron (%s); se reintenta en 30 s", error_code(exc))
            await asyncio.sleep(30.0)


async def process_notices(client: Any = None, now: datetime | None = None) -> dict[str, int]:
    """Los avisos vencidos, por el mismo camino que la cola (con su cupo diario). Durante la noche pasan a la cola, que se
    procesa cuando la corrida termina; con el vigía trabajando esperan a la próxima vuelta."""
    now = now or utcnow()
    due = {pid: n for pid, n in _notices.items() if n.due <= now}
    if not due:
        return {}
    if price_monitor.price_monitor_lock.locked():
        for pid in due:
            _notices.pop(pid, None)
        enqueue(list(due))
        return {"corrida": len(due)}
    if _busy.locked():
        return {"ocupado": len(due)}
    async with _busy:
        ctx = _context(client, now, from_queue=True)
        for pid in due:
            _notices.pop(pid, None)
        counts = await _run(ctx, sorted(due), "avisos de Vendure")
    for pid, notice in due.items():
        if notice.recheck and ctx.outcomes.get(pid) == "sin_cambio" and pid not in _notices:
            _notices[pid] = _Notice(now + NOTICE_RECHECK_AFTER, recheck=False)
    return counts


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
    max_watch_reads: int
    # True = la cola de cambios (gasta el cupo diario y lo que frena la noche vuelve a la cola); False = el vigía.
    from_queue: bool = True
    catalog: dict[str, VendureProduct] = field(default_factory=dict)
    to_diagnose: list[int] = field(default_factory=list)
    capped: bool = False                 # se llegó al tope del día: el resto (un cambio masivo) va directo a la noche
    outcomes: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class _Attempt:
    """Una reactivación automática para después de guardar. `hold` = (código, por qué) si no se puede: se anota a mano."""
    item_id: int
    state: str
    hold: tuple[str, str] | None = None


@dataclass(slots=True)
class _Change:
    """Lo que hay que hacer después de guardar: reactivar o rehacer el diagnóstico. `changed` = cambió el precio."""
    changed: bool = False
    reactivate: _Attempt | None = None
    diagnose: int | None = None


def _context(client: Any, now: datetime | None, *, from_queue: bool) -> _Ctx:
    now = now or utcnow()
    return _Ctx(client=client or VendureClient(), now=now, day=daily_budget.today_art(now),
                params=pr.ReviewParams.from_runtime(), mode=int(runtime.get("pm_mode") or 0),
                tier_policy=int(runtime.get("pm_tier_policy")), max_reads=int(runtime.get("pm_recalc_daily_max")),
                max_reactivations=int(runtime.get("pm_recalc_reactivations_daily_max")),
                max_watch_reads=int(runtime.get("pm_recalc_watch_daily_max")), from_queue=from_queue)


async def run_pending(client: Any = None, now: datetime | None = None) -> dict[str, int]:
    """Recalcula lo que está en la cola. Devuelve cuántos productos terminaron en cada caso (también va al log). Lo que no
    se pudo por la corrida de la noche queda en la cola; lo demás sale (incluido lo que pasó el tope: eso es de la noche)."""
    if not _pending:
        return {}
    if price_monitor.price_monitor_lock.locked():
        return {"corrida": len(_pending)}
    if _busy.locked():                           # el vigía está recalculando: la cola sigue entera para la próxima vuelta
        return {"ocupado": len(_pending)}
    async with _busy:
        ctx = _context(client, now, from_queue=True)     # antes de sacar nada: si falla, la cola queda como estaba
        ids = sorted(_pending)
        _pending.clear()
        return await _run(ctx, ids, "cambios en Vendure")


async def watch_live_items(client: Any = None, now: datetime | None = None, limit: int = WATCH_MAX) -> dict[str, int]:
    """El vigía (R-1b, cada 30 min): relee el precio de los productos con ficha viva o `mantenido`, hasta `limit` por
    vuelta, primero los que se miraron hace más tiempo (los nunca mirados, por la ficha más vieja). Mismo camino que la
    cola: si el precio y el tramo no cambiaron, el snapshot no se toca, pero la ficha que apagó Hugo se vuelve a mirar
    (es la segunda lectura que confirma el «no rojo», o lo que no se pudo aplicar antes). Contador diario propio
    (`pm_recalc_watch_daily_max`), no el de la cola. No corre con la noche, con `pm_mode = 0` ni con el recálculo
    apagado (`pm_recalc_daily_max = 0`)."""
    if price_monitor.price_monitor_lock.locked():
        return {"corrida": 0}
    if int(runtime.get("pm_mode") or 0) == 0 or int(runtime.get("pm_recalc_daily_max") or 0) <= 0:
        return {}                                # bandeja en sombra o recálculo apagado: el vigía tampoco lee
    async with _busy:
        pids = await asyncio.to_thread(_live_products)
        never = datetime.min
        pids = sorted(pids, key=lambda pid: _watched_at.get(pid, never))[:max(0, limit)]   # sorted es estable
        ctx = _context(client, now, from_queue=False)
        counts = await _run(ctx, pids, "fichas vivas")
        for pid, outcome in ctx.outcomes.items():
            if outcome != "corrida":
                _watched_at[pid] = ctx.now
        return counts


def _live_products() -> list[str]:
    """Los productos con ficha viva (`open_key`: abiertas y mantenidas), de la ficha más vieja a la más nueva."""
    with Session(engine) as s:
        return list(s.exec(select(PriceReviewItem.product_id).where(PriceReviewItem.open_key.is_not(None))  # type: ignore[union-attr]
                           .order_by(PriceReviewItem.state_since, PriceReviewItem.id)).all())


async def _run(ctx: _Ctx, ids: list[str], what: str) -> dict[str, int]:
    """Los productos de a `CONCURRENCY` (primero los que tienen ficha viva) y, al final, el diagnóstico de las fichas que
    siguen abiertas. Un lote de más de `MASSIVE` productos es un cambio masivo: se leen solo las fichas vivas y el resto
    queda para la noche."""
    now = ctx.now
    counts: dict[str, int] = {}
    live = set(await asyncio.to_thread(_live_products)) if ids else set()
    ids = sorted(ids, key=lambda pid: pid not in live)                          # sorted es estable
    if len(ids) > MASSIVE:
        rest = [pid for pid in ids if pid not in live]
        ids = [pid for pid in ids if pid in live]
        counts["masivo"] = len(rest)
        log.warning("recálculo (%s): cambio masivo, %d productos sin ficha quedan para la noche", what, len(rest))
    sem = asyncio.Semaphore(CONCURRENCY)

    async def _one(pid: str) -> None:
        async with sem:
            try:
                outcome = await _recalc_one(ctx, pid)
            except Exception as exc:  # noqa: BLE001  (un producto no frena a los demás)
                log.warning("recálculo de %s falló: %s", pid, error_code(exc))
                outcome = "error"
            ctx.outcomes[pid] = outcome
            counts[outcome] = counts.get(outcome, 0) + 1

    await asyncio.gather(*(_one(pid) for pid in ids))
    if ctx.to_diagnose and not price_monitor.price_monitor_lock.locked():
        try:
            if done := await pr.rediagnose(ctx.to_diagnose, catalog=ctx.catalog, now=now):
                counts["diagnosticos"] = done
        except Exception as exc:  # noqa: BLE001
            log.warning("recálculo: el diagnóstico de las fichas falló: %s", error_code(exc))
    if counts:
        log.info("recálculo (%s): %d productos → %s", what, len(ids) + counts.get("masivo", 0), counts)
    return counts


async def _recalc_one(ctx: _Ctx, pid: str) -> str:
    if price_monitor.price_monitor_lock.locked():
        if ctx.from_queue:
            _pending.add(pid)                   # arrancó la corrida de la noche: lo que falta queda en la cola
        return "corrida"
    if ctx.capped:
        return "tope"
    snap_id, reason = await asyncio.to_thread(_last_measured, pid, ctx)
    if reason == "corrida" and ctx.from_queue:
        _pending.add(pid)
    if snap_id is None:
        return reason
    key, cap = (READS_KEY, ctx.max_reads) if ctx.from_queue else (WATCH_READS_KEY, ctx.max_watch_reads)
    if await asyncio.to_thread(daily_budget.reserve, key, cap, None, None, ctx.day) is None:
        ctx.capped = True
        return "tope"
    product = await ctx.client.get_product_priced(pid)
    if product is None:
        return "no_existe"
    if (mark := _newest_update(product)) is not None:
        _read_marks[pid] = mark
    variants = product.priced_variants or []
    our = semaforo.pick_our_price(variants, ctx.tier_policy)
    if our is None:
        return "sin_precio"
    if our.currency and our.currency.upper() != price_monitor.CURRENCY:
        return "moneda"
    other_policy = (semaforo.TIER_POLICY_CHEAPEST if ctx.tier_policy == semaforo.TIER_POLICY_MIN
                    else semaforo.TIER_POLICY_MIN)
    ctx.catalog[pid] = product
    change = await asyncio.to_thread(_apply, ctx, snap_id, product, our, semaforo.pick_our_price(variants, other_policy))
    if change.reactivate is not None:
        await _reactivate(ctx, change.reactivate)
    if change.diagnose is not None:
        ctx.to_diagnose.append(change.diagnose)
    return "recalculado" if change.changed else "sin_cambio"


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


def _apply(ctx: _Ctx, snap_id: int, product: VendureProduct, our: semaforo.OurPrice,
           other: semaforo.OurPrice | None) -> _Change:
    """El snapshot recalculado, los contadores de la corrida y la ficha, en UNA transacción.

    Si el precio y el tramo son los del snapshot (con la política de tramos vigente o con la otra: un cambio de
    `pm_tier_policy` es configuración, no un arreglo en Vendure) el snapshot no se toca; la ficha que apagó Hugo igual se
    vuelve a mirar, porque esta lectura puede ser la que confirma el «no rojo»."""
    change = _Change()
    with Session(engine, expire_on_commit=False) as s:
        snap = s.get(MarketPriceSnapshot, snap_id)
        if snap is None:
            return change
        seen = {(our.price_cents, our.tier_used)}
        if other is not None and _tier_policy_changed_since(s, snap.captured_at):
            seen.add((other.price_cents, other.tier_used))      # el tramo cambió por configuración, no en Vendure
        if (snap.our_price_cents, snap.tier_used) in seen:
            if ctx.mode >= 1:
                item = _live_item(s, snap.product_id)
                if item is not None:
                    change.reactivate = _decide(s, ctx, item, snap)
                    s.add(item)
                    s.commit()
            return change
        change.changed = True
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
        # `review_flag` es la medición de la noche y de ahí sale la racha (R8): el veredicto del recálculo va aparte.
        snap.recalc_flag = verdict.flag
        s.add(snap)
        price_monitor.recount_run(s, snap.run_id)
        if ctx.mode >= 1:
            _update_item(s, ctx, snap, verdict, change, old_price, old_color, ratio_before)
            if verdict.firm:                     # rojo firme de nuevo tras una reactivación automática (como la noche)
                pr._red_after_auto_reactivation(s, [snap], ctx.now)
        s.commit()
    return change


def _tier_policy_changed_since(s: Session, since: datetime) -> bool:
    """¿Alguien cambió `pm_tier_policy` después de esta medición? Es un setting de admin y cada cambio queda en el audit
    log: sin una fila así, un `tier_used` distinto es un cambio de tramos en Vendure, no de configuración."""
    return s.exec(select(AuditLog.id).where(
        AuditLog.action == "setting_changed", AuditLog.before.like('{"pm_tier_policy":%'),  # type: ignore[union-attr]
        AuditLog.created_at > since).limit(1)).first() is not None


def _live_item(s: Session, product_id: str) -> PriceReviewItem | None:
    """La ficha viva del producto, salvo que tenga una escritura en Vendure en curso (esa no se toca: B-6)."""
    item = pr.open_item_for(s, product_id)
    if item is None or pr.pending_intent(s, int(item.id)) is not None:   # type: ignore[arg-type]
        return None
    return item


def _update_item(s: Session, ctx: _Ctx, snap: MarketPriceSnapshot, verdict: pr.FirmVerdict, change: _Change,
                 old_price: int | None, old_color: str, ratio_before: float | None) -> None:
    """La ficha viva (abierta o `mantenido`) del producto. Sin ficha no se abre ninguna: eso es de la noche (R8)."""
    item = _live_item(s, snap.product_id)
    if item is None:
        return
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
    else:
        change.reactivate = _decide(s, ctx, item, snap)
    s.add(item)
    if item.state in pr.OPEN_STATES:
        change.diagnose = int(item.id)  # type: ignore[arg-type]


AVISO = {
    pr.CORRIGIENDO: "Ya no está en rojo: reactivala con «Ya lo corregí».",
    pr.EN_REVISION: "Ya no está en rojo: pasala a «Corregir» y reactivala con «Ya lo corregí».",
}
PRECIO_BAJO = ("el precio nuevo quedó demasiado bajo (contra Mercado Libre o contra el de la noche): revisá que no sea un "
               "error de carga")


def _decide(s: Session, ctx: _Ctx, item: PriceReviewItem, snap: MarketPriceSnapshot) -> _Attempt | None:
    """¿Hugo reactiva solo esta ficha? (A-1 a A-5 y los ajustes del 30-oct). Solo lo que apagó Hugo:

      * no rojo FIRME (verde o amarillo con la misma calidad de dato que la entrada); si no, nada y se va el aviso;
      * piso de precio: nuestro precio / mediana firme >= 1 / `pm_review_max_price_ratio` y no más de 50 % por debajo del
        de la noche; si no, se intenta igual por el protocolo para que quede «reactivar a mano: precio_bajo»;
      * `en_revision` nunca: solo el aviso «ya no está en rojo» (una vez);
      * `corrigiendo`, con el mismo precio visto en dos lecturas separadas por `CONFIRM_AFTER`, y un solo intento por
        precio (lo que terminó sin escribir no se repite en cada lectura).

    Devuelve el intento para después de guardar, o None. No escribe en Vendure ni hace commit."""
    if item.hugo_disabled is not True or item.state not in AVISO:
        return None
    ok = pr.evaluate_firm(snap, ctx.params, ctx.now, colors=pr.NOT_RED)
    if not ok.firm:
        pr.drop_flag(item, FLAG_NOT_RED)
        return None
    hold = _too_low(snap, ok, ctx.params, item)
    if hold is None and _held_for_a_person(s, int(item.id)):  # type: ignore[arg-type]
        return None                              # «precio bajo» una vez: hasta que una persona decida, no se prende solo
    if hold is not None:
        pr.drop_flag(item, FLAG_NOT_RED)             # con un precio sospechoso no se invita a reactivar
    if item.state == pr.EN_REVISION:
        if hold is None:
            _warn_once(s, item, "en_revision", ctx.now)
        return None
    first = snap.recalc_at or snap.captured_at
    if ctx.now - first < CONFIRM_AFTER or _attempted_since(s, int(item.id), first):  # type: ignore[arg-type]
        return None
    return _Attempt(int(item.id), item.state, hold)  # type: ignore[arg-type]


def _too_low(snap: MarketPriceSnapshot, ok: pr.FirmVerdict, p: pr.ReviewParams,
             item: PriceReviewItem) -> tuple[str, str] | None:
    """El piso de precio: un precio con ceros de menos es «verde firme» y no se puede prender solo. La caída se mide contra
    el MAYOR entre el precio de la noche y el precio con que se abrió la ficha: un precio malo cargado antes de la corrida
    pasa a ser «el de la noche» y contra eso no cae nada; contra el rojo con que se abrió, sí. Un arreglo legítimo de más de
    50 % contra ese rojo también queda para una persona: a propósito."""
    night = (pr._loads(snap.recalc_before) or {}).get("our_price_cents") if snap.recalc_before else snap.our_price_cents
    refs = [x for x in (night, item.our_price_cents) if isinstance(x, (int, float)) and x > 0]
    now_price = snap.our_price_cents or 0
    if ok.price_ratio is None or ok.price_ratio < 1.0 / p.max_price_ratio:
        return "precio_bajo", PRECIO_BAJO
    if refs and now_price < max(refs) * (1 - MAX_DROP):
        return "precio_bajo", PRECIO_BAJO
    return None


def _held_for_a_person(s: Session, item_id: int) -> bool:
    """¿La ficha quedó «reactivar a mano: precio_bajo» y después nadie decidió nada? (una decisión, Deshacer o
    «Inactivar» de una persona la libera)."""
    from app.pricing import price_review_actions as actions   # lazy: actions importa price_review y el cliente

    held = s.exec(select(func.max(PriceReviewEvent.id)).where(
        PriceReviewEvent.item_id == item_id, PriceReviewEvent.kind == "reactivar_a_mano",
        PriceReviewEvent.motive_code == "precio_bajo")).one()
    if held is None:
        return False
    return s.exec(select(PriceReviewEvent.id).where(
        PriceReviewEvent.item_id == item_id, PriceReviewEvent.id > held, PriceReviewEvent.actor != pr.ACTOR_AUTO,
        PriceReviewEvent.kind.in_((*actions.HUMAN_KINDS, "deshacer"))).limit(1)).first() is None  # type: ignore[attr-defined]


def _attempted_since(s: Session, item_id: int, since: datetime) -> bool:
    """¿Ya hubo un intento automático con este precio? (el que terminó en «reactivar a mano» o en un error de Vendure no se
    repite en cada lectura: vuelve a intentarse recién con otro precio u otra medición)."""
    from app.pricing import price_review_actions as actions   # lazy: actions importa price_review y el cliente

    return s.exec(select(PriceReviewEvent.id).where(
        PriceReviewEvent.item_id == item_id, PriceReviewEvent.kind == actions.AUTO_KIND,
        PriceReviewEvent.at >= since).limit(1)).first() is not None


def _warn_once(s: Session, item: PriceReviewItem, code: str, now: datetime) -> None:
    """El aviso «ya no está en rojo» (marca y evento), una sola vez mientras siga valiendo."""
    if FLAG_NOT_RED in (pr._loads(item.flags) or []):
        return
    item.flags = json.dumps([*(pr._loads(item.flags) or []), FLAG_NOT_RED])
    pr.add_event(s, item, "aviso_reactivar", motive_code=code, motive_text=AVISO[item.state], now=now)


async def _reactivate(ctx: _Ctx, attempt: _Attempt) -> None:
    """La reactivación automática, con su tope del día. Las guardas, la escritura y lo que queda anotado (evento, AuditLog,
    «reactivar a mano» si algo frena, también el piso de precio) son de `price_review_actions`: este módulo no escribe en
    Vendure."""
    from app.pricing import price_review_actions as actions   # lazy: actions importa price_review y el cliente

    if attempt.hold is None and await asyncio.to_thread(
            daily_budget.reserve, REACTIVATIONS_KEY, ctx.max_reactivations, None, None, ctx.day) is None:
        await asyncio.to_thread(_warn_cap, attempt.item_id)
        return
    try:
        if attempt.hold is None:
            await actions.auto_reactivate(attempt.item_id, attempt.state)
        else:
            await actions.auto_reactivate(attempt.item_id, attempt.state, hold=attempt.hold)
    except actions.ActionError as exc:
        log.info("recálculo: la ficha #%s no se reactivó sola (%s)", attempt.item_id, exc.code)
    else:
        await asyncio.to_thread(_mark_enabled, attempt.item_id)


def _warn_cap(item_id: int) -> None:
    """Pasado el tope del día, la ficha queda con el aviso para reactivarla a mano (una vez; el día siguiente se reintenta)."""
    with Session(engine) as s:
        item = s.get(PriceReviewItem, item_id)
        if item is None or item.state != pr.CORRIGIENDO:
            return
        _warn_once(s, item, "tope_del_dia", utcnow())
        s.add(item)
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
