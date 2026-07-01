"""Guard anti-SSRF para fetches server-side de URLs no confiables.

Hugo descarga URLs que vienen de afuera: imágenes que manda Luis en /verify y
links de proveedor guardados en campos custom de Vendure. Sin control, un
atacante puede hacer que Hugo pegue a IPs internas (metadata cloud
169.254.169.254, servicios internos, localhost, etc.) → SSRF.

Este módulo:
  - `assert_public_url(url)`: valida scheme http(s) y que el host NO resuelva a
    una IP privada / loopback / link-local / reservada.
  - `safe_get(url, ...)`: httpx GET que sigue redirects manualmente, validando
    CADA salto (un redirect a http://169.254.169.254 no pasa).

Diseño: fail-closed. Ante cualquier duda (DNS falla, IP rara) → SsrfBlocked.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

_ALLOWED_SCHEMES = {"http", "https"}
_MAX_REDIRECTS = 5


class SsrfBlocked(ValueError):
    """La URL apunta (directa o vía redirect/DNS) a una red no pública."""


def _ip_is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def assert_public_url(url: str) -> None:
    """Lanza SsrfBlocked si la URL no es http(s) pública. No hace requests."""
    if not url or not isinstance(url, str):
        raise SsrfBlocked("URL vacía o inválida")
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise SsrfBlocked(f"scheme no permitido: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise SsrfBlocked("URL sin host")
    # Resolver TODAS las IPs del host: si CUALQUIERA es privada, bloqueamos
    # (evita DNS rebinding parcial y hosts con A/AAAA mixtos).
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SsrfBlocked(f"no se pudo resolver el host {host!r}: {exc}") from exc
    resolved = {info[4][0] for info in infos}
    if not resolved:
        raise SsrfBlocked(f"host {host!r} sin IPs")
    for ip in resolved:
        if not _ip_is_public(ip):
            raise SsrfBlocked(f"host {host!r} resuelve a IP no pública: {ip}")


async def safe_get(
    url: str,
    *,
    timeout: httpx.Timeout,
    headers: dict[str, str] | None = None,
    max_redirects: int = _MAX_REDIRECTS,
) -> httpx.Response:
    """GET con protección SSRF, validando cada redirect. `follow_redirects=False`
    a propósito: seguimos a mano para validar cada `Location`."""
    current = url
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(max_redirects + 1):
            assert_public_url(current)
            resp = await client.get(current, headers=headers)
            if resp.is_redirect and resp.has_redirect_location:
                current = str(resp.next_request.url)  # type: ignore[union-attr]
                continue
            return resp
    raise SsrfBlocked(f"demasiados redirects (>{max_redirects}) para {url}")
