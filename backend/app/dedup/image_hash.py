"""Estrategia 2 — perceptual image hashing.

Usamos pHash (perceptual hash) de la lib `imagehash`. Dos imágenes visualmente
similares tienen hashes con baja distancia de Hamming. Convertimos esa distancia
a un score [0, 1] donde 1.0 = idénticas.

Las imágenes se descargan via httpx con un cache simple en memoria por URL.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from io import BytesIO
from typing import Iterable

import logging

import httpx
import imagehash
from PIL import Image
from sqlmodel import Session

from app.config import get_settings
from app.net_guard import safe_get

log = logging.getLogger(__name__)

# Cache LRU acotada por URL (proceso-vivo, L1). El tope evita que crezca sin
# techo (OOM en el container). L2 = tabla image_hash_cache en la DB, que
# sobrevive reinicios (ver _db_get / _db_put).
_HASH_CACHE: "OrderedDict[str, imagehash.ImageHash]" = OrderedDict()
_HASH_BITS = 64  # phash default
_MAX_URL_LEN = 1024  # coincide con ImageHashCache.url


def _db_get(url: str) -> imagehash.ImageHash | None:
    """L2: lee el pHash de la DB (persistente entre reinicios)."""
    if len(url) > _MAX_URL_LEN:
        return None
    try:
        from app.db.models import ImageHashCache
        from app.db.session import engine

        with Session(engine) as session:
            row = session.get(ImageHashCache, url)
            if row is None:
                return None
            return imagehash.hex_to_hash(row.phash)
    except Exception:  # noqa: BLE001
        return None


def _db_put(url: str, h: imagehash.ImageHash) -> None:
    """L2: persiste el pHash en la DB para reusarlo tras reiniciar."""
    if len(url) > _MAX_URL_LEN:
        return
    try:
        from app.clock import utcnow
        from app.db.models import ImageHashCache
        from app.db.session import engine

        with Session(engine) as session:
            row = session.get(ImageHashCache, url)
            if row is None:
                session.add(ImageHashCache(url=url, phash=str(h)))
            else:
                row.phash = str(h)
                row.updated_at = utcnow()
                session.add(row)
            session.commit()
    except Exception:  # noqa: BLE001
        log.debug("No se pudo persistir pHash de %s", url, exc_info=True)

_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
# Tope de bytes por imagen (evita descargar archivos gigantes → memoria/DoS).
_MAX_IMAGE_BYTES = 8 * 1024 * 1024

# Cabeceras de cliente normal. Sin esto httpx se anuncia como "python-httpx/0.x"
# y varios CDN de marketplace (mlstatic entre ellos) cortan o tiran 403. No es
# disfrazarse de nadie: es mandar lo que manda cualquier cliente HTTP que pide
# una imagen para mostrarla.
_IMAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
}

# Reintentos ante fallos transitorios. Indexar el catálogo son miles de descargas
# seguidas contra el mismo CDN, y ahí aparecen cortes intermitentes: sin
# reintento, cada uno es un producto que queda fuera del índice para siempre.
_FETCH_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (0.5, 1.5)
# Códigos que vale la pena reintentar (throttling y errores de servidor).
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _cache_put(url: str, h: imagehash.ImageHash) -> None:
    maxlen = get_settings().dedup_image_cache_max
    _HASH_CACHE[url] = h
    _HASH_CACHE.move_to_end(url)
    while len(_HASH_CACHE) > maxlen:
        _HASH_CACHE.popitem(last=False)


async def _fetch(url: str) -> bytes:
    """Descarga una imagen, reintentando los fallos transitorios.

    safe_get valida scheme + IP pública y cada redirect (anti-SSRF); el guard
    sigue corriendo en cada intento.
    """
    last_error: Exception | None = None
    for attempt in range(_FETCH_ATTEMPTS):
        try:
            r = await safe_get(url, timeout=_HTTP_TIMEOUT, headers=_IMAGE_HEADERS)
            if r.status_code in _RETRYABLE_STATUS:
                raise httpx.HTTPStatusError(
                    f"HTTP {r.status_code}", request=r.request, response=r
                )
            r.raise_for_status()
            if len(r.content) > _MAX_IMAGE_BYTES:
                # No es transitorio: reintentar solo gastaría red.
                raise ValueError(f"imagen demasiado grande: {len(r.content)} bytes")
            return r.content
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            # Un 404 o un 403 no mejoran reintentando: propagamos ya.
            if isinstance(exc, httpx.HTTPStatusError):
                status = exc.response.status_code if exc.response is not None else None
                if status not in _RETRYABLE_STATUS:
                    raise
            last_error = exc
            if attempt + 1 >= _FETCH_ATTEMPTS:
                break
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS[attempt])
    log.debug("No se pudo descargar %s tras %d intentos: %s",
              url, _FETCH_ATTEMPTS, last_error)
    raise last_error if last_error else RuntimeError(f"fetch falló: {url}")


async def hash_image(url: str) -> imagehash.ImageHash | None:
    """Devuelve el pHash de la imagen, o None si no se pudo procesar.

    Orden de búsqueda: L1 (memoria) → L2 (DB) → descarga+hash.
    """
    cached = _HASH_CACHE.get(url)
    if cached is not None:
        _HASH_CACHE.move_to_end(url)
        return cached
    # L2: DB (persistente). La lectura es sync y rápida; la corremos en thread
    # para no bloquear el event loop.
    import asyncio

    db_hash = await asyncio.to_thread(_db_get, url)
    if db_hash is not None:
        _cache_put(url, db_hash)
        return db_hash
    try:
        raw = await _fetch(url)
        img = Image.open(BytesIO(raw)).convert("RGB")
        h = imagehash.phash(img)
        _cache_put(url, h)
        await asyncio.to_thread(_db_put, url, h)
        return h
    except Exception:  # incluye SsrfBlocked, HTTP, PIL, etc.
        return None


async def hash_many(urls: Iterable[str]) -> list[imagehash.ImageHash]:
    """Hashea varias URLs en paralelo, descartando las que fallan."""
    results = await asyncio.gather(*(hash_image(u) for u in urls), return_exceptions=False)
    return [h for h in results if h is not None]


def _score_from_distance(d: int) -> float:
    """Convierte una distancia de Hamming (0..bits) en score 0..1."""
    return max(0.0, 1.0 - d / _HASH_BITS)


async def image_similarity(urls_a: list[str], urls_b: list[str]) -> float:
    """Devuelve el mejor score entre cualquier par (a, b) de imágenes.

    Si A o B no tiene imágenes procesables, devuelve 0.0.
    """
    if not urls_a or not urls_b:
        return 0.0
    hashes_a, hashes_b = await asyncio.gather(hash_many(urls_a), hash_many(urls_b))
    if not hashes_a or not hashes_b:
        return 0.0
    best = 0.0
    for ha in hashes_a:
        for hb in hashes_b:
            score = _score_from_distance(ha - hb)
            if score > best:
                best = score
                if best >= 0.999:  # cortocircuito
                    return best
    return best
