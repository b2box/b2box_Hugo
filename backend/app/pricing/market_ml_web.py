"""Fuente "ML web" del semáforo: buscar el título en listado.mercadolibre.com.ar.

La API de ML solo deja buscar FICHAS de catálogo (`/products/search`); las
publicaciones comunes (`/sites/MLA/search`, `/items/{id}`) dan 403 por política
(ver memoria meli-api-que-endpoints-pasan). Lo importado de China casi nunca
tiene ficha, así que ~94 % del catálogo quedaba "sin dato". La web pública de
ML sí lista esas publicaciones: acá se abre la búsqueda con el MISMO navegador
y el MISMO proxy que ya usa Hugo para leer links de ML
(`ingest.browser_fetch.ListingBrowser`) y se lee lo que la página trae
embebido, sin parsear HTML:

  1. `_n.ctx.r = {...}` (script `__NORDIC_RENDERING_CTX__`): el estado con el
     que ML renderiza la página. Cada resultado es una "polycard" con id,
     título, precio en pesos, link, foto, vendedor y ventas.
  2. `<script type="application/ld+json">`: schema.org `Product` con nombre,
     foto, marca, precio y url. Es el respaldo si ML cambia el estado, y de
     ahí sale la MARCA, que la polycard no trae.

Tiene que haber BROWSER_PROXY: ML bloquea la IP del datacenter (memoria
hugo-camoufox-datacenter-ip-bloqueada), así que sin proxy la fuente queda
apagada y ni lo intenta.

Límites que respeta (ver README, "Búsqueda web de ML"):
  * tope diario de búsquedas con reserva atómica (`pm_ml_web_daily_budget`);
  * concurrencia baja y pausa entre búsquedas;
  * un bloqueo (captcha, verificación, 403/429) NO se reintenta: el producto
    queda sin dato con el motivo y la corrida sigue; con N bloqueos seguidos la
    fuente se corta por esa noche;
  * se miden los bytes de cada búsqueda para estimar el costo del proxy.

Todo lo que sale de la página es texto de terceros: ids, links y fotos se
validan con las mismas listas blancas que la API (`market_ml.safe_*`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

from app.config import get_settings
from app.ingest import browser_fetch
from app.pricing import daily_budget
from app.pricing.market_ml import ORIGIN_WEB, MlCandidate, is_click_tracker, safe_image_url, safe_permalink

log = logging.getLogger(__name__)

WEB_COUNTER_KEY = "_meta:pm_ml_web_calls_today"
SEARCH_HOST = "listado.mercadolibre.com.ar"

# Ítems (MLA123…) y productos de usuario (MLAU123…). Las fichas de catálogo
# (MLA123 en /p/) comparten el mismo formato que los ítems.
_REF = re.compile(r"^MLAU?\d+$", re.ASCII)
_PICTURE_ID = re.compile(r"^[0-9A-Za-z_-]{6,80}$", re.ASCII)
_ANY_ML_ID = re.compile(r"MLAU?-?(\d{5,})", re.ASCII)
_BRACES = re.compile(r"\{[^}]*\}")
_WS = re.compile(r"\s+")
_NORDIC_CTX = re.compile(r'<script[^>]*id="__NORDIC_RENDERING_CTX__"[^>]*>', re.I)
_JSON_LD = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.I | re.S)
_CTX_PREFIX = "_n.ctx.r="
# Tope de HTML que se mira. Las páginas reales pesan 2,2 a 3,6 MB sin comprimir y
# el JSON-LD (de donde sale la marca) puede quedar pasando los 3,0 MB: se corta
# más arriba para no perderlo, pero no se parsea una página sin techo.
MAX_HTML_CHARS = 5_000_000
_PICTURE_URL = "https://http2.mlstatic.com/D_NQ_NP_{}-F.jpg"
_MAX_PRICE_CENTS = 10**11

# Marcas de una página anti-bot (la misma idea que image_from_url).
_ANTIBOT_MARKERS = (
    "suspicious-traffic", "captcha", "unusual traffic", "tráfico inusual",
    "are you a robot", "account-verification",
)


# ─── URL de búsqueda ──────────────────────────────────────────────


def slugify(query: str) -> str:
    """Título → slug de ML: palabras en minúscula unidas por guiones. Se sacan
    los acentos de las vocales (ML los ignora) pero NO la ñ ("baño" ≠ "bano")."""
    text = (query or "").lower().replace("ñ", "\x00")
    text = "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))
    words = re.findall(r"[a-z0-9\x00]+", text)
    return "-".join(words).replace("\x00", "ñ")[:110].strip("-")


def search_url(query: str) -> str | None:
    slug = slugify(query)
    return f"https://{SEARCH_HOST}/{quote(slug)}" if slug else None


# ─── Parseo ───────────────────────────────────────────────────────


@dataclass(slots=True)
class ParsedSearch:
    candidates: list[MlCandidate] = field(default_factory=list)
    # state | jsonld | none: de dónde salieron los resultados.
    source: str = "none"
    # Resultados leídos de la página antes de aplicar el tope.
    total: int = 0
    # La página es un listado válido pero sin publicaciones.
    empty: bool = False


def _json_after(text: str, start: int) -> Any:
    """Objeto JSON que empieza en `text[start]`. raw_decode se detiene donde
    termina el objeto, sin importar lo que venga después (`;_n.ctx.r.assets…`)."""
    try:
        obj, _ = json.JSONDecoder().raw_decode(text, start)
    except (ValueError, RecursionError):   # RecursionError: un JSON anidado a propósito
        return None
    return obj


def _nordic_state(html: str) -> dict | None:
    m = _NORDIC_CTX.search(html)
    if not m:
        return None
    end = html.find("</script>", m.end())
    body = html[m.end(): end if end >= 0 else len(html)]
    at = body.find(_CTX_PREFIX)
    if at < 0:
        return None
    obj = _json_after(body, at + len(_CTX_PREFIX))
    return obj if isinstance(obj, dict) else None


def _find_results(state: dict) -> list | None:
    """Los resultados viven en appProps.pageProps.initialState.results. Si ML
    mueve el árbol, se busca la primera lista `results` de polycards."""
    try:
        direct = state["appProps"]["pageProps"]["initialState"]["results"]
    except (KeyError, TypeError):
        direct = None
    if isinstance(direct, list):
        return direct
    stack: list[tuple[Any, int]] = [(state, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > 7:
            continue
        if isinstance(node, dict):
            res = node.get("results")
            if isinstance(res, list) and any(isinstance(x, dict) and "polycard" in x for x in res):
                return res
            stack.extend((v, depth + 1) for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node[:60] if isinstance(v, (dict, list)))
    return None


def _clean_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return _WS.sub(" ", _BRACES.sub("", value)).strip()[:limit]


def _price_cents(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not (0 < value < _MAX_PRICE_CENTS / 100):
        return None
    return round(float(value) * 100)


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _component(polycard: dict, component_id: str) -> dict:
    for c in polycard.get("components") or []:
        if isinstance(c, dict) and component_id in (c.get("id"), c.get("type")):
            inner = c.get(component_id)
            return inner if isinstance(inner, dict) else {}
    return {}


def _clean_url(url: object) -> str:
    """El `url` de la polycard sin fragmento y con esquema."""
    if not isinstance(url, str) or not url.strip():
        return ""
    raw = url.strip().split("#", 1)[0]
    return raw if "://" in raw else "https://" + raw.lstrip("/")


def _permalink(url: object) -> str:
    """El `url` de la polycard viene sin esquema ("www.mercadolibre.com.ar/…").
    Un resultado PATROCINADO trae como url el click-tracker de ML
    (`click1.mercadolibre.com.ar/mclics/…`) y el link real nunca viaja: eso no se
    guarda (cada clic de un humano cobraría un clic al anunciante) y `safe_permalink`
    lo descarta. Quien llama arma entonces el link canónico por id."""
    return safe_permalink(_clean_url(url))


def canonical_permalink(ref: str) -> str:
    """Link por id cuando la publicación no trae uno usable: un ítem (MLA123) en
    articulo.mercadolibre.com.ar, un producto de usuario (MLAU123) en /up/."""
    if _REF.fullmatch(ref or ""):
        if ref.startswith("MLAU"):
            return f"https://www.mercadolibre.com.ar/up/{ref}"
        return f"https://articulo.mercadolibre.com.ar/MLA-{ref[3:]}"
    return ""


def _from_polycard(item: dict) -> MlCandidate | None:
    pc = item.get("polycard")
    if not isinstance(pc, dict):
        return None
    meta = pc.get("metadata") if isinstance(pc.get("metadata"), dict) else {}
    ref = str(meta.get("id") or "")
    if not _REF.fullmatch(ref):
        return None
    title = _clean_text(_component(pc, "title").get("text"), 200)
    if not title:
        return None
    price = _component(pc, "price").get("current_price")
    price = price if isinstance(price, dict) else {}
    pictures = (pc.get("pictures") or {}).get("pictures") if isinstance(pc.get("pictures"), dict) else None
    picture_id = next((str(p.get("id")) for p in pictures or []
                       if isinstance(p, dict) and _PICTURE_ID.fullmatch(str(p.get("id") or ""))), "")
    image = safe_image_url(_PICTURE_URL.format(picture_id)) if picture_id else None
    tracks = meta.get("tracks") if isinstance(meta.get("tracks"), dict) else {}
    catalog = str(meta.get("product_id") or "")
    currency = price.get("currency")
    return MlCandidate(
        id=ref,
        name=title,
        image_urls=[image] if image else [],
        permalink=_permalink(meta.get("url")) or (
            canonical_permalink(ref) if is_click_tracker(_clean_url(meta.get("url"))) else ""),
        domain_id=str(meta.get("domain_id") or "")[:60],
        origin=ORIGIN_WEB,
        price_cents=_price_cents(price.get("value")),
        currency=currency.upper()[:8] if isinstance(currency, str) else None,
        seller=_clean_text(_component(pc, "seller").get("text"), 60),
        sold_quantity=_int_or_none(tracks.get("sold_quantity")),
        catalog_id=catalog if _REF.fullmatch(catalog) else "",
    )


def _ld_products(html: str) -> list[dict]:
    out: list[dict] = []
    for m in _JSON_LD.finditer(html):
        try:
            data = json.loads(m.group(1))
        except (ValueError, RecursionError):
            continue
        graph = data.get("@graph") if isinstance(data, dict) else data
        for node in graph if isinstance(graph, list) else [graph]:
            if isinstance(node, dict) and node.get("@type") == "Product":
                out.append(node)
    return out


def _ids_in(text: object) -> set[str]:
    """Ids de ML que aparecen en un link: MLAU123 (producto de usuario), MLA123
    (ficha) o MLA-123-titulo (ítem)."""
    if not isinstance(text, str):
        return set()
    ids = {"MLA" + n for n in re.findall(r"MLA-?(\d{5,})", text, re.ASCII)}
    ids.update(re.findall(r"MLAU\d+", text, re.ASCII))
    return ids


def _from_ld(node: dict) -> tuple[MlCandidate, set[str]] | None:
    offers = node.get("offers") if isinstance(node.get("offers"), dict) else {}
    url = offers.get("url") if isinstance(offers.get("url"), str) else ""
    ids = _ids_in(url)
    # La referencia sale del link: /up/MLAU123, /p/MLA123 o …/MLA-123-titulo.
    ref = next(iter(sorted(ids, key=lambda x: (not x.startswith("MLAU"), x))), "")
    title = _clean_text(node.get("name"), 200)
    if not _REF.fullmatch(ref) or not title:
        return None
    image = node.get("image")
    image = image[0] if isinstance(image, list) and image else image
    safe_image = safe_image_url(image)
    brand = node.get("brand")
    brand = brand.get("name") if isinstance(brand, dict) else brand
    currency = offers.get("priceCurrency")
    cand = MlCandidate(
        id=ref, name=title, image_urls=[safe_image] if safe_image else [],
        permalink=safe_permalink(url), origin=ORIGIN_WEB,
        price_cents=_price_cents(offers.get("price")),
        currency=currency.upper()[:8] if isinstance(currency, str) else None,
        brand=_clean_text(brand, 40),
    )
    return cand, ids


def parse_search(html: str, max_results: int) -> ParsedSearch:
    """Resultados de una página de listado. Nunca lanza: lo ilegible vuelve
    como `source="none"`."""
    limit = max(1, int(max_results))
    html = (html or "")[:MAX_HTML_CHARS]
    ld_list: list[MlCandidate] = []
    ld_by_id: dict[str, MlCandidate] = {}
    for node in _ld_products(html):
        parsed = _from_ld(node)
        if parsed is not None:
            ld_list.append(parsed[0])
            for key in parsed[1]:
                ld_by_id.setdefault(key, parsed[0])

    state = _nordic_state(html)
    results = _find_results(state) if state else None
    if results is not None:
        cands: list[MlCandidate] = []
        for raw in results:
            cand = _from_polycard(raw) if isinstance(raw, dict) else None
            if cand is None:
                continue
            meta = raw["polycard"].get("metadata") or {}
            # La marca solo viene en el JSON-LD: se cruza por cualquiera de los ids.
            for key in (cand.id, str(meta.get("product_id") or ""), str(meta.get("user_product_id") or "")):
                hit = ld_by_id.get(key)
                if hit is not None and hit.brand:
                    cand.brand = hit.brand
                    break
            cands.append(cand)
        return ParsedSearch(cands[:limit], "state", len(cands), empty=not cands)
    if ld_list:
        return ParsedSearch(ld_list[:limit], "jsonld", len(ld_list))
    return ParsedSearch()


# ─── Qué pasó con la página ───────────────────────────────────────


@dataclass(slots=True)
class WebSearch:
    # ok | empty | blocked | error | budget | off
    kind: str
    candidates: list[MlCandidate] = field(default_factory=list)
    bytes: int = 0
    reason: str = ""
    source: str = ""


# Dominios donde puede terminar una búsqueda de ML (con chequeo de borde).
ML_PAGE_HOSTS = ("mercadolibre.com.ar", "mercadolibre.com")
_VERIFICATION_PATHS = ("account-verification",)


def page_problem(page: browser_fetch.ListingPage, parsed: ParsedSearch) -> tuple[str, str] | None:
    """(kind, motivo) si la página NO es un listado legible; None si lo es.
    Un listado válido sin resultados NO es un problema (`parsed.empty`).

    Lo que dice la URL final y el status manda aunque la página traiga el estado
    de ML: una página de verificación puede llevar un `results: []` y no por eso
    es "ML no tiene nada para ese título". Las marcas de anti-bot en el HTML solo
    cuentan cuando no se pudo leer ningún resultado."""
    if page.status in (403, 429):
        return "blocked", f"ML bloqueó la búsqueda (HTTP {page.status})"
    final = urlsplit(page.final_url or "")
    if not browser_fetch.url_host_allowed(page.final_url, ML_PAGE_HOSTS) \
            or any(v in final.path.lower() for v in _VERIFICATION_PATHS):
        return "blocked", "ML pidió verificación anti-bot (captcha o redirect)"
    if parsed.source != "none":
        return None
    head = (page.html or "")[:6000].lower()
    if any(m in head for m in _ANTIBOT_MARKERS):
        return "blocked", "ML pidió verificación anti-bot (captcha o redirect)"
    if page.status and page.status >= 500:
        return "error", f"ML respondió HTTP {page.status}"
    return "error", "la página de ML no trae resultados en un formato conocido"


# ─── La fuente de la corrida ──────────────────────────────────────

Fetcher = Callable[[str], Awaitable[browser_fetch.ListingPage]]


def unavailable_reason() -> str | None:
    """Por qué la fuente web NO puede correr hoy, o None si puede. Sin proxy
    residencial ni se intenta: ML bloquea la IP del datacenter."""
    if not browser_fetch.available():
        return "el browser no está disponible (BROWSER_FETCH_ENABLED o Camoufox)"
    if not browser_fetch.proxy_configured():
        return browser_fetch.proxy_problem() or "falta BROWSER_PROXY (ML bloquea la IP del datacenter)"
    return None


class MlWebSource:
    """Búsquedas web de UNA corrida: cuenta cupo, bytes y bloqueos, y se corta
    sola si ML bloquea varias veces seguidas.

    `fetcher` y `sleep` se inyectan para que los tests no abran un navegador ni
    esperen. `on_reserve` corre en la transacción que reserva cada búsqueda (el
    semáforo suma ahí `web_searches` a su corrida)."""

    def __init__(
        self,
        *,
        budget: int,
        max_results: int = 8,
        concurrency: int = 1,
        pause_s: float = 4.0,
        block_streak: int = 5,
        block_scripts: bool = True,
        on_reserve: Callable[[Any], None] | None = None,
        fetcher: Fetcher | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.budget = int(budget)
        self.max_results = max(1, int(max_results))
        self.pause_s = max(0.0, float(pause_s))
        self.block_streak = max(1, int(block_streak))
        self._block_scripts = bool(block_scripts)
        self._on_reserve = on_reserve
        self._sleep = sleep
        self._jitter = jitter
        self._sem = asyncio.Semaphore(max(1, int(concurrency)))
        self._browser: browser_fetch.ListingBrowser | None = None
        self._fetch: Fetcher = fetcher or self._default_fetch
        self._streak = 0
        # Contadores de la corrida.
        self.searches = 0
        self.bytes = 0
        self.blocked = 0
        self.errors = 0
        self.cut_reason: str | None = None
        self.exhausted = False

    async def _default_fetch(self, url: str) -> browser_fetch.ListingPage:
        if self._browser is None:
            self._browser = browser_fetch.ListingBrowser(block_scripts=self._block_scripts)
        return await self._browser.fetch(url)

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None

    @property
    def active(self) -> bool:
        return self.cut_reason is None and not self.exhausted

    def status_text(self) -> str:
        if self.cut_reason:
            return self.cut_reason
        if self.exhausted:
            return "sin cupo diario de búsquedas web"
        return "ok"

    def _note_failure(self, kind: str) -> None:
        self._streak += 1
        if kind == "blocked":
            self.blocked += 1
        else:
            self.errors += 1
        if self._streak >= self.block_streak and self.cut_reason is None:
            self.cut_reason = f"cortada por esta noche: {self._streak} fallos seguidos de ML web"
            log.warning("ML web: %s", self.cut_reason)

    async def search(self, query: str) -> WebSearch:
        """Una búsqueda. Nunca lanza: todo problema vuelve en `kind`/`reason`."""
        url = search_url(query)
        if url is None:
            return WebSearch("empty", reason="el título no tiene palabras para buscar")
        if not self.active:
            return WebSearch("off", reason=self.status_text())
        async with self._sem:
            if not self.active:  # se cortó mientras esperaba su turno
                return WebSearch("off", reason=self.status_text())
            try:
                return await self._one(url)
            finally:
                if self.pause_s:
                    await self._sleep(self.pause_s * (0.7 + 0.6 * self._jitter()))

    async def _one(self, url: str) -> WebSearch:
        if await daily_budget.reserve_async(
            WEB_COUNTER_KEY, self.budget, None, self._on_reserve,
        ) is None:
            self.exhausted = True
            return WebSearch("budget", reason="sin cupo diario de búsquedas web")
        self.searches += 1
        try:
            page = await self._fetch(url)
        except browser_fetch.SsrfBlocked as exc:
            return WebSearch("error", reason=f"URL rechazada por el guard: {browser_fetch.redact(exc)}")
        except browser_fetch.CircuitOpen:
            self._note_failure("blocked")
            return WebSearch("blocked", reason="ML web en descanso por bloqueos recientes")
        except Exception as exc:  # noqa: BLE001  (BrowserUnavailable, red, timeout)
            self._note_failure("error")
            return WebSearch("error", reason=f"el browser falló: {browser_fetch.redact(exc)[:160]}")

        self.bytes += page.bytes
        if not browser_fetch.url_host_allowed(page.final_url, ML_PAGE_HOSTS):
            # Defensa en profundidad: el guard del browser ya solo deja salir a
            # ML, pero una página que terminó en otro host no se parsea.
            self._note_failure("blocked")
            log.warning("ML web: la búsqueda terminó fuera de ML (%s), no se lee",
                        urlsplit(page.final_url or "").hostname or "sin URL")
            return WebSearch("blocked", bytes=page.bytes,
                             reason="ML redirigió la búsqueda a otro sitio")
        parsed = parse_search(page.html, self.max_results)
        problem = page_problem(page, parsed)
        browser_fetch.note_listing_result(url, results=len(parsed.candidates))
        if problem is not None:
            kind, reason = problem
            self._note_failure(kind)
            log.info("ML web: %s (%s, %d bytes)", reason, url[:120], page.bytes)
            return WebSearch(kind, bytes=page.bytes, reason=reason)
        self._streak = 0
        kind = "empty" if not parsed.candidates else "ok"
        return WebSearch(kind, parsed.candidates, page.bytes, source=parsed.source,
                         reason="ML web no tiene publicaciones para el título" if kind == "empty" else "")


def disabled_reason() -> str | None:
    """Por qué la fuente web NO corre hoy según settings, proxy y cupo, o None si
    corre. Es lo que el semáforo deja anotado en la corrida."""
    from app import runtime  # import tardío: runtime importa la DB

    budget = int(runtime.get("pm_ml_web_daily_budget") or 0)
    if budget <= 0:
        return "pm_ml_web_daily_budget = 0"
    reason = unavailable_reason()
    if reason:
        return reason
    used = daily_budget.used_today(WEB_COUNTER_KEY)
    if used >= budget:
        return f"sin cupo diario de búsquedas web (usadas {used} de {budget})"
    return None


def from_runtime(on_reserve: Callable[[Any], None] | None = None) -> MlWebSource:
    """La fuente con los settings del dashboard. Llamar solo si `disabled_reason()`
    es None."""
    from app import runtime

    return MlWebSource(
        budget=int(runtime.get("pm_ml_web_daily_budget")),
        max_results=int(runtime.get("pm_ml_web_max_results")),
        concurrency=int(runtime.get("pm_ml_web_concurrency")),
        pause_s=float(runtime.get("pm_ml_web_pause_s")),
        block_streak=int(runtime.get("pm_ml_web_block_streak")),
        block_scripts=bool(int(runtime.get("pm_ml_web_block_scripts"))),
        on_reserve=on_reserve,
    )


def web_budget_status() -> dict:
    """Consumo del día para el dashboard."""
    from app import runtime

    used = daily_budget.used_today(WEB_COUNTER_KEY)
    raw = runtime.get("pm_ml_web_daily_budget")
    budget = int(get_settings().pm_ml_web_daily_budget if raw is None else raw)
    return {"used": used, "budget": budget, "remaining": max(0, budget - used)}
