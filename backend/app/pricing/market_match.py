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

Igual / similar / diferente (pedido de Nico: "que cuente SOLO lo idéntico"):
`MATCH` es IGUAL y es lo único que entra a mediana, mínimo, ganancia y color.
`SIMILAR` es el mismo tipo de producto pero con una diferencia que importa
(marca conocida, pack, medidas, capacidad): se guarda aparte para mostrarlo y
nunca toca el color. Dos cosas pueden bajar un MATCH a SIMILAR: el veredicto del
juez (`apply_judge_verdict`) y el chequeo de medidas (`apply_specs`).

Productos deshabilitados: el índice CLIP del app solo tiene los habilitados (un
duplicado deshabilitado no debe devolverse como "lo tenemos"). Para el semáforo
sus fotos se embeben al vuelo (cache L2) y se comparan en el MISMO espacio
centrado del índice, así los umbrales valen igual.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

import numpy as np

from app import runtime
from app.config import get_settings
from app.dedup import catalog_index, fuzzy_text, image_embed
from app.pricing import market_judge, market_specs
from app.pricing.market_ml import MlCandidate, safe_image_url
from app.vendure.client import VendureProduct

log = logging.getLogger(__name__)

MATCH = "match"
SIMILAR = "similar"
NO = "no"
AMBIGUOUS = "ambiguous"

SOURCE_CLIP = "clip"
SOURCE_CLIP_NAME = "clip+nombre"
SOURCE_LLM = "llm"
SOURCE_SPECS = "specs"
# Una persona la marcó ("Es el mismo" / "No es el mismo"): manda sobre todo lo demás.
SOURCE_MANUAL = "manual"
# Parecida en foto y nombre pero sin confirmar (no hay juez, no hay cupo o no
# contestó): se muestra como SIMILAR, no entra a ningún cálculo real.
SOURCE_UNCONFIRMED = "ambiguo"

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
    verdict: str                 # match (= IGUAL) | similar | no | ambiguous
    source: str | None = None    # clip | clip+nombre | llm | specs
    confidence: float | None = None
    reason: str = ""
    # Qué cambia respecto de lo nuestro (vocabulario cerrado), si es SIMILAR.
    differences: list[str] = field(default_factory=list)
    # El juez opinó sobre esta publicación (aunque no haya cambiado el veredicto).
    judged: bool = False
    # Avisos que no cambian el veredicto ("medida dudosa en Vendure").
    notes: list[str] = field(default_factory=list)


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


def apply_judge_verdict(
    d: Decision, v: "market_judge.JudgeVerdict", *,
    igual_min: float = market_judge.MIN_CONFIDENCE,
    similar_min: float = market_judge.SIMILAR_MIN_CONFIDENCE,
) -> None:
    """Aplica el veredicto del juez a una decisión. Puro.

    Banda ambigua (el juez es el único que decide):
        igual   con confianza ≥ igual_min     → MATCH (fuente llm)
        igual   entre similar_min e igual_min → SIMILAR (ante la duda no se contamina el precio)
        similar con confianza ≥ similar_min   → SIMILAR
        el resto                              → NO

    Ya aceptada por reglas (foto + nombre) y revisada por el juez para aplicar la
    regla de marca: solo cambia si el juez está SEGURO (confianza ≥ igual_min).
    "igual" la confirma, "similar" la baja a SIMILAR y "diferente" la descarta;
    un juez dudoso no la toca ni deja su confianza anotada (la foto ya la había
    aceptado y mostrar "confirmado por el juez 40 %" sería engañoso).
    """
    cat, conf = v.cat, v.confidence
    if d.verdict == MATCH:
        if conf < igual_min:
            return
        new = {market_judge.CAT_IGUAL: MATCH, market_judge.CAT_SIMILAR: SIMILAR}.get(cat, NO)
    elif cat == market_judge.CAT_IGUAL and conf >= igual_min:
        new = MATCH
    elif cat in (market_judge.CAT_IGUAL, market_judge.CAT_SIMILAR) and conf >= similar_min:
        new = SIMILAR
    else:
        new = NO
    d.confidence, d.reason, d.judged = conf, v.reason, True
    d.differences = list(v.differences) if new == SIMILAR else []
    if new == d.verdict:
        return
    d.verdict = new
    d.source = SOURCE_LLM


def rank_key(d: Decision) -> tuple[float, float]:
    """Orden por parecido, el más parecido primero (foto y después nombre)."""
    return (d.image_score if d.image_score is not None else -1.0, d.name_score)


def explain_different(d: Decision, thr: Thresholds) -> str:
    """Motivo corto de por qué una publicación quedó DIFERENTE (o, sin juez, solo
    "parecida sin confirmar"). Lo que ya trae un motivo (juez, medidas, una
    persona) lo conserva."""
    if d.reason:
        return d.reason
    if d.image_score is None:
        return "no se pudo comparar la foto (sin foto o sin CLIP)"
    if d.image_score < thr.image_veto:
        return "otro producto: la foto no se parece"
    if d.name_score < thr.name_veto:
        return "otro producto: el nombre no tiene relación"
    if d.verdict == NO:
        return "el juez no lo confirma como el mismo producto"
    return "parecido en foto y nombre, sin confirmar"


def apply_specs(
    d: Decision, our_name: str, our_specs: "market_specs.OurSpecs | None", *,
    dim_tol_pct: float, weight_tol_pct: float,
) -> None:
    """Baja un MATCH a SIMILAR si la cantidad, la capacidad, las medidas o el
    peso de la publicación difieren de los nuestros (ver market_specs)."""
    if d.verdict != MATCH or d.source == SOURCE_MANUAL:
        return                                   # una persona dijo que es el mismo
    result = market_specs.check(
        our_name, our_specs, d.candidate.name, d.candidate.attributes,
        dim_tol_pct=dim_tol_pct, weight_tol_pct=weight_tol_pct,
    )
    d.notes = list(result.notes)
    if result.differences:
        d.verdict = SIMILAR
        d.source = SOURCE_SPECS
        d.differences = sorted({*d.differences, *result.differences})
        d.reason = d.reason or "difiere en " + ", ".join(result.differences)


ImageScorer = Callable[[VendureProduct, Sequence[str]], Awaitable[float | None]]


def indexed(product: VendureProduct) -> bool:
    """¿Podemos puntuar este producto por imagen con el scorer por defecto?
    Se chequea ANTES de buscar en ML: un producto sin fotos en el índice
    terminaría `skipped` igual, después de gastar el request."""
    if not (image_embed.available() and catalog_index.is_ready()):
        return False
    if catalog_index.has_product(product.id):
        return True
    # Deshabilitado: no está en el índice a propósito, sus fotos se embeben al vuelo.
    return not product.enabled and bool(_own_photos(product))


def _own_photos(product: VendureProduct) -> list[str]:
    """Fotos propias para embeber al vuelo (las mismas que indexa el catálogo)."""
    urls = [u for u in [product.featured_image_url, *(product.image_urls or [])] if u]
    return list(dict.fromkeys(urls))[:max(1, get_settings().embed_images_per_product)]


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
    own: list[np.ndarray] | None = None
    if not catalog_index.has_product(our.id):
        # Deshabilitado (fuera del índice): se compara contra sus fotos embebidas
        # al vuelo, proyectadas al mismo espacio centrado.
        own = [v for v in await image_embed.embed_urls_aligned(_own_photos(our), concurrency=2)
               if v is not None]
        if not own:
            return None
        own = [p for p in (catalog_index.project(v) for v in own) if p is not None]
    best: float | None = None
    for vec in vecs:
        if vec is None:
            continue
        if own is not None:
            scores = [float(np.dot(catalog_index.project(vec), o)) for o in own]
        else:
            scores = [score for _p, score, _u in catalog_index.score_products(vec, [our.id])]
        for score in scores:
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
