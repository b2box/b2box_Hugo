"""Fotos del estimado: cuál se manda a Apify, su sha256 y los vectores CLIP.

Condición B1 del PR 3: a Apify (y a Qwen) solo viajan fotos de CDNs conocidos
(mlstatic de Mercado Libre, alicdn y alibaba.com) o del bucket propio
(`ESTIMATE_UPLOAD_IMAGE_HOSTS`). Cualquier otra URL que pegue el visitante no
sale de Hugo.

Condición M1: el sha256 de la foto (clave de la caché de Apify) lo calcula Hugo
sobre los bytes que bajó él (`image_hash._fetch`: anti-SSRF, tope de tamaño),
nunca un hash o una URL que mande el cliente.

Los vectores de las miniaturas de 1688 NO van a la caché de embeddings en base
(`image_embed_cache`): son de un solo uso y la llenarían de fotos ajenas.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

import numpy as np

from app.config import get_settings
from app.dedup import image_embed
from app.dedup.image_hash import _fetch
from app.ingest import apify_1688

log = logging.getLogger(__name__)

_CDN_DOMAINS = ("mlstatic.com", "alicdn.com", "alibaba.com")
_THUMB_CONCURRENCY = 6


@dataclass(slots=True)
class QueryPhoto:
    url: str                       # la URL normalizada que va a Apify
    sha256: str | None = None      # de los bytes, calculado acá
    vector: np.ndarray | None = None


def _upload_hosts() -> set[str]:
    raw = get_settings().estimate_upload_image_hosts or ""
    return {h.strip().lower().rstrip(".") for h in raw.split(",") if h.strip()}


def allowed_search_image(url: object) -> str:
    """La foto que se puede mandar a Apify y a Qwen, o "" si no."""
    target = apify_1688.allowed_image_url(url)
    if not target:
        return ""
    host = (urlsplit(target).hostname or "").lower()
    if any(host == d or host.endswith("." + d) for d in _CDN_DOMAINS):
        return target
    return target if host in _upload_hosts() else ""


def pick_search_image(image_urls: Sequence[str]) -> str:
    """La primera foto del link (o la subida) que puede salir de Hugo."""
    for url in image_urls:
        target = allowed_search_image(url)
        if target:
            return target
    return ""


def clip_available() -> bool:
    return image_embed.available()


def _vector(raw: bytes) -> np.ndarray | None:
    return image_embed._infer(raw) if clip_available() else None


async def load_query_photo(url: str, *, timeout_s: float) -> QueryPhoto:
    """Baja la foto UNA vez: sha256 de los bytes y vector CLIP. Si no se pudo
    bajar, la foto igual puede ir a Apify, pero sin sha (caché por URL) ni CLIP."""
    photo = QueryPhoto(url=url)
    try:
        raw = await asyncio.wait_for(_fetch(url, interactive=True), timeout=max(timeout_s, 0.1))
    except Exception as exc:  # noqa: BLE001  (timeout, SSRF, HTTP, tamaño)
        log.info("estimado: no se pudo bajar la foto (%s)", type(exc).__name__)
        return photo
    photo.sha256 = hashlib.sha256(raw).hexdigest()
    try:
        photo.vector = await asyncio.to_thread(_vector, raw)
    except Exception as exc:  # noqa: BLE001
        log.info("estimado: CLIP de la foto falló (%s)", type(exc).__name__)
    return photo


async def embed_thumbnails(urls: Sequence[str], *, timeout_s: float) -> dict[str, np.ndarray]:
    """{url: vector} de las miniaturas que llegaron antes de `timeout_s`."""
    if not urls or not clip_available():
        return {}
    sem = asyncio.Semaphore(_THUMB_CONCURRENCY)
    out: dict[str, np.ndarray] = {}

    async def one(url: str) -> None:
        async with sem:
            try:
                raw = await _fetch(url, interactive=True)
                vec = await asyncio.to_thread(_vector, raw)
            except Exception:  # noqa: BLE001
                return
        if vec is not None:
            out[url] = vec

    tasks = [asyncio.create_task(one(u)) for u in dict.fromkeys(urls)]
    _, pending = await asyncio.wait(tasks, timeout=max(timeout_s, 0.1))
    for task in pending:
        task.cancel()
    return dict(out)


def cosine(a: np.ndarray | None, b: np.ndarray | None) -> float | None:
    if a is None or b is None:
        return None
    return float(np.dot(a, b))
