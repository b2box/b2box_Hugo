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
from dataclasses import dataclass, field
from typing import Literal

from app import runtime
from app.dedup.fuzzy_text import text_similarity
from app.dedup.image_hash import image_similarity
from app.dedup.url_match import url_similarity
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
