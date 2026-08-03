"""Extracción de la foto del producto a partir de una URL del b2box app.

Cubre los tres orígenes reales: publicación de MercadoLibre, ficha de Alibaba/1688
y foto propia del cliente (link directo a la imagen).
"""

import httpx
import pytest

from app.ingest import image_from_url
from app.ingest.image_from_url import ExtractError, detect_marketplace, extract


def _response(url: str, *, body: bytes = b"", content_type: str = "text/html",
              status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers={"content-type": content_type},
        content=body,
        request=httpx.Request("GET", url),
    )


def _patch_get(monkeypatch, response: httpx.Response):
    async def fake_safe_get(url, **kwargs):  # noqa: ARG001
        return response

    monkeypatch.setattr(image_from_url, "safe_get", fake_safe_get)


# ─── Detección de marketplace ──────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://articulo.mercadolibre.com.ar/MLA-123-auricular", "mercadolibre"),
        ("https://spanish.alibaba.com/product-detail/x_123.html", "alibaba"),
        ("https://detail.1688.com/offer/987654.html", "1688"),
        ("https://es.aliexpress.com/item/100500.html", "aliexpress"),
        ("https://mitienda.example.com/p/1", "other"),
    ],
)
def test_detect_marketplace(url, expected):
    assert detect_marketplace(url) == expected


# ─── Caso 1: foto del cliente (link directo) ───────────────────────


@pytest.mark.asyncio
async def test_direct_image_url(monkeypatch):
    url = "https://cdn.example.com/uploads/foto-cliente.jpg"
    _patch_get(monkeypatch, _response(url, body=b"\xff\xd8\xff", content_type="image/jpeg"))

    result = await extract(url)

    assert result.kind == "image"
    assert result.image_urls == [url]
    assert result.marketplace == "upload"


# ─── Caso 2: MercadoLibre (og:image) ───────────────────────────────


@pytest.mark.asyncio
async def test_mercadolibre_og_image(monkeypatch):
    url = "https://articulo.mercadolibre.com.ar/MLA-123-auricular-bluetooth"
    html = b"""
    <html><head>
      <meta property="og:title" content="Auricular Bluetooth TWS">
      <meta property="og:image" content="https://http2.mlstatic.com/D_NQ_NP_2X_998.jpg">
      <title>ignorado</title>
    </head><body></body></html>
    """
    _patch_get(monkeypatch, _response(url, body=html))

    result = await extract(url)

    assert result.marketplace == "mercadolibre"
    assert result.kind == "page"
    assert result.image_urls == ["https://http2.mlstatic.com/D_NQ_NP_2X_998.jpg"]
    assert result.title == "Auricular Bluetooth TWS"


# ─── Caso 3: Alibaba con JSON-LD ───────────────────────────────────


@pytest.mark.asyncio
async def test_alibaba_json_ld_wins_and_is_absolutized(monkeypatch):
    url = "https://spanish.alibaba.com/product-detail/lampara_123.html"
    html = b"""
    <html><head>
      <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"Product","name":"Lampara LED",
       "image":["//s.alicdn.com/imgs/lampara-1.jpg","https://s.alicdn.com/imgs/lampara-2.jpg"]}
      </script>
      <meta property="og:image" content="https://s.alicdn.com/imgs/og.jpg">
    </head><body></body></html>
    """
    _patch_get(monkeypatch, _response(url, body=html))

    result = await extract(url)

    # El JSON-LD manda: es lo que el sitio declara como foto del producto.
    assert result.image_urls[0] == "https://s.alicdn.com/imgs/lampara-1.jpg"
    assert "https://s.alicdn.com/imgs/og.jpg" in result.image_urls
    assert result.title == "Lampara LED"


@pytest.mark.asyncio
async def test_json_ld_inside_graph(monkeypatch):
    url = "https://tienda.example.com/p/1"
    html = b"""
    <html><head><script type="application/ld+json">
    {"@graph":[{"@type":"BreadcrumbList"},
               {"@type":"Product","name":"Silla","image":"https://cdn.example.com/silla.jpg"}]}
    </script></head><body></body></html>
    """
    _patch_get(monkeypatch, _response(url, body=html))

    result = await extract(url)
    assert result.image_urls == ["https://cdn.example.com/silla.jpg"]
    assert result.title == "Silla"


# ─── Fallback por CDN (1688 arma la galería con JS) ────────────────


@pytest.mark.asyncio
async def test_cdn_regex_fallback(monkeypatch):
    url = "https://detail.1688.com/offer/987654.html"
    html = b"""
    <html><head></head><body><script>
      window.__DATA__ = {"images":["https://cbu01.alicdn.com/img/ibank/offer.jpg"]};
    </script></body></html>
    """
    _patch_get(monkeypatch, _response(url, body=html))

    result = await extract(url)
    assert result.image_urls == ["https://cbu01.alicdn.com/img/ibank/offer.jpg"]


@pytest.mark.asyncio
async def test_placeholders_are_dropped(monkeypatch):
    url = "https://tienda.example.com/p/1"
    html = b"""
    <html><head>
      <meta property="og:image" content="https://cdn.example.com/logo.png">
      <meta name="twitter:image" content="https://cdn.example.com/producto-real.jpg">
    </head></html>
    """
    _patch_get(monkeypatch, _response(url, body=html))

    result = await extract(url)
    assert result.image_urls == ["https://cdn.example.com/producto-real.jpg"]


# ─── Errores ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_page_without_images_raises(monkeypatch):
    url = "https://tienda.example.com/p/1"
    _patch_get(monkeypatch, _response(url, body=b"<html><body>hola</body></html>"))

    with pytest.raises(ExtractError):
        await extract(url)


@pytest.mark.asyncio
async def test_http_error_raises(monkeypatch):
    url = "https://tienda.example.com/p/1"
    _patch_get(monkeypatch, _response(url, body=b"", status=404))

    with pytest.raises(ExtractError):
        await extract(url)


@pytest.mark.asyncio
async def test_empty_url_raises():
    with pytest.raises(ExtractError):
        await extract("   ")
