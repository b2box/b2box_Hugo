"""Saneado de lo que viene de las tiendas: links, fotos y hosts permitidos.

Los títulos, links y fotos de una tienda son texto de terceros: terminan en un
`href` del dashboard, en una descarga de CLIP y en el prompt del juez. Se acepta
solo lo que tiene la forma esperada, igual que `market_ml.safe_permalink` /
`safe_image_url` hacen con Mercado Libre:

  * links: https, el host de la tienda (o su `www.`), sin usuario ni puerto raro;
  * fotos: https, host de la tienda o de su CDN. Cada tienda tiene una lista de hosts de foto y
    cada entrada es una de tres formas: `host` (ese host exacto o su `www.`), `*.dominio` (el
    dominio y todos sus subdominios) o `acdn*.dominio` (un patrón sobre el host). Tiendanube sirve
    las fotos desde `acdn*.mitiendanube.com`, no desde todo `mitiendanube.com` (ahí vive cualquier
    tienda). Una URL tipo "resizer" (`…/resize?src=<otra URL>`) solo pasa si la URL de adentro
    también es https a un host permitido.

Quien carga una tienda desde el dashboard no puede sumar CUALQUIER dominio de fotos: solo los de la
tienda misma, los de su plataforma y los que un administrador dejó en `STORE_TRUSTED_IMAGE_HOSTS`
(ver `extra_host_allowed`). Sin eso, cargar `s3.amazonaws.com` abría el juez a medio internet.

Además guarda la lista de hosts de foto de las tiendas ACTIVAS para que
`judge_images.allowed_url` (que baja las fotos que viajan al juez en base64)
las acepte. Vacía = ninguna foto de tienda pasa (falla cerrado).
"""

from __future__ import annotations

import fnmatch
import re
from urllib.parse import parse_qs

from app.pricing.market_ml import _clean_https_parts

# Tiendanube: las fotos viven en acdn-us.mitiendanube.com (y hermanos acdn*).
TIENDANUBE_IMAGE_PATTERN = "acdn*.mitiendanube.com"
_PLATFORM_IMAGE_HOSTS = {"tiendanube": (TIENDANUBE_IMAGE_PATTERN,)}

# Dominios demasiado amplios (sufijos públicos, nubes y plataformas multi-inquilino): permitirlos
# abriría las fotos, o el sitemap, a medio internet.
_TOO_BROAD = frozenset({
    "com", "net", "org", "ar", "com.ar", "net.ar", "org.ar", "gob.ar", "gov.ar", "edu.ar", "tur.ar", "mil.ar",
    "co", "io", "app", "dev", "pro", "info", "biz", "me", "co.uk", "com.br", "com.mx", "com.co", "com.cl",
    "cloudfront.net", "amazonaws.com", "s3.amazonaws.com", "googleusercontent.com", "googleapis.com",
    "storage.googleapis.com", "github.io", "githubusercontent.com", "raw.githubusercontent.com", "github.com",
    "gitlab.io", "herokuapp.com", "blob.core.windows.net", "azureedge.net", "r2.dev", "pages.dev", "workers.dev",
    "vercel.app", "netlify.app", "firebaseapp.com", "web.app", "appspot.com", "imgur.com", "wordpress.com",
    "wp.com", "blogspot.com", "wixsite.com", "myshopify.com", "mitiendanube.com", "tiendanube.com",
    "mercadolibre.com", "mercadolibre.com.ar", "mlstatic.com", "cdn.shopify.com", "shopify.com",
})
_HOST_RE = re.compile(r"^(?=.{4,100}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
_GLOB_HEAD_RE = re.compile(r"^[a-z0-9-]{3,30}\*[a-z0-9-]{0,30}$")
MAX_HOST_LEN = 100
# Lo más largo que se acepta de un link o una foto (columnas VARCHAR(500)).
MAX_URL_LEN = 500


# Segundas etiquetas genéricas que, delante de un TLD de país de dos letras, forman un SUFIJO PÚBLICO donde
# cualquiera registra su dominio: com.uy, com.pe, co.nz, org.uk, gob.ar, co.jp, ne.jp… No hay una lista
# oficial en el lock (la Public Suffix List completa no es una dependencia nuestra), así que la regla es
# estructural: «<genérica>.<país de 2 letras>». Alcanza para los países donde Hugo puede tener tiendas.
_GENERIC_SLD = frozenset({
    "com", "co", "net", "org", "gob", "gov", "gub", "edu", "mil", "ac", "go", "or", "ne", "nom", "sch", "int", "ltd",
    "plc", "govt", "info", "biz", "tur", "blog", "mus", "ind", "adm", "adv", "arq", "bio", "eng", "fin", "jus", "leg",
    "med", "psi", "tec", "coop", "gen", "iwi", "mod", "nhs", "k12", "pro", "me", "gouv", "gv", "lg", "ed", "id",
})


def is_public_suffix(domain: str) -> bool:
    """¿Es un sufijo público (un TLD, o «com.uy» / «co.nz»), o sea no el dominio de UNA tienda?"""
    labels = (domain or "").lower().strip(".").split(".")
    if len(labels) == 1:
        return True
    return len(labels) == 2 and len(labels[1]) == 2 and labels[0] in _GENERIC_SLD


def valid_hostname(host: str) -> bool:
    """¿Es un nombre de dominio común (con TLD de letras, hasta 100 caracteres) y no un sufijo
    público ni una plataforma multi-inquilino? Descarta IPs, `localhost` y nombres de una etiqueta."""
    host = host or ""
    return bool(_HOST_RE.match(host)) and host not in _TOO_BROAD and not is_public_suffix(host)


def host_of(url: object) -> str:
    """Host en minúsculas de una URL https, o ""."""
    parts = _clean_https_parts(url)
    return parts.hostname.lower() if parts is not None and parts.hostname else ""


def apex(host: str) -> str:
    """`www.casaperfecta.com.ar` → `casaperfecta.com.ar` (solo se saca el `www.`)."""
    host = (host or "").lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def _entry_ok(entry: str) -> bool:
    """¿Es una entrada válida de la lista de hosts de foto?"""
    if entry in {p for ps in _PLATFORM_IMAGE_HOSTS.values() for p in ps}:
        return True
    if "*" not in entry:
        return valid_hostname(entry)
    head, _, tail = entry.partition(".")
    if "*" in tail or not tail:
        return False
    if head == "*":
        return valid_hostname(tail)                     # *.dominio
    return bool(_GLOB_HEAD_RE.match(head)) and valid_hostname(tail)


def _pieces(raw: str | None) -> list[str]:
    out = []
    for piece in re.split(r"[,\s;]+", (raw or "").lower()):
        piece = piece.strip().strip(".")
        if piece:
            out.append(piece)
    return out


def parse_hosts(raw: str | None) -> tuple[str, ...]:
    """CSV de entradas de hosts → tupla de entradas válidas, sin repetir y en orden. Lo que no
    sea válido (o sea demasiado amplio) se descarta."""
    return tuple(dict.fromkeys(p for p in _pieces(raw) if _entry_ok(p)))


def invalid_hosts(raw: str | None) -> list[str]:
    """Los elementos de un CSV de hosts que NO son aceptables (para avisar al guardar)."""
    return [p[:60] for p in _pieces(raw) if not _entry_ok(p)]


def host_matches(host: str, entry: str) -> bool:
    """¿Este host cae bajo la entrada? `host` (o su www.), `*.dominio` o un patrón con `*`."""
    host = (host or "").lower()
    if entry.startswith("*."):
        domain = entry[2:]
        return host == domain or host.endswith("." + domain)
    if "*" in entry:
        # El comodín vive en la primera etiqueta y NO cruza puntos: `acdn*.mitiendanube.com` es
        # `acdn-us.mitiendanube.com`, no `acdn.cualquier-cosa.mitiendanube.com`.
        pattern_first, _, pattern_rest = entry.partition(".")
        first, _, rest = host.partition(".")
        return rest == pattern_rest and fnmatch.fnmatchcase(first, pattern_first)
    return host == entry or host == "www." + entry or entry == "www." + host


def default_image_hosts(platform: str, base_url: str) -> tuple[str, ...]:
    base = apex(host_of(base_url))
    hosts = [base] if base else []
    hosts.extend(_PLATFORM_IMAGE_HOSTS.get(platform, ()))
    return tuple(dict.fromkeys(hosts))


def extra_host_allowed(entry: str, base_url: str, platform: str, trusted_csv: str | None) -> bool:
    """¿Puede quien carga la tienda desde el dashboard sumar esta entrada a sus hosts de foto?
    Sí si es de la tienda misma (su dominio o un subdominio), de su plataforma, o si un
    administrador la dejó en la lista de confianza (`STORE_TRUSTED_IMAGE_HOSTS`)."""
    entry = entry.strip().lower()
    if not _entry_ok(entry):
        return False
    own = apex(host_of(base_url))
    domain = entry[2:] if entry.startswith("*.") else entry
    if own and "*" not in domain and (domain == own or domain.endswith("." + own)):
        return True
    return entry in _PLATFORM_IMAGE_HOSTS.get(platform, ()) or entry in parse_hosts(trusted_csv)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def like_patterns(entry: str) -> list[str]:
    """Patrones SQL LIKE (con `escape="\\"`) de las URLs https que caen bajo una entrada de hosts.
    Los `_` y `%` literales se escapan: no se vuelven comodines."""
    entry = entry.lower()
    if entry.startswith("*."):
        domain = _escape_like(entry[2:])
        return [f"https://{domain}/%", f"https://%.{domain}/%"]
    if "*" in entry:
        return ["https://" + "%".join(_escape_like(p) for p in entry.split("*")) + "/%"]
    host = _escape_like(entry[4:] if entry.startswith("www.") else entry)
    return [f"https://{host}/%", f"https://www.{host}/%"]


def safe_link(url: object, base_url: str) -> str:
    """Link apto para un href: https y el host de la tienda (o su www.). Si no, ""."""
    parts = _clean_https_parts(url)
    own = apex(host_of(base_url))
    if parts is None or not own or not host_matches(parts.hostname.lower(), own):
        return ""
    link = parts._replace(fragment="").geturl()
    return link if len(link) <= MAX_URL_LEN else ""


def _in_hosts(host: str, hosts: tuple[str, ...]) -> bool:
    return any(host_matches(host, h) for h in hosts)


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
    link = parts._replace(scheme="https", fragment="").geturl()
    return link if len(link) <= MAX_URL_LEN else None      # cabe en VARCHAR(500): una más larga no sirve


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
