"""Embeddings CLIP: serialización, preprocesado y búsqueda del mejor match."""

from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from app.dedup import image_embed


def _png_bytes(size=(640, 480), color=(200, 30, 40)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def test_vector_roundtrip():
    vec = np.random.rand(image_embed.EMBED_DIM).astype(np.float32)
    restored = image_embed.decode_vector(image_embed.encode_vector(vec))
    assert restored is not None
    assert np.allclose(vec, restored, atol=1e-6)


def test_decode_rejects_wrong_dimension():
    short = image_embed.encode_vector(np.zeros(16, dtype=np.float32))
    assert image_embed.decode_vector(short) is None


def test_decode_rejects_garbage():
    assert image_embed.decode_vector("no-es-base64-valido!!") is None


def test_preprocess_shape_and_normalization():
    tensor = image_embed._preprocess(_png_bytes())

    assert tensor.shape == (1, 3, 224, 224)
    assert tensor.dtype == np.float32
    # Con la normalización de CLIP los valores salen del rango [0,1] original.
    assert tensor.min() < 0.0


def test_preprocess_upscales_small_images():
    # Una foto chica del cliente no debe romper el center crop.
    tensor = image_embed._preprocess(_png_bytes(size=(80, 50)))
    assert tensor.shape == (1, 3, 224, 224)


def test_best_match_picks_closest_row():
    query = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    matrix = np.array(
        [
            [0.0, 1.0, 0.0],   # ortogonal
            [0.9, 0.1, 0.0],   # parecido
            [-1.0, 0.0, 0.0],  # opuesto
        ],
        dtype=np.float32,
    )
    idx, score = image_embed.best_match(query, matrix)
    assert idx == 1
    assert score == pytest.approx(0.9, abs=1e-6)


def test_best_match_on_empty_matrix():
    empty = np.zeros((0, 3), dtype=np.float32)
    assert image_embed.best_match(np.ones(3, dtype=np.float32), empty) == (-1, 0.0)


def test_cosine_of_identical_vectors_is_one():
    vec = image_embed._normalize(np.array([3.0, 4.0], dtype=np.float32))
    assert image_embed.cosine(vec, vec) == pytest.approx(1.0, abs=1e-6)


def test_normalize_handles_zero_vector():
    zero = image_embed._normalize(np.zeros(4, dtype=np.float32))
    assert not np.isnan(zero).any()


def test_available_is_false_when_disabled(monkeypatch):
    class FakeSettings:
        embed_enabled = False

    monkeypatch.setattr(image_embed, "get_settings", lambda: FakeSettings())
    assert image_embed.available() is False


@pytest.mark.asyncio
async def test_embed_url_returns_none_when_unavailable(monkeypatch):
    monkeypatch.setattr(image_embed, "available", lambda: False)
    assert await image_embed.embed_url("https://cdn.example.com/x.jpg") is None
