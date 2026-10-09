"""Las fotos que bajan de hosts de terceros (tiendas) tienen tope de bytes en el cable y de píxeles
antes de decodificar: un cuerpo infinito o un PNG de cientos de millones de píxeles no tumban a Hugo."""

from __future__ import annotations

import os
from io import BytesIO

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import httpx  # noqa: E402
import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from app.dedup import image_embed, image_hash  # noqa: E402


def _png(width: int, height: int) -> bytes:
    out = BytesIO()
    Image.new("L", (width, height)).save(out, format="PNG", compress_level=9)
    return out.getvalue()


def _jpeg(width: int, height: int) -> bytes:
    out = BytesIO()
    Image.new("L", (width, height), 128).save(out, format="JPEG", quality=5)
    return out.getvalue()


async def test_the_photo_download_has_a_byte_cap(monkeypatch):
    seen = {}

    async def fake_safe_get(url, **kw):
        seen.update(kw)
        return httpx.Response(200, content=b"x", request=httpx.Request("GET", url))

    monkeypatch.setattr(image_hash, "safe_get", fake_safe_get)
    assert await image_hash._fetch("https://acdn-us.mitiendanube.com/a.webp") == b"x"
    assert seen["max_bytes"] == image_hash._MAX_IMAGE_BYTES == 8 * 1024 * 1024


async def test_a_hostile_photo_host_cannot_make_the_download_unbounded(monkeypatch):
    """Un cuerpo infinito se corta en el tope, sin juntarlo entero (el tope lo aplica safe_get)."""
    from app import net_guard

    class Endless(httpx.AsyncByteStream):
        sent = 0

        async def __aiter__(self):
            while True:
                Endless.sent += 1_000_000
                yield b"x" * 1_000_000

    def handler(req):
        return httpx.Response(200, stream=Endless(), request=req)

    real = httpx.AsyncClient
    monkeypatch.setattr(net_guard.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(net_guard.socket, "getaddrinfo", lambda host, *a, **kw: [(0, 0, 0, "", ("93.184.216.34", 0))])
    monkeypatch.setattr(net_guard, "assert_peer_public", lambda resp: None)
    monkeypatch.setattr(image_hash, "_RETRY_BACKOFF_SECONDS", (0.0, 0.0))
    with pytest.raises(net_guard.ResponseTooLarge):
        await image_hash._fetch("https://acdn-us.mitiendanube.com/infinito.webp")
    assert Endless.sent <= 10 * 1_000_000


def test_open_checked_rejects_a_png_that_declares_too_many_pixels():
    bomb = _png(5000, 4000)                      # 20 MP: pesa casi nada y decodificado son decenas de MB
    assert len(bomb) < 100_000
    with pytest.raises(image_hash.ImageTooBig):
        image_hash.open_checked(bomb)
    assert image_hash.open_checked(_png(2000, 2000)).size == (2000, 2000)


def test_a_big_jpeg_is_accepted_but_decoded_reduced():
    img = image_hash.open_checked(_jpeg(4000, 3000))       # 12 MP < 40 MP: entra y se decodifica reducido
    assert img.convert("RGB").size[0] < 4000


def test_clip_preprocess_refuses_a_pixel_bomb_before_decoding_it():
    with pytest.raises(image_hash.ImageTooBig):
        image_embed._preprocess(_png(5000, 4000))
    assert image_embed._preprocess(_png(300, 200)).shape == (1, 3, 224, 224)


async def test_hash_image_gives_up_on_a_pixel_bomb(monkeypatch):
    async def fake_fetch(url, **kw):
        return _png(5000, 4000)

    monkeypatch.setattr(image_hash, "_fetch", fake_fetch)
    monkeypatch.setattr(image_hash, "_db_get", lambda url: None)
    monkeypatch.setattr(image_hash, "_db_put", lambda url, h: None)
    assert await image_hash.hash_image("https://acdn-us.mitiendanube.com/bomba.png") is None


# ─── tope total de tiempo ────────────────────────────────────────────────────


async def test_a_photo_that_drips_is_cut_by_the_total_deadline(monkeypatch):
    """Un host que gotea un byte cada 19 s: el timeout de httpx es por chunk y no lo corta nunca."""
    import asyncio
    import time

    async def drip(url, **kw):
        await asyncio.sleep(60)

    monkeypatch.setattr(image_hash, "safe_get", drip)
    monkeypatch.setattr(image_hash, "_DEADLINE_S", 0.1)
    t0 = time.time()
    with pytest.raises(asyncio.TimeoutError):
        await image_hash._fetch("https://acdn-us.mitiendanube.com/lenta.webp")
    assert time.time() - t0 < 2.0


async def test_the_deadline_is_shorter_when_a_client_is_waiting(monkeypatch):
    import asyncio

    seen = {}
    real = asyncio.wait_for

    async def spy(aw, timeout):
        seen["timeout"] = timeout
        return await real(aw, timeout)

    async def ok(url, **kw):
        return httpx.Response(200, content=b"x", request=httpx.Request("GET", url))

    monkeypatch.setattr(image_hash, "safe_get", ok)
    monkeypatch.setattr(image_hash.asyncio, "wait_for", spy)
    await image_hash._fetch("https://x.example/a.png", interactive=True)
    assert seen["timeout"] == image_hash._INTERACTIVE_DEADLINE_S == 12.0
    await image_hash._fetch("https://x.example/a.png")
    assert seen["timeout"] == image_hash._DEADLINE_S == 30.0
