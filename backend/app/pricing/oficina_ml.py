"""Buscador de la oficina: la búsqueda web de ML hecha desde la Mac de la oficina.

Desde el servidor, ML contesta con captcha a la IP del proxy residencial y Hugo corta la
búsqueda web (con razón: no se esquiva). Desde la conexión de la oficina ML responde la
búsqueda normal. Diseño: **la Mac busca de a poco y Hugo hace el matching**.

    Mac (backend/tools/oficina_ml_search.py)            Hugo (este módulo + api/oficina_routes.py)
    ────────────────────────────────────────            ──────────────────────────────────────────
    GET  /api/oficina/ml-queue      ────────────▶  qué productos buscar y con qué consultas
    busca en listado.mercadolibre.com.ar (sin proxy)
    POST /api/oficina/ml-results    ────────────▶  re-sanea TODO y guarda en `ml_web_result`
                                                    el semáforo usa lo fresco como fuente "web"
                                                    (origen `oficina`) en vez de buscar desde el servidor

Hugo no confía en la Mac: lo que llega es texto de terceros (viene de la página de ML) pasando por una
máquina y un canal que no controlamos, así que se vuelve a sanear campo por campo (ids, links, fotos, precios,
títulos) con las mismas listas blancas que la búsqueda web del servidor. Este módulo es lógica pura + DB; la
autenticación, el rate limit y el tope de body viven en api/oficina_routes.py.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from app import runtime
from app.clock import utcnow
from app.config import get_settings
from app.db.models import MarketPriceSnapshot, MlWebResult
from app.db.session import engine
from app.pricing import market_query
from app.pricing.market_ml import (
    ORIGIN_OFICINA,
    MlCandidate,
    safe_image_url,
    safe_permalink,
)
from app.pricing.market_ml_web import MAX_PRICE_CENTS, canonical_permalink, clean_line, valid_ref
from app.security import weak_key_reason

log = logging.getLogger(__name__)

ST_OK, ST_EMPTY, ST_BLOCKED, ST_ERROR = "ok", "empty", "blocked", "error"
STATUSES = (ST_OK, ST_EMPTY, ST_BLOCKED, ST_ERROR)
# Los que sirven como resultado: la Mac buscó y ML contestó (con o sin publicaciones).
RESULT_STATUSES = (ST_OK, ST_EMPTY)

# Topes. El body entero y los productos por lote los pone la ruta (MAX_*); acá los de cada campo.
MAX_BODY_BYTES = 512 * 1024
MAX_PRODUCTS_PER_BATCH = 50
MAX_QUEUE = 500
HARD_MAX_CANDIDATES = 24           # el máximo de `pm_ml_web_max_results`
KEEP_RESULTS_PER_PRODUCT = 5       # historial por producto (lo más viejo se poda al guardar)
FUTURE_SKEW = timedelta(minutes=5)
# La cola vuelve a pedir un producto UN DÍA ANTES de que su resultado venza: si no, el semáforo
# de esa noche (03:00 ART) lo encontraría vencido y el producto perdería el dato un día por semana.
QUEUE_REFRESH_MARGIN = timedelta(days=1)
MIN_REFRESH_AGE = timedelta(hours=12)

_PRODUCT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$", re.ASCII)
_CURRENCY = re.compile(r"^[A-Za-z]{3}$", re.ASCII)
_DOMAIN_ID = re.compile(r"^[A-Za-z0-9_-]{1,60}$", re.ASCII)
_MAX_SOLD = 10**9

# ml_status de la última medición que justifican buscar de nuevo (el semáforo no tuvo un IGUAL con precio).
_NEEDS_SEARCH = ("no_data", "failed")


# ─── La key ────────────────────────────────────────────────────────


def configured_key() -> str | None:
    """OFICINA_SEARCH_KEY si está y es una key de verdad (>= 24 caracteres, no un placeholder)."""
    key = (get_settings().oficina_search_key or "").strip()
    if not key:
        return None
    reason = weak_key_reason(key)
    if reason:
        log.error("OFICINA_SEARCH_KEY %s: el buscador de la oficina queda apagado", reason)
        return None
    return key


def ttl() -> timedelta:
    return timedelta(days=int(get_settings().oficina_result_ttl_days))


# ─── Saneo de lo que manda la Mac ──────────────────────────────────


def _int(value: object, *, low: int, high: int) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")) or not value.is_integer():
            return None
        value = int(value)
    if not isinstance(value, int) or not (low <= value <= high):
        return None
    return value


def sanitize_candidate(raw: object) -> MlCandidate | None:
    """Una publicación que mandó la Mac, ya saneada, o None si no sirve (id raro, sin título).
    Cada campo pasa por la lista blanca que corresponde; lo que no se puede arreglar se descarta."""
    if not isinstance(raw, dict):
        return None
    ref = raw.get("id")
    name = clean_line(raw.get("name"), 200)
    if not valid_ref(ref) or not name:
        return None
    ref = str(ref)
    images = raw.get("image_urls")
    safe_images = [u for u in (safe_image_url(x) for x in (images[:3] if isinstance(images, list) else [])) if u]
    price = _int(raw.get("price_cents"), low=1, high=MAX_PRICE_CENTS - 1)
    currency_raw = raw.get("currency")
    currency: str | None = None
    if isinstance(currency_raw, str) and currency_raw.strip():
        if _CURRENCY.fullmatch(currency_raw.strip()):
            currency = currency_raw.strip().upper()
        else:
            price = None        # una moneda que no se entiende no puede contar como pesos
    catalog = raw.get("catalog_id")
    domain = raw.get("domain_id")
    return MlCandidate(
        id=ref,
        name=name,
        image_urls=safe_images[:1],
        # Un link que no es de ML se reemplaza por el canónico del id (que ya está validado).
        permalink=safe_permalink(raw.get("permalink")) or canonical_permalink(ref),
        domain_id=domain if isinstance(domain, str) and _DOMAIN_ID.fullmatch(domain) else "",
        origin=ORIGIN_OFICINA,
        price_cents=price,
        currency=currency if price is not None else None,
        brand=clean_line(raw.get("brand"), 40),
        seller=clean_line(raw.get("seller"), 60),
        sold_quantity=_int(raw.get("sold_quantity"), low=0, high=_MAX_SOLD),
        catalog_id=str(catalog) if valid_ref(catalog) else "",
    )


def candidate_to_wire(c: MlCandidate) -> dict[str, Any]:
    """La forma en que la Mac manda una publicación (y en que Hugo la guarda)."""
    return {
        "id": c.id, "name": c.name, "image_urls": list(c.image_urls), "permalink": c.permalink,
        "domain_id": c.domain_id, "price_cents": c.price_cents, "currency": c.currency,
        "brand": c.brand, "seller": c.seller, "sold_quantity": c.sold_quantity, "catalog_id": c.catalog_id,
    }


def sanitize_candidates(raw: object, limit: int) -> list[MlCandidate]:
    """Hasta `limit` publicaciones válidas, sin repetir id, en el orden en que llegaron (el de ML)."""
    out: list[MlCandidate] = []
    seen: set[str] = set()
    for item in raw[:HARD_MAX_CANDIDATES * 4] if isinstance(raw, list) else []:
        cand = sanitize_candidate(item)
        if cand is None or cand.id in seen:
            continue
        seen.add(cand.id)
        out.append(cand)
        if len(out) >= limit:
            break
    return out


def parse_fetched_at(value: object, now: datetime) -> datetime | None:
    """ISO 8601 (con o sin zona; sin zona = UTC) → UTC naive a los segundos. None si no se
    entiende o está en el futuro (más allá del desvío de reloj tolerado)."""
    if not isinstance(value, str) or not (10 <= len(value.strip()) <= 40):
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    parsed = parsed.replace(microsecond=0)
    if parsed > now + FUTURE_SKEW or parsed.year < 2024:
        return None
    return parsed


@dataclass(slots=True)
class CleanResult:
    product_id: str
    query: str
    fetched_at: datetime
    status: str
    reason: str
    candidates: list[MlCandidate] = field(default_factory=list)


def clean_result(raw: object, *, now: datetime, max_candidates: int) -> CleanResult | str:
    """Un resultado de la Mac, saneado, o el motivo (str) por el que se rechaza."""
    if not isinstance(raw, dict):
        return "no es un objeto"
    product_id = raw.get("product_id")
    if not isinstance(product_id, str) or not _PRODUCT_ID.fullmatch(product_id):
        return "product_id inválido"
    status = raw.get("status")
    if status not in STATUSES:
        return "status inválido"
    fetched_at = parse_fetched_at(raw.get("fetched_at"), now)
    if fetched_at is None:
        return "fetched_at inválido o en el futuro"
    query = clean_line(raw.get("query"), 200)
    if not query:
        return "falta la consulta"
    reason = clean_line(raw.get("reason"), 300)
    candidates: list[MlCandidate] = []
    if status == ST_OK:
        candidates = sanitize_candidates(raw.get("candidates"), max_candidates)
        if not candidates:
            # La Mac dijo que había resultados y ninguno sirve: no es un "ok" que pueda decidir nada.
            status, reason = ST_EMPTY, reason or "ningún resultado pasó el saneo"
    return CleanResult(product_id, query, fetched_at, status, reason, candidates)


# ─── Guardar ───────────────────────────────────────────────────────


@dataclass(slots=True)
class IngestReport:
    received: int = 0
    stored: int = 0
    duplicates: int = 0
    rejected: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"received": self.received, "stored": self.stored, "duplicates": self.duplicates,
                "rejected": [{"product_id": p, "reason": r} for p, r in self.rejected[:20]],
                "rejected_total": len(self.rejected)}


def _max_candidates() -> int:
    return max(1, min(int(runtime.get("pm_ml_web_max_results") or 8), HARD_MAX_CANDIDATES))


def _row(r: CleanResult) -> MlWebResult:
    return MlWebResult(
        product_id=r.product_id, query=r.query, fetched_at=r.fetched_at, origin=ORIGIN_OFICINA,
        candidates=json.dumps([candidate_to_wire(c) for c in r.candidates], ensure_ascii=False),
        n_candidates=len(r.candidates), status=r.status, reason=r.reason or None)


def ingest(items: list[Any], *, now: datetime | None = None) -> IngestReport:
    """Re-sanea y guarda un lote. Idempotente por (product_id, fetched_at): mandar dos veces el
    mismo resultado no duplica nada. Un resultado malo se rechaza solo, sin tirar el lote."""
    now = now or utcnow()
    report = IngestReport(received=len(items))
    limit = _max_candidates()
    cleaned: list[CleanResult] = []
    for raw in items:
        res = clean_result(raw, now=now, max_candidates=limit)
        if isinstance(res, str):
            pid = clean_line(raw.get("product_id"), 64) if isinstance(raw, dict) else ""
            report.rejected.append((pid, res))
        else:
            cleaned.append(res)
    if not cleaned:
        return report

    ids = sorted({r.product_id for r in cleaned})
    with Session(engine) as s:
        known = set(s.exec(
            select(MarketPriceSnapshot.product_id).where(MarketPriceSnapshot.product_id.in_(ids)).distinct()  # type: ignore[attr-defined]
        ).all())
        taken = set(s.exec(
            select(MlWebResult.product_id, MlWebResult.fetched_at).where(MlWebResult.product_id.in_(ids))  # type: ignore[attr-defined]
        ).all())
        fresh: list[CleanResult] = []
        for r in cleaned:
            key = (r.product_id, r.fetched_at)
            if r.product_id not in known:
                report.rejected.append((r.product_id, "producto desconocido"))
            elif key in taken:
                report.duplicates += 1
            else:
                taken.add(key)
                fresh.append(r)
        _insert(s, fresh, report)
        _prune(s, {r.product_id for r in fresh})
    for r in cleaned:
        if r.status == ST_BLOCKED:
            log.warning("oficina: la Mac reportó que ML bloqueó la búsqueda (producto %s)", r.product_id)
    return report


def _insert(s: Session, fresh: list[CleanResult], report: IngestReport) -> None:
    """Guarda los resultados nuevos. Si otro lote se coló entre el chequeo de repetidos y el guardado (el
    índice único lo frena), se guarda de a uno y se cuenta como repetido lo que ya estaba."""
    for r in fresh:
        s.add(_row(r))
    try:
        s.commit()
        report.stored += len(fresh)
    except IntegrityError:
        s.rollback()
        for r in fresh:
            try:
                s.add(_row(r))
                s.commit()
                report.stored += 1
            except IntegrityError:
                s.rollback()
                report.duplicates += 1


def _prune(s: Session, product_ids: set[str]) -> None:
    """Se conservan los últimos KEEP_RESULTS_PER_PRODUCT resultados de cada producto tocado."""
    for pid in product_ids:
        ids = s.exec(
            select(MlWebResult.id).where(MlWebResult.product_id == pid)
            .order_by(MlWebResult.fetched_at.desc(), MlWebResult.id.desc())  # type: ignore[union-attr]
        ).all()
        for old in ids[KEEP_RESULTS_PER_PRODUCT:]:
            row = s.get(MlWebResult, old)
            if row is not None:
                s.delete(row)
    s.commit()


# ─── La cola ───────────────────────────────────────────────────────


def build_queue(limit: int, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Qué buscar esta noche, en este orden:

      1. productos sin IDÉNTICO con precio en su última medición (`no_data` o `failed`) que la oficina
         todavía no buscó (habilitados antes que deshabilitados);
      2. los que ya tienen resultado de la oficina pero se está por vencer (el más viejo primero): los
         que siguen sin idéntico y los que hoy tienen precio gracias a la oficina.

    Cada item lleva solo el id del producto y las consultas (las mismas variantes de la API de ML): ni
    costos ni proveedor ni nada interno."""
    now = now or utcnow()
    variants = market_query.clamp_variants(runtime.get("pm_ml_query_variants"))
    refresh_after = max(ttl() - QUEUE_REFRESH_MARGIN, MIN_REFRESH_AGE)
    stale_before = now - refresh_after
    with Session(engine) as s:
        newest = select(func.max(MarketPriceSnapshot.id)).group_by(MarketPriceSnapshot.product_id)
        snaps = s.exec(
            select(MarketPriceSnapshot.product_id, MarketPriceSnapshot.ml_status, MarketPriceSnapshot.match_origin,
                   MarketPriceSnapshot.product_name, MarketPriceSnapshot.product_enabled)
            .where(MarketPriceSnapshot.id.in_(newest))  # type: ignore[union-attr]
        ).all()
        last_search = dict(s.exec(
            select(MlWebResult.product_id, func.max(MlWebResult.fetched_at))
            .where(MlWebResult.status.in_(RESULT_STATUSES))  # type: ignore[attr-defined]
            .group_by(MlWebResult.product_id)
        ).all())

    never: list[tuple[Any, ...]] = []
    expiring: list[tuple[Any, ...]] = []
    for pid, ml_status, origin, name, enabled in snaps:
        if not name:
            continue
        by_oficina = ml_status == "ok" and origin == ORIGIN_OFICINA
        if ml_status not in _NEEDS_SEARCH and not by_oficina:
            continue
        fetched = last_search.get(pid)
        if fetched is None:
            if not by_oficina:
                never.append((enabled is False, _sort_id(pid), pid, name))
        elif fetched <= stale_before:
            expiring.append((fetched, _sort_id(pid), pid, name))
    never.sort(key=lambda t: t[:2])
    expiring.sort(key=lambda t: t[:2])

    out: list[dict[str, Any]] = []
    for *_, pid, name in [*never, *expiring]:
        queries = market_query.query_variants(name, variants)
        if queries:
            out.append({"product_id": pid, "queries": queries})
        if len(out) >= limit:
            break
    return out


def _sort_id(pid: str) -> tuple[int, str]:
    return (0, f"{int(pid):020d}") if pid.isdigit() else (1, pid)


# ─── Lo fresco, para el semáforo ───────────────────────────────────


@dataclass(slots=True)
class FreshResult:
    product_id: str
    status: str
    query: str
    fetched_at: datetime
    candidates: list[MlCandidate]


def load_fresh(*, now: datetime | None = None) -> dict[str, FreshResult]:
    """El último resultado (ok o empty) de cada producto con menos de OFICINA_RESULT_TTL_DAYS. Los
    candidatos se vuelven a sanear al leerlos: la tabla no es de fiar más que la entrada."""
    now = now or utcnow()
    limit = _max_candidates()
    out: dict[str, FreshResult] = {}
    with Session(engine) as s:
        rows = s.exec(
            select(MlWebResult)
            .where(MlWebResult.status.in_(RESULT_STATUSES), MlWebResult.fetched_at >= now - ttl())  # type: ignore[attr-defined]
            .order_by(MlWebResult.fetched_at.desc(), MlWebResult.id.desc())  # type: ignore[union-attr]
        ).all()
    for r in rows:
        if r.product_id in out:
            continue
        try:
            raw = json.loads(r.candidates or "[]")
        except ValueError:
            raw = []
        cands = sanitize_candidates(raw, limit) if r.status == ST_OK else []
        out[r.product_id] = FreshResult(r.product_id, ST_OK if cands else ST_EMPTY, r.query, r.fetched_at, cands)
    return out


def status() -> dict[str, Any]:
    """Para la card de Salud: si está prendido, qué tan fresco está y cómo le fue a la Mac en el último día."""
    now = utcnow()
    with Session(engine) as s:
        last = s.exec(select(func.max(MlWebResult.received_at))).one()
        recent = dict(s.exec(
            select(MlWebResult.status, func.count(MlWebResult.id))  # type: ignore[arg-type]
            .where(MlWebResult.received_at >= now - timedelta(days=1)).group_by(MlWebResult.status)
        ).all())
        fresh = s.exec(
            select(func.count(func.distinct(MlWebResult.product_id)))
            .where(MlWebResult.status.in_(RESULT_STATUSES), MlWebResult.fetched_at >= now - ttl())  # type: ignore[attr-defined]
        ).one()
    return {
        "enabled": configured_key() is not None,
        "ttl_days": int(get_settings().oficina_result_ttl_days),
        "fresh_products": int(fresh or 0),
        "last_received_at": last.isoformat() + "Z" if last else None,
        "last_24h": {k: int(recent.get(k, 0)) for k in STATUSES},
    }
