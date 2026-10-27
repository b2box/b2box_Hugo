"""Radar de Luis: cuántas publicaciones tiene Mercado Libre Argentina por palabra (POST /radar/ml-supply).

Luis calcula la escasez con ese número (`e_ml = 1 − clamp(log10(N+1)/4, 0, 1)`). Por qué Apify: la API oficial
no sirve para esto (`/sites/MLA/search` da 403 con el token de Hugo y `/products/search` cuenta fichas de
catálogo con techo 10.000, no publicaciones) y la web de ML bloquea al servidor y a la oficina. El actor
`lergassy/mercadolibre-reviews-scraper` en modo `search` devuelve, por término, una fila `type:"product"` con
`searchTerm` y `totalResults` (el "N resultados" del listado; ML lo corta en 9.999).

Reglas:
  * caché de 7 días por término normalizado (lower, strip, espacios colapsados): un acierto no paga;
  * UNA corrida del actor por pedido con todos los términos sin caché, con techo de cobro y timeout;
  * tope diario propio de términos pagos (`HUGO_RADAR_ML_DAILY_CAP`), reservado atómico y fail-closed;
  * tope mensual en USD (`HUGO_RADAR_APIFY_MONTHLY_USD`): si lo gastado en el mes UTC más el techo de cobro de
    esta corrida lo pasa, no se corre;
  * cada corrida deja su costo en `apify_spend` (etiqueta `radar_ml`): el que informa Apify o uno estimado;
  * key de Apify propia opcional (`HUGO_RADAR_APIFY_TOKEN`) para separar el gasto en Apify Console;
  * un término sin fila válida vuelve `ok:false`, `total:null`. Nunca un 0 inventado: 0 solo si ML dijo 0.

Reusa el cliente de Apify de la búsqueda del semáforo (apify_ml_search / apify_1688): token solo en el header,
respuestas acotadas, abort de la corrida que no terminó y el chequeo de permisos limitados del actor.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import httpx
from sqlmodel import Session, func, select

from app.clock import utcnow
from app.config import get_settings
from app.db.models import ApifySpend, RadarMlSupplyCache
from app.db.session import engine
from app.ingest.apify_1688 import _json, _number, _request_bounded, _Run
from app.pricing import apify_ml_search as ams
from app.pricing import daily_budget

log = logging.getLogger(__name__)

# Lista blanca de UN actor: su formato de salida es el que entiende `parse_totals`. Otro actor entra por PR.
ACTOR = "lergassy/mercadolibre-reviews-scraper"
COUNTER_KEY = "_meta:radar_ml_supply_today"
CACHE_TTL = timedelta(days=7)
RUN_TIMEOUT_S = 240
SPEND_LABEL = "radar_ml"
# Techo de cobro por corrida: USD 0,0025 por término medido; el margen absorbe el arranque y un cambio de precio.
_USD_PER_TERM_CAP = 0.01
_USD_PER_RUN_CAP = 0.05
# Estimación cuando Apify no informa `usageTotalUsd`: lo medido por fila cobrada.
_USD_PER_ROW = 0.0025
USAGE_DAYS = 30
# ML corta en 9.999; más que esto es basura del actor.
_MAX_TOTAL = 10**7
_MAX_ROWS = 500

ERR_NOT_CONFIGURED = "apify no configurado"
ERR_DAILY_CAP = "tope diario"
ERR_MONTHLY_CAP = "tope mensual de Apify"
ERR_NO_TOTAL = "ML no informó el total"


def normalize_term(term: str) -> str:
    return " ".join(term.split()).lower()


def _iso(moment) -> str:
    return moment.replace(microsecond=0).isoformat() + "Z"


def _result(term: str, *, total: int | None = None, source: str = "apify", fetched_at: str | None = None,
            error: str | None = None) -> dict[str, Any]:
    ok = error is None and total is not None
    return {"term": term, "ok": ok, "total": total if ok else None, "sellers": None, "median_price_ars": None,
            "source": source, "fetched_at": fetched_at if ok else None, "error": None if ok else (error or ERR_NO_TOTAL)}


# ─── Caché ─────────────────────────────────────────────────────────


def _load_cache(keys: list[str]) -> dict[str, RadarMlSupplyCache]:
    cutoff = utcnow() - CACHE_TTL
    with Session(engine) as s:
        rows = s.exec(select(RadarMlSupplyCache).where(
            RadarMlSupplyCache.term_key.in_(keys), RadarMlSupplyCache.fetched_at >= cutoff)).all()
        return {r.term_key: RadarMlSupplyCache(term_key=r.term_key, total=r.total, fetched_at=r.fetched_at)
                for r in rows}


def _save_cache(totals: dict[str, int], fetched_at) -> None:
    try:
        with Session(engine) as s:
            for key, total in totals.items():
                s.merge(RadarMlSupplyCache(term_key=key, total=total, fetched_at=fetched_at))
            s.commit()
    except Exception as exc:  # noqa: BLE001
        # Sin caché el dato igual vuelve; solo se paga de nuevo la próxima vez.
        log.warning("Radar ML: no se pudo guardar la caché (%s)", type(exc).__name__)


# ─── El actor ──────────────────────────────────────────────────────


def parse_totals(items: object, wanted: set[str]) -> dict[str, int]:
    """Dataset del actor → {término normalizado: total}. Se ignoran las filas que no son `product` (el `notice`),
    los términos que no se pidieron y los totales que no son un entero ≥ 0."""
    out: dict[str, int] = {}
    for row in items[:_MAX_ROWS] if isinstance(items, list) else []:
        if not isinstance(row, dict) or row.get("type") != "product":
            continue
        term = row.get("searchTerm")
        if not isinstance(term, str) or len(term) > 200:
            continue
        key = normalize_term(term)
        total = row.get("totalResults")
        if key in wanted and key not in out and isinstance(total, int) and not isinstance(total, bool) \
                and 0 <= total <= _MAX_TOTAL:
            out[key] = total
    return out


def max_charge_usd(n_terms: int) -> float:
    return round(_USD_PER_TERM_CAP * n_terms + _USD_PER_RUN_CAP, 4)


def _start_url(n_terms: int) -> str:
    return (f"{ams.API_BASE}/acts/{quote(ACTOR.replace('/', '~'), safe='~')}/runs"
            f"?timeout={RUN_TIMEOUT_S}&maxTotalChargeUsd={max_charge_usd(n_terms)}")


def _actor_input(terms: list[str]) -> dict[str, Any]:
    return {"mode": "search", "country": "AR", "searchTerms": terms, "maxItems": 1, "maxConcurrency": 3}


def _token() -> str:
    s = get_settings()
    return s.hugo_radar_apify_token.strip() or s.apify_token.strip()


def _new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(ams._POLL_WAIT_S + 15.0, connect=10.0))


@dataclass(slots=True)
class RunOutcome:
    totals: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    run_id: str = ""
    # Lo que informó Apify (`usageTotalUsd`), None si no vino.
    reported_usd: float | None = None
    # Filas `product` del dataset (las que cobra el actor), None si no se llegó a leer.
    rows: int | None = None


def _reported_usd(payload: object) -> float | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    usd = _number(data.get("usageTotalUsd")) if isinstance(data, dict) else None
    return usd if usd is not None and usd >= 0 else None


async def _drive(http: httpx.AsyncClient, run: _Run, out: RunOutcome, headers: dict, terms: list[str]) -> list:
    """Lanza la corrida, espera a que termine y devuelve el dataset. Lanza RuntimeError con un motivo corto."""
    status, body = await _request_bounded(http, "POST", _start_url(len(terms)), headers, _actor_input(terms))
    if status not in (200, 201):
        raise RuntimeError(f"apify: HTTP {status} al lanzar ({ams._STATUS_REASON.get(status, 'error')})")
    if not run.absorb(_json(body)):
        raise RuntimeError("apify: respuesta sin corrida al lanzar")
    errors = 0
    while not run.finished:
        status, body = await _request_bounded(
            http, "GET", f"{ams.API_BASE}/actor-runs/{run.run_id}?waitForFinish={ams._POLL_WAIT_S}", headers)
        payload = _json(body) if status in (200, 201) else None
        if run.absorb(payload):
            errors = 0
            out.reported_usd = _reported_usd(payload)
        else:
            errors += 1
            if errors >= ams._MAX_POLL_ERRORS:
                raise RuntimeError(f"apify: {errors} consultas de estado fallidas")
        if not run.finished:
            await asyncio.sleep(ams._POLL_INTERVAL_S)
    if run.status != "SUCCEEDED" or not run.dataset_id:
        raise RuntimeError(f"apify: la corrida terminó {run.status or 'sin estado'}")
    status, body = await _request_bounded(
        http, "GET", f"{ams.API_BASE}/datasets/{run.dataset_id}/items?clean=true&format=json"
                     f"&limit={len(terms) * 5 + 10}", headers)
    items = _json(body) if status in (200, 201) else None
    if not isinstance(items, list):
        raise RuntimeError("apify: no se pudo leer el dataset")  # noqa: TRY004  (motivo para Luis, no un bug)
    out.rows = sum(1 for r in items if isinstance(r, dict) and r.get("type") == "product")
    return items


async def fetch_totals(terms: list[str]) -> RunOutcome:
    """UNA corrida del actor para todos los términos. Nunca lanza; el error es corto y sin token ni URLs."""
    headers = {"Authorization": f"Bearer {_token()}", "Accept": "application/json"}
    http = _new_client()
    run = _Run(n=1)
    out = RunOutcome()
    try:
        level = await ams._actor_permission(http, ACTOR, headers)
        if level != ams._REQUIRED_PERMISSION:
            out.error = "apify: el actor no corre con permisos limitados"
            return out
        items = await asyncio.wait_for(_drive(http, run, out, headers, terms),
                                       timeout=RUN_TIMEOUT_S + ams._DEADLINE_MARGIN_S)
        out.totals = parse_totals(items, {normalize_term(t) for t in terms})
    except asyncio.TimeoutError:
        out.error = f"apify: la corrida no terminó en {RUN_TIMEOUT_S} s"
    except RuntimeError as exc:
        out.error = str(exc)[:120]
    except Exception as exc:  # noqa: BLE001
        # Solo el tipo: el str de algunas excepciones de httpx arrastra el request.
        log.warning("Radar ML: la corrida falló (%s)", type(exc).__name__)
        out.error = f"apify: error {type(exc).__name__}"
    finally:
        if run.run_id and not run.finished:
            await ams._abort(http, run, headers)
        await http.aclose()
        out.run_id = run.run_id
    return out


# ─── Gasto ─────────────────────────────────────────────────────────


def _month_start(now) -> Any:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _day_start(now) -> Any:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _spent_since(since) -> float:
    with Session(engine) as s:
        total = s.exec(select(func.coalesce(func.sum(ApifySpend.usd), 0.0)).where(
            ApifySpend.label == SPEND_LABEL, ApifySpend.created_at >= since)).one()
    return round(float(total), 6)


def record_spend(out: RunOutcome, n_terms: int) -> None:
    """Una fila por corrida lanzada. Sin `usageTotalUsd` se estima por filas cobradas; sin dataset, el techo de
    cobro de la corrida (se imputa para arriba, nunca para abajo)."""
    if not out.run_id:
        return  # no se llegó a lanzar: Apify no cobra
    if out.reported_usd is not None:
        usd, estimated = out.reported_usd, False
    elif out.rows is not None:
        usd, estimated = _USD_PER_ROW * out.rows, True
    else:
        usd, estimated = max_charge_usd(n_terms), True
    try:
        with Session(engine) as s:
            s.add(ApifySpend(label=SPEND_LABEL, run_id=out.run_id[:40], terms=n_terms, usd=round(usd, 6),
                             estimated=estimated))
            s.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("Radar ML: no se pudo registrar el gasto de la corrida %s (%s)", out.run_id, type(exc).__name__)


def spend_summary() -> dict[str, float]:
    now = utcnow()
    return {"apify_usd_month": _spent_since(_month_start(now)),
            "apify_usd_cap_month": float(get_settings().hugo_radar_apify_monthly_usd),
            "apify_usd_today": _spent_since(_day_start(now))}


def usage_report() -> dict[str, Any]:
    """Gasto del radar por día UTC de los últimos 30 días (solo los días con corridas) y el total del mes."""
    now = utcnow()
    since = _day_start(now) - timedelta(days=USAGE_DAYS - 1)
    with Session(engine) as s:
        rows = s.exec(select(ApifySpend).where(ApifySpend.label == SPEND_LABEL, ApifySpend.created_at >= since)
                      .order_by(ApifySpend.created_at)).all()
    days: dict[str, dict[str, Any]] = {}
    for r in rows:
        day = days.setdefault(r.created_at.strftime("%Y-%m-%d"),
                              {"usd": 0.0, "runs": 0, "terms": 0, "estimated_runs": 0})
        day["usd"] += r.usd
        day["runs"] += 1
        day["terms"] += r.terms
        day["estimated_runs"] += int(r.estimated)
    return {"label": SPEND_LABEL,
            "days": [{"date": d, **v, "usd": round(v["usd"], 6)} for d, v in days.items()],
            **spend_summary()}


# ─── El pedido ─────────────────────────────────────────────────────


async def lookup(terms: list[str]) -> dict[str, Any]:
    """Términos ya validados (recortados, sin repetidos) → la respuesta de /radar/ml-supply.

    `term` vuelve con el texto que mandó Luis; la forma normalizada es solo la clave de la caché y del emparejado
    con el actor. Dos términos que normalizan igual comparten consulta (se paga una) y vuelven uno por cada uno."""
    keys = list(dict.fromkeys(normalize_term(t) for t in terms))
    settings = get_settings()
    cap = int(settings.hugo_radar_ml_daily_cap)
    cached = await asyncio.to_thread(_load_cache, keys)
    # Por clave, los argumentos de `_result` salvo el término.
    found: dict[str, dict[str, Any]] = {
        key: {"total": row.total, "source": "cache", "fetched_at": _iso(row.fetched_at)}
        for key, row in cached.items()}
    pending = [key for key in keys if key not in found]
    # El actor recibe el primer texto original de cada clave.
    query_for: dict[str, str] = {}
    for t in terms:
        query_for.setdefault(normalize_term(t), t)

    if pending and not _token():
        found.update({key: {"error": ERR_NOT_CONFIGURED} for key in pending})
    elif pending:
        # Lo que entraría por el tope diario, para saber cuánto podría cobrar la corrida.
        room = max(0, cap - await asyncio.to_thread(daily_budget.used_today, COUNTER_KEY))
        to_run = min(room, len(pending))
        month = await asyncio.to_thread(_spent_since, _month_start(utcnow()))
        if to_run and month + max_charge_usd(to_run) > float(settings.hugo_radar_apify_monthly_usd):
            # shortcut: dos pedidos simultáneos pueden pasar el chequeo juntos (como mucho una corrida de más).
            found.update({key: {"error": ERR_MONTHLY_CAP} for key in pending})
            log.warning("Radar ML: tope mensual de Apify (USD %.4f gastados de %.2f), no se corre",
                        month, settings.hugo_radar_apify_monthly_usd)
        else:
            # Se reserva antes de lanzar y no se devuelve si la corrida falla: el gasto se imputa para arriba.
            granted = await asyncio.to_thread(daily_budget.reserve_up_to, COUNTER_KEY, cap, to_run)
            paid, over = pending[:granted], pending[granted:]
            found.update({key: {"error": ERR_DAILY_CAP} for key in over})
            if paid:
                out = await fetch_totals([query_for[key] for key in paid])
                now = utcnow()
                found.update({key: {"total": out.totals.get(key), "fetched_at": _iso(now), "error": out.error}
                              for key in paid})
                await asyncio.to_thread(record_spend, out, len(paid))
                if out.totals:
                    await asyncio.to_thread(_save_cache, out.totals, now)
                log.info("Radar ML: %d términos (%d de caché, %d pagos, %d con total, %d por tope)%s",
                         len(keys), len(cached), len(paid), len(out.totals), len(over),
                         f" — {out.error}" if out.error else "")

    used = await asyncio.to_thread(daily_budget.used_today, COUNTER_KEY)
    spend = await asyncio.to_thread(spend_summary)
    return {"results": [_result(t, **found[normalize_term(t)]) for t in terms],
            "budget": {"used_today": used, "daily_cap": cap, **spend}}
