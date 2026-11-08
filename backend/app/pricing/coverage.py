"""Cobertura del semáforo sobre el catálogo publicado (lo ACTIVO en Vendure).

La corrida de la noche (`price_monitor`) ya mide todo el catálogo: los activos siempre y los apagados con
`pm_include_disabled`. Con el cupo diario de ML (`pm_ml_daily_budget`) lo que no entra queda `skipped` y se mide primero la
noche siguiente (el que hace más que no se mide va adelante). Este módulo agrega:

  * `order_for_coverage`: con `pm_order_reds_first = 1`, primero los activos que la última vez dieron rojo, después el resto
    de los activos y al final los apagados (dentro de cada grupo, el que se midió hace más tiempo). Así, si se acaba el
    cupo, lo que queda afuera es lo menos urgente. PURA.
  * `summary`: cuántos activos hay, cuántos tienen medición vigente y de qué color (verde / amarillo / rojo / sin dato /
    no medible), cuántos rojos firmes y cuáles no se midieron (`GET /api/price-monitor/coverage`).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlmodel import Session, func, select

from app.clock import utcnow
from app.db.models import MarketPriceSnapshot, PriceMonitorRun
from app.db.session import engine
from app.pricing import semaforo

# Una medición más vieja que esto ya no cuenta como «cubierto».
FRESH_DAYS = 7
MISSING_SHOWN = 50


def order_for_coverage(products: Sequence[Any], last_measured: dict[str, datetime],
                       last_colors: dict[str, str]) -> list[Any]:
    """Rojos activos → resto de activos → apagados; en cada grupo, el que se midió hace más tiempo (o nunca) primero.
    `products` son objetos con `id` y `enabled`. Orden estable."""
    never = datetime.min

    def key(p: Any) -> tuple[int, datetime]:
        group = 2 if not p.enabled else (0 if last_colors.get(p.id) == semaforo.ROJO else 1)
        return group, last_measured.get(p.id) or never

    return sorted(products, key=key)


def last_colors(product_ids: Iterable[str]) -> dict[str, str]:
    """Color del último snapshot de cada producto."""
    ids = set(product_ids)
    if not ids:
        return {}
    with Session(engine) as s:
        newest = select(func.max(MarketPriceSnapshot.id)).group_by(MarketPriceSnapshot.product_id)
        rows = s.exec(select(MarketPriceSnapshot.product_id, MarketPriceSnapshot.color)
                      .where(MarketPriceSnapshot.id.in_(newest))).all()  # type: ignore[union-attr]
    return {pid: color for pid, color in rows if pid in ids}


def summarize(active_ids: set[str] | None, snaps: Sequence[MarketPriceSnapshot], now: datetime,
              fresh_days: int = FRESH_DAYS) -> dict[str, Any]:
    """El resumen, PURO: `snaps` = el último snapshot de cada producto; `active_ids` = los activos del catálogo (None si no
    se pudo leer: se usan los que la última medición vio activos)."""
    latest = {sn.product_id: sn for sn in snaps}
    if active_ids is None:
        active_ids = {pid for pid, sn in latest.items() if sn.product_enabled}
        source = "snapshots"
    else:
        source = "vendure"
    cutoff = now - timedelta(days=fresh_days)
    colors = {c: 0 for c in (*semaforo.COLORS, semaforo.NO_MEDIBLE)}
    firm = measured = stale = 0
    missing: list[str] = []
    for pid in sorted(active_ids):
        sn = latest.get(pid)
        if sn is None or sn.ml_status not in ("ok", "no_data") or sn.captured_at < cutoff:
            if sn is not None and sn.captured_at < cutoff:
                stale += 1
            if sn is not None and sn.unmeasurable:
                colors[semaforo.NO_MEDIBLE] += 1
            missing.append(pid)
            continue
        measured += 1
        colors[sn.color if sn.color in colors else semaforo.SIN_DATO] += 1
        if sn.color == semaforo.ROJO and (sn.review_flag or "") == "firme":
            firm += 1
    total = len(active_ids)
    return {
        "fuente_activos": source, "activos": total, "medidos": measured,
        "cobertura_pct": round(measured / total * 100, 1) if total else None,
        "verdes": colors[semaforo.VERDE], "amarillos": colors[semaforo.AMARILLO], "rojos": colors[semaforo.ROJO],
        "sin_dato": colors[semaforo.SIN_DATO], "no_medibles": colors[semaforo.NO_MEDIBLE], "rojos_firmes": firm,
        "sin_medir": len(missing), "medicion_vieja": stale, "sin_medir_ids": missing[:MISSING_SHOWN],
        "dias_vigencia": fresh_days,
    }


def summary(active_ids: set[str] | None, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    with Session(engine) as s:
        newest = select(func.max(MarketPriceSnapshot.id)).group_by(MarketPriceSnapshot.product_id)
        snaps = list(s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.id.in_(newest))).all())  # type: ignore[union-attr]
        run = s.exec(select(PriceMonitorRun).order_by(PriceMonitorRun.id.desc()).limit(1)).first()  # type: ignore[union-attr]
    out = summarize(active_ids, snaps, now)
    from app import runtime

    out["rojos_primero"] = bool(int(runtime.get("pm_order_reds_first") or 0))
    out["ultima_corrida"] = None if run is None else {
        "id": run.id, "status": run.status, "started_at": run.started_at.isoformat() + "Z" if run.started_at else None,
        "finished_at": run.finished_at.isoformat() + "Z" if run.finished_at else None,
        "total": run.total_products, "procesados": run.processed}
    return out
