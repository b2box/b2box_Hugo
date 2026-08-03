"""Índice vectorial del catálogo Vendure (para buscar por imagen).

`/app/lookup` recibe una URL, saca la foto y necesita responder "¿tenemos esto?"
en un par de segundos. Comparar la foto contra las ~1500 fichas del catálogo una
por una (como hace el dedup de /verify) es imposible en ese tiempo.

Acá precalculamos los embeddings CLIP de las imágenes del catálogo una sola vez
y los dejamos en una matriz N×512 normalizada. Buscar es entonces un producto
matriz-vector en numpy: microsegundos, sin red.

Costo del primer build: descarga + inferencia de ~N imágenes. Con 1 CPU son
varios minutos. Por eso:
  - los embeddings se persisten (tabla image_embed_cache) → los rebuilds
    posteriores no vuelven a descargar ni a inferir;
  - el build corre en background y `status()` dice si está listo. Mientras no lo
    esté, /app/lookup NO manda el formulario a Cloud: un índice a medias diría
    "no lo tenemos" sobre productos que sí tenemos.
"""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from app.config import get_settings
from app.dedup import image_embed
from app.vendure import catalog as vendure_catalog
from app.vendure.client import VendureProduct

log = logging.getLogger(__name__)


class _State:
    matrix: np.ndarray = np.zeros((0, image_embed.EMBED_DIM), dtype=np.float32)
    product_ids: list[str] = []
    image_urls: list[str] = []
    products: dict[str, VendureProduct] = {}
    built_at: float = 0.0
    building: bool = False
    progress_done: int = 0
    progress_total: int = 0
    last_error: str | None = None


_state = _State()
_lock = asyncio.Lock()


def _images_for(product: VendureProduct, limit: int) -> list[str]:
    """Imágenes a indexar de un producto: la featured primero, sin repetir."""
    urls: list[str] = []
    if product.featured_image_url:
        urls.append(product.featured_image_url)
    for u in product.image_urls or []:
        if u and u not in urls:
            urls.append(u)
    return [u for u in urls if u][:max(1, limit)]


def is_ready() -> bool:
    return _state.matrix.shape[0] > 0


def is_stale() -> bool:
    ttl = get_settings().embed_index_ttl_seconds
    return (time.monotonic() - _state.built_at) > ttl


def status() -> dict:
    """Estado del índice, para el endpoint de diagnóstico y para /app/lookup."""
    return {
        "ready": is_ready(),
        "building": _state.building,
        "vectors": int(_state.matrix.shape[0]),
        "products": len(set(_state.product_ids)),
        "progress_done": _state.progress_done,
        "progress_total": _state.progress_total,
        "stale": is_stale() if is_ready() else True,
        "embed_available": image_embed.available(),
        "last_error": _state.last_error,
    }


def invalidate() -> None:
    """Marca el índice como viejo; el próximo ensure_index() lo reconstruye."""
    _state.built_at = 0.0


async def build(force: bool = False) -> dict:
    """Reconstruye el índice. Idempotente y protegido por lock."""
    async with _lock:
        if not force and is_ready() and not is_stale():
            return status()
        if not image_embed.available():
            _state.last_error = "modelo CLIP no disponible"
            return status()

        _state.building = True
        _state.last_error = None
        try:
            products = await vendure_catalog.get_catalog()
            per_product = get_settings().embed_images_per_product
            # Solo productos habilitados: un duplicado deshabilitado no debe
            # devolverse al app como "lo tenemos, comprá ahora".
            targets = [(p, _images_for(p, per_product)) for p in products if p.enabled]
            targets = [(p, urls) for p, urls in targets if urls]

            _state.progress_total = sum(len(urls) for _, urls in targets)
            _state.progress_done = 0

            vectors: list[np.ndarray] = []
            product_ids: list[str] = []
            image_urls: list[str] = []

            for product, urls in targets:
                # aligned: mantiene la posición de las que fallaron (None), así
                # cada vector queda apareado con SU url.
                vecs = await image_embed.embed_urls_aligned(urls)
                _state.progress_done += len(urls)
                for url, vec in zip(urls, vecs):
                    if vec is None:
                        continue
                    vectors.append(vec)
                    product_ids.append(product.id)
                    image_urls.append(url)
                # Cede el event loop: el build puede tardar minutos y no debe
                # dejar sin atender los requests HTTP mientras tanto.
                await asyncio.sleep(0)

            _state.matrix = (
                np.vstack(vectors).astype(np.float32)
                if vectors
                else np.zeros((0, image_embed.EMBED_DIM), dtype=np.float32)
            )
            _state.product_ids = product_ids
            _state.image_urls = image_urls
            _state.products = {p.id: p for p, _ in targets}
            _state.built_at = time.monotonic()
            log.info(
                "Índice CLIP construido: %d vectores de %d productos",
                _state.matrix.shape[0], len(_state.products),
            )
        except Exception as exc:  # noqa: BLE001
            _state.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("Build del índice CLIP falló")
        finally:
            _state.building = False
        return status()


async def ensure_index() -> None:
    """Dispara un build en background si hace falta. No bloquea al llamador."""
    if _state.building:
        return
    if is_ready() and not is_stale():
        return
    asyncio.create_task(build())


def search(query: np.ndarray, top_k: int = 3) -> list[tuple[VendureProduct, float, str]]:
    """Productos más parecidos a `query`, de mayor a menor score.

    Devuelve (producto, score coseno, url de la imagen que matcheó). Un producto
    aparece una sola vez, con su mejor imagen.
    """
    if not is_ready() or query is None:
        return []
    scores = _state.matrix @ query
    order = np.argsort(-scores)
    out: list[tuple[VendureProduct, float, str]] = []
    seen: set[str] = set()
    for i in order:
        pid = _state.product_ids[int(i)]
        if pid in seen:
            continue
        product = _state.products.get(pid)
        if product is None:
            continue
        seen.add(pid)
        out.append((product, float(scores[int(i)]), _state.image_urls[int(i)]))
        if len(out) >= top_k:
            break
    return out
