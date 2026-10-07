"""La IP del cliente sale del último hop confiable de X-Forwarded-For.

Hugo corre detrás del Traefik de Coolify. Tomar el PRIMER valor del header
dejaba falsificar la IP con un `X-Forwarded-For: 1.2.3.4` y saltear el rate
limit de /verify y el lockout del login.
"""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app import auth, security  # noqa: E402

PEER = "10.0.0.7"  # el socket: Traefik hablándole a Hugo


def _request(xff: str | None = None, real_ip: str | None = None) -> Request:
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    if real_ip is not None:
        headers.append((b"x-real-ip", real_ip.encode()))
    return Request({
        "type": "http", "method": "GET", "path": "/verify", "headers": headers,
        "client": (PEER, 1234), "scheme": "http", "server": ("hugo", 8000),
        "query_string": b"", "root_path": "",
    })


def test_one_trusted_hop_uses_the_rightmost_entry():
    # Traefik agregó "203.0.113.9" al final; lo de la izquierda lo mandó el cliente.
    assert security.client_ip(_request("1.2.3.4, 203.0.113.9"), hops=1) == "203.0.113.9"


def test_spoofed_prefix_is_ignored():
    """Con el código viejo esto devolvía 1.2.3.4 (lo que puso el atacante)."""
    assert security.client_ip(_request("1.2.3.4, 5.6.7.8, 203.0.113.9"), hops=1) == "203.0.113.9"


def test_two_trusted_hops_cloudflare_then_traefik():
    # Cloudflare agregó la IP real, Traefik agregó la de Cloudflare.
    assert security.client_ip(_request("203.0.113.9, 172.64.1.1"), hops=2) == "203.0.113.9"


def test_chain_shorter_than_hops_falls_back_to_the_first_entry():
    assert security.client_ip(_request("203.0.113.9"), hops=2) == "203.0.113.9"


def test_zero_hops_ignores_headers_entirely():
    assert security.client_ip(_request("1.2.3.4", real_ip="9.9.9.9"), hops=0) == PEER


def test_without_forwarded_for_uses_x_real_ip_then_socket():
    assert security.client_ip(_request(real_ip="203.0.113.9"), hops=1) == "203.0.113.9"
    assert security.client_ip(_request(), hops=1) == PEER


def test_default_hops_come_from_settings(monkeypatch):
    monkeypatch.setattr(security, "get_settings", lambda: type("S", (), {"trusted_proxy_hops": 2})())
    assert security.client_ip(_request("203.0.113.9, 172.64.1.1")) == "203.0.113.9"


def test_login_and_rate_limit_share_the_same_function():
    assert auth.client_ip is security.client_ip


def test_rate_limit_keys_by_the_trusted_ip(monkeypatch):
    """Dos requests con el mismo hop confiable comparten la cuota aunque el
    cliente cambie el prefijo falsificado."""
    from fastapi import HTTPException

    monkeypatch.setattr(security, "get_settings", lambda: type("S", (), {"trusted_proxy_hops": 1})())
    monkeypatch.setattr(security, "_VERIFY_MAX_PER_WINDOW", 2)
    security._verify_hits.clear()
    security.verify_rate_limit(_request("1.1.1.1, 203.0.113.9"))
    security.verify_rate_limit(_request("2.2.2.2, 203.0.113.9"))
    with pytest.raises(HTTPException) as exc:
        security.verify_rate_limit(_request("3.3.3.3, 203.0.113.9"))
    assert exc.value.status_code == 429
    security._verify_hits.clear()
