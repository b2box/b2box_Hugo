"""Tests para la lógica de deduplicación.

Cubren las 3 estrategias y el orquestador. No tocan red ni Vendure.
"""

from __future__ import annotations

import os

# Mínimo necesario para que pydantic-settings no explote sin .env real
os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")
os.environ.setdefault("VENDURE_ADMIN_TOKEN", "test-token")

import pytest  # noqa: E402

from app.dedup.fuzzy_text import text_similarity  # noqa: E402
from app.dedup.orchestrator import CandidateInput, compare  # noqa: E402
from app.dedup.url_match import normalize_url, url_similarity  # noqa: E402


# ── url_match ────────────────────────────────────────────────────


def test_normalize_url_drops_tracking_params():
    a = "https://www.aliexpress.com/item/12345.html?utm_source=ig&spm=abc"
    b = "https://www.aliexpress.com/item/12345.html"
    assert normalize_url(a) == normalize_url(b)


def test_normalize_url_strips_trailing_slash():
    assert normalize_url("https://x.com/a/") == normalize_url("https://x.com/a")


def test_url_similarity_exact_match():
    assert url_similarity(
        "https://www.aliexpress.com/item/1.html?utm_source=x",
        "https://www.aliexpress.com/item/1.html",
    ) == 1.0


def test_url_similarity_diff_returns_zero():
    assert url_similarity(
        "https://aliexpress.com/item/1.html",
        "https://aliexpress.com/item/2.html",
    ) == 0.0


def test_url_similarity_handles_none():
    assert url_similarity(None, "https://x.com") == 0.0
    assert url_similarity("https://x.com", None) == 0.0


# ── fuzzy_text ───────────────────────────────────────────────────


def test_text_similarity_identical_titles():
    score = text_similarity("Mochila escolar azul", "", "Mochila escolar azul", "")
    assert score >= 0.99


def test_text_similarity_reordered_words():
    # token_set_ratio tolera reordenamiento
    score = text_similarity("Mochila escolar azul", "", "Azul mochila escolar", "")
    assert score >= 0.95


def test_text_similarity_unrelated_titles():
    score = text_similarity("Mochila escolar", "", "Auriculares bluetooth", "")
    assert score < 0.5


def test_text_similarity_html_is_stripped_from_description():
    a = text_similarity(
        "Mochila X", "<p>Excelente <b>calidad</b> y diseño moderno.</p>",
        "Mochila X", "Excelente calidad y diseño moderno.",
    )
    assert a >= 0.95


# ── orchestrator ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compare_url_match_shortcircuits():
    """Si las URLs coinciden, devolvemos duplicado sin tocar imágenes."""
    a = CandidateInput(
        name="Mochila A",
        description="x",
        source_url="https://www.aliexpress.com/item/9.html",
        image_urls=[],  # vacío a propósito → confirma que no se llamó al network
    )
    b = CandidateInput(
        name="Distinta totalmente",
        description="y",
        source_url="https://www.aliexpress.com/item/9.html?spm=tracking",
        image_urls=[],
    )
    verdict = await compare(a, b)
    assert verdict.is_duplicate is True
    assert verdict.matched_by == ["url"]
    assert verdict.confidence == 1.0


@pytest.mark.asyncio
async def test_compare_no_signals_returns_no_duplicate():
    a = CandidateInput(name="Mochila escolar", description="", source_url=None, image_urls=[])
    b = CandidateInput(name="Auriculares bluetooth", description="", source_url=None, image_urls=[])
    verdict = await compare(a, b)
    assert verdict.is_duplicate is False
    assert verdict.confidence < 0.5


@pytest.mark.asyncio
async def test_compare_text_only_match():
    """Sin URL ni imagen, pero textos casi idénticos → match por texto."""
    a = CandidateInput(
        name="Mochila escolar impermeable azul",
        description="Mochila ideal para colegio con compartimento para notebook.",
        source_url=None,
        image_urls=[],
    )
    b = CandidateInput(
        name="Mochila escolar azul impermeable",
        description="Mochila ideal para colegio con compartimento para laptop.",
        source_url=None,
        image_urls=[],
    )
    verdict = await compare(a, b)
    # No exigimos is_duplicate=True (depende del threshold), pero sí alto score
    assert verdict.per_strategy_scores["text"] >= 0.85
