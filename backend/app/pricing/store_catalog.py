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
from sqlalchemy import delete, func, update
from sqlmodel import Session, select

from app import net_guard
from app.clock import utcnow
from app.config import get_settings
from app.db.models import (
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
MAX_CHILD_SITEMAPS = 8
MAX_STORE_URLS = 60_000
# Cortes de la pasada de UNA tienda (no tiran el job: queda dicho en el estado).
MAX_CONSECUTIVE_TRANSIENT = 5
MAX_CONSECUTIVE_403 = 3
DEAD_AFTER_FAILS = 2

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
        hosts = store_urls.parse_hosts(row.image_hosts) or store_urls.default_image_hosts(row.platform, row.base_url)
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
    """Pide páginas de UNA tienda: User-Agent honesto y una pausa entre página y página."""

    def __init__(self, info: StoreInfo, *, get: GetFn, sleep: SleepFn, rng: random.Random,
                 delay: tuple[float, float], monotonic: Callable[[], float] = time.monotonic) -> None:
        self.info = info
        self._get, self._sleep, self._rng, self._monotonic = get, sleep, rng, monotonic
        self._delay = delay
        self._ready_at = 0.0
        self.crawl_delay: float | None = None

    def set_robots_delay(self, seconds: float | None) -> None:
        self.crawl_delay = seconds

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

    async def __call__(self, url: str, *, max_bytes: int, extra: dict[str, str] | None = None) -> httpx.Response:
        headers = {
            "User-Agent": get_settings().store_user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
            "Accept-Language": "es-AR,es;q=0.9",
            **(extra or {}),
        }
        await self._pace()
        try:
            return await self._get(url, timeout=_TIMEOUT, headers=headers, max_bytes=max_bytes)
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


async def _collect_urls(fetch: _Fetcher, info: StoreInfo, robots: store_robots.Robots,
                        report: IndexReport) -> tuple[list[str], bool]:
    """URLs de producto del sitemap, ya filtradas por robots y por tienda. El bool dice si
    el sitemap se leyó COMPLETO (solo así se puede dar por "ya no está" una URL)."""
    root = info.sitemap_url or f"{info.base_url}/sitemap.xml"
    complete = True

    async def read(url: str) -> store_parse.Sitemap | None:
        nonlocal complete
        clean = store_urls.safe_link(url, info.base_url)
        if not clean or not robots.allows(clean):
            report.notes.append(f"sitemap fuera de la tienda o vedado por robots: {url[:80]}")
            complete = False
            return None
        try:
            resp = await fetch(clean, max_bytes=store_parse.MAX_SITEMAP_BYTES)
        except (httpx.HTTPError, net_guard.SsrfBlocked, net_guard.ResponseTooLarge, OSError) as exc:
            report.notes.append(f"sitemap {clean[-60:]}: {type(exc).__name__}")
            complete = False
            return None
        if resp.status_code != 200:
            report.notes.append(f"sitemap {clean[-60:]}: HTTP {resp.status_code}")
            complete = False
            return None
        sm = store_parse.parse_sitemap(store_parse.decode_sitemap_body(resp.content))
        complete = complete and not sm.truncated
        return sm

    top = await read(root)
    if top is None:
        return [], False
    maps = [top]
    if top.is_index:
        children = [c for c in top.locs if _wants_child_sitemap(info, c)][:MAX_CHILD_SITEMAPS]
        if not children:
            report.notes.append("el sitemap no lista ninguno de productos")
            complete = False
        maps = []
        for child in children:
            sm = await read(child)
            if sm is not None and not sm.is_index:
                maps.append(sm)
    urls: list[str] = []
    seen: set[str] = set()
    for sm in maps:
        for loc in sm.locs:
            clean = store_urls.safe_link(loc, info.base_url)
            if not clean or clean in seen or not _is_product_url(info, clean) or not robots.allows(clean):
                continue
            seen.add(clean)
            urls.append(clean)
            if len(urls) >= MAX_STORE_URLS:
                complete = False
                return urls, complete
    return urls, complete


def _sync_items(store_id: int, urls: list[str], complete: bool, now: datetime) -> tuple[int, int]:
    """Alta de las URLs nuevas y baja lógica de las que salieron del sitemap. Devuelve
    (nuevas, que ya no están)."""
    wanted = set(urls)
    with Session(engine) as s:
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
    from sqlalchemy import and_, or_

    return or_(
        and_(StoreCatalogItem.dead.is_(False),  # type: ignore[attr-defined]
             or_(StoreCatalogItem.last_checked_at.is_(None),  # type: ignore[union-attr]
                 StoreCatalogItem.last_checked_at < now - timedelta(days=refresh_days))),
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


def _same_site(info: StoreInfo, url: str) -> bool:
    host = store_urls.host_of(url)
    domain = store_urls.apex(store_urls.host_of(info.base_url))
    return bool(host) and bool(domain) and (host == domain or host.endswith("." + domain))


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
    except (httpx.HTTPError, net_guard.SsrfBlocked, OSError) as exc:
        return _Outcome(TRANSIENT, f"{type(exc).__name__}")
    code = resp.status_code
    if code == 304 and due.has_data:
        return _Outcome(NOT_MODIFIED)
    if code == 429:
        return _Outcome(BLOCKED, "HTTP 429")
    if code in (401, 403):
        return _Outcome(FORBIDDEN, f"HTTP {code}")
    if code in (404, 410) or 500 <= code < 600:
        return _Outcome(STRIKE, f"HTTP {code}")
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


def _save_outcome(item_id: int, out: _Outcome, now: datetime) -> bool:
    """Guarda lo leído. True si esta lectura dejó la página `dead`."""
    with Session(engine) as s:
        row = s.get(StoreCatalogItem, item_id)
        if row is None:
            return False
        row.last_checked_at = now
        if out.kind == OK and out.item is not None:
            it = out.item
            row.title, row.sku = it.title, (it.sku or None)
            row.price_cents, row.price_doubtful = it.price_cents, it.price_doubtful
            row.price_note = (it.price_note or None) and it.price_note[:200]
            row.brand, row.stock = (it.brand or None), it.stock
            row.image_url = out.image_url
            row.etag, row.last_modified = (out.etag or None), (out.last_modified or None)
            row.last_seen_at, row.fails, row.fail_reason = now, 0, None
            row.dead, row.dead_since = False, None
        elif out.kind == NOT_MODIFIED:
            row.last_seen_at, row.fails, row.fail_reason = now, 0, None
            row.dead, row.dead_since = False, None
        elif out.kind == STRIKE:
            row.fails = (row.fails or 0) + 1
            row.fail_reason = out.reason[:100]
            if row.fails >= DEAD_AFTER_FAILS and not row.dead:
                row.dead, row.dead_since = True, now
                s.add(row)
                s.commit()
                return True
            if row.dead:
                row.dead_since = now          # sigue muerta: otros 30 días
        s.add(row)
        s.commit()
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
            s.add(row)
            s.commit()


async def _index(info: StoreInfo, report: IndexReport, *, max_seconds: float | None, max_pages: int | None,
                 get: GetFn, sleep: SleepFn, rng: random.Random, monotonic: Callable[[], float],
                 delay: tuple[float, float]) -> None:
    deadline = None if max_seconds is None else monotonic() + max_seconds
    fetch = _Fetcher(info, get=get, sleep=sleep, rng=rng, delay=delay, monotonic=monotonic)

    # 1) robots.txt
    try:
        resp = await fetch(f"{info.base_url}/robots.txt", max_bytes=store_robots.MAX_ROBOTS_BYTES)
        robots = store_robots.from_status(resp.status_code, resp.text, get_settings().store_user_agent)
        if robots.blocked_all:
            report.status, report.message = "aborted", f"robots.txt devolvió HTTP {resp.status_code}: no se rastrea"
            return
    except (httpx.HTTPError, net_guard.SsrfBlocked, net_guard.ResponseTooLarge, OSError) as exc:
        report.status, report.message = "aborted", f"no se pudo leer robots.txt ({type(exc).__name__}): no se rastrea"
        return
    fetch.set_robots_delay(robots.crawl_delay)

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
    quota_left = max(0, info.max_pages_per_day - await asyncio.to_thread(pages_used_today, info))
    limit = min(quota_left, max_pages) if max_pages is not None else quota_left
    if limit <= 0:
        report.message = report.message or "tope diario de páginas alcanzado"
        return
    due = await asyncio.to_thread(_due_items, info, limit, now)
    transient_streak = forbidden_streak = 0
    for item in due:
        if deadline is not None and monotonic() >= deadline:
            report.message = report.message or "se acabó el tiempo de esta pasada"
            break
        if not robots.allows(item.url):
            await asyncio.to_thread(_save_outcome, item.id, _Outcome(STRIKE, "robots.txt lo prohíbe"), utcnow())
            continue
        if await daily_budget.reserve_async(info.counter_key, info.max_pages_per_day) is None:
            report.message = report.message or "tope diario de páginas alcanzado"
            break
        out = await _read_page(fetch, info, item)
        report.fetched += 1
        transient_streak = transient_streak + 1 if out.kind == TRANSIENT else 0
        forbidden_streak = forbidden_streak + 1 if out.kind == FORBIDDEN else 0
        if out.kind in (OK, NOT_MODIFIED, STRIKE):
            died = await asyncio.to_thread(_save_outcome, item.id, out, utcnow())
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
                "pages_today": daily_budget.used_today(info.counter_key),
                "max_pages_per_day": info.max_pages_per_day,
                "last_indexed_at": row.last_indexed_at.isoformat() + "Z" if row.last_indexed_at else None,
                "last_index_status": row.last_index_status,
            })
    return out


# ─── Alta de tiendas: validación, CRUD y semilla ─────────────────────────────

DEFAULT_STORES: tuple[dict, ...] = (
    {
        "name": "Casa Perfecta", "base_url": "https://www.casaperfecta.com.ar", "platform": "tiendanube",
        "refresh_days": 7, "max_pages_per_day": 1000,
        "notes": "Tiendanube. robots permite /productos/ y prohíbe /search/. ~150 productos: se lee todo.",
    },
    {
        "name": "Gadnic", "base_url": "https://www.gadnic.com.ar", "platform": "jsonld_sitemap",
        "refresh_days": 11, "max_pages_per_day": 2000,
        "image_hosts": "gadnic.com.ar,bidcom.com.ar", "house_brand": "Gadnic",
        "notes": ("Next.js con JSON-LD. robots prohíbe las URLs con «?» (no se usa su buscador). ~22.000 URLs, la "
                  "mitad muertas (500): a 2.000 por día rota en ~11 días. Su marca propia se trata como genérica."),
    },
)

_NAME_MAX = 60


def _clean_base_url(raw: object) -> str:
    parts = urlsplit(str(raw or "").strip())
    if parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError("la URL de la tienda tiene que ser https://dominio (sin usuario ni puerto)")
    if "." not in parts.hostname:
        raise ValueError("la URL de la tienda no tiene un dominio válido")
    return f"https://{parts.hostname.lower()}"


def clean_store_fields(data: dict, *, partial: bool = False) -> dict:
    """Valida lo que carga una persona desde el dashboard. ValueError con un mensaje
    que se puede mostrar tal cual."""
    out: dict = {}

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
    if "sitemap_url" in data:
        raw = str(data["sitemap_url"] or "").strip()
        out["sitemap_url"] = raw[:300] or None
    if "image_hosts" in data:
        raw = str(data["image_hosts"] or "").strip()
        bad = store_urls.invalid_hosts(raw)
        if bad:
            raise ValueError("dominios de foto no válidos: " + ", ".join(bad))
        out["image_hosts"] = ",".join(store_urls.parse_hosts(raw)) or None
    if "house_brand" in data:
        out["house_brand"] = " ".join(str(data["house_brand"] or "").split())[:60] or None
    if "notes" in data:
        out["notes"] = " ".join(str(data["notes"] or "").split())[:500] or None
    # El sitemap, si lo dan, tiene que ser de la misma tienda.
    base = out.get("base_url")
    if out.get("sitemap_url") and base and not store_urls.safe_link(out["sitemap_url"], base):
        raise ValueError("el sitemap tiene que ser https y del mismo dominio que la tienda")
    return out


def store_to_dict(row: MarketStore) -> dict:
    return {
        "id": row.id, "name": row.name, "base_url": row.base_url, "platform": row.platform,
        "enabled": bool(row.enabled), "refresh_days": row.refresh_days, "max_pages_per_day": row.max_pages_per_day,
        "sitemap_url": row.sitemap_url, "image_hosts": row.image_hosts, "house_brand": row.house_brand,
        "notes": row.notes,
        "last_indexed_at": row.last_indexed_at.isoformat() + "Z" if row.last_indexed_at else None,
        "last_index_status": row.last_index_status,
    }


def seed_default_stores() -> int:
    """Siembra Casa Perfecta y Gadnic UNA sola vez (la marca queda en `settings`):
    si después alguien las borra a propósito, no reaparecen al reiniciar."""
    with Session(engine) as s:
        if s.get(Setting, SEEDED_KEY) is not None:
            return 0
        added = 0
        for spec in DEFAULT_STORES:
            if s.exec(select(MarketStore.id).where(MarketStore.name == spec["name"])).first() is None:
                s.add(MarketStore(**spec))
                added += 1
        s.add(Setting(key=SEEDED_KEY, value="1"))
        s.commit()
    if added:
        log.info("tiendas: sembradas %d tiendas por defecto", added)
    return added


def delete_store(store_id: int) -> bool:
    """Borra la tienda y todo lo que se guardó de ella (catálogo, coincidencias, correcciones)."""
    with Session(engine) as s:
        row = s.get(MarketStore, store_id)
        if row is None:
            return False
        for model in (StoreCatalogItem, StoreMatch, StoreMatchFeedback):
            s.execute(delete(model).where(model.store_id == store_id))  # type: ignore[attr-defined]
        s.delete(row)
        s.commit()
    return True

