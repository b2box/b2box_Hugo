"""Endpoints de las tiendas (dashboard, sesión por cookie).

GET    /api/stores                                   → tiendas + estado del índice
POST   /api/stores                                   → carga una tienda nueva
PUT    /api/stores/{id}                              → edita (o apaga) una tienda
DELETE /api/stores/{id}                              → la borra con todo lo que se guardó de ella
POST   /api/stores/{id}/index                        → indexa ahora (en background)
POST   /api/price-monitor/store-matches/{id}/label   → "Es el mismo" / "No es el mismo"
DELETE /api/price-monitor/store-matches/{id}/label   → deshace la corrección

Agregar otra tienda Tiendanube es un POST (o la fila desde Configuración): sin deploy.
Viven bajo /api/ → el middleware de auth exige sesión del dashboard.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import auth
from app.clock import utcnow
from app.db.models import MarketPriceSnapshot, MarketStore
from app.db.session import get_session
from app.pricing import price_monitor, store_catalog, store_match, store_parse

log = logging.getLogger(__name__)
router = APIRouter(tags=["stores"])

DB_INT_MAX = 2**31 - 1
# Ver price_monitor_routes: el loop solo guarda referencias débiles a las tasks.
_background: set[asyncio.Task] = set()


class StoreBody(BaseModel):
    """Lo que se carga desde Configuración. Todo opcional al editar."""
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, max_length=120)
    base_url: str | None = Field(None, max_length=300)
    platform: str | None = Field(None, max_length=30)
    enabled: bool | None = None
    refresh_days: int | None = None
    max_pages_per_day: int | None = None
    sitemap_url: str | None = Field(None, max_length=300)
    image_hosts: str | None = Field(None, max_length=300)
    house_brand: str | None = Field(None, max_length=120)
    notes: str | None = Field(None, max_length=800)


class LabelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., max_length=8)


def _clean(body: StoreBody, *, partial: bool, current: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return store_catalog.clean_store_fields(body.model_dump(exclude_unset=True), partial=partial, current=current)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _actor(request: Request) -> str:
    """Quién hizo el cambio, para el log: borrar una tienda borra también sus correcciones humanas."""
    return auth.session_username(request.cookies.get(auth.COOKIE_NAME)) or "?"


def _listing(session: Session) -> list[dict[str, Any]]:
    status = {s["id"]: s for s in store_catalog.index_status()}
    return [{**store_catalog.store_to_dict(row), "index": status.get(row.id)}
            for row in session.exec(select(MarketStore).order_by(MarketStore.id)).all()]


@router.get("/api/stores")
async def list_stores(session: Session = Depends(get_session)) -> dict[str, Any]:
    return {"items": _listing(session), "platforms": list(store_parse.PLATFORMS),
            "affect_color": store_match.affect_color_enabled()}


@router.post("/api/stores", status_code=201)
async def create_store(body: StoreBody, request: Request, session: Session = Depends(get_session)) -> dict[str, Any]:
    fields = _clean(body, partial=False)
    try:
        store_catalog.check_conflicts(session, fields, creating=True)
    except store_catalog.StoreConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    row = MarketStore(**fields)
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "ya hay una tienda con ese nombre") from None
    session.refresh(row)
    store_catalog.refresh_allowed_image_hosts()
    log.info("tiendas: %s cargó la tienda «%s» (%s, %s)", _actor(request), row.name, row.base_url, row.platform)
    return store_catalog.store_to_dict(row)


@router.put("/api/stores/{store_id}")
async def update_store(body: StoreBody, request: Request, store_id: int = Path(..., ge=1, le=DB_INT_MAX),
                       session: Session = Depends(get_session)) -> dict[str, Any]:
    row = session.get(MarketStore, store_id)
    if row is None:
        raise HTTPException(404, "tienda no encontrada")
    fields = _clean(body, partial=True, current={"base_url": row.base_url, "platform": row.platform})
    try:
        store_catalog.check_conflicts(session, fields, exclude_id=store_id)
    except store_catalog.StoreConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    for key, value in fields.items():
        setattr(row, key, value)
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "ya hay una tienda con ese nombre") from None
    session.refresh(row)
    store_catalog.refresh_allowed_image_hosts()
    log.info("tiendas: %s editó la tienda «%s» (%s)", _actor(request), row.name, ", ".join(sorted(fields)) or "sin cambios")
    return store_catalog.store_to_dict(row)


@router.delete("/api/stores/{store_id}")
async def remove_store(request: Request, store_id: int = Path(..., ge=1, le=DB_INT_MAX)) -> dict[str, bool]:
    info = await asyncio.to_thread(store_catalog.get_store, store_id)
    if not await asyncio.to_thread(store_catalog.delete_store, store_id):
        raise HTTPException(404, "tienda no encontrada")
    store_catalog.refresh_allowed_image_hosts()
    log.warning("tiendas: %s BORRÓ la tienda «%s» con su catálogo, coincidencias y correcciones",
                _actor(request), info.name if info else store_id)
    return {"removed": True}


@router.post("/api/stores/{store_id}/index", status_code=202)
async def index_now(request: Request, store_id: int = Path(..., ge=1, le=DB_INT_MAX),
                    session: Session = Depends(get_session)) -> dict[str, str]:
    """Indexa esa tienda ahora (dentro de su tope diario de páginas y a su ritmo). Tarda:
    corre en background; el avance se ve en `index` de GET /api/stores. No se puede repetir en loop:
    entre dos pasadas manuales pasan `MANUAL_COOLDOWN_MIN` minutos (el sitio es de un tercero)."""
    info = await asyncio.to_thread(store_catalog.get_store, store_id)
    if info is None:
        raise HTTPException(404, "tienda no encontrada")
    lock = store_catalog._locks.get(store_id)
    if lock is not None and lock.locked():
        raise HTTPException(409, "ya hay un indexado de esa tienda en curso")
    row = session.get(MarketStore, store_id)
    if row is not None and row.last_indexed_at is not None:
        wait_s = int(store_catalog.MANUAL_COOLDOWN_MIN * 60 - (utcnow() - row.last_indexed_at).total_seconds())
        if wait_s > 0:
            raise HTTPException(429, f"La última pasada de esa tienda terminó hace poco. Probá de nuevo en "
                                     f"{max(1, round(wait_s / 60))} min.", headers={"Retry-After": str(wait_s)})
    log.info("tiendas: %s pidió indexar «%s» ahora", _actor(request), info.name)
    task = asyncio.create_task(store_catalog.index_store(store_id))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"status": "scheduled", "store": info.name}


def _labeled(session: Session, match: Any) -> dict[str, Any]:
    info = store_catalog.get_store(match.store_id)
    snap = session.exec(select(MarketPriceSnapshot).where(
        MarketPriceSnapshot.run_id == match.run_id, MarketPriceSnapshot.product_id == match.product_id)).first()
    return {
        "match": store_match.match_to_dict(match, info) if info else None,
        "snapshot": None if snap is None else {
            "id": snap.id, "color": snap.color, "est_margin_pct": snap.est_margin_pct, "price_basis": snap.price_basis},
    }


@router.post("/api/price-monitor/store-matches/{match_id}/label")
async def label_match(body: LabelBody, request: Request, match_id: int = Path(..., ge=1, le=DB_INT_MAX),
                      session: Session = Depends(get_session)) -> dict[str, Any]:
    """"Es el mismo" (`es`) / "No es el mismo" (`no_es`) sobre un candidato de una tienda.
    Cambia su categoría en esa corrida, queda recordado para las próximas y los
    contadores (y el color, si las tiendas cuentan) se rehacen en la misma transacción."""
    if body.label not in store_match.LABELS:
        raise HTTPException(400, "label inválido: es | no_es")
    actor = auth.session_username(request.cookies.get(auth.COOKIE_NAME))
    m = store_match.set_label(session, match_id, body.label, actor)
    if m is None:
        raise HTTPException(404, "coincidencia no encontrada")
    price_monitor.recount_run(session, m.run_id)
    try:
        session.commit()
    except IntegrityError:
        # Otra pestaña la marcó en este mismo instante.
        session.rollback()
        session.expire_all()
        m = session.get(type(m), match_id)
    session.refresh(m)
    return _labeled(session, m)


@router.delete("/api/price-monitor/store-matches/{match_id}/label")
async def unlabel_match(match_id: int = Path(..., ge=1, le=DB_INT_MAX),
                        session: Session = Depends(get_session)) -> dict[str, Any]:
    m = store_match.clear_label(session, match_id)
    if m is None:
        raise HTTPException(404, "coincidencia no encontrada")
    price_monitor.recount_run(session, m.run_id)
    session.commit()
    session.refresh(m)
    return _labeled(session, m)
