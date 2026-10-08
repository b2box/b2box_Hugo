"""Páginas y sitemaps SINTÉTICOS con la forma de los dos sitios reales.

Armados a partir de lo que se vio el 08-oct-2026 en Casa Perfecta (Tiendanube) y
Gadnic (Next.js con JSON-LD), con productos inventados. Sin red. Las trampas que
tienen las páginas reales están a propósito:

  * Tiendanube: hay un JSON-LD `Product` por cada producto RELACIONADO del carrusel
    (con su propio @id y, en promoción, el precio TACHADO como `price`), y varios
    `data-variants` en la página; el de este producto es el del `#single-product`.
  * Gadnic: el estado de Next.js repite `listPrice` / `finalPrice` de los
    relacionados después de los del producto; hay fichas con un precio viejo
    (JSON-LD y página dicen ARS 249 por un mini teclado).
"""

from __future__ import annotations

import html
import json

CP = "https://www.casaperfecta.com.ar"
GD = "https://www.gadnic.com.ar"
TN_IMG = "https://acdn-us.mitiendanube.com/stores/001/133/924/products"
GD_STATIC = "https://static.bidcom.com.ar/publicacionesML/productos"
GD_RESIZE = "https://images.bidcom.com.ar/resize?src="


def variant(price: float, *, compare: float | None = None, stock: int | None = 5, sku: str = "ART-X",
            available: bool = True, visible: bool = True, option: str | None = None,
            image: str = "//acdn-us.mitiendanube.com/stores/001/133/924/products/v-1024-1024.webp") -> dict:
    return {
        "product_id": 1, "price_short": f"${price:,.0f}", "price_number": price,
        "price_number_raw": int(price * 100), "compare_at_price_number": compare,
        "has_promotional_price": compare is not None, "stock": stock, "sku": sku,
        "available": available, "is_visible": visible, "option0": option, "id": 99,
        "image_url": image,
    }


def _ld_product(url: str, name: str, price: float, *, sku: str = "SKU1", brand: str | None = None,
                image: str | None = None, weight: str = "0.111", in_stock: bool = True) -> dict:
    prod: dict = {
        "@type": "Product", "@id": url, "name": name,
        "image": image or f"{TN_IMG}/{sku.lower()}-480-0.webp", "sku": sku,
        "weight": {"@type": "QuantitativeValue", "unitCode": "KGM", "value": weight},
        "offers": {"@type": "Offer", "url": url, "priceCurrency": "ARS", "price": str(price),
                   "availability": "https://schema.org/InStock" if in_stock else "https://schema.org/OutOfStock"},
    }
    if brand:
        prod["brand"] = {"@type": "Brand", "name": brand}
    return prod


def _script_ld(obj: dict) -> str:
    return f'<script type="application/ld+json">\n{json.dumps(obj, ensure_ascii=False, indent=1)}\n</script>'


def tiendanube_page(url: str, name: str, variants: list[dict], *, ld_price: float | None = None,
                    sku: str = "ART-X", brand: str | None = None, related: int = 3,
                    og_image: str = f"http://acdn-us.mitiendanube.com/stores/001/133/924/products/og-640-0.webp",
                    ) -> str:
    """Una página de producto de Tiendanube. `ld_price` es lo que dice el JSON-LD del
    producto (en una promoción real es el precio TACHADO)."""
    price = ld_price if ld_price is not None else (variants[0]["price_number"] if variants else 1)
    main = _ld_product(url, name, price, sku=sku, brand=brand)
    parts = [
        "<html><head>",
        f'<meta property="og:title" content="{html.escape(name)}" />',
        f'<meta property="og:image" content="{og_image}" />',
        f'<meta property="og:image:secure_url" content="{og_image.replace("http://", "https://")}" />',
        _script_ld({"@context": "https://schema.org", "@type": "Organization", "name": "Casa Perfecta"}),
        _script_ld({"@context": "https://schema.org/", "@type": "WebPage", "name": f"{name} - Comprar",
                    "mainEntity": main}),
    ]
    # Relacionados: otro @id, otro precio. Lo que NO se tiene que leer.
    for i in range(related):
        rel_url = f"{CP}/productos/relacionado-{i}/"
        parts.append(_script_ld({"@context": "https://schema.org/", **_ld_product(
            rel_url, f"Relacionado {i}", 67000 + i, sku=f"REL{i}", brand="Marca Rel")}))
    parts.append("</head><body>")
    rel_variants = html.escape(json.dumps([variant(1500 + i, compare=99999, sku=f"REL{i}") for i in range(2)]), quote=True)
    parts.append(f'<div class="related" data-variants="{rel_variants}">carrusel</div>')
    attr = html.escape(json.dumps(variants), quote=True)
    parts.append(f'<div id="single-product" class="js-product-detail js-has-new-shipping" data-variants="{attr}">')
    parts.append('<span id="price_display" data-product-price="1">$ 1</span></div></body></html>')
    return "\n".join(parts)


def gadnic_page(url: str, name: str, *, ld_price: float | None, final_price: float | None = None,
                sku: str = "MICCOND6", brand: str | None = "Gadnic", in_stock: bool = True,
                max_qty: int = 10, related_prices: tuple[int, ...] = (66220, 37331, 106664)) -> str:
    """Una página de Gadnic: JSON-LD `Product` + el estado de Next.js con el precio visible.
    `final_price=None` arma una página sin estado de Next.js."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    product: dict = {
        "@context": "https://schema.org", "@type": "Product", "gtin13": "", "sku": sku,
        "description": "Descripción larga\ncon salto de línea.", "name": name,
        "image": f"{GD_RESIZE}{GD_STATIC}/{sku}/1000x1000-{sku}.jpg&w=800&q=100",
        "offers": {"@type": "Offer", "url": url, "priceCurrency": "ARS", "priceValidUntil": "2030-01-01",
                   "availability": "https://schema.org/InStock" if in_stock else "https://schema.org/OutOfStock"},
    }
    if brand:
        product["brand"] = brand                       # en Gadnic es un string, no un objeto
    if ld_price is not None:
        product["offers"]["price"] = ld_price
    crumbs = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Inicio", "item": GD + "/"},
        {"@type": "ListItem", "position": 2, "name": sku, "item": url}]}
    parts = ["<html><head>", f'<meta property="og:title" content="{html.escape(name)} | Gadnic"/>',
             _script_ld(product), _script_ld(crumbs), "</head><body>"]
    if final_price is not None:
        state = {"product": {"sku": sku, "slug": slug, "name": name, "brand": brand or "", "maxQuantity": max_qty,
                             "priceOptions": [{"code": "3_cuotas", "listPrice": round(final_price * 1.25),
                                               "finalPrice": final_price, "currency": "ARS"}],
                             "untaxedPrice": round(final_price / 1.21, 2),
                             "media": {"gallery": [f"{GD_STATIC}/{sku}/1000x1000-{sku}.jpg",
                                                   f"{GD_STATIC}/{sku}/1000x1000-{sku}-1.jpg"]}},
                 "related": [{"priceOptions": [{"listPrice": p * 2, "finalPrice": p}]} for p in related_prices]}
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":")).replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'<script>self.__next_f.push([1,"6:[[\\"$\\",\\"$L1d\\",null,{payload}]]"])</script>')
    parts.append("</body></html>")
    return "\n".join(parts)


def sitemap_index(*locs: str) -> str:
    items = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return f'<?xml version="1.0" encoding="utf-8"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</sitemapindex>'


def urlset(*locs: str, with_hreflang: bool = False) -> str:
    items = ""
    for loc in locs:
        alt = f'<xhtml:link rel="alternate" hreflang="es-ar" href="{loc}" />' if with_hreflang else ""
        items += f"<url>\n<loc>{html.escape(loc)}</loc>\n<changefreq>weekly</changefreq>{alt}\n</url>\n"
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            f'xmlns:xhtml="http://www.w3.org/1999/xhtml">{items}</urlset>')
