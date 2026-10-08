"""Endpoints del semáforo de precios (dashboard, sesión por cookie).

POST /api/price-monitor/run                    → dispara una corrida a mano
GET  /api/price-monitor/runs                   → últimas corridas
GET  /api/price-monitor/snapshots              → tabla paginada (run_id, color, q, enabled,
                                                  match, origin)
GET  /api/price-monitor/products/{id}/history  → tendencia de un producto
GET  /api/price-monitor/summary                → tarjeta de Salud
POST /api/price-monitor/snapshots/{id}/not-same → "No es el mismo": saca una publicación,
                                                  recalcula el snapshot y la excluye
POST /api/price-monitor/snapshots/{id}/same     → "Es el mismo": promueve un similar o diferente
                                                  a idéntico (recalcula el color real)
DELETE /api/price-monitor/products/{id}/not-same/{ml_id} → vuelve a considerarla (también
                                                  /products/{id}/feedback/{ml_id}: sirve para las dos marcas)

Viven bajo /api/ → el middleware de auth exige sesión del dashboard.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, not_, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from app import auth, runtime
from app.clock import utcnow
from app.db.models import MarketPriceSnapshot, PriceMonitorRun
from app.db.session import get_session
from app.pricing import match_feedback, price_monitor
from app.pricing.market_ml import ml_budget_status
from app.pricing.semaforo import COLORS

router = APIRouter(prefix="/api/price-monitor", tags=["price-monitor"])

PAGE_SIZE_DEFAULT = 25
PAGE_SIZE_MAX = 200
# Topes de los parámetros numéricos: un entero enorme no puede llegar a la DB
# (OFFSET / id fuera de rango → 500 en Postgres). 10.000 páginas de 200 cubren
# de sobra cualquier corrida.
PAGE_MAX = 10_000
DB_INT_MAX = 2**31 - 1
PRODUCT_ID_MAX_LEN = 64
STATUSES = ("ok", "no_data", "failed", "skipped")
# enabled=: qué productos de Vendure se ven. match=: qué hay de parecido en ML.
# origin=: de dónde salió el precio.
ENABLED_FILTERS = ("enabled", "disabled", "all")
# Qué devolvió ML: igual (tiene idéntico) | similar (tiene similares) | solo_similar |
# diferente (tiene diferentes) | solo_diferente | ninguno (sin resultados).
MATCH_FILTERS = ("igual", "similar", "solo_similar", "diferente", "solo_diferente", "ninguno")
ORIGINS = ("api", "web")
# Color estimado por similares: uno concreto o cualquiera.
ESTIMATED_FILTERS = ("verde", "amarillo", "rojo", "any")

# El event loop solo guarda referencias débiles a las tasks: sin esto, una
# corrida de horas disparada a mano podría ser recolectada a mitad de camino.
_background: set[asyncio.Task] = set()


def _manual_run_blocker(session: Session) -> tuple[int, str, int | None] | None:
    """Por qué no se puede disparar a mano ahora, o None si se puede.
    (status HTTP, mensaje, segundos para reintentar)."""
    if price_monitor.price_monitor_lock.locked():
        return 409, "Ya hay una corrida del semáforo en curso.", None
    budget = ml_budget_status()
    if budget["remaining"] <= 0:
        return 429, (f"Sin cupo de Mercado Libre hoy ({budget['used']} de {budget['budget']} "
                     "requests). Se renueva a las 00:00 UTC."), None
    cooldown_min = int(runtime.get("pm_manual_cooldown_min") or 0)
    last_start = session.exec(
        select(PriceMonitorRun.started_at)
        .order_by(PriceMonitorRun.started_at.desc())  # type: ignore[union-attr]
        .limit(1)
    ).first()
    if cooldown_min > 0 and last_start is not None:
        wait_s = int(cooldown_min * 60 - (utcnow() - last_start).total_seconds())
        if wait_s > 0:
            return 429, (f"La última corrida arrancó hace menos de {cooldown_min} min. "
                         f"Probá de nuevo en {max(1, round(wait_s / 60))} min."), wait_s
    return None


@router.post("/run", status_code=202)
async def run_now(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Dispara una corrida en background (modo sombra: no toca Vendure).
    409 si ya hay una en curso; 429 si no hay cupo de ML o si la última
    arrancó hace menos de `pm_manual_cooldown_min`."""
    from app.scheduler import jobs  # import tardío: jobs arrastra todo el scheduler

    blocker = _manual_run_blocker(session)
    if blocker is not None:
        status, detail, retry_after = blocker
        headers = {"Retry-After": str(retry_after)} if retry_after else None
        raise HTTPException(status, detail, headers=headers)
    task = asyncio.create_task(jobs.price_monitor(trigger="manual"))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"status": "scheduled", "mode": "sombra"}


@router.get("/summary")
async def summary() -> dict[str, Any]:
    return price_monitor.summary()


@router.get("/runs")
async def list_runs(
    limit: int = Query(20, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rows = session.exec(
        select(PriceMonitorRun)
        .order_by(PriceMonitorRun.started_at.desc())  # type: ignore[union-attr]
        .limit(limit)
    ).all()
    return {"items": [price_monitor.run_to_dict(r) for r in rows]}


def _latest_run_id(session: Session) -> int | None:
    """La última corrida con snapshots: terminada si la hay, si no la que está
    corriendo (para ver cómo va)."""
    finished = session.exec(
        select(PriceMonitorRun.id)
        .where(PriceMonitorRun.status != price_monitor.RUN_RUNNING)
        .order_by(PriceMonitorRun.started_at.desc())  # type: ignore[union-attr]
        .limit(1)
    ).first()
    if finished is not None:
        return int(finished)
    any_run = session.exec(
        select(PriceMonitorRun.id)
        .order_by(PriceMonitorRun.started_at.desc())  # type: ignore[union-attr]
        .limit(1)
    ).first()
    return int(any_run) if any_run is not None else None


def _has_igual() -> Any:
    """Tiene al menos una publicación idéntica (con precio o sin precio que cuente)."""
    # match_state puede ser NULL (filas viejas): sin el is_not, NOT(NULL) dejaría
    # esas filas fuera de "no tiene idéntico".
    return or_(MarketPriceSnapshot.ml_status == "ok",
               and_(MarketPriceSnapshot.match_state.is_not(None),  # type: ignore[union-attr]
                    MarketPriceSnapshot.match_state == "igual_sin_precio"))


def _match_condition(match: str) -> list[Any]:
    """Condiciones SQL de cada estado de `match`."""
    no_igual = not_(_has_igual())
    if match == "igual":
        return [_has_igual()]
    if match == "similar":
        return [MarketPriceSnapshot.similar_count > 0]
    if match == "solo_similar":
        return [no_igual, MarketPriceSnapshot.similar_count > 0]
    if match == "diferente":
        return [MarketPriceSnapshot.other_count > 0]
    if match == "solo_diferente":
        return [no_igual, MarketPriceSnapshot.similar_count == 0, MarketPriceSnapshot.other_count > 0]
    # ninguno: ML no devolvió nada (no es lo mismo que fallar: eso es failed/skipped)
    return [MarketPriceSnapshot.ml_status == "no_data", no_igual,
            MarketPriceSnapshot.similar_count == 0, MarketPriceSnapshot.other_count == 0]


def _scope_conditions(enabled: str, match: str | None, origin: str | None,
                      estimated: str | None = None) -> list[Any]:
    """Condiciones SQL de enabled / match / origin / estimated."""
    conds: list[Any] = []
    if enabled == "enabled":
        conds.append(MarketPriceSnapshot.product_enabled.is_(True))  # type: ignore[union-attr]
    elif enabled == "disabled":
        conds.append(MarketPriceSnapshot.product_enabled.is_(False))  # type: ignore[union-attr]
    if match:
        conds.extend(_match_condition(match))
    if origin:
        conds.append(MarketPriceSnapshot.match_origin == origin)
    if estimated:
        conds.append(MarketPriceSnapshot.ml_status != "ok")
        conds.append(MarketPriceSnapshot.estimated_color.is_not(None) if estimated == "any"  # type: ignore[union-attr]
                     else MarketPriceSnapshot.estimated_color == estimated)
    return conds


@router.get("/snapshots")
async def list_snapshots(
    run_id: int | None = Query(None, ge=1, le=DB_INT_MAX),
    color: str | None = Query(None, max_length=16),
    status: str | None = Query(None, max_length=16),
    q: str | None = Query(None, max_length=120),
    enabled: str = Query("all", max_length=16),
    match: str | None = Query(None, max_length=16),
    origin: str | None = Query(None, max_length=8),
    estimated: str | None = Query(None, max_length=8),
    page: int = Query(0, ge=0, le=PAGE_MAX),
    page_size: int = Query(PAGE_SIZE_DEFAULT, ge=1, le=PAGE_SIZE_MAX),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Tabla del semáforo. Sin `run_id` muestra la última corrida.

    `enabled`: enabled | disabled | all (productos de Vendure). `match`: igual
    (tiene idéntico) | similar (tiene similares) | solo_similar | diferente (tiene
    diferentes) | solo_diferente | ninguno (ML no devolvió nada). `origin`: api |
    web. `estimated`: verde | amarillo | rojo | any, el color ESTIMADO por similares
    (los productos sin idéntico; no es el color real)."""
    if color and color not in COLORS:
        raise HTTPException(400, f"color inválido: {color}")
    if status and status not in STATUSES:
        raise HTTPException(400, f"status inválido: {status}")
    if enabled not in ENABLED_FILTERS:
        raise HTTPException(400, f"enabled inválido: {enabled}")
    if match and match not in MATCH_FILTERS:
        raise HTTPException(400, f"match inválido: {match}")
    if origin and origin not in ORIGINS:
        raise HTTPException(400, f"origin inválido: {origin}")
    if estimated and estimated not in ESTIMATED_FILTERS:
        raise HTTPException(400, f"estimated inválido: {estimated}")
    if run_id is None:
        run_id = _latest_run_id(session)
    if run_id is None:
        return {"run_id": None, "items": [], "total": 0, "page": page, "page_size": page_size,
                "has_more": False, "colors": {}, "estimated_colors": {}, "states": {}}

    base = select(MarketPriceSnapshot).where(MarketPriceSnapshot.run_id == run_id)
    count_stmt = select(func.count(MarketPriceSnapshot.id)).where(  # type: ignore[arg-type]
        MarketPriceSnapshot.run_id == run_id
    )
    # Filtros que también acotan los contadores por color (los chips de arriba).
    scope = _scope_conditions(enabled, match, origin, estimated)
    for cond in scope:
        base = base.where(cond)
        count_stmt = count_stmt.where(cond)
    if color:
        base = base.where(MarketPriceSnapshot.color == color)
        count_stmt = count_stmt.where(MarketPriceSnapshot.color == color)
    if status:
        base = base.where(MarketPriceSnapshot.ml_status == status)
        count_stmt = count_stmt.where(MarketPriceSnapshot.ml_status == status)
    if q and q.strip():
        term = q.strip()
        like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        cond = (
            MarketPriceSnapshot.product_name.ilike(like, escape="\\")  # type: ignore[union-attr]
            | MarketPriceSnapshot.product_code.ilike(like, escape="\\")  # type: ignore[union-attr]
            | (MarketPriceSnapshot.product_id == term)
        )
        base = base.where(cond)
        count_stmt = count_stmt.where(cond)

    total = int(session.exec(count_stmt).one() or 0)
    # Los que tienen dato primero, peor margen arriba: es lo que hay que mirar.
    rows = session.exec(
        base.order_by(
            MarketPriceSnapshot.est_margin_pct.is_(None),  # type: ignore[union-attr]
            MarketPriceSnapshot.est_margin_pct.asc(),  # type: ignore[union-attr]
            # Después de los de color real, los de color estimado (peor margen primero).
            MarketPriceSnapshot.estimated_margin_pct.is_(None),  # type: ignore[union-attr]
            MarketPriceSnapshot.estimated_margin_pct.asc(),  # type: ignore[union-attr]
            MarketPriceSnapshot.product_name.asc(),  # type: ignore[union-attr]
        )
        .offset(page * page_size)
        .limit(page_size)
    ).all()
    colors = dict(session.exec(
        select(MarketPriceSnapshot.color, func.count(MarketPriceSnapshot.id))  # type: ignore[arg-type]
        .where(MarketPriceSnapshot.run_id == run_id, *scope)
        .group_by(MarketPriceSnapshot.color)
    ).all())
    # Chips aparte del color real: el estimado (solo productos sin idéntico) y
    # cuántos productos hay en cada estado, con los filtros de producto y origen.
    base_scope = _scope_conditions(enabled, None, origin)
    estimated_counts = dict(session.exec(
        select(MarketPriceSnapshot.estimated_color, func.count(MarketPriceSnapshot.id))  # type: ignore[arg-type]
        .where(MarketPriceSnapshot.run_id == run_id, MarketPriceSnapshot.ml_status != "ok",
               MarketPriceSnapshot.estimated_color.is_not(None), *scope)  # type: ignore[union-attr]
        .group_by(MarketPriceSnapshot.estimated_color)
    ).all())
    states = {
        name: int(session.exec(
            select(func.count(MarketPriceSnapshot.id))  # type: ignore[arg-type]
            .where(MarketPriceSnapshot.run_id == run_id, *base_scope, *_match_condition(name))
        ).one() or 0)
        for name in ("igual", "solo_similar", "solo_diferente", "ninguno")
    }
    return {
        "run_id": run_id,
        "items": [price_monitor.snapshot_to_dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": (page + 1) * page_size < total,
        "colors": {c: int(colors.get(c, 0)) for c in COLORS},
        "estimated_colors": {c: int(estimated_counts.get(c, 0)) for c in ("verde", "amarillo", "rojo")},
        "states": states,
    }


@router.get("/products/{product_id}/history")
async def product_history(
    product_id: str = Path(..., min_length=1, max_length=PRODUCT_ID_MAX_LEN),
    limit: int = Query(60, ge=1, le=365),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rows = session.exec(
        select(MarketPriceSnapshot)
        .where(MarketPriceSnapshot.product_id == product_id)
        .order_by(MarketPriceSnapshot.captured_at.desc())  # type: ignore[union-attr]
        .limit(limit)
    ).all()
    return {"product_id": product_id, "items": [price_monitor.snapshot_to_dict(r) for r in rows]}


class NotSameBody(BaseModel):
    ml_id: str = Field(min_length=3, max_length=24)


def _commit_correction(session: Session, snap: MarketPriceSnapshot, snapshot_id: int, added: bool) -> dict[str, Any]:
    """Guarda en UNA transacción la corrección, el snapshot recalculado y los
    contadores de la corrida. Si otra pestaña guardó la misma corrección en este
    instante (IntegrityError), responde "ya estaba" con lo que quedó."""
    session.add(snap)
    price_monitor.recount_run(session, snap.run_id)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        session.expire_all()
        fresh = session.get(MarketPriceSnapshot, snapshot_id)
        return {"snapshot": price_monitor.snapshot_to_dict(fresh), "already": True}  # type: ignore[arg-type]
    session.refresh(snap)
    return {"snapshot": price_monitor.snapshot_to_dict(snap), "already": not added}


@router.post("/snapshots/{snapshot_id}/not-same")
async def not_the_same(
    body: NotSameBody,
    request: Request,
    snapshot_id: int = Path(..., ge=1, le=DB_INT_MAX),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """"No es el mismo": una persona dice que esta publicación de ML no es el
    producto. Pasa a la lista de diferentes del snapshot (nada se descarta; con "Es
    el mismo" se puede dar vuelta) y, si era idéntica, se recalculan el color real
    y los contadores. Queda como diferente para ese producto en las próximas
    corridas y se guarda como etiqueta (label 0) para calibrar, con quién la marcó.
    No toca Vendure ni el precio nuestro.

    La corrección, el snapshot y los contadores se guardan en UNA transacción: o
    quedan los tres o ninguno. Apretarlo dos veces (o desde dos pestañas a la vez)
    no duplica nada."""
    if not match_feedback.valid_ml_id(body.ml_id):
        raise HTTPException(400, "ml_id inválido")
    snap = session.get(MarketPriceSnapshot, snapshot_id)
    if snap is None:
        raise HTTPException(404, "snapshot no encontrado")
    removed = price_monitor.drop_listing(snap, body.ml_id)
    if removed is None:
        already = body.ml_id in match_feedback.load_excluded().get(snap.product_id, frozenset())
        if not already:
            raise HTTPException(404, "esa publicación no está en este snapshot")
        return {"snapshot": price_monitor.snapshot_to_dict(snap), "already": True}
    actor = auth.session_username(request.cookies.get(auth.COOKIE_NAME))
    added = match_feedback.add_feedback(
        product_id=snap.product_id, ml_id=body.ml_id, entry=removed, snapshot_id=snapshot_id,
        product_name=snap.product_name, actor=actor, label=match_feedback.LABEL_NOT_SAME, session=session,
    )
    return _commit_correction(session, snap, snapshot_id, added)


@router.post("/snapshots/{snapshot_id}/same")
async def same_product(
    body: NotSameBody,
    request: Request,
    snapshot_id: int = Path(..., ge=1, le=DB_INT_MAX),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """"Es el mismo": una persona dice que esta publicación SIMILAR o DIFERENTE es
    el producto. Pasa a idéntica para ese producto: se recalculan mediana, mínimo,
    ganancia y color real contando su precio (si lo tiene), el color estimado
    desaparece, y la próxima corrida la respeta como IGUAL (label 1 en
    market_match_feedback, con quién la marcó). Misma transacción única y misma
    idempotencia que "No es el mismo"; "Deshacer" borra la marca (DELETE)."""
    if not match_feedback.valid_ml_id(body.ml_id):
        raise HTTPException(400, "ml_id inválido")
    snap = session.get(MarketPriceSnapshot, snapshot_id)
    if snap is None:
        raise HTTPException(404, "snapshot no encontrado")
    category = price_monitor.listing_category(snap, body.ml_id)
    if category is None:
        raise HTTPException(404, "esa publicación no está en este snapshot")
    if category == "igual":
        return {"snapshot": price_monitor.snapshot_to_dict(snap), "already": True}
    promoted = price_monitor.promote_listing(snap, body.ml_id)
    actor = auth.session_username(request.cookies.get(auth.COOKIE_NAME))
    added = match_feedback.add_feedback(
        product_id=snap.product_id, ml_id=body.ml_id, entry=promoted, snapshot_id=snapshot_id,
        product_name=snap.product_name, actor=actor, label=match_feedback.LABEL_SAME, session=session,
    )
    return _commit_correction(session, snap, snapshot_id, added)


@router.delete("/products/{product_id}/feedback/{ml_id}")
@router.delete("/products/{product_id}/not-same/{ml_id}")
async def undo_feedback(
    product_id: str = Path(..., min_length=1, max_length=PRODUCT_ID_MAX_LEN),
    ml_id: str = Path(..., min_length=3, max_length=24),
) -> dict[str, Any]:
    """Deshacer la última marca ("No es el mismo" o "Es el mismo") de esa
    publicación para ese producto. Si la persona había cambiado de opinión vuelve a
    la marca anterior (`{"removed": false, "restored": 0|1}`); si no, borra la marca
    (`{"removed": true}`) y la próxima corrida vuelve a juzgarla sola. El snapshot de
    hoy no se reconstruye."""
    if not match_feedback.valid_ml_id(ml_id):
        raise HTTPException(400, "ml_id inválido")
    result = match_feedback.undo_feedback(product_id, ml_id)
    if result is None:
        raise HTTPException(404, "esa publicación no estaba marcada")
    return result
