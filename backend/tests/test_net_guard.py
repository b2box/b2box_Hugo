"""Tests del guard anti-SSRF."""

import httpx
import pytest

from app import net_guard
from app.net_guard import SsrfBlocked, assert_public_url


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",  # metadata cloud
    "http://localhost:8000/",
    "http://127.0.0.1/",
    "http://10.0.0.5/x",
    "http://192.168.1.1/",
    "file:///etc/passwd",
    "ftp://example.com/x",
    "",
    "http:///nohost",
])
def test_blocks_private_and_bad_schemes(url):
    with pytest.raises(SsrfBlocked):
        assert_public_url(url)


def test_allows_public_https():
    # dominio público real y estable; resuelve a IP pública
    assert_public_url("https://example.com/image.jpg")


# ─── IPs que no son de internet pública (M1) ────────────────────────────────
# Antes pasaban: el rango compartido 100.64.0.0/10 (la metadata de Alibaba está
# en 100.100.100.200), fec0::/10, el multicast y las IPv6 mapeadas / NAT64 que
# llevan una IPv4 privada adentro.

NOT_PUBLIC = [
    "100.64.0.1", "100.100.100.200", "100.127.255.254",    # CGNAT / metadata de Alibaba
    "169.254.169.254", "127.0.0.1", "10.0.0.5", "172.16.0.1", "192.168.1.1", "0.0.0.0",
    "224.0.0.1", "239.255.255.250",                        # multicast IPv4
    "::1", "::", "fe80::1", "fc00::1", "fd00::1",
    "fec0::1",                                             # site-local (deprecado)
    "ff02::1", "ff0e::1",                                  # multicast IPv6
    "::ffff:10.0.0.1", "::ffff:127.0.0.1", "::ffff:169.254.169.254",   # mapeadas a IPv4 privada
    "::ffff:100.100.100.200", "::ffff:192.168.0.1",
    "64:ff9b::a00:1", "64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe",         # NAT64 hacia IPv4 privada
    "64:ff9b::808:808",                                    # NAT64 aunque apunte a una pública
    "2002:a00:1::", "2002:7f00:1::",                       # 6to4 con IPv4 privada adentro
    "2001:db8::1", "100::1",
    "not-an-ip", "", "999.1.1.1",
]
PUBLIC = ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111", "2a00:1450:4001:81b::200e",
          "::ffff:8.8.8.8"]                                # mapeada a una IPv4 pública: se juzga por la v4


@pytest.mark.parametrize("ip", NOT_PUBLIC)
def test_ip_is_public_rejects(ip):
    assert not net_guard._ip_is_public(ip)


@pytest.mark.parametrize("ip", PUBLIC)
def test_ip_is_public_accepts(ip):
    assert net_guard._ip_is_public(ip)


@pytest.mark.parametrize("host", [
    "100.100.100.200", "100.64.0.1", "[fec0::1]", "[::ffff:10.0.0.1]", "[64:ff9b::a00:1]",
    "[::ffff:127.0.0.1]", "224.0.0.1", "[ff02::1]",
])
def test_assert_public_url_blocks_literal_hosts(host):
    with pytest.raises(SsrfBlocked, match="no pública"):
        assert_public_url(f"http://{host}/latest/meta-data/")


@pytest.mark.parametrize("answers", [
    ["93.184.216.34", "100.100.100.200"],                  # A pública + A de metadata
    ["2606:4700:4700::1111", "fec0::1"],                   # AAAA pública + site-local
    ["8.8.8.8", "::ffff:10.0.0.1"],
    ["64:ff9b::a00:1"],
])
def test_assert_public_url_blocks_a_host_if_any_of_its_ips_is_not_public(monkeypatch, answers):
    infos = [(0, 0, 0, "", (ip, 0)) for ip in answers]
    monkeypatch.setattr(net_guard.socket, "getaddrinfo", lambda *a, **kw: infos)
    with pytest.raises(SsrfBlocked, match="no pública"):
        assert_public_url("https://rebind.example/x.jpg")


def test_assert_public_url_allows_a_host_whose_ips_are_all_public(monkeypatch):
    infos = [(0, 0, 0, "", (ip, 0)) for ip in ("93.184.216.34", "2606:4700:4700::1111")]
    monkeypatch.setattr(net_guard.socket, "getaddrinfo", lambda *a, **kw: infos)
    assert_public_url("https://ok.example/x.jpg")


# safe_get = lo que usan los fetch de imágenes de /verify (Luis).


def _client_factory(handler):
    real = httpx.AsyncClient
    return lambda **kw: real(transport=httpx.MockTransport(handler), **kw)


async def test_safe_get_refuses_the_alibaba_metadata_ip():
    with pytest.raises(SsrfBlocked):
        await net_guard.safe_get("http://100.100.100.200/latest/meta-data/", timeout=httpx.Timeout(2.0))


async def test_safe_get_refuses_a_redirect_into_cgnat(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://100.100.100.200/latest/meta-data/"},
                              request=request)

    monkeypatch.setattr(net_guard.httpx, "AsyncClient", _client_factory(handler))
    # DNS: los nombres resuelven a una pública; una IP literal se resuelve a sí misma.
    monkeypatch.setattr(net_guard.socket, "getaddrinfo",
                        lambda host, *a, **kw: [(0, 0, 0, "", (host if host[0].isdigit() else "93.184.216.34", 0))])
    with pytest.raises(SsrfBlocked, match="100.100.100.200"):
        await net_guard.safe_get("https://example.com/a.jpg", timeout=httpx.Timeout(2.0))
    assert seen == ["https://example.com/a.jpg"]


# ─── assert_peer_public: la IP a la que quedó conectado el socket ───────────


class _Stream:
    def __init__(self, server_addr):
        self.server_addr = server_addr

    def get_extra_info(self, name):
        return self.server_addr if name == "server_addr" else None


def _response(server_addr="__none__"):
    ext = {} if server_addr == "__none__" else {"network_stream": _Stream(server_addr)}
    return httpx.Response(200, extensions=ext)


@pytest.mark.parametrize("addr", [("93.184.216.34", 443), ("2606:4700:4700::1111", 443, 0, 0), ["8.8.8.8", 80]])
def test_assert_peer_public_accepts_public_peers(addr):
    net_guard.assert_peer_public(_response(addr))


@pytest.mark.parametrize("addr", [
    ("127.0.0.1", 80), ("10.0.0.1", 443), ("169.254.169.254", 80), ("100.100.100.200", 80),
    ("::1", 80, 0, 0), ("fec0::1", 80, 0, 0), ("::ffff:10.0.0.1", 80, 0, 0), ("64:ff9b::a00:1", 80, 0, 0),
])
def test_assert_peer_public_rejects_internal_peers(addr):
    with pytest.raises(SsrfBlocked, match="no pública"):
        net_guard.assert_peer_public(_response(addr))


@pytest.mark.parametrize("addr", ["__none__", None, (), "/var/run/docker.sock", (None, 80), (1234, 80)])
def test_assert_peer_public_fails_closed_when_the_peer_is_unknown(addr):
    with pytest.raises(SsrfBlocked, match="no se pudo verificar"):
        net_guard.assert_peer_public(_response(addr))


async def test_assert_peer_public_with_the_real_httpcore_extension():
    """El `network_stream` de verdad (httpcore + anyio) expone server_addr."""
    import asyncio

    async def serve(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nab")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            async with client.stream("GET", f"http://127.0.0.1:{port}/") as resp:
                with pytest.raises(SsrfBlocked, match="127.0.0.1"):
                    net_guard.assert_peer_public(resp)
    finally:
        server.close()
        await server.wait_closed()


# ─── safe_get(max_bytes=…): tope del cuerpo descomprimido ───────────────────


class _Raw(httpx.AsyncByteStream):
    """Cuerpo CRUDO (lo que viaja por el cable): con `content=` httpx lo leería y descomprimiría
    al construir la Response, y no se podría probar la lectura en streaming."""

    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for c in self.chunks:
            yield c


def _raw_response(req, *chunks: bytes, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(status, headers={k.replace("_", "-"): v for k, v in headers.items()},
                          stream=_Raw(*chunks), request=req)


def _patch_for_streaming(monkeypatch, handler):
    monkeypatch.setattr(net_guard.httpx, "AsyncClient", _client_factory(handler))
    monkeypatch.setattr(net_guard.socket, "getaddrinfo", lambda host, *a, **kw: [(0, 0, 0, "", ("93.184.216.34", 0))])
    # El transporte de mentira no expone el socket: el chequeo de la IP conectada se salta.
    monkeypatch.setattr(net_guard, "assert_peer_public", lambda resp: None)


async def test_safe_get_with_max_bytes_returns_a_normal_response(monkeypatch):
    _patch_for_streaming(monkeypatch, lambda req: _raw_response(req, b"hola ", b"mundo", x_test="1"))
    resp = await net_guard.safe_get("https://example.com/p", timeout=httpx.Timeout(2.0), max_bytes=100)
    assert resp.status_code == 200 and resp.content == b"hola mundo" and resp.text == "hola mundo"
    assert resp.headers["x-test"] == "1"


async def test_safe_get_with_max_bytes_rejects_a_body_over_the_cap(monkeypatch):
    _patch_for_streaming(monkeypatch, lambda req: _raw_response(req, b"x" * 600, b"x" * 600, b"x" * 600))
    with pytest.raises(net_guard.ResponseTooLarge):
        await net_guard.safe_get("https://example.com/p", timeout=httpx.Timeout(2.0), max_bytes=1000)


async def test_safe_get_with_max_bytes_rejects_a_declared_size_over_the_cap(monkeypatch):
    _patch_for_streaming(monkeypatch, lambda req: _raw_response(req, b"x", content_length="999999"))
    with pytest.raises(net_guard.ResponseTooLarge):
        await net_guard.safe_get("https://example.com/p", timeout=httpx.Timeout(2.0), max_bytes=1000)


async def test_safe_get_with_max_bytes_opens_one_layer_of_gzip_with_a_cap(monkeypatch):
    import gzip

    bomb = gzip.compress(b"a" * 200_000)           # chico en el cable, grande al abrirlo
    assert len(bomb) < 1000
    _patch_for_streaming(monkeypatch, lambda req: _raw_response(req, bomb, content_encoding="gzip"))
    with pytest.raises(net_guard.ResponseTooLarge):
        await net_guard.safe_get("https://example.com/p", timeout=httpx.Timeout(2.0), max_bytes=10_000)
    ok = await net_guard.safe_get("https://example.com/p", timeout=httpx.Timeout(2.0), max_bytes=300_000)
    assert ok.content == b"a" * 200_000 and "content-encoding" not in ok.headers


async def test_safe_get_with_max_bytes_opens_gzip_that_arrives_in_pieces(monkeypatch):
    import gzip

    data = gzip.compress(b"hola mundo " * 5000)
    pieces = [data[i:i + 7] for i in range(0, len(data), 7)]
    _patch_for_streaming(monkeypatch, lambda req: _raw_response(req, *pieces, content_encoding="gzip"))
    resp = await net_guard.safe_get("https://example.com/p", timeout=httpx.Timeout(2.0), max_bytes=1_000_000)
    assert resp.content == b"hola mundo " * 5000


async def test_stacked_gzip_bomb_is_refused_before_anything_is_decompressed(monkeypatch):
    """`Content-Encoding: gzip, gzip, gzip`: 246 bytes en el cable que httpx convertía en UN chunk de 64 MB."""
    import gzip
    import time
    import tracemalloc

    payload = b"\0" * (64 * 1024 * 1024)
    for _ in range(3):
        payload = gzip.compress(payload, 9)
    assert len(payload) < 100_000
    _patch_for_streaming(monkeypatch, lambda req: _raw_response(req, payload, content_encoding="gzip, gzip, gzip"))
    tracemalloc.start()
    t0 = time.time()
    with pytest.raises(net_guard.BadEncoding):
        await net_guard.safe_get("https://example.com/p", timeout=httpx.Timeout(2.0), max_bytes=1_000_000)
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert time.time() - t0 < 1.0 and peak < 5_000_000, f"pico de memoria {peak} bytes"


@pytest.mark.parametrize("encoding", ["br", "zstd", "deflate", "gzip, br", "compress", "gzip,gzip"])
async def test_content_encodings_that_are_not_one_layer_of_gzip_are_refused(monkeypatch, encoding):
    _patch_for_streaming(monkeypatch, lambda req: _raw_response(req, b"datos", content_encoding=encoding))
    with pytest.raises(net_guard.BadEncoding):
        await net_guard.safe_get("https://example.com/p", timeout=httpx.Timeout(2.0), max_bytes=1000)


async def test_a_truncated_gzip_is_an_error_not_a_partial_page(monkeypatch):
    import gzip

    data = gzip.compress(b"x" * 5000)[:-10]
    _patch_for_streaming(monkeypatch, lambda req: _raw_response(req, data, content_encoding="gzip"))
    with pytest.raises(net_guard.BadEncoding):
        await net_guard.safe_get("https://example.com/p", timeout=httpx.Timeout(2.0), max_bytes=100_000)


async def test_the_request_only_offers_gzip(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req.headers["accept-encoding"])
        return _raw_response(req, b"ok")

    _patch_for_streaming(monkeypatch, handler)
    await net_guard.safe_get("https://example.com/p", timeout=httpx.Timeout(2.0), max_bytes=100,
                             headers={"accept-encoding": "gzip, deflate, br, zstd", "User-Agent": "x"})
    assert seen == ["gzip"]


async def test_safe_get_with_max_bytes_still_follows_and_validates_redirects(monkeypatch):
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if req.url.path == "/a":
            return _raw_response(req, status=301, location="https://example.com/b")
        return _raw_response(req, b"final")

    _patch_for_streaming(monkeypatch, handler)
    resp = await net_guard.safe_get("https://example.com/a", timeout=httpx.Timeout(2.0), max_bytes=100)
    assert resp.content == b"final" and seen == ["https://example.com/a", "https://example.com/b"]


async def test_safe_get_with_max_bytes_refuses_a_redirect_into_a_private_network(monkeypatch):
    def handler(req):
        return _raw_response(req, status=302, location="http://169.254.169.254/x")

    _patch_for_streaming(monkeypatch, handler)
    monkeypatch.setattr(net_guard.socket, "getaddrinfo",
                        lambda host, *a, **kw: [(0, 0, 0, "", (host if host[0].isdigit() else "93.184.216.34", 0))])
    with pytest.raises(SsrfBlocked):
        await net_guard.safe_get("https://example.com/a", timeout=httpx.Timeout(2.0), max_bytes=100)


async def test_redirect_ok_is_asked_before_following_each_location(monkeypatch):
    seen, asked = [], []

    def handler(req):
        seen.append(str(req.url))
        if req.url.host == "example.com":
            return _raw_response(req, status=301, location="https://otro-sitio.com/x")
        return _raw_response(req, b"no deberia llegar")

    _patch_for_streaming(monkeypatch, handler)

    def ok(url):
        asked.append(url)
        return "example.com" in url

    with pytest.raises(net_guard.RedirectBlocked):
        await net_guard.safe_get("https://example.com/a", timeout=httpx.Timeout(2.0), max_bytes=100, redirect_ok=ok)
    assert asked == ["https://otro-sitio.com/x"] and seen == ["https://example.com/a"], "no se pidió la URL ajena"


async def test_redirect_ok_also_applies_without_a_byte_cap(monkeypatch):
    def handler(req):
        return httpx.Response(302, headers={"location": "https://otro.com/"}, request=req)

    monkeypatch.setattr(net_guard.httpx, "AsyncClient", _client_factory(handler))
    monkeypatch.setattr(net_guard.socket, "getaddrinfo", lambda host, *a, **kw: [(0, 0, 0, "", ("93.184.216.34", 0))])
    with pytest.raises(net_guard.RedirectBlocked):
        await net_guard.safe_get("https://example.com/a", timeout=httpx.Timeout(2.0), redirect_ok=lambda u: False)


async def test_dns_resolution_does_not_block_the_event_loop(monkeypatch):
    """getaddrinfo es bloqueante: corre en un thread, así un DNS lento no congela el resto."""
    import asyncio
    import threading
    import time

    main = threading.get_ident()
    seen = {}

    def slow_dns(host, *a, **kw):
        seen["thread"] = threading.get_ident()
        time.sleep(0.3)
        return [(0, 0, 0, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(net_guard.httpx, "AsyncClient", _client_factory(lambda req: _raw_response(req, b"ok")))
    monkeypatch.setattr(net_guard.socket, "getaddrinfo", slow_dns)
    monkeypatch.setattr(net_guard, "assert_peer_public", lambda resp: None)
    ticks = []

    async def ticker():
        for _ in range(5):
            await asyncio.sleep(0.05)
            ticks.append(1)

    task = asyncio.create_task(ticker())
    await net_guard.safe_get("https://example.com/a", timeout=httpx.Timeout(2.0), max_bytes=100)
    await task
    assert seen["thread"] != main and len(ticks) == 5
