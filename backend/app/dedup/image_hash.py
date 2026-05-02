"""Estrategia 2 — perceptual image hashing.

Usamos pHash (perceptual hash) de la lib `imagehash`. Dos imágenes visualmente
similares tienen hashes con baja distancia de Hamming. Convertimos esa distancia
a un score [0, 1] donde 1.0 = idénticas.

Las imágenes se descargan via httpx con un cache simple en memoria por URL.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from typing import Iterable

import httpx
import imagehash
from PIL import Image

# Cache LRU básica por URL (proceso-vivo). Para producción conviene Redis.
_HASH_CACHE: dict[str, imagehash.ImageHash] = {}
_HASH_BITS = 64  # phash default

_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


async def _fetch(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.content


async def hash_image(url: str) -> imagehash.ImageHash | None:
    """Devuelve el pHash de la imagen, o None si no se pudo procesar."""
    if url in _HASH_CACHE:
        return _HASH_CACHE[url]
    try:
        raw = await _fetch(url)
        img = Image.open(BytesIO(raw)).convert("RGB")
        h = imagehash.phash(img)
        _HASH_CACHE[url] = h
        return h
    except Exception:
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
