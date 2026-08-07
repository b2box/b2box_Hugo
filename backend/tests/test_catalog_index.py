"""Índice vectorial del catálogo: centrado, proyección del query y búsqueda.

El centrado es lo que evita el falso positivo que motivó todo esto: sin él, el
máximo contra un catálogo grande siempre supera el umbral y /app/lookup propone
cualquier producto.
"""

import numpy as np
import pytest

from app.dedup import catalog_index, image_embed
from app.vendure.client import VendureProduct


@pytest.fixture(autouse=True)
def _clean_state():
    """Cada test arranca con el índice vacío y lo deja vacío."""
    catalog_index._state.matrix = np.zeros((0, image_embed.EMBED_DIM), dtype=np.float32)
    catalog_index._state.mean = np.zeros(image_embed.EMBED_DIM, dtype=np.float32)
    catalog_index._state.product_ids = []
    catalog_index._state.image_urls = []
    catalog_index._state.products = {}
    yield
    catalog_index._state.matrix = np.zeros((0, image_embed.EMBED_DIM), dtype=np.float32)
    catalog_index._state.mean = np.zeros(image_embed.EMBED_DIM, dtype=np.float32)
    catalog_index._state.products = {}


def _product(pid: str) -> VendureProduct:
    return VendureProduct(
        id=pid,
        name=f"Producto {pid}",
        slug=f"p-{pid}",
        description="",
        enabled=True,
        source_url="",
        image_urls=[f"http://img/{pid}.jpg"],
        product_code=f"BX-{pid}",
        featured_image_url=f"http://img/{pid}.jpg",
        first_variant_price_cents=1000,
        variant_count=1,
    )


def _unit(rows: np.ndarray) -> np.ndarray:
    return (rows / np.linalg.norm(rows, axis=1, keepdims=True)).astype(np.float32)


def _fake_index(raw: np.ndarray) -> None:
    """Carga `raw` (N×512, sin normalizar) como si viniera de un build."""
    rows = _unit(raw)
    catalog_index._state.matrix, catalog_index._state.mean = catalog_index._center(rows)
    catalog_index._state.product_ids = [str(i) for i in range(rows.shape[0])]
    catalog_index._state.image_urls = [f"http://img/{i}.jpg" for i in range(rows.shape[0])]
    catalog_index._state.products = {str(i): _product(str(i)) for i in range(rows.shape[0])}


def _biased_catalog(n: int, seed: int = 0) -> np.ndarray:
    """N vectores con una dirección común fuerte — el 'fondo blanco' de CLIP."""
    rng = np.random.default_rng(seed)
    common = np.zeros(image_embed.EMBED_DIM, dtype=np.float32)
    common[0] = 1.0
    noise = rng.normal(size=(n, image_embed.EMBED_DIM)).astype(np.float32)
    return common * 3.0 + _unit(noise)


# ─── Centrado ──────────────────────────────────────────────────────


def test_center_is_skipped_with_too_few_vectors():
    raw = _unit(np.random.default_rng(1).normal(size=(3, image_embed.EMBED_DIM)))
    matrix, mean = catalog_index._center(raw)
    assert not mean.any()
    np.testing.assert_allclose(matrix, raw)


def test_center_removes_the_common_direction():
    raw = _unit(_biased_catalog(60))
    matrix, mean = catalog_index._center(raw)
    assert mean.any()
    # Filas normalizadas y con media ~0: la dirección común se fue.
    np.testing.assert_allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5)
    assert abs(float(matrix.mean(axis=0)[0])) < 0.05


def test_center_disabled_by_setting(monkeypatch):
    monkeypatch.setattr(
        catalog_index.get_settings(), "embed_center_index", False, raising=False
    )
    raw = _unit(_biased_catalog(60))
    matrix, mean = catalog_index._center(raw)
    assert not mean.any()
    np.testing.assert_allclose(matrix, raw)


def test_centering_lowers_the_score_of_unrelated_products():
    """El punto de todo el fix: el impostor deja de puntuar como match."""
    raw = _unit(_biased_catalog(80, seed=3))
    query = _unit(_biased_catalog(1, seed=99))[0]  # nada que ver con el catálogo

    crudo = float((raw @ query).max())
    matrix, mean = catalog_index._center(raw)
    centrado_q = (query - mean) / np.linalg.norm(query - mean)
    centrado = float((matrix @ centrado_q).max())

    assert crudo > 0.85, "sin centrar, el impostor pasa el umbral viejo"
    assert centrado < crudo - 0.3, "centrar tiene que hundir al impostor"


# ─── project() ─────────────────────────────────────────────────────


def test_project_is_identity_without_centering():
    vec = _unit(np.random.default_rng(2).normal(size=(1, image_embed.EMBED_DIM)))[0]
    assert catalog_index.project(vec) is vec


def test_project_centers_and_renormalizes():
    _fake_index(_biased_catalog(40))
    vec = _unit(_biased_catalog(1, seed=11))[0]
    out = catalog_index.project(vec)
    assert out is not None
    assert abs(float(np.linalg.norm(out)) - 1.0) < 1e-5


def test_project_handles_query_equal_to_the_mean():
    """Query == media del índice: score 0, no división por cero."""
    _fake_index(_biased_catalog(40))
    out = catalog_index.project(catalog_index._state.mean.copy())
    assert out is not None
    assert float(np.linalg.norm(out)) == pytest.approx(0.0, abs=1e-6)


def test_project_passes_through_none():
    assert catalog_index.project(None) is None


# ─── search / score_products ───────────────────────────────────────


def test_search_finds_the_exact_row_with_centering_on():
    raw = _biased_catalog(40, seed=5)
    _fake_index(raw)
    assert catalog_index.is_centered()
    query = _unit(raw)[7]
    hits = catalog_index.search(query, top_k=3)
    assert hits[0][0].id == "7"
    assert hits[0][2] == "http://img/7.jpg"


def test_score_products_uses_the_same_space_as_search():
    raw = _biased_catalog(40, seed=6)
    _fake_index(raw)
    query = _unit(raw)[12]
    from_search = {p.id: s for p, s, _ in catalog_index.search(query, top_k=40)}
    for product, score, _url in catalog_index.score_products(query, ["12", "3"]):
        assert score == pytest.approx(from_search[product.id], abs=1e-6)


def test_search_returns_empty_when_index_not_ready():
    vec = _unit(np.random.default_rng(4).normal(size=(1, image_embed.EMBED_DIM)))[0]
    assert catalog_index.search(vec) == []
    assert catalog_index.score_products(vec, ["1"]) == []


def test_status_reports_centering():
    _fake_index(_biased_catalog(40))
    assert catalog_index.status()["centered"] is True


# ─── Umbrales de la escala vieja ───────────────────────────────────


def test_effective_threshold_keeps_values_in_the_centered_scale():
    _fake_index(_biased_catalog(40))
    assert catalog_index.effective_threshold("embed_match_threshold", 0.65) == 0.65


def test_effective_threshold_discards_stale_uncentered_overrides():
    """Un 0.88 guardado de antes dejaría al app sin encontrar nada."""
    _fake_index(_biased_catalog(40))
    default = catalog_index.get_settings().embed_match_threshold
    assert catalog_index.effective_threshold("embed_match_threshold", 0.88) == default


def test_effective_threshold_respects_overrides_without_centering(monkeypatch):
    """Sin centrado la escala vieja es la correcta: no hay nada que descartar."""
    monkeypatch.setattr(
        catalog_index.get_settings(), "embed_center_index", False, raising=False
    )
    _fake_index(_biased_catalog(40))
    assert not catalog_index.is_centered()
    assert catalog_index.effective_threshold("embed_match_threshold", 0.88) == 0.88
