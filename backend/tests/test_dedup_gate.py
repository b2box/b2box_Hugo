"""El gate de imagen debe evitar descargar/comparar imágenes cuando el texto
es claramente distinto (ahorro de costo O(N²))."""

import pytest

from app.dedup import orchestrator
from app.dedup.orchestrator import CandidateInput, compare


@pytest.mark.asyncio
async def test_image_skipped_when_text_below_gate(monkeypatch):
    calls = {"n": 0}

    async def fake_image_similarity(a, b):
        calls["n"] += 1
        return 1.0  # si se llamara, marcaría duplicado por imagen

    # gate alto: solo compara imagen si texto >= 0.9
    monkeypatch.setattr(orchestrator, "image_similarity", fake_image_similarity)
    monkeypatch.setattr(
        orchestrator.runtime, "get",
        lambda k: {"dedup_url_threshold": 1.0, "dedup_image_threshold": 0.92,
                   "dedup_text_threshold": 0.88, "dedup_image_text_gate": 0.9}[k],
    )

    a = CandidateInput(name="Auriculares bluetooth", description="", source_url=None,
                       image_urls=["http://img/a.jpg"])
    b = CandidateInput(name="Licuadora industrial 3L", description="", source_url=None,
                       image_urls=["http://img/b.jpg"])
    verdict = await compare(a, b)

    assert calls["n"] == 0, "no debió descargar imágenes (texto bajo el gate)"
    assert verdict.per_strategy_scores["image"] == 0.0
    assert verdict.is_duplicate is False


@pytest.mark.asyncio
async def test_image_used_when_text_above_gate(monkeypatch):
    calls = {"n": 0}

    async def fake_image_similarity(a, b):
        calls["n"] += 1
        return 0.99

    monkeypatch.setattr(orchestrator, "image_similarity", fake_image_similarity)
    monkeypatch.setattr(
        orchestrator.runtime, "get",
        lambda k: {"dedup_url_threshold": 1.0, "dedup_image_threshold": 0.92,
                   "dedup_text_threshold": 0.88, "dedup_image_text_gate": 0.3}[k],
    )

    a = CandidateInput(name="Auriculares bluetooth negros", description="", source_url=None,
                       image_urls=["http://img/a.jpg"])
    b = CandidateInput(name="Auriculares bluetooth negro", description="", source_url=None,
                       image_urls=["http://img/b.jpg"])
    verdict = await compare(a, b)

    assert calls["n"] == 1, "debió comparar imágenes (texto sobre el gate)"
    assert verdict.is_duplicate is True
