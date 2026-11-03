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

Desde el 21-oct la misma cola la toma también un job de Hugo que busca con un actor de Apify
(pricing/apify_ml_search.py) y guarda por `ingest` con origen `apify`: el semáforo usa los dos igual. Para que
la Mac y Apify no busquen (y paguen) el mismo producto, quien lo toma lo RESERVA (`claim`, tabla `ml_web_claim`)
y la cola no se lo ofrece al otro mientras la reserva esté viva.

Hugo no confía en la Mac: lo que llega es texto de terceros (viene de la página de ML) pasando por una
máquina y un canal que no controlamos, así que se vuelve a sanear campo por campo (ids, links, fotos, precios,
títulos) con las mismas listas blancas que la búsqueda web del servidor. Este módulo es lógica pura + DB; la
autenticación, el rate limit y el tope de body viven en api/oficina_routes.py.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, delete, or_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from app import runtime
from app.clock import utcnow
from app.config import get_settings
from app.db.models import MarketPriceSnapshot, MlLensResult, MlWebClaim, MlWebResult, PriceMonitorRun
from app.db.session import engine
from app.pricing import market_query, semaforo
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
# Origen de las búsquedas de Apify con una consulta de Google Lens (pricing/ml_lens.py): el semáforo las usa igual y
# marca el snapshot con ml_variant="lens" para medir cuántos IGUAL ganó Lens.
ORIGIN_LENS = "lens"
MAX_LENS_QUERY_CHARS = 120
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
# UNA página de ML por producto por noche: si la consulta vino vacía, la siguiente variante se prueba recién la noche
# siguiente (pasadas al menos estas horas), para no multiplicar las búsquedas desde la IP de la oficina.
NEXT_VARIANT_AFTER = timedelta(hours=12)
# Los rojos se re-buscan cuando su dato pasa `pm_review_data_max_age_h` - 1 día (piso: 1 día), con media noche de
# tolerancia: Apify no busca cada producto a la misma hora (fetched_at es el final de cada búsqueda y la corrida dura
# hasta 2 h), y sin esto a "las 48 h" el dato tiene 47 h 55 min y se re-busca una noche tarde.
RED_REFRESH_SLACK = timedelta(hours=12)
# Un rojo con un intento de hace menos de esto (de cualquier estado: error y bloqueo también) no se re-busca: lo que
# falló anoche no se vuelve a pagar hasta la noche siguiente.
RED_RETRY_AFTER = timedelta(hours=20)
# Si la última corrida terminada es más vieja que esto (el semáforo está parado), sus rojos ya no se re-buscan.
RED_RUN_MAX_AGE = timedelta(days=2)
# Los límites de `pm_review_data_max_age_h` en runtime.py: un valor fuera de rango metido a mano en la tabla daría
# OverflowError y tiraría la cola entera.
_DATA_MAX_AGE_H = (6, 168)
# Corridas del semáforo que terminaron (price_monitor.RUN_OK / RUN_DEGRADED; importarlo sería circular). Una `failed`
# reventó a mitad y una `skipped` no midió nada: sus rojos no son todos los rojos.
_RUN_FINISHED = ("ok", "degraded")
# Un producto cuyo último snapshot es más viejo que esto (respecto del snapshot más nuevo de todos) ya no se mide: borrado de
# Vendure, deshabilitado con `pm_include_disabled=0`. Buscarlo sería gastar una página de ML en un resultado que nadie usa.
SNAPSHOT_MAX_AGE = timedelta(days=3)
# Los resultados se purgan por tiempo (nunca antes de que venza el TTL, con margen).
RESULT_RETENTION = timedelta(days=30)

# Quién reserva productos de la cola (ver `claim`). La reserva de la Mac es CORTA y DESLIZANTE: 90 min desde que pide
# la cola, y cada lote que manda la renueva (manda uno cada ~3 min mientras busca). Un GET repetido NO la estira. Lo
# que reporta se suelta; si reporta un bloqueo, o al terminar (POST /ml-queue/release), se sueltan todas.
CLAIM_OFICINA = "oficina"
OFICINA_CLAIM_LEASE = timedelta(minutes=90)
# Una reserva de la Mac con más de esto y sin ningún lote de la Mac en ese tiempo es de una Mac que se colgó o se
# apagó sin avisar: Apify la puede tomar aunque no haya vencido.
OFICINA_STALE_AFTER = timedelta(minutes=45)
_CLAIMER = re.compile(r"^[a-z0-9-]{1,24}$", re.ASCII)

# Una key de `secrets.token_urlsafe(32)` tiene ~30 caracteres distintos; esto frena "aaaa…" o "abcabcabc…".
MIN_KEY_DISTINCT_CHARS = 12

_PRODUCT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$", re.ASCII)
_CURRENCY = re.compile(r"^[A-Za-z]{3}$", re.ASCII)
_DOMAIN_ID = re.compile(r"^[A-Za-z0-9_-]{1,60}$", re.ASCII)
_MAX_SOLD = 10**9

# ml_status de la última medición que justifican buscar de nuevo (el semáforo no tuvo un IGUAL con precio).
_NEEDS_SEARCH = ("no_data", "failed")


# ─── La key ────────────────────────────────────────────────────────


# Una key de `secrets.token_urlsafe(32)` mide 43 caracteres. Se exigen 32 (~190 bits si es aleatoria): es lo que hace inútil la
# fuerza bruta, porque el bloqueo por IP frena fallos pero no a quien trae la key buena (ver api/oficina_routes.guard). Usar la que
# genera `oficina_ml_search.py --init`.
MIN_KEY_LEN = 32
_weak_logged: set[str] = set()


def configured_key() -> str | None:
    """OFICINA_SEARCH_KEY si está y es una key de verdad (>= 32 caracteres, >= 12 distintos, no un placeholder). Una key floja
    deja el buscador apagado y se avisa UNA vez (no en cada request)."""
    key = (get_settings().oficina_search_key or "").strip()
    if not key:
        return None
    reason = weak_key_reason(key, min_distinct=MIN_KEY_DISTINCT_CHARS)
    if reason is None and len(key) < MIN_KEY_LEN:
        reason = f"tiene menos de {MIN_KEY_LEN} caracteres (usá la que genera `oficina_ml_search.py --init`)"
    if reason:
        if reason not in _weak_logged:
            _weak_logged.add(reason)
            log.error("OFICINA_SEARCH_KEY %s: el buscador de la oficina queda apagado", reason)
        return None
    return key


def ttl() -> timedelta:
    return timedelta(days=int(runtime.get("oficina_result_ttl_days")))


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


def _row(r: CleanResult, origin: str = ORIGIN_OFICINA) -> MlWebResult:
    return MlWebResult(
        product_id=r.product_id, query=r.query, fetched_at=r.fetched_at, origin=origin,
        candidates=json.dumps([candidate_to_wire(c) for c in r.candidates], ensure_ascii=False),
        n_candidates=len(r.candidates), status=r.status, reason=r.reason or None)


def ingest(items: list[Any], *, now: datetime | None = None, origin: str = ORIGIN_OFICINA) -> IngestReport:
    """Re-sanea y guarda un lote. Idempotente por (product_id, fetched_at): mandar dos veces el
    mismo resultado no duplica nada. Un resultado malo se rechaza solo, sin tirar el lote.

    `origin`: quién buscó (`oficina` = la Mac, por la API; `apify` = el job de Hugo). Solo queda en la
    fila para auditar: el saneo y la forma de lo guardado son los mismos."""
    now = now or utcnow()
    report = IngestReport(received=len(items))
    limit = _max_candidates()
    cleaned: list[CleanResult] = []
    for raw in items:
        try:
            res = clean_result(raw, now=now, max_candidates=limit)
        except Exception as exc:  # noqa: BLE001  (un resultado raro no puede tirar el lote entero)
            log.warning("oficina: no se pudo procesar un resultado (%s)", type(exc).__name__)
            res = "no se pudo procesar"
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
        _insert(s, fresh, report, origin)
        _prune(s, {r.product_id for r in fresh}, now)
    if origin == ORIGIN_OFICINA:
        _release_after_oficina(cleaned)
    return report


def _release_after_oficina(cleaned: list[CleanResult]) -> None:
    """La Mac ya terminó con los productos que reportó: se sueltan sus reservas. Si reportó un bloqueo,
    la noche de la Mac terminó (no reintenta): se sueltan TODAS, así Apify toma el resto esa misma noche."""
    blocked = [r.product_id for r in cleaned if r.status == ST_BLOCKED]
    for pid in blocked:
        log.warning("oficina: la Mac reportó que ML bloqueó la búsqueda (producto %s)", pid)
    release(CLAIM_OFICINA, None if blocked else [r.product_id for r in cleaned])
    if not blocked:
        renew(CLAIM_OFICINA, OFICINA_CLAIM_LEASE)       # la Mac sigue viva: sus reservas restantes se estiran


def _insert(s: Session, fresh: list[CleanResult], report: IngestReport, origin: str = ORIGIN_OFICINA) -> None:
    """Guarda los resultados nuevos. Si otro lote se coló entre el chequeo de repetidos y el guardado (el
    índice único lo frena), se guarda de a uno y se cuenta como repetido lo que ya estaba."""
    for r in fresh:
        s.add(_row(r, origin))
    try:
        s.commit()
        report.stored += len(fresh)
    except IntegrityError:
        s.rollback()
        for r in fresh:
            try:
                s.add(_row(r, origin))
                s.commit()
                report.stored += 1
            except IntegrityError:
                s.rollback()
                report.duplicates += 1


def _prune(s: Session, product_ids: set[str], now: datetime) -> None:
    """Retención. Por producto se conservan los últimos KEEP_RESULTS_PER_PRODUCT resultados (ok / empty) Y, aparte, los
    últimos KEEP_RESULTS_PER_PRODUCT bloqueos / errores: una racha de bloqueos no puede desplazar el último resultado
    bueno que todavía está dentro del TTL. Además se purga por tiempo todo lo más viejo que la retención."""
    for pid in product_ids:
        rows = s.exec(
            select(MlWebResult.id, MlWebResult.status).where(MlWebResult.product_id == pid)
            .order_by(MlWebResult.fetched_at.desc(), MlWebResult.id.desc())  # type: ignore[union-attr]
        ).all()
        kept = {"result": 0, "failure": 0}
        for row_id, status in rows:
            bucket = "result" if status in RESULT_STATUSES else "failure"
            kept[bucket] += 1
            if kept[bucket] > KEEP_RESULTS_PER_PRODUCT:
                row = s.get(MlWebResult, row_id)
                if row is not None:
                    s.delete(row)
    horizon = now - max(RESULT_RETENTION, ttl() + timedelta(days=7))
    for old in s.exec(select(MlWebResult).where(MlWebResult.fetched_at < horizon)).all():  # type: ignore[arg-type]
        s.delete(old)
    s.commit()


# ─── La cola ───────────────────────────────────────────────────────


def build_queue(limit: int, *, now: datetime | None = None, claimer: str | None = None,
                take_stale_oficina: bool = False, with_lens: bool = False) -> list[dict[str, Any]]:
    """Qué buscar esta noche, en este orden:

      1. los ROJOS de la última corrida terminada (de hace 2 días o menos) cuyo precio salió de la oficina y cuyo
         resultado vigente es `ok` y tiene más de `pm_review_data_max_age_h` - 1 día (piso: 1 día; el más viejo primero),
         con la MISMA consulta de ese resultado: la bandeja (R5) y el recálculo solo usan dato de menos de
         `pm_review_data_max_age_h`, y el TTL de 15 días los dejaba sin dato fresco al tercer día. Con 72 h, cada rojo se
         re-busca cada 2 noches. A lo sumo la MITAD de `limit` (el resto es para los otros grupos); no entra el que tuvo
         un intento de cualquier estado hace menos de 20 h (un error no se re-paga esa noche) y la Mac NUNCA recibe este
         grupo (nada de tráfico a ML desde la oficina por esto);
      2. productos sin IDÉNTICO con precio en su última medición (`no_data` o `failed`) que la oficina
         todavía no buscó (habilitados antes que deshabilitados; los deshabilitados se miden por pedido de Nico y
         se buscan también);
      3. los que la oficina buscó con una consulta que vino VACÍA y todavía tienen otra variante por probar (la
         siguiente, la noche siguiente);
      4. los que ya tienen resultado de la oficina pero se está por vencer (el más viejo primero): los que siguen sin
         idéntico empiezan de nuevo por la primera variante; los que hoy tienen precio gracias a la oficina se
         refrescan con la consulta que lo ganó (con la primera podrían perderlo).

    Con `with_lens` (solo Apify, si `pm_lens_enabled`; la Mac nunca recibe consultas Lens) el orden queda: rojos →
    nunca buscados → siguiente variante de texto → refresco de los que tienen precio → **lens** → refresco de los sin
    dato. El grupo lens son los sin IDÉNTICO con una consulta de Google Lens todavía sin buscar (ver pricing/ml_lens.py),
    pasadas NEXT_VARIANT_AFTER desde su último resultado y sin variante de texto pendiente; el item lleva `"lens": True`.
    Lens solo desplaza el refresco de los sin dato, que repetiría la primera variante y daría lo mismo.

    Un producto sale UNA vez: si salió como rojo (1) no se repite en los otros grupos.

    **Cada item lleva UNA sola consulta**: una página de ML por producto por noche como tope. Y solo el id del producto:
    ni costos ni proveedor ni nada interno. Los productos cuyo último snapshot es viejo (ya no se miden) no entran.

    Tampoco entran los que tiene reservados OTRO (`claim` vigente de alguien que no es `claimer`; sin `claimer`,
    de cualquiera). Armar la cola no reserva nada: eso lo hace quien la va a buscar. Con `take_stale_oficina` (Apify)
    las reservas de una Mac colgada (ver OFICINA_STALE_AFTER) no cuentan."""
    now = now or utcnow()
    variants = market_query.clamp_variants(runtime.get("pm_ml_query_variants"))
    refresh_after = max(ttl() - QUEUE_REFRESH_MARGIN, MIN_REFRESH_AGE)
    stale_before = now - refresh_after
    max_age_h = min(max(int(runtime.get("pm_review_data_max_age_h")), _DATA_MAX_AGE_H[0]), _DATA_MAX_AGE_H[1])
    red_after = max(timedelta(hours=max_age_h) - timedelta(days=1), timedelta(days=1))
    red_before = now - red_after + RED_REFRESH_SLACK
    with Session(engine) as s:
        newest = select(func.max(MarketPriceSnapshot.id)).group_by(MarketPriceSnapshot.product_id)
        snaps = s.exec(
            select(MarketPriceSnapshot.product_id, MarketPriceSnapshot.ml_status, MarketPriceSnapshot.match_origin,
                   MarketPriceSnapshot.product_name, MarketPriceSnapshot.product_enabled, MarketPriceSnapshot.captured_at)
            .where(MarketPriceSnapshot.id.in_(newest))  # type: ignore[union-attr]
        ).all()
        # Todos los estados: los bloqueos y errores cuentan como intento para los rojos (RED_RETRY_AFTER).
        results = s.exec(
            select(MlWebResult.product_id, MlWebResult.fetched_at, MlWebResult.status, MlWebResult.query)
        ).all()
        reds: set[str] = set()
        if claimer != CLAIM_OFICINA:
            # La última corrida terminada, si terminó hace RED_RUN_MAX_AGE o menos (las corridas no se pisan: la de id
            # más alto es la última en terminar). Si es más vieja, la subconsulta da NULL y no hay rojos.
            last_run = (select(func.max(PriceMonitorRun.id))
                        .where(PriceMonitorRun.status.in_(_RUN_FINISHED),  # type: ignore[attr-defined]
                               PriceMonitorRun.finished_at >= now - RED_RUN_MAX_AGE)  # type: ignore[operator]
                        .scalar_subquery())
            # Solo con precio por la oficina: el dato de la API o de la web del servidor es de la hora de la corrida.
            reds = set(s.exec(
                select(MarketPriceSnapshot.product_id)
                .where(MarketPriceSnapshot.run_id == last_run, MarketPriceSnapshot.color == semaforo.ROJO,
                       MarketPriceSnapshot.match_origin == ORIGIN_OFICINA)
            ).all())
        taken_by_others = _claimed_by_others(s, claimer, now, take_stale_oficina)
        lens_rows = s.exec(
            select(MlLensResult.product_id, MlLensResult.ran_at, MlLensResult.queries).where(MlLensResult.status == "ok")
        ).all() if with_lens else []
    last_result: dict[str, tuple[datetime, str, str]] = {}
    last_try: dict[str, datetime] = {}
    for pid, fetched, status, query in results:
        if pid not in last_try or fetched > last_try[pid]:
            last_try[pid] = fetched
        if status in RESULT_STATUSES and (pid not in last_result or fetched > last_result[pid][0]):
            last_result[pid] = (fetched, status, query)
    lens_pending = _lens_pending(lens_rows, results)
    still_measured = max((row[5] for row in snaps), default=now) - SNAPSHOT_MAX_AGE

    red: list[tuple[Any, ...]] = []
    never: list[tuple[Any, ...]] = []
    next_variant: list[tuple[Any, ...]] = []
    expiring_priced: list[tuple[Any, ...]] = []
    lens: list[tuple[Any, ...]] = []
    expiring_rest: list[tuple[Any, ...]] = []
    for pid, ml_status, origin, name, enabled, captured in snaps:
        if not name or captured < still_measured or pid in taken_by_others:
            continue
        last = last_result.get(pid)
        if (pid in reds and last is not None and last[1] == ST_OK and last[0] <= red_before
                and last_try[pid] <= now - RED_RETRY_AFTER):
            red.append((last[0], _sort_id(pid), pid, last[2]))
        by_oficina = ml_status == "ok" and origin == ORIGIN_OFICINA
        needs_search = ml_status in _NEEDS_SEARCH
        if not needs_search and not by_oficina:
            continue
        plan = market_query.query_variants(name, variants)
        if not plan:
            continue
        if last is None:
            if needs_search:
                never.append((enabled is False, _sort_id(pid), pid, plan[0]))
            continue
        fetched, status, used = last
        next_text = _next_text(plan, used) if needs_search and status == ST_EMPTY else None
        pending = lens_pending.get(pid) if needs_search else None
        waited = fetched <= now - NEXT_VARIANT_AFTER
        if pending and next_text is None and waited:
            lens.append((fetched, _sort_id(pid), pid, pending[0]))
        elif fetched <= stale_before:
            if by_oficina:
                # Con la consulta que lo ganó; la Mac, solo si es de texto (nunca recibe una consulta de Google Lens).
                text = claimer != CLAIM_OFICINA or market_query._key(used) in {market_query._key(q) for q in plan}
                expiring_priced.append((fetched, _sort_id(pid), pid, used if used and text else plan[0]))
            else:
                expiring_rest.append((fetched, _sort_id(pid), pid, plan[0]))
        elif next_text is not None and waited:
            next_variant.append((fetched, _sort_id(pid), pid, next_text))
    for group in (red, never, next_variant, expiring_priced, lens, expiring_rest):
        group.sort(key=lambda t: t[:2])
    # Los rojos, a lo sumo la mitad: con 71 rojos viejos se comerían el cupo de Apify la primera noche. Los que no
    # entran siguen en su grupo de siempre (si les toca) y vuelven mañana.
    red = red[:max(limit // 2, 1)]
    chosen = {t[2] for t in red}

    if with_lens:
        rest = [*never, *next_variant, *expiring_priced, *lens, *expiring_rest]
    else:       # la cola de siempre: los que vencen, con y sin precio, mezclados por antigüedad
        rest = [*never, *next_variant, *sorted([*expiring_priced, *expiring_rest], key=lambda t: t[:2])]
    lens_ids = {t[2] for t in lens}
    return [{"product_id": pid, "queries": [query], **({"lens": True} if pid in lens_ids else {})}
            for *_, pid, query in [*red, *(t for t in rest if t[2] not in chosen)]][:limit]


def _next_text(plan: list[str], used: str) -> str | None:
    """La variante de texto que sigue a `used` en el plan, o None (se probaron todas o `used` no es del plan)."""
    keys = [market_query._key(q) for q in plan]
    at = keys.index(market_query._key(used)) if market_query._key(used) in keys else len(plan)
    return plan[at + 1] if at + 1 < len(plan) else None


def _lens_pending(lens_rows: Sequence[Any], results: Sequence[Any]) -> dict[str, list[str]]:
    """Por producto, las consultas Lens que todavía no tienen un resultado ok / empty de después de lensear. Un
    `error` no cuenta como buscada: se vuelve a ofrecer."""
    if not lens_rows:
        return {}
    searched: dict[str, list[tuple[datetime, str]]] = {}
    for pid, fetched, status, query in results:
        if status in RESULT_STATUSES:
            searched.setdefault(pid, []).append((fetched, market_query._key(query)))
    out: dict[str, list[str]] = {}
    for pid, ran_at, raw in lens_rows:
        used = {key for fetched, key in searched.get(pid, ()) if fetched >= ran_at}
        pending = [q for q in lens_queries(raw) if market_query._key(q) not in used]
        if pending:
            out[pid] = pending
    return out


def clean_query(text: object) -> str:
    """Una consulta de Google Lens: solo letras, números y espacios, hasta 120 caracteres y con al menos 2 palabras; ""
    si no. Sale de un slug de un tercero y llega al actor de Apify y al prompt del juez. La usa ml_lens al escribir y
    la cola al leer (la tabla no es de fiar más que la entrada)."""
    if not isinstance(text, str):
        return ""
    text = "".join(c if c.isalnum() else " " for c in text[: MAX_LENS_QUERY_CHARS * 2])
    text = " ".join(text.split())[:MAX_LENS_QUERY_CHARS].strip()
    return text if len(text.split()) >= 2 else ""


def lens_queries(raw: str | None) -> list[str]:
    """Las consultas de `ml_lens_result.queries`, re-saneadas con la misma regla con que se escribieron."""
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    found = [clean_query(q.get("q")) for q in data[:4] if isinstance(q, dict)] if isinstance(data, list) else []
    return [q for q in found if q]


def _sort_id(pid: str) -> tuple[int, str]:
    return (0, f"{int(pid):020d}") if pid.isdigit() else (1, pid)


# ─── Reservas de la cola (la Mac y Apify no buscan lo mismo) ───────


def _stale_oficina(now: datetime):
    """Reserva de la Mac sin señales de vida: tomada hace más de OFICINA_STALE_AFTER y sin lotes desde entonces."""
    t = MlWebClaim.__table__.c
    edge = now - OFICINA_STALE_AFTER
    return and_(t.claimed_by == CLAIM_OFICINA, t.claimed_at <= edge, t.renewed_at <= edge)


def _claimed_by_others(s: Session, claimer: str | None, now: datetime, take_stale_oficina: bool = False) -> set[str]:
    t = MlWebClaim.__table__.c
    stmt = select(t.product_id).where(t.expires_at > now)
    if claimer is not None:
        stmt = stmt.where(t.claimed_by != claimer)
    if take_stale_oficina:
        stmt = stmt.where(~_stale_oficina(now))
    return set(s.exec(stmt).all())


def _insert_stmt():
    return pg_insert if engine.dialect.name == "postgresql" else sqlite_insert


def claim(product_ids: list[str], claimer: str, lease: timedelta, *, now: datetime | None = None,
          renew: bool = True, take_stale_oficina: bool = False) -> list[str]:
    """Reserva para `claimer` los productos libres y devuelve los que quedaron suyos, en el mismo orden.

    Libre = sin reserva, vencida o (con `take_stale_oficina`) de una Mac colgada. UNA sentencia para todo el lote
    (`INSERT … ON CONFLICT DO UPDATE … WHERE libre … RETURNING`, en SQLite y en Postgres): si la Mac y Apify piden lo
    mismo a la vez, la base decide fila por fila y cada producto queda de uno solo.

    Las reservas vivas del propio `claimer` se devuelven como suyas; con `renew=True` además se estiran, con
    `renew=False` (la cola de la Mac pedida de nuevo) quedan como estaban. Si la base falla no se reserva nada (y
    quien llama no busca nada): fail-closed, como el cupo."""
    if not _CLAIMER.fullmatch(claimer or ""):
        raise ValueError("claimer inválido")
    pids = list(dict.fromkeys(product_ids))
    if not pids:
        return []
    now = now or utcnow()
    until = now + lease
    t = MlWebClaim.__table__
    free = t.c.expires_at <= now
    if renew:
        free = or_(free, t.c.claimed_by == claimer)
    if take_stale_oficina:
        free = or_(free, _stale_oficina(now))
    ins = _insert_stmt()(t).values([
        {"product_id": pid, "claimed_by": claimer, "claimed_at": now, "renewed_at": now, "expires_at": until}
        for pid in pids])
    stmt = ins.on_conflict_do_update(
        index_elements=[t.c.product_id],
        set_={"claimed_by": ins.excluded.claimed_by, "claimed_at": ins.excluded.claimed_at,
              "renewed_at": ins.excluded.renewed_at, "expires_at": ins.excluded.expires_at},
        where=free,
    ).returning(t.c.product_id)
    try:
        with Session(engine) as s:
            got = set(s.execute(stmt).scalars().all())
            if not renew:
                got |= set(s.execute(select(t.c.product_id).where(
                    t.c.claimed_by == claimer, t.c.expires_at > now, t.c.product_id.in_(pids))).scalars().all())
            s.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("cola de ML: no se pudieron reservar productos para %s (%s)", claimer, type(exc).__name__)
        return []
    return [pid for pid in pids if pid in got]


def renew(claimer: str, lease: timedelta, *, now: datetime | None = None) -> int:
    """Estira las reservas vivas de `claimer` (la Mac mandó un lote: sigue viva). Nunca lanza."""
    now = now or utcnow()
    try:
        with Session(engine) as s:
            done = s.execute(
                update(MlWebClaim)
                .where(MlWebClaim.claimed_by == claimer, MlWebClaim.expires_at > now)  # type: ignore[arg-type]
                .values(expires_at=now + lease, renewed_at=now))
            s.commit()
            return int(done.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("cola de ML: no se pudieron renovar las reservas de %s (%s)", claimer, type(exc).__name__)
        return 0


def release(claimer: str, product_ids: list[str] | None = None) -> int:
    """Suelta las reservas de `claimer` (todas, o solo esos productos) y dice cuántas. Nunca lanza: si falla, la
    reserva vence sola."""
    if product_ids is not None and not product_ids:
        return 0
    try:
        with Session(engine) as s:
            stmt = delete(MlWebClaim).where(MlWebClaim.claimed_by == claimer)  # type: ignore[arg-type]
            if product_ids is not None:
                stmt = stmt.where(MlWebClaim.product_id.in_(list(product_ids)))  # type: ignore[attr-defined]
            done = s.execute(stmt)
            s.commit()
            return int(done.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("cola de ML: no se pudieron soltar las reservas de %s (%s)", claimer, type(exc).__name__)
        return 0


def queue_for_oficina(limit: int, *, now: datetime | None = None, take: bool = True) -> list[dict[str, Any]]:
    """La cola que se le da a la Mac: lo que nadie más tiene reservado. Con `take` queda reservado para la oficina
    (sin estirar lo que ya tenía: eso lo hacen sus lotes); sin `take` (--check, --dry-run) solo se mira."""
    now = now or utcnow()
    items = build_queue(limit, now=now, claimer=CLAIM_OFICINA)
    if not take:
        return items
    got = set(claim([i["product_id"] for i in items], CLAIM_OFICINA, OFICINA_CLAIM_LEASE, now=now, renew=False))
    return [i for i in items if i["product_id"] in got]


# ─── Lo fresco, para el semáforo ───────────────────────────────────


@dataclass(slots=True)
class FreshResult:
    product_id: str
    status: str
    query: str
    fetched_at: datetime
    candidates: list[MlCandidate]
    # Al final y con default: hay quien lo construye por posición.
    origin: str = ORIGIN_OFICINA


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
        out[r.product_id] = FreshResult(r.product_id, ST_OK if cands else ST_EMPTY, r.query, r.fetched_at, cands,
                                        r.origin or ORIGIN_OFICINA)
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
        "ttl_days": ttl().days,
        "fresh_products": int(fresh or 0),
        "last_received_at": last.isoformat() + "Z" if last else None,
        "last_24h": {k: int(recent.get(k, 0)) for k in STATUSES},
    }
