"""Vecinos de un producto en el índice CLIP (los usa el diagnóstico de la bandeja): en memoria, sin red."""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from app.dedup import catalog_index, image_embed  # noqa: E402


def _unit(*xs: float) -> np.ndarray:
    v = np.zeros(image_embed.EMBED_DIM, dtype=np.float32)
    v[: len(xs)] = xs
    return v / np.linalg.norm(v)


@pytest.fixture
def index(monkeypatch):
    # a y b casi iguales; c algo parecido; d ortogonal; e sin vector en la matriz
    rows = [("a", _unit(1, 0.0)), ("b", _unit(1, 0.1)), ("c", _unit(1, 1)), ("d", _unit(0, 0, 1)), ("a", _unit(1, 0.05))]
    st = catalog_index._state
    monkeypatch.setattr(st, "matrix", np.stack([r[1] for r in rows]).astype(np.float32))
    monkeypatch.setattr(st, "product_ids", [r[0] for r in rows])
    monkeypatch.setattr(st, "vector_ids", frozenset({"a", "b", "c", "d"}))
    return st


def test_neighbors_come_ordered_and_without_the_product_itself(index):
    got = catalog_index.neighbors_of("a", top_k=10)
    assert [pid for pid, _ in got] == ["b", "c", "d"]
    assert got[0][1] > got[1][1] > got[2][1]
    assert got[0][1] == pytest.approx(1.0, abs=0.01) and got[2][1] == pytest.approx(0.0, abs=1e-6)


def test_top_k_cuts_the_list(index):
    assert [pid for pid, _ in catalog_index.neighbors_of("a", top_k=2)] == ["b", "c"]
    assert catalog_index.neighbors_of("a", top_k=0) == []


def test_a_product_with_two_photos_is_one_neighbor(index):
    assert [pid for pid, _ in catalog_index.neighbors_of("b", top_k=10)].count("a") == 1


def test_unknown_product_or_not_ready_index_gives_nothing(index, monkeypatch):
    assert catalog_index.neighbors_of("zzz") == []
    monkeypatch.setattr(catalog_index._state, "matrix", np.zeros((0, image_embed.EMBED_DIM), dtype=np.float32))
    assert catalog_index.neighbors_of("a") == []
