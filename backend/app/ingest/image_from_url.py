"""Sacar la foto del producto a partir de una URL cualquiera.

El b2box app manda una sola URL y puede ser tres cosas distintas:

  1. Una foto del cliente ya subida (link directo a .jpg/.png/.webp).
  2. Una publicación de MercadoLibre.
  3. Una ficha de Alibaba / AliExpress / 1688.

Este módulo las unifica: devuelve las URLs de imagen del producto y, si las
encuentra, el título. Estrategia por página, de más confiable a menos:

  a. JSON-LD `Product.image` — dato estructurado, es lo que el sitio declara
     como la foto del producto.
  b. `og:image` / `twitter:image` — casi universal y suele ser la foto principal.
  c. Regex sobre los CDNs conocidos de cada marketplace — red de emergencia para
     cuando la ficha se arma con JS y el HTML plano no trae los meta tags.

Todos los fetch pasan por `safe_get` (guard anti-SSRF): la URL viene de un
usuario final, no se puede confiar.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.ingest import browser_fetch, meli
from app.net_guard import SsrfBlocked, safe_get

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(15.0, connect=6.0)
# Tope del HTML que parseamos. Una ficha de ML pesa ~1-2 MB; 5 es holgado y
# evita quedarnos sin memoria con una página hostil.
_MAX_HTML_BYTES = 5 * 1024 * 1024
# 10 fotos por ficha. La galería completa entra y el match se queda con el mejor
# score entre todas: la foto principal suele ser la de marketing (packaging, con
# modelo) y la que se parece a la nuestra puede ser la sexta.
_MAX_IMAGES = 10

# Sin User-Agent de browser, ML y Alibaba devuelven 403 o una página vacía.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
}

_IMAGE_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif|bmp|avif)(\?|$)", re.I)

_MARKETPLACE_HOSTS = {
    "mercadolibre": ("mercadolibre.", "mercadolivre.", "mlstatic.com"),
    "alibaba": ("alibaba.com",),
    "aliexpress": ("aliexpress.", "ae01.alicdn.com"),
    "1688": ("1688.com", "alicdn.com"),
    "made-in-china": ("made-in-china.com",),
    "amazon": ("amazon.",),
}

# CDNs por marketplace, para el fallback por regex.
_CDN_PATTERNS = [
    re.compile(r"https?://http2\.mlstatic\.com/[\w\-./]+\.(?:jpe?g|png|webp)", re.I),
    re.compile(r"https?://[\w.\-]*alicdn\.com/[\w\-./%]+\.(?:jpe?g|png|webp)", re.I),
    re.compile(r"https?://[\w.\-]*alibaba\.com/[\w\-./%]+\.(?:jpe?g|png|webp)", re.I),
    re.compile(r"https?://image\.made-in-china\.com/[\w\-./%]+\.(?:jpe?g|png|webp)", re.I),
]


class ExtractError(RuntimeError):
    """No se pudo sacar ninguna imagen de la URL."""


class BlockedByCaptcha(ExtractError):
    """El sitio nos devolvió su página anti-bot en vez de la ficha.

    Es distinto de "no encontré imágenes": acá el problema no tiene arreglo
    del lado del parser. El sitio no le contesta a un servidor, y la única
    salida es que la foto la mande el cliente desde su dispositivo.
    """


@dataclass(slots=True)
class ExtractedProduct:
    image_urls: list[str] = field(default_factory=list)
    title: str = ""
    marketplace: str = "other"
    canonical_url: str = ""
    kind: str = "page"  # "image" (link directo) | "page" (ficha de producto)
    # Precio al que se vende en el marketplace de origen, si lo sabemos.
    # Hoy solo MercadoLibre, vía su API oficial.
    market_price_cents: int | None = None
    market_currency: str | None = None
    market_seller_count: int = 0
    # Cuando la ficha del link está bloqueada y hubo que resolverla buscando
    # su nombre en el catálogo del marketplace, el producto puede ser uno
    # parecido en vez del exacto. El llamador no debe afirmarlo.
    approximate: bool = False
    # Precio por URL de foto: con varios candidatos, el precio que vale es el
    # del que terminó matcheando contra nuestro catálogo, no el del primero.
    price_by_image: dict[str, tuple[int | None, str | None, int]] = field(
        default_factory=dict
    )


# ─── Helpers ───────────────────────────────────────────────────────


def detect_marketplace(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for name, needles in _MARKETPLACE_HOSTS.items():
        if any(n in host for n in needles):
            return name
    return "other"


def _obviously_internal(host: str) -> bool:
    """Filtro barato de hosts internos, SIN resolver DNS.

    El guard real anti-SSRF corre en `safe_get`/`_fetch` cuando efectivamente
    descargamos la imagen. Acá solo descartamos lo evidente: llamar a
    `assert_public_url` por cada candidato haría un `getaddrinfo` bloqueante
    dentro del event loop, por una URL que quizá ni usemos.
    """
    h = (host or "").lower()
    if h in {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}:
        return True
    if h.endswith(".local") or h.endswith(".internal"):
        return True
    try:
        return not ipaddress.ip_address(h).is_global
    except ValueError:
        return False  # no es IP literal: se valida al descargar


def _absolutize(candidate: str, base: str) -> str | None:
    """Normaliza una URL de imagen: protocol-relative, relativa o absoluta."""
    if not candidate:
        return None
    c = candidate.strip()
    if c.startswith("data:"):
        return None
    if c.startswith("//"):
        c = "https:" + c
    elif c.startswith("/") or not c.lower().startswith(("http://", "https://")):
        c = urljoin(base, c)
    parsed = urlparse(c)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return None
    if _obviously_internal(parsed.hostname):
        return None
    return c


def _dedupe(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _looks_like_image_url(url: str) -> bool:
    return bool(_IMAGE_EXT_RE.search(urlparse(url).path or ""))


# ─── Extractores de HTML ───────────────────────────────────────────


def _from_json_ld(soup: BeautifulSoup, base: str) -> tuple[list[str], str]:
    """Lee los bloques `application/ld+json` buscando un `Product`."""
    images: list[str] = []
    title = ""
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        # Un bloque puede ser un objeto, una lista, o traer @graph.
        blocks = data if isinstance(data, list) else [data]
        expanded: list[dict] = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            expanded.append(b)
            graph = b.get("@graph")
            if isinstance(graph, list):
                expanded.extend(x for x in graph if isinstance(x, dict))

        for block in expanded:
            types = block.get("@type")
            types = types if isinstance(types, list) else [types]
            if not any(str(t).lower() == "product" for t in types if t):
                continue
            if not title and isinstance(block.get("name"), str):
                title = block["name"].strip()
            img = block.get("image")
            candidates: list[str] = []
            if isinstance(img, str):
                candidates = [img]
            elif isinstance(img, list):
                candidates = [i for i in img if isinstance(i, str)]
            elif isinstance(img, dict):
                url = img.get("url") or img.get("contentUrl")
                if isinstance(url, str):
                    candidates = [url]
            for c in candidates:
                abs_url = _absolutize(c, base)
                if abs_url:
                    images.append(abs_url)
    return images, title


def _from_meta(soup: BeautifulSoup, base: str) -> tuple[list[str], str]:
    """og:image / twitter:image / link[rel=image_src] + título."""
    images: list[str] = []
    meta_keys = [
        ("property", "og:image:secure_url"),
        ("property", "og:image"),
        ("name", "og:image"),
        ("name", "twitter:image"),
        ("property", "twitter:image"),
        ("name", "twitter:image:src"),
    ]
    for attr, key in meta_keys:
        for tag in soup.find_all("meta", attrs={attr: key}):
            abs_url = _absolutize(tag.get("content", ""), base)
            if abs_url:
                images.append(abs_url)

    for tag in soup.find_all("link", attrs={"rel": "image_src"}):
        abs_url = _absolutize(tag.get("href", ""), base)
        if abs_url:
            images.append(abs_url)

    title = ""
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()
    return images, title


def _from_cdn_regex(html: str, base: str) -> list[str]:
    """Última red: buscar URLs de los CDNs conocidos en el HTML/JS crudo.

    Sirve para 1688 y AliExpress, que arman la galería por JS y a veces no
    dejan og:image útil en el HTML plano.
    """
    images: list[str] = []
    for pattern in _CDN_PATTERNS:
        for match in pattern.findall(html):
            abs_url = _absolutize(match, base)
            if abs_url:
                images.append(abs_url)
            if len(images) >= _MAX_IMAGES * 3:
                return images
    return images


# Marcadores de página anti-bot. MercadoLibre devuelve HTTP 200 con su
# interstitial de "tráfico sospechoso" cuando el request sale de una IP de
# datacenter (como la de Hugo): la página no trae ni og:image ni JSON-LD.
# Sin esta detección el error sería "no se encontró ninguna imagen", que manda
# a buscar el problema al lado equivocado.
_ANTIBOT_MARKERS = (
    "suspicious-traffic",
    "captcha",
    "unusual traffic",
    "tráfico inusual",
    "are you a robot",
)


def _looks_like_antibot(html: str) -> bool:
    head = html[:4000].lower()
    return any(m in head for m in _ANTIBOT_MARKERS)


def _drop_placeholders(urls: list[str]) -> list[str]:
    """Saca logos/sprites/íconos que no son la foto del producto."""
    bad = ("logo", "sprite", "placeholder", "icon", "favicon", "/ui/", "banner")
    return [u for u in urls if not any(b in u.lower() for b in bad)]


async def _via_browser(url: str, marketplace: str) -> ExtractedProduct | None:
    """Segundo intento: renderizar la ficha en Camoufox y leer las fotos del DOM.

    Devuelve None si el browser no está disponible o no sacó nada útil — el
    llamador sigue con el error que ya tenía. Nunca propaga: este camino es un
    extra, y que falle no puede empeorar la respuesta al app.
    """
    if not browser_fetch.available():
        return None
    try:
        page = await browser_fetch.render(url, max_images=_MAX_IMAGES * 3)
    except browser_fetch.SsrfBlocked:
        raise  # esto sí importa: la URL apunta a una red interna
    except Exception as exc:  # noqa: BLE001
        log.info("browser_fetch no pudo con %s: %s", url[:120], exc)
        return None

    final_url = page.final_url or url
    # El DOM renderizado también trae JSON-LD y og:image, y son más confiables
    # que los <img> sueltos: los probamos primero, igual que en el camino plano.
    images: list[str] = []
    title = ""
    if page.html:
        soup = BeautifulSoup(page.html, "lxml")
        ld_images, ld_title = _from_json_ld(soup, final_url)
        meta_images, meta_title = _from_meta(soup, final_url)
        images = ld_images + meta_images
        title = ld_title or meta_title or ""

    dom_images = [u for u in (_absolutize(i, final_url) or "" for i in page.image_urls) if u]
    images = _dedupe(_drop_placeholders(images + dom_images))
    if not images:
        return None

    log.info(
        "browser_fetch rescató %d fotos de %s (%s)", len(images), url[:120], marketplace
    )
    return ExtractedProduct(
        image_urls=images[:_MAX_IMAGES],
        title=(title or page.title or "")[:300],
        marketplace=marketplace,
        canonical_url=final_url,
        kind="page",
    )


# ─── API pública ───────────────────────────────────────────────────


async def extract(url: str) -> ExtractedProduct:
    """Devuelve las imágenes (y el título) del producto que hay en `url`.

    Lanza ExtractError si la URL no responde o no tiene ninguna imagen usable.
    """
    if not url or not url.strip():
        raise ExtractError("URL vacía")
    url = url.strip()
    marketplace = detect_marketplace(url)

    # MercadoLibre primero por la API oficial: su web no le contesta a un
    # servidor (ver meli.py). Si no hay credenciales o la API falla, seguimos
    # con el scraping — que para ML va a terminar en BlockedByCaptcha, pero el
    # camino queda igual para no tratar a ML distinto sin necesidad.
    if marketplace == "mercadolibre" and meli.enabled():
        candidates = await meli.fetch_from_url(url)
        if candidates:
            # Con un candidato es la ficha del link. Con varios, ML bloqueó el
            # link y los sacamos del catálogo por nombre: juntamos las fotos de
            # todos y que desempate el match de imagen contra nuestro catálogo.
            images: list[str] = []
            price_by_image: dict[str, tuple[int | None, str | None, int]] = {}
            per_candidate = max(1, _MAX_IMAGES // len(candidates))
            for candidate in candidates:
                for image in candidate.image_urls[:per_candidate]:
                    if image in price_by_image:
                        continue
                    images.append(image)
                    price_by_image[image] = (
                        candidate.price_cents,
                        candidate.currency,
                        candidate.seller_count,
                    )
            first = candidates[0]
            # Con la ficha del link, su título es el bueno. Cuando vino del
            # catálogo, el título del candidato es el de OTRO producto: usarlo
            # haría que la corroboración por nombre compare contra un nombre que
            # ya está desviado, y le daría la razón al candidato equivocado. El
            # único nombre que describe lo que pidió el cliente es el que viaja
            # en su propia URL.
            approximate = first.resolved_by == "catalog"
            title = meli.name_from_url(url) if approximate else first.title
            return ExtractedProduct(
                image_urls=images[:_MAX_IMAGES],
                title=title[:300],
                marketplace=marketplace,
                canonical_url=first.permalink or url,
                kind="page",
                market_price_cents=first.price_cents,
                market_currency=first.currency,
                market_seller_count=first.seller_count,
                approximate=approximate,
                price_by_image=price_by_image,
            )

    try:
        resp = await safe_get(url, timeout=_TIMEOUT, headers=_BROWSER_HEADERS)
    except SsrfBlocked as exc:
        raise ExtractError(f"URL bloqueada: {exc}") from exc
    except httpx.HTTPError as exc:
        # El sitio no le contesta a httpx: puede ser que sí le conteste a un
        # browser de verdad (TLS fingerprint, JS challenge).
        rendered = await _via_browser(url, marketplace)
        if rendered is not None:
            return rendered
        raise ExtractError(f"No se pudo abrir la URL: {type(exc).__name__}") from exc

    final_url = str(resp.url)
    if resp.status_code >= 400:
        rendered = await _via_browser(url, marketplace)
        if rendered is not None:
            return rendered
        raise ExtractError(f"La URL respondió HTTP {resp.status_code}")

    content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()

    # ── Caso 1: link directo a una imagen (foto del cliente) ──────
    if content_type.startswith("image/") or (not content_type and _looks_like_image_url(final_url)):
        return ExtractedProduct(
            image_urls=[final_url],
            title="",
            marketplace=marketplace if marketplace != "other" else "upload",
            canonical_url=final_url,
            kind="image",
        )

    # ── Caso 2: página de producto ────────────────────────────────
    raw = resp.content[:_MAX_HTML_BYTES]
    try:
        html = raw.decode(resp.encoding or "utf-8", errors="replace")
    except (LookupError, UnicodeDecodeError):
        html = raw.decode("utf-8", errors="replace")

    soup = BeautifulSoup(html, "lxml")

    ld_images, ld_title = _from_json_ld(soup, final_url)
    meta_images, meta_title = _from_meta(soup, final_url)
    images = _dedupe(_drop_placeholders(ld_images + meta_images))

    if not images:
        images = _dedupe(_drop_placeholders(_from_cdn_regex(html, final_url)))

    if not images:
        # Los dos motivos por los que llegamos acá —página anti-bot y galería
        # armada por JS— son exactamente los que un browser real resuelve.
        rendered = await _via_browser(url, marketplace)
        if rendered is not None:
            return rendered

        if _looks_like_antibot(html):
            if marketplace == "mercadolibre" and not meli.enabled():
                extra = (
                    " Configurá MELI_CLIENT_ID / MELI_CLIENT_SECRET para leerlo por la "
                    "API oficial de MercadoLibre."
                )
            elif not browser_fetch.available():
                extra = (
                    " Activá BROWSER_FETCH_ENABLED para reintentar con un browser real, "
                    "o que el app mande la foto en `image_url`."
                )
            else:
                extra = " La foto tiene que mandarla el app en `image_url`."
            raise BlockedByCaptcha(
                f"{marketplace} bloqueó el pedido desde el servidor (página anti-bot)."
                + extra
            )
        raise ExtractError(
            "No se encontró ninguna imagen de producto en la página "
            f"({marketplace}). Puede que la ficha se arme por JavaScript."
        )

    return ExtractedProduct(
        image_urls=images[:_MAX_IMAGES],
        title=(ld_title or meta_title or "")[:300],
        marketplace=marketplace,
        canonical_url=final_url,
        kind="page",
    )
