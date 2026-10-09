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

import asyncio
import ipaddress
import logging
import socket
import zlib
from collections.abc import Callable
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

_ALLOWED_SCHEMES = {"http", "https"}
_MAX_REDIRECTS = 5


class SsrfBlocked(ValueError):
    """La URL apunta (directa o vía redirect/DNS) a una red no pública."""


class ResponseTooLarge(ValueError):
    """El cuerpo de la respuesta (ya descomprimido) pasó el tope `max_bytes`."""


class BadEncoding(ValueError):
    """`Content-Encoding` que no se acepta (varias capas, br, zstd…) o gzip roto."""


class RedirectBlocked(ValueError):
    """Un redirect apunta a una URL que el llamador no acepta (otro sitio, vedada por robots)."""


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


# Con `max_bytes` solo se aceptan cuerpos sin comprimir o con UNA capa de gzip, que se abre acá con tope.
# `Content-Encoding: gzip, gzip, gzip` (o br, zstd, deflate) se rechaza: httpx descomprime cada capa
# entera antes de dar el chunk, y 250 bytes en el cable pueden volverse cientos de MB en memoria.
_ACCEPT_ENCODING = "gzip"
_ENCODINGS_OK = frozenset({"", "identity", "gzip", "x-gzip"})


def _with_accept_encoding(headers: dict[str, str] | None) -> dict[str, str]:
    kept = {k: v for k, v in (headers or {}).items() if k.lower() != "accept-encoding"}
    kept["Accept-Encoding"] = _ACCEPT_ENCODING
    return kept


_DROPPED_HEADERS = (b"content-encoding", b"content-length", b"transfer-encoding")
_GZIP_MAGIC = b"\x1f\x8b"


def _decoded_response(resp: httpx.Response, body: bytes) -> httpx.Response:
    """Una Response normal con el cuerpo ya leído. Los headers se copian CRUDOS (bytes): reconstruirlos
    desde strings los re-codifica en ASCII y un `Content-Disposition` con acento, un ETag con 0xFF o un
    header en latin-1 reventaban con UnicodeEncodeError. Se sacan los que describen el cuerpo del cable,
    que ya no vale (sin esto httpx intentaría descomprimirlo otra vez al leer `.content`)."""
    kept = [(k, v) for k, v in resp.headers.raw if k.lower() not in _DROPPED_HEADERS]
    return httpx.Response(resp.status_code, headers=kept, content=body, request=resp.request,
                          extensions={"http_version": resp.extensions.get("http_version", b"HTTP/1.1")})


async def _read_capped(client: httpx.AsyncClient, url: str, headers: dict[str, str] | None,
                       max_bytes: int) -> httpx.Response:
    """GET en streaming que corta apenas el cuerpo DESCOMPRIMIDO pasa `max_bytes`. Devuelve una
    Response normal con el cuerpo ya leído; si no cabe lanza ResponseTooLarge sin haberlo juntado
    entero, y si viene con una codificación que no se acepta (o un gzip roto, de varios miembros o
    incompleto), BadEncoding."""
    async with client.stream("GET", url, headers=_with_accept_encoding(headers)) as resp:
        # Ya conectado, todavía sin leer el body: ¿a quién nos conectamos de verdad?
        # (assert_public_url resuelve el DNS por su cuenta y httpx vuelve a resolver.)
        assert_peer_public(resp)
        if resp.is_redirect:
            redirect = httpx.Response(resp.status_code, headers=resp.headers, request=resp.request)
            redirect.next_request = resp.next_request
            return redirect
        encoding = (resp.headers.get("content-encoding") or "").strip().lower()
        if encoding not in _ENCODINGS_OK:
            raise BadEncoding(f"content-encoding no aceptado: {encoding[:40]}")
        gzipped = encoding in ("gzip", "x-gzip")
        declared = resp.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            # Comprimido o no, nada que pese más que el tope tiene sentido en el cable.
            raise ResponseTooLarge(f"el servidor declara {declared} bytes (tope {max_bytes})")
        if resp.is_stream_consumed:
            # Un transporte que entrega el cuerpo ya leído (los de prueba, `httpx.MockTransport`): no hay
            # nada que cortar en streaming, solo el tope. Los transportes reales nunca llegan acá.
            body = resp.content
            if len(body) > max_bytes:
                raise ResponseTooLarge(f"el cuerpo pasó el tope de {max_bytes} bytes")
            return _decoded_response(resp, body)
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS) if gzipped else None
        buf = bytearray()
        raw = 0
        after_eof = b""            # lo que llega después de que terminó el primer miembro gzip
        try:
            async for chunk in resp.aiter_raw():
                raw += len(chunk)
                if raw > max_bytes:
                    raise ResponseTooLarge(f"el cuerpo pasó el tope de {max_bytes} bytes")
                if inflater is None:
                    buf += chunk
                else:
                    if inflater.eof:
                        after_eof = (after_eof + chunk)[:2]           # llegó algo después del último miembro
                        continue
                    pending = chunk
                    while pending and not inflater.eof:
                        # max_length=0 sería "sin tope": siempre queda lugar para al menos 1 byte.
                        buf += inflater.decompress(pending, max_bytes + 1 - len(buf))
                        if len(buf) > max_bytes:
                            raise ResponseTooLarge(f"el cuerpo descomprimido pasó el tope de {max_bytes} bytes")
                        pending = inflater.unconsumed_tail
                if len(buf) > max_bytes:
                    raise ResponseTooLarge(f"el cuerpo pasó el tope de {max_bytes} bytes")
        except zlib.error as exc:
            # Cuerpo corrupto o que dice ser gzip y no lo es: es un error de la tienda, no una excepción suelta.
            raise BadEncoding(f"gzip inválido: {str(exc)[:60]}") from exc
        if inflater is not None:
            if raw and not inflater.eof:
                raise BadEncoding("gzip incompleto")
            if (inflater.unused_data + after_eof)[:2] == _GZIP_MAGIC:
                # Varios miembros pegados: solo se abre el primero y el resto se perdería en silencio.
                raise BadEncoding("gzip de varios miembros")
        return _decoded_response(resp, bytes(buf))


async def safe_get(
    url: str,
    *,
    timeout: httpx.Timeout,
    headers: dict[str, str] | None = None,
    max_redirects: int = _MAX_REDIRECTS,
    max_bytes: int | None = None,
    redirect_ok: Callable[[str], bool] | None = None,
) -> httpx.Response:
    """GET con protección SSRF, validando cada redirect. `follow_redirects=False`
    a propósito: seguimos a mano para validar cada `Location`.

    `max_bytes`: tope del cuerpo ya descomprimido. Si se pasa, la respuesta se lee en streaming,
    solo se acepta sin comprimir o con UNA capa de gzip, y lanza ResponseTooLarge / BadEncoding sin
    bajar el resto. Sin él se lee entera (como siempre).

    `redirect_ok(url)`: cada `Location` se le consulta ANTES de seguirla; si devuelve False se
    lanza RedirectBlocked y no se pide nada (así un redirect no saca al bot del sitio ni lo mete
    en una URL que robots.txt veda)."""
    current = url
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(max_redirects + 1):
            # getaddrinfo es bloqueante: un DNS lento no puede congelar el event loop.
            await asyncio.to_thread(assert_public_url, current)
            if max_bytes is None:
                resp = await client.get(current, headers=headers)
            else:
                resp = await _read_capped(client, current, headers, max_bytes)
            if resp.is_redirect and resp.has_redirect_location:
                current = str(resp.next_request.url)  # type: ignore[union-attr]
                if redirect_ok is not None and not redirect_ok(current):
                    raise RedirectBlocked(f"redirect a una URL no aceptada: {current[:120]}")
                continue
            return resp
    raise SsrfBlocked(f"demasiados redirects (>{max_redirects}) para {url}")
