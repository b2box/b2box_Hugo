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


class ResponseTooLarge(ValueError):
    """El cuerpo de la respuesta (ya descomprimido) pasó el tope `max_bytes`."""


# NAT64 (RFC 6052): un gateway NAT64 traduce 64:ff9b::a.b.c.d a la IPv4
# a.b.c.d, incluidas las privadas. Hugo no corre detrás de uno: se bloquea todo.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _ip_is_public(ip: str) -> bool:
    """True solo para una IP de internet pública.

    `is_global` deja afuera el rango compartido 100.64.0.0/10 (CGNAT, también
    la metadata de Alibaba en 100.100.100.200), pero NO el multicast ni el
    site-local fec0::/10, y una IPv6 "mapeada" (::ffff:10.0.0.1) hay que
    juzgarla por la IPv4 que lleva adentro, no como IPv6.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return _ip_is_public(str(addr.ipv4_mapped))
        if addr in _NAT64 or addr.is_site_local:
            return False
        if addr.sixtofour is not None and not _ip_is_public(str(addr.sixtofour)):
            return False
    return addr.is_global and not (
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


def assert_peer_public(resp: httpx.Response) -> None:
    """Lanza SsrfBlocked si el servidor que contestó NO es una IP pública.

    Para respuestas en streaming: se llama con los headers ya recibidos y
    ANTES de leer el body. `assert_public_url` resuelve el DNS por su cuenta y
    httpx vuelve a resolver al conectar (DNS rebinding): esta es la IP a la
    que quedó conectado el socket de verdad. Fail-closed: si el transporte no
    la expone (`network_stream`), se rechaza.
    """
    stream = resp.extensions.get("network_stream")
    addr = stream.get_extra_info("server_addr") if stream is not None else None
    host = addr[0] if isinstance(addr, (tuple, list)) and addr else None
    if not isinstance(host, str):
        raise SsrfBlocked("no se pudo verificar la IP del servidor")
    if not _ip_is_public(host):
        raise SsrfBlocked(f"el servidor contestó desde una IP no pública: {host}")


async def _read_capped(client: httpx.AsyncClient, url: str, headers: dict[str, str] | None,
                       max_bytes: int) -> httpx.Response:
    """GET en streaming que corta apenas el cuerpo DESCOMPRIMIDO pasa `max_bytes`
    (también frena un gzip bomba). Devuelve una Response normal con el cuerpo ya
    leído; si no cabe lanza ResponseTooLarge sin haberlo juntado entero."""
    async with client.stream("GET", url, headers=headers) as resp:
        # Ya conectado, todavía sin leer el body: ¿a quién nos conectamos de verdad?
        # (assert_public_url resuelve el DNS por su cuenta y httpx vuelve a resolver.)
        assert_peer_public(resp)
        if resp.is_redirect:
            redirect = httpx.Response(resp.status_code, headers=resp.headers, request=resp.request)
            redirect.next_request = resp.next_request
            return redirect
        declared = resp.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes and not resp.headers.get("content-encoding"):
            raise ResponseTooLarge(f"el servidor declara {declared} bytes (tope {max_bytes})")
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf += chunk
            if len(buf) > max_bytes:
                raise ResponseTooLarge(f"el cuerpo pasó el tope de {max_bytes} bytes")
        # El cuerpo ya viene decodificado: sin estos headers httpx intentaría
        # descomprimirlo otra vez al leer `.content`.
        kept = [(k, v) for k, v in resp.headers.multi_items()
                if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
        return httpx.Response(resp.status_code, headers=kept, content=bytes(buf),
                              request=resp.request, extensions={"http_version": resp.http_version})


async def safe_get(
    url: str,
    *,
    timeout: httpx.Timeout,
    headers: dict[str, str] | None = None,
    max_redirects: int = _MAX_REDIRECTS,
    max_bytes: int | None = None,
) -> httpx.Response:
    """GET con protección SSRF, validando cada redirect. `follow_redirects=False`
    a propósito: seguimos a mano para validar cada `Location`.

    `max_bytes`: tope del cuerpo ya descomprimido. Si se pasa, la respuesta se
    lee en streaming y lanza ResponseTooLarge sin bajar el resto. Sin él se lee
    entera (como siempre)."""
    current = url
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(max_redirects + 1):
            assert_public_url(current)
            if max_bytes is None:
                resp = await client.get(current, headers=headers)
            else:
                resp = await _read_capped(client, current, headers, max_bytes)
            if resp.is_redirect and resp.has_redirect_location:
                current = str(resp.next_request.url)  # type: ignore[union-attr]
                continue
            return resp
    raise SsrfBlocked(f"demasiados redirects (>{max_redirects}) para {url}")
