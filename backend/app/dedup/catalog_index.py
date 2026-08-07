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

Centrado del índice
-------------------
Los embeddings de CLIP no ocupan la esfera entera: todas las fotos de producto
comparten una dirección común (fondo blanco, objeto centrado, estudio) que mete
un piso de coseno alto entre cosas que no tienen NADA que ver. Medido sobre 194
fotos de e-commerce de categorías distintas: mediana 0.60, p99 0.82, max 0.88.

El problema no es ese piso sino que acá tomamos el MÁXIMO contra ~1500 fichas.
El máximo de N muestras crece con N, así que el argmax siempre encuentra algo
arriba del umbral aunque el producto no esté en el catálogo:

    catálogo N=50   → max contra productos sin relación: mediana 0.780
    catálogo N=100  → mediana 0.798, 29.9% pasa 0.82, 9.3% pasa 0.88
    catálogo N=150  → mediana 0.801, 34.5% pasa 0.82, 11.3% pasa 0.88

Con umbral absoluto sobre 1500 fichas eso es un falso positivo garantizado.

Restarle a cada vector la media del índice y renormalizar mata esa dirección
común. Medido contra el catálogo real (1006 productos del canal 'ar', 173 queries
con una foto held-out del mismo producto), a igual tasa de falsos positivos:

                              coseno crudo   centrado
    umbral para 1% de FP          0.891        0.720
      recupera                    12.9%        18.5%
    umbral para 10% de FP         0.867        0.619
      recupera                    18.8%        32.9%

La escala de scores cambia: los umbrales de `embed_*_threshold` están calibrados
para el espacio centrado. Para recalibrarlos correr
`python -m app.dedup.calibrate_embed`.

Lo que el centrado NO arregla
-----------------------------
El catálogo es casi todo la misma categoría (organizadores, estantes, cocina) y
CLIP no distingue un "Organizador Doble Ajustable" de un "Organizador de Baño
Multitalle": se ven igual. Medido, el producto correcto queda top-1 solo el 52.6%
de las veces y en el top-20 el 80.3%. Por eso la imagen sirve para ARMAR una
lista corta, no para decidir sola: el que decide es el nombre. Subir el umbral
compra precisión pagando recall (1% de FP = 18.5% de recall), no la arregla.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable

import numpy as np

from app.config import get_settings
from app.dedup import image_embed
from app.vendure import catalog as vendure_catalog
from app.vendure.client import VendureProduct

log = logging.getLogger(__name__)


class _State:
    matrix: np.ndarray = np.zeros((0, image_embed.EMBED_DIM), dtype=np.float32)
    # Media de los vectores crudos del índice. Se le resta al query antes de
    # buscar para que compare en el mismo espacio que `matrix`. Ceros = índice
    # sin centrar (centrado apagado, o todavía sin construir).
    mean: np.ndarray = np.zeros(image_embed.EMBED_DIM, dtype=np.float32)
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


# Con muy pocos vectores la media no representa nada (con 1 vector el centrado
# lo anula) y el índice queda peor que sin centrar.
_MIN_VECTORS_TO_CENTER = 8


def _renormalize(mat: np.ndarray) -> np.ndarray:
    """Renormaliza filas a norma 1. Las filas nulas quedan nulas (score 0)."""
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms <= 0.0] = 1.0
    return (mat / norms).astype(np.float32)


def _center(raw: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(matriz centrada+renormalizada, media). Sin centrar si hay pocos vectores."""
    zero = np.zeros(image_embed.EMBED_DIM, dtype=np.float32)
    if not get_settings().embed_center_index or raw.shape[0] < _MIN_VECTORS_TO_CENTER:
        return raw.astype(np.float32), zero
    mean = raw.mean(axis=0).astype(np.float32)
    return _renormalize(raw - mean), mean


def project(query: np.ndarray | None) -> np.ndarray | None:
    """Lleva un vector de query al mismo espacio que `_state.matrix`.

    Sin centrado (media en ceros) devuelve el vector tal cual. Con centrado le
    resta la media del índice y renormaliza — exactamente lo que se le hizo a
    cada fila de la matriz, que es lo que hace comparables los cosenos.
    """
    if query is None:
        return None
    if not _state.mean.any():
        return query
    centered = np.asarray(query, dtype=np.float32) - _state.mean
    norm = float(np.linalg.norm(centered))
    if norm <= 0.0:
        # El query ES la media del catálogo: no aporta señal, que puntúe 0 y no
        # que reviente por división por cero.
        return centered
    return (centered / norm).astype(np.float32)


def is_centered() -> bool:
    return bool(_state.mean.any())


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
        "centered": is_centered(),
        "last_error": _state.last_error,
    }


# Umbral máximo creíble en el espacio centrado: ahí el par correcto da ~0.72 y
# el mejor impostor ~0.38. Un valor por encima de esto es un resto de la escala
# vieja (sin centrar) que quedó guardado como override en el dashboard, y deja
# /app/lookup sin matchear nada.
_UNCENTERED_THRESHOLD_HINT = 0.80


def effective_threshold(key: str, value: float) -> float:
    """Descarta un umbral guardado en la escala VIEJA (índice sin centrar).

    Los overrides del dashboard sobreviven al deploy. Si alguien había subido
    `embed_match_threshold` a 0.88 cuando el índice no se centraba, ese valor
    pisa el default nuevo y /app/lookup deja de encontrar nada — en la escala
    centrada el match correcto da ~0.72, así que 0.88 no lo alcanza nunca.

    Un falso negativo silencioso es peor que un override ignorado: el cliente ve
    "no lo tenemos" sobre productos que sí tenemos y nadie se entera. Por eso
    acá gana el default, con un warning para que se corrija en el dashboard.
    """
    if not is_centered() or value <= _UNCENTERED_THRESHOLD_HINT:
        return value
    default = float(getattr(get_settings(), key, value))
    log.warning(
        "%s guardado en %.2f es de la escala vieja (sin centrar): uso %.2f. "
        "Reseteá ese setting a default en el dashboard.",
        key, value, default,
    )
    return default


def _warn_if_thresholds_look_uncentered() -> None:
    """Avisa si los umbrales guardados son de la escala vieja (índice sin centrar)."""
    if not is_centered():
        return
    try:
        from app import runtime

        stale = {
            key: float(runtime.get(key))
            for key in ("embed_match_threshold", "embed_suggest_threshold")
            if float(runtime.get(key)) > _UNCENTERED_THRESHOLD_HINT
        }
    except Exception:  # noqa: BLE001
        return
    if stale:
        log.warning(
            "El índice CLIP está centrado pero %s siguen en la escala vieja. "
            "En el espacio centrado el match correcto da ~0.72: con esos valores "
            "/app/lookup no va a encontrar nada. Reseteá esos settings a default "
            "en el dashboard o recalibrá con app.dedup.calibrate_embed.",
            stale,
        )


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

            raw = (
                np.vstack(vectors).astype(np.float32)
                if vectors
                else np.zeros((0, image_embed.EMBED_DIM), dtype=np.float32)
            )
            _state.matrix, _state.mean = _center(raw)
            _state.product_ids = product_ids
            _state.image_urls = image_urls
            _state.products = {p.id: p for p, _ in targets}
            _state.built_at = time.monotonic()
            log.info(
                "Índice CLIP construido: %d vectores de %d productos (centrado=%s)",
                _state.matrix.shape[0], len(_state.products), is_centered(),
            )
            _warn_if_thresholds_look_uncentered()
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
    scores = _state.matrix @ project(query)
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


def score_products(
    query: np.ndarray, product_ids: Iterable[str]
) -> list[tuple[VendureProduct, float, str]]:
    """Score de `query` contra productos PUNTUALES, sin pasar por el ranking.

    `search` solo devuelve el top-K global: un producto cuya foto de catálogo es
    una lámina de marketing (varias unidades, fondo de color, watermark) puntúa
    ~0.70 contra la foto blanca del marketplace y nunca entra al top-K, aunque sea
    el producto correcto. Cuando otra señal (el nombre) ya lo propuso, necesitamos
    su score igual: esto lo calcula para esos ids y nada más.
    """
    if not is_ready() or query is None:
        return []
    wanted = {pid for pid in product_ids if pid in _state.products}
    if not wanted:
        return []
    scores = _state.matrix @ project(query)
    best: dict[str, tuple[float, str]] = {}
    for i, pid in enumerate(_state.product_ids):
        if pid not in wanted:
            continue
        score = float(scores[i])
        prev = best.get(pid)
        if prev is None or score > prev[0]:
            best[pid] = (score, _state.image_urls[i])
    return [(_state.products[pid], score, url) for pid, (score, url) in best.items()]
