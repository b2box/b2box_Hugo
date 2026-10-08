"""Productos deshabilitados en el semáforo: no están en el índice CLIP del app
(a propósito), así que sus fotos se embeben al vuelo y se comparan en el mismo
espacio centrado. Con numpy chico y dobles de image_embed / catalog_index."""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from app.dedup import catalog_index, image_embed  # noqa: E402
from app.pricing import market_match  # noqa: E402
from app.vendure.client import VendureProduct  # noqa: E402

ML_PHOTO = "https://http2.mlstatic.com/D_NQ_NP_1-F.jpg"


def _product(pid="9", enabled=False, photos=("https://cdn.b2box/9.jpg",)) -> VendureProduct:
    return VendureProduct(
        id=pid, name="Taza", slug="taza", description="", enabled=enabled, source_url=None,
        image_urls=list(photos), product_code="BX9", featured_image_url=photos[0] if photos else None,
        first_variant_price_cents=100, variant_count=1)


def _unit(*v) -> np.ndarray:
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


@pytest.fixture
def clip(monkeypatch):
    """CLIP "prendido", índice listo con un solo producto habilitado (id "1")."""
    vectors = {ML_PHOTO: _unit(1, 0, 0), "https://cdn.b2box/9.jpg": _unit(1, 1, 0),
               "https://cdn.b2box/9b.jpg": _unit(0, 0, 1)}
    asked: list[list[str]] = []

    async def embed(urls, *, concurrency=4, interactive=False):  # noqa: ARG001
        asked.append(list(urls))
        return [vectors.get(u) for u in urls]

    monkeypatch.setattr(image_embed, "available", lambda: True)
    monkeypatch.setattr(image_embed, "embed_urls_aligned", embed)
    monkeypatch.setattr(catalog_index, "is_ready", lambda: True)
    monkeypatch.setattr(catalog_index, "has_product", lambda pid: pid == "1")
    monkeypatch.setattr(catalog_index, "project", lambda v: v)        # sin centrar: identidad
    monkeypatch.setattr(catalog_index, "score_products",
                        lambda vec, ids: [(None, 0.5, "x")] if "1" in list(ids) else [])
    return vectors, asked


def test_a_disabled_product_can_be_scored_even_outside_the_index(clip):
    assert market_match.indexed(_product(enabled=False)) is True
    assert market_match.indexed(_product("1", enabled=True)) is True          # está en el índice


def test_an_enabled_product_outside_the_index_is_still_skipped(clip):
    assert market_match.indexed(_product("7", enabled=True)) is False


def test_a_disabled_product_without_photos_cannot_be_scored(clip):
    assert market_match.indexed(_product(enabled=False, photos=())) is False


async def test_disabled_score_is_the_cosine_against_its_own_photos(clip):
    score = await market_match.clip_index_scorer(_product(enabled=False), [ML_PHOTO])
    assert score == pytest.approx(np.dot(_unit(1, 0, 0), _unit(1, 1, 0)))     # ≈ 0.707


async def test_disabled_score_takes_the_best_of_its_photos(clip):
    prod = _product(enabled=False, photos=("https://cdn.b2box/9b.jpg", "https://cdn.b2box/9.jpg"))
    score = await market_match.clip_index_scorer(prod, [ML_PHOTO])
    assert score == pytest.approx(0.7071, abs=1e-3)


async def test_disabled_score_is_none_if_none_of_its_photos_embed(clip):
    prod = _product(enabled=False, photos=("https://cdn.b2box/rota.jpg",))
    assert await market_match.clip_index_scorer(prod, [ML_PHOTO]) is None


async def test_enabled_products_keep_using_the_index(clip):
    _, asked = clip
    score = await market_match.clip_index_scorer(_product("1", enabled=True), [ML_PHOTO])
    assert score == 0.5
    assert asked == [[ML_PHOTO]]                  # no embebió fotos propias al vuelo
