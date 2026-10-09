"""Job nocturno: semáforo de precios contra Mercado Libre — MODO SOMBRA.

Cada noche, para cada producto habilitado del catálogo:

    1. precio nuestro FRESCO desde Vendure (priceWithTax + tramos, según
       `pm_tier_policy`)                                   → semaforo.pick_our_price
    2. búsqueda en ML por título: fichas de catálogo por la API
                                                           → market_ml.MlMarket.search
    3. filtro "mismo producto" (CLIP + nombre, juez LLM
       opcional para la banda ambigua), con veredicto IGUAL /
       SIMILAR / diferente y chequeo de medidas            → market_match / market_judge / market_specs
    4. vendedores de las fichas IGUALES, sin los de pocas
       ventas                                              → MlMarket.listings / seller_sales
    5. Si la API no dio un IGUAL con precio (lo importado de China casi nunca
       tiene ficha): el título en la web de ML con el navegador de Hugo
       (Camoufox + BROWSER_PROXY), mismo filtro                → market_ml_web
    6. mediana, mínimo, cantidad → ganancia estimada → color → semaforo.
       SOLO lo IGUAL cuenta; lo SIMILAR se guarda aparte para mostrar.
    7. una fila en `market_price_snapshot`, SIEMPRE, con `ml_status` que dice
       si hubo dato (ok), si ML no lo tiene (no_data), si ML falló (failed) o
       si no se pudo evaluar (skipped).

También se miden los productos deshabilitados de Vendure (`pm_include_disabled`):
quedan marcados en el snapshot y nunca llevan a escribir nada.

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
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from app import runtime
from app.clock import utcnow
from app.config import get_settings
from app.db.models import MarketPriceSnapshot, PriceMonitorRun
from app.db.session import engine
from app.dedup import catalog_index, image_embed
from app.ingest import browser_fetch, meli
from app.pricing import (
    daily_budget,
    match_feedback,
    market_judge,
    market_match,
    market_ml,
    market_ml_web,
    market_specs,
    semaforo,
    store_catalog,
    store_match,
)
from app.pricing.market_ml import (
    ORIGIN_API,
    ORIGIN_WEB,
    PROBE_LISTING_PRICES_KEY,
    PROBE_SOLD_QUANTITY_KEY,
    BudgetExhausted,
    MlCandidate,
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
# Arrancó sin cupo de ML: no se evaluó nada (una sola marca en la corrida, no
# un snapshot `skipped` por producto).
RUN_SKIPPED = "skipped"

# Más que esto de productos `failed` sobre el total → corrida degraded.
DEGRADED_FAILED_RATIO = 0.20
# Una corrida `running` más vieja que esto no se retoma: se cierra como
# `failed` y arranca una nueva. Sin esto, un proceso caído todo un día haría que
# el cron de la noche siguiente "retome" una corrida de ayer en vez de medir hoy.
STALE_RUN_HOURS = 20
# Fichas de ML (ya matcheadas) cuyos vendedores se consultan por producto.
MAX_MATCHED_PRODUCTS = 4
# Estados de la web con los que el producto quedó sin medir del todo.
WEB_UNMEASURED = ("blocked", "error", "budget", "off")
# match_state: qué devolvió ML para el producto.
STATE_IGUAL = "igual"
STATE_IGUAL_NO_PRICE = "igual_sin_precio"
STATE_SIMILAR = "similar"
STATE_DIFFERENT = "diferente"
STATE_NONE = "ninguno"
ESTIMATED_FROM_SIMILAR = "similar"
# Publicaciones SIMILARES que se guardan por producto (solo para mostrar).
MAX_SIMILAR_STORED = 6
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
    # Chequeo de medidas/cantidad (pm_spec_check) y sus tolerancias.
    spec_check: bool = True
    dim_tol_pct: float = 10.0
    weight_tol_pct: float = 15.0
    # Fuente "ML web": None = apagada (sin proxy, cupo 0, sin browser).
    web: market_ml_web.MlWebSource | None = None
    # Publicaciones que una persona marcó "No es el mismo" (quedan DIFERENTES) y
    # "Es el mismo" (quedan IGUALES), por producto.
    excluded: dict[str, frozenset[str]] = field(default_factory=dict)
    promoted: dict[str, frozenset[str]] = field(default_factory=dict)
    # Cuántas publicaciones se guardan por producto (pm_ml_keep_listings).
    keep_listings: int = 8
    scorer: market_match.ImageScorer = market_match.clip_index_scorer
    # ¿Se puede puntuar este producto por imagen? Se chequea antes de gastar
    # el request de búsqueda (ver market_match.indexed).
    can_score: Callable[[VendureProduct], bool] = market_match.indexed
    judge_fn: JudgeFn = market_judge.judge
    # Un solo cliente del juez por corrida (se crea al primer uso y se cierra
    # al terminar).
    judge_client: Any | None = None
    # Para la sonda de listing_prices: la primera categoría que vimos.
    first_category_id: str | None = None
    sold_quantity_probed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


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


def _last_measured(product_ids: set[str]) -> dict[str, Any]:
    """Último snapshot en que ML contestó algo (ok o no_data) por producto.
    Con budget corto, la corrida empieza por los que hace más que no se miden
    (o nunca): así todo el catálogo rota en vez de medirse siempre los mismos."""
    if not product_ids:
        return {}
    with Session(engine) as s:
        rows = s.exec(
            select(MarketPriceSnapshot.product_id, func.max(MarketPriceSnapshot.captured_at))
            .where(MarketPriceSnapshot.ml_status.in_([OK, NO_DATA]))  # type: ignore[attr-defined]
            # Un no_data porque la web estaba bloqueada o sin cupo NO se midió del
            # todo: vuelve a la cabeza de la fila en la próxima corrida.
            .where(or_(
                MarketPriceSnapshot.web_state.is_(None),  # type: ignore[union-attr]
                MarketPriceSnapshot.web_state.notin_(WEB_UNMEASURED),  # type: ignore[union-attr]
            ))
            .group_by(MarketPriceSnapshot.product_id)
        ).all()
    return {pid: at for pid, at in rows if pid in product_ids}


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


def _usage_increment(run_id: int, **deltas: int | float) -> Callable[[Session], None]:
    """UPDATE atómico `col = col + delta` sobre la fila de la corrida, para
    correr DENTRO de otra transacción (la reserva del budget, el insert del
    snapshot). Así lo gastado y lo avanzado nunca quedan atrás de un corte."""
    def _apply(session: Session) -> None:
        session.execute(
            update(PriceMonitorRun)
            .where(PriceMonitorRun.id == run_id)  # type: ignore[arg-type]
            .values({getattr(PriceMonitorRun, col): getattr(PriceMonitorRun, col) + delta
                     for col, delta in deltas.items()})
        )
    return _apply


def _add_usage(run_id: int, **deltas: int | float) -> None:
    """Lo mismo, en su propia transacción (tokens y costo del juez, que se
    conocen recién cuando vuelve la respuesta)."""
    with Session(engine) as s:
        _usage_increment(run_id, **deltas)(s)
        s.commit()


def _save_snapshot(snap: MarketPriceSnapshot) -> bool:
    """Guarda la fila y suma 1 a `processed` en la misma transacción. False si
    ya existía (otro worker la hizo): el índice único (run_id, product_id) es
    lo que hace idempotente la reanudación."""
    # expire_on_commit=False: el llamador sigue leyendo la instancia después
    # de guardarla y la sesión ya está cerrada.
    with Session(engine, expire_on_commit=False) as s:
        s.add(snap)
        try:
            s.flush()
        except IntegrityError:
            s.rollback()
            return False
        _usage_increment(snap.run_id, processed=1)(s)
        s.commit()
    return True


def _persist(snap: MarketPriceSnapshot, run_id: int) -> None:
    """prev_color + insert. Bloqueante: se llama con asyncio.to_thread."""
    snap.prev_color = _last_color(snap.product_id, run_id)
    _save_snapshot(snap)


def _recount(s: Session, run: PriceMonitorRun) -> None:
    """Contadores de la corrida (estados, colores, web, similares) a partir de
    sus snapshots."""
    run_id = run.id
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
    run.n_verde = int(by_color.get(semaforo.VERDE, 0))
    run.n_amarillo = int(by_color.get(semaforo.AMARILLO, 0))
    run.n_rojo = int(by_color.get(semaforo.ROJO, 0))
    run.n_sin_dato = int(by_color.get(semaforo.SIN_DATO, 0))
    run.n_web_ok = int(s.exec(
        select(func.count(MarketPriceSnapshot.id))  # type: ignore[arg-type]
        .where(MarketPriceSnapshot.run_id == run_id, MarketPriceSnapshot.ml_status == OK,
               MarketPriceSnapshot.match_origin == ORIGIN_WEB)
    ).one() or 0)
    run.n_con_similares = int(s.exec(
        select(func.count(MarketPriceSnapshot.id))  # type: ignore[arg-type]
        .where(MarketPriceSnapshot.run_id == run_id, MarketPriceSnapshot.similar_count > 0)
    ).one() or 0)
    # Aparte del color real: los estimados por similares y los "solo diferentes".
    estimated = dict(s.exec(
        select(MarketPriceSnapshot.estimated_color, func.count(MarketPriceSnapshot.id))  # type: ignore[arg-type]
        .where(MarketPriceSnapshot.run_id == run_id, MarketPriceSnapshot.ml_status != OK,
               MarketPriceSnapshot.estimated_color.is_not(None))  # type: ignore[union-attr]
        .group_by(MarketPriceSnapshot.estimated_color)
    ).all())
    run.n_est_verde = int(estimated.get(semaforo.VERDE, 0))
    run.n_est_amarillo = int(estimated.get(semaforo.AMARILLO, 0))
    run.n_est_rojo = int(estimated.get(semaforo.ROJO, 0))
    run.n_solo_diferentes = int(s.exec(
        select(func.count(MarketPriceSnapshot.id))  # type: ignore[arg-type]
        .where(MarketPriceSnapshot.run_id == run_id, MarketPriceSnapshot.match_state == STATE_DIFFERENT)
    ).one() or 0)
    run.source_stats = json.dumps(store_match.source_stats(s, run_id))
    run.processed = sum(by_status.values())


def recount_run(session: Session, run_id: int) -> None:
    """Rehace los contadores de una corrida después de corregir un snapshot
    ("No es el mismo" puede cambiar su color y su estado). Usa la sesión del
    llamador y no commitea."""
    run = session.get(PriceMonitorRun, run_id)
    if run is not None:
        _recount(session, run)
        session.add(run)


def _finalize_run(run_id: int, *, error: str | None = None, force_status: str | None = None,
                  web_status: str | None = None) -> str:
    """Cierra la corrida con los conteos calculados desde los snapshots. El
    consumo (requests ML, juez) ya está al día: se suma request a request."""
    with Session(engine) as s:
        run = s.get(PriceMonitorRun, run_id)
        if run is None:
            return RUN_FAILED
        _recount(s, run)
        if web_status is not None:
            run.web_status = web_status[:200]
        if force_status:
            status = force_status
        else:
            total = max(1, run.total_products or run.processed)
            status = RUN_DEGRADED if run.n_failed / total > DEGRADED_FAILED_RATIO else RUN_OK
        run.status = status
        run.error = browser_fetch.redact(error or "")[:500] or None
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
        product_enabled=bool(product.enabled),
        commission_pct=ctx.commission_pct,
        shipping_cents=ctx.shipping_cents,
        ml_currency=CURRENCY,
    )


def _mark(snap: MarketPriceSnapshot, status: str, reason: str) -> MarketPriceSnapshot:
    snap.ml_status = status
    snap.ml_error = browser_fetch.redact(reason)[:300]
    snap.color = semaforo.SIN_DATO
    return snap


def _our_photos(product: VendureProduct) -> list[str]:
    """La foto destacada del producto, solo https (es la que puede salir hacia
    el juez). UNA sola versión: Vendure expone la misma foto como `preview`
    (achicada) y como `source` (original) y mandar las dos gastaba tokens, una
    descarga y un decode de más, con una "segunda foto" que era la primera.
    Va la preview; si no hay, el source."""
    for u in [product.featured_image_url, *(product.image_urls or [])]:
        if u and u.startswith("https://"):
            return [u]
    return []


def _our_specs(product: VendureProduct, variant_id: str | None) -> market_specs.OurSpecs | None:
    """Medidas de la variante con la que se compara el precio (cm y kg)."""
    for v in product.priced_variants or []:
        if v.id == variant_id and v.specs:
            return market_specs.OurSpecs.from_dict(v.specs)
    return None


Listings = tuple[list[MlListing], dict]

# Marcas que declaran las publicaciones y que NO son una marca con valor propio.
_GENERIC_BRANDS = frozenset({
    "generica", "generico", "sin marca", "no aplica", "n/a", "na", "otra", "otras", "otros",
    "no especificada", "no especificado", "importado", "oem", "marca generica",
    # Decisión de Nico (08-oct-2026): «Gadnic» es marca de importador, como la nuestra; no es una marca conocida
    # con valor propio. Vale en TODAS las fuentes (una publicación de ML o de otra tienda que la declare incluida).
    "gadnic",
})


def _declared_brand(candidate: MlCandidate) -> str:
    """La marca que declara la publicación, o "" si no hay o es genérica."""
    brand = " ".join((candidate.brand or "").split())
    folded = "".join(c for c in unicodedata.normalize("NFKD", brand.lower())
                     if not unicodedata.combining(c)).strip()
    return "" if folded in _GENERIC_BRANDS else brand[:40]


@dataclass(slots=True)
class _Work:
    """Lo que se junta mientras se evalúa UN producto: todo lo que devolvió ML,
    clasificado. Nada se descarta: lo IGUAL con precio arma el color real, y lo
    demás (similares, diferentes, iguales sin precio) se guarda para mostrar."""
    query: str
    excluded: frozenset[str]
    specs: market_specs.OurSpecs | None
    promoted: frozenset[str] = frozenset()
    similars: list[dict[str, Any]] = field(default_factory=list)
    others: list[dict[str, Any]] = field(default_factory=list)
    igual_unpriced: list[dict[str, Any]] = field(default_factory=list)


async def _judge_will_answer(ctx: RunContext, counter_key: str = market_judge.LLM_COUNTER_KEY,
                             max_calls: int | None = None) -> bool:
    """¿Vale la pena preparar la consulta? Juez configurado y con cupo hoy (en el contador que corresponda)."""
    cap = ctx.judge_max_calls if max_calls is None else max_calls
    if cap <= 0 or not market_judge.enabled():
        return False
    used = await asyncio.to_thread(daily_budget.used_today, counter_key)
    return used < cap


def _ars_median(listings: list[MlListing]) -> int | None:
    ars = [x.price_cents for x in listings
           if x.price_cents and (not x.currency or x.currency.upper() == CURRENCY)]
    return semaforo.median_cents(ars)


async def _prefetch_prices(ctx: RunContext, ambiguous: list[market_match.Decision],
                           prefetched: dict[str, Listings]) -> dict[str, int | None]:
    """Mediana en pesos de cada ficha ambigua, para que el juez vea el precio.
    Lo que se baja queda en `prefetched` y se reusa si la ficha termina siendo
    match (no se pide dos veces)."""
    prices: dict[str, int | None] = {}
    for d in ambiguous[:market_judge.MAX_CANDIDATES]:
        try:
            listings, raw = await ctx.ml.listings(d.candidate.id)
        except BudgetExhausted:
            break
        except meli.MeliError:
            continue
        prefetched[d.candidate.id] = (listings, raw)
        prices[d.candidate.id] = _ars_median(listings)
    return prices


async def _consult_judge(ctx: RunContext, product: VendureProduct,
                         targets: list[market_match.Decision],
                         prefetched: dict[str, Listings], *,
                         counter_key: str = market_judge.LLM_COUNTER_KEY,
                         max_calls: int | None = None) -> None:
    """Le pregunta al juez por la banda ambigua (y, en la web, por los matches
    con marca declarada, para aplicar la regla de marca) y aplica su veredicto
    de tres valores. Nunca lanza. `counter_key` / `max_calls`: el contador y el tope
    diarios que corresponden (las tiendas tienen los suyos, separados de los de ML)."""
    cap = ctx.judge_max_calls if max_calls is None else max_calls
    # Solo se pasa `counter_key` si no es el de siempre (los dobles de los tests no lo conocen).
    extra = {} if counter_key == market_judge.LLM_COUNTER_KEY else {"counter_key": counter_key}
    prices: dict[str, int | None] = {}
    if await _judge_will_answer(ctx, counter_key, cap):
        api_targets = [d for d in targets if d.candidate.origin == ORIGIN_API]
        if api_targets:
            prices = await _prefetch_prices(ctx, api_targets, prefetched)
        if ctx.judge_client is None:
            ctx.judge_client = market_judge.make_client()
    cands = [
        market_judge.JudgeCandidate(
            ml_id=d.candidate.id, title=d.candidate.name,
            image_url=(d.candidate.image_urls or [None])[0],
            price_cents=prices.get(d.candidate.id) or d.candidate.price_cents,
            brand=_declared_brand(d.candidate) or None,
        )
        for d in targets[:market_judge.MAX_CANDIDATES]
    ]
    try:
        result = await ctx.judge_fn(
            product.name, _our_photos(product), cands, max_calls=cap,
            client=ctx.judge_client,
            # La llamada se cuenta al reservar el cupo, aunque después falle.
            on_reserve=_usage_increment(ctx.run_id, llm_calls=1), **extra,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Juez LLM reventó para %s: %s", product.id, exc)
        return
    if result is None:
        return
    if result.input_tokens or result.output_tokens or result.cost_usd:
        await asyncio.to_thread(
            _add_usage, ctx.run_id, llm_input_tokens=result.input_tokens,
            llm_output_tokens=result.output_tokens, llm_cost_usd=result.cost_usd,
        )
    for d in targets:
        verdict = result.verdicts.get(d.candidate.id)
        if verdict is not None:
            market_match.apply_judge_verdict(d, verdict)


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
        if ctx.first_category_id is None and market_ml.valid_category_id(item.category_id):
            ctx.first_category_id = item.category_id
        sales = item.sold_quantity
        if sales is None:
            sales = await ctx.ml.seller_sales(item.seller_id)
        if sales is not None and sales < ctx.min_seller_sales:
            continue
        out.append((item.price_cents, item.seller_id))
    return out


def _listing_entry(d: market_match.Decision, category: str) -> dict[str, Any]:
    """Una publicación para el dashboard (igual o similar). Todo lo que viene
    de ML ya está saneado en el candidato; acá solo se acota el largo."""
    c = d.candidate
    return {
        "ml_id": c.id,
        "title": c.name[:160],
        "permalink": c.permalink,
        "origin": c.origin,
        "category": category,
        "source": d.source,
        "image_score": round(d.image_score, 3) if d.image_score is not None else None,
        "name_score": round(d.name_score, 3),
        "confidence": d.confidence,
        "reason": (d.reason or "")[:300],
        "differences": list(d.differences),
        "notes": list(d.notes),
        "brand": c.brand[:40] or None,
        "image_url": (c.image_urls or [None])[0],
        "specs": market_specs.extract(c.name).as_dict(),
    }


# Fuentes que CONFIRMAN un similar: el juez, el chequeo de medidas o una persona.
# Un similar "sin confirmar" (banda ambigua sin juez) no se sabe de qué es.
_CONFIRMED_SOURCES = (market_match.SOURCE_LLM, market_match.SOURCE_SPECS, market_match.SOURCE_MANUAL)
# Si la diferencia es la cantidad o la capacidad, su precio no es comparable con el
# nuestro (un pack x4 cuesta 4 veces; un termo de 1 L no vale lo que uno de 500 ml).
_NOT_COMPARABLE = frozenset({market_specs.DIFF_QUANTITY, market_specs.DIFF_CAPACITY})


def _feeds_estimate(d: market_match.Decision) -> bool:
    """¿Este similar puede entrar al color ESTIMADO? Solo si está confirmado y no
    difiere en cantidad ni en capacidad. Los demás se siguen mostrando, con su
    etiqueta, pero no mueven el estimado."""
    return d.source in _CONFIRMED_SOURCES and not (set(d.differences) & _NOT_COMPARABLE)


def _ref_entry(ctx: RunContext, d: market_match.Decision, category: str,
               prefetched: dict[str, Listings]) -> dict[str, Any]:
    """Una publicación SIMILAR o DIFERENTE para mostrar: con su precio de
    referencia (sin filtrar por vendedores), que no entra a ningún cálculo real.
    `est_ok` dice si ese precio cuenta para el color ESTIMADO: similar confirmado
    (`_feeds_estimate`) y con precio en pesos (y, en la web, con ventas
    suficientes)."""
    entry = _listing_entry(d, category)
    c = d.candidate
    price = c.price_cents
    if price is None and c.id in prefetched:
        price = _ars_median(prefetched[c.id][0])
    in_pesos = price is not None and price > 0 and (not c.currency or c.currency.upper() == CURRENCY)
    price_ok = _web_price(ctx, c) is not None if c.origin == ORIGIN_WEB else in_pesos
    entry.update(price_cents=price if in_pesos else None,
                 est_ok=bool(category == "similar" and _feeds_estimate(d) and price_ok and in_pesos),
                 seller=c.seller[:60] or None, sold_quantity=c.sold_quantity)
    return entry


def _manual_other_entry(ctx: RunContext, c: MlCandidate, our_name: str) -> dict[str, Any]:
    """Una publicación que una persona marcó "No es el mismo": no se compara de
    nuevo (ni se baja su foto), pero se muestra, con su precio, como DIFERENTE."""
    d = market_match.Decision(c, None, market_match.name_score(our_name, c.name), market_match.NO,
                              market_match.SOURCE_MANUAL, reason=_NOT_SAME_REASON)
    return _ref_entry(ctx, d, "diferente", {})


_NOT_SAME_REASON = "una persona la marcó «No es el mismo»"
_SAME_REASON = "una persona la marcó «Es el mismo»"


def _apply_prices(snap: MarketPriceSnapshot, prices: list[int], n_sellers: int,
                  *, green_min: float, yellow_min: float) -> None:
    """Mediana, mínimo, cantidad, ganancia y color a partir de los precios que
    cuentan. Lo comparten la corrida y el recálculo de "No es el mismo"."""
    snap.ml_status = OK
    snap.ml_error = None
    snap.ml_median_cents = semaforo.median_cents(prices)
    snap.ml_min_cents = min(prices)
    snap.ml_listing_count = len(prices)
    snap.ml_seller_count = n_sellers
    margin = semaforo.estimated_margin_pct(
        snap.ml_median_cents, snap.our_price_cents, snap.commission_pct or 0.0,
        snap.shipping_cents or 0,
    )
    # El color con el margen exacto; redondeado solo para guardar/mostrar.
    snap.color = semaforo.color(
        margin, snap.our_price_cents, snap.ml_median_cents, green_min, yellow_min,
    )
    snap.est_margin_pct = None if margin is None else round(margin, 2)


def _clear_prices(snap: MarketPriceSnapshot) -> None:
    snap.ml_median_cents = snap.ml_min_cents = snap.est_margin_pct = None
    snap.ml_listing_count = snap.ml_seller_count = 0
    snap.match_source = snap.match_origin = None
    snap.match_confidence = None


def _score_summary(snap: MarketPriceSnapshot, decisions: list[market_match.Decision]) -> bool:
    """Acumula los puntajes máximos en el snapshot. False si ninguna publicación
    tuvo score de imagen (CLIP o el índice no estaban)."""
    images = [d.image_score for d in decisions if d.image_score is not None]
    if images:
        snap.image_score_max = max(images + ([snap.image_score_max] if snap.image_score_max is not None else []))
    names = [d.name_score for d in decisions]
    if names:
        snap.name_score_max = max(names + ([snap.name_score_max] if snap.name_score_max is not None else []))
    return bool(images)


def _judge_targets(decisions: list[market_match.Decision], *, with_brand_check: bool,
                   ) -> list[market_match.Decision]:
    """A quién se le pregunta al juez: la banda ambigua y, en la web, los matches
    por reglas que declaran una marca (Nico: marca conocida = similar)."""
    targets = [d for d in decisions if d.verdict == market_match.AMBIGUOUS]
    if with_brand_check:
        branded = [d for d in decisions
                   if d.verdict == market_match.MATCH and d.source != market_match.SOURCE_MANUAL
                   and _declared_brand(d.candidate)]
        branded.sort(key=lambda d: d.image_score or 0.0, reverse=True)
        targets += branded
    return targets[:market_judge.MAX_CANDIDATES]


async def _decide(ctx: RunContext, product: VendureProduct, w: _Work, snap: MarketPriceSnapshot,
                  candidates: list[MlCandidate], prefetched: dict[str, Listings],
                  *, web: bool) -> list[market_match.Decision] | None:
    """Puntúa las publicaciones y decide igual / similar / diferente. None si no
    hubo score de imagen.

    Nada se descarta: lo SIMILAR queda en `w.similars` y lo DIFERENTE (con su
    motivo) en `w.others`; lo IGUAL lo arma quien llama. Lo que no se pudo
    confirmar (banda ambigua sin juez, sin cupo o sin respuesta) queda SIMILAR,
    marcado "sin confirmar": se muestra y alimenta solo el color ESTIMADO.
    Una persona manda: "Es el mismo" = IGUAL y no se vuelve a juzgar."""
    decisions = await market_match.score_candidates(
        product, candidates, ctx.thresholds, scorer=ctx.scorer,
        max_candidates=len(candidates) if web else market_match.CANDIDATES_TO_SCORE,
    )
    scored = {d.candidate.id for d in decisions}
    # Una publicación que una persona promovió entra aunque no haya quedado entre
    # las puntuadas (nombre menos parecido que las demás).
    for c in candidates:
        if c.id in w.promoted and c.id not in scored:
            decisions.append(market_match.Decision(
                c, None, market_match.name_score(product.name, c.name), market_match.NO))
    if not _score_summary(snap, decisions):
        # Sin ningún score de foto (CLIP o el índice no estaban) no se puede decir si
        # se parecen: el producto no se evalúa, pero lo que devolvió ML se muestra.
        for d in decisions:
            d.reason = market_match.explain_different(d, ctx.thresholds)
            w.others.append(_ref_entry(ctx, d, "diferente", prefetched))
        return None
    for d in decisions:
        if d.candidate.id in w.promoted:
            d.verdict, d.source, d.reason, d.differences = (
                market_match.MATCH, market_match.SOURCE_MANUAL, _SAME_REASON, [])
    snap.ambiguous_count += sum(1 for d in decisions if d.verdict == market_match.AMBIGUOUS)
    targets = _judge_targets(decisions, with_brand_check=web)
    if targets and ctx.judge_max_calls > 0:
        await _consult_judge(ctx, product, targets, prefetched)
    if ctx.spec_check:
        for d in decisions:
            market_match.apply_specs(d, product.name, w.specs,
                                     dim_tol_pct=ctx.dim_tol_pct, weight_tol_pct=ctx.weight_tol_pct)
    for d in decisions:
        if d.verdict == market_match.AMBIGUOUS:       # nadie la confirmó ni la descartó
            d.verdict, d.source = market_match.SIMILAR, market_match.SOURCE_UNCONFIRMED
            d.reason = d.reason or "parecido en foto y nombre, sin confirmar"
        if d.verdict == market_match.NO:
            d.reason = market_match.explain_different(d, ctx.thresholds)
    for d in decisions:
        if d.verdict == market_match.SIMILAR:
            w.similars.append(_ref_entry(ctx, d, "similar", prefetched))
        elif d.verdict == market_match.NO:
            w.others.append(_ref_entry(ctx, d, "diferente", prefetched))
    return decisions


async def _match_via_api(ctx: RunContext, product: VendureProduct, snap: MarketPriceSnapshot,
                         w: _Work) -> MarketPriceSnapshot:
    """Fichas de catálogo por la API de ML (como hasta ahora)."""
    try:
        candidates = await ctx.ml.search(w.query)
        if not candidates:
            shorter = market_match.fallback_query(product.name)
            if shorter:
                candidates = await ctx.ml.search(shorter)
    except BudgetExhausted as exc:
        return _mark(snap, SKIPPED, f"budget ML agotado: {exc}")
    except meli.MeliError as exc:
        return _mark(snap, FAILED, f"búsqueda ML: {exc}")
    candidates = _split_excluded(ctx, product, w, candidates)
    snap.candidates_count = len(candidates)
    if not candidates:
        return _mark(snap, NO_DATA, "ML no devolvió fichas para el título")

    prefetched: dict[str, Listings] = {}
    decisions = await _decide(ctx, product, w, snap, candidates, prefetched, web=False)
    if decisions is None:
        return _mark(snap, SKIPPED, "sin score de imagen (CLIP o índice no disponibles)")

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
    why: dict[str, str] = {d.candidate.id: "no se consultó su precio (tope de fichas por producto)"
                           for d in matches[MAX_MATCHED_PRODUCTS:]}
    try:
        for d in matches[:MAX_MATCHED_PRODUCTS]:
            try:
                listings, raw = prefetched.get(d.candidate.id) or await ctx.ml.listings(d.candidate.id)
            except meli.MeliError as exc:
                ml_errors.append(str(exc))
                why[d.candidate.id] = "no se pudo consultar su precio (ML falló)"
                continue
            if not ctx.sold_quantity_probed and raw.get("results"):
                ctx.sold_quantity_probed = True
                if not await asyncio.to_thread(probe_recorded, PROBE_SOLD_QUANTITY_KEY):
                    await asyncio.to_thread(
                        record_probe, PROBE_SOLD_QUANTITY_KEY,
                        {"present": payload_has_sold_quantity(raw), "product": d.candidate.id},
                    )
            accepted = await _accepted_prices(ctx, listings)
            if not accepted:
                why[d.candidate.id] = "sin vendedores que cuenten (pocas ventas o sin precio en pesos)"
                continue
            cents = [p for p, _ in accepted]
            prices.extend(cents)
            sellers.update(sid for _, sid in accepted if sid)
            matched_json.append({
                **_listing_entry(d, "igual"),
                "listings": len(accepted),
                "min_cents": min(cents),
                "median_cents": semaforo.median_cents(cents),
                "prices_cents": cents[:20],
                "sellers": sorted({sid for _, sid in accepted if sid})[:20],
            })
    except BudgetExhausted as exc:
        _record_unpriced(w, matches, matched_json, why, "se agotó el cupo de ML antes de pedir su precio")
        return _mark(snap, SKIPPED, f"budget ML agotado: {exc}")

    _record_unpriced(w, matches, matched_json, why)
    snap.matched_listings = json.dumps(matched_json, ensure_ascii=False)
    if not prices:
        if ml_errors and not matched_json:
            return _mark(snap, FAILED, f"vendedores ML: {ml_errors[0]}")
        return _mark(snap, NO_DATA, "las fichas no tienen vendedores que cuenten")

    snap.match_origin = ORIGIN_API
    _apply_prices(snap, prices, len(sellers), green_min=ctx.green_min, yellow_min=ctx.yellow_min)
    return snap


def _split_excluded(ctx: RunContext, product: VendureProduct, w: _Work,
                    candidates: list[MlCandidate]) -> list[MlCandidate]:
    """Las que una persona marcó "No es el mismo" no se vuelven a juzgar: pasan
    directo a DIFERENTES (con su precio, para poder darlas vuelta con "Es el
    mismo"). Devuelve las demás."""
    keep: list[MlCandidate] = []
    for c in candidates:
        if c.id in w.excluded and c.id not in w.promoted:
            w.others.append(_manual_other_entry(ctx, c, product.name))
        else:
            keep.append(c)
    return keep


def _record_unpriced(w: _Work, matches: list[market_match.Decision], priced: list[dict[str, Any]],
                     why: dict[str, str], default: str = "sin precio que cuente") -> None:
    """Los IGUAL que no llegaron a tener precio (sin vendedores, pocas ventas, el
    tope de fichas, un error de ML) se guardan igual: son idénticos, solo que no
    suman a la mediana."""
    done = {m["ml_id"] for m in priced}
    for d in matches:
        if d.candidate.id in done:
            continue
        entry = _listing_entry(d, "igual")
        entry.update(listings=0, min_cents=None, median_cents=None, prices_cents=[], sellers=[],
                     price_cents=d.candidate.price_cents, seller=d.candidate.seller[:60] or None,
                     sold_quantity=d.candidate.sold_quantity)
        entry["notes"] = [*entry["notes"], why.get(d.candidate.id, default)]
        w.igual_unpriced.append(entry)


def _web_price(ctx: RunContext, c: MlCandidate) -> int | None:
    """Precio de una publicación de la web si cuenta: en pesos y con ventas
    suficientes (la web publica las ventas del ÍTEM, en baldes; ventas
    desconocidas no descartan, igual que con la API)."""
    if c.price_cents is None or c.price_cents <= 0:
        return None
    if c.currency and c.currency.upper() != CURRENCY:
        return None
    if c.sold_quantity is not None and c.sold_quantity < ctx.min_seller_sales:
        return None
    return c.price_cents


def _with_note(snap: MarketPriceSnapshot, note: str) -> MarketPriceSnapshot:
    snap.ml_error = browser_fetch.redact(f"{snap.ml_error} · {note}" if snap.ml_error else note)[:300]
    return snap


async def _web_search(ctx: RunContext, product: VendureProduct, snap: MarketPriceSnapshot,
                      query: str) -> market_ml_web.WebSearch:
    """Busca el título en la web de ML (y, si no hay nada, las primeras palabras)
    y deja los bytes y el estado en el snapshot y en la corrida."""
    assert ctx.web is not None
    res = await ctx.web.search(query)
    total_bytes, searches = res.bytes, int(res.kind not in ("off", "budget"))
    if res.kind == "empty":
        shorter = market_match.fallback_query(product.name)
        if shorter and ctx.web.active:
            second = await ctx.web.search(shorter)
            total_bytes += second.bytes
            searches += int(second.kind not in ("off", "budget"))
            res = second if second.kind != "empty" else res
    snap.web_searches += searches
    snap.web_bytes += total_bytes
    snap.web_state = res.kind
    if total_bytes or res.kind == "blocked":
        await asyncio.to_thread(_add_usage, ctx.run_id, web_bytes=total_bytes,
                                web_blocked=int(res.kind == "blocked"))
    return res


async def _match_via_web(ctx: RunContext, product: VendureProduct, snap: MarketPriceSnapshot,
                         w: _Work) -> MarketPriceSnapshot:
    """Búsqueda del título en la web de ML cuando la API no dio un IGUAL con
    precio. Si no sale nada el snapshot queda como lo dejó la API, con el motivo
    de la web agregado."""
    res = await _web_search(ctx, product, snap, w.query)
    if res.kind != "ok":
        return _with_note(snap, f"ML web: {res.reason}")

    candidates = _split_excluded(ctx, product, w, res.candidates)
    snap.candidates_count += len(candidates)
    prefetched: dict[str, Listings] = {}
    decisions = await _decide(ctx, product, w, snap, candidates, prefetched, web=True) if candidates else None
    if decisions is None:
        return _with_note(snap, "ML web: sin score de imagen" if candidates else "ML web: sin publicaciones nuevas")

    matches = [d for d in decisions if d.verdict == market_match.MATCH]
    matches.sort(key=lambda d: (d.image_score or 0.0), reverse=True)
    prices: list[int] = []
    sellers: set[str] = set()
    matched_json: list[dict[str, Any]] = []
    why: dict[str, str] = {}
    for d in matches:
        price = _web_price(ctx, d.candidate)
        if price is None:
            why[d.candidate.id] = "sin precio en pesos o con pocas ventas"
            continue
        prices.append(price)
        if d.candidate.seller:
            sellers.add(d.candidate.seller)
        matched_json.append({
            **_listing_entry(d, "igual"),
            "listings": 1, "min_cents": price, "median_cents": price,
            "prices_cents": [price],
            "sellers": [d.candidate.seller[:60]] if d.candidate.seller else [],
            "seller": d.candidate.seller[:60] or None,
            "sold_quantity": d.candidate.sold_quantity,
        })
    _record_unpriced(w, matches, matched_json, why)
    if not prices:
        note = ("las publicaciones iguales no tienen ventas suficientes" if matches
                else f"ninguna publicación es igual ({len(w.similars)} similares)" if w.similars
                else "ninguna publicación es igual")
        return _with_note(snap, f"ML web: {note}")

    best = next(d for d in matches if _web_price(ctx, d.candidate) is not None)
    snap.matched_listings = json.dumps(matched_json, ensure_ascii=False)
    snap.match_source, snap.match_confidence = best.source, best.confidence
    snap.match_origin = ORIGIN_WEB
    _apply_prices(snap, prices, len(sellers), green_min=ctx.green_min, yellow_min=ctx.yellow_min)
    return snap


def _dedupe(entries: list[dict[str, Any]], taken: set[str]) -> list[dict[str, Any]]:
    """Una publicación una sola vez (la primera) y que no esté ya en otra lista."""
    out: list[dict[str, Any]] = []
    for e in entries:
        ml_id = e.get("ml_id")
        if ml_id and ml_id not in taken:
            taken.add(ml_id)
            out.append(e)
    return out


def _by_similarity(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Las más parecidas primero (foto y después nombre)."""
    return sorted(entries, key=lambda e: (e.get("image_score") if e.get("image_score") is not None else -1.0,
                                          e.get("name_score") or 0.0), reverse=True)


def _is_manual(entry: dict[str, Any]) -> bool:
    return entry.get("source") == market_match.SOURCE_MANUAL


def _cap(similar: list[dict[str, Any]], other: list[dict[str, Any]], *, n_igual: int, keep: int,
         ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Recorta a `keep` publicaciones en total, lo más parecido primero y los
    SIMILARES antes que los DIFERENTES. Nunca se recortan los IGUAL (definen el
    precio) ni lo que una persona marcó (`source: manual`): esa card tiene que
    seguir visible para poder darla vuelta. Ocupan lugar: lo que queda para el
    resto es `keep` menos ellos (o nada)."""
    manual_s = [e for e in similar if _is_manual(e)]
    manual_o = [e for e in other if _is_manual(e)]
    room = max(0, keep - n_igual - len(manual_s) - len(manual_o))
    free_s = _by_similarity([e for e in similar if not _is_manual(e)])[:room]
    free_o = _by_similarity([e for e in other if not _is_manual(e)])[:max(0, room - len(free_s))]
    return _by_similarity([*manual_s, *free_s]), _by_similarity([*manual_o, *free_o])


def _set_estimate(snap: MarketPriceSnapshot, similar: list[dict[str, Any]],
                  *, green_min: float, yellow_min: float) -> None:
    """Color ESTIMADO por la mediana de los SIMILARES CONFIRMADOS (`est_ok`: juez,
    medidas o una persona, sin diferencia de cantidad ni de capacidad y con precio
    en pesos), con la misma fórmula de ganancia, cuando NO hay IGUAL. Si no queda
    ninguno no hay color estimado (la lista se muestra igual). Va en campos
    aparte: el color real (`color`) y sus contadores no se tocan nunca por esto."""
    snap.estimated_color = snap.estimated_margin_pct = snap.estimated_median_cents = None
    snap.estimated_listing_count = 0
    snap.estimated_from = None
    if snap.ml_status != NO_DATA:
        return
    prices = [int(e["price_cents"]) for e in similar if e.get("est_ok") and e.get("price_cents")]
    if not prices:
        return
    median = semaforo.median_cents(prices)
    margin = semaforo.estimated_margin_pct(
        median, snap.our_price_cents, snap.commission_pct or 0.0, snap.shipping_cents or 0)
    color = semaforo.color(margin, snap.our_price_cents, median, green_min, yellow_min)
    if color == semaforo.SIN_DATO or margin is None:
        return
    snap.estimated_color, snap.estimated_margin_pct = color, round(margin, 2)
    snap.estimated_median_cents, snap.estimated_listing_count = median, len(prices)
    snap.estimated_from = ESTIMATED_FROM_SIMILAR


def _set_state(snap: MarketPriceSnapshot, *, has_igual: bool) -> None:
    """Qué devolvió ML, en una palabra. "Sin dato" (`ninguno`) es solo cuando ML
    no devolvió nada; si falló o no se evaluó no hay estado."""
    if snap.ml_status == OK:
        snap.match_state = STATE_IGUAL
    elif snap.ml_status in (FAILED, SKIPPED):
        snap.match_state = None
    elif has_igual:
        snap.match_state = STATE_IGUAL_NO_PRICE
    elif snap.similar_count:
        snap.match_state = STATE_SIMILAR
    elif snap.other_count:
        snap.match_state = STATE_DIFFERENT
    else:
        snap.match_state = STATE_NONE


def _store_listings(snap: MarketPriceSnapshot, igual: list[dict[str, Any]], unpriced: list[dict[str, Any]],
                    similar: list[dict[str, Any]], other: list[dict[str, Any]],
                    *, keep: int, green_min: float, yellow_min: float) -> None:
    """Guarda las tres listas (idénticos, similares, diferentes: nada se descarta,
    hasta `keep` en total), el color estimado y el estado del producto.

    Solo lo IGUAL con precio arma la mediana, el mínimo, la ganancia y el color
    real (ya calculados por quien llama). Lo SIMILAR da el color estimado, que va
    aparte. Lo DIFERENTE solo se muestra."""
    taken = {e["ml_id"] for e in igual if e.get("ml_id")}
    unpriced = _dedupe(unpriced, taken)
    similar = _dedupe(similar, taken)
    other = _dedupe(other, taken)
    similar, other = _cap(similar, other, n_igual=len(igual) + len(unpriced), keep=keep)
    if igual or snap.matched_listings is not None:
        snap.matched_listings = json.dumps(igual, ensure_ascii=False)
    snap.unpriced_listings = json.dumps(unpriced, ensure_ascii=False) if unpriced else None
    snap.similar_count = len(similar)
    snap.similar_listings = json.dumps(similar, ensure_ascii=False) if similar else None
    snap.other_count = len(other)
    snap.other_listings = json.dumps(other, ensure_ascii=False) if other else None
    _set_estimate(snap, similar, green_min=green_min, yellow_min=yellow_min)
    _set_state(snap, has_igual=bool(igual or unpriced))


async def evaluate_product(ctx: RunContext, product: VendureProduct) -> MarketPriceSnapshot:
    """Todo el camino de UN producto → snapshot (sin guardar): Mercado Libre y, en la
    MISMA corrida, las tiendas (Casa Perfecta, Gadnic…). Las tiendas son referencia: no
    cambian el color salvo `pm_stores_affect_color` (ver store_match)."""
    snap = await _evaluate_ml(ctx, product)
    return await store_match.attach(ctx, product, snap, judge=_consult_judge, brand_of=_declared_brand)


async def _evaluate_ml(ctx: RunContext, product: VendureProduct) -> MarketPriceSnapshot:
    """El camino de ML para UN producto.

    1. Fichas de catálogo por la API de ML. Con un IGUAL con precio, listo.
    2. Si no, el título en la web de ML (si la fuente está prendida).
    3. Siempre se guarda lo que devolvió ML, clasificado: idénticos, similares y
       diferentes. El color real sale solo de los idénticos; sin idénticos pero con
       similares hay un color estimado aparte.
    """
    snap = _base_snapshot(ctx, product)

    our = semaforo.pick_our_price(product.priced_variants or [], ctx.tier_policy)
    if our is None:
        return _mark(snap, SKIPPED, "sin precio en Vendure")
    snap.our_price_cents, snap.variant_id, snap.tier_used = our.price_cents, our.variant_id, our.tier_used
    if our.currency and our.currency.upper() != CURRENCY:
        # ML se compara en pesos: un precio en otra moneda daría un margen sin sentido.
        return _mark(snap, SKIPPED, f"nuestro precio está en {our.currency}, no en {CURRENCY}")

    if not _our_photos(product):
        return _mark(snap, SKIPPED, "sin foto propia para comparar")
    if not ctx.can_score(product):
        return _mark(snap, SKIPPED, "sin fotos en el índice CLIP (o CLIP apagado)")
    query = market_match.search_query(product.name)
    if not query:
        return _mark(snap, SKIPPED, "sin nombre para buscar")

    specs = _our_specs(product, our.variant_id)
    if specs is not None:
        snap.our_specs = json.dumps(specs.as_dict())
    w = _Work(query=query, excluded=ctx.excluded.get(product.id, frozenset()), specs=specs,
              promoted=ctx.promoted.get(product.id, frozenset()))

    snap = await _match_via_api(ctx, product, snap, w)
    if snap.ml_status != OK and ctx.web is not None:
        snap = await _match_via_web(ctx, product, snap, w)
    _store_listings(snap, _json_list(snap.matched_listings), w.igual_unpriced, w.similars, w.others,
                    keep=ctx.keep_listings, green_min=ctx.green_min, yellow_min=ctx.yellow_min)
    return snap


# ─── La corrida ───────────────────────────────────────────────────


async def _close_judge_client(ctx: RunContext) -> None:
    if ctx.judge_client is None:
        return
    try:
        await ctx.judge_client.close()
    except Exception as exc:  # noqa: BLE001
        log.debug("No se pudo cerrar el cliente del juez: %s", exc)
    ctx.judge_client = None


def _context(run_id: int, ml: MlMarket, web: market_ml_web.MlWebSource | None = None,
             excluded: dict[str, frozenset[str]] | None = None,
             promoted: dict[str, frozenset[str]] | None = None) -> RunContext:
    return RunContext(
        run_id=run_id,
        ml=ml,
        web=web,
        excluded=excluded or {},
        promoted=promoted or {},
        keep_listings=int(runtime.get("pm_ml_keep_listings")),
        spec_check=bool(int(runtime.get("pm_spec_check"))),
        dim_tol_pct=float(runtime.get("pm_dim_tol_pct")),
        weight_tol_pct=float(runtime.get("pm_weight_tol_pct")),
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
        _finalize_run(int(run.id),  # type: ignore[arg-type]
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

    if not meli.enabled():
        msg = "MELI_CLIENT_ID / MELI_CLIENT_SECRET no configurados"
        log.error("price_monitor: %s", msg)
        status = _finalize_run(run_id, error=msg, force_status=RUN_FAILED)
        return {"run_id": run_id, "status": status, "error": msg}

    try:
        products = await VendureClient().fetch_all_products_priced()
    except Exception as exc:  # noqa: BLE001
        log.exception("price_monitor: Vendure no respondió")
        error = f"Vendure: {type(exc).__name__}: {exc}"
        status = _finalize_run(run_id, error=error, force_status=RUN_FAILED)
        return {"run_id": run_id, "status": status, "error": error}

    try:
        return await _evaluate_catalog(run_id, trigger, products)
    except Exception as exc:  # noqa: BLE001
        # Un bug no puede dejar la corrida `running` para siempre: se reintentaría
        # en cada arranque y reventaría igual. (Un reinicio del proceso NO pasa
        # por acá — eso sí se retoma.)
        log.exception("price_monitor: la corrida #%s reventó", run_id)
        error = f"{type(exc).__name__}: {exc}"
        status = _finalize_run(run_id, error=error, force_status=RUN_FAILED)
        return {"run_id": run_id, "status": status, "error": error}


def _make_web(run_id: int) -> tuple[market_ml_web.MlWebSource | None, str]:
    """La fuente "ML web" de esta corrida y su estado para `run.web_status`.
    Sin proxy, sin browser o con el cupo en 0 queda apagada y se avisa: NO se
    intenta desde la IP del datacenter (ML la bloquea)."""
    reason = market_ml_web.disabled_reason()
    if reason:
        log.warning("price_monitor #%s: ML web apagada: %s", run_id, reason)
        return None, f"apagada: {reason}"
    return market_ml_web.from_runtime(on_reserve=_usage_increment(run_id, web_searches=1)), "ok"


async def _evaluate_catalog(run_id: int, trigger: str, products: list[VendureProduct]) -> dict[str, Any]:
    # Un id repetido en el listado de Vendure (paginado que se corre mientras
    # se lee) se evalúa una sola vez (QA bug 5).
    unique: dict[str, VendureProduct] = {}
    include_disabled = bool(int(runtime.get("pm_include_disabled") or 0))
    for p in products:
        if p.enabled or include_disabled:
            unique.setdefault(p.id, p)
    enabled = list(unique.values())
    done = _done_product_ids(run_id)
    pending = [p for p in enabled if p.id not in done]
    last = _last_measured({p.id for p in pending})
    never = datetime.min
    pending.sort(key=lambda p: last.get(p.id) or never)
    _update_run(run_id, total_products=len(enabled), processed=len(done))
    log.info("price_monitor #%s (%s): %d productos (%d deshabilitados), %d ya hechos, %d pendientes",
             run_id, trigger, len(enabled), sum(1 for p in enabled if not p.enabled), len(done),
             len(pending))

    budget_now = await asyncio.to_thread(ml_budget_status)
    if budget_now["remaining"] <= 0:
        msg = f"sin cupo de ML hoy: usados {budget_now['used']} de {budget_now['budget']}"
        log.warning("price_monitor #%s: %s; no se evalúa nada", run_id, msg)
        status = _finalize_run(run_id, error=msg, force_status=RUN_SKIPPED)
        return {"run_id": run_id, "status": status, "error": msg,
                "counts": {OK: 0, NO_DATA: 0, FAILED: 0, SKIPPED: 0}}

    await _ensure_clip_index()
    # Tiendas: si su índice está viejo se refresca (dentro del cupo) y se carga para comparar.
    stores = await store_match.prepare()

    budget = int(budget_now["budget"])
    concurrency = max(1, int(runtime.get("pm_ml_concurrency")))
    counts: dict[str, int] = {OK: 0, NO_DATA: 0, FAILED: 0, SKIPPED: 0}

    web, web_status = _make_web(run_id)
    excluded = await asyncio.to_thread(match_feedback.load_excluded)
    promoted = await asyncio.to_thread(match_feedback.load_promoted)
    async with MlMarket(budget=budget, on_reserve=_usage_increment(run_id, ml_requests_used=1)) as ml:
        ctx = _context(run_id, ml, web, excluded, promoted)
        ctx.extra["stores"] = stores
        try:
            sem = asyncio.Semaphore(concurrency)

            async def _one(product: VendureProduct) -> None:
                async with sem:
                    try:
                        snap = await evaluate_product(ctx, product)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("price_monitor: %s reventó", product.id)
                        snap = _mark(_base_snapshot(ctx, product), FAILED,
                                     f"{type(exc).__name__}: {exc}")
                    status = snap.ml_status
                    try:
                        await asyncio.to_thread(_persist, snap, run_id)
                    except Exception:  # noqa: BLE001
                        log.exception("price_monitor: no se pudo guardar el snapshot de %s", product.id)
                    counts[status] = counts.get(status, 0) + 1

            # `_one` ya atrapa todo lo esperable; return_exceptions evita que un bug
            # en un producto deje a los demás corriendo huérfanos tras el gather.
            for result in await asyncio.gather(*(_one(p) for p in pending), return_exceptions=True):
                if isinstance(result, Exception):
                    log.error("price_monitor #%s: error no atrapado en un producto: %r", run_id, result)

            if ctx.first_category_id and not await asyncio.to_thread(probe_recorded, PROBE_LISTING_PRICES_KEY):
                await ml.probe_listing_prices(ctx.first_category_id)

            if web is not None:
                web_status = web.status_text()
            status = _finalize_run(run_id, web_status=web_status)
        finally:
            await _close_judge_client(ctx)
            if web is not None:
                await web.aclose()

    with Session(engine) as s:
        run = s.get(PriceMonitorRun, run_id)
    log.info("price_monitor terminado: corrida #%s %s — %s, %d requests ML (%d esta vez), "
             "%d llamadas LLM (USD %.4f), ML web: %s (%d búsquedas, %.1f MB)",
             run_id, status, counts, run.ml_requests_used, ml.requests_used, run.llm_calls,
             run.llm_cost_usd, run.web_status, run.web_searches, run.web_bytes / 1_048_576)
    return {"run_id": run_id, "status": status, "counts": counts,
            "ml_requests_used": run.ml_requests_used, "llm_calls": run.llm_calls}


# ─── Retención ────────────────────────────────────────────────────


def prune_snapshots(retention_days: int) -> tuple[int, int]:
    """Borra los snapshots de más de `retention_days` días, salvo el ÚLTIMO de cada
    producto (es el dato vigente: color anterior, última medición), y después las
    corridas viejas que se quedaron sin snapshots. Una corrida en curso o con
    snapshots vigentes no se toca. Devuelve (snapshots, corridas) borrados; con
    0 días no hace nada."""
    if retention_days <= 0:
        return 0, 0
    cutoff = utcnow() - timedelta(days=retention_days)
    with Session(engine) as s:
        latest = select(func.max(MarketPriceSnapshot.id)).group_by(MarketPriceSnapshot.product_id)
        snaps = s.execute(
            delete(MarketPriceSnapshot).where(
                MarketPriceSnapshot.captured_at < cutoff,  # type: ignore[arg-type]
                MarketPriceSnapshot.id.notin_(latest),  # type: ignore[union-attr]
            )
        ).rowcount or 0
        used = select(MarketPriceSnapshot.run_id).distinct()
        runs = s.execute(
            delete(PriceMonitorRun).where(
                PriceMonitorRun.started_at < cutoff,  # type: ignore[arg-type]
                PriceMonitorRun.status != RUN_RUNNING,
                PriceMonitorRun.id.notin_(used),  # type: ignore[union-attr]
            )
        ).rowcount or 0
        s.commit()
    if snaps or runs:
        log.info("prune: %d snapshots y %d corridas del semáforo de más de %d días", snaps, runs, retention_days)
    return int(snaps), int(runs)


# ─── "No es el mismo" ─────────────────────────────────────────────


def _json_list(raw: str | None) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []


def _approx_prices(entry: dict[str, Any]) -> list[Any]:
    """Los precios de una publicación guardada ANTES de que se guardara la lista
    (`prices_cents`): el mínimo real una vez y el resto en la mediana. Así sacar
    otra publicación no hace perder el mínimo (con la mediana repetida
    `listings` veces el mínimo subía)."""
    n = max(1, int(entry.get("listings") or 1))
    low, mid = entry.get("min_cents"), entry.get("median_cents")
    if n == 1 or not isinstance(low, (int, float)) or low <= 0:
        return [mid if isinstance(mid, (int, float)) and mid > 0 else low] * n
    return [low] + [mid] * (n - 1)


def _lists(snap: MarketPriceSnapshot) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """(idénticos con precio, idénticos sin precio, similares, diferentes) de un snapshot."""
    return (_json_list(snap.matched_listings), _json_list(snap.unpriced_listings),
            _json_list(snap.similar_listings), _json_list(snap.other_listings))


def listing_category(snap: MarketPriceSnapshot, ml_id: str) -> str | None:
    """En qué lista del snapshot está una publicación: igual | similar | diferente."""
    priced, unpriced, similar, other = _lists(snap)
    if any(m.get("ml_id") == ml_id for m in (*priced, *unpriced)):
        return "igual"
    if any(m.get("ml_id") == ml_id for m in similar):
        return "similar"
    if any(m.get("ml_id") == ml_id for m in other):
        return "diferente"
    return None


def _entry_prices(m: dict[str, Any]) -> list[int]:
    """Los precios con los que una publicación IGUAL aporta a la mediana. Sin la
    lista guardada (fila anterior) se aproximan; con la lista vacía (idéntica sin
    precio que cuente) no aporta nada."""
    listed = m.get("prices_cents") if "prices_cents" in m else _approx_prices(m)
    return [int(p) for p in listed or [] if isinstance(p, (int, float)) and p > 0]


def _rebuild(snap: MarketPriceSnapshot, priced: list[dict], unpriced: list[dict], similar: list[dict],
             other: list[dict], *, real_changed: bool, reason: str = "") -> None:
    """Deja el snapshot coherente con las tres listas después de una corrección de
    una persona: si cambiaron los IDÉNTICOS recalcula mediana, mínimo, ganancia y
    color real; siempre rehace el color estimado, el estado y los contadores de la
    lista. No toca el precio nuestro ni nada de Vendure."""
    green, yellow = float(runtime.get("pm_green_min_pct")), float(runtime.get("pm_yellow_min_pct"))
    if real_changed:
        prices: list[int] = []
        sellers: set[str] = set()
        for m in priced:
            prices.extend(_entry_prices(m))
            sellers.update(str(x) for x in (m.get("sellers") or []))
        if prices:
            best = priced[0]
            snap.match_source, snap.match_confidence = best.get("source"), best.get("confidence")
            snap.match_origin = best.get("origin")
            n_sellers = len(sellers) or min(snap.ml_seller_count or 1, len(prices))
            _apply_prices(snap, prices, n_sellers, green_min=green, yellow_min=yellow)
        else:
            _clear_prices(snap)
            _mark(snap, NO_DATA, reason or "no quedan publicaciones idénticas con precio")
    _store_listings(snap, priced, unpriced, similar, other, keep=int(runtime.get("pm_ml_keep_listings")),
                    green_min=green, yellow_min=yellow)


def _as_manual_other(entry: dict[str, Any]) -> dict[str, Any]:
    """Una publicación que una persona marcó "No es el mismo", para la lista de
    diferentes (con su precio de referencia; ya no aporta a ningún cálculo)."""
    prices = _entry_prices(entry)
    price = entry.get("price_cents") or (prices[0] if prices else None) or entry.get("median_cents")
    clean = {k: v for k, v in entry.items()
             if k not in ("prices_cents", "sellers", "listings", "min_cents", "median_cents", "est_ok")}
    return {**clean, "category": "diferente", "source": market_match.SOURCE_MANUAL,
            "reason": _NOT_SAME_REASON, "differences": [], "price_cents": _clean_price(price)}


def _as_manual_igual(entry: dict[str, Any]) -> dict[str, Any]:
    """Una publicación que una persona marcó "Es el mismo", como IDÉNTICA: su
    precio (el que tenía de referencia) pasa a contar."""
    price = _clean_price(entry.get("price_cents"))
    seller = entry.get("seller")
    clean = {k: v for k, v in entry.items() if k not in ("est_ok",)}
    return {**clean, "category": "igual", "source": market_match.SOURCE_MANUAL, "reason": _SAME_REASON,
            "differences": [], "confidence": None, "listings": 1 if price else 0,
            "min_cents": price, "median_cents": price, "prices_cents": [price] if price else [],
            "sellers": [str(seller)[:60]] if price and seller else []}


def drop_listing(snap: MarketPriceSnapshot, ml_id: str) -> dict[str, Any] | None:
    """"No es el mismo": la publicación pasa a la lista de DIFERENTES (nada se
    descarta; con "Es el mismo" se puede dar vuelta) y, si era IDÉNTICA, se
    recalculan mediana, mínimo, ganancia y color real con las que quedan. Devuelve
    la publicación tal como estaba, o None si no estaba. No guarda: lo hace el
    llamador.

    Los precios salen de `prices_cents` de cada publicación; en filas viejas (sin
    esa lista) se aproximan con el mínimo real y la mediana."""
    priced, unpriced, similar, other = _lists(snap)
    removed = next((m for m in (*priced, *unpriced, *similar, *other) if m.get("ml_id") == ml_id), None)
    if removed is None:
        return None
    was_igual = any(m.get("ml_id") == ml_id for m in (*priced, *unpriced))
    priced = [m for m in priced if m.get("ml_id") != ml_id]
    unpriced = [m for m in unpriced if m.get("ml_id") != ml_id]
    similar = [m for m in similar if m.get("ml_id") != ml_id]
    other = [_as_manual_other(removed)] + [m for m in other if m.get("ml_id") != ml_id]
    _rebuild(snap, priced, unpriced, similar, other, real_changed=was_igual,
             reason=f"se descartó «{(removed.get('title') or ml_id)[:60]}» (No es el mismo)")
    return removed


def promote_listing(snap: MarketPriceSnapshot, ml_id: str) -> dict[str, Any] | None:
    """"Es el mismo": una publicación SIMILAR o DIFERENTE pasa a IDÉNTICA y se
    recalculan mediana, mínimo, ganancia y color real contando su precio. Devuelve
    la publicación tal como estaba, o None si no estaba entre los similares y los
    diferentes. No guarda: lo hace el llamador. La próxima corrida la respeta
    (market_match_feedback, label 1)."""
    priced, unpriced, similar, other = _lists(snap)
    removed = next((m for m in (*similar, *other) if m.get("ml_id") == ml_id), None)
    if removed is None:
        return None
    similar = [m for m in similar if m.get("ml_id") != ml_id]
    other = [m for m in other if m.get("ml_id") != ml_id]
    promoted = _as_manual_igual(removed)
    if promoted["prices_cents"]:
        priced = [*priced, promoted]
    else:
        unpriced = [*unpriced, promoted]
    _rebuild(snap, priced, unpriced, similar, other, real_changed=bool(promoted["prices_cents"]))
    return removed


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
        "web": {
            "status": run.web_status, "searches": run.web_searches, "bytes": run.web_bytes,
            "blocked": run.web_blocked, "n_ok": run.n_web_ok,
            "bytes_per_search": round(run.web_bytes / run.web_searches) if run.web_searches else None,
        },
        "n_con_similares": run.n_con_similares,
        # Color ESTIMADO por similares (aparte del real) y productos donde ML solo
        # devolvió publicaciones diferentes.
        "estimated": {"verde": run.n_est_verde, "amarillo": run.n_est_amarillo, "rojo": run.n_est_rojo},
        "solo_diferentes": run.n_solo_diferentes,
        # Por fuente (ML y cada tienda): productos con idéntico / similar / solo diferentes / nada.
        "sources": store_match.run_sources(run),
    }


def _clean_price(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def _sanitized_listings(raw: str | None) -> list[dict[str, Any]]:
    """El JSON de publicaciones del snapshot, listo para servir. Se sanea al
    servir (no solo al guardar): cubre filas viejas y nada que no tenga la forma
    esperada llega a un href ni a un <img>."""
    try:
        data = json.loads(raw) if raw else []
    except ValueError:
        return []
    out: list[dict[str, Any]] = []
    for m in data if isinstance(data, list) else []:
        if not isinstance(m, dict):
            continue
        diffs = [d for d in (m.get("differences") or []) if d in market_judge.DIFFERENCES
                 or d in (market_specs.DIFF_QUANTITY, market_specs.DIFF_CAPACITY,
                          market_specs.DIFF_SIZE, market_specs.DIFF_WEIGHT)]
        entry = {
            **{k: v for k, v in m.items() if k not in ("prices_cents", "sellers", "est_ok")},
            "permalink": market_ml.safe_permalink(m.get("permalink")),
            "image_url": market_ml.safe_image_url(m.get("image_url")),
            "differences": diffs,
        }
        if "price_cents" in entry:
            entry["price_cents"] = _clean_price(entry["price_cents"])
        # ¿Este similar cuenta para el color estimado? (la UI lo aclara en cada card)
        entry["in_estimate"] = bool(m.get("est_ok")) and m.get("category") == "similar"
        out.append(entry)
    return out


def derived_state(snap: MarketPriceSnapshot) -> str | None:
    """`match_state` de la fila, o el que le corresponde a una fila anterior a ese
    campo (solo se sabía si había IGUAL o no)."""
    if snap.match_state:
        return snap.match_state
    if snap.ml_status == OK:
        return STATE_IGUAL
    if snap.ml_status == NO_DATA:
        if snap.similar_count:
            return STATE_SIMILAR
        return STATE_DIFFERENT if snap.other_count else STATE_NONE
    return None


def snapshot_to_dict(snap: MarketPriceSnapshot) -> dict[str, Any]:
    matched = _sanitized_listings(snap.matched_listings)
    similar = _sanitized_listings(snap.similar_listings)
    other = _sanitized_listings(snap.other_listings)
    unpriced = _sanitized_listings(snap.unpriced_listings)
    try:
        our_specs = json.loads(snap.our_specs) if snap.our_specs else None
    except ValueError:
        our_specs = None
    return {
        "id": snap.id,
        "run_id": snap.run_id,
        "product": {
            "id": snap.product_id, "name": snap.product_name, "code": snap.product_code,
            "image_url": snap.product_image_url, "slug": snap.product_slug,
            "enabled": snap.product_enabled is not False,
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
        "unpriced_listings": unpriced,
        "similar_count": snap.similar_count or 0,
        "similar_listings": similar,
        "other_count": snap.other_count or 0,
        "other_listings": other,
        "match_state": derived_state(snap),
        # Color ESTIMADO por la mediana de los similares (solo si no hay idéntico):
        # no es el color real y no entra a ningún contador de color.
        "estimated_color": snap.estimated_color,
        "estimated_margin_pct": snap.estimated_margin_pct,
        "estimated_median_cents": snap.estimated_median_cents,
        "estimated_listing_count": snap.estimated_listing_count or 0,
        "estimated_from": snap.estimated_from,
        "match_origin": snap.match_origin,
        "web_state": snap.web_state,
        "web_searches": snap.web_searches or 0,
        "web_bytes": snap.web_bytes or 0,
        "our_specs": our_specs if isinstance(our_specs, dict) else None,
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
        # De qué precios sale el color: ml | ml+tiendas | tiendas.
        "price_basis": snap.price_basis or "ml",
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
        "include_disabled": bool(int(runtime.get("pm_include_disabled") or 0)),
        "web": {**market_ml_web.web_budget_status(), "off_reason": market_ml_web.disabled_reason()},
        "stores": store_catalog.index_status(),
        "stores_affect_color": store_match.affect_color_enabled(),
    }
