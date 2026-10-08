"""Indexador incremental de las tiendas que se usan como fuente de comparación.

Cada tienda (`market_store`) se lee desde su sitemap, SIN su buscador, y lo que
sale queda en `store_catalog_item`. El semáforo compara contra esa tabla local:
cero requests a la tienda por producto nuestro.

Cómo se porta Hugo con la tienda (todo verificable en `tests/test_store_catalog.py`):

  * robots.txt: se baja, se parsea (con comodines, ver store_robots) y se respeta
    para el sitemap y para CADA página. Nada de URLs con "?" en Gadnic ni de
    /search/ en Tiendanube. Si no se puede bajar (5xx) no se rastrea nada.
  * User-Agent honesto (`STORE_USER_AGENT`), sin navegador y sin proxy: httpx por
    `net_guard.safe_get` (anti-SSRF, cada redirect validado, tope de bytes).
  * Una página cada 2-3 segundos POR TIENDA (más si el robots pide Crawl-delay) y
    un tope diario por tienda (`max_pages_per_day`, contador atómico por día UTC).
  * GET condicional: se manda If-None-Match / If-Modified-Since con lo que la
    tienda dio la vez anterior; un 304 no baja nada.
  * Incremental: primero las URLs nuevas, después las que hace más que no se
    miran, hasta llenar el tope. Con 22.000 URLs y 2.000 por día el catálogo de
    Gadnic rota entero en ~11 días. Las que ya no están en el sitemap dejan de
    leerse.
  * 404/500 (o una página que ya no es un producto) dos veces → `dead`: no se
    vuelve a pedir por 30 días. 429 corta la pasada de esa tienda; tres 403
    seguidos también; errores de red seguidos, también. Nada de eso tira el job.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import and_, delete, func, or_, update
from sqlmodel import Session, select

from app import net_guard
from app.clock import utcnow
from app.config import get_settings
from app.db.models import (
    ImageEmbedCache,
    MarketStore,
    Setting,
    StoreCatalogItem,
    StoreMatch,
    StoreMatchFeedback,
)
from app.db.session import engine
from app.pricing import daily_budget, store_parse, store_robots, store_urls

log = logging.getLogger(__name__)

JOB_ID = "store_index"
PAGES_COUNTER_PREFIX = "_meta:store_pages_today:"
SEEDED_KEY = "_meta:stores_seeded"

_TIMEOUT = httpx.Timeout(25.0, connect=8.0)
# El timeout de httpx es POR CHUNK: un servidor que gotea un byte cada 20 s lo estira para siempre.
# Estos son los topes de punta a punta de cada pedido (si no, una tienda lenta traba el job y el semáforo).
ROBOTS_TIMEOUT_S = 30.0
PAGE_TIMEOUT_S = 60.0
SITEMAP_TIMEOUT_S = 180.0
MAX_CHILD_SITEMAPS = 8
MAX_STORE_URLS = 60_000
MAX_URL_LEN = 500          # VARCHAR(500) de store_catalog_item.url / image_url
# Cortes de la pasada de UNA tienda (no tiran el job: queda dicho en el estado).
MAX_CONSECUTIVE_TRANSIENT = 5
MAX_CONSECUTIVE_403 = 3
# Una tienda que contesta 5xx en TODAS las fichas (y no dio una sola bien en mucho tiempo) está caída:
# se corta la pasada en vez de pedirle 1.000 páginas a un sitio roto.
OUTAGE_STREAK = 25
DEAD_AFTER_FAILS = 2
# Un 404/410 (o una página que ya no es un producto) es una respuesta del sitio: dos veces y listo.
# Un 5xx puede ser una caída: además de dos fallos tienen que pasar estos días desde el primero.
DEAD_MIN_DAYS_5XX = 3
# «Indexar ahora» desde el dashboard no se puede repetir en loop contra el sitio de un tercero.
MANUAL_COOLDOWN_MIN = 10
HEALTH_OK, HEALTH_DEGRADED, HEALTH_DOWN = "ok", "degradada", "caida"

GetFn = Callable[..., Awaitable[httpx.Response]]
SleepFn = Callable[[float], Awaitable[None]]


# ─── Datos ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StoreInfo:
    """Vista inmutable de una fila de `market_store` para usar fuera de la sesión."""
    id: int
    name: str
    base_url: str
    platform: str
    refresh_days: int
    max_pages_per_day: int
    sitemap_url: str
    image_hosts: tuple[str, ...]
    house_brand: str

    @classmethod
    def from_row(cls, row: MarketStore) -> "StoreInfo":
        # Los de la tienda y su plataforma siempre; los extras (CDN) solo los que cargó un admin.
        hosts = tuple(dict.fromkeys([*store_urls.default_image_hosts(row.platform, row.base_url),
                                     *store_urls.parse_hosts(row.image_hosts)]))
        return cls(
            id=int(row.id), name=row.name, base_url=row.base_url.rstrip("/"), platform=row.platform,
            refresh_days=max(1, int(row.refresh_days or 1)), max_pages_per_day=max(1, int(row.max_pages_per_day or 1)),
            sitemap_url=(row.sitemap_url or "").strip(), image_hosts=hosts, house_brand=(row.house_brand or "").strip(),
        )

    @property
    def counter_key(self) -> str:
        return f"{PAGES_COUNTER_PREFIX}{self.id}"


@dataclass(slots=True)
class IndexReport:
    store_id: int
    store: str
    status: str = "ok"            # ok | skipped | aborted | error
    message: str = ""
    sitemap_urls: int = 0
    new_urls: int = 0
    gone_urls: int = 0
    fetched: int = 0
    ok: int = 0
    not_modified: int = 0
    failed: int = 0
    newly_dead: int = 0
    transient: int = 0
    server_errors: int = 0         # fichas que dieron 5xx en esta pasada
    health: str | None = None
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.fetched} leídas ({self.ok} ok, {self.not_modified} sin cambios, {self.failed} fallidas, "
                 f"{self.newly_dead} muertas)"]
        if self.new_urls:
            parts.append(f"{self.new_urls} URLs nuevas")
        if self.gone_urls:
            parts.append(f"{self.gone_urls} ya no están en el sitemap")
        if self.transient:
            parts.append(f"{self.transient} errores de red")
        text = f"{self.status}: " + ", ".join(parts)
        if self.message:
            text += f" · {self.message}"
        return text[:300]


def active_stores() -> list[StoreInfo]:
    with Session(engine) as s:
        rows = s.exec(select(MarketStore).where(MarketStore.enabled.is_(True)).order_by(MarketStore.id)).all()  # type: ignore[attr-defined]
        return [StoreInfo.from_row(r) for r in rows]


def get_store(store_id: int) -> StoreInfo | None:
    with Session(engine) as s:
        row = s.get(MarketStore, store_id)
        return StoreInfo.from_row(row) if row is not None else None


def refresh_allowed_image_hosts() -> None:
    """Carga en `store_urls` los dominios de foto de las tiendas activas (para que el
    juez pueda bajar sus fotos). Una tienda apagada deja de aceptarse."""
    hosts: set[str] = set()
    try:
        for info in active_stores():
            hosts.update(info.image_hosts)
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudieron leer las tiendas para sus hosts de foto: %s", exc)
    store_urls.set_allowed_image_hosts(hosts)


# ─── HTTP educado ────────────────────────────────────────────────────────────


class _Fetcher:
    """Pide páginas de UNA tienda: User-Agent honesto, una pausa entre página y página, un tope de
    tiempo por pedido y, en cada redirect, la comprobación de que sigue en el sitio y no es una URL
    que robots.txt veda (`redirect_ok`)."""

    def __init__(self, info: StoreInfo, *, get: GetFn, sleep: SleepFn, rng: random.Random,
                 delay: tuple[float, float], monotonic: Callable[[], float] = time.monotonic) -> None:
        self.info = info
        self._get, self._sleep, self._rng, self._monotonic = get, sleep, rng, monotonic
        self._delay = delay
        self._ready_at = 0.0
        self.crawl_delay: float | None = None
        self.robots: store_robots.Robots | None = None

    def set_robots(self, robots: store_robots.Robots) -> None:
        self.robots = robots
        self.crawl_delay = robots.crawl_delay

    def redirect_ok(self, url: str) -> bool:
        """Un redirect solo se sigue si cae en el sitio de la tienda y (ya leído robots.txt) si
        robots.txt no lo veda: «ficha → …?utm=1» en Gadnic o «ficha → /search/» en Tiendanube."""
        if not store_urls.safe_link(url, self.info.base_url):
            return False
        return self.robots is None or self.robots.allows(url)

    async def _pace(self) -> None:
        wait = self._ready_at - self._monotonic()
        if wait > 0:
            await self._sleep(wait)

    def _schedule_next(self) -> None:
        low, high = self._delay
        pause = self._rng.uniform(low, high) if high > low else low
        if self.crawl_delay:
            pause = max(pause, self.crawl_delay)
        self._ready_at = self._monotonic() + pause

    async def __call__(self, url: str, *, max_bytes: int, extra: dict[str, str] | None = None,
                       timeout_s: float | None = None) -> httpx.Response:
        headers = {
            "User-Agent": get_settings().store_user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
            "Accept-Language": "es-AR,es;q=0.9",
            **(extra or {}),
        }
        await self._pace()
        try:
            return await asyncio.wait_for(
                self._get(url, timeout=_TIMEOUT, headers=headers, max_bytes=max_bytes, redirect_ok=self.redirect_ok),
                timeout=PAGE_TIMEOUT_S if timeout_s is None else timeout_s)
        finally:
            self._schedule_next()


# ─── Sitemap → URLs de producto ──────────────────────────────────────────────


def _is_product_url(info: StoreInfo, url: str) -> bool:
    """¿Esta URL del sitemap parece una ficha de producto? Tiendanube: /productos/<slug>/.
    JSON-LD: cualquier página limpia que no sea la home (el parser descarta lo que no sea producto)."""
    parts = urlsplit(url)
    if parts.query:
        return False
    path = parts.path or "/"
    if info.platform == store_parse.PLATFORM_TIENDANUBE:
        slug = path[len("/productos/"):].strip("/") if path.startswith("/productos/") else ""
        return bool(slug) and not slug.startswith("page/")
    return path.strip("/") != ""


def _wants_child_sitemap(info: StoreInfo, loc: str) -> bool:
    if "product" in loc.lower():
        return True
    return info.platform == store_parse.PLATFORM_TIENDANUBE


_NETWORK_ERRORS = (httpx.HTTPError, net_guard.SsrfBlocked, net_guard.ResponseTooLarge, net_guard.BadEncoding,
                   net_guard.RedirectBlocked, OSError, asyncio.TimeoutError)


async def _collect_urls(fetch: _Fetcher, info: StoreInfo, robots: store_robots.Robots,
                        report: IndexReport) -> tuple[list[str], bool]:
    """URLs de producto del sitemap, ya filtradas por robots y por tienda. El bool dice si
    el sitemap se leyó COMPLETO (solo así se puede dar por "ya no está" una URL). Las URLs se juntan
    a medida que se lee cada sitemap (y se suelta), con un tope total: un índice con 8 sitemaps de
    100.000 URLs no se retiene entero en memoria."""
    root = info.sitemap_url or f"{info.base_url}/sitemap.xml"
    state = {"complete": True}
    urls: list[str] = []
    seen: set[str] = set()

    async def read(url: str) -> store_parse.Sitemap | None:
        clean = store_urls.safe_link(url, info.base_url)
        if not clean or not robots.allows(clean):
            report.notes.append(f"sitemap fuera de la tienda o vedado por robots: {url[:80]}")
            state["complete"] = False
            return None
        try:
            resp = await fetch(clean, max_bytes=store_parse.MAX_SITEMAP_BYTES, timeout_s=SITEMAP_TIMEOUT_S)
        except _NETWORK_ERRORS as exc:
            report.notes.append(f"sitemap {clean[-60:]}: {type(exc).__name__}")
            state["complete"] = False
            return None
        if resp.status_code != 200:
            report.notes.append(f"sitemap {clean[-60:]}: HTTP {resp.status_code}")
            state["complete"] = False
            return None
        sm = store_parse.parse_sitemap(store_parse.decode_sitemap_body(resp.content), max_urls=MAX_STORE_URLS)
        if sm.truncated:
            state["complete"] = False
        return sm

    def take(sm: store_parse.Sitemap) -> None:
        for loc in sm.locs:
            clean = store_urls.safe_link(loc, info.base_url)
            if not clean or clean in seen or not _is_product_url(info, clean) or not robots.allows(clean):
                continue
            if len(urls) >= MAX_STORE_URLS:
                state["complete"] = False
                return
            seen.add(clean)
            urls.append(clean)

    top = await read(root)
    if top is None:
        return [], False
    if not top.is_index:
        take(top)
        return urls, state["complete"]
    children = [c for c in top.locs if _wants_child_sitemap(info, c)][:MAX_CHILD_SITEMAPS]
    if not children:
        report.notes.append("el sitemap no lista ninguno de productos")
        state["complete"] = False
    for child in children:
        sm = await read(child)
        if sm is not None and not sm.is_index:
            take(sm)
        del sm
    return urls, state["complete"]


def _sync_items(store_id: int, urls: list[str], complete: bool, now: datetime) -> tuple[int, int]:
    """Alta de las URLs nuevas y baja lógica de las que salieron del sitemap. Devuelve
    (nuevas, que ya no están)."""
    wanted = set(urls)
    with Session(engine) as s:
        if s.get(MarketStore, store_id) is None:        # se borró mientras se leía el sitemap
            return 0, 0
        existing = {url: (iid, in_map) for iid, url, in_map in s.exec(
            select(StoreCatalogItem.id, StoreCatalogItem.url, StoreCatalogItem.in_sitemap)
            .where(StoreCatalogItem.store_id == store_id)).all()}
        new = [u for u in urls if u not in existing]
        for i in range(0, len(new), 1000):
            s.add_all([StoreCatalogItem(store_id=store_id, url=u, first_seen_at=now) for u in new[i:i + 1000]])
            s.flush()
        back = [iid for url, (iid, in_map) in existing.items() if url in wanted and not in_map]
        gone = [iid for url, (iid, in_map) in existing.items() if url not in wanted and in_map] if complete and urls else []
        for ids, value in ((back, True), (gone, False)):
            for i in range(0, len(ids), 500):
                s.execute(update(StoreCatalogItem).where(StoreCatalogItem.id.in_(ids[i:i + 500]))  # type: ignore[attr-defined]
                          .values(in_sitemap=value))
        s.commit()
    return len(new), len(gone)


# ─── Qué toca leer ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Due:
    id: int
    url: str
    etag: str
    last_modified: str
    has_data: bool
    fails: int


def pages_used_today(info: StoreInfo) -> int:
    return daily_budget.used_today(info.counter_key)


def _due_filter(refresh_days: int, dead_retry_days: int, now: datetime):
    """Qué toca leer: lo que nunca se leyó, lo que pasó `refresh_days` desde el último intento y lo
    muerto que ya cumplió su descanso. Una ficha que viene fallando se reintenta antes (1, 2 y 4
    días, nunca más que `refresh_days`): así una caída corta se recupera rápido y una muerta de
    verdad se confirma en pocos días en vez de en dos ciclos enteros."""
    def older(days: float):
        return StoreCatalogItem.last_checked_at <= now - timedelta(days=min(days, refresh_days))  # type: ignore[operator]

    alive = StoreCatalogItem.dead.is_(False)  # type: ignore[attr-defined]
    fails = StoreCatalogItem.fails
    return or_(
        and_(alive, or_(StoreCatalogItem.last_checked_at.is_(None),  # type: ignore[union-attr]
                        and_(fails == 0, older(refresh_days)),
                        and_(fails == 1, older(1)), and_(fails == 2, older(2)), and_(fails >= 3, older(4)))),
        and_(StoreCatalogItem.dead.is_(True),  # type: ignore[attr-defined]
             StoreCatalogItem.dead_since < now - timedelta(days=dead_retry_days)),
    )


def count_due(info: StoreInfo, now: datetime | None = None) -> int:
    now = now or utcnow()
    with Session(engine) as s:
        return int(s.exec(
            select(func.count(StoreCatalogItem.id))  # type: ignore[arg-type]
            .where(StoreCatalogItem.store_id == info.id, StoreCatalogItem.in_sitemap.is_(True),  # type: ignore[attr-defined]
                   _due_filter(info.refresh_days, get_settings().store_dead_retry_days, now))
        ).one() or 0)


def _due_items(info: StoreInfo, limit: int, now: datetime) -> list[_Due]:
    """Primero las URLs que nunca se leyeron; después, las que hace más que no se miran."""
    if limit <= 0:
        return []
    with Session(engine) as s:
        rows = s.exec(
            select(StoreCatalogItem)
            .where(StoreCatalogItem.store_id == info.id, StoreCatalogItem.in_sitemap.is_(True),  # type: ignore[attr-defined]
                   _due_filter(info.refresh_days, get_settings().store_dead_retry_days, now))
            .order_by(StoreCatalogItem.last_checked_at.is_(None).desc(),  # type: ignore[union-attr]
                      StoreCatalogItem.last_checked_at.asc(), StoreCatalogItem.id.asc())  # type: ignore[union-attr]
            .limit(limit)
        ).all()
        return [_Due(r.id, r.url, r.etag or "", r.last_modified or "",  # type: ignore[arg-type]
                     has_data=bool(r.title and r.last_seen_at), fails=r.fails or 0) for r in rows]


# ─── Una página ──────────────────────────────────────────────────────────────

OK, NOT_MODIFIED, STRIKE, TRANSIENT, BLOCKED, FORBIDDEN = (
    "ok", "not_modified", "strike", "transient", "blocked", "forbidden")


@dataclass(slots=True)
class _Outcome:
    kind: str
    reason: str = ""
    item: store_parse.ParsedItem | None = None
    image_url: str | None = None
    etag: str = ""
    last_modified: str = ""
    # STRIKE por un 5xx (puede ser una caída de la tienda) y no por una respuesta definitiva.
    soft: bool = False


def _same_site(info: StoreInfo, url: str) -> bool:
    return bool(store_urls.safe_link(url, info.base_url))


async def _read_page(fetch: _Fetcher, info: StoreInfo, due: _Due) -> _Outcome:
    conditional: dict[str, str] = {}
    if due.has_data:
        if due.etag:
            conditional["If-None-Match"] = due.etag
        if due.last_modified:
            conditional["If-Modified-Since"] = due.last_modified
    try:
        resp = await fetch(due.url, max_bytes=store_parse.MAX_PAGE_BYTES, extra=conditional)
    except net_guard.ResponseTooLarge:
        return _Outcome(STRIKE, "página demasiado grande")
    except net_guard.BadEncoding:
        return _Outcome(STRIKE, "contenido comprimido no soportado")
    except net_guard.RedirectBlocked:
        return _Outcome(STRIKE, "redirige fuera del sitio o a una URL vedada")
    except (asyncio.TimeoutError, TimeoutError):
        return _Outcome(TRANSIENT, "tardó demasiado")
    except (httpx.HTTPError, net_guard.SsrfBlocked, OSError) as exc:
        return _Outcome(TRANSIENT, f"{type(exc).__name__}")
    code = resp.status_code
    if code == 304 and due.has_data:
        return _Outcome(NOT_MODIFIED)
    if code == 429:
        return _Outcome(BLOCKED, "HTTP 429")
    if code in (401, 403):
        return _Outcome(FORBIDDEN, f"HTTP {code}")
    if code in (404, 410):
        return _Outcome(STRIKE, f"HTTP {code}")
    if 500 <= code < 600:
        return _Outcome(STRIKE, f"HTTP {code}", soft=True)
    if code != 200:
        return _Outcome(TRANSIENT, f"HTTP {code}")
    if not _same_site(info, str(resp.url)):
        return _Outcome(STRIKE, "redirige a otro sitio")
    parsed = store_parse.parse_product_page(info.platform, resp.text, [due.url, str(resp.url)])
    if parsed is None:
        return _Outcome(STRIKE, "la página no es un producto")
    image = next((u for u in (store_urls.safe_image(c, info.image_hosts) for c in parsed.image_urls) if u), None)
    return _Outcome(OK, item=parsed, image_url=image,
                    etag=(resp.headers.get("etag") or "")[:200],
                    last_modified=(resp.headers.get("last-modified") or "")[:100])


def _clip(value: str | None, limit: int) -> str | None:
    return (value[:limit] or None) if value else None


def _save_outcome(item_id: int, out: _Outcome, now: datetime) -> bool:
    """Guarda lo leído. True si esta lectura dejó la página `dead`. Todo lo que viene del sitio se
    acota a lo que cabe en la columna: un valor largo no puede dar un DataError en Postgres."""
    with Session(engine) as s:
        row = s.get(StoreCatalogItem, item_id)
        if row is None:
            return False
        row.last_checked_at = now
        if out.kind == OK and out.item is not None:
            it = out.item
            row.title, row.sku = _clip(it.title, 300), _clip(it.sku, 80)
            row.price_cents, row.price_doubtful = it.price_cents, it.price_doubtful
            row.price_note = _clip(it.price_note, 200)
            row.brand = _clip(it.brand, 80)
            row.stock = None if it.stock is None else max(0, min(int(it.stock), store_parse.MAX_STOCK))
            row.image_url = out.image_url if out.image_url and len(out.image_url) <= MAX_URL_LEN else None
            row.etag, row.last_modified = _clip(out.etag, 200), _clip(out.last_modified, 100)
            row.last_seen_at, row.fails, row.fail_reason, row.first_fail_at = now, 0, None, None
            row.dead, row.dead_since = False, None
        elif out.kind == NOT_MODIFIED:
            row.last_seen_at, row.fails, row.fail_reason, row.first_fail_at = now, 0, None, None
            row.dead, row.dead_since = False, None
        elif out.kind == STRIKE:
            row.fails = (row.fails or 0) + 1
            row.fail_reason = out.reason[:100]
            if row.first_fail_at is None:
                row.first_fail_at = now
            if row.dead:
                row.dead_since = now          # sigue muerta: otros 30 días
            elif row.fails >= DEAD_AFTER_FAILS and (
                    not out.soft or now - row.first_fail_at >= timedelta(days=_dead_min_days())):
                row.dead, row.dead_since = True, now
                s.add(row)
                s.commit()
                return True
        s.add(row)
        s.commit()
    return False


def _dead_min_days() -> float:
    return float(get_settings().store_dead_min_days_5xx)


def _save_outcome_safe(item_id: int, out: _Outcome, now: datetime) -> bool:
    """`_save_outcome` sin que una ficha rara pueda frenar a la tienda: si el guardado falla se
    anota en la ficha (y queda como leída hoy) y la pasada sigue con la siguiente. Antes un valor
    que no cabía en la columna la dejaba sin marcar y volvía a ser la primera cada noche."""
    try:
        return _save_outcome(item_id, out, now)
    except Exception as exc:  # noqa: BLE001
        log.warning("tiendas: no se pudo guardar la ficha %s (%s)", item_id, type(exc).__name__)
        try:
            with Session(engine) as s:
                row = s.get(StoreCatalogItem, item_id)
                if row is not None:
                    row.last_checked_at, row.fail_reason = now, "no se pudo guardar la ficha"
                    row.fails = (row.fails or 0) + 1
                    s.add(row)
                    s.commit()
        except Exception:  # noqa: BLE001
            log.warning("tiendas: tampoco se pudo marcar la ficha %s", item_id)
        return False


# ─── La pasada de una tienda ─────────────────────────────────────────────────

_locks: dict[int, asyncio.Lock] = {}


def _delay_range() -> tuple[float, float]:
    s = get_settings()
    low = max(0.0, float(s.store_request_delay_min_s))
    return low, max(low, float(s.store_request_delay_max_s))


async def index_store(
    store_id: int, *, max_seconds: float | None = None, max_pages: int | None = None,
    get: GetFn | None = None, sleep: SleepFn | None = None, rng: random.Random | None = None,
    monotonic: Callable[[], float] = time.monotonic, delay: tuple[float, float] | None = None,
) -> IndexReport:
    """Una pasada de indexado de una tienda. Nunca lanza: lo que pasó queda en el
    informe y en `market_store.last_index_status`.

    `max_pages`: tope de esta pasada (además del diario). `max_seconds`: reloj de la
    pasada (el top-up del semáforo no puede demorar la corrida)."""
    info = await asyncio.to_thread(get_store, store_id)
    if info is None:
        return IndexReport(store_id, "?", "skipped", "la tienda no existe")
    lock = _locks.setdefault(store_id, asyncio.Lock())
    if lock.locked():
        return IndexReport(store_id, info.name, "skipped", "ya hay una pasada en curso")
    report = IndexReport(store_id, info.name)
    async with lock:
        try:
            await _index(info, report, max_seconds=max_seconds, max_pages=max_pages,
                         get=get or net_guard.safe_get, sleep=sleep or asyncio.sleep,
                         rng=rng or random.Random(), monotonic=monotonic, delay=delay or _delay_range())
        except Exception as exc:  # noqa: BLE001  (un bug no tumba a las demás tiendas)
            log.exception("indexado de %s reventó", info.name)
            report.status, report.message = "error", f"{type(exc).__name__}: {str(exc)[:120]}"
        await asyncio.to_thread(_record_status, store_id, report)
    log.info("tiendas: %s → %s", info.name, report.summary())
    return report


def _record_status(store_id: int, report: IndexReport) -> None:
    if report.status == "skipped":
        return
    with Session(engine) as s:
        row = s.get(MarketStore, store_id)
        if row is not None:
            row.last_indexed_at = utcnow()
            row.last_index_status = report.summary()
            if report.health:
                row.health = report.health
            s.add(row)
            s.commit()


def _store_active(store_id: int) -> bool:
    """¿La tienda sigue existiendo y prendida? Se mira antes de cada página: borrarla o apagarla
    desde el dashboard frena la pasada en curso en vez de seguir pidiéndole páginas."""
    with Session(engine) as s:
        row = s.get(MarketStore, store_id)
        return row is not None and bool(row.enabled)


def _has_recent_success(store_id: int, now: datetime, days: int) -> bool:
    with Session(engine) as s:
        last = s.exec(select(func.max(StoreCatalogItem.last_seen_at)).where(StoreCatalogItem.store_id == store_id)).one()
    return last is not None and now - last < timedelta(days=days)


async def _index(info: StoreInfo, report: IndexReport, *, max_seconds: float | None, max_pages: int | None,
                 get: GetFn, sleep: SleepFn, rng: random.Random, monotonic: Callable[[], float],
                 delay: tuple[float, float]) -> None:
    deadline = None if max_seconds is None else monotonic() + max_seconds
    fetch = _Fetcher(info, get=get, sleep=sleep, rng=rng, delay=delay, monotonic=monotonic)

    # 0) el cupo del día, ANTES de bajar nada: con el tope gastado no se le piden ni robots ni sitemaps
    # (cada pasada bajaba hasta 9 sitemaps de 25 MB aunque no fuera a leer una sola ficha).
    quota_left = max(0, info.max_pages_per_day - await asyncio.to_thread(pages_used_today, info))
    limit = min(quota_left, max_pages) if max_pages is not None else quota_left
    if limit <= 0:
        report.message = "tope diario de páginas alcanzado"
        return

    # 1) robots.txt (un 429, 401, 403 o 5xx = no se rastrea)
    try:
        resp = await fetch(f"{info.base_url}/robots.txt", max_bytes=store_robots.MAX_ROBOTS_BYTES,
                           timeout_s=ROBOTS_TIMEOUT_S)
        robots = store_robots.from_status(resp.status_code, resp.text, get_settings().store_user_agent)
        if robots.blocked_all:
            report.status, report.message = "aborted", f"robots.txt devolvió HTTP {resp.status_code}: no se rastrea"
            return
    except _NETWORK_ERRORS as exc:
        report.status, report.message = "aborted", f"no se pudo leer robots.txt ({type(exc).__name__}): no se rastrea"
        return
    fetch.set_robots(robots)

    # 2) sitemap → altas y bajas
    urls, complete = await _collect_urls(fetch, info, robots, report)
    report.sitemap_urls = len(urls)
    now = utcnow()
    if urls or complete:
        report.new_urls, report.gone_urls = await asyncio.to_thread(_sync_items, info.id, urls, complete, now)
    if not urls:
        report.message = "; ".join(report.notes[:2]) or "el sitemap no trae productos"
        report.status = "aborted" if report.notes else report.status

    # 3) leer lo que toca, hasta llenar el cupo
    due = await asyncio.to_thread(_due_items, info, limit, now)
    recent_success = await asyncio.to_thread(_has_recent_success, info.id, now, info.refresh_days)
    transient_streak = forbidden_streak = server_error_streak = 0
    for item in due:
        if deadline is not None and monotonic() >= deadline:
            report.message = report.message or "se acabó el tiempo de esta pasada"
            break
        if not await asyncio.to_thread(_store_active, info.id):
            report.status, report.message = "aborted", "la tienda se borró o se apagó mientras se leía: se corta"
            break
        if not robots.allows(item.url):
            await asyncio.to_thread(_save_outcome_safe, item.id, _Outcome(STRIKE, "robots.txt lo prohíbe"), utcnow())
            continue
        if await daily_budget.reserve_async(info.counter_key, info.max_pages_per_day) is None:
            report.message = report.message or "tope diario de páginas alcanzado"
            break
        out = await _read_page(fetch, info, item)
        report.fetched += 1
        transient_streak = transient_streak + 1 if out.kind == TRANSIENT else 0
        forbidden_streak = forbidden_streak + 1 if out.kind == FORBIDDEN else 0
        server_error = out.kind == STRIKE and out.soft
        server_error_streak = server_error_streak + 1 if server_error else 0
        report.server_errors += int(server_error)
        if out.kind in (OK, NOT_MODIFIED, STRIKE):
            died = await asyncio.to_thread(_save_outcome_safe, item.id, out, utcnow())
            if out.kind == OK:
                report.ok += 1
            elif out.kind == NOT_MODIFIED:
                report.not_modified += 1
            else:
                report.failed += 1
                report.newly_dead += int(died)
        else:
            report.transient += int(out.kind == TRANSIENT)
        if out.kind == BLOCKED:
            report.status, report.message = "aborted", "la tienda contestó 429 (pidió frenar): se corta por hoy"
            break
        if forbidden_streak >= MAX_CONSECUTIVE_403:
            report.status, report.message = "aborted", f"{forbidden_streak} respuestas 403 seguidas: la tienda nos bloquea"
            break
        if transient_streak >= MAX_CONSECUTIVE_TRANSIENT:
            report.status, report.message = "aborted", f"{transient_streak} errores de red seguidos ({out.reason})"
            break
        if server_error_streak >= OUTAGE_STREAK and report.ok == 0 and not recent_success:
            # Ni una ficha bien en esta pasada y ninguna en la última semana: la tienda está caída, no
            # son páginas muertas sueltas (en Gadnic, que tiene muchas muertas, siempre hay alguna viva).
            report.status, report.health = "aborted", HEALTH_DOWN
            report.message = (f"la tienda contesta 5xx en todas las fichas ({server_error_streak} seguidas, ninguna "
                              "bien): parece caída; se corta y no se marcan muertas")
            break
    if report.health is None and report.fetched >= 10 and report.server_errors * 2 >= report.fetched:
        report.health = HEALTH_DEGRADED
    report.health = report.health or HEALTH_OK


# ─── Job y top-up ────────────────────────────────────────────────────────────

_COOLDOWN_HOURS = 6


def _recently_blocked(info: StoreInfo, now: datetime) -> bool:
    with Session(engine) as s:
        row = s.get(MarketStore, info.id)
    return bool(row and row.last_indexed_at and row.last_index_status
                and row.last_index_status.startswith("aborted")
                and now - row.last_indexed_at < timedelta(hours=_COOLDOWN_HOURS))


async def index_all(*, max_seconds: float | None = None, only_if_due: bool = False,
                    **kwargs) -> list[IndexReport]:
    """Indexa todas las tiendas activas, cada una a su ritmo y en paralelo.

    `only_if_due` (el top-up del semáforo): salta las que no tienen nada vencido, ya
    gastaron el tope de hoy o cortaron hace poco (429 / bloqueo / red)."""
    stores = await asyncio.to_thread(active_stores)
    now = utcnow()
    targets: list[StoreInfo] = []
    for info in stores:
        if only_if_due:
            if pages_used_today(info) >= info.max_pages_per_day or await asyncio.to_thread(count_due, info, now) == 0:
                continue
            if await asyncio.to_thread(_recently_blocked, info, now):
                continue
        targets.append(info)
    if not targets:
        return []
    return list(await asyncio.gather(*(index_store(i.id, max_seconds=max_seconds, **kwargs) for i in targets)))


async def topup_for_run(max_minutes: int, **kwargs) -> list[IndexReport]:
    """Al empezar el semáforo: si el índice de alguna tienda está viejo, refresca lo que
    entra en el cupo de hoy, hasta `max_minutes`, antes de comparar. Nunca lanza."""
    if max_minutes <= 0:
        return []
    try:
        return await index_all(max_seconds=max_minutes * 60.0, only_if_due=True, **kwargs)
    except Exception as exc:  # noqa: BLE001
        log.warning("tiendas: el top-up del índice falló: %s", exc)
        return []


# ─── Estado para el dashboard ────────────────────────────────────────────────


def index_status() -> list[dict]:
    """Por tienda: cuánto hay indexado, cuánto vencido, muertas, cupo de hoy y la última pasada."""
    out: list[dict] = []
    with Session(engine) as s:
        stores = s.exec(select(MarketStore).order_by(MarketStore.id)).all()
        for row in stores:
            info = StoreInfo.from_row(row)
            def count(*conds, store_id: int = info.id) -> int:
                return int(s.exec(
                    select(func.count(StoreCatalogItem.id))  # type: ignore[arg-type]
                    .where(StoreCatalogItem.store_id == store_id,
                           StoreCatalogItem.in_sitemap.is_(True), *conds)  # type: ignore[attr-defined]
                ).one() or 0)

            out.append({
                "id": info.id, "name": info.name, "enabled": bool(row.enabled),
                "urls": count(),
                "indexed": count(StoreCatalogItem.dead.is_(False), StoreCatalogItem.last_seen_at.is_not(None)),  # type: ignore[attr-defined,union-attr]
                "dead": count(StoreCatalogItem.dead.is_(True)),  # type: ignore[attr-defined]
                "never_read": count(StoreCatalogItem.last_checked_at.is_(None)),  # type: ignore[union-attr]
                "doubtful_price": count(StoreCatalogItem.price_doubtful.is_(True)),  # type: ignore[attr-defined]
                # Fichas que vienen fallando pero todavía no se dan por muertas (una tienda caída aparece acá).
                "failing": count(StoreCatalogItem.fails > 0, StoreCatalogItem.dead.is_(False)),  # type: ignore[attr-defined]
                "errors_5xx": count(StoreCatalogItem.dead.is_(False), StoreCatalogItem.fails > 0,  # type: ignore[attr-defined]
                                    StoreCatalogItem.fail_reason.like("HTTP 5%")),  # type: ignore[union-attr]
                "health": row.health,
                "pages_today": daily_budget.used_today(info.counter_key),
                "max_pages_per_day": info.max_pages_per_day,
                "last_indexed_at": row.last_indexed_at.isoformat() + "Z" if row.last_indexed_at else None,
                "last_index_status": row.last_index_status,
            })
    return out


# ─── Alta de tiendas: validación, CRUD y semilla ─────────────────────────────

# Orden de las columnas en el dashboard (por id): Mercado Libre | Gadnic | Casa Perfecta.
DEFAULT_STORES: tuple[dict, ...] = (
    {
        "name": "Gadnic", "base_url": "https://www.gadnic.com.ar", "platform": "jsonld_sitemap",
        "refresh_days": 11, "max_pages_per_day": 2000,
        "image_hosts": "gadnic.com.ar,*.bidcom.com.ar", "house_brand": "Gadnic",
        "notes": ("Next.js con JSON-LD. robots prohíbe las URLs con «?» (no se usa su buscador). ~22.000 URLs, la "
                  "mitad muertas (500): a 2.000 por día rota en ~11 días. Su marca propia se trata como genérica."),
    },
    {
        "name": "Casa Perfecta", "base_url": "https://www.casaperfecta.com.ar", "platform": "tiendanube",
        "refresh_days": 7, "max_pages_per_day": 1000,
        "notes": "Tiendanube. robots permite /productos/ y prohíbe /search/. ~150 productos: se lee todo.",
    },
)

_NAME_MAX = 60


class StoreConflict(ValueError):
    """La tienda choca con otra (mismo nombre, mismo sitio) o se pasó el tope de tiendas (HTTP 409)."""


def _clean_base_url(raw: object) -> str:
    try:
        parts = urlsplit(str(raw or "").strip())
        port = parts.port
    except ValueError:
        raise ValueError("la URL de la tienda no es válida") from None
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "https" or not host or parts.username or parts.password or port not in (None, 443):
        raise ValueError("la URL de la tienda tiene que ser https://dominio (sin usuario ni puerto)")
    # Un dominio común: sin IPs, sin «localhost», sin sufijos públicos (com.ar) ni plataformas donde vive
    # cualquiera (amazonaws.com, github.io…). Con eso base_url cabe en su columna y safe_link no se abre.
    if not store_urls.valid_hostname(host) or not store_urls.valid_hostname(store_urls.apex(host)):
        raise ValueError("la URL de la tienda no tiene un dominio válido (o es demasiado amplio)")
    return f"https://{host}"


def clean_store_fields(data: dict, *, partial: bool = False, current: dict | None = None) -> dict:
    """Valida lo que carga una persona desde el dashboard. ValueError con un mensaje que se puede
    mostrar tal cual. `current` (al editar): `base_url` y `platform` de la fila, contra los que se
    validan el sitemap y los dominios de foto si el pedido no los trae."""
    out: dict = {}
    current = current or {}

    def has(key: str) -> bool:
        return key in data and (not partial or data[key] is not None)

    if has("name") or not partial:
        name = " ".join(str(data.get("name") or "").split())
        if not 1 <= len(name) <= _NAME_MAX:
            raise ValueError(f"el nombre tiene que tener entre 1 y {_NAME_MAX} caracteres")
        out["name"] = name
    if has("base_url") or not partial:
        out["base_url"] = _clean_base_url(data.get("base_url"))
    if has("platform") or not partial:
        platform = str(data.get("platform") or "")
        if platform not in store_parse.PLATFORMS:
            raise ValueError("plataforma inválida: " + " | ".join(store_parse.PLATFORMS))
        out["platform"] = platform
    for key, low, high in (("refresh_days", 1, 90), ("max_pages_per_day", 1, 20000)):
        if has(key) or not partial:
            try:
                value = int(data.get(key, 7 if key == "refresh_days" else 1000))
            except (TypeError, ValueError):
                raise ValueError(f"{key} tiene que ser un número entero") from None
            if not low <= value <= high:
                raise ValueError(f"{key} tiene que estar entre {low} y {high}")
            out[key] = value
    if "enabled" in data and data["enabled"] is not None:
        out["enabled"] = bool(data["enabled"])
    base = out.get("base_url") or current.get("base_url") or ""
    platform = out.get("platform") or current.get("platform") or ""
    if "sitemap_url" in data:
        raw = str(data["sitemap_url"] or "").strip()
        out["sitemap_url"] = raw[:300] or None
        # El sitemap, si lo dan, tiene que ser https y del mismo sitio que la tienda (también al editar
        # solo el sitemap: se compara con la dirección que ya tiene la fila).
        if out["sitemap_url"] and not (base and store_urls.safe_link(out["sitemap_url"], base)):
            raise ValueError("el sitemap tiene que ser https y del mismo dominio que la tienda")
    if "image_hosts" in data:
        raw = str(data["image_hosts"] or "").strip()
        bad = store_urls.invalid_hosts(raw)
        if bad:
            raise ValueError("dominios de foto no válidos: " + ", ".join(bad))
        hosts = store_urls.parse_hosts(raw)
        trusted = get_settings().store_trusted_image_hosts
        not_allowed = [h for h in hosts if not store_urls.extra_host_allowed(h, base, platform, trusted)]
        if not_allowed:
            raise ValueError("estos dominios de foto no son de la tienda ni de su plataforma, y solo un administrador "
                             "puede autorizarlos (STORE_TRUSTED_IMAGE_HOSTS): " + ", ".join(not_allowed))
        out["image_hosts"] = ",".join(hosts) or None
    if "house_brand" in data:
        out["house_brand"] = " ".join(str(data["house_brand"] or "").split())[:60] or None
    if "notes" in data:
        out["notes"] = " ".join(str(data["notes"] or "").split())[:500] or None
    return out


def check_conflicts(session: Session, fields: dict, *, exclude_id: int | None = None, creating: bool = False) -> None:
    """Nombre único sin importar mayúsculas, un solo registro por sitio (dos tiendas con la misma
    dirección serían dos rastreadores contra el mismo tercero) y un tope de tiendas. StoreConflict."""
    rows = session.exec(select(MarketStore)).all()
    others = [r for r in rows if r.id != exclude_id]
    if creating and len(rows) >= get_settings().store_max_stores:
        raise StoreConflict(f"ya hay {len(rows)} tiendas cargadas (el tope es {get_settings().store_max_stores})")
    name = fields.get("name")
    if name and any(r.name.casefold() == name.casefold() for r in others):
        raise StoreConflict("ya hay una tienda con ese nombre")
    base = fields.get("base_url")
    if base and any(store_urls.apex(store_urls.host_of(r.base_url)) == store_urls.apex(store_urls.host_of(base))
                    for r in others):
        raise StoreConflict("ya hay una tienda con esa dirección")


def store_to_dict(row: MarketStore) -> dict:
    return {
        "id": row.id, "name": row.name, "base_url": row.base_url, "platform": row.platform,
        "enabled": bool(row.enabled), "refresh_days": row.refresh_days, "max_pages_per_day": row.max_pages_per_day,
        "sitemap_url": row.sitemap_url, "image_hosts": row.image_hosts, "house_brand": row.house_brand,
        "notes": row.notes, "health": row.health,
        "last_indexed_at": row.last_indexed_at.isoformat() + "Z" if row.last_indexed_at else None,
        "last_index_status": row.last_index_status,
    }


def seed_default_stores() -> int:
    """Siembra Gadnic y Casa Perfecta UNA sola vez (la marca queda en `settings`): si después
    alguien las borra a propósito, no reaparecen al reiniciar."""
    with Session(engine) as s:
        if s.get(Setting, SEEDED_KEY) is not None:
            return 0
        added = 0
        existing = {r.name.casefold() for r in s.exec(select(MarketStore)).all()}
        for spec in DEFAULT_STORES:
            if spec["name"].casefold() not in existing:
                s.add(MarketStore(**spec))
                added += 1
        s.add(Setting(key=SEEDED_KEY, value="1"))
        s.commit()
    if added:
        log.info("tiendas: sembradas %d tiendas por defecto", added)
    return added


def all_image_host_entries() -> set[str]:
    """Las entradas de hosts de foto de TODAS las tiendas (prendidas o no): para podar el cache de
    embeddings de las que se apagaron."""
    with Session(engine) as s:
        return {h for r in s.exec(select(MarketStore)).all() for h in StoreInfo.from_row(r).image_hosts}


def delete_store(store_id: int) -> bool:
    """Borra la tienda y todo lo que se guardó de ella: catálogo, coincidencias, correcciones y los
    embeddings de sus fotos (salvo los de un CDN que comparte con otra tienda). Una pasada que la
    esté leyendo se corta sola en la página siguiente (`_store_active`)."""
    with Session(engine) as s:
        row = s.get(MarketStore, store_id)
        if row is None:
            return False
        mine = set(StoreInfo.from_row(row).image_hosts)
        others = {h for r in s.exec(select(MarketStore).where(MarketStore.id != store_id)).all()
                  for h in StoreInfo.from_row(r).image_hosts}
        for model in (StoreCatalogItem, StoreMatch, StoreMatchFeedback):
            s.execute(delete(model).where(model.store_id == store_id))  # type: ignore[attr-defined]
        for entry in mine - others:
            for pattern in store_urls.like_patterns(entry):
                s.execute(delete(ImageEmbedCache).where(ImageEmbedCache.url.like(pattern, escape="\\")))  # type: ignore[attr-defined]
        s.delete(row)
        s.commit()
    return True
