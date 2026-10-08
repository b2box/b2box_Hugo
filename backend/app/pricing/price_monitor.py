"""Job nocturno: semáforo de precios contra Mercado Libre — MODO SOMBRA.

Cada noche, para cada producto habilitado del catálogo:

    1. precio nuestro FRESCO desde Vendure (priceWithTax + tramos, según
       `pm_tier_policy`)                                   → semaforo.pick_our_price
    2. búsqueda en ML por título                           → market_ml.MlMarket.search
    3. filtro "mismo producto" (CLIP + nombre, juez LLM
       opcional para la banda ambigua)                     → market_match / market_judge
    4. vendedores de las fichas que matchearon, sin los de
       pocas ventas                                        → MlMarket.listings / seller_sales
    5. mediana, mínimo, cantidad → ganancia estimada → color → semaforo
    6. una fila en `market_price_snapshot`, SIEMPRE, con `ml_status` que dice
       si hubo dato (ok), si ML no lo tiene (no_data), si ML falló (failed) o
       si no se pudo evaluar (skipped).

Lo que este job NO hace (PR 2): no toca Vendure. Ni `enabled`, ni custom
fields, ni nada. `pm_mode=1` existe como setting pero acá solo se loguea.

Reanudación: la corrida vive en `price_monitor_run` con status `running`
hasta que termina. Si el proceso se reinicia a mitad de la noche, la próxima
invocación (register_jobs la programa con _STARTUP_GRACE) retoma ESA corrida y
procesa solo los productos sin snapshot en ese `run_id`.

Robustez: un 429/5xx persistente de ML deja al producto `failed` y el job
sigue; si al final más del 20 % quedó `failed`, la corrida es `degraded`. El
budget diario de requests corta con `skipped` (no con `failed`): no es culpa
de ML. Nada de esto tira el job.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from app import runtime
from app.clock import utcnow
from app.config import get_settings
from app.db.models import MarketPriceSnapshot, PriceMonitorRun
from app.db.session import engine
from app.dedup import catalog_index, image_embed
from app.ingest import meli
from app.pricing import market_judge, market_match, semaforo
from app.pricing.market_ml import (
    PROBE_LISTING_PRICES_KEY,
    PROBE_SOLD_QUANTITY_KEY,
    BudgetExhausted,
    MlListing,
    MlMarket,
    ml_budget_status,
    payload_has_sold_quantity,
    probe_recorded,
    record_probe,
)
from app.vendure.client import VendureClient, VendureProduct

log = logging.getLogger(__name__)

JOB_ID = "price_monitor"
price_monitor_lock = asyncio.Lock()

# ml_status por producto
OK = "ok"
NO_DATA = "no_data"
FAILED = "failed"
SKIPPED = "skipped"

# status de la corrida
RUN_RUNNING = "running"
RUN_OK = "ok"
RUN_DEGRADED = "degraded"
RUN_FAILED = "failed"

# Más que esto de productos `failed` sobre el total → corrida degraded.
DEGRADED_FAILED_RATIO = 0.20
# Una corrida `running` más vieja que esto no se retoma: se cierra como
# `failed` y arranca una nueva. Sin esto, un proceso caído todo un día haría que
# el cron de la noche siguiente "retome" una corrida de ayer en vez de medir hoy.
STALE_RUN_HOURS = 20
# Cada cuántos productos se persisten el avance y los requests gastados: si el
# proceso muere, la corrida retomada no pierde la cuenta de lo ya consumido.
CHECKPOINT_EVERY = 25
# Fichas de ML (ya matcheadas) cuyos vendedores se consultan por producto.
MAX_MATCHED_PRODUCTS = 4
# Solo comparamos pesos con pesos.
CURRENCY = "ARS"

JudgeFn = Callable[..., Awaitable[market_judge.JudgeResult | None]]


@dataclass(slots=True)
class RunContext:
    run_id: int
    ml: MlMarket
    thresholds: market_match.Thresholds
    commission_pct: float
    shipping_cents: int
    green_min: float
    yellow_min: float
    tier_policy: int
    min_seller_sales: int
    judge_max_calls: int
    scorer: market_match.ImageScorer = market_match.clip_index_scorer
    # ¿Se puede puntuar este producto por imagen? Se chequea antes de gastar
    # el request de búsqueda (ver market_match.indexed).
    can_score: Callable[[VendureProduct], bool] = market_match.indexed
    judge_fn: JudgeFn = market_judge.judge
    # Acumuladores del juez (un solo hilo: el event loop).
    llm_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cost_usd: float = 0.0
    # Para la sonda de listing_prices: la primera categoría que vimos.
    first_category_id: str | None = None
    sold_quantity_probed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Totals:
    """Consumo acumulado de una corrida (absoluto, no delta). Al retomar se
    parte de lo que ya tenía guardado la fila."""
    ml_requests: int = 0
    llm_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cost_usd: float = 0.0

    @classmethod
    def of(cls, run: PriceMonitorRun) -> "Totals":
        return cls(
            ml_requests=int(run.ml_requests_used or 0),
            llm_calls=int(run.llm_calls or 0),
            llm_input_tokens=int(run.llm_input_tokens or 0),
            llm_output_tokens=int(run.llm_output_tokens or 0),
            llm_cost_usd=float(run.llm_cost_usd or 0.0),
        )

    def plus(self, ml: MlMarket, ctx: RunContext) -> "Totals":
        return Totals(
            ml_requests=self.ml_requests + ml.requests_used,
            llm_calls=self.llm_calls + ctx.llm_calls,
            llm_input_tokens=self.llm_input_tokens + ctx.llm_input_tokens,
            llm_output_tokens=self.llm_output_tokens + ctx.llm_output_tokens,
            llm_cost_usd=round(self.llm_cost_usd + ctx.llm_cost_usd, 6),
        )

    def apply(self, run: PriceMonitorRun) -> None:
        run.ml_requests_used = self.ml_requests
        run.llm_calls = self.llm_calls
        run.llm_input_tokens = self.llm_input_tokens
        run.llm_output_tokens = self.llm_output_tokens
        run.llm_cost_usd = self.llm_cost_usd


# ─── Persistencia de la corrida ───────────────────────────────────


def open_run() -> PriceMonitorRun | None:
    """La corrida sin terminar más reciente, si la hay (= cursor de reanudación)."""
    with Session(engine) as s:
        return s.exec(
            select(PriceMonitorRun)
            .where(PriceMonitorRun.status == RUN_RUNNING)
            .order_by(PriceMonitorRun.started_at.desc())  # type: ignore[union-attr]
            .limit(1)
        ).first()


def _is_stale(run: PriceMonitorRun) -> bool:
    return run.started_at < utcnow() - timedelta(hours=STALE_RUN_HOURS)


def has_unfinished_run() -> bool:
    """¿Hay una corrida a medias que valga la pena retomar al arrancar? Las
    abandonadas (más de STALE_RUN_HOURS) no cuentan: esas las cierra el
    próximo cron, no un arranque a media tarde."""
    try:
        run = open_run()
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo consultar price_monitor_run: %s", exc)
        return False
    return run is not None and not _is_stale(run)


def _create_run(mode: int, trigger: str) -> PriceMonitorRun:
    run = PriceMonitorRun(mode=mode, trigger=trigger, status=RUN_RUNNING)
    with Session(engine) as s:
        s.add(run)
        s.commit()
        s.refresh(run)
    return run


def _update_run(run_id: int, **fields: Any) -> None:
    with Session(engine) as s:
        run = s.get(PriceMonitorRun, run_id)
        if run is None:
            return
        for k, v in fields.items():
            setattr(run, k, v)
        s.add(run)
        s.commit()


def _done_product_ids(run_id: int) -> set[str]:
    with Session(engine) as s:
        return set(s.exec(
            select(MarketPriceSnapshot.product_id).where(MarketPriceSnapshot.run_id == run_id)
        ).all())


def _last_color(product_id: str, current_run_id: int) -> str | None:
    """Color del snapshot anterior del producto (otra corrida), para `prev_color`."""
    with Session(engine) as s:
        return s.exec(
            select(MarketPriceSnapshot.color)
            .where(MarketPriceSnapshot.product_id == product_id,
                   MarketPriceSnapshot.run_id != current_run_id)
            .order_by(MarketPriceSnapshot.captured_at.desc())  # type: ignore[union-attr]
            .limit(1)
        ).first()


def _save_snapshot(snap: MarketPriceSnapshot) -> bool:
    """Guarda la fila. False si ya existía (otro worker la hizo): el índice
    único (run_id, product_id) es lo que hace idempotente la reanudación."""
    # expire_on_commit=False: el llamador sigue leyendo la instancia después
    # de guardarla (status para los conteos) y la sesión ya está cerrada.
    with Session(engine, expire_on_commit=False) as s:
        s.add(snap)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            return False
    return True


def _checkpoint(run_id: int, processed: int, totals: Totals) -> None:
    """Persiste el avance a mitad de corrida. Si falla, la corrida sigue."""
    try:
        with Session(engine) as s:
            run = s.get(PriceMonitorRun, run_id)
            if run is None:
                return
            run.processed = processed
            totals.apply(run)
            s.add(run)
            s.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("price_monitor: no se pudo guardar el avance de #%s: %s", run_id, exc)


def _finalize_run(
    run_id: int, *, totals: Totals | None, error: str | None = None,
    force_status: str | None = None,
) -> str:
    """Cierra la corrida con los conteos calculados desde los snapshots.
    `totals` None = dejar el consumo que ya tenía guardado la fila."""
    with Session(engine) as s:
        run = s.get(PriceMonitorRun, run_id)
        if run is None:
            return RUN_FAILED
        by_status = dict(s.exec(
            select(MarketPriceSnapshot.ml_status, func.count(MarketPriceSnapshot.id))  # type: ignore[arg-type]
            .where(MarketPriceSnapshot.run_id == run_id)
            .group_by(MarketPriceSnapshot.ml_status)
        ).all())
        by_color = dict(s.exec(
            select(MarketPriceSnapshot.color, func.count(MarketPriceSnapshot.id))  # type: ignore[arg-type]
            .where(MarketPriceSnapshot.run_id == run_id)
            .group_by(MarketPriceSnapshot.color)
        ).all())
        run.n_ok = int(by_status.get(OK, 0))
        run.n_no_data = int(by_status.get(NO_DATA, 0))
        run.n_failed = int(by_status.get(FAILED, 0))
        run.n_skipped = int(by_status.get(SKIPPED, 0))
        run.processed = sum(by_status.values())
        run.n_verde = int(by_color.get(semaforo.VERDE, 0))
        run.n_amarillo = int(by_color.get(semaforo.AMARILLO, 0))
        run.n_rojo = int(by_color.get(semaforo.ROJO, 0))
        run.n_sin_dato = int(by_color.get(semaforo.SIN_DATO, 0))
        if totals is not None:
            totals.apply(run)
        if force_status:
            status = force_status
        else:
            total = max(1, run.total_products or run.processed)
            status = RUN_DEGRADED if run.n_failed / total > DEGRADED_FAILED_RATIO else RUN_OK
        run.status = status
        run.error = (error or "")[:500] or None
        run.finished_at = utcnow()
        s.add(run)
        s.commit()
    return status


# ─── Evaluación de UN producto ────────────────────────────────────


def _base_snapshot(ctx: RunContext, product: VendureProduct) -> MarketPriceSnapshot:
    return MarketPriceSnapshot(
        run_id=ctx.run_id,
        product_id=product.id,
        product_name=(product.name or "")[:200] or None,
        product_code=product.product_code,
        product_image_url=product.featured_image_url,
        product_slug=product.slug or None,
        commission_pct=ctx.commission_pct,
        shipping_cents=ctx.shipping_cents,
        ml_currency=CURRENCY,
    )


def _mark(snap: MarketPriceSnapshot, status: str, reason: str) -> MarketPriceSnapshot:
    snap.ml_status = status
    snap.ml_error = reason[:300]
    snap.color = semaforo.SIN_DATO
    return snap


def _our_photos(product: VendureProduct) -> list[str]:
    urls: list[str] = []
    for u in [product.featured_image_url, *(product.image_urls or [])]:
        if u and u not in urls:
            urls.append(u)
    return urls


async def _consult_judge(ctx: RunContext, product: VendureProduct,
                         ambiguous: list[market_match.Decision]) -> None:
    """Le pregunta al juez por la banda ambigua y promueve a MATCH (fuente llm)
    los que confirma con confianza suficiente. Nunca lanza."""
    cands = [
        market_judge.JudgeCandidate(
            ml_id=d.candidate.id, title=d.candidate.name,
            image_url=(d.candidate.image_urls or [None])[0],
        )
        for d in ambiguous
    ]
    try:
        result = await ctx.judge_fn(
            product.name, _our_photos(product), cands, max_calls=ctx.judge_max_calls,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Juez LLM reventó para %s: %s", product.id, exc)
        return
    if result is None:
        return
    ctx.llm_calls += 1
    ctx.llm_input_tokens += result.input_tokens
    ctx.llm_output_tokens += result.output_tokens
    ctx.llm_cost_usd += result.cost_usd
    for d in ambiguous:
        verdict = result.verdicts.get(d.candidate.id)
        if verdict is None:
            continue
        d.confidence = verdict.confidence
        d.reason = verdict.reason
        if verdict.same_product and verdict.confidence >= market_judge.MIN_CONFIDENCE:
            d.verdict = market_match.MATCH
            d.source = market_match.SOURCE_LLM


async def _accepted_prices(ctx: RunContext, listings: list[MlListing]) -> list[tuple[int, str]]:
    """(precio, vendedor) de las publicaciones que cuentan: en pesos y de
    vendedores con ventas suficientes. Si el payload trae `sold_quantity` se
    usa eso; si no, `/users/{seller}` (cacheado 30 días). Ventas desconocidas
    (ML no lo dice) NO descartan: preferimos un dato de más a quedarnos sin
    mediana, y queda contado para revisarlo en sombra."""
    out: list[tuple[int, str]] = []
    for item in listings:
        if item.price_cents is None or item.price_cents <= 0:
            continue
        if item.currency and item.currency.upper() != CURRENCY:
            continue
        if ctx.first_category_id is None and item.category_id:
            ctx.first_category_id = item.category_id
        sales = item.sold_quantity
        if sales is None:
            sales = await ctx.ml.seller_sales(item.seller_id)
        if sales is not None and sales < ctx.min_seller_sales:
            continue
        out.append((item.price_cents, item.seller_id))
    return out


async def evaluate_product(ctx: RunContext, product: VendureProduct) -> MarketPriceSnapshot:
    """Todo el camino de UN producto → snapshot (sin guardar)."""
    snap = _base_snapshot(ctx, product)

    our = semaforo.pick_our_price(product.priced_variants or [], ctx.tier_policy)
    if our is None:
        return _mark(snap, SKIPPED, "sin precio en Vendure")
    snap.our_price_cents, snap.variant_id, snap.tier_used = our.price_cents, our.variant_id, our.tier_used

    if not _our_photos(product):
        return _mark(snap, SKIPPED, "sin foto propia para comparar")
    if not ctx.can_score(product):
        return _mark(snap, SKIPPED, "sin fotos en el índice CLIP (o CLIP apagado)")
    query = market_match.search_query(product.name)
    if not query:
        return _mark(snap, SKIPPED, "sin nombre para buscar")

    try:
        candidates = await ctx.ml.search(query)
        if not candidates:
            shorter = market_match.fallback_query(product.name)
            if shorter:
                candidates = await ctx.ml.search(shorter)
    except BudgetExhausted as exc:
        return _mark(snap, SKIPPED, f"budget ML agotado: {exc}")
    except meli.MeliError as exc:
        return _mark(snap, FAILED, f"búsqueda ML: {exc}")
    snap.candidates_count = len(candidates)
    if not candidates:
        return _mark(snap, NO_DATA, "ML no devolvió fichas para el título")

    decisions = await market_match.score_candidates(product, candidates, ctx.thresholds, scorer=ctx.scorer)
    images = [d.image_score for d in decisions if d.image_score is not None]
    snap.image_score_max = max(images) if images else None
    snap.name_score_max = max((d.name_score for d in decisions), default=None)
    if not images:
        return _mark(snap, SKIPPED, "sin score de imagen (CLIP o índice no disponibles)")

    ambiguous = [d for d in decisions if d.verdict == market_match.AMBIGUOUS]
    snap.ambiguous_count = len(ambiguous)
    if ambiguous and ctx.judge_max_calls > 0:
        await _consult_judge(ctx, product, ambiguous)

    matches = [d for d in decisions if d.verdict == market_match.MATCH]
    if not matches:
        return _mark(snap, NO_DATA, "ninguna ficha es el mismo producto")
    matches.sort(key=lambda d: (d.image_score or 0.0), reverse=True)
    snap.match_source = matches[0].source
    snap.match_confidence = matches[0].confidence

    prices: list[int] = []
    sellers: set[str] = set()
    matched_json: list[dict[str, Any]] = []
    ml_errors: list[str] = []
    try:
        for d in matches[:MAX_MATCHED_PRODUCTS]:
            try:
                listings, raw = await ctx.ml.listings(d.candidate.id)
            except meli.MeliError as exc:
                ml_errors.append(str(exc))
                continue
            if not ctx.sold_quantity_probed and raw.get("results"):
                ctx.sold_quantity_probed = True
                if not probe_recorded(PROBE_SOLD_QUANTITY_KEY):
                    record_probe(PROBE_SOLD_QUANTITY_KEY,
                                 {"present": payload_has_sold_quantity(raw), "product": d.candidate.id})
            accepted = await _accepted_prices(ctx, listings)
            if not accepted:
                continue
            cents = [p for p, _ in accepted]
            prices.extend(cents)
            sellers.update(sid for _, sid in accepted if sid)
            matched_json.append({
                "ml_id": d.candidate.id,
                "title": d.candidate.name[:160],
                "permalink": d.candidate.permalink,
                "listings": len(accepted),
                "min_cents": min(cents),
                "median_cents": semaforo.median_cents(cents),
                "source": d.source,
                "image_score": round(d.image_score, 3) if d.image_score is not None else None,
                "name_score": round(d.name_score, 3),
                "confidence": d.confidence,
            })
    except BudgetExhausted as exc:
        return _mark(snap, SKIPPED, f"budget ML agotado: {exc}")

    snap.matched_listings = json.dumps(matched_json, ensure_ascii=False)
    if not prices:
        if ml_errors and not matched_json:
            return _mark(snap, FAILED, f"vendedores ML: {ml_errors[0]}")
        return _mark(snap, NO_DATA, "las fichas no tienen vendedores que cuenten")

    snap.ml_status = OK
    snap.ml_error = None
    snap.ml_median_cents = semaforo.median_cents(prices)
    snap.ml_min_cents = min(prices)
    snap.ml_listing_count = len(prices)
    snap.ml_seller_count = len(sellers)
    snap.est_margin_pct = semaforo.estimated_margin_pct(
        snap.ml_median_cents, snap.our_price_cents, ctx.commission_pct, ctx.shipping_cents,
    )
    snap.color = semaforo.color(
        snap.est_margin_pct, snap.our_price_cents, snap.ml_median_cents, ctx.green_min, ctx.yellow_min,
    )
    return snap


# ─── La corrida ───────────────────────────────────────────────────


def _context(run_id: int, ml: MlMarket) -> RunContext:
    return RunContext(
        run_id=run_id,
        ml=ml,
        thresholds=market_match.Thresholds.from_runtime(),
        commission_pct=float(runtime.get("pm_ml_commission_pct")),
        shipping_cents=int(runtime.get("pm_ml_shipping_cents")),
        green_min=float(runtime.get("pm_green_min_pct")),
        yellow_min=float(runtime.get("pm_yellow_min_pct")),
        tier_policy=int(runtime.get("pm_tier_policy")),
        min_seller_sales=int(runtime.get("pm_min_seller_sales")),
        judge_max_calls=int(runtime.get("pm_vision_max_calls") or 0),
        # Resueltos acá (y no como default del dataclass) para que un cambio en
        # market_match / market_judge —o un doble en los tests— se respete.
        scorer=market_match.clip_index_scorer,
        can_score=market_match.indexed,
        judge_fn=market_judge.judge,
    )


async def _ensure_clip_index() -> None:
    """El filtro por imagen necesita el índice del catálogo. `build()` es
    idempotente: si está listo y fresco vuelve enseguida; si no, lo construye
    acá (minutos la primera vez; después sale del cache de embeddings)."""
    if not image_embed.available():
        log.warning("CLIP no disponible: los productos quedarán `skipped` (sin score de imagen)")
        return
    await catalog_index.build()


async def run_price_monitor(trigger: str = "cron") -> dict[str, Any] | None:
    """Entry point del job. Devuelve un resumen o None si ya había una corrida
    en curso en este proceso (lock)."""
    if price_monitor_lock.locked():
        log.warning("price_monitor ya está corriendo, ignoro la nueva invocación")
        return None
    async with price_monitor_lock:
        return await _run(trigger)


def _open_or_resume(mode: int, trigger: str) -> tuple[PriceMonitorRun, str]:
    """Retoma la corrida a medias si la hay (mismo run_id); si está abandonada
    la cierra como `failed` y arranca una nueva."""
    run = open_run()
    if run is not None and _is_stale(run):
        log.warning("price_monitor: la corrida #%s quedó abandonada desde %s; la cierro y arranco otra",
                    run.id, run.started_at.isoformat(timespec="minutes"))
        _finalize_run(int(run.id), totals=None,  # type: ignore[arg-type]
                      error=f"abandonada: sin terminar después de {STALE_RUN_HOURS} h",
                      force_status=RUN_FAILED)
        run = None
    if run is not None:
        log.info("price_monitor: retomo la corrida #%s (reinicio a mitad)", run.id)
        _update_run(int(run.id), resumed_count=(run.resumed_count or 0) + 1,  # type: ignore[arg-type]
                    trigger="resume")
        run.resumed_count = (run.resumed_count or 0) + 1
        return run, "resume"
    run = _create_run(mode, trigger)
    log.info("price_monitor: corrida #%s (%s, modo %d)", run.id, trigger, mode)
    return run, trigger


async def _run(trigger: str) -> dict[str, Any] | None:
    mode = int(runtime.get("pm_mode") or 0)
    if mode == 1:
        # PR 2: rojos → inactivo + bandeja de Pao. Hasta entonces, sombra siempre.
        log.warning("pm_mode=1 (activo): modo activo todavía no implementado; "
                    "esta corrida es SOMBRA y no toca Vendure")

    run, trigger = _open_or_resume(mode, trigger)
    run_id = int(run.id)  # type: ignore[arg-type]
    base = Totals.of(run)

    if not meli.enabled():
        msg = "MELI_CLIENT_ID / MELI_CLIENT_SECRET no configurados"
        log.error("price_monitor: %s", msg)
        status = _finalize_run(run_id, totals=None, error=msg, force_status=RUN_FAILED)
        return {"run_id": run_id, "status": status, "error": msg}

    try:
        products = await VendureClient().fetch_all_products_priced()
    except Exception as exc:  # noqa: BLE001
        log.exception("price_monitor: Vendure no respondió")
        error = f"Vendure: {type(exc).__name__}: {exc}"
        status = _finalize_run(run_id, totals=None, error=error, force_status=RUN_FAILED)
        return {"run_id": run_id, "status": status, "error": error}

    try:
        return await _evaluate_catalog(run_id, base, trigger, products)
    except Exception as exc:  # noqa: BLE001
        # Un bug no puede dejar la corrida `running` para siempre: se reintentaría
        # en cada arranque y reventaría igual. (Un reinicio del proceso NO pasa
        # por acá — eso sí se retoma.)
        log.exception("price_monitor: la corrida #%s reventó", run_id)
        error = f"{type(exc).__name__}: {exc}"
        status = _finalize_run(run_id, totals=None, error=error, force_status=RUN_FAILED)
        return {"run_id": run_id, "status": status, "error": error}


async def _evaluate_catalog(
    run_id: int, base: Totals, trigger: str, products: list[VendureProduct],
) -> dict[str, Any]:
    enabled = [p for p in products if p.enabled]
    done = _done_product_ids(run_id)
    pending = [p for p in enabled if p.id not in done]
    _update_run(run_id, total_products=len(enabled), processed=len(done))
    log.info("price_monitor #%s (%s): %d habilitados, %d ya hechos, %d pendientes",
             run_id, trigger, len(enabled), len(done), len(pending))

    await _ensure_clip_index()

    budget = int(runtime.get("pm_ml_daily_budget"))
    concurrency = max(1, int(runtime.get("pm_ml_concurrency")))
    counts: dict[str, int] = {OK: 0, NO_DATA: 0, FAILED: 0, SKIPPED: 0}
    processed = len(done)

    async with MlMarket(budget=budget) as ml:
        ctx = _context(run_id, ml)
        sem = asyncio.Semaphore(concurrency)

        async def _one(product: VendureProduct) -> None:
            nonlocal processed
            async with sem:
                try:
                    snap = await evaluate_product(ctx, product)
                except Exception as exc:  # noqa: BLE001
                    log.exception("price_monitor: %s reventó", product.id)
                    snap = _mark(_base_snapshot(ctx, product), FAILED,
                                 f"{type(exc).__name__}: {exc}")
                status = snap.ml_status
                try:
                    snap.prev_color = _last_color(product.id, run_id)
                    _save_snapshot(snap)
                except Exception:  # noqa: BLE001
                    log.exception("price_monitor: no se pudo guardar el snapshot de %s", product.id)
                counts[status] = counts.get(status, 0) + 1
                processed += 1
                if processed % CHECKPOINT_EVERY == 0:
                    _checkpoint(run_id, processed, base.plus(ml, ctx))
                # Cede el loop: el job dura horas y no debe dejar sin atender HTTP.
                await asyncio.sleep(0)

        # `_one` ya atrapa todo lo esperable; return_exceptions evita que un bug
        # en un producto deje a los demás corriendo huérfanos tras el gather.
        for result in await asyncio.gather(*(_one(p) for p in pending), return_exceptions=True):
            if isinstance(result, Exception):
                log.error("price_monitor #%s: error no atrapado en un producto: %r", run_id, result)

        if ctx.first_category_id and not probe_recorded(PROBE_LISTING_PRICES_KEY):
            await ml.probe_listing_prices(ctx.first_category_id)

        totals = base.plus(ml, ctx)
        status = _finalize_run(run_id, totals=totals)

    log.info("price_monitor terminado: corrida #%s %s — %s, %d requests ML, %d llamadas LLM (USD %.4f)",
             run_id, status, counts, ml.requests_used, ctx.llm_calls, ctx.llm_cost_usd)
    return {"run_id": run_id, "status": status, "counts": counts,
            "ml_requests_used": totals.ml_requests, "llm_calls": totals.llm_calls}


# ─── Para el dashboard ────────────────────────────────────────────


def run_to_dict(run: PriceMonitorRun) -> dict[str, Any]:
    total = int(run.total_products or 0)

    def pct(n: int) -> float | None:
        return round(n / total * 100.0, 1) if total else None

    return {
        "id": run.id,
        "started_at": run.started_at.isoformat() + "Z" if run.started_at else None,
        "finished_at": run.finished_at.isoformat() + "Z" if run.finished_at else None,
        "status": run.status,
        "mode": run.mode,
        "trigger": run.trigger,
        "total_products": total,
        "processed": run.processed,
        "counts": {"ok": run.n_ok, "no_data": run.n_no_data, "failed": run.n_failed,
                   "skipped": run.n_skipped},
        "colors": {"verde": run.n_verde, "amarillo": run.n_amarillo, "rojo": run.n_rojo,
                   "sin_dato": run.n_sin_dato},
        "pct_no_data": pct(run.n_no_data),
        "pct_failed": pct(run.n_failed),
        "ml_requests_used": run.ml_requests_used,
        "llm": {"calls": run.llm_calls, "input_tokens": run.llm_input_tokens,
                "output_tokens": run.llm_output_tokens, "cost_usd": run.llm_cost_usd},
        "resumed_count": run.resumed_count,
        "error": run.error,
    }


def snapshot_to_dict(snap: MarketPriceSnapshot) -> dict[str, Any]:
    try:
        matched = json.loads(snap.matched_listings) if snap.matched_listings else []
    except ValueError:
        matched = []
    return {
        "id": snap.id,
        "run_id": snap.run_id,
        "product": {
            "id": snap.product_id, "name": snap.product_name, "code": snap.product_code,
            "image_url": snap.product_image_url, "slug": snap.product_slug,
        },
        "variant_id": snap.variant_id,
        "captured_at": snap.captured_at.isoformat() + "Z" if snap.captured_at else None,
        "ml_status": snap.ml_status,
        "ml_error": snap.ml_error,
        "ml_median_cents": snap.ml_median_cents,
        "ml_min_cents": snap.ml_min_cents,
        "ml_listing_count": snap.ml_listing_count,
        "ml_seller_count": snap.ml_seller_count,
        "ml_currency": snap.ml_currency,
        "matched_listings": matched,
        "match_source": snap.match_source,
        "match_confidence": snap.match_confidence,
        "image_score_max": snap.image_score_max,
        "name_score_max": snap.name_score_max,
        "candidates_count": snap.candidates_count,
        "ambiguous_count": snap.ambiguous_count,
        "our_price_cents": snap.our_price_cents,
        "tier_used": snap.tier_used,
        "commission_pct": snap.commission_pct,
        "shipping_cents": snap.shipping_cents,
        "est_margin_pct": snap.est_margin_pct,
        "color": snap.color,
        "prev_color": snap.prev_color,
    }


def summary() -> dict[str, Any]:
    """Tarjeta de Salud: última corrida + budget ML del día."""
    with Session(engine) as s:
        last = s.exec(
            select(PriceMonitorRun)
            .order_by(PriceMonitorRun.started_at.desc())  # type: ignore[union-attr]
            .limit(1)
        ).first()
    return {
        "last_run": run_to_dict(last) if last else None,
        "running": price_monitor_lock.locked(),
        "mode": int(runtime.get("pm_mode") or 0),
        "ml_budget": ml_budget_status(),
        "judge_enabled": market_judge.enabled() and int(runtime.get("pm_vision_max_calls") or 0) > 0,
        "cron_utc": get_settings().price_monitor_cron_utc,
    }
