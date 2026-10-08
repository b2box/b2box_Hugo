"""Fotos del juez en base64 (judge_images): qué se baja, qué no y cómo sale.

Sin red: el HTTP va contra un `httpx.MockTransport` y el chequeo de DNS de
net_guard se reemplaza (los hosts de prueba no resuelven).
"""

from __future__ import annotations

import asyncio
import base64
import os
from io import BytesIO

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import httpx  # noqa: E402
import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from app import net_guard  # noqa: E402
from app.pricing import judge_images  # noqa: E402
from app.pricing.judge_images import ImageRejected  # noqa: E402

ML = "https://http2.mlstatic.com/D_NQ_NP_123-O.webp"
OURS = "https://example.invalid/assets/preview/ab/foto__preview.jpg"   # host de VENDURE_API_URL


def image_bytes(fmt="JPEG", size=(64, 48), mode="RGB", color=(200, 30, 30)) -> bytes:
    buf = BytesIO()
    Image.new(mode, size, color).save(buf, format=fmt)
    return buf.getvalue()


JPEG = image_bytes()


class PhotoServer:
    """Rutas fijas → respuesta. Registra cada request que llega."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.requested: list[str] = []

    def add(self, url, body=JPEG, ctype="image/jpeg", status=200, headers=None):
        self.routes[url] = (status, body, {"content-type": ctype, **(headers or {})})

    async def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        route = self.routes.get(url)
        if callable(route):
            return await route(request)
        if route is None:
            return httpx.Response(404, request=request)
        status, body, headers = route
        return httpx.Response(status, content=body, headers=headers, request=request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), follow_redirects=False)


@pytest.fixture
def server(monkeypatch):
    srv = PhotoServer()
    monkeypatch.setattr(net_guard, "assert_public_url", lambda url: None)
    monkeypatch.setattr(judge_images, "make_http_client", srv.client)
    return srv


def decode(data_url: str) -> Image.Image:
    prefix = "data:image/jpeg;base64,"
    assert data_url.startswith(prefix)
    return Image.open(BytesIO(base64.b64decode(data_url[len(prefix):])))


# ─── hosts permitidos ──────────────────────────────────────────────────────


@pytest.mark.parametrize("url,expected", [
    (ML, ML),
    ("http://http2.mlstatic.com/a.jpg", "https://http2.mlstatic.com/a.jpg"),   # se sube a https
    (OURS, OURS),
    ("https://EXAMPLE.invalid/assets/x.png", "https://EXAMPLE.invalid/assets/x.png"),
    ("http://example.invalid/assets/x.jpg", None),          # nuestras: solo https
    ("https://cdn.example.invalid/assets/x.jpg", None),     # subdominio de Vendure: no
    ("https://mlstatic.com.evil.com/a.jpg", None),
    ("https://evil.com/a.jpg", None),
    ("https://example.invalid@evil.com/a.jpg", None),       # userinfo
    ("https://example.invalid:8443/a.jpg", None),           # puerto raro
    ("javascript:alert(1)", None),
    ("data:image/png;base64,AAAA", None),
    ("", None),
    (None, None),
])
def test_allowed_url(url, expected):
    assert judge_images.allowed_url(url) == expected


# ─── descarga ──────────────────────────────────────────────────────────────


async def test_downloads_an_allowed_photo_with_browser_headers(server):
    seen = {}

    async def route(request):
        seen.update(request.headers)
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}, request=request)

    server.routes[ML] = route
    async with server.client() as http:
        assert await judge_images.download(http, ML) == JPEG
    assert "python-httpx" not in seen["user-agent"]


async def test_a_host_that_is_not_allowed_is_never_requested(server):
    async with server.client() as http:
        with pytest.raises(ImageRejected, match="no permitido"):
            await judge_images.download(http, "https://evil.com/a.jpg")
    assert server.requested == []


async def test_dns_to_a_private_ip_is_blocked(server, monkeypatch):
    def private(url):
        raise net_guard.SsrfBlocked("resuelve a 10.0.0.1")

    monkeypatch.setattr(net_guard, "assert_public_url", private)
    server.add(ML)
    async with server.client() as http:
        with pytest.raises(net_guard.SsrfBlocked):
            await judge_images.download(http, ML)
    assert server.requested == []


@pytest.mark.parametrize("ctype", ["text/html", "image/svg+xml", "image/tiff", "application/octet-stream", ""])
async def test_content_type_must_be_an_allowed_image(server, ctype):
    server.add(ML, ctype=ctype)
    async with server.client() as http:
        with pytest.raises(ImageRejected, match="content-type"):
            await judge_images.download(http, ML)


@pytest.mark.parametrize("ctype", sorted(judge_images.CONTENT_TYPES) + ["image/JPEG; charset=binary"])
async def test_allowed_content_types(server, ctype):
    server.add(ML, ctype=ctype)
    async with server.client() as http:
        assert await judge_images.download(http, ML) == JPEG


async def test_declared_size_over_the_cap_is_rejected_before_reading(server, monkeypatch):
    monkeypatch.setattr(judge_images, "MAX_BYTES", 1000)
    server.add(ML, body=b"x" * 10, headers={"content-length": "5000"})
    async with server.client() as http:
        with pytest.raises(ImageRejected, match="demasiado grande"):
            await judge_images.download(http, ML)


async def test_streamed_size_over_the_cap_is_rejected(server, monkeypatch):
    monkeypatch.setattr(judge_images, "MAX_BYTES", 1000)

    async def chunks():
        for _ in range(10):
            yield b"x" * 500

    async def route(request):
        # Sin content-length: el tope se mide mientras se lee.
        return httpx.Response(200, content=chunks(), headers={"content-type": "image/jpeg"}, request=request)

    server.routes[ML] = route
    async with server.client() as http:
        with pytest.raises(ImageRejected, match="demasiado grande"):
            await judge_images.download(http, ML)


def test_the_default_cap_is_5_mb():
    assert judge_images.MAX_BYTES == 5 * 1024 * 1024


@pytest.mark.parametrize("status", [403, 404, 500, 204])
async def test_non_200_is_rejected(server, status):
    server.add(ML, status=status)
    async with server.client() as http:
        with pytest.raises(ImageRejected, match=f"HTTP {status}"):
            await judge_images.download(http, ML)


async def test_redirect_to_an_allowed_host_is_followed(server):
    final = "https://http2.mlstatic.com/final.jpg"
    server.add(ML, status=302, body=b"", headers={"location": final})
    server.add(final)
    async with server.client() as http:
        assert await judge_images.download(http, ML) == JPEG
    assert server.requested == [ML, final]


@pytest.mark.parametrize("location", ["https://evil.com/x.jpg", "http://example.invalid/assets/x.jpg",
                                      "https://169.254.169.254/latest/meta-data"])
async def test_redirect_to_a_host_that_is_not_allowed_is_rejected(server, location):
    server.add(ML, status=301, body=b"", headers={"location": location})
    async with server.client() as http:
        with pytest.raises(ImageRejected, match="redirect"):
            await judge_images.download(http, ML)
    assert server.requested == [ML]


async def test_redirect_loop_is_cut(server):
    server.add(ML, status=302, body=b"", headers={"location": ML})
    async with server.client() as http:
        with pytest.raises(ImageRejected, match="redirects"):
            await judge_images.download(http, ML)


# ─── reducción a JPEG ──────────────────────────────────────────────────────


def test_big_photo_is_resized_to_768_and_reencoded_as_jpeg():
    img = decode(judge_images.to_jpeg_data_url(image_bytes("PNG", size=(2000, 1000))))
    assert img.format == "JPEG" and img.size == (768, 384)


def test_small_photo_keeps_its_size():
    img = decode(judge_images.to_jpeg_data_url(image_bytes("WEBP", size=(300, 200))))
    assert img.size == (300, 200)


def test_transparency_goes_on_white_not_black():
    raw = image_bytes("PNG", size=(40, 40), mode="RGBA", color=(0, 0, 0, 0))
    img = decode(judge_images.to_jpeg_data_url(raw)).convert("RGB")
    assert min(img.getpixel((20, 20))) > 240


@pytest.mark.parametrize("fmt", ["GIF", "BMP", "JPEG"])
def test_other_allowed_formats(fmt):
    assert decode(judge_images.to_jpeg_data_url(image_bytes(fmt))).size == (64, 48)


@pytest.mark.parametrize("raw", [b"<html>no</html>", b"", image_bytes("TIFF"), JPEG[:40]])
def test_bytes_that_are_not_an_allowed_image_are_rejected(raw):
    with pytest.raises(ImageRejected):
        judge_images.to_jpeg_data_url(raw)


def test_decompression_bomb_is_rejected(monkeypatch):
    monkeypatch.setattr(judge_images, "_MAX_PIXELS", 1000)
    with pytest.raises(ImageRejected, match="dimensiones"):
        judge_images.to_jpeg_data_url(image_bytes("PNG", size=(100, 100)))


# ─── varias fotos: la que falla se omite ───────────────────────────────────


async def test_inline_images_skips_the_ones_that_fail_and_dedups(server):
    server.add(OURS, body=image_bytes("PNG"), ctype="image/png")
    server.add(ML, ctype="text/html")                       # tipo no permitido
    other = "https://http2.mlstatic.com/D_2.jpg"
    server.add(other)
    out = await judge_images.inline_images([OURS, OURS, ML, other, "https://evil.com/x.jpg", ""])
    assert set(out) == {OURS, other}
    assert all(v.startswith("data:image/jpeg;base64,") for v in out.values())
    assert server.requested.count(OURS) == 1


async def test_a_photo_that_times_out_is_skipped(server, monkeypatch):
    monkeypatch.setattr(judge_images, "DEADLINE_S", 0.05)

    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}, request=request)

    async def read_timeout(request):
        raise httpx.ReadTimeout("lento", request=request)

    server.routes[ML] = slow
    server.routes["https://http2.mlstatic.com/rt.jpg"] = read_timeout
    server.add(OURS)
    out = await judge_images.inline_images([OURS, ML, "https://http2.mlstatic.com/rt.jpg"])
    assert set(out) == {OURS}


async def test_inline_images_without_urls_makes_no_client(monkeypatch):
    monkeypatch.setattr(judge_images, "make_http_client", lambda: pytest.fail("no debería abrir un cliente"))
    assert await judge_images.inline_images([]) == {}
