"""find_duplicate_in solo devuelve duplicado si algún veredicto pasó su umbral.

Antes se quedaba con la confianza más alta aunque `is_duplicate` fuera False:
un producto parecido-pero-no-tanto (imagen 0.91 con umbral 0.92) le ganaba a
uno que SÍ matcheaba (texto 0.89 con umbral 0.88), y /verify respondía
"no es duplicado" pero con candidate_id puesto.
"""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app.dedup import orchestrator  # noqa: E402
from app.dedup.orchestrator import CandidateInput, DedupVerdict, find_duplicate_in  # noqa: E402
from app.vendure.client import VendureProduct  # noqa: E402

CANDIDATE = CandidateInput(name="x", description="", source_url=None, image_urls=[])


def _prod(pid: str) -> VendureProduct:
    return VendureProduct(
        id=pid, name=f"p{pid}", slug=f"p{pid}", description="", enabled=True,
        source_url=None, image_urls=[], product_code=None, featured_image_url=None,
        first_variant_price_cents=None, variant_count=0,
    )


def _scripted_compare(monkeypatch, verdicts_by_id: dict[str, DedupVerdict]):
    """compare() devuelve el veredicto preparado para cada producto (por nombre)."""
    async def fake_compare(a, b):  # noqa: ARG001
        return verdicts_by_id[b.name.removeprefix("p")]

    monkeypatch.setattr(orchestrator, "compare", fake_compare)


def _v(conf: float, dup: bool, by: list[str] | None = None) -> DedupVerdict:
    return DedupVerdict(is_duplicate=dup, confidence=conf, matched_by=by or [],
                        per_strategy_scores={"url": 0.0, "image": conf, "text": 0.0})


@pytest.mark.asyncio
async def test_a_real_match_beats_a_higher_score_that_did_not_pass(monkeypatch):
    _scripted_compare(monkeypatch, {
        "A": _v(0.91, dup=False),            # casi, pero bajo el umbral
        "B": _v(0.89, dup=True, by=["text"]),  # pasó su umbral
    })
    verdict = await find_duplicate_in(CANDIDATE, [_prod("A"), _prod("B")])
    assert verdict.is_duplicate is True
    assert verdict.candidate_id == "B"
    assert verdict.confidence == 0.89
    assert verdict.matched_by == ["text"]


@pytest.mark.asyncio
async def test_nobody_passed_means_no_duplicate_and_no_candidate(monkeypatch):
    _scripted_compare(monkeypatch, {"A": _v(0.80, dup=False), "B": _v(0.91, dup=False)})
    verdict = await find_duplicate_in(CANDIDATE, [_prod("A"), _prod("B")])
    assert verdict.is_duplicate is False
    assert verdict.candidate_id is None, "sin duplicado no hay candidato"
    assert verdict.matched_by == []
    # La mejor confianza se informa igual, y se sabe de quién era.
    assert verdict.confidence == 0.91
    assert verdict.closest_id == "B"


@pytest.mark.asyncio
async def test_best_duplicate_wins_among_several(monkeypatch):
    _scripted_compare(monkeypatch, {
        "A": _v(0.89, dup=True, by=["text"]),
        "B": _v(0.95, dup=True, by=["image"]),
        "C": _v(0.99, dup=False),
    })
    verdict = await find_duplicate_in(CANDIDATE, [_prod("A"), _prod("B"), _prod("C")])
    assert verdict.is_duplicate is True
    assert verdict.candidate_id == "B"
    assert verdict.confidence == 0.95


@pytest.mark.asyncio
async def test_url_match_short_circuits(monkeypatch):
    seen: list[str] = []

    async def fake_compare(a, b):  # noqa: ARG001
        seen.append(b.name)
        return _v(1.0, dup=True, by=["url"]) if b.name == "pA" else _v(0.5, dup=False)

    monkeypatch.setattr(orchestrator, "compare", fake_compare)
    verdict = await find_duplicate_in(CANDIDATE, [_prod("A"), _prod("B")])
    assert verdict.candidate_id == "A"
    assert seen == ["pA"], "con match por URL no se sigue comparando"


@pytest.mark.asyncio
async def test_empty_catalog():
    verdict = await find_duplicate_in(CANDIDATE, [])
    assert verdict.is_duplicate is False
    assert verdict.candidate_id is None
    assert verdict.confidence == 0.0
