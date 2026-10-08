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
