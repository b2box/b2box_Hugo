"""Último recurso para links de Mercado Libre: un actor de Apify.

Por qué existe: hay links de ML que la API oficial no deja leer (403 en
`/items` y en `/products/{MLAU…}` para publicaciones de otros vendedores) y
cuya ficha de catálogo de la ruta tampoco sirve. El scraping propio desde el
datacenter de Hugo choca siempre con el interstitial anti-bot, así que el
cliente se quedaba con "no se pudo sacar la foto (página anti-bot)". Un actor
de Apify lee la misma ficha con proxies residenciales de ellos y devuelve
título, fotos y (a veces) precio.

Es caro en tiempo (1-3 min) y cuesta plata (~USD 0,005 por resultado), así que:
  * se prende solo con APIFY_TOKEN y hay cupo diario runtime
    (`apify_meli_daily_budget`, 0 = apagado), reservado atómico y fail-closed;
  * a Apify solo van URLs de Mercado Libre (la misma lista blanca que el
    semáforo usa para los permalinks), y todo lo que vuelve pasa por la lista
    blanca de fotos (`*.mlstatic.com`, https) y el saneo de títulos;
  * nunca propaga: si Apify falla (401, 402 sin saldo, timeout, actor caído)
    se loguea el status —sin body ni token— y el llamador sigue como antes.

El token viaja SOLO en el header Authorization: Apify también lo acepta en el
query (`?token=`), pero ahí termina en logs de httpx, de proxies y en tracebacks.

Actores soportados (el formato de salida se normaliza acá):
  khadinakbar/mercadolibre-all-in-one-scraper  → resuelve /up/ y /p/ (default)
      {itemId, title, price|null, currencyId, pictures[], thumbnail, url}
  memo23/mercadolibre-scraper                   → rápido en /p/, falla en /up/
      {productId, title, price, currency, primaryImage, images[]}
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.config import get_settings
from app.ingest import meli
from app.pricing import daily_budget
from app.pricing.market_ml import safe_image_url, safe_permalink
from app.pricing.market_ml_web import clean_line

log = logging.getLogger(__name__)

API_BASE = "https://api.apify.com/v2"
COUNTER_KEY = "_meta:apify_meli_calls_today"
# Una ficha con 20 fotos pesa ~20-40 KB de JSON. 2 MB es holgado y corta a un
# actor que se vuelva loco (o a una respuesta hostil) antes de llenar memoria.
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# Lo que dejamos que el actor gaste por corrida. Una corrida pide UN resultado
# (~USD 0,005); el techo es para que un cambio de precio del actor no se note
# recién en la factura.
_MAX_CHARGE_USD = 0.05
# Margen sobre el timeout del actor para el viaje de ida y vuelta a la API.
_NETWORK_MARGIN_S = 5.0
# Memoria del actor en MB. Sin esto Apify usa el default del actor (que puede
# ser 4 GB) y la corrida se cobra por GB-hora además del resultado.
_ACTOR_MEMORY_MB = 1024
# Corridas en paralelo. Más que esto no es uso real del app: es alguien pegando
# links en loop, y cada uno cuesta plata y 1-3 min de una corrida.
_MAX_CONCURRENT = 2
# Cuánto se espera un lugar libre. Corto a propósito: la espera sale del mismo
# reloj de ~110 s del cliente, y con los dos lugares ocupados es mejor seguir
# sin Apify que contestar tarde.
_SLOT_WAIT_S = 2.0
# Caché por link (ver _cache_key). Lo que Apify resolvió, o leyó y no trajo
# fotos, vale 24 h: repetir el mismo link no vuelve a pagar. Un error de Apify
# (402 sin saldo, timeout, red) vale 1 h: también frena el loop, pero no deja
# un link sin resolver un día entero después de cargar saldo.
_CACHE_TTL_S = 24 * 3600
_CACHE_ERROR_TTL_S = 3600
_CACHE_MAX = 2000
_MAX_IMAGES = 10
_MAX_TITLE = 300
# `usuario/nombre` o `usuario~nombre`: va en el path de la URL, nada más entra.
_ACTOR_RE = re.compile(r"^[A-Za-z0-9_.-]+[/~][A-Za-z0-9_.-]+$")
# Id de foto de ML (`735512-MLA74859284738_032024`): la misma foto viene en
# varios tamaños y es la clave para quedarse con una sola.
_PICTURE_ID_RE = re.compile(r"(\d+-ML[A-Z]U?\d+_\d+)")
# Sufijo de tamaño de mlstatic: -F grande, -O original, -R miniatura, etc.
_SIZE_SUFFIX_RE = re.compile(r"-([A-Z])\.(?:webp|jpe?g|png)$", re.I)
# Motivos legibles para los status que ya vimos. El body NO se loguea: puede
# traer el input (la URL del cliente) o detalles de la cuenta.
_STATUS_REASON = {
    400: "input inválido o corrida fallida",
    401: "token inválido",
    402: "sin saldo en Apify",
    403: "token sin permiso para el actor",
    404: "actor inexistente",
    408: "el actor no terminó a tiempo",
    429: "Apify limitó los requests",
}


@dataclass(slots=True)
class ApifyMeliResult:
    """Lo que devolvió Apify, ya saneado. `image_urls` vacío = se mandó y no sirvió."""
    image_urls: list[str] = field(default_factory=list)
    title: str = ""
    price_cents: int | None = None
    currency: str | None = None
    permalink: str = ""

    @property
    def found(self) -> bool:
        return bool(self.image_urls)


# ─── Configuración ─────────────────────────────────────────────────


def enabled() -> bool:
    """Token cargado y actor con forma válida. El cupo se mira al reservar."""
    s = get_settings()
    return bool(s.apify_token.strip()) and bool(_ACTOR_RE.fullmatch(s.apify_meli_actor.strip()))


def _daily_budget() -> int:
    from app import runtime

    raw = runtime.get("apify_meli_daily_budget")
    # `is None`, no `or`: 0 es "apagado", no "usá el default".
    return int(get_settings().apify_meli_daily_budget if raw is None else raw)


def allowed_url(url: str) -> str:
    """La URL que se le puede mandar a Apify, o "" si no es de Mercado Libre.

    Misma lista blanca que los permalinks del semáforo: https, host de ML, sin
    userinfo ni click-trackers. Un http:// de ML se sube a https (es lo que el
    cliente pegó, no un intento raro). El fragmento no viaja.
    """
    raw = (url or "").strip()
    if raw.lower().startswith("http://"):
        raw = "https://" + raw[7:]
    return safe_permalink(raw).split("#", 1)[0]


def _actor_input(actor: str, url: str) -> dict[str, Any]:
    if actor.split("/")[0].split("~")[0].lower() == "memo23":
        return {"startUrls": [url], "country": "MLA", "enrichDetail": True,
                "maxReviews": 0, "maxItems": 1}
    return {"startUrls": [{"url": url}], "mode": "product", "maxResults": 1}


# ─── Normalización de la salida ────────────────────────────────────


def _raw_pictures(item: dict) -> list[str]:
    """Las URLs de foto en el orden en que vienen, de cualquiera de los dos
    formatos. Cada entrada puede ser string o un dict con url/secure_url."""
    out: list[str] = []
    sources: list[Any] = []
    for key in ("pictures", "images"):
        value = item.get(key)
        if isinstance(value, list):
            sources.extend(value)
    for key in ("primaryImage", "thumbnail"):
        sources.append(item.get(key))
    for entry in sources:
        if isinstance(entry, dict):
            entry = entry.get("secure_url") or entry.get("url") or entry.get("src")
        if isinstance(entry, str) and entry:
            out.append(entry)
    return out


def _size_rank(url: str) -> int:
    """Más alto = mejor. La -F (y la 2X) es la grande; la -R, la miniatura."""
    rank = 0
    m = _SIZE_SUFFIX_RE.search(url)
    if m:
        letter = m.group(1).upper()
        rank += {"F": 3, "O": 2, "R": -3}.get(letter, 0)
    if "_2X_" in url.upper():
        rank += 1
    return rank


def pick_pictures(raw_urls: list[str], limit: int = _MAX_IMAGES) -> list[str]:
    """Fotos aptas, una por foto de ML y en su tamaño más grande.

    khadinakbar devuelve cada foto dos veces (miniatura -R y grande -F): para
    CLIP y para el juez la miniatura no sirve. Lo que no es https de
    *.mlstatic.com se descarta acá, antes de que nadie lo descargue.
    """
    best: dict[str, str] = {}
    order: list[str] = []
    for raw in raw_urls:
        url = safe_image_url(raw)
        if url is None:
            continue
        m = _PICTURE_ID_RE.search(url)
        key = m.group(1) if m else url
        if key not in best:
            order.append(key)
            best[key] = url
        elif _size_rank(url) > _size_rank(best[key]):
            best[key] = url
    return [best[k] for k in order][:limit]


def _price_cents(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # Mismo techo que el semáforo: un precio así es basura, no un producto.
    if not (0 < value < 1_000_000_000):
        return None
    return round(float(value) * 100)


def normalize(items: object, fallback_url: str) -> ApifyMeliResult:
    """Dataset del actor → ApifyMeliResult. Toma el primer item con fotos."""
    if not isinstance(items, list):
        return ApifyMeliResult()
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        pictures = pick_pictures(_raw_pictures(item))
        if not pictures:
            continue
        price = _price_cents(item.get("price"))
        currency = clean_line(item.get("currencyId") or item.get("currency"), 8) or None
        return ApifyMeliResult(
            image_urls=pictures,
            title=clean_line(item.get("title"), _MAX_TITLE),
            price_cents=price,
            currency=currency if price is not None else None,
            permalink=safe_permalink(item.get("url")) or fallback_url,
        )
    return ApifyMeliResult()


# ─── Llamada a Apify ───────────────────────────────────────────────


async def _post_bounded(client: httpx.AsyncClient, endpoint: str, body: dict,
                        headers: dict) -> tuple[int, bytes | None]:
    """POST leyendo como mucho _MAX_RESPONSE_BYTES. (status, body | None si se pasó)."""
    async with client.stream("POST", endpoint, json=body, headers=headers) as resp:
        if resp.status_code not in (200, 201):
            return resp.status_code, b""
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > _MAX_RESPONSE_BYTES:
                return resp.status_code, None
            chunks.append(chunk)
        return resp.status_code, b"".join(chunks)


def _new_client(total_s: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(total_s, connect=10.0))


# ─── Caché y concurrencia ──────────────────────────────────────────

# clave → (vence_en monotonic, resultado). OrderedDict como LRU.
_cache: OrderedDict[str, tuple[float, ApifyMeliResult]] = OrderedDict()
# Un semáforo por event loop: asyncio.Semaphore queda atado al loop donde
# alguien esperó por primera vez, y los tests (o un reinicio) usan otro.
_slots: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _reset_for_tests() -> None:
    global _slots
    _cache.clear()
    _slots = None


def _semaphore() -> asyncio.Semaphore:
    global _slots
    loop = asyncio.get_running_loop()
    if _slots is None or _slots[0] is not loop:
        _slots = (loop, asyncio.Semaphore(_MAX_CONCURRENT))
    return _slots[1]


def _cache_key(target: str) -> str:
    """El mismo producto aunque el link traiga otro tracking: los ids de ML
    (publicación del query y ficha de la ruta) si los hay; si no, host + ruta."""
    ref = meli.parse_url(target)
    catalog = meli.catalog_ref(target)
    if ref is not None:
        return f"{catalog.id if catalog else ''}|{ref.kind}:{ref.id}"
    parts = urlsplit(target)
    return f"{(parts.hostname or '').lower()}{parts.path}"


def _cache_get(key: str) -> ApifyMeliResult | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    expires_at, result = hit
    if time.monotonic() >= expires_at:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return result


def _cache_put(key: str, result: ApifyMeliResult, ttl_s: float) -> None:
    _cache[key] = (time.monotonic() + ttl_s, result)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


# ─── Llamada a Apify ───────────────────────────────────────────────


async def fetch(url: str) -> ApifyMeliResult | None:
    """Lee el link de ML con el actor de Apify.

    None → no se mandó nada (apagado, sin cupo, URL que no es de ML, sin lugar
    libre, o ya se probó ese link y no sirvió): no se esperó ni se gastó.
    ApifyMeliResult sin fotos → se mandó y no sirvió (ya se esperó y se gastó).
    Nunca lanza: cualquier sorpresa (un JSON que revienta la recursión, la DB
    del cupo caída) termina en un resultado vacío y el lookup sigue.
    """
    try:
        return await _fetch(url)
    except Exception as exc:  # noqa: BLE001
        # Solo el tipo: el mensaje puede arrastrar el request o el body.
        log.warning("Apify: error inesperado %s, sigo sin Apify", type(exc).__name__)
        return ApifyMeliResult()


async def _fetch(url: str) -> ApifyMeliResult | None:
    if not enabled():
        return None
    target = allowed_url(url)
    if not target:
        log.info("Apify: %s no es un link de Mercado Libre, no se manda", (url or "")[:120])
        return None
    key = _cache_key(target)
    cached = _cache_get(key)
    if cached is not None:
        if cached.found:
            log.info("Apify: %s sale de la caché, no se paga de nuevo", target[:120])
            return cached
        log.info("Apify: %s ya se probó y no sirvió, no se manda de nuevo", target[:120])
        return None
    budget = _daily_budget()
    if budget <= 0:
        return None

    slots = _semaphore()
    try:
        await asyncio.wait_for(slots.acquire(), timeout=_SLOT_WAIT_S)
    except asyncio.TimeoutError:
        log.info("Apify: %d corridas en curso, salteo %s", _MAX_CONCURRENT, target[:120])
        return None
    try:
        if await daily_budget.reserve_async(COUNTER_KEY, budget) is None:
            log.info("Apify: cupo diario agotado (%d), no se manda %s", budget, target[:120])
            return None
        result, ttl_s = await _run_actor(target)
        _cache_put(key, result, ttl_s)
        return result
    finally:
        slots.release()


async def _run_actor(target: str) -> tuple[ApifyMeliResult, float]:
    """Una corrida del actor. (resultado, cuánto vale en la caché)."""
    s = get_settings()
    actor = s.apify_meli_actor.strip()
    timeout_s = int(s.apify_meli_timeout_s)
    endpoint = (
        f"{API_BASE}/acts/{quote(actor.replace('/', '~'), safe='~')}/run-sync-get-dataset-items"
        f"?timeout={timeout_s}&memory={_ACTOR_MEMORY_MB}&maxItems=1"
        f"&maxTotalChargeUsd={_MAX_CHARGE_USD}"
    )
    headers = {"Authorization": f"Bearer {s.apify_token.strip()}", "Accept": "application/json"}
    total = timeout_s + _NETWORK_MARGIN_S
    started = time.monotonic()
    failed = (ApifyMeliResult(), _CACHE_ERROR_TTL_S)
    http = _new_client(total)
    try:
        status, body = await asyncio.wait_for(
            _post_bounded(http, endpoint, _actor_input(actor, target), headers), timeout=total,
        )
    except asyncio.TimeoutError:
        log.warning("Apify %s: sin respuesta en %.0fs para %s", actor, total, target[:120])
        return failed
    except httpx.HTTPError as exc:
        # Solo el tipo: el str de algunas excepciones de httpx arrastra el request.
        log.warning("Apify %s: error de red %s para %s", actor, type(exc).__name__, target[:120])
        return failed
    finally:
        await http.aclose()

    elapsed = time.monotonic() - started
    if status not in (200, 201):
        log.warning("Apify %s: HTTP %s (%s) en %.0fs para %s", actor, status,
                    _STATUS_REASON.get(status, "error"), elapsed, target[:120])
        return failed
    if body is None:
        log.warning("Apify %s: respuesta de más de %d bytes, descartada", actor, _MAX_RESPONSE_BYTES)
        return failed
    try:
        items = json.loads(body)
    except (ValueError, RecursionError):
        # RecursionError: un JSON con cien mil corchetes anidados.
        log.warning("Apify %s: respuesta no-JSON para %s", actor, target[:120])
        return failed

    result = normalize(items, target)
    if result.found:
        log.info("Apify %s resolvió %s: %d fotos en %.0fs", actor, target[:120],
                 len(result.image_urls), elapsed)
    else:
        log.info("Apify %s no trajo fotos usables de %s (%.0fs)", actor, target[:120], elapsed)
    return result, _CACHE_TTL_S
