"""Google Lens para el semáforo: la foto encuentra el título que usa el vendedor en ML.

Por qué: la búsqueda de ML usa NUESTRO nombre y el vendedor le pone otro, así que muchos productos quedan sin
IDÉNTICO. Este paso corre dentro del job de Apify (pricing/apify_ml_search.py), antes de armar la cola:

  1. elige hasta `pm_lens_daily_max` productos sin IDÉNTICO que ya se buscaron por texto (`eligible`);
  2. pasa sus fotos por el actor `s-r/google-lens` (build fijado, lotes de 25, hasta 3 corridas a la vez);
  3. de cada foto toma los matches de Mercado Libre y resuelve su `https://www.google.com/goto?…` con UN GET sin
     seguir la redirección: el destino sale del `Location`. A ML no se le pide nada nunca;
  4. del link de ML saca la consulta (el slug del título) y la guarda en `ml_lens_result`.

La cola (`oficina_ml.build_queue(with_lens=True)`) ofrece esa consulta como una búsqueda más de Apify y el juez de
siempre decide. Si ganó el IDÉNTICO, el snapshot queda con ml_variant="lens" y así se mide si Lens sirve.

Seguridad: lo que vuelve del actor es de un tercero. Solo se le pega a `www.google.com/goto` (URL exacta, sin
userinfo ni puerto) y solo se acepta un `Location` https a un dominio de ML hispano de la lista cerrada. La consulta
sale de un slug externo: queda en letras, números y espacios antes de llegar a Apify o al juez. Los logs llevan
contadores, nunca URLs ni el token.

Gasto acotado: cupo diario atómico releído en cada lote, 3 errores seguidos de una foto la dejan sin_links 60 días, y si
Google frena los goto (BRAKE_KEY) se saltean 2 noches y se prueba con pocas fotos antes de volver.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from sqlalchemy import and_, case
from sqlmodel import Session, func, select

from app import runtime
from app.clock import utcnow
from app.db.models import MarketPriceSnapshot, MlLensResult, MlWebResult, Setting
from app.db.session import engine
from app.ingest.apify_1688 import _json, _request_bounded, _Run, allowed_image_url
from app.pricing import apify_ml_search as ams
from app.pricing import daily_budget, market_query, oficina_ml
from app.notifier.dispatcher import notify
from app.pricing.market_ml import _clean_https_parts, _host_in

log = logging.getLogger(__name__)

COUNTER_KEY = "_meta:ml_lens_today"
# Freno que dura entre noches: el día (UTC, AAAA-MM-DD) en que Google frenó los goto. Las _BRAKE_SKIP_NIGHTS noches
# siguientes no se lanza el actor; después se prueba con _PROBE_PHOTOS fotos y, si Google no frena, se suelta solo.
BRAKE_KEY = "_meta:ml_lens_brake"
_BRAKE_SKIP_NIGHTS = 2
_PROBE_PHOTOS = 5
# Errores seguidos de una foto: al tercero queda sin_links (60 días sin pagar otro intento).
MAX_ATTEMPTS = 3
# Actor y build en constantes, no en env: otro actor (u otro formato de salida) entra por PR.
_ACTOR = "s-r/google-lens"
_BUILD = "0.2.7"
_ACTOR_TIMEOUT_S = 600
_ACTOR_MEMORY_MB = 256
_USD_PER_PHOTO = 0.005               # el actor cobra la foto con al menos un match
_PHOTOS_PER_RUN = 25                 # el actor acepta 100; con 25 el dataset entra holgado en los 2 MB de _request_bounded
_PARALLEL_RUNS = 3
_STEP_DEADLINE_S = 15 * 60           # el paso nunca frena las búsquedas de ML (el semáforo corre 90 min después)
_MAX_MATCHES = 100
REFRESH_AFTER = timedelta(days=60)
ERROR_RETRY_AFTER = timedelta(hours=20)
# Decisión del usuario (03-nov): UNA consulta Lens por producto.
MAX_QUERIES = 1
_TEXT_PLAN_VARIANTS = 3              # contra qué plan de texto se deduplica

ST_OK, ST_SIN_LINKS, ST_ERROR = "ok", "sin_links", "error"
_NO_IGUAL = ("diferente", "similar", "ninguno")

# Resolución de los goto.
_GOTO_HOST = "www.google.com"
_MAX_ML_PER_PHOTO = 10
_RESOLVE_PARALLEL = 4
_RESOLVE_TIMEOUT_S = 5.0
_BLOCK_STREAK = 3
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_ML_AR = "mercadolibre.com.ar"
# Respaldo cuando no hay link AR (decisión del usuario). Brasil nunca.
_ML_HISPANIC = ("mercadolibre.com.mx", "mercadolibre.cl", "mercadolibre.com.co", "mercadolibre.com.uy",
                "mercadolibre.com.pe", "mercadolibre.com.ve", "mercadolibre.com.ec")
_ML_DOMAINS = (_ML_AR, *_ML_HISPANIC)

# Link de ML → consulta. La regla del texto (`clean_query`) vive en oficina_ml: la cola la usa al leer la tabla.
clean_query = oficina_ml.clean_query
# ML corta el slug en ~60 caracteres y deja la última palabra a medias ("gran-capac"); con 55 todavía está entera.
_CUT_SLUG_AT = 56
_SLUG = re.compile(r"^[A-Za-z0-9%-]{1,200}$", re.ASCII)
_ITEM_ID = re.compile(r"^ML[A-Z]U?\d{1,20}$", re.ASCII)
_LOOKS_LIKE_ID = re.compile(r"^ML[A-Z]U?-?\d", re.ASCII | re.IGNORECASE)
_ARTICLE = re.compile(r"^(ML[A-Z])-(\d{1,20})-([A-Za-z0-9%-]+?)-_JM$", re.ASCII)


# ─── Configuración ─────────────────────────────────────────────────


def is_enabled() -> bool:
    return ams._runtime_int("pm_lens_enabled", "pm_lens_enabled") == 1


def daily_max() -> int:
    return max(0, ams._runtime_int("pm_lens_daily_max", "pm_lens_daily_max"))


# ─── Link de ML → consulta ─────────────────────────────────────────


def slug_to_query(slug: str) -> str:
    text = unquote(slug).lower()
    words = [w for w in text.split("-") if w]
    if len(text) >= _CUT_SLUG_AT and len(words) > 1:
        words = words[:-1]
    return clean_query(" ".join(words))


def _ml_domain(url: object) -> tuple[str, Any] | None:
    """(dominio de ML de la lista blanca, partes de la URL) o None. https, sin userinfo ni puerto raro."""
    parts = _clean_https_parts(url)
    if parts is None:
        return None
    host = parts.hostname.lower()
    domain = next((d for d in _ML_DOMAINS if _host_in(host, d)), None)
    return (domain, parts) if domain else None


def query_from_link(url: object) -> dict[str, str] | None:
    """{"q", "host", "ml_id"} de un link de publicación de ML, o None si no es una forma conocida.

    Formas: `/{slug}/up/{MLAU…}`, `/{slug}/p/{MLA…}`, `articulo.<host>/{SITE}-{dígitos}-{slug}-_JM` y `/{slug}`.
    Lo demás (`/p/MLA…` sin slug, `/shorts/…`, `listado.…`, el query string) se ignora."""
    found = _ml_domain(url)
    if found is None:
        return None
    domain, parts = found
    host = parts.hostname.lower()
    segs = [s for s in parts.path.split("/") if s]
    slug = ml_id = ""
    if host == f"articulo.{domain}":
        m = _ARTICLE.fullmatch(segs[0]) if len(segs) == 1 else None
        if m:
            ml_id, slug = m.group(1) + m.group(2), m.group(3)
    elif host in (domain, f"www.{domain}"):
        if len(segs) == 3 and segs[1] in ("p", "up") and _ITEM_ID.fullmatch(segs[2]):
            slug, ml_id = segs[0], segs[2]
        elif len(segs) == 1 and not _LOOKS_LIKE_ID.match(segs[0]):
            slug = segs[0]
    if not slug or not _SLUG.fullmatch(slug):
        return None
    query = slug_to_query(slug)
    return {"q": query, "host": domain, "ml_id": ml_id} if query else None


def pick_queries(links: list[tuple[int, str]], plan: list[str]) -> list[dict[str, Any]]:
    """Las consultas de una foto: primero links AR por posición en Lens; de países hispanos solo si faltan.
    Sin repetir (por `market_query._key`, ni contra el plan de texto del producto, ni por id de ML)."""
    parsed: list[dict[str, Any]] = [{**found, "pos": pos} for pos, url in links if (found := query_from_link(url))]
    parsed.sort(key=lambda x: (x["host"] != _ML_AR, x["pos"]))
    seen = {market_query._key(q) for q in plan}
    ids: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in parsed:
        key = market_query._key(item["q"])
        if key in seen or (item["ml_id"] and item["ml_id"] in ids):
            continue
        seen.add(key)
        ids.add(item["ml_id"])
        out.append(item)
        if len(out) >= MAX_QUERIES:
            break
    return out


# ─── Resolver los google.com/goto ──────────────────────────────────


def goto_url(raw: object) -> str | None:
    """La URL si es exactamente `https://www.google.com/goto?…` (sin userinfo ni puerto); None si no.
    Anti-SSRF: la salida del actor es de un tercero y a otra cosa no se le hace ningún request."""
    parts = _clean_https_parts(raw)
    if parts is None or parts.hostname != _GOTO_HOST or parts.port is not None or parts.path != "/goto" or not parts.query:
        return None
    return parts._replace(netloc=_GOTO_HOST, fragment="").geturl()


def _is_google_block(status: int, target: str) -> bool:
    if status == 429:
        return True
    parts = urlsplit(target)
    host = (parts.hostname or "").lower()
    return host == "consent.google.com" or (_host_in(host, "google.com") and parts.path.startswith("/sorry"))


_FAILED = object()      # el link no se pudo resolver por Google (freno, error, red): el producto queda en error y se reintenta


class _Resolver:
    """GET al goto sin seguir la redirección y sin leer el body; el destino es el `Location`. Como mucho
    _RESOLVE_PARALLEL a la vez. Un 429, una redirección a /sorry o a consent y cualquier respuesta que no es una
    redirección (200, 403, 5xx…) son fallas que suman a la racha: con _BLOCK_STREAK seguidas deja de resolver por esta
    noche. Solo una redirección a algo que no es un ML hispano da None (ese link no sirve, sin reintentar)."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http
        self.sem = asyncio.Semaphore(_RESOLVE_PARALLEL)
        self.streak = 0
        self.blocked = False

    async def resolve(self, url: str) -> object:
        """El link de ML aceptado (str), None si el goto no lleva a ML hispano, o _FAILED."""
        async with self.sem:
            if self.blocked:
                return _FAILED
            try:
                status, location = await asyncio.wait_for(self._get(url), timeout=_RESOLVE_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001
                log.info("Google Lens: un goto no se pudo resolver (%s)", type(exc).__name__)
                return _FAILED
        target = urljoin(url, location) if location else ""
        if status not in _REDIRECTS or _is_google_block(status, target):
            self.streak += 1
            if self.streak >= _BLOCK_STREAK and not self.blocked:
                self.blocked = True
                log.warning("Google Lens: Google frenó %d goto seguidos; no se resuelve más esta noche", self.streak)
            return _FAILED
        self.streak = 0
        return target if _ml_domain(target) else None

    async def _get(self, url: str) -> tuple[int, str]:
        # Sin headers: el de Apify (Authorization) nunca viaja a Google.
        async with self.http.stream("GET", url, timeout=httpx.Timeout(_RESOLVE_TIMEOUT_S),
                                    follow_redirects=False) as resp:
            return resp.status_code, resp.headers.get("location", "")


def _ml_gotos(matches: list) -> list[tuple[int, str]]:
    """(posición en Lens, goto) de los matches de Mercado Libre, como mucho _MAX_ML_PER_PHOTO."""
    out: list[tuple[int, str]] = []
    for pos, m in enumerate(matches, start=1):
        if not isinstance(m, dict):
            continue
        source = m.get("source_domain")
        url = goto_url(m.get("page_url") or m.get("url"))
        if isinstance(source, str) and source.startswith("Mercado") and url:
            out.append((pos, url))
            if len(out) >= _MAX_ML_PER_PHOTO:
                break
    return out


# ─── Elegibles ─────────────────────────────────────────────────────


def _due(ran_at: datetime, status: str, now: datetime) -> bool:
    return ran_at <= now - REFRESH_AFTER or (status == ST_ERROR and ran_at <= now - ERROR_RETRY_AFTER)


def eligible(limit: int, *, now: datetime | None = None) -> list[tuple[str, str, str]]:
    """(product_id, foto, nombre) de hasta `limit` productos para lensear: último snapshot vigente sin IDÉNTICO
    (`no_data` con diferente / similar / ninguno), medible, con foto pública, ya buscado por texto, y sin
    lensear (o lenseado hace 60 días; con error, 20 h). Primero los nunca lenseados (habilitados primero); después los
    que vuelven (error o 60 días), el que se lenseó hace más tiempo primero: un error no le gana el lugar a uno nuevo."""
    if limit <= 0:
        return []
    now = now or utcnow()
    with Session(engine) as s:
        newest = select(func.max(MarketPriceSnapshot.id)).group_by(MarketPriceSnapshot.product_id)
        snaps = s.exec(
            select(MarketPriceSnapshot.product_id, MarketPriceSnapshot.ml_status, MarketPriceSnapshot.match_state,
                   MarketPriceSnapshot.unmeasurable, MarketPriceSnapshot.product_image_url,
                   MarketPriceSnapshot.product_name, MarketPriceSnapshot.product_enabled,
                   MarketPriceSnapshot.captured_at)
            .where(MarketPriceSnapshot.id.in_(newest))  # type: ignore[union-attr]
        ).all()
        searched = set(s.exec(
            select(MlWebResult.product_id).where(MlWebResult.status.in_(oficina_ml.RESULT_STATUSES)).distinct()  # type: ignore[attr-defined]
        ).all())
        lensed = {pid: (ran_at, status) for pid, ran_at, status in s.exec(
            select(MlLensResult.product_id, MlLensResult.ran_at, MlLensResult.status)).all()}
    still_measured = max((row[7] for row in snaps), default=now) - oficina_ml.SNAPSHOT_MAX_AGE
    out: list[tuple[Any, ...]] = []
    for pid, ml_status, state, unmeasurable, image, name, enabled, captured in snaps:
        if (ml_status != "no_data" or state not in _NO_IGUAL or unmeasurable is not None
                or captured < still_measured or pid not in searched):
            continue
        if pid in lensed and not _due(*lensed[pid], now):
            continue
        url = allowed_image_url(image)
        if url:
            order = (1, lensed[pid][0], oficina_ml._sort_id(pid)) if pid in lensed else \
                (0, enabled is False, oficina_ml._sort_id(pid))
            out.append((order, pid, url, name or ""))
    out.sort(key=lambda t: t[0])
    return [t[1:] for t in out[:limit]]


# ─── El paso de la noche ───────────────────────────────────────────


@dataclass(slots=True)
class LensReport:
    status: str = "disabled"        # disabled | ok | over_budget | stopped | error
    reason: str = ""
    products: int = 0               # fotos lenseadas (con fila en ml_lens_result)
    with_queries: int = 0
    sin_links: int = 0
    errors: int = 0
    matches: int = 0
    ml_links: int = 0
    usd: float = 0.0
    resolve_blocked: bool = False
    # Hubo matches de Lens y ningún link de ML: Google pudo haber cambiado el goto (todo quedaría sin_links 60 días).
    suspicious: bool = False
    # Corridas que informaron más que el techo de cobro (n · 0,005 · 1,5): [{run_id, usage_usd}].
    overcharged: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason, "products": self.products,
                "with_queries": self.with_queries, "sin_links": self.sin_links, "errors": self.errors,
                "matches": self.matches, "ml_links": self.ml_links, "usd": round(self.usd, 6),
                "resolve_blocked": self.resolve_blocked, "suspicious": self.suspicious,
                "overcharged_runs": self.overcharged}


@dataclass(slots=True)
class _Step:
    http: httpx.AsyncClient
    headers: dict
    budget: int
    sem: asyncio.Semaphore
    resolver: _Resolver
    rows: dict[str, MlLensResult]
    runs: list[_Run]
    report: LensReport
    out_of_budget: bool = False
    switched_off: bool = False


def _actor_input(urls: list[str]) -> dict[str, Any]:
    return {"image_urls": urls, "language": "es", "retries": 4}


def _max_charge(n: int) -> float:
    return round(n * _USD_PER_PHOTO * 1.5, 4)


def _start_url(n: int) -> str:
    return (f"{ams.API_BASE}/acts/{_ACTOR.replace('/', '~')}/runs?timeout={_ACTOR_TIMEOUT_S}"
            f"&memory={_ACTOR_MEMORY_MB}&build={_BUILD}&maxTotalChargeUsd={_max_charge(n)}")


def _brake_day() -> date | None:
    """El día en que se puso el freno de Google, o None. Si la base no contesta: sin freno (el cupo es fail-closed)."""
    try:
        with Session(engine) as s:
            row = s.get(Setting, BRAKE_KEY)
        return date.fromisoformat(row.value) if row else None
    except Exception as exc:  # noqa: BLE001
        log.warning("Google Lens: no se pudo leer el freno (%s)", type(exc).__name__)
        return None


def _set_brake(day: date | None) -> None:
    try:
        with Session(engine) as s:
            row = s.get(Setting, BRAKE_KEY)
            if day is None:
                if row is not None:
                    s.delete(row)
            elif row is None:
                s.add(Setting(key=BRAKE_KEY, value=day.isoformat()))
            else:
                row.value = day.isoformat()
            s.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("Google Lens: no se pudo guardar el freno (%s)", type(exc).__name__)


async def _notify_brake(day: date) -> None:
    retry = day + timedelta(days=_BRAKE_SKIP_NIGHTS + 1)
    try:
        await notify(
            "Hugo: Google frenó Google Lens",
            f"Google contestó {_BLOCK_STREAK} veces seguidas con un freno (429, /sorry, consent u otro error) al resolver "
            f"los links de Google Lens. Las próximas {_BRAKE_SKIP_NIGHTS} noches no se lanza el actor de Lens (las "
            f"búsquedas de ML siguen); el {retry.isoformat()} se prueba con {_PROBE_PHOTOS} fotos y, si Google ya no "
            "frena, vuelve solo. Para apagarlo del todo: pm_lens_enabled = 0.")
    except Exception as exc:  # noqa: BLE001
        log.warning("Google Lens: no se pudo avisar el freno (%s)", type(exc).__name__)


async def run_step(http: httpx.AsyncClient, headers: dict) -> LensReport:
    """El paso Lens de la noche. Nunca lanza y nunca tarda más que _STEP_DEADLINE_S (+ los abort)."""
    report = LensReport()
    if not is_enabled():
        report.reason = "apagado (pm_lens_enabled = 0)"
        return report
    report.status = "ok"
    try:
        await _run(http, headers, report)
    except Exception as exc:  # noqa: BLE001
        report.status, report.reason = "error", f"error inesperado {type(exc).__name__}"
        log.warning("Google Lens: el paso se cortó por %s", type(exc).__name__)
    log.info("Google Lens: %s%s — %d fotos (%d con consulta, %d sin links, %d con error), %d links de ML, USD %.4f",
             report.status, f" ({report.reason})" if report.reason else "", report.products, report.with_queries,
             report.sin_links, report.errors, report.ml_links, report.usd)
    return report


async def _run(http: httpx.AsyncClient, headers: dict, report: LensReport) -> None:
    today = utcnow().date()
    brake = await asyncio.to_thread(_brake_day)
    if brake is not None and today <= brake + timedelta(days=_BRAKE_SKIP_NIGHTS):
        retry = brake + timedelta(days=_BRAKE_SKIP_NIGHTS + 1)
        report.status = "stopped"
        report.reason = (f"Google frenó los goto el {brake.isoformat()}: no se lanza Lens hasta el "
                         f"{retry.isoformat()} (prueba con {_PROBE_PHOTOS} fotos)")
        log.warning("Google Lens: %s", report.reason)
        return
    budget = daily_max()
    left = budget - await asyncio.to_thread(daily_budget.used_today, COUNTER_KEY)
    if left <= 0:
        report.status, report.reason = "over_budget", f"cupo diario agotado ({budget})"
        return
    probe = brake is not None              # pasaron las noches salteadas: pocas fotos para ver si Google sigue frenando
    picked = await asyncio.to_thread(eligible, min(left, _PROBE_PHOTOS) if probe else left)
    if not picked:
        report.reason = "no hay productos para lensear"
        return
    level = await ams._actor_permission(http, _ACTOR, headers)
    if level != ams._REQUIRED_PERMISSION:
        report.status = "stopped"
        report.reason = (f"el actor corre con permisos {level}, se exige {ams._REQUIRED_PERMISSION}" if level
                         else "no se pudo confirmar que el actor corra con permisos limitados")
        log.warning("Google Lens: %s", report.reason)
        return
    step = _Step(http, headers, budget, asyncio.Semaphore(_PARALLEL_RUNS), _Resolver(http), {}, [], report)
    batches = [picked[i:i + _PHOTOS_PER_RUN] for i in range(0, len(picked), _PHOTOS_PER_RUN)]
    try:
        await asyncio.wait_for(asyncio.gather(*(_one_run(step, b) for b in batches)), timeout=_STEP_DEADLINE_S)
    except asyncio.TimeoutError:
        report.reason = f"no terminó en {_STEP_DEADLINE_S // 60} min: lo pendiente queda con error"
        log.warning("Google Lens: %s", report.reason)
    finally:
        await asyncio.gather(*(ams._abort(http, r, headers) for r in step.runs if r.run_id and not r.finished))
        await asyncio.to_thread(_save, list(step.rows.values()))
    if step.switched_off and not report.reason:
        report.status, report.reason = "stopped", "se apagó durante el paso (pm_lens_enabled = 0)"
    elif step.out_of_budget and not report.reason:
        report.status, report.reason = "over_budget", f"cupo diario agotado ({budget}) o no se pudo reservar"
    report.resolve_blocked = step.resolver.blocked
    if step.resolver.blocked:
        await asyncio.to_thread(_set_brake, today)
        await _notify_brake(today)
    elif probe and step.rows:
        await asyncio.to_thread(_set_brake, None)
        log.info("Google Lens: la prueba después del freno pasó, se suelta el freno")
    for row in step.rows.values():
        report.products += 1
        report.matches += row.n_matches
        report.ml_links += row.n_ml_links
        report.usd += row.usd
        if row.status == ST_OK:
            report.with_queries += 1
        elif row.status == ST_SIN_LINKS:
            report.sin_links += 1
        else:
            report.errors += 1
    report.suspicious = report.matches > 0 and report.ml_links == 0
    if report.suspicious:
        log.warning("Google Lens: %d matches y ningún link de ML resuelto: revisar los goto de Google", report.matches)


async def _one_run(step: _Step, batch: list[tuple[str, str, str]]) -> None:
    """Una corrida del actor para un lote de fotos. Nunca lanza: lo que no termina queda con error."""
    async with step.sem:
        # Prendido y tope se releen en cada lote (como la búsqueda de ML): apagarlo o bajarlo corta esta misma noche.
        runtime.invalidate()
        if not is_enabled():
            step.switched_off = True
            return
        granted = await asyncio.to_thread(daily_budget.reserve_up_to, COUNTER_KEY, daily_max(), len(batch))
        if granted < len(batch):
            step.out_of_budget = True
        batch = batch[:granted]
        if not batch:
            return
        now = utcnow().replace(microsecond=0)
        # Peor caso de entrada: si la corrida falla o no llega, cada foto queda con error y se imputa entera.
        for pid, _, _ in batch:
            step.rows[pid] = MlLensResult(product_id=pid, ran_at=now, status=ST_ERROR, usd=_USD_PER_PHOTO)
        run = _Run(n=len(batch))
        step.runs.append(run)
        try:
            items = await _drive(step, run, [url for _, url, _ in batch])
        except Exception as exc:  # noqa: BLE001
            log.warning("Google Lens: la corrida falló (%s)", type(exc).__name__)
            items = None
        if run.usage_usd > _max_charge(len(batch)):
            log.warning("Google Lens: la corrida %s informa USD %.4f (más que el techo de %.4f)",
                        run.run_id, run.usage_usd, _max_charge(len(batch)))
            step.report.overcharged.append({"run_id": run.run_id, "usage_usd": round(run.usage_usd, 6)})
        if items is None:
            return
        by_index = _by_input_index(items, len(batch))
        with_matches = sum(1 for item in by_index.values() if _matches(item))
        per_photo = round(max(run.usage_usd, with_matches * _USD_PER_PHOTO) / len(batch), 6)
        await asyncio.gather(*(_photo(step, pid, name, by_index.get(i), per_photo)
                               for i, (pid, _, name) in enumerate(batch)))


async def _drive(step: _Step, run: _Run, urls: list[str]) -> list | None:
    """Lanzar, esperar y leer el dataset (como apify_ml_search._drive). None si algo falló."""
    status, body = await _request_bounded(step.http, "POST", _start_url(len(urls)), step.headers, _actor_input(urls))
    if status not in (200, 201):
        log.warning("Google Lens: HTTP %s al lanzar (%s)", status, ams._STATUS_REASON.get(status, "error"))
        return None
    if not run.absorb(_json(body)):
        return None
    errors = 0
    while not run.finished:
        status, body = await _request_bounded(
            step.http, "GET", f"{ams.API_BASE}/actor-runs/{run.run_id}?waitForFinish={ams._POLL_WAIT_S}", step.headers)
        if status in (200, 201) and run.absorb(_json(body)):
            errors = 0
        else:
            errors += 1
            if errors >= ams._MAX_POLL_ERRORS:
                return None
        if not run.finished:
            await asyncio.sleep(ams._POLL_INTERVAL_S)
    if run.status != "SUCCEEDED" or not run.dataset_id:
        log.warning("Google Lens: la corrida %s terminó %s", run.run_id, run.status or "sin estado")
        return None
    status, body = await _request_bounded(
        step.http, "GET", f"{ams.API_BASE}/datasets/{run.dataset_id}/items?clean=true&format=json&limit={len(urls) * 2}",
        step.headers)
    items = _json(body) if status in (200, 201) else None      # más de 2 MB: body None → nada se asigna
    return items if isinstance(items, list) else None


def _by_input_index(items: list, n: int) -> dict[int, dict]:
    """Cada ítem a su foto por `input_index` (entero dentro del rango, el primero que llega); lo demás se descarta."""
    out: dict[int, dict] = {}
    for item in items[: n * 2]:
        idx = item.get("input_index") if isinstance(item, dict) else None
        if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < n and idx not in out:
            out[idx] = item
    return out


def _matches(item: dict | None) -> list:
    matches = item.get("matches") if isinstance(item, dict) and not item.get("error") else None
    return matches[:_MAX_MATCHES] if isinstance(matches, list) else []


async def _photo(step: _Step, pid: str, name: str, item: dict | None, usd: float) -> None:
    row = step.rows[pid]
    row.usd = usd
    if item is None or item.get("error"):
        return                                              # Lens no pudo con esta foto: error, se reintenta
    matches = _matches(item)
    row.n_matches = len(matches)
    gotos = _ml_gotos(matches)
    resolved = await asyncio.gather(*(step.resolver.resolve(url) for _, url in gotos))
    links = [(pos, loc) for (pos, _), loc in zip(gotos, resolved) if isinstance(loc, str)]
    row.n_ml_links = len(links)
    queries = pick_queries(links, market_query.query_variants(name, _TEXT_PLAN_VARIANTS) if name else [])
    if not queries and any(r is _FAILED for r in resolved):
        return                                              # sin consulta y con un link sin resolver: error, se reintenta
    row.queries = json.dumps(queries, ensure_ascii=False)
    row.status = ST_OK if queries else ST_SIN_LINKS         # con consulta vale aunque algún otro goto haya fallado


def _save(rows: list[MlLensResult]) -> None:
    """Upsert por producto (`INSERT … ON CONFLICT (product_id) DO UPDATE`, igual en SQLite y Postgres): volver a lensear
    pisa la fila, dos procesos no chocan y una fila que falla no se lleva las demás (ya están pagas). `attempts` cuenta
    los errores seguidos; al MAX_ATTEMPTS-ésimo la fila queda sin_links (60 días)."""
    t = MlLensResult.__table__  # type: ignore[attr-defined]
    with Session(engine) as s:
        for row in rows:
            ins = oficina_ml._insert_stmt()(t).values(
                product_id=row.product_id, ran_at=row.ran_at, status=row.status, queries=row.queries,
                n_matches=row.n_matches, n_ml_links=row.n_ml_links, usd=row.usd,
                attempts=1 if row.status == ST_ERROR else 0)
            failed = ins.excluded.status == ST_ERROR
            stmt = ins.on_conflict_do_update(index_elements=[t.c.product_id], set_={
                "ran_at": ins.excluded.ran_at, "queries": ins.excluded.queries, "n_matches": ins.excluded.n_matches,
                "n_ml_links": ins.excluded.n_ml_links, "usd": ins.excluded.usd,
                "status": case((and_(failed, t.c.attempts + 1 >= MAX_ATTEMPTS), ST_SIN_LINKS), else_=ins.excluded.status),
                "attempts": case((failed, t.c.attempts + 1), else_=0)})
            try:
                s.execute(stmt)
                s.commit()
            except Exception as exc:  # noqa: BLE001
                s.rollback()
                log.warning("Google Lens: no se pudo guardar un resultado (%s)", type(exc).__name__)


def lens_product_ids(queue: list[dict[str, Any]]) -> set[str]:
    """Los items de la cola que buscan una consulta de Google Lens: los del grupo lens y también el refresco (o el rojo)
    de lo que ganó el IDÉNTICO con una. Una consulta es de Lens si está en `ml_lens_result` del producto o si ya se buscó
    para ese producto con origen `lens` (así sigue contando aunque la fila de Lens se haya pisado o borrado). Lo que se
    busque con ellos se guarda con origen `lens`."""
    out = {i["product_id"] for i in queue if i.get("lens")}
    pids = [i["product_id"] for i in queue]
    keys: dict[str, set[str]] = {}
    try:
        with Session(engine) as s:
            for pid, raw in s.exec(select(MlLensResult.product_id, MlLensResult.queries)
                                   .where(MlLensResult.product_id.in_(pids))).all():  # type: ignore[attr-defined]
                keys.setdefault(pid, set()).update(market_query._key(q) for q in oficina_ml.lens_queries(raw))
            for pid, query in s.exec(select(MlWebResult.product_id, MlWebResult.query)
                                     .where(MlWebResult.origin == oficina_ml.ORIGIN_LENS,
                                            MlWebResult.product_id.in_(pids))).all():  # type: ignore[attr-defined]
                keys.setdefault(pid, set()).add(market_query._key(query))
    except Exception as exc:  # noqa: BLE001
        log.warning("Google Lens: no se pudieron leer las consultas Lens (%s)", type(exc).__name__)
        return out
    return out | {i["product_id"] for i in queue if market_query._key(i["queries"][0]) in keys.get(i["product_id"], ())}

