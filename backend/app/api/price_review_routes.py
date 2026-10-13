"""Endpoints de la bandeja de revisión de precios (dashboard, sesión por cookie). SOLO LECTURA en esta etapa.

GET /api/price-review/summary            → modo, conteos por estado, pendientes, antigüedad y última pasada
GET /api/price-review/items              → listado paginado (state, cause, q, page)
GET /api/price-review/items/{id}         → detalle: ficha, diagnóstico a-e, desglose del precio, publicaciones
                                           de ML y de las tiendas, historia (eventos) y links

Nada de acá escribe: ni en Vendure ni en las tablas. Las decisiones de Pao (aprobar, corregir, mantener,
despublicar, deshacer) llegan en la etapa siguiente. Los botones «No es el mismo» / «Es el mismo» siguen siendo
los del semáforo (`/api/price-monitor/snapshots/{id}/not-same`), que cierran la ficha si el producto deja de ser rojo.

Viven bajo /api/ → el middleware de auth exige sesión del dashboard.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from sqlalchemy import or_
from sqlmodel import Session, func, select

from app import auth, runtime
from app.clock import utcnow
from app.config import get_settings
from app.db.models import MarketPriceSnapshot, PriceMonitorRun, PriceReviewEvent, PriceReviewItem
from app.db.session import get_session
from app.notifier.email import parse_recipients
from app.pricing import price_monitor, price_review, store_match

router = APIRouter(prefix="/api/price-review", tags=["price-review"])

PAGE_SIZE_DEFAULT = 25
PAGE_SIZE_MAX = 100
PAGE_MAX = 10_000
DB_INT_MAX = 2**31 - 1

# Las pestañas de la pantalla → estados.
GROUPS: dict[str, tuple[str, ...]] = {
    "para_decidir": (price_review.PROPUESTO,),
    "en_proceso": (price_review.EN_REVISION, price_review.CORRIGIENDO, price_review.BUSCANDO_PROVEEDOR),
    "mantenidos": (price_review.MANTENIDO,),
    "cerrados": (price_review.DESPUBLICADO, price_review.CORREGIDO, price_review.CERRADO_AUTO),
}
ALL_STATES = tuple(s for states in GROUPS.values() for s in states)
CAUSES = ("a", "b", "c", "d", "e", "ninguna", "sin_dato", "pendiente")

CAUSE_LABEL = {
    "a": "Caja fuera de rango", "b": "Tramos mal armados", "c": "Certificado alto",
    "d": "Valor bajo por m³", "e": "ML bajo el costo de fábrica", "ninguna": "Sin causa clara",
    "sin_dato": "Diagnóstico incompleto",
}
STATE_LABEL = {
    "propuesto": "Propuesto", "en_revision": "En revisión", "corrigiendo": "Corrigiendo",
    "buscando_proveedor": "Buscando proveedor", "mantenido": "Mantenido", "despublicado": "Despublicado",
    "corregido": "Corregido", "cerrado_auto": "Cerrado solo",
}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def _link(template: str, **values: str) -> str | None:
    """Link a partir de una plantilla del entorno (`https://…/{id}`). Solo https, y solo si trae el
    marcador; los valores se escapan: nada de lo que viene de la base arma una URL a medida."""
    if not template or not template.lower().startswith("https://"):
        return None
    out, used = template, 0
    for key, value in values.items():
        marker = "{" + key + "}"
        if marker in out:
            out = out.replace(marker, quote(str(value), safe=""))
            used += 1
    # Sin ningún marcador la plantilla da el MISMO link para todas las fichas; con un marcador sin valor, un link a medias.
    return None if used == 0 or "{" in out or "}" in out else out


def _https_or_none(url: str | None) -> str | None:
    """Una foto solo si es una URL https con host, sin usuario ni contraseña (`https://user:pass@host/…`) y sin puerto raro:
    lo que llega a un <img src> no es de fiar aunque venga de nuestra base."""
    if not isinstance(url, str) or len(url) > 500 or any(ord(c) < 33 or ord(c) == 127 for c in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return None
    return url if port in (None, 443) else None


def _loads(raw: str | None) -> Any:
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


# ─── Quién ve los costos ──────────────────────────────────────────


def costs_visible(request: Request) -> bool:
    """¿La sesión puede ver el costo de fábrica y el fee? Solo los emails de `PRICE_REVIEW_VIEWERS` (fail-closed: vacío =
    nadie). La sesión sin usuario identificable (cookie sin email) tampoco."""
    user = auth.session_username(request.cookies.get(auth.COOKIE_NAME))
    if not user:
        return False
    return user.strip().lower() in {e.lower() for e in parse_recipients(get_settings().price_review_viewers)}


def restrict_breakdown(bd: dict[str, Any] | None) -> dict[str, Any] | None:
    """El desglose de un tramo SIN el costo de fábrica: se sacan los yuanes, el cambio, el FOB (componente «fábrica»),
    el fee en % y todo lo que permita despejarlos (la factura y el neto en dólares, el % de cada parte y los pesos por
    unidad de cada parte). Quedan las partes de logística, aranceles, certificado, etc. en dólares."""
    if not bd:
        return bd
    out = {k: v for k, v in bd.items()
           if k not in ("cny_unit", "cny_to_usd", "usd_to_channel", "fee_pct", "factura_usd", "neto_usd", "components")}
    out["components"] = [
        {"key": c.get("key"), "label": c.get("label"), "usd": c.get("usd"), "detail": c.get("detail") or []}
        for c in bd.get("components") or [] if c.get("key") != "fabrica"
    ]
    out["restricted"] = True
    return out


def restrict_tiers(tiers: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """La tabla de tramos sin el costo de fábrica en yuanes (precio de venta, neto y margen sí)."""
    return [{k: v for k, v in t.items() if k != "cny_unit"} for t in tiers or []]


def restrict_diagnosis(diag: dict[str, Any] | None) -> dict[str, Any] | None:
    """El diagnóstico sin evidencias (traen los costos por tramo, el FOB y el objetivo en yuanes) y con la frase pública de
    las reglas que, escritas completas, nombran cifras de fábrica."""
    if not diag:
        return diag
    out = {k: v for k, v in diag.items() if k not in ("tiers", "rules")}
    out["rules"] = [
        {**{k: v for k, v in r.items() if k not in ("evidence", "summary_public")}, "summary": r.get("summary_public") or r.get("summary")}
        for r in diag.get("rules") or []
    ]
    return out


def _cause_sentence(item: PriceReviewItem, diag: dict[str, Any] | None, *, costs: bool = True) -> str | None:
    """Una oración con el número que explica la causa probable (la de la regla que dispara)."""
    if not diag or item.probable_cause is None:
        return None
    if item.probable_cause == "ninguna":
        return "Ninguna de las cinco causas lo explica: lo más probable es que el match de Mercado Libre esté mal."
    if item.probable_cause == "sin_dato":
        missing = ", ".join(diag.get("missing") or []) or "algunos datos"
        return f"No se pudo completar el diagnóstico (falta: {missing}): no se puede descartar ninguna causa."
    for r in diag.get("rules") or []:
        if r.get("code") == item.probable_cause:
            return r.get("summary") if costs else (r.get("summary_public") or r.get("summary"))
    return None


def item_to_dict(item: PriceReviewItem, *, now: datetime | None = None, costs: bool = True) -> dict[str, Any]:
    """La ficha como la ve la lista. Sin el diagnóstico completo (eso va en el detalle). `costs=False`: sin cifras de
    costo de fábrica en la frase de la causa."""
    now = now or utcnow()
    diag = _loads(item.diag)
    cfg = get_settings()
    return {
        "id": item.id,
        "product": {"id": item.product_id, "name": item.product_name, "code": item.product_code,
                    "slug": item.product_slug, "image_url": _https_or_none(item.image_url)},
        "state": item.state, "state_label": STATE_LABEL.get(item.state, item.state),
        "state_since": _iso(item.state_since), "opened_at": _iso(item.opened_at), "closed_at": _iso(item.closed_at),
        "waiting_days": round(max(0.0, (now - item.state_since).total_seconds() / 86400.0), 1) if item.state_since else None,
        "mode_at_open": item.mode_at_open, "streak_nights": item.streak_nights,
        "last_color": item.last_color, "last_margin_pct": item.last_margin_pct,
        "our_price_cents": item.our_price_cents, "ml_median_cents": item.ml_median_cents,
        "firm_listing_count": item.firm_listing_count, "firm_seller_count": item.firm_seller_count,
        "price_ratio": item.price_ratio, "reff_ratio": item.reff_ratio,
        "flags": _loads(item.flags) or [],
        "probable_cause": item.probable_cause,
        "cause_label": CAUSE_LABEL.get(item.probable_cause or "", None),
        "cause_summary": _cause_sentence(item, diag, costs=costs),
        "fixable": item.fixable, "diag_status": item.diag_status,
        "links": {
            "vendure_admin": _link(cfg.vendure_admin_product_url, id=item.product_id),
            "storefront": _link(cfg.storefront_product_url, slug=item.product_slug or "", id=item.product_id),
        },
    }


def event_to_dict(ev: PriceReviewEvent) -> dict[str, Any]:
    return {
        "id": ev.id, "at": _iso(ev.at), "actor": ev.actor, "kind": ev.kind, "from_state": ev.from_state,
        "to_state": ev.to_state, "motive_code": ev.motive_code, "motive_text": ev.motive_text,
        "run_id": ev.run_id, "snapshot_id": ev.snapshot_id, "payload": _loads(ev.payload),
    }


# ─── Resumen ──────────────────────────────────────────────────────


@router.get("/summary")
async def summary(session: Session = Depends(get_session)) -> dict[str, Any]:
    now = utcnow()
    by_state = dict(session.exec(
        select(PriceReviewItem.state, func.count(PriceReviewItem.id)).group_by(PriceReviewItem.state)  # type: ignore[arg-type]
    ).all())
    groups = {name: int(sum(by_state.get(s, 0) for s in states)) for name, states in GROUPS.items()}
    oldest = session.exec(
        select(func.min(PriceReviewItem.state_since)).where(PriceReviewItem.state == price_review.PROPUESTO)
    ).one()
    mode = int(runtime.get("pm_mode") or 0)
    last = session.exec(
        select(PriceMonitorRun).where(PriceMonitorRun.review_at.is_not(None))  # type: ignore[union-attr]
        .order_by(PriceMonitorRun.started_at.desc()).limit(1)  # type: ignore[union-attr]
    ).first()
    latest = session.exec(
        select(PriceMonitorRun).order_by(PriceMonitorRun.started_at.desc()).limit(1)  # type: ignore[union-attr]
    ).first()
    last_pass = None
    if last is not None:
        info = _loads(last.review_summary) or {}
        last_pass = {
            "run_id": last.id, "at": _iso(last.review_at), "run_started_at": _iso(last.started_at),
            "new": int(info.get("nuevas", 0)), "firm": int(info.get("firmes", 0)),
            "doubtful": int(sum((info.get("dudosos") or {}).values())), "doubtful_by_reason": info.get("dudosos") or {},
            "closed": int(info.get("cerradas_solas", 0)), "flips": int(last.n_flipped or 0),
            "vendure_reads": int(info.get("consultas_vendure", 0)), "read_cap_reached": bool(info.get("tope_alcanzado")),
            "note": info.get("note"),
        }
    failure = None
    if latest is not None and latest.review_at is None and latest.review_summary:
        failure = (_loads(latest.review_summary) or {}).get("error")
    return {
        "mode": mode, "mode_label": price_review.mode_label(mode), "mode_effective": min(mode, 1),
        "read_only": True,                          # en esta etapa la bandeja no decide ni escribe en Vendure
        "counts": {s: int(by_state.get(s, 0)) for s in ALL_STATES},
        "groups": groups, "pending": groups["para_decidir"],
        "oldest_pending_days": None if oldest is None else round((now - oldest).total_seconds() / 86400.0, 1),
        "last_pass": last_pass, "last_pass_error": failure,
        "params": {"confirm_nights": int(runtime.get("pm_review_confirm_nights")),
                   "data_max_age_h": int(runtime.get("pm_review_data_max_age_h")),
                   "min_listings": int(runtime.get("pm_review_min_listings")),
                   "min_sellers": int(runtime.get("pm_review_min_sellers")),
                   "max_price_ratio": float(runtime.get("pm_review_max_price_ratio"))},
    }


# ─── Listado ──────────────────────────────────────────────────────


@router.get("/items")
async def list_items(
    request: Request,
    state: str = Query("para_decidir", max_length=24),
    cause: str | None = Query(None, max_length=12),
    q: str | None = Query(None, max_length=120),
    page: int = Query(0, ge=0, le=PAGE_MAX),
    page_size: int = Query(PAGE_SIZE_DEFAULT, ge=1, le=PAGE_SIZE_MAX),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """`state`: una pestaña (para_decidir | en_proceso | mantenidos | cerrados), `abiertas`, `todas` o un estado.
    `cause`: a | b | c | d | e | ninguna | pendiente (diagnóstico sin completar)."""
    if state in GROUPS:
        states: tuple[str, ...] | None = GROUPS[state]
    elif state == "abiertas":
        states = price_review.OPEN_STATES
    elif state == "todas":
        states = None
    elif state in ALL_STATES:
        states = (state,)
    else:
        raise HTTPException(400, f"state inválido: {state}")
    if cause is not None and cause not in CAUSES:
        raise HTTPException(400, f"cause inválida: {cause}")
    q = (q or "").replace("\x00", "")           # Postgres rechaza el NUL en un texto
    base: list[Any] = []                      # estado y búsqueda: también acotan los contadores por causa
    if states is not None:
        base.append(PriceReviewItem.state.in_(states))  # type: ignore[attr-defined]
    if q and q.strip():
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        base.append(or_(PriceReviewItem.product_name.ilike(like, escape="\\"),  # type: ignore[union-attr]
                        PriceReviewItem.product_code.ilike(like, escape="\\"),  # type: ignore[union-attr]
                        PriceReviewItem.product_id == q.strip()))
    conds = list(base)
    if cause == "pendiente":
        conds.append(PriceReviewItem.diag_status.in_(("pendiente", "parcial")))  # type: ignore[union-attr]
    elif cause:
        conds.append(PriceReviewItem.probable_cause == cause)
    open_only = states is not None and all(s in price_review.OPEN_STATES for s in states)
    order = (PriceReviewItem.state_since.asc(), PriceReviewItem.id.asc()) if open_only else (  # type: ignore[union-attr]
        PriceReviewItem.closed_at.desc(), PriceReviewItem.id.desc())  # type: ignore[union-attr]
    total = int(session.exec(select(func.count(PriceReviewItem.id)).where(*conds)).one() or 0)  # type: ignore[arg-type]
    rows = session.exec(select(PriceReviewItem).where(*conds).order_by(*order)
                        .offset(page * page_size).limit(page_size)).all()
    cause_counts = dict(session.exec(
        select(PriceReviewItem.probable_cause, func.count(PriceReviewItem.id))  # type: ignore[arg-type]
        .where(*base).group_by(PriceReviewItem.probable_cause)).all())
    now = utcnow()
    costs = costs_visible(request)
    return {
        "items": [item_to_dict(r, now=now, costs=costs) for r in rows], "total": total, "page": page, "page_size": page_size,
        "has_more": (page + 1) * page_size < total,
        "causes": {c: int(cause_counts.get(c, 0)) for c in CAUSES if c != "pendiente"},
    }


# ─── Detalle ──────────────────────────────────────────────────────


@router.get("/items/{item_id}")
async def item_detail(
    request: Request,
    item_id: int = Path(..., ge=1, le=DB_INT_MAX),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    item = session.get(PriceReviewItem, item_id)
    if item is None:
        raise HTTPException(404, "ficha no encontrada")
    costs = costs_visible(request)
    out = item_to_dict(item, costs=costs)
    diag = _loads(item.diag) or {}
    breakdown = _loads(item.breakdown) or {}
    snap = session.get(MarketPriceSnapshot, item.last_snapshot_id) if item.last_snapshot_id else None
    if snap is None and item.opened_snapshot_id:
        snap = session.get(MarketPriceSnapshot, item.opened_snapshot_id)
    snapshot = None
    if snap is not None:
        snapshot = store_match.decorate_items(session, snap.run_id, [price_monitor.snapshot_to_dict(snap)])[0]
    events = session.exec(select(PriceReviewEvent).where(PriceReviewEvent.item_id == item.id)
                          .order_by(PriceReviewEvent.at.desc(), PriceReviewEvent.id.desc())).all()  # type: ignore[union-attr]
    params = price_review.ReviewParams.from_runtime()
    flags = out["flags"]
    tiers = breakdown.get("tiers") or diag.get("tiers") or []
    out.update({
        "costs_visible": costs,
        "diagnosis": (diag if costs else restrict_diagnosis(diag)) or None,
        "breakdown": breakdown.get("tier") if costs else restrict_breakdown(breakdown.get("tier")),
        "tiers": tiers if costs else restrict_tiers(tiers),
        "snapshot": snapshot,
        "data": None if snap is None else {
            "at": _iso(snap.data_at or snap.captured_at), "origin": snap.match_origin,
            "via": snap.web_via, "run_id": snap.run_id, "captured_at": _iso(snap.captured_at)},
        "doubtful_today": next((params.reason_text(f.split(":", 1)[1]) for f in flags if str(f).startswith("dudoso:")), None),
        "events": [event_to_dict(e) for e in events],
        "decisions_enabled": False,                   # las decisiones de Pao llegan en la etapa siguiente
    })
    return out
