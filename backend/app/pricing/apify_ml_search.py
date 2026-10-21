"""Búsqueda web de Mercado Libre con un actor de Apify, para el semáforo.

Por qué existe: desde el servidor ML contesta la búsqueda con captcha, y desde el
09-oct la hacía la Mac de la oficina (pricing/oficina_ml.py). Desde el 20-oct ML
bloquea también a la oficina en la primera búsqueda de la noche. El actor
`devcake/mercadolibre-scraper` busca con proxies de Apify y devuelve las
publicaciones del listado.

Qué hace el job (scheduler/jobs.py, de madrugada y antes del semáforo):

  1. toma la MISMA cola que la Mac (`oficina_ml.build_queue`: una consulta por
     producto, lo que el semáforo no pudo medir), sin lo que tiene reservado otro;
  2. de a lotes de `apify_meli_search_concurrency` productos: los reserva en la
     cola (`oficina_ml.claim`, así la Mac no los busca a la vez), reserva el cupo
     del lote ATÓMICO contra `apify_meli_search_daily_max` (dos corridas que se
     pisan no se pasan del tope) y lanza una corrida del actor por producto;
  3. valida cada publicación (id de ML, link de mercadolibre.com.ar, precio
     finito > 0, foto de mlstatic) y guarda por `oficina_ml.ingest` con origen
     `apify`: misma tabla, mismo saneo, misma forma. El semáforo no distingue.

UNA corrida por producto, a propósito: el actor acepta varias consultas por
corrida pero sus publicaciones no dicen qué consulta las trajo, salen mezcladas
(páginas en paralelo) y una publicación que aparece en dos consultas se devuelve
una sola vez. Agrupar consultas atribuiría publicaciones al producto equivocado.
El costo extra es el arranque (USD 0,00005 por corrida).

Gasto: el actor cobra USD 0,001 por publicación con un mínimo de 20 por consulta
(~USD 0,02 por búsqueda). Cada corrida lleva `maxTotalChargeUsd` como techo y se
imputa para arriba (lo que diga Apify o 20 publicaciones, lo que sea más).
Una corrida que no termina a tiempo se aborta.

Seguridad: el token viaja solo en el header Authorization y nunca se loguea. Lo que
vuelve es texto de terceros: se valida acá y `ingest` lo vuelve a sanear.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import httpx
from sqlmodel import Session

from app import runtime
from app.clock import utcnow
from app.config import get_settings
from app.db.models import AuditLog
from app.db.session import engine
from app.ingest.apify_1688 import _json, _number, _request_bounded, _Run
from app.pricing import daily_budget, oficina_ml
from app.pricing.market_ml import (
    MAX_URL_CHARS,
    _clean_https_parts,
    _host_in,
    is_click_tracker,
    safe_image_url,
    safe_permalink,
)
from app.pricing.market_ml_web import MAX_PRICE_CENTS, canonical_permalink, clean_line, valid_ref

log = logging.getLogger(__name__)

API_BASE = "https://api.apify.com/v2"
COUNTER_KEY = "_meta:apify_meli_search_today"
ORIGIN_APIFY = "apify"
JOB_ID = "apify_ml_search"
# Actores cuyo formato de salida conoce `to_wire`. Uno nuevo entra por PR.
_ACTOR_ALLOWLIST = frozenset({"devcake/mercadolibre-scraper"})
_BUILD_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
# El mínimo que acepta el actor por consulta. Se guardan las primeras `pm_ml_web_max_results` (8).
_MAX_ITEMS = 20
_USD_PER_RESULT = 0.001
_USD_PER_START = 0.00005
# Lo que se imputa por corrida cuando no se sabe cuánto trajo (falló, se abortó):
# el gasto se estima para arriba, nunca para abajo.
_WORST_USD_PER_RUN = _MAX_ITEMS * _USD_PER_RESULT + _USD_PER_START
# Techo de cobro por corrida que respeta Apify: 20 publicaciones son USD 0,02; el
# margen es para que un cambio de precio del actor se note acá y no en la factura.
_MAX_CHARGE_USD = 0.03
_ACTOR_MEMORY_MB = 512
_POLL_WAIT_S = 10
_POLL_INTERVAL_S = 0.5
_MAX_POLL_ERRORS = 3
# Margen sobre el timeout del actor antes de abortarlo desde acá.
_DEADLINE_MARGIN_S = 15.0
_ABORT_WAIT_S = 10.0
# Lo que dura la reserva de un lote en la cola: un lote son a lo sumo
# `concurrency` corridas en paralelo de hasta ~2 min.
_CLAIM_LEASE = timedelta(minutes=30)
# Tope de la corrida entera: pasado esto no se lanza otro lote (lo que falta queda para mañana).
_JOB_MAX_S = 2 * 3600
# Corridas fallidas seguidas (no fatales: red, 5xx, timeouts) que frenan la noche.
_FAIL_STREAK = 6
# Errores al lanzar que la próxima corrida repetiría igual: se frena la noche.
_FATAL_HTTP = frozenset({400, 401, 402, 403, 404})
_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"})
_STATUS_REASON = {
    400: "input inválido",
    401: "token inválido",
    402: "sin saldo en Apify",
    403: "token sin permiso para el actor",
    404: "actor o corrida inexistente",
    429: "Apify limitó los requests",
}
_AR_HOST = "mercadolibre.com.ar"
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$", re.ASCII)
_DOMAIN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,60}$", re.ASCII)
_CATALOG_IN_PATH = re.compile(r"/p/(MLA\d{1,20})(?:[/?#]|$)", re.ASCII)

# Una corrida a la vez en este proceso (cron + un disparo a mano). Entre procesos
# lo cubren el líder del scheduler, el cupo atómico y las reservas de la cola.
_job_lock = asyncio.Lock()


# ─── Configuración ─────────────────────────────────────────────────


def _actor() -> str:
    actor = get_settings().apify_meli_search_actor.strip()
    return actor if actor in _ACTOR_ALLOWLIST else ""


def _build() -> str | None:
    """"" = sin fijar; None = valor inválido (no se corre)."""
    build = get_settings().apify_meli_search_build.strip()
    if not build:
        return ""
    return build if _BUILD_RE.fullmatch(build) else None


def _runtime_int(key: str, default_attr: str) -> int:
    raw = runtime.get(key)
    # `is None`, no `or`: 0 es "apagado", no "usá el default".
    return int(getattr(get_settings(), default_attr) if raw is None else raw)


def is_enabled() -> bool:
    return _runtime_int("apify_meli_search_enabled", "apify_meli_search_enabled") == 1


def daily_max() -> int:
    return max(0, _runtime_int("apify_meli_search_daily_max", "apify_meli_search_daily_max"))


def disabled_reason() -> str | None:
    """Por qué no corre, o None si puede correr (el cupo se mira al reservar)."""
    if not is_enabled():
        return "apagado (apify_meli_search_enabled = 0)"
    if not get_settings().apify_token.strip():
        return "falta APIFY_TOKEN"
    if not _actor():
        return "el actor configurado no está en la lista blanca"
    if _build() is None:
        return "el build configurado no es válido"
    return None


def _actor_input(query: str) -> dict[str, Any]:
    return {"queries": [query], "country": "AR", "maxItems": _MAX_ITEMS,
            "enrichDetailPage": False, "sort": "relevance", "condition": "new"}


def _start_url(actor: str, build: str, timeout_s: int) -> str:
    url = (
        f"{API_BASE}/acts/{quote(actor.replace('/', '~'), safe='~')}/runs"
        f"?timeout={timeout_s}&memory={_ACTOR_MEMORY_MB}&maxTotalChargeUsd={_MAX_CHARGE_USD}"
    )
    return f"{url}&build={quote(build, safe='')}" if build else url


# ─── Validación de la salida ───────────────────────────────────────


def _ar_permalink(item: dict) -> str | None:
    """El link de la publicación si es https de mercadolibre.com.ar (o subdominio); None si no lo es.
    Un click-tracker de publicidad (también .com.ar) se cambia por el link canónico del id."""
    for key in ("url_item", "permalink"):
        raw = item.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        parts = _clean_https_parts(raw.strip(), max_chars=MAX_URL_CHARS)
        if parts is None or not _host_in(parts.hostname.lower(), _AR_HOST):
            return None
        if is_click_tracker(raw.strip()):
            return ""
        return safe_permalink(parts._replace(fragment="").geturl()) or None
    return None


def _price_cents(value: object) -> int | None:
    price = _number(value)
    # El tope va ANTES de multiplicar: 1e308 es finito pero x100 da infinito y round() lanza OverflowError,
    # que tiraba la corrida entera del producto (ya cobrada) en vez de descartar solo esa publicación.
    if price is None or not math.isfinite(price) or price <= 0 or price >= MAX_PRICE_CENTS / 100:
        return None
    cents = round(price * 100)
    return cents if 0 < cents < MAX_PRICE_CENTS else None


def to_wire(item: object) -> dict[str, Any] | None:
    """Una publicación del actor en la forma en que la Mac manda las suyas (oficina_ml.candidate_to_wire),
    o None si no pasa: id de ML, título, precio finito > 0 con moneda, link de mercadolibre.com.ar."""
    if not isinstance(item, dict):
        return None
    ref = item.get("ml_id")
    name = clean_line(item.get("name"), 200)
    if not valid_ref(ref) or not name:
        return None
    price = _price_cents(item.get("price"))
    currency = item.get("currency")
    currency = currency.strip().upper() if isinstance(currency, str) else ""
    if price is None or not _CURRENCY_RE.fullmatch(currency):
        return None
    permalink = _ar_permalink(item)
    if permalink is None:
        return None
    permalink = permalink or canonical_permalink(ref)
    images = item.get("image_item") if isinstance(item.get("image_item"), list) else []
    image = next((u for u in (safe_image_url(x) for x in [item.get("thumbnail"), *images[:3]]) if u), None)
    domain = item.get("domain_id")
    catalog = _CATALOG_IN_PATH.search(permalink)
    seller = item.get("vendor_name") if isinstance(item.get("vendor_name"), str) else item.get("seller")
    return {
        "id": ref, "name": name, "image_urls": [image] if image else [], "permalink": permalink,
        "domain_id": domain if isinstance(domain, str) and _DOMAIN_ID_RE.fullmatch(domain) else "",
        "price_cents": price, "currency": currency, "brand": clean_line(item.get("brand"), 40),
        "seller": clean_line(seller, 60), "sold_quantity": None,
        "catalog_id": catalog.group(1) if catalog else "",
    }


def normalize(items: object, limit: int) -> list[dict[str, Any]]:
    """Dataset de UNA corrida (una consulta) → hasta `limit` publicaciones válidas, sin repetir id, en el
    orden del actor (el de relevancia de ML). Lo que no pasa la validación se descarta."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in items[:_MAX_ITEMS * 3] if isinstance(items, list) else []:
        wire = to_wire(raw)
        if wire is None or wire["id"] in seen:
            continue
        seen.add(wire["id"])
        out.append(wire)
        if len(out) >= limit:
            break
    return out


# ─── Una búsqueda (una corrida) ────────────────────────────────────


@dataclass(slots=True)
class SearchOutcome:
    product_id: str
    query: str
    status: str                     # ok | empty | error
    reason: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    raw_items: int = 0
    run_id: str = ""
    usd_imputado: float = 0.0
    fatal: bool = False

    def as_result(self) -> dict[str, Any]:
        """La forma de un resultado de la Mac (lo que recibe `oficina_ml.ingest`)."""
        return {"product_id": self.product_id, "query": self.query, "status": self.status,
                "reason": self.reason, "candidates": self.candidates,
                "fetched_at": utcnow().replace(microsecond=0).isoformat() + "Z"}


def _new_client(total_s: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(total_s, connect=10.0))


async def _abort(http: httpx.AsyncClient, run: _Run, headers: dict) -> None:
    try:
        status, _ = await asyncio.wait_for(
            _request_bounded(http, "POST", f"{API_BASE}/actor-runs/{run.run_id}/abort", headers),
            timeout=_ABORT_WAIT_S)
        if status in (200, 201):
            log.info("Apify ML: corrida %s abortada", run.run_id)
        else:
            log.warning("Apify ML: el abort de %s devolvió HTTP %s", run.run_id, status)
    except Exception as exc:  # noqa: BLE001
        log.warning("Apify ML: no se pudo abortar %s (%s)", run.run_id, type(exc).__name__)


async def search_one(http: httpx.AsyncClient, product_id: str, query: str, *, actor: str, build: str,
                     headers: dict, timeout_s: int, limit: int) -> SearchOutcome:
    """Una corrida del actor para UNA consulta. Nunca lanza: cualquier sorpresa es `error`."""
    out = SearchOutcome(product_id, query, "error", usd_imputado=_WORST_USD_PER_RUN)
    run = _Run(n=1)
    try:
        await asyncio.wait_for(_drive(http, run, out, actor, build, headers, timeout_s, limit),
                               timeout=timeout_s + _DEADLINE_MARGIN_S)
    except asyncio.TimeoutError:
        out.status, out.reason, out.candidates = "error", f"Apify no terminó en {timeout_s} s (abortada)", []
        log.warning("Apify ML: la búsqueda del producto %s no terminó a tiempo", product_id)
    except Exception as exc:  # noqa: BLE001
        # Solo el tipo: el str de algunas excepciones de httpx arrastra el request.
        out.status, out.reason, out.candidates = "error", f"error {type(exc).__name__}", []
        log.warning("Apify ML: la búsqueda del producto %s falló (%s)", product_id, type(exc).__name__)
    if run.run_id and not run.finished:
        await _abort(http, run, headers)
    out.run_id = run.run_id
    if out.status in ("ok", "empty"):
        # Lo que vino en el dataset es lo que se cobró; Apify puede informar más (nunca se imputa menos).
        out.usd_imputado = max(run.usage_usd, out.raw_items * _USD_PER_RESULT + _USD_PER_START)
    elif not run.run_id:
        out.usd_imputado = 0.0      # no llegó a lanzarse: no hay cobro
    out.usd_imputado = round(out.usd_imputado, 6)
    return out


async def _drive(http: httpx.AsyncClient, run: _Run, out: SearchOutcome, actor: str, build: str,
                 headers: dict, timeout_s: int, limit: int) -> None:
    status, body = await _request_bounded(http, "POST", _start_url(actor, build, timeout_s), headers,
                                          _actor_input(out.query))
    if status not in (200, 201):
        out.reason = f"HTTP {status} al lanzar ({_STATUS_REASON.get(status, 'error')})"
        out.fatal = status in _FATAL_HTTP
        log.warning("Apify ML: %s (producto %s)", out.reason, out.product_id)
        return
    if not run.absorb(_json(body)):
        out.reason = "respuesta sin corrida al lanzar"
        return
    errors = 0
    while not run.finished:
        status, body = await _request_bounded(
            http, "GET", f"{API_BASE}/actor-runs/{run.run_id}?waitForFinish={_POLL_WAIT_S}", headers)
        if status in (200, 201) and run.absorb(_json(body)):
            errors = 0
        else:
            errors += 1
            if errors >= _MAX_POLL_ERRORS:
                out.reason = f"{errors} polls fallidos (último HTTP {status})"
                return
        if not run.finished:
            await asyncio.sleep(_POLL_INTERVAL_S)
    if run.status != "SUCCEEDED" or not run.dataset_id:
        out.reason = f"la corrida terminó {run.status or 'sin estado'}"
        return
    status, body = await _request_bounded(
        http, "GET", f"{API_BASE}/datasets/{run.dataset_id}/items?clean=true&format=json&limit={_MAX_ITEMS * 2}",
        headers)
    items = _json(body) if status in (200, 201) else None
    if not isinstance(items, list):
        out.reason = "no se pudo leer el dataset" if status in (200, 201) else f"HTTP {status} al leer el dataset"
        return
    out.raw_items = len(items)
    out.candidates = normalize(items, limit)
    if out.candidates:
        out.status = "ok"
    else:
        out.status = "empty"
        out.reason = ("ML no tiene publicaciones para la consulta" if not items
                      else f"ninguna de las {len(items)} publicaciones pasó la validación")


# ─── La corrida de la noche ────────────────────────────────────────


@dataclass(slots=True)
class RunReport:
    status: str = "ok"              # ok | disabled | over_budget | stopped | busy | error
    reason: str = ""
    searches: int = 0
    ok: int = 0
    empty: int = 0
    errors: int = 0
    results: int = 0                # publicaciones guardadas (suma de candidatos)
    stored: int = 0
    rejected: int = 0
    usd_imputado: float = 0.0
    run_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason, "searches": self.searches, "ok": self.ok,
                "empty": self.empty, "errors": self.errors, "results": self.results, "stored": self.stored,
                "rejected": self.rejected, "usd_imputado": round(self.usd_imputado, 6),
                "runs": len(self.run_ids)}


async def run_search(trigger: str = "cron") -> RunReport:
    """La búsqueda de la noche. Nunca lanza."""
    if _job_lock.locked():
        log.info("Apify ML: ya hay una corrida en marcha, no se lanza otra")
        return RunReport(status="busy", reason="ya hay una corrida en marcha")
    async with _job_lock:
        report = RunReport()
        started = time.monotonic()
        try:
            await _run(report, started)
        except Exception as exc:  # noqa: BLE001
            report.status, report.reason = "error", f"error inesperado {type(exc).__name__}"
            log.warning("Apify ML: la corrida se cortó por %s", type(exc).__name__)
        log.info("Apify ML (%s): %s%s — %d búsquedas (%d con resultados, %d vacías, %d con error), "
                 "%d publicaciones, USD %.4f imputados, %.0f s", trigger, report.status,
                 f" ({report.reason})" if report.reason else "", report.searches, report.ok, report.empty,
                 report.errors, report.results, report.usd_imputado, time.monotonic() - started)
        if report.status != "disabled":
            _audit(report, trigger)
        return report


async def _run(report: RunReport, started: float) -> None:
    reason = disabled_reason()
    if reason:
        report.status, report.reason = "disabled", reason
        return
    budget = daily_max()
    left = budget - daily_budget.used_today(COUNTER_KEY)
    if left <= 0:
        report.status, report.reason = "over_budget", f"cupo diario agotado ({budget})"
        return
    s = get_settings()
    actor, build = _actor(), _build() or ""
    timeout_s, batch_size = int(s.apify_meli_search_timeout_s), max(1, int(s.apify_meli_search_concurrency))
    limit = oficina_ml._max_candidates()
    claimer = f"apify-{secrets.token_hex(4)}"
    # Un lote de más que el cupo libre: quien corta es la reserva atómica (otra corrida puede gastar entre medio),
    # y así se sabe si quedó cola sin buscar por falta de cupo.
    queue = await asyncio.to_thread(
        oficina_ml.build_queue, min(left + batch_size, oficina_ml.MAX_QUEUE), claimer=claimer)
    if not queue:
        report.reason = "la cola está vacía"
        return
    headers = {"Authorization": f"Bearer {s.apify_token.strip()}", "Accept": "application/json"}
    http = _new_client(_POLL_WAIT_S + 15.0)
    fail_streak = 0
    try:
        for at in range(0, len(queue), batch_size):
            if time.monotonic() - started > _JOB_MAX_S:
                report.status, report.reason = "stopped", "se pasó el tiempo máximo de la corrida"
                return
            batch = {i["product_id"]: i["queries"][0] for i in queue[at:at + batch_size]}
            claimed = await asyncio.to_thread(oficina_ml.claim, list(batch), claimer, _CLAIM_LEASE)
            if not claimed:
                continue                     # los tomó la Mac entre que se armó la cola y ahora
            granted = await asyncio.to_thread(daily_budget.reserve_up_to, COUNTER_KEY, budget, len(claimed))
            todo, spare = claimed[:granted], claimed[granted:]
            await asyncio.to_thread(oficina_ml.release, claimer, spare)
            if todo:
                outcomes = await asyncio.gather(*(
                    search_one(http, pid, batch[pid], actor=actor, build=build, headers=headers,
                               timeout_s=timeout_s, limit=limit) for pid in todo))
                await asyncio.to_thread(_store, outcomes, report)
                await asyncio.to_thread(oficina_ml.release, claimer, todo)
                if any(o.fatal for o in outcomes):
                    report.status = "stopped"
                    report.reason = next(o.reason for o in outcomes if o.fatal)
                    return
                fail_streak = 0 if any(o.status != "error" for o in outcomes) else fail_streak + len(outcomes)
                if fail_streak >= _FAIL_STREAK:
                    report.status, report.reason = "stopped", f"{fail_streak} búsquedas seguidas con error"
                    return
            if granted < len(claimed):
                report.status = "over_budget"
                report.reason = f"cupo diario agotado ({budget}) o no se pudo reservar"
                return
    finally:
        await http.aclose()
        await asyncio.to_thread(oficina_ml.release, claimer)


def _store(outcomes: list[SearchOutcome], report: RunReport) -> None:
    for o in outcomes:
        report.searches += 1
        report.usd_imputado += o.usd_imputado
        if o.run_id:
            report.run_ids.append(o.run_id)
        if o.status == "ok":
            report.ok += 1
            report.results += len(o.candidates)
        elif o.status == "empty":
            report.empty += 1
        else:
            report.errors += 1
    ingested = oficina_ml.ingest([o.as_result() for o in outcomes], origin=ORIGIN_APIFY)
    report.stored += ingested.stored
    report.rejected += len(ingested.rejected)


def _audit(report: RunReport, trigger: str) -> None:
    """Una fila por corrida en el audit log: búsquedas, publicaciones y USD imputados."""
    data = report.as_dict()
    detail = (f"Búsqueda web de ML con Apify ({trigger}): {report.searches} búsquedas, {report.results} "
              f"publicaciones, USD {report.usd_imputado:.4f} — {report.status}"
              + (f" ({report.reason})" if report.reason else ""))
    try:
        with Session(engine) as s:
            s.add(AuditLog(action="apify_ml_search_run", source="apify", product_id="(semaforo)",
                           detail=detail[:500], after=json.dumps(data), notified=True))
            s.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("Apify ML: no se pudo registrar la corrida en el audit log (%s)", type(exc).__name__)
