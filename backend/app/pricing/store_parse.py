"""Lectura de las páginas de una tienda: sitemap y ficha de producto.

Todo es texto de terceros y puede venir roto: acá solo se EXTRAE, con topes de
tamaño y tolerando JSON malo. Sin red (recibe el texto). Dos formatos:

  * Tiendanube (`platform = "tiendanube"`, ej. Casa Perfecta). La página trae VARIOS
    bloques JSON-LD: el del producto (adentro de un `WebPage.mainEntity`) y uno por
    cada producto relacionado del carrusel, que ni siquiera es el de esta página.
    Se elige el que coincide con la URL. El precio que vale es el de
    `data-variants` del bloque `#single-product` (`price_number`): el `price` del
    JSON-LD muchas veces es el precio TACHADO (`compare_at_price_number`) y, por
    eso, no se usa solo. El peso del JSON-LD es un default (0,111 kg): se ignora.

  * JSON-LD + sitemap (`platform = "jsonld_sitemap"`, ej. Gadnic, un Next.js). Una
    ficha `Product` con nombre, precio ARS, sku, marca y foto. Si la página
    también trae el estado de Next.js con `finalPrice` (lo que se ve en pantalla)
    se contrasta contra el JSON-LD; si no coinciden se usa el visible y el precio
    queda marcado como dudoso. Un precio absurdamente bajo (< ARS 1.000) también:
    en Gadnic hay fichas con un precio viejo (un mini teclado a ARS 249).

`price_doubtful` no frena nada: el precio se guarda y se muestra con el aviso, y
no cuenta para "más barato afuera" ni para el color.
"""

from __future__ import annotations

import gzip
import html as html_lib
import json
import logging
import re
import zlib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

PLATFORM_TIENDANUBE = "tiendanube"
PLATFORM_JSONLD = "jsonld_sitemap"
PLATFORMS = (PLATFORM_TIENDANUBE, PLATFORM_JSONLD)

# Topes (la página de Gadnic pesa ~0,7 MB y su sitemap de productos ~4 MB).
MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_SITEMAP_BYTES = 25 * 1024 * 1024
MAX_SITEMAP_URLS = 100_000
_MAX_LD_BLOCKS = 60
_MAX_LD_BLOCK_CHARS = 400_000
_MAX_URL_LEN = 500
TITLE_MAX = 300
# Nada de lo que vende una tienda razonable cuesta menos que esto, en pesos de hoy.
MIN_PLAUSIBLE_PRICE_CENTS = 100_000          # ARS 1.000
MAX_PLAUSIBLE_PRICE_CENTS = 100_000_000_000  # ARS 1.000 millones: más es basura
_PRICE_TOLERANCE = 0.005


@dataclass(slots=True)
class ParsedItem:
    title: str
    price_cents: int | None = None
    price_doubtful: bool = False
    price_note: str = ""
    sku: str = ""
    brand: str = ""
    stock: int | None = None
    # Candidatas a foto, en orden de preferencia (sin sanear: lo hace el indexador).
    image_urls: list[str] = field(default_factory=list)


# ─── Utilidades ──────────────────────────────────────────────────────────────

_CTRL = re.compile(r"[\x00-\x1f\x7f  ]+")


def one_line(value: object, limit: int) -> str:
    """Texto de tercero en UNA línea y acotado (títulos, marcas): sin saltos ni
    caracteres de control, que podrían partir un prompt o una celda."""
    if not isinstance(value, str):
        return ""
    return " ".join(_CTRL.sub(" ", value).split())[:limit]


def parse_price_cents(value: object) -> int | None:
    """"11000", 11000, "3804.24", "$ 1.234,56" → centavos. None si no es un precio
    positivo y razonable."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = Decimal(str(value))
    elif isinstance(value, str):
        # "ARS 1.234,56" / "$ 11.000" sí; "1e999" o "doce mil" no (no se adivina).
        if re.search(r"[a-z]", re.sub(r"ars|\$", "", value.lower())):
            return None
        raw = re.sub(r"[^\d.,]", "", value)
        if not raw:
            return None
        if "," in raw and "." in raw:
            # El último separador es el decimal.
            decimal_sep = "," if raw.rfind(",") > raw.rfind(".") else "."
            thousands_sep = "." if decimal_sep == "," else ","
            raw = raw.replace(thousands_sep, "").replace(decimal_sep, ".")
        elif "," in raw:
            raw = raw.replace(".", "").replace(",", ".") if raw.count(",") == 1 else raw.replace(",", "")
        elif re.fullmatch(r"\d{1,3}(\.\d{3})+", raw):
            raw = raw.replace(".", "")              # 11.000 → once mil (formato AR)
        try:
            number = Decimal(raw)
        except InvalidOperation:
            return None
    else:
        return None
    if not number.is_finite() or number <= 0:
        return None
    cents = int((number * 100).to_integral_value())
    return cents if 0 < cents <= MAX_PLAUSIBLE_PRICE_CENTS else None


def _close(a: int, b: int) -> bool:
    return abs(a - b) <= max(100, int(max(a, b) * _PRICE_TOLERANCE))


def _normalize_url(url: object) -> str:
    """Para comparar URLs: sin esquema, sin `www.`, sin fragmento, sin / final, en minúsculas."""
    if not isinstance(url, str):
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    path = (parts.path or "").rstrip("/")
    return f"{host}{path}" + (f"?{parts.query}" if parts.query else "")


# ─── Sitemap ─────────────────────────────────────────────────────────────────

_LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", re.I | re.S)


@dataclass(slots=True)
class Sitemap:
    is_index: bool
    locs: list[str]
    truncated: bool = False


def decode_sitemap_body(body: bytes, limit: int = MAX_SITEMAP_BYTES) -> str:
    """Bytes → texto. Si vienen en gzip (un `.xml.gz` no lo descomprime httpx) se
    abre con tope de tamaño."""
    if body[:2] == b"\x1f\x8b":
        try:
            dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
            body = dec.decompress(body, limit + 1)
        except (zlib.error, EOFError, gzip.BadGzipFile):
            return ""
        if len(body) > limit:
            return ""
    return body[:limit].decode("utf-8", errors="replace")


def parse_sitemap(text: str) -> Sitemap:
    """`<urlset>` o `<sitemapindex>` → las URLs de sus `<loc>`. Regex y no un parser
    XML: es tolerante a XML roto y no resuelve entidades externas."""
    if not isinstance(text, str):
        return Sitemap(False, [])
    is_index = bool(re.search(r"<sitemapindex\b", text[:4000], re.I))
    locs: list[str] = []
    truncated = False
    for m in _LOC.finditer(text):
        loc = html_lib.unescape(m.group(1)).strip()
        if not loc or len(loc) > _MAX_URL_LEN or any(ord(c) <= 0x20 for c in loc):
            continue
        if len(locs) >= MAX_SITEMAP_URLS:
            truncated = True
            break
        locs.append(loc)
    return Sitemap(is_index, locs, truncated)


# ─── JSON-LD ────────────────────────────────────────────────────────────────

_LD_BLOCK = re.compile(
    r"<script\b[^>]*\btype\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S)


def iter_json_ld(page: str) -> list[Any]:
    """Los bloques JSON-LD de la página que se pueden leer. Un bloque roto se saltea."""
    out: list[Any] = []
    for m in _LD_BLOCK.finditer(page):
        if len(out) >= _MAX_LD_BLOCKS:
            break
        body = m.group(1).strip()
        if not body or len(body) > _MAX_LD_BLOCK_CHARS:
            continue
        try:
            out.append(json.loads(body, strict=False))
        except ValueError:
            continue
    return out


def _is_type(node: dict, wanted: str) -> bool:
    t = node.get("@type")
    return t == wanted if isinstance(t, str) else isinstance(t, list) and wanted in t


def _walk(node: Any, depth: int = 0):
    """Todos los dicts de un JSON-LD (incluye @graph, listas y `mainEntity`)."""
    if depth > 8:
        return
    if isinstance(node, dict):
        yield node
        for v in node.values():
            if isinstance(v, (dict, list)):
                yield from _walk(v, depth + 1)
    elif isinstance(node, list):
        for v in node[:200]:
            yield from _walk(v, depth + 1)


def _product_urls(prod: dict) -> set[str]:
    urls = {prod.get("@id"), prod.get("url")}
    main = prod.get("mainEntityOfPage")
    urls.add(main.get("@id") if isinstance(main, dict) else main)
    offers = prod.get("offers")
    for offer in offers if isinstance(offers, list) else [offers]:
        if isinstance(offer, dict):
            urls.add(offer.get("url"))
    return {n for n in (_normalize_url(u) for u in urls) if n}


def pick_product(blocks: list[Any], page_urls: list[str]) -> dict | None:
    """El `Product` JSON-LD DE ESTA PÁGINA: el que coincide con alguna de sus URLs.
    Si ninguno coincide y hay exactamente uno, ese. Con varios y ninguno que
    coincida → None (no se arriesga a tomar un producto relacionado)."""
    products = [n for b in blocks for n in _walk(b) if _is_type(n, "Product")]
    wanted = {n for n in (_normalize_url(u) for u in page_urls) if n}
    for prod in products:
        if _product_urls(prod) & wanted:
            return prod
    return products[0] if len(products) == 1 else None


def _offers(prod: dict) -> list[dict]:
    offers = prod.get("offers")
    return [o for o in (offers if isinstance(offers, list) else [offers]) if isinstance(o, dict)]


def _ld_price_cents(prod: dict) -> tuple[int | None, str]:
    """(precio en centavos, nota). Solo pesos; con varias ofertas, la menor."""
    prices: list[int] = []
    for offer in _offers(prod):
        currency = str(offer.get("priceCurrency") or "ARS").upper()
        if currency != "ARS":
            return None, f"moneda {currency[:6]}"
        spec = offer.get("priceSpecification")
        raw = offer.get("price", offer.get("lowPrice", spec.get("price") if isinstance(spec, dict) else None))
        cents = parse_price_cents(raw)
        if cents:
            prices.append(cents)
    return (min(prices), "") if prices else (None, "")


def _ld_text(value: object, limit: int) -> str:
    if isinstance(value, dict):
        value = value.get("name")
    return one_line(value, limit)


def _ld_images(prod: dict) -> list[str]:
    raw = prod.get("image")
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for item in items[:4]:
        url = item.get("url") if isinstance(item, dict) else item
        if isinstance(url, str) and url.strip() and len(url) <= _MAX_URL_LEN:
            out.append(url.strip())
    return out


def _ld_availability_stock(prod: dict) -> int | None:
    """0 si todas las ofertas dicen OutOfStock; None si no se sabe cuántas hay."""
    states = [str(o.get("availability") or "") for o in _offers(prod)]
    if states and all("outofstock" in s.lower() or "soldout" in s.lower() for s in states):
        return 0
    return None


def _meta(page: str, prop: str) -> str:
    m = re.search(r"<meta\b[^>]*\bproperty\s*=\s*[\"']%s[\"'][^>]*\bcontent\s*=\s*[\"']([^\"']*)[\"']"
                  % re.escape(prop), page, re.I)
    return html_lib.unescape(m.group(1)).strip() if m else ""


def _plausibility(price: int, doubtful: bool, note: str) -> tuple[bool, str]:
    if price < MIN_PLAUSIBLE_PRICE_CENTS:
        reason = f"precio muy bajo (ARS {price / 100:,.0f}): probable dato viejo".replace(",", ".")
        return True, f"{note} · {reason}" if note else reason
    return doubtful, note


# ─── Tiendanube ──────────────────────────────────────────────────────────────

_SINGLE_PRODUCT_TAG = re.compile(r"<[a-z]+\b[^>]*\bid\s*=\s*[\"']single-product[\"'][^>]*>", re.I | re.S)
_DATA_VARIANTS = re.compile(r"\bdata-variants\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.I | re.S)


def _tiendanube_variants(page: str) -> list[dict]:
    """`data-variants` del bloque `#single-product` (el producto de ESTA página; los
    demás `data-variants` de la página son los del carrusel de relacionados)."""
    tag = _SINGLE_PRODUCT_TAG.search(page)
    if tag is None:
        return []
    attr = _DATA_VARIANTS.search(tag.group(0))
    if attr is None:
        return []
    try:
        data = json.loads(html_lib.unescape(attr.group(1) or attr.group(2) or ""), strict=False)
    except ValueError:
        return []
    return [v for v in data if isinstance(v, dict)][:100] if isinstance(data, list) else []


def _num_cents(value: object) -> int | None:
    return parse_price_cents(value) if isinstance(value, (int, float, str)) else None


def _tiendanube_price(variants: list[dict]) -> tuple[int | None, int | None, set[int]]:
    """(precio, stock, precios tachados). El precio es el menor entre las variantes con
    stock; si ninguna tiene, entre las visibles; si ninguna, entre todas."""
    def usable(v: dict) -> bool:
        return _num_cents(v.get("price_number")) is not None

    pool = [v for v in variants if usable(v)]
    if not pool:
        return None, None, set()
    visible = [v for v in pool if v.get("is_visible") is not False] or pool
    in_stock = [v for v in visible if v.get("available") is not False
                and (v.get("stock") is None or (isinstance(v.get("stock"), (int, float)) and v["stock"] > 0))]
    chosen = in_stock or visible
    price = min(_num_cents(v["price_number"]) for v in chosen)  # type: ignore[type-var]
    compare = {c for c in (_num_cents(v.get("compare_at_price_number")) for v in pool) if c}
    stocks = [v.get("stock") for v in visible]
    if any(s is None for s in stocks):
        stock = None                                  # sin tope declarado
    else:
        stock = int(sum(s for s in stocks if isinstance(s, (int, float)) and s > 0))
    return price, stock, compare


def parse_tiendanube(page: str, urls: list[str]) -> ParsedItem | None:
    prod = pick_product(iter_json_ld(page), urls)
    title = one_line(prod.get("name"), TITLE_MAX) if prod else ""
    title = title or one_line(_meta(page, "og:title"), TITLE_MAX)
    if not title:
        return None
    variants = _tiendanube_variants(page)
    price, stock, compare_at = _tiendanube_price(variants)
    ld_price, ld_note = _ld_price_cents(prod) if prod else (None, "")
    doubtful, note = False, ""
    if price is None:
        # Sin data-variants no hay con qué contrastar el JSON-LD.
        price = ld_price
        if price is not None:
            doubtful, note = True, "sin variantes en la página: precio del JSON-LD sin verificar"
        elif ld_note:
            note = ld_note
    elif ld_price is not None and not _close(ld_price, price) and not any(_close(ld_price, c) for c in compare_at):
        doubtful = True
        note = f"el JSON-LD dice ARS {ld_price / 100:,.0f} y la página ARS {price / 100:,.0f}".replace(",", ".")
    if price is not None:
        doubtful, note = _plausibility(price, doubtful, note)
    sku = one_line(prod.get("sku"), 80) if prod else ""
    if not sku:
        sku = one_line(next((v.get("sku") for v in variants if v.get("sku")), ""), 80)
    images = _ld_images(prod) if prod else []
    for extra in (_meta(page, "og:image:secure_url"), _meta(page, "og:image"),
                  next((v.get("image_url") for v in variants if isinstance(v.get("image_url"), str)), "")):
        if extra and extra not in images:
            images.append(extra)
    if stock is None and prod:
        stock = _ld_availability_stock(prod)
    return ParsedItem(
        title=title, price_cents=price, price_doubtful=doubtful, price_note=note, sku=sku,
        brand=_ld_text(prod.get("brand"), 80) if prod else "", stock=stock, image_urls=images[:4],
    )


# ─── JSON-LD + estado de Next.js (Gadnic) ───────────────────────────────────

_FLIGHT_WINDOW = 8000


def _flight_product(page: str, sku: str) -> str:
    """El tramo del estado de Next.js (`self.__next_f.push`) que describe ESTE producto:
    desde `"product":{"sku":"<sku>"`. Los `listPrice` / `finalPrice` que vienen
    después son los de los productos relacionados."""
    flat = page.replace('\\"', '"')
    sku_pattern = re.escape(sku) if sku else '[^"]*'
    m = re.search(r'"product"\s*:\s*\{\s*"sku"\s*:\s*"%s"' % sku_pattern, flat)
    return flat[m.start():m.start() + _FLIGHT_WINDOW] if m else ""


def _first_number(window: str, key: str) -> str | None:
    m = re.search(r'"%s":\s*([\d.]+)' % re.escape(key), window)
    return m.group(1) if m else None


def parse_jsonld(page: str, urls: list[str]) -> ParsedItem | None:
    prod = pick_product(iter_json_ld(page), urls)
    if prod is None:
        return None
    title = one_line(prod.get("name"), TITLE_MAX) or one_line(_meta(page, "og:title"), TITLE_MAX)
    if not title:
        return None
    sku = one_line(prod.get("sku"), 80)
    ld_price, note = _ld_price_cents(prod)
    window = _flight_product(page, sku)
    visible = parse_price_cents(_first_number(window, "finalPrice")) if window else None
    price, doubtful = ld_price, False
    if visible is not None and ld_price is not None and not _close(visible, ld_price):
        # Manda lo que se ve en pantalla, pero algo no cierra: se avisa.
        price, doubtful = visible, True
        note = (f"el JSON-LD dice ARS {ld_price / 100:,.0f} y la página ARS {visible / 100:,.0f}"
                .replace(",", "."))
    elif ld_price is None and visible is not None:
        price = visible
    if price is not None:
        doubtful, note = _plausibility(price, doubtful, note)
    stock = _ld_availability_stock(prod)
    max_qty = _first_number(window, "maxQuantity") if window else None
    if stock is None and max_qty is not None and max_qty.isdigit():
        stock = int(max_qty) or None
    images: list[str] = []
    gallery = re.search(r'"gallery"\s*:\s*\[\s*("[^"]+"(?:\s*,\s*"[^"]+")*)', window) if window else None
    if gallery:
        images += [u for u in re.findall(r'"([^"]+)"', gallery.group(1))[:2] if len(u) <= _MAX_URL_LEN]
    images += [u for u in _ld_images(prod) if u not in images]
    for extra in (_meta(page, "og:image"),):
        if extra and extra not in images:
            images.append(extra)
    brand = _ld_text(prod.get("brand"), 80)
    if not brand and window:
        m = re.search(r'"brand":"([^"]{1,80})"', window)
        brand = one_line(m.group(1), 80) if m else ""
    return ParsedItem(title=title, price_cents=price, price_doubtful=doubtful, price_note=note,
                      sku=sku, brand=brand, stock=stock, image_urls=images[:4])


def parse_product_page(platform: str, page: str, urls: list[str]) -> ParsedItem | None:
    """Ficha de producto → datos, o None si la página no es un producto legible.
    `urls` son las URLs con que se pidió y a las que terminó respondiendo."""
    if not isinstance(page, str) or not page:
        return None
    try:
        if platform == PLATFORM_TIENDANUBE:
            return parse_tiendanube(page, urls)
        if platform == PLATFORM_JSONLD:
            return parse_jsonld(page, urls)
    except Exception:  # noqa: BLE001  (un parser no puede tumbar el indexado)
        log.warning("parser de %s reventó en %s", platform, (urls[0] if urls else "?")[:120], exc_info=True)
    return None
