"""Orquestador de deduplicación.

Combina las 3 estrategias (URL, imagen, texto) en un solo veredicto.

Estrategia:
  1. Si la URL coincide → match seguro (confianza 1.0). No hace falta más.
  2. Si NO hay URL match, calculamos imagen y texto en paralelo.
  3. Devolvemos un DedupVerdict con:
       - is_duplicate: True si CUALQUIER score supera su threshold
       - confidence: el score más alto
       - matched_by: qué estrategia(s) dispararon
       - per_strategy_scores: detalle de cada una

Esta lógica es la que va a usar la API (POST /verify) y el scheduler (audit).
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from app import runtime
from app.dedup.fuzzy_text import text_similarity
from app.dedup.image_hash import image_similarity
from app.dedup.url_match import normalize_url, url_similarity
from app.vendure.client import VendureProduct

Strategy = Literal["url", "image", "text"]


@dataclass(slots=True)
class DedupVerdict:
    is_duplicate: bool
    confidence: float
    matched_by: list[Strategy] = field(default_factory=list)
    per_strategy_scores: dict[Strategy, float] = field(default_factory=dict)
    candidate_id: str | None = None  # id del producto contra el que matcheó


@dataclass(slots=True)
class CandidateInput:
    """Datos de un producto-candidato (puede no estar en Vendure todavía)."""

    name: str
    description: str
    source_url: str | None
    image_urls: list[str]


def _from_vendure(p: VendureProduct) -> CandidateInput:
    return CandidateInput(
        name=p.name,
        description=p.description,
        source_url=p.source_url,
        image_urls=p.image_urls,
    )


async def compare(a: CandidateInput, b: CandidateInput) -> DedupVerdict:
    """Compara dos candidatos y emite un veredicto."""
    url_th = runtime.get("dedup_url_threshold")
    image_th = runtime.get("dedup_image_threshold")
    text_th = runtime.get("dedup_text_threshold")

    # 1) URL — barato, exacto, cortocircuita
    url_score = url_similarity(a.source_url, b.source_url)
    if url_score >= url_th:
        return DedupVerdict(
            is_duplicate=True,
            confidence=url_score,
            matched_by=["url"],
            per_strategy_scores={"url": url_score, "image": 0.0, "text": 0.0},
        )

    # 2) texto primero (CPU barato, sin red). La imagen cuesta $/red: solo la
    #    calculamos si el texto ya sugiere parentesco (gate) o si el par ya
    #    matchearía por texto. Esto corta el O(N²) de descargas de imagen entre
    #    productos claramente distintos.
    text_score = await asyncio.to_thread(
        text_similarity, a.name, a.description, b.name, b.description
    )
    image_gate = runtime.get("dedup_image_text_gate") or 0.0
    if text_score >= image_gate or text_score >= text_th:
        image_score = await image_similarity(a.image_urls, b.image_urls)
    else:
        image_score = 0.0

    matched: list[Strategy] = []
    if url_score >= url_th:
        matched.append("url")
    if image_score >= image_th:
        matched.append("image")
    if text_score >= text_th:
        matched.append("text")

    confidence = max(url_score, image_score, text_score)
    return DedupVerdict(
        is_duplicate=bool(matched),
        confidence=confidence,
        matched_by=matched,
        per_strategy_scores={"url": url_score, "image": image_score, "text": text_score},
    )


async def find_duplicate_in(
    candidate: CandidateInput,
    existing: list[VendureProduct],
) -> DedupVerdict:
    """Compara `candidate` contra una lista de productos existentes en Vendure
    y devuelve el match de más alta confianza (o no-duplicado si nadie pasa)."""
    if not existing:
        return DedupVerdict(is_duplicate=False, confidence=0.0)

    best = DedupVerdict(is_duplicate=False, confidence=0.0)
    for product in existing:
        verdict = await compare(candidate, _from_vendure(product))
        if verdict.confidence > best.confidence:
            best = verdict
            best.candidate_id = product.id
            if verdict.is_duplicate and "url" in verdict.matched_by:
                # match seguro, no seguimos buscando
                return best
    return best


# ─── Batch dedup para el scheduler (audit_duplicates) ──────────────
# El barrido completo es O(N²) si comparás todos contra todos (1494 productos =
# ~2,2M pares). Acá lo bajamos con dos técnicas:
#   1. Buckets de URL exacta → duplicados seguros sin comparar texto/imagen.
#   2. Índice invertido de tokens de nombre → solo comparamos pares que comparten
#      señal de texto (≥2 tokens, o un token poco frecuente). El resto ni se toca.
# `compare()` (con su gate de imagen) sigue siendo el que puntúa cada par
# candidato, así que la lógica de match no cambia — solo dejamos de comparar
# pares obviamente no relacionados.

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "the", "and", "for", "with", "de", "la", "el", "los", "las", "un", "una",
    "para", "con", "por", "cm", "mm", "ml", "pcs", "set", "new", "pro", "usb",
}
# Un token con document-frequency por encima de esto se considera "común": no
# alcanza por sí solo para emparejar (hace falta compartir ≥2 tokens).
_COMMON_DF = 60
_MAX_TOKENS_PER_PRODUCT = 12


@dataclass(slots=True)
class DuplicatePair:
    drop: VendureProduct   # producto a marcar como duplicado
    keep: VendureProduct   # producto "canónico" que se conserva
    verdict: DedupVerdict


def _tokenize(name: str) -> list[str]:
    toks = [t for t in _TOKEN_RE.findall((name or "").lower()) if len(t) >= 3 and t not in _STOPWORDS]
    # Dedupe preservando orden, y cap defensivo por producto.
    seen: set[str] = set()
    out: list[str] = []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
        if len(out) >= _MAX_TOKENS_PER_PRODUCT:
            break
    return out


def _candidate_pairs(products: list[VendureProduct]) -> set[tuple[int, int]]:
    """Devuelve los pares (i, j) que vale la pena comparar por texto/imagen.

    Un par entra si comparte ≥2 tokens de nombre, o comparte al menos un token
    poco frecuente (df ≤ _COMMON_DF). Corta el O(N²) a los pares con señal real.
    """
    tokens = [_tokenize(p.name) for p in products]
    inverted: dict[str, list[int]] = defaultdict(list)
    for i, toks in enumerate(tokens):
        for t in toks:
            inverted[t].append(i)

    pairs: set[tuple[int, int]] = set()
    shared_count: dict[tuple[int, int], int] = defaultdict(int)
    for t, idxs in inverted.items():
        df = len(idxs)
        if df < 2:
            continue
        rare = df <= _COMMON_DF
        for a_pos in range(len(idxs)):
            for b_pos in range(a_pos + 1, len(idxs)):
                i, j = idxs[a_pos], idxs[b_pos]
                key = (i, j) if i < j else (j, i)
                if rare:
                    pairs.add(key)
                else:
                    shared_count[key] += 1
                    if shared_count[key] >= 2:
                        pairs.add(key)
    return pairs


async def find_duplicate_pairs(
    products: list[VendureProduct],
    changed_ids: set[str] | None = None,
) -> list[DuplicatePair]:
    """Detecta duplicados en todo el catálogo de forma eficiente.

    Devuelve, por cada producto marcado como duplicado (`drop`), el mejor match
    canónico (`keep`) y el veredicto. Solo productos habilitados se consideran.

    Si `changed_ids` viene (dedup incremental), las comparaciones de texto/imagen
    solo se corren para pares que incluyen al menos un producto nuevo/cambiado —
    los pares viejo-viejo ya se compararon en corridas anteriores. Los buckets de
    URL exacta se evalúan siempre (son baratos y no queremos perder un match
    seguro).
    """
    enabled = [p for p in products if p.enabled]
    if len(enabled) < 2:
        return []

    # Mantenemos la convención previa: de un par, el id "menor" (orden
    # lexicográfico de strings) es el que se conserva; el "mayor" se marca.
    def _keep_drop(a: VendureProduct, b: VendureProduct) -> tuple[VendureProduct, VendureProduct]:
        return (a, b) if a.id <= b.id else (b, a)

    best_by_drop: dict[str, DuplicatePair] = {}

    def _offer(keep: VendureProduct, drop: VendureProduct, verdict: DedupVerdict) -> None:
        verdict.candidate_id = keep.id
        prev = best_by_drop.get(drop.id)
        if prev is None or verdict.confidence > prev.verdict.confidence:
            best_by_drop[drop.id] = DuplicatePair(drop=drop, keep=keep, verdict=verdict)

    # 1) Buckets de URL exacta — match seguro sin tocar texto/imagen.
    by_url: dict[str, list[VendureProduct]] = defaultdict(list)
    for p in enabled:
        nu = normalize_url(p.source_url)
        if nu:
            by_url[nu].append(p)
    url_dup_ids: set[str] = set()
    for group in by_url.values():
        if len(group) < 2:
            continue
        canonical = min(group, key=lambda p: p.id)
        for p in group:
            if p.id == canonical.id:
                continue
            _offer(
                canonical, p,
                DedupVerdict(
                    is_duplicate=True, confidence=1.0, matched_by=["url"],
                    per_strategy_scores={"url": 1.0, "image": 0.0, "text": 0.0},
                ),
            )
            url_dup_ids.add(p.id)

    # 2) Pares candidatos por texto → compare() (con gate de imagen).
    pairs = _candidate_pairs(enabled)
    if changed_ids is not None:
        # Incremental: quedarse solo con pares donde al menos un producto cambió.
        pairs = {
            (i, j) for (i, j) in pairs
            if enabled[i].id in changed_ids or enabled[j].id in changed_ids
        }

    async def _score(i: int, j: int) -> None:
        a, b = enabled[i], enabled[j]
        # Si el drop ya quedó fijado por URL exacta (confianza 1.0), no hay nada
        # que mejorar.
        keep, drop = _keep_drop(a, b)
        if drop.id in url_dup_ids:
            return
        verdict = await compare(_from_vendure(a), _from_vendure(b))
        if verdict.is_duplicate:
            _offer(keep, drop, verdict)

    # Concurrencia acotada (las comparaciones con imagen tocan red).
    sem = asyncio.Semaphore(10)

    async def _guarded(i: int, j: int) -> None:
        async with sem:
            await _score(i, j)

    await asyncio.gather(*(_guarded(i, j) for (i, j) in pairs))

    return list(best_by_drop.values())
