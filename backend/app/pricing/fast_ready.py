"""Vía rápida: un producto recién subido queda «listo para publicar» en minutos, no al día siguiente.

Hasta ahora lo que se sube apagado a Vendure recién se medía en la corrida de la noche. Con `fast_ready_enabled = 1`
(default 0: apagado), cuando Vendure avisa que hay un producto nuevo (`POST /api/vendure/events`), Hugo mide ESE producto en
el momento y, si pasa TODO esto, lo deja en la bandeja como «listo para publicar» para que una persona lo publique con un
click:

    1. no cae en la lista de lo que no vendemos (app/prohibidos.py), y la lista completa se pudo cargar;
    2. no es duplicado de otro producto del catálogo, contando los INACTIVOS (dedup/orchestrator.py);
    3. da VERDE FIRME contra Mercado Libre con las mismas reglas del portón (`price_gate.classify`: idénticos confirmados
       por foto y nombre, 3 publicaciones, 2 vendedores, piso de precio, ajustes aprendidos);
    4. está aprobado (o sin estado) en el portal de proveedores;
    5. sus medidas (producto, caja y caja master) fueron revisadas y cierran (pricing/measures_check.py). Acá el chequeo
       es obligatorio aunque `pm_gate_measures_check` esté en 0: «listo» quiere decir «medidas aprobadas».

**No activa nada.** No escribe en Vendure: deja una ficha `por_activar` con la marca `porton:listo_para_publicar`. La
publica una persona con «Activar» (que pasa por las guardas de siempre) o, si el modo automático está prendido
(`pm_mode = 2`), el portón de la noche como cualquier otro verde firme. Lo que no llega a «listo» no abre ficha: queda
anotado en `fast_ready` con el motivo y lo toma la corrida de la noche, como hoy.

Cómo mide (un producto a la vez, nunca con la corrida de la noche en curso):

  * espera ~45 s desde el último aviso del producto (el que sube manda varios avisos seguidos y Vendure termina de calcular
    el precio después de guardar);
  * solo productos APAGADOS, nunca medidos, sin ficha viva y que vienen de una subida (link de proveedor de 1688 o unidos a
    lo que mandó el radar);
  * la API de Mercado Libre primero (gratis); si no da un idéntico con precio y hay cupo propio
    (`fast_ready_apify_daily_max`), UNA búsqueda web con el actor de Apify de la noche (~USD 0,02), que queda guardada como
    cualquier otra (la noche no la vuelve a pagar);
  * el juez por IA no se usa: un idéntico confirmado solo por el juez no es «firme», así que no cambiaría el resultado;
  * el snapshot se guarda en la ÚLTIMA corrida terminada del semáforo (así el producto aparece hoy en el semáforo, el
    recálculo y la bandeja lo tratan como a cualquier otro, y la noche siguiente lo vuelve a medir).

Topes: `fast_ready_daily_max` productos por día (hora de Argentina) y `fast_ready_apify_daily_max` búsquedas pagas. Cada
intento queda en la tabla `fast_ready` con sus tiempos: `GET /api/price-monitor/fast-ready` dice cuántos llegaron a «listo»
y en cuánto (el objetivo es menos de 5 minutos desde el aviso de la subida). El gasto de Apify va a `apify_spend` (etiqueta
`fast_ready`).

Un producto a medio subir (sin precio, sin foto) o que no se pudo medir en ese momento (el índice de fotos todavía no está
listo, no queda cupo de Mercado Libre) no se guarda: queda `pendiente` y un aviso posterior lo reintenta, hasta 3 veces.
Lo que ya se midió o ya está en la bandeja ni se anota: casi todos los avisos de Vendure son de productos de siempre.

La cola vive en memoria: si el proceso se reinicia, lo que estaba ahí lo toma la corrida de la noche.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import statistics
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import motor_eventos, runtime
from app.clock import utcnow
from app.config import get_settings
from app.db.models import AuditLog, FastReady, LuisIntake, MarketPriceSnapshot, PriceMonitorRun
from app.db.session import engine
from app.pricing import daily_budget, price_monitor
from app.pricing import price_review as pr
from app.pricing.safe_error import error_code

log = logging.getLogger(__name__)

COUNTER_KEY = "fast_ready"                 # productos medidos por día (ART)
APIFY_COUNTER_KEY = "fast_ready_apify"     # búsquedas pagas por día (UTC, como las demás de Apify)
SPEND_LABEL = "fast_ready"
RULE = "listo_para_publicar"
READY_FLAG = f"porton:{RULE}"
READ_AFTER = timedelta(seconds=45)
MAX_QUEUED = 50
MAX_ATTEMPTS = 3
TARGET_SECONDS = 300.0                     # el objetivo: de la subida a «listo» en menos de 5 minutos
# Tope de todo el intento de un producto (medir + duplicados + medidas): pasado esto se corta y lo toma la noche.
DEADLINE_S = 270.0

PENDIENTE, LISTO, NO_LISTO, OMITIDO, ERROR = "pendiente", "listo", "no_listo", "omitido", "error"
FINAL = (LISTO, NO_LISTO, OMITIDO)
READY_TEXT = ("Listo para publicar: verde firme contra Mercado Libre, medidas revisadas, sin duplicado y fuera de la lista "
              "de lo que no vendemos")

_due: dict[str, datetime] = {}
_first_seen: dict[str, datetime] = {}
_task: asyncio.Task | None = None
_lock = asyncio.Lock()


def enabled() -> bool:
    """Prendida y con la bandeja fuera del modo sombra (con `pm_mode = 0` nadie podría publicar desde la bandeja)."""
    return int(runtime.get("fast_ready_enabled") or 0) == 1 and int(runtime.get("pm_mode") or 0) >= 1


# ─── Avisos de Vendure ─────────────────────────────────────────────


def _is_leader() -> bool:
    from app.scheduler import jobs  # lazy: jobs arrastra todo el scheduler

    return bool(jobs.scheduler.running)


def notify(product_ids: list[str], now: datetime | None = None) -> int:
    """Desde `POST /api/vendure/events`: anota los productos para mirarlos a los ~45 s del último aviso. Devuelve cuántos
    quedaron anotados. Apagada (el default) no hace nada."""
    if not product_ids or not enabled():
        return 0
    if not _is_leader():
        return 0
    now = now or utcnow()
    taken = 0
    for pid in dict.fromkeys(product_ids):
        if pid not in _due and len(_due) >= MAX_QUEUED:
            continue
        _first_seen.setdefault(pid, now)
        _due[pid] = now + READ_AFTER
        taken += 1
    if _due:
        _start_worker()
    return taken


def _start_worker() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_worker())


async def _worker() -> None:
    """Procesa los avisos a medida que vencen. Uno solo por proceso; nunca levanta."""
    while _due:
        wait = (min(_due.values()) - utcnow()).total_seconds()
        await asyncio.sleep(min(30.0, max(1.0, wait)))
        try:
            await process_due()
        except Exception as exc:  # noqa: BLE001
            log.warning("vía rápida: la pasada falló (%s)", error_code(exc))
            await asyncio.sleep(30.0)


async def process_due(client: Any = None, now: datetime | None = None) -> dict[str, int]:
    """Los avisos vencidos, de a uno. Con la corrida de la noche en curso no mide nada: se descartan (la noche los mide)."""
    now = now or utcnow()
    due = [pid for pid, at in _due.items() if at <= now]
    counts: dict[str, int] = {}
    for pid in due:
        _due.pop(pid, None)
        seen = _first_seen.pop(pid, now)
        try:
            outcome = await process_one(pid, client=client, first_seen=seen)
        except Exception as exc:  # noqa: BLE001  (un producto no frena a los demás)
            log.warning("vía rápida: el producto %s falló (%s)", pid, error_code(exc))
            outcome = "error"
        counts[outcome] = counts.get(outcome, 0) + 1
    if counts:
        log.info("vía rápida: %s", counts)
    return counts


# ─── Base ──────────────────────────────────────────────────────────


def _row(product_id: str, first_seen: datetime) -> FastReady | None:
    """La fila del producto (se crea si no está). None si ya terminó o se quedó sin intentos."""
    with Session(engine, expire_on_commit=False) as s:
        row = s.exec(select(FastReady).where(FastReady.product_id == product_id)).first()
        if row is None:
            row = FastReady(product_id=product_id, first_seen_at=first_seen)
            s.add(row)
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
                return None
        if row.status in FINAL or row.attempts >= MAX_ATTEMPTS:
            return None
        row.attempts += 1
        s.add(row)
        s.commit()
        return row


def _finish(row_id: int, status: str, reason: str, text: str = "", **fields: Any) -> None:
    now = utcnow()
    with Session(engine) as s:
        row = s.get(FastReady, row_id)
        if row is None:
            return
        row.status, row.reason, row.reason_text = status, reason[:40], (text or None) and text[:300]
        row.finished_at = now
        row.seconds_to_finish = round(max(0.0, (now - row.first_seen_at).total_seconds()), 1)
        for key, value in fields.items():
            setattr(row, key, value)
        s.add(row)
        s.commit()


def _local_state(product_id: str) -> tuple[bool, bool, int | None, LuisIntake | None, bool]:
    """Sin red: ¿ya tiene alguna medición?, ¿tiene una ficha viva?, la última corrida terminada, la fila del radar (si
    llegó por ahí) y si otro control ya lo apagó o lo marcó (duplicado, BX sin foto)."""
    with Session(engine, expire_on_commit=False) as s:
        measured = s.exec(select(MarketPriceSnapshot.id).where(MarketPriceSnapshot.product_id == product_id)
                          .limit(1)).first() is not None
        has_item = pr.open_item_for(s, product_id) is not None
        run_id = s.exec(select(PriceMonitorRun.id)
                        .where(PriceMonitorRun.status.in_((price_monitor.RUN_OK, price_monitor.RUN_DEGRADED)))  # type: ignore[attr-defined]
                        .order_by(PriceMonitorRun.id.desc()).limit(1)).first()  # type: ignore[union-attr]
        intake = s.exec(select(LuisIntake).where(LuisIntake.vendure_product_id == product_id)
                        .order_by(LuisIntake.id.desc()).limit(1)).first()  # type: ignore[union-attr]
        flagged = s.exec(select(AuditLog.id).where(or_(
            AuditLog.product_id == product_id, AuditLog.disable_target_id == product_id),
            AuditLog.action.in_(("duplicate_disabled", "duplicate_confirmed", "duplicate_flagged",  # type: ignore[attr-defined]
                                 "bx_no_image_disabled", "bx_no_image_flagged")),
            AuditLog.dismissed.is_not(True)).limit(1)).first() is not None  # type: ignore[union-attr]
    return measured, has_item, run_id, intake, flagged


def _attach(snap: MarketPriceSnapshot, run_id: int) -> bool:
    """Guarda el snapshot en la corrida `run_id` y rehace sus contadores. False si el producto ya tenía uno ahí."""
    with Session(engine, expire_on_commit=False) as s:
        s.add(snap)
        try:
            s.flush()
        except IntegrityError:
            s.rollback()
            return False
        run = s.get(PriceMonitorRun, run_id)
        if run is not None:
            run.total_products = (run.total_products or 0) + 1
            run.processed = (run.processed or 0) + 1
            s.add(run)
            s.flush()
            price_monitor.recount_run(s, run_id)
        s.commit()
    return True


# ─── Un producto ───────────────────────────────────────────────────


async def process_one(product_id: str, *, client: Any = None, first_seen: datetime | None = None) -> str:
    """Todo el camino de UN producto. Devuelve el resultado en una palabra. No escribe en Vendure."""
    if not enabled():
        return "apagada"
    if price_monitor.price_monitor_lock.locked() or price_monitor.has_unfinished_run():
        return "corrida_en_curso"                    # lo mide la noche: no se compite por el cupo ni por la CPU
    async with _lock:
        # Lo que ya se midió o ya está en la bandeja no es una subida nueva: ni se anota (casi todos los avisos de Vendure
        # son de productos de siempre que alguien editó).
        state = await asyncio.to_thread(_local_state, product_id)
        if state[0]:
            return "ya_medido"
        if state[1]:
            return "ya_en_bandeja"
        row = await asyncio.to_thread(_row, product_id, first_seen or utcnow())
        if row is None:
            return "ya_visto"
        started = time.monotonic()
        try:
            async with asyncio.timeout(DEADLINE_S):
                return await _process(row, product_id, client, started, state)
        except TimeoutError:
            await asyncio.to_thread(_finish, int(row.id), ERROR, "sin_tiempo",  # type: ignore[arg-type]
                                    f"No terminó en {int(DEADLINE_S)} s: lo toma la corrida de la noche")
            return "sin_tiempo"
        except Exception as exc:
            await asyncio.to_thread(_finish, int(row.id), ERROR, "error", error_code(exc))  # type: ignore[arg-type]
            raise


async def _process(row: FastReady, pid: str, client: Any, started: float,
                   state: tuple[bool, bool, int | None, LuisIntake | None, bool]) -> str:
    from app.estimate.otapi_item import offer_id_from_url
    from app.pricing import price_gate
    from app.pricing import price_review_actions as actions
    from app.pricing import semaforo_aprende as learn
    from app.pricing import semaforo_correcciones as corr

    rid = int(row.id)  # type: ignore[arg-type]

    def done(status: str, reason: str, text: str = "", **fields: Any):
        return asyncio.to_thread(_finish, rid, status, reason, text, **fields)

    _measured, _has_item, run_id, intake, flagged = state
    hilo = motor_eventos.limpiar_hilo(intake.hilo) if intake is not None else None
    if run_id is None:
        await done(OMITIDO, "sin_corrida", "Todavía no hay una corrida del semáforo terminada")
        return "sin_corrida"
    if client is None:
        from app.vendure.client import VendureClient
        client = VendureClient()
    product = await client.get_product_priced(pid)
    if product is None:
        await done(OMITIDO, "no_existe", "El producto no existe en Vendure")
        return "no_existe"
    names = {"product_name": (product.name or "")[:200] or None, "product_code": product.product_code}
    if product.enabled:
        await done(OMITIDO, "ya_activo", "Ya está publicado", **names)
        return "ya_activo"
    if intake is None and not offer_id_from_url(product.source_url or ""):
        await done(OMITIDO, "no_es_subida", "No viene de una subida con link de proveedor", **names)
        return "no_es_subida"

    cap = int(runtime.get("fast_ready_daily_max") or 0)
    if await asyncio.to_thread(daily_budget.reserve, COUNTER_KEY, cap, None, None, daily_budget.today_art()) is None:
        await done(OMITIDO, "tope_del_dia", "Se llegó al tope de mediciones rápidas del día: lo mide la noche", **names)
        return "tope_del_dia"

    # 1. Lo que no vendemos (gratis): antes de gastar nada.
    banned = await actions.prohibido_check(pid, product.name, intake.name if intake is not None else None, auto=True)
    if banned is not None:
        code, why = banned
        await done(NO_LISTO, code, why, hilo=hilo, **names)
        motor_eventos.avisar(motor_eventos.evento(hilo, "prohibido", "frenado", ref=pid, motivo=code))
        return code

    # 2. Duplicados (contando inactivos) y medición contra Mercado Libre, a la vez.
    dup_task = asyncio.create_task(_duplicate_of(product))
    try:
        snap, apify_usd = await _measure(product, int(run_id))
    except BaseException:
        dup_task.cancel()
        raise
    measured_at = utcnow()
    dup_id = await dup_task
    numbers = {
        **names, "measured_at": measured_at, "hilo": hilo, "apify_usd": apify_usd, "run_id": int(run_id),
        "color": snap.color, "margin_pct": snap.est_margin_pct, "our_price_cents": snap.our_price_cents,
        "ml_median_cents": snap.ml_median_cents, "listing_count": snap.ml_listing_count,
        "seller_count": snap.ml_seller_count, "match_origin": snap.match_origin,
    }
    if snap.unmeasurable:
        # Todavía no terminó de subirse (sin precio, sin foto): no se guarda nada y un aviso posterior lo reintenta.
        await asyncio.to_thread(_finish, rid, PENDIENTE, f"incompleto:{snap.unmeasurable}"[:40],
                                snap.ml_error or "El producto todavía no está completo en Vendure", **names)
        return "incompleto"
    if snap.ml_status == price_monitor.SKIPPED:
        # No se pudo medir AHORA (el índice de fotos todavía no está listo después de un reinicio, o se acabó el cupo de
        # Mercado Libre del día): tampoco se guarda nada. Un aviso posterior lo reintenta; si no, lo mide la noche.
        await asyncio.to_thread(_finish, rid, PENDIENTE, "no_medible_ahora",
                                snap.ml_error or "No se pudo medir en este momento", **names)
        return "no_medible_ahora"
    if not await asyncio.to_thread(_attach, snap, int(run_id)):
        await done(OMITIDO, "ya_medido", "Otra medición llegó antes", **names)
        return "ya_medido"
    numbers["snapshot_id"] = snap.id
    ms = int((time.monotonic() - started) * 1000)
    measured_event = motor_eventos.evento(hilo, "medido", "ok", ref=pid, ms=ms, costo_usd=apify_usd,
                                          motivo=f"{snap.color or 'sin_dato'} ({snap.ml_status})")

    if dup_id is not None or flagged:
        text = (f"Parece duplicado del producto #{dup_id} del catálogo" if dup_id is not None
                else "El control de duplicados o de fotos ya lo marcó")
        await done(NO_LISTO, "duplicado", text, **numbers)
        motor_eventos.avisar(measured_event, motor_eventos.evento(hilo, "duplicado", "frenado", ref=pid,
                                                                  motivo="parece duplicado de otro producto del catálogo"))
        return "duplicado"

    now = utcnow()
    params = pr.ReviewParams.from_runtime()
    rules = await asyncio.to_thread(learn.active_rules)
    with Session(engine) as s:
        category = corr.category_of(s, pid)
    decision = price_gate.classify(snap, params, now, rules, category)
    if decision.action != price_gate.ACTIVAR:
        await done(NO_LISTO, decision.reason, decision.text, **numbers)
        motor_eventos.avisar(measured_event, motor_eventos.evento(hilo, "listo", "frenado", ref=pid,
                                                                  motivo=decision.reason))
        return "no_verde_firme"

    guard = await client.get_review_guard(pid)
    if guard is None or guard.enabled or guard.approval_status not in actions.APPROVAL_OK:
        why = ("ya_activo" if guard is not None and guard.enabled else "aprobacion_pendiente")
        await done(NO_LISTO if why != "ya_activo" else OMITIDO, why,
                   "Lo publicaron mientras se medía" if why == "ya_activo"
                   else "No está aprobado en el portal de proveedores", **numbers)
        motor_eventos.avisar(measured_event, motor_eventos.evento(hilo, "listo", "frenado", ref=pid, motivo=why))
        return why

    ok, code, text, measures_usd = await _measures_ok(snap)
    numbers["measures_usd"] = measures_usd
    if not ok:
        await done(NO_LISTO, code, text, **numbers)
        motor_eventos.avisar(measured_event, motor_eventos.evento(hilo, "listo", "frenado", ref=pid, motivo=code,
                                                                  costo_usd=measures_usd))
        return "medidas"

    ready = price_gate.GateDecision(price_gate.BANDEJA, RULE, decision.verdict, note=READY_TEXT)
    item = await asyncio.to_thread(price_gate._ensure_item, snap, pr.POR_ACTIVAR, ready, now, int(run_id))
    if item is None or item.state != pr.POR_ACTIVAR:
        await done(ERROR, "sin_ficha", "No se pudo abrir la ficha en la bandeja", **numbers)
        return "sin_ficha"
    await done(LISTO, RULE, READY_TEXT, item_id=int(item.id), **numbers)  # type: ignore[arg-type]
    total_ms = int((time.monotonic() - started) * 1000)
    motor_eventos.avisar(measured_event, motor_eventos.evento(hilo, "listo", "ok", ref=pid, ms=total_ms,
                                                              costo_usd=measures_usd))
    log.info("vía rápida: %s quedó listo para publicar (ficha #%s)", pid, item.id)
    return "listo"


async def _duplicate_of(product: Any) -> str | None:
    """El id del producto del catálogo (activo o inactivo) del que este parece duplicado, o None. Si no se puede
    comparar, levanta: sin chequeo de duplicados no hay «listo»."""
    from app.dedup.orchestrator import CandidateInput, find_duplicate_in
    from app.vendure import catalog as vendure_catalog

    catalog = [p for p in await vendure_catalog.get_catalog() if p.id != product.id]
    catalog.sort(key=lambda p: not p.enabled)
    verdict = await find_duplicate_in(
        CandidateInput(name=product.name, description=product.description, source_url=product.source_url,
                       image_urls=list(product.image_urls or [])[:5]), catalog)
    return verdict.candidate_id if verdict.is_duplicate else None


async def _measures_ok(snap: MarketPriceSnapshot) -> tuple[bool, str, str, float]:
    """(¿cierran?, código, texto, costo). Siempre se chequea, esté o no la guarda del portón prendida."""
    from app.pricing import measures_check

    try:
        row = await measures_check.check_snapshot(snap)
    except Exception as exc:  # noqa: BLE001
        log.warning("vía rápida: no se pudieron chequear las medidas de %s (%s)", snap.product_id, error_code(exc))
        return False, f"medidas_{measures_check.ERROR}", measures_check.GUARD_TEXT[measures_check.ERROR], 0.0
    cost = float(getattr(row, "cost_usd", 0.0) or 0.0)
    if row.verdict == measures_check.OK:
        return True, "", "", cost
    text = measures_check.GUARD_TEXT.get(row.verdict, measures_check.GUARD_TEXT[measures_check.ERROR])
    if row.motive and row.verdict in (measures_check.MAL, measures_check.DUDOSO):
        text = f"{text}: {row.motive}"
    return False, f"medidas_{row.verdict}", text[:300], cost


# ─── Medir contra Mercado Libre ────────────────────────────────────


async def _measure(product: Any, run_id: int) -> tuple[MarketPriceSnapshot, float]:
    """El snapshot de ESTE producto (sin guardar) y lo gastado en Apify. La API primero; si no dio un idéntico con
    precio, una búsqueda web paga (si hay cupo propio)."""
    from app.dedup import catalog_index
    from app.pricing.market_ml import MlMarket, ml_budget_status

    budget = await asyncio.to_thread(ml_budget_status)
    await catalog_index.ensure_index()               # no bloquea: si el índice no está listo, sale «skipped»
    usd = 0.0
    async with MlMarket(budget=int(budget["budget"])) as ml:
        ctx = price_monitor._context(run_id, ml, None, {}, {})
        ctx.judge_max_calls = 0                      # sin juez: un idéntico solo del juez no es firme
        try:
            snap = await price_monitor._evaluate_ml(ctx, product)
            if snap.ml_status in (price_monitor.NO_DATA, price_monitor.FAILED) and not snap.unmeasurable:
                fresh, usd = await _web_search(product)
                if fresh is not None:
                    ctx.oficina = {product.id: fresh}
                    ctx.ml_api = False
                    snap = await price_monitor._evaluate_ml(ctx, product)
        finally:
            await price_monitor._close_judge_client(ctx)
    return snap, usd


def apify_reason() -> str | None:
    """Por qué la vía rápida no puede buscar con Apify, o None. Usa el token, el actor y el build de la búsqueda de la
    noche, pero NO su interruptor ni su cupo: tiene los suyos."""
    from app.pricing import apify_ml_search as ams

    if int(runtime.get("fast_ready_apify_daily_max") or 0) <= 0:
        return "sin cupo propio (fast_ready_apify_daily_max = 0)"
    if not get_settings().apify_token.strip():
        return "falta APIFY_TOKEN"
    if not ams._actor():
        return "el actor configurado no está en la lista blanca"
    if ams._build() is None:
        return "el build configurado no es válido"
    return None


async def _web_search(product: Any, http: Any = None) -> tuple[Any | None, float]:
    """UNA búsqueda web de Mercado Libre para este producto con el actor de Apify. Devuelve (resultado fresco o None,
    USD imputados). Nunca lanza."""
    from app.pricing import apify_ml_search as ams
    from app.pricing import market_match, oficina_ml

    if apify_reason() is not None:
        return None, 0.0
    query = oficina_ml.clean_query(market_match.search_query(product.name or "")) or market_match.search_query(
        product.name or "")
    if not query:
        return None, 0.0
    s = get_settings()
    claimer = f"rapida-{secrets.token_hex(3)}"
    headers = {"Authorization": f"Bearer {s.apify_token.strip()}", "Accept": "application/json"}
    own = http is None
    http = http or ams._new_client(ams._POLL_WAIT_S + 15.0)
    usd = 0.0
    try:
        if await ams._actor_permission(http, ams._actor(), headers) != ams._REQUIRED_PERMISSION:
            log.warning("vía rápida: el actor de Apify no corre con permisos limitados: no se busca")
            return None, 0.0
        if not await asyncio.to_thread(oficina_ml.claim, [product.id], claimer, ams._CLAIM_LEASE):
            return None, 0.0                          # otro lo está buscando
        cap = int(runtime.get("fast_ready_apify_daily_max") or 0)
        if await asyncio.to_thread(daily_budget.reserve_up_to, APIFY_COUNTER_KEY, cap, 1) < 1:
            return None, 0.0
        out = await ams.search_one(http, product.id, query, actor=ams._actor(), build=ams._build() or "",
                                   headers=headers, timeout_s=int(s.apify_meli_search_timeout_s),
                                   limit=oficina_ml._max_candidates())
        usd = float(out.usage_usd or out.usd_imputado or 0.0)
        await asyncio.to_thread(_record_spend, out)
        await asyncio.to_thread(oficina_ml.ingest, [out.as_result()], origin=ams.ORIGIN_APIFY)
        fresh = (await asyncio.to_thread(oficina_ml.load_fresh)).get(product.id)
        return fresh, usd
    except Exception as exc:  # noqa: BLE001
        log.warning("vía rápida: la búsqueda con Apify de %s se cortó (%s)", product.id, type(exc).__name__)
        return None, usd
    finally:
        await asyncio.to_thread(oficina_ml.release, claimer)
        if own:
            await http.aclose()


def _record_spend(outcome: Any) -> None:
    from app.db.models import ApifySpend

    if not outcome.run_id and not outcome.usd_imputado:
        return
    try:
        usd = outcome.usage_usd if outcome.usage_usd else outcome.usd_imputado
        with Session(engine) as s:
            s.add(ApifySpend(label=SPEND_LABEL, run_id=(outcome.run_id or "")[:40], terms=1, usd=round(float(usd), 6),
                             estimated=not outcome.usage_usd))
            s.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("vía rápida: no se pudo registrar el gasto de Apify (%s)", type(exc).__name__)


# ─── Lo que se ve ──────────────────────────────────────────────────


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() + "Z" if moment is not None else None


def as_dict(r: FastReady) -> dict[str, Any]:
    return {
        "product_id": r.product_id, "product_name": r.product_name, "product_code": r.product_code,
        "status": r.status, "reason": r.reason, "reason_text": r.reason_text, "attempts": r.attempts,
        "first_seen_at": _iso(r.first_seen_at), "measured_at": _iso(r.measured_at), "finished_at": _iso(r.finished_at),
        "seconds_to_finish": r.seconds_to_finish, "color": r.color, "margin_pct": r.margin_pct,
        "our_price_cents": r.our_price_cents, "ml_median_cents": r.ml_median_cents, "listing_count": r.listing_count,
        "seller_count": r.seller_count, "match_origin": r.match_origin, "item_id": r.item_id, "run_id": r.run_id,
        "apify_usd": r.apify_usd, "measures_usd": r.measures_usd, "hilo": r.hilo,
    }


def report(days: int = 7, *, limit: int = 200, now: datetime | None = None) -> dict[str, Any]:
    """Los intentos de los últimos `days` días, el más nuevo primero, y cuántos llegaron a «listo» y en cuánto."""
    now = now or utcnow()
    days = max(1, min(int(days), 90))
    since = now - timedelta(days=days)
    with Session(engine) as s:
        rows = s.exec(select(FastReady).where(FastReady.first_seen_at >= since)
                      .order_by(FastReady.first_seen_at.desc(), FastReady.id.desc()).limit(2000)).all()  # type: ignore[union-attr]
    by_status: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        if r.status != LISTO and r.reason:
            by_reason[r.reason] = by_reason.get(r.reason, 0) + 1
    ready = sorted(r.seconds_to_finish for r in rows if r.status == LISTO and r.seconds_to_finish is not None)
    return {
        "enabled": enabled(),
        "days": days,
        "daily_max": int(runtime.get("fast_ready_daily_max") or 0),
        "used_today": daily_budget.used_today(COUNTER_KEY, day=daily_budget.today_art(now)),
        "apify_daily_max": int(runtime.get("fast_ready_apify_daily_max") or 0),
        "apify_reason": apify_reason(),
        "total": len(rows),
        "by_status": by_status,
        "not_ready_reasons": dict(sorted(by_reason.items(), key=lambda kv: -kv[1])),
        "ready": {
            "count": len(ready),
            "target_seconds": TARGET_SECONDS,
            "under_target": sum(1 for x in ready if x <= TARGET_SECONDS),
            "median_seconds": round(statistics.median(ready), 1) if ready else None,
            "max_seconds": ready[-1] if ready else None,
        },
        "apify_usd": round(sum(float(r.apify_usd or 0.0) for r in rows), 6),
        "measures_usd": round(sum(float(r.measures_usd or 0.0) for r in rows), 6),
        "items": [as_dict(r) for r in rows[:max(1, min(int(limit), 2000))]],
    }


def reset_for_tests() -> None:
    _due.clear()
    _first_seen.clear()
