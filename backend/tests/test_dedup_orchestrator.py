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


# ─── find_duplicate_in: el link primero, contra todos ───────────────────────


def _vp(pid: str, name: str, url: str | None, enabled: bool = True):
    from app.vendure.client import VendureProduct

    return VendureProduct(id=pid, name=name, slug=f"p{pid}", description="", enabled=enabled, source_url=url,
                          image_urls=[], product_code=None, featured_image_url=None, first_variant_price_cents=100,
                          variant_count=1)


@pytest.mark.asyncio
async def test_un_link_repetido_se_contesta_sin_comparar_texto_ni_fotos(monkeypatch):
    """Antes el link repetido recién aparecía después de comparar texto y fotos contra todos los productos anteriores."""
    from app.dedup import orchestrator

    compared: list[str] = []
    real = orchestrator.compare

    async def spy(a, b):
        compared.append(b.name)
        return await real(a, b)

    monkeypatch.setattr(orchestrator, "compare", spy)
    link = "https://detail.1688.com/offer/5551234.html"
    catalog = [_vp(str(i), f"Producto distinto {i}", f"https://detail.1688.com/offer/{1000 + i}.html") for i in range(50)]
    catalog.append(_vp("777", "Otro nombre cualquiera", link + "?spm=abc", enabled=False))
    out = await orchestrator.find_duplicate_in(
        CandidateInput(name="Organizador de cocina", description="", source_url=link, image_urls=[]), catalog)
    assert out.is_duplicate is True and out.candidate_id == "777" and out.matched_by == ["url"]
    assert out.confidence == 1.0 and out.per_strategy_scores == {"url": 1.0, "image": 0.0, "text": 0.0}
    assert compared == [], "con el link alcanza: no se compara nada más"


@pytest.mark.asyncio
async def test_el_link_gana_aunque_un_producto_anterior_matchee_por_texto():
    from app.dedup import orchestrator

    link = "https://detail.1688.com/offer/5559999.html"
    catalog = [_vp("1", "Organizador de cocina", "https://detail.1688.com/offer/1.html"),
               _vp("2", "Cualquier otra cosa", link)]
    out = await orchestrator.find_duplicate_in(
        CandidateInput(name="Organizador de cocina", description="", source_url=link, image_urls=[]), catalog)
    assert out.candidate_id == "2" and out.matched_by == ["url"]
    # Sin link (o con uno que nadie tiene) sigue el camino de siempre: texto y fotos.
    out = await orchestrator.find_duplicate_in(
        CandidateInput(name="Organizador de cocina", description="", source_url=None, image_urls=[]), catalog)
    assert out.is_duplicate is True and out.candidate_id == "1" and out.matched_by == ["text"]
