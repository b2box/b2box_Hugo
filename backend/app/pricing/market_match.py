"""Filtro "mismo producto": ¿esta ficha de ML es lo que vendemos nosotros?

La búsqueda en ML es por título y devuelve hasta 20 fichas; ML ordena por su
relevancia comercial, no por parecido. Acá decidimos cuáles son el mismo
producto con dos señales, igual que /app/lookup (app_routes._best_embed_candidate):

  * imagen: CLIP, en la escala CENTRADA del índice del catálogo
    (`catalog_index.score_products`). Se eligió esa escala y no el coseno
    crudo de `image_embed.similarity` por tres motivos: (1) es la que ya está
    calibrada contra ESTE catálogo (embed_match_threshold=0.72 → 1 % de falsos
    positivos; impostor mediano ~0.38-0.49), (2) nuestras fotos ya están en el
    índice, así que por noche solo se embeben las fotos de ML (y quedan en el
    cache L2), y (3) el coseno crudo tiene un piso alto entre fotos de producto
    que no tienen nada que ver (mediana 0.60, p99 0.82): un umbral absoluto
    sobre esa escala es frágil. Los umbrales `pm_image_*` están en la escala
    centrada; se recalibran con `python -m app.pricing.calibrate_market_match`.
  * nombre: `fuzzy_text.text_similarity` entre nuestro nombre y el de la ficha.

Reglas, en orden (ver `classify`):

    imagen < pm_image_veto  o  nombre < pm_name_veto      → NO (vetado)
    imagen ≥ pm_image_strong                              → match "clip"
    imagen ≥ pm_image_threshold y nombre ≥ pm_name_threshold → match "clip+nombre"
    el resto                                              → AMBIGUO

La banda ambigua es la única que mira el juez LLM (market_judge.py), y solo si
`pm_vision_max_calls` > 0. Sin juez, ambiguo = no es el mismo producto: en
sombra preferimos un `no_data` a inventar un precio de referencia.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from app import runtime
from app.dedup import catalog_index, fuzzy_text, image_embed
from app.pricing.market_ml import MlCandidate, safe_image_url
from app.vendure.client import VendureProduct

log = logging.getLogger(__name__)

MATCH = "match"
NO = "no"
AMBIGUOUS = "ambiguous"

SOURCE_CLIP = "clip"
SOURCE_CLIP_NAME = "clip+nombre"
SOURCE_LLM = "llm"

# Fotos de cada ficha de ML que se embeben. La primera es la foto principal
# (fondo blanco) y es la que mejor compara contra nuestra featured. Cada foto
# extra son ~1.500 descargas más por noche la primera vez.
PHOTOS_PER_CANDIDATE = 1
# Cuántas fichas de la búsqueda se puntúan por imagen. Vienen ordenadas por
# parecido de nombre, así que las de abajo casi nunca son el producto.
CANDIDATES_TO_SCORE = 6

_CODE_TOKEN = re.compile(r"^(?:bx|pa)[-_]?\d+[a-z]*$", re.I)
_WS = re.compile(r"\s+")


@dataclass(slots=True, frozen=True)
class Thresholds:
    image: float
    name: float
    image_strong: float
    image_veto: float
    name_veto: float

    @classmethod
    def from_runtime(cls) -> "Thresholds":
        return cls(
            image=float(runtime.get("pm_image_threshold")),
            name=float(runtime.get("pm_name_threshold")),
            image_strong=float(runtime.get("pm_image_strong")),
            image_veto=float(runtime.get("pm_image_veto")),
            name_veto=float(runtime.get("pm_name_veto")),
        )


@dataclass(slots=True)
class Decision:
    candidate: MlCandidate
    image_score: float | None
    name_score: float
    verdict: str                 # match | no | ambiguous
    source: str | None = None    # clip | clip+nombre | llm
    confidence: float | None = None
    reason: str = ""


def classify(image_score: float | None, name_score: float, thr: Thresholds) -> tuple[str, str | None]:
    """(veredicto, fuente). Puro. Sin score de imagen no hay match posible:
    el nombre solo no alcanza para fijar un precio de referencia."""
    if image_score is None:
        return NO, None
    if image_score < thr.image_veto or name_score < thr.name_veto:
        return NO, None
    if image_score >= thr.image_strong:
        return MATCH, SOURCE_CLIP
    if image_score >= thr.image and name_score >= thr.name:
        return MATCH, SOURCE_CLIP_NAME
    return AMBIGUOUS, None


def search_query(name: str) -> str:
    """Nombre del producto → query para ML. Saca códigos internos (BX0123,
    PA-45) que en ML no significan nada y colapsa espacios."""
    words = [w for w in _WS.split(name or "") if w and not _CODE_TOKEN.match(w)]
    return " ".join(words)[:120].strip()


# Palabras de la segunda búsqueda. Nuestros nombres suelen arrastrar medidas,
# packs y adjetivos ("Organizador Doble Ajustable 3 Niveles 40x30 Blanco") que
# en ML vuelven la búsqueda demasiado específica y devuelven 0 fichas.
FALLBACK_WORDS = 4


def fallback_query(name: str) -> str | None:
    """Segunda query cuando la primera no trajo NADA: las primeras palabras
    del nombre. None si no hay nada más corto que probar (así nunca se gasta
    un request repitiendo la misma búsqueda)."""
    words = search_query(name).split()
    if len(words) <= FALLBACK_WORDS:
        return None
    return " ".join(words[:FALLBACK_WORDS])


def name_score(our_name: str, ml_name: str) -> float:
    return fuzzy_text.text_similarity(our_name, "", ml_name, "")


ImageScorer = Callable[[VendureProduct, Sequence[str]], Awaitable[float | None]]


def indexed(product: VendureProduct) -> bool:
    """¿Podemos puntuar este producto por imagen con el scorer por defecto?
    Se chequea ANTES de buscar en ML: un producto sin fotos en el índice
    terminaría `skipped` igual, después de gastar el request."""
    return image_embed.available() and catalog_index.is_ready() and catalog_index.has_product(product.id)


async def clip_index_scorer(our: VendureProduct, urls: Sequence[str]) -> float | None:
    """Score de imagen por defecto: mejor coseno (escala centrada) entre las
    fotos de la ficha de ML y las fotos indexadas de NUESTRO producto.

    None cuando no se puede comparar: CLIP apagado, índice sin construir,
    producto sin foto en el índice, o ninguna foto de ML se pudo embeber.
    """
    safe = [u for u in (safe_image_url(x) for x in urls) if u]
    if not safe or not image_embed.available() or not catalog_index.is_ready():
        return None
    vecs = await image_embed.embed_urls_aligned(safe[:PHOTOS_PER_CANDIDATE], concurrency=2)
    best: float | None = None
    for vec in vecs:
        if vec is None:
            continue
        for _product, score, _url in catalog_index.score_products(vec, [our.id]):
            best = score if best is None else max(best, score)
    return best


async def score_candidates(
    our: VendureProduct,
    candidates: Sequence[MlCandidate],
    thr: Thresholds,
    *,
    scorer: ImageScorer = clip_index_scorer,
    max_candidates: int = CANDIDATES_TO_SCORE,
) -> list[Decision]:
    """Puntúa y clasifica las fichas. Primero por nombre (gratis), y solo las
    `max_candidates` más parecidas pasan por imagen (descarga + CLIP)."""
    ranked = sorted(
        ((name_score(our.name, c.name), c) for c in candidates),
        key=lambda pair: pair[0], reverse=True,
    )
    decisions: list[Decision] = []
    for n_score, cand in ranked[:max(1, max_candidates)]:
        img = await scorer(our, cand.image_urls) if cand.image_urls else None
        verdict, source = classify(img, n_score, thr)
        decisions.append(Decision(cand, img, n_score, verdict, source))
    return decisions
