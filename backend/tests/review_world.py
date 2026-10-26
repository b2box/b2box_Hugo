"""Mundo de prueba para la bandeja de revisión (PR2): corridas y snapshots armados a mano en la SQLite de la suite.

`snap()` arma un rojo firme por defecto (3 idénticos confirmados por foto + nombre, 3 vendedores, dato de la hora de
la corrida, nuestro precio 1,11 veces el de ML) y cada argumento rompe UNA condición.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete
from sqlmodel import Session, select

from app import runtime
from app.db.models import (
    MarketMatchFeedback,
    MarketPriceSnapshot,
    MarketStore,
    PriceMonitorRun,
    PriceReviewEvent,
    PriceReviewItem,
    Setting,
    StoreMatch,
    StoreMatchFeedback,
)
from app.db.session import engine, init_db
from app.pricing import semaforo

NOW = datetime(2026, 10, 14, 9, 0, 0)          # la hora a la que "corre" la pasada de la noche en los tests
OK_PRICES = (900_000, 905_000, 895_000)         # mediana 900.000 → nuestro 1.000.000 es rojo (P_max ≈ 711.818)


def reset_db() -> None:
    """Base limpia + settings por defecto. La suite usa UNA SQLite compartida: cada test empieza de cero."""
    init_db()
    with Session(engine) as s:
        for model in (PriceReviewEvent, PriceReviewItem, MarketPriceSnapshot, PriceMonitorRun, Setting, StoreMatch,
                      StoreMatchFeedback, MarketStore, MarketMatchFeedback):
            s.execute(delete(model))
        s.commit()
    runtime.invalidate()


def set_setting(key: str, value: Any) -> None:
    runtime.set_value(key, value)


def make_run(status: str = "ok", *, started: datetime | None = None, mode: int = 1) -> int:
    with Session(engine) as s:
        run = PriceMonitorRun(status=status, mode=mode, started_at=started or NOW, finished_at=started or NOW,
                              total_products=0)
        s.add(run)
        s.commit()
        s.refresh(run)
        return int(run.id)


def listing(i: int, price: int, *, source: str = "clip", seller: str | None = None, origin: str = "api") -> dict[str, Any]:
    return {"ml_id": f"MLA{1000 + i}", "title": f"Producto idéntico {i}",
            "permalink": f"https://articulo.mercadolibre.com.ar/MLA-{1000 + i}-x",
            "origin": origin, "category": "igual", "source": source, "image_score": 0.9, "name_score": 0.8,
            "confidence": None, "reason": "", "differences": [], "notes": [], "brand": None, "image_url": None,
            "specs": {}, "listings": 1, "min_cents": price, "median_cents": price, "prices_cents": [price],
            "sellers": [seller or f"seller{i}"], "seller": seller or f"seller{i}"}


def add_snap(run_id: int, pid: str, *, color: str = "rojo", ml_status: str = "ok", prices: tuple[int, ...] = OK_PRICES,
             sources: tuple[str, ...] | None = None, sellers: tuple[str, ...] | None = None, our: int = 1_000_000,
             enabled: bool = True, origin: str = "api", data_at: datetime | None = NOW, basis: str = "ml",
             flag: str | None = None, variant: str = "101", tier: str = "tier:min_qty=25", specs: dict | None = None,
             prev_color: str | None = None, name: str | None = None, web_state: str | None = None,
             captured_at: datetime | None = None, commission: float = 13.0, shipping: int = 0,
             matched: list[dict] | None = None, **kw: Any) -> int:
    """Un snapshot. `matched` pisa la lista de idénticos entera; `flag` es el `review_flag` de esa noche."""
    entries = matched if matched is not None else [
        listing(i, p, source=sources[i - 1] if sources and i <= len(sources) else "clip",
                seller=sellers[i - 1] if sellers and i <= len(sellers) else None, origin=origin)
        for i, p in enumerate(prices, 1)]
    all_prices = [p for e in entries for p in e["prices_cents"]]
    median = semaforo.median_cents(all_prices) if all_prices else None
    margin = semaforo.estimated_margin_pct(median, our, commission, shipping) if median else None
    snap = MarketPriceSnapshot(
        run_id=run_id, product_id=pid, variant_id=variant, captured_at=captured_at or NOW, ml_status=ml_status,
        ml_median_cents=median if ml_status == "ok" else None, ml_min_cents=min(all_prices) if all_prices else None,
        ml_listing_count=len(all_prices) if ml_status == "ok" else 0,
        ml_seller_count=len({s for e in entries for s in e["sellers"]}) if ml_status == "ok" else 0,
        matched_listings=json.dumps(entries) if entries and ml_status == "ok" else None,
        match_source="clip", match_origin=origin if ml_status == "ok" else None, our_price_cents=our,
        tier_used=tier, commission_pct=commission, shipping_cents=shipping,
        est_margin_pct=None if margin is None else round(margin, 2), color=color, prev_color=prev_color,
        product_name=name or f"Producto {pid}", product_code=f"BX{pid}", product_image_url=f"https://cdn.b2box/{pid}.jpg",
        product_slug=f"producto-{pid}", product_enabled=enabled, data_at=data_at, price_basis=basis,
        review_flag=flag, web_state=web_state, our_specs=json.dumps(specs) if specs else None,
        web_via="oficina" if origin == "oficina" else None, **kw)
    with Session(engine) as s:
        s.add(snap)
        s.commit()
        s.refresh(snap)
        return int(snap.id)


def items(state: str | None = None) -> list[PriceReviewItem]:
    with Session(engine) as s:
        stmt = select(PriceReviewItem).order_by(PriceReviewItem.id)
        if state:
            stmt = stmt.where(PriceReviewItem.state == state)
        return list(s.exec(stmt))


def events(kind: str | None = None) -> list[PriceReviewEvent]:
    with Session(engine) as s:
        stmt = select(PriceReviewEvent).order_by(PriceReviewEvent.id)
        if kind:
            stmt = stmt.where(PriceReviewEvent.kind == kind)
        return list(s.exec(stmt))


def snap_of(run_id: int, pid: str) -> MarketPriceSnapshot:
    with Session(engine) as s:
        return s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.run_id == run_id,
                                                        MarketPriceSnapshot.product_id == pid)).one()


def run_of(run_id: int) -> PriceMonitorRun:
    with Session(engine) as s:
        return s.get(PriceMonitorRun, run_id)


def days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)
