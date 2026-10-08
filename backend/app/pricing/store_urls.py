"""Saneado de lo que viene de las tiendas: links, fotos y hosts permitidos.

Los títulos, links y fotos de una tienda son texto de terceros: terminan en un
`href` del dashboard, en una descarga de CLIP y en el prompt del juez. Se acepta
solo lo que tiene la forma esperada, igual que `market_ml.safe_permalink` /
`safe_image_url` hacen con Mercado Libre:

  * links: https, host de la tienda (o un subdominio), sin usuario ni puerto raro;
  * fotos: https, host de la tienda o de su CDN (lista por tienda: Tiendanube
    usa `*.mitiendanube.com`; Gadnic sirve las fotos desde `bidcom.com.ar`).
    Una URL tipo "resizer" (`…/resize?src=<otra URL>`) solo pasa si la URL de
    adentro también es https a un host permitido.

Además guarda la lista de hosts de foto de las tiendas ACTIVAS para que
`judge_images.allowed_url` (que baja las fotos que viajan al juez en base64)
las acepte. Vacía = ninguna foto de tienda pasa (falla cerrado).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from app.pricing.market_ml import _clean_https_parts, _host_in

# Tiendanube: las fotos viven en acdn-us.mitiendanube.com (y hermanos).
TIENDANUBE_IMAGE_DOMAIN = "mitiendanube.com"

# Dominios demasiado amplios: permitirlos abriría las fotos a medio internet.
_TOO_BROAD = frozenset({
    "com", "net", "org", "ar", "com.ar", "net.ar", "org.ar", "gob.ar", "gov.ar", "edu.ar",
    "co", "io", "app", "dev", "pro", "info", "biz", "me", "co.uk", "com.br", "com.mx",
    "cloudfront.net", "amazonaws.com", "googleusercontent.com", "github.io", "herokuapp.com",
})
_HOST_RE = re.compile(r"^(?=.{4,100}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")


def host_of(url: object) -> str:
    """Host en minúsculas de una URL https, o ""."""
    parts = _clean_https_parts(url)
    return parts.hostname.lower() if parts is not None and parts.hostname else ""


def apex(host: str) -> str:
    """`www.casaperfecta.com.ar` → `casaperfecta.com.ar` (solo se saca el `www.`)."""
    host = (host or "").lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def parse_hosts(raw: str | None) -> tuple[str, ...]:
    """CSV de dominios → tupla de dominios válidos, sin repetir y en orden. Lo que no
    sea un dominio (o sea demasiado amplio) se descarta."""
    out: list[str] = []
    for piece in re.split(r"[,\s;]+", (raw or "").lower()):
        piece = piece.strip().lstrip("*.").strip(".")
        if _HOST_RE.match(piece) and piece not in _TOO_BROAD and piece not in out:
            out.append(piece)
    return tuple(out)


def invalid_hosts(raw: str | None) -> list[str]:
    """Los elementos de un CSV de dominios que NO son aceptables (para avisar al guardar)."""
    bad: list[str] = []
    for piece in re.split(r"[,\s;]+", (raw or "").lower()):
        piece = piece.strip().lstrip("*.").strip(".")
        if piece and not (_HOST_RE.match(piece) and piece not in _TOO_BROAD):
            bad.append(piece[:60])
    return bad


def default_image_hosts(platform: str, base_url: str) -> tuple[str, ...]:
    base = apex(host_of(base_url))
    hosts = [base] if base else []
    if platform == "tiendanube":
        hosts.append(TIENDANUBE_IMAGE_DOMAIN)
    return tuple(dict.fromkeys(hosts))


def safe_link(url: object, base_url: str) -> str:
    """Link apto para un href: https y el host de la tienda (o un subdominio). Si no, ""."""
    parts = _clean_https_parts(url)
    domain = apex(host_of(base_url))
    if parts is None or not domain or not _host_in(parts.hostname.lower(), domain):
        return ""
    return parts._replace(fragment="").geturl()


def _in_hosts(host: str, hosts: tuple[str, ...]) -> bool:
    return any(_host_in(host, h) for h in hosts)


def safe_image(url: object, hosts: tuple[str, ...]) -> str | None:
    """Foto apta para descargar (CLIP) o mandar al juez, o None.

    Acepta `//host/…` (protocolo relativo, lo usa Tiendanube) y sube `http://` a
    https. El host tiene que estar en `hosts`."""
    if isinstance(url, str) and url.strip().startswith("//"):
        url = "https:" + url.strip()
    parts = _clean_https_parts(url, allow_http=True)
    if parts is None or not _in_hosts(parts.hostname.lower(), hosts):
        return None
    # Resizer: la URL de adentro también tiene que ser de un host permitido.
    for key in ("src", "url"):
        for inner in parse_qs(parts.query).get(key, []):
            if "://" in inner or inner.startswith("//"):
                nested = _clean_https_parts("https:" + inner if inner.startswith("//") else inner, allow_http=True)
                if nested is None or not _in_hosts(nested.hostname.lower(), hosts):
                    return None
    return parts._replace(scheme="https", fragment="").geturl()


# ─── Hosts de foto de las tiendas activas (para judge_images) ───────────────

_allowed_image_hosts: frozenset[str] = frozenset()


def set_allowed_image_hosts(hosts: "set[str] | frozenset[str] | list[str]") -> None:
    """Reemplaza la lista de hosts de foto permitidos (la carga el semáforo al
    empezar y la API de tiendas al guardar)."""
    global _allowed_image_hosts
    _allowed_image_hosts = frozenset(h for h in hosts if h)


def allowed_image_hosts() -> frozenset[str]:
    return _allowed_image_hosts


def allowed_store_image(url: object) -> str | None:
    """Para `judge_images.allowed_url`: foto de una tienda activa, o None."""
    return safe_image(url, tuple(_allowed_image_hosts)) if _allowed_image_hosts else None
