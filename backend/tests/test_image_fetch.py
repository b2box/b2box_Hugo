"""Descarga de imágenes: cabeceras de cliente normal + reintentos.

Indexar el catálogo son miles de descargas seguidas contra el mismo CDN. Sin
reintento, cada corte intermitente es un producto que queda fuera del índice
para siempre; y sin cabeceras de cliente normal, varios CDN de marketplace
(mlstatic) cortan directamente.
"""

import httpx
import pytest

from app.dedup import image_hash


def _resp(status: int, content: bytes = b"x", url: str = "https://cdn/x.jpg") -> httpx.Response:
    req = httpx.Request("GET", url)
    return httpx.Response(status_code=status, content=content, request=req)


def _patch(monkeypatch, responses):
    """safe_get que va devolviendo `responses` en orden; registra las cabeceras."""
    seen = {"calls": 0, "headers": None, "timeouts": []}
    seq = list(responses)

    async def fake_safe_get(url, *, timeout, headers=None, **kw):  # noqa: ARG001
        seen["calls"] += 1
        seen["headers"] = headers
        seen["timeouts"].append(timeout)
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(image_hash, "safe_get", fake_safe_get)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(image_hash.asyncio, "sleep", no_sleep)
    return seen


@pytest.mark.asyncio
async def test_manda_cabeceras_de_cliente_normal(monkeypatch):
    seen = _patch(monkeypatch, [_resp(200, b"datos")])

    assert await image_hash._fetch("https://cdn/x.jpg") == b"datos"
    assert "Mozilla/5.0" in seen["headers"]["User-Agent"]
    assert "image/" in seen["headers"]["Accept"]


@pytest.mark.asyncio
async def test_reintenta_ante_429_y_termina_bien(monkeypatch):
    seen = _patch(monkeypatch, [_resp(429), _resp(200, b"ok")])

    assert await image_hash._fetch("https://cdn/x.jpg") == b"ok"
    assert seen["calls"] == 2


@pytest.mark.asyncio
async def test_reintenta_ante_corte_de_red(monkeypatch):
    seen = _patch(monkeypatch, [httpx.ConnectError("boom"), _resp(200, b"ok")])

    assert await image_hash._fetch("https://cdn/x.jpg") == b"ok"
    assert seen["calls"] == 2


@pytest.mark.asyncio
async def test_se_rinde_tras_los_intentos_configurados(monkeypatch):
    seen = _patch(monkeypatch, [_resp(503), _resp(503), _resp(503)])

    with pytest.raises(httpx.HTTPStatusError):
        await image_hash._fetch("https://cdn/x.jpg")
    assert seen["calls"] == image_hash._FETCH_ATTEMPTS


@pytest.mark.asyncio
async def test_404_no_se_reintenta(monkeypatch):
    """Un 404 no mejora reintentando: solo gastaría red y tiempo."""
    seen = _patch(monkeypatch, [_resp(404)])

    with pytest.raises(httpx.HTTPStatusError):
        await image_hash._fetch("https://cdn/x.jpg")
    assert seen["calls"] == 1


@pytest.mark.asyncio
async def test_imagen_gigante_se_rechaza_sin_reintentar(monkeypatch):
    grande = b"x" * (image_hash._MAX_IMAGE_BYTES + 1)
    seen = _patch(monkeypatch, [_resp(200, grande)])

    with pytest.raises(ValueError):
        await image_hash._fetch("https://cdn/x.jpg")
    assert seen["calls"] == 1
