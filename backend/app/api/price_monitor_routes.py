"""Endpoints del semáforo de precios (dashboard, sesión por cookie).

POST /api/price-monitor/run                    → dispara una corrida a mano
GET  /api/price-monitor/runs                   → últimas corridas
GET  /api/price-monitor/snapshots              → tabla paginada (run_id, color, q)
GET  /api/price-monitor/products/{id}/history  → tendencia de un producto
GET  /api/price-monitor/summary                → tarjeta de Salud

Viven bajo /api/ → el middleware de auth exige sesión del dashboard.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, func, select

from app.db.models import MarketPriceSnapshot, PriceMonitorRun
from app.db.session import get_session
from app.pricing import price_monitor
from app.pricing.semaforo import COLORS

router = APIRouter(prefix="/api/price-monitor", tags=["price-monitor"])

PAGE_SIZE_DEFAULT = 25
PAGE_SIZE_MAX = 200
STATUSES = ("ok", "no_data", "failed", "skipped")

# El event loop solo guarda referencias débiles a las tasks: sin esto, una
# corrida de horas disparada a mano podría ser recolectada a mitad de camino.
_background: set[asyncio.Task] = set()


@router.post("/run", status_code=202)
async def run_now() -> dict[str, Any]:
    """Dispara una corrida en background (modo sombra: no toca Vendure).
    409 si ya hay una en curso en este proceso."""
    from app.scheduler import jobs  # import tardío: jobs arrastra todo el scheduler

    if price_monitor.price_monitor_lock.locked():
        raise HTTPException(409, "Ya hay una corrida del semáforo en curso.")
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


@router.get("/snapshots")
async def list_snapshots(
    run_id: int | None = Query(None, ge=1),
    color: str | None = Query(None),
    status: str | None = Query(None),
    q: str | None = Query(None, max_length=120),
    page: int = Query(0, ge=0),
    page_size: int = Query(PAGE_SIZE_DEFAULT, ge=1, le=PAGE_SIZE_MAX),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Tabla del semáforo. Sin `run_id` muestra la última corrida."""
    if color and color not in COLORS:
        raise HTTPException(400, f"color inválido: {color}")
    if status and status not in STATUSES:
        raise HTTPException(400, f"status inválido: {status}")
    if run_id is None:
        run_id = _latest_run_id(session)
    if run_id is None:
        return {"run_id": None, "items": [], "total": 0, "page": page, "page_size": page_size,
                "has_more": False, "colors": {}}

    base = select(MarketPriceSnapshot).where(MarketPriceSnapshot.run_id == run_id)
    count_stmt = select(func.count(MarketPriceSnapshot.id)).where(  # type: ignore[arg-type]
        MarketPriceSnapshot.run_id == run_id
    )
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
            MarketPriceSnapshot.product_name.asc(),  # type: ignore[union-attr]
        )
        .offset(page * page_size)
        .limit(page_size)
    ).all()
    colors = dict(session.exec(
        select(MarketPriceSnapshot.color, func.count(MarketPriceSnapshot.id))  # type: ignore[arg-type]
        .where(MarketPriceSnapshot.run_id == run_id)
        .group_by(MarketPriceSnapshot.color)
    ).all())
    return {
        "run_id": run_id,
        "items": [price_monitor.snapshot_to_dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": (page + 1) * page_size < total,
        "colors": {c: int(colors.get(c, 0)) for c in COLORS},
    }


@router.get("/products/{product_id}/history")
async def product_history(
    product_id: str,
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
