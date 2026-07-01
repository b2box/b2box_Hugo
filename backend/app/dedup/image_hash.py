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

import httpx
import imagehash
from PIL import Image

from app.config import get_settings
from app.net_guard import safe_get

# Cache LRU acotada por URL (proceso-vivo). El tope evita que crezca sin techo
# (OOM en el container). Para producción multi-instancia conviene Redis.
_HASH_CACHE: "OrderedDict[str, imagehash.ImageHash]" = OrderedDict()
_HASH_BITS = 64  # phash default

_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
# Tope de bytes por imagen (evita descargar archivos gigantes → memoria/DoS).
_MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _cache_put(url: str, h: imagehash.ImageHash) -> None:
    maxlen = get_settings().dedup_image_cache_max
    _HASH_CACHE[url] = h
    _HASH_CACHE.move_to_end(url)
    while len(_HASH_CACHE) > maxlen:
        _HASH_CACHE.popitem(last=False)


async def _fetch(url: str) -> bytes:
    # safe_get valida scheme + IP pública y cada redirect (anti-SSRF).
    r = await safe_get(url, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    if len(r.content) > _MAX_IMAGE_BYTES:
        raise ValueError(f"imagen demasiado grande: {len(r.content)} bytes")
    return r.content


async def hash_image(url: str) -> imagehash.ImageHash | None:
    """Devuelve el pHash de la imagen, o None si no se pudo procesar."""
    cached = _HASH_CACHE.get(url)
    if cached is not None:
        _HASH_CACHE.move_to_end(url)
        return cached
    try:
        raw = await _fetch(url)
        img = Image.open(BytesIO(raw)).convert("RGB")
        h = imagehash.phash(img)
        _cache_put(url, h)
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
