"""Fotos del juez en base64 (judge_images): qué se baja, qué no y cómo sale.

Sin red: el HTTP va contra un `httpx.MockTransport` y el chequeo de DNS de
net_guard se reemplaza (los hosts de prueba no resuelven).
"""

from __future__ import annotations

import asyncio
import base64
import os
import threading
import time
from io import BytesIO

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import httpx  # noqa: E402
import pytest  # noqa: E402
from PIL import Image, ImageFile  # noqa: E402

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


class FakeNetworkStream:
    """Lo que httpcore cuelga en `response.extensions["network_stream"]`."""

    def __init__(self, server_addr):
        self.server_addr = server_addr

    def get_extra_info(self, name):
        return self.server_addr if name == "server_addr" else None


class PhotoServer:
    """Rutas fijas → respuesta. Registra cada request que llega. Cada respuesta
    dice haber salido de `peer` (IP del socket), una pública por default."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.requested: list[str] = []
        self.peer: object = ("93.184.216.34", 443)
        self.request_headers: list[httpx.Headers] = []

    def add(self, url, body=JPEG, ctype="image/jpeg", status=200, headers=None):
        self.routes[url] = (status, body, {"content-type": ctype, **(headers or {})})

    async def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        self.request_headers.append(request.headers)
        route = self.routes.get(url)
        if callable(route):
            resp = await route(request)
        elif route is None:
            resp = httpx.Response(404, request=request)
        else:
            status, body, headers = route
            resp = httpx.Response(status, content=body, headers=headers, request=request)
        if self.peer is not None:
            resp.extensions["network_stream"] = FakeNetworkStream(self.peer)
        return resp

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


# ─── IP real del servidor (B1), proxy de entorno (B2), compresión (B3) ──────


class NeverRead:
    """Cuerpo que no se puede leer: si el código lo toca, el test falla."""

    def __aiter__(self):
        raise AssertionError("se leyó el body de un servidor que no era público")


async def test_the_real_peer_ip_is_checked_before_reading_the_body(server, monkeypatch):
    """DNS dijo "pública" (assert_public_url pasa) pero el socket quedó
    conectado a una IP interna: rebinding. Hay que cortar sin leer el body."""
    async def read(resp):
        raise AssertionError("se leyó el body de un servidor que no era público")

    monkeypatch.setattr(judge_images, "_read_capped", read)
    for peer in [("127.0.0.1", 443), ("10.1.2.3", 443), ("169.254.169.254", 80), ("100.100.100.200", 443),
                 ("::1", 443, 0, 0), ("fec0::1", 443, 0, 0), ("::ffff:10.0.0.1", 443, 0, 0)]:
        server.peer = peer
        server.add(ML)
        async with server.client() as http:
            with pytest.raises(net_guard.SsrfBlocked, match="no pública"):
                await judge_images.download(http, ML)


async def test_a_redirect_from_a_non_public_peer_is_not_followed(server):
    final = "https://http2.mlstatic.com/final.jpg"
    server.peer = ("10.0.0.7", 443)
    server.add(ML, status=302, body=b"", headers={"location": final})
    server.add(final)
    async with server.client() as http:
        with pytest.raises(net_guard.SsrfBlocked):
            await judge_images.download(http, ML)
    assert server.requested == [ML]


async def test_when_the_transport_does_not_expose_the_peer_it_fails_closed(server):
    server.peer = None
    server.add(ML)
    async with server.client() as http:
        with pytest.raises(net_guard.SsrfBlocked, match="no se pudo verificar"):
            await judge_images.download(http, ML)


@pytest.mark.parametrize("peer", [("93.184.216.34", 443), ("2606:4700:4700::1111", 443, 0, 0)])
async def test_a_public_peer_passes(server, peer):
    server.peer = peer
    server.add(ML)
    async with server.client() as http:
        assert await judge_images.download(http, ML) == JPEG


async def test_a_photo_from_a_non_public_peer_is_just_omitted(server):
    server.add(OURS)
    server.add(ML)
    server.peer = ("10.0.0.7", 443)
    assert await judge_images.inline_images([OURS, ML]) == {}


async def test_dns_rebinding_against_a_real_socket(monkeypatch):
    """Sin dobles de transporte: cliente real contra un servidor en 127.0.0.1
    con el chequeo de DNS "engañado". No se lee ni un byte del body."""
    async def serve(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: image/jpeg\r\ncontent-length: 2\r\n\r\nab")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/x.jpg"
    monkeypatch.setattr(judge_images, "allowed_url", lambda u: u)          # el host "pasa" la lista
    monkeypatch.setattr(net_guard, "assert_public_url", lambda u: None)    # y el DNS "dio" pública

    async def read(resp):
        raise AssertionError("se leyó el body")

    monkeypatch.setattr(judge_images, "_read_capped", read)
    try:
        async with judge_images.make_http_client() as http:
            with pytest.raises(net_guard.SsrfBlocked, match="127.0.0.1"):
                await judge_images.download(http, url)
    finally:
        server.close()
        await server.wait_closed()


def test_the_client_ignores_proxy_and_ssl_environment(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(var, "http://10.9.9.9:3128")
    assert httpx.AsyncClient()._mounts                      # control: por default el proxy de entorno entra
    client = judge_images.make_http_client()
    assert client.trust_env is False and not client._mounts and client.follow_redirects is False


async def test_it_asks_for_no_compression(server):
    server.add(ML)
    async with server.client() as http:
        await judge_images.download(http, ML)
    assert server.request_headers[0]["accept-encoding"] == "identity"


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "zstd", "GZIP", "identity, gzip", "gzip, gzip"])
async def test_a_compressed_response_is_rejected_before_reading(server, monkeypatch, encoding):
    async def read(resp):
        raise AssertionError("se leyó un body comprimido")

    monkeypatch.setattr(judge_images, "_read_capped", read)

    async def body():
        yield JPEG

    async def route(request):
        # Body en streaming: con bytes, httpx intentaría descomprimirlo al construir la respuesta.
        return httpx.Response(200, content=body(), request=request,
                              headers={"content-type": "image/jpeg", "content-encoding": encoding})

    server.routes[ML] = route
    async with server.client() as http:
        with pytest.raises(ImageRejected, match="content-encoding"):
            await judge_images.download(http, ML)


@pytest.mark.parametrize("encoding", ["identity", "Identity", ""])
async def test_identity_encoding_is_fine(server, encoding):
    server.add(ML, headers={"content-encoding": encoding} if encoding else None)
    async with server.client() as http:
        assert await judge_images.download(http, ML) == JPEG


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


# ─── memoria acotada (QA-2) ────────────────────────────────────────────────


def _never_decode(monkeypatch):
    def boom(self, *a, **kw):
        raise AssertionError("se decodificó una imagen que se tenía que rechazar por el header")

    monkeypatch.setattr(ImageFile.ImageFile, "load", boom)


@pytest.mark.parametrize("fmt,mode,color", [
    ("PNG", "RGBA", (10, 20, 30, 128)), ("PNG", "RGB", (10, 20, 30)), ("WEBP", "RGB", (10, 20, 30)),
    ("BMP", "RGB", (10, 20, 30)), ("GIF", "P", 3),
])
def test_formats_without_draft_are_capped_at_16_mp_from_the_header(monkeypatch, fmt, mode, color):
    """PNG/WebP/GIF/BMP se decodifican enteros: 17 MP se rechazan sin decodificar."""
    raw = image_bytes(fmt, size=(4100, 4100), mode=mode, color=color)
    _never_decode(monkeypatch)
    with pytest.raises(ImageRejected, match=r"dimensiones fuera de rango \(4100x4100\)"):
        judge_images.to_jpeg_data_url(raw)


def test_a_40_mp_png_is_rejected_from_the_header_and_weighs_kb(monkeypatch):
    raw = image_bytes("PNG", size=(8000, 5000), mode="RGBA", color=(200, 10, 10, 128))
    assert len(raw) < 300_000                       # una bomba: KB en el cable, 160 MB decodificada
    _never_decode(monkeypatch)
    with pytest.raises(ImageRejected, match="dimensiones"):
        judge_images.to_jpeg_data_url(raw)


def test_a_png_just_under_16_mp_still_goes_through():
    img = decode(judge_images.to_jpeg_data_url(image_bytes("PNG", size=(4000, 4000), mode="RGBA",
                                                          color=(200, 10, 10, 255))))
    assert img.size == (768, 768)


def test_a_jpeg_keeps_the_higher_cap_because_draft_decodes_it_reduced():
    raw = image_bytes("JPEG", size=(5500, 4000))    # 22 MP: pasa de 16 MP pero es JPEG
    assert 5500 * 4000 > judge_images._MAX_PIXELS
    width, height = decode(judge_images.to_jpeg_data_url(raw)).size
    assert width == 768 and abs(height - 558) <= 1


def test_a_jpeg_over_its_own_cap_is_rejected(monkeypatch):
    monkeypatch.setattr(judge_images, "_MAX_PIXELS_JPEG", 1000)
    with pytest.raises(ImageRejected, match="dimensiones"):
        judge_images.to_jpeg_data_url(image_bytes("JPEG", size=(100, 100)))


@pytest.mark.parametrize("mode,color", [("RGBA", (200, 10, 10, 128)), ("LA", (90, 128)), ("RGB", (1, 2, 3)),
                                        ("L", 5), ("P", 3)])
def test_the_photo_is_shrunk_before_it_is_flattened(monkeypatch, mode, color):
    """Aplanar la transparencia a tamaño completo copiaba la imagen entera
    (RGBA → RGBA → canvas): _flatten_rgb solo debe ver la imagen ya chica."""
    seen = []
    real = judge_images._flatten_rgb
    monkeypatch.setattr(judge_images, "_flatten_rgb", lambda img: seen.append(img.size) or real(img))
    raw = image_bytes("PNG", size=(3000, 2000), mode=mode, color=color)
    assert decode(judge_images.to_jpeg_data_url(raw)).size == (768, 512)
    assert seen == [(768, 512)]


def test_a_palette_png_with_transparency_goes_on_white_and_is_filtered_not_nearest():
    buf = BytesIO()
    pal = Image.new("P", (2000, 2000), 1)
    pal.putpalette([255, 255, 255, 0, 0, 0] + [0, 0, 0] * 254)   # 0 = blanco (transparente), 1 = negro
    pal.paste(0, (0, 0, 2000, 1000))
    pal.save(buf, format="PNG", transparency=0)
    img = decode(judge_images.to_jpeg_data_url(buf.getvalue())).convert("RGB")
    assert img.size == (768, 768)
    assert min(img.getpixel((384, 100))) > 240           # arriba, transparente → blanco
    assert max(img.getpixel((384, 700))) < 30            # abajo, negro opaco


def test_metadata_is_not_kept_in_the_jpeg():
    """El comentario COM, el EXIF y el perfil ICC no viajan al proveedor."""
    exif = Image.Exif()
    exif[0x010E] = "descripcion-secreta"
    buf = BytesIO()
    Image.new("RGB", (200, 100), (9, 9, 9)).save(buf, format="JPEG", comment=b"comentario-secreto",
                                                 exif=exif, icc_profile=b"\x00" * 128)
    out = base64.b64decode(judge_images.to_jpeg_data_url(buf.getvalue()).split(",", 1)[1])
    assert b"comentario-secreto" not in out and b"descripcion-secreta" not in out
    img = Image.open(BytesIO(out))
    assert "comment" not in img.info and "icc_profile" not in img.info and len(img.getexif()) == 0


async def test_decode_runs_one_at_a_time_across_concurrent_queries(server, monkeypatch):
    """Descarga concurrente, pero UN decode a la vez en todo el proceso, aunque
    haya dos consultas (dos inline_images) en paralelo."""
    lock, state = threading.Lock(), {"now": 0, "max": 0, "calls": 0}

    def fake_decode(raw):
        with lock:
            state["now"] += 1
            state["calls"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.02)
        with lock:
            state["now"] -= 1
        return "data:image/jpeg;base64,AAAA"

    monkeypatch.setattr(judge_images, "to_jpeg_data_url", fake_decode)
    first, second = _mlstatic(8), [f"https://http2.mlstatic.com/E_{i}.jpg" for i in range(8)]
    for u in [*first, *second]:
        server.add(u)
    a, b = await asyncio.gather(judge_images.inline_images(first), judge_images.inline_images(second))
    assert len(a) == len(b) == 8 and state["calls"] == 16
    assert state["max"] == judge_images._DECODE_WORKERS == 1


async def test_a_decode_still_queued_at_the_deadline_never_runs(server, monkeypatch):
    monkeypatch.setattr(judge_images, "DEADLINE_S", 0.25)
    started = []

    def slow_decode(raw):
        started.append(1)
        time.sleep(0.1)
        return "data:image/jpeg;base64,AAAA"

    monkeypatch.setattr(judge_images, "to_jpeg_data_url", slow_decode)
    urls = _mlstatic(8)
    for u in urls:
        server.add(u)
    out = await judge_images.inline_images(urls)
    at_return = len(started)
    assert 1 <= len(out) < 8 and at_return < 8
    await asyncio.sleep(0.5)                     # lo que quedó en cola no arranca después
    assert len(started) == at_return


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


def _mlstatic(n):
    return [f"https://http2.mlstatic.com/D_{i}.jpg" for i in range(n)]


async def test_the_deadline_is_global_not_per_photo(server, monkeypatch):
    """8 fotos de 0,3 s con concurrencia 4 y tope de 0,45 s: terminan 4 (la
    primera tanda); la segunda tanda se corta a los 0,45 s en vez de llegar a
    0,6 s (con un tope por foto, las 8 entraban y el total pasaba de lo
    permitido: 2 x tope en el peor caso)."""
    monkeypatch.setattr(judge_images, "DEADLINE_S", 0.45)

    async def slowish(request):
        await asyncio.sleep(0.3)
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}, request=request)

    urls = _mlstatic(8)
    for u in urls:
        server.routes[u] = slowish
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    out = await judge_images.inline_images(urls)
    elapsed = loop.time() - t0
    assert set(out) == set(urls[:4])
    assert 0.4 <= elapsed < 0.57


async def test_what_finished_before_the_deadline_is_kept(server, monkeypatch):
    monkeypatch.setattr(judge_images, "DEADLINE_S", 0.2)

    async def hangs(request):
        await asyncio.sleep(30)

    urls = _mlstatic(6)
    server.add(urls[0])
    server.add(urls[1], body=image_bytes("PNG"), ctype="image/png")
    for u in urls[2:]:
        server.routes[u] = hangs
    out = await judge_images.inline_images(urls)
    assert set(out) == set(urls[:2])


async def test_the_photos_that_miss_the_deadline_are_cancelled_not_left_running(server, monkeypatch):
    monkeypatch.setattr(judge_images, "DEADLINE_S", 0.1)
    started, cancelled = [], []

    async def hangs(request):
        started.append(1)
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    urls = _mlstatic(6)
    for u in urls:
        server.routes[u] = hangs
    assert await judge_images.inline_images(urls) == {}
    assert len(started) == 4 and len(cancelled) == 4          # la concurrencia; las otras 2 ni arrancaron
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def test_cancelling_the_caller_cancels_the_photos(server):
    cancelled = []

    async def hangs(request):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    urls = _mlstatic(3)
    for u in urls:
        server.routes[u] = hangs
    job = asyncio.create_task(judge_images.inline_images(urls))
    await asyncio.sleep(0.05)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert len(cancelled) == 3
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def test_inline_images_without_urls_makes_no_client(monkeypatch):
    monkeypatch.setattr(judge_images, "make_http_client", lambda: pytest.fail("no debería abrir un cliente"))
    assert await judge_images.inline_images([]) == {}
