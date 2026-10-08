"""Páginas de listado de ML SINTÉTICAS, con la misma estructura que la real
(medida el 08-oct-2026): un script `__NORDIC_RENDERING_CTX__` con
`_n.ctx.r = {...}` y un JSON-LD schema.org. Sin red ni datos reales."""

from __future__ import annotations

import json
from typing import Any

from app.ingest.browser_fetch import ListingPage


def polycard(ref: str, title: str, price: float, *, picture: str | None = None,
             url: str | None = None, seller: str | None = "Tienda Uno {icon_cockade}",
             sold: int | None = 100, catalog: str | None = None, user_product: str | None = None,
             currency: str = "ARS") -> dict[str, Any]:
    comps: list[dict[str, Any]] = [
        {"type": "title", "id": "title", "title": {"text": title}},
        {"type": "price", "id": "price_v2", "price": {"current_price": {"value": price, "currency": currency}}},
        {"type": "shipping_v2", "id": "shipping_v2", "shipping_v2": []},
    ]
    if seller is not None:
        comps.insert(1, {"type": "seller", "id": "seller", "seller": {"text": seller}})
    return {
        "id": "POLYCARD", "state": "VISIBLE",
        "polycard": {
            "metadata": {
                "id": ref, "product_id": catalog, "user_product_id": user_product,
                "url": url or f"articulo.mercadolibre.com.ar/{ref[:3]}-{ref[3:]}-{title.lower().replace(' ', '-')}",
                "tracks": {"sold_quantity": sold}, "domain_id": "MLA-ORGANIZERS",
            },
            "pictures": {"pictures": [{"id": picture or f"{ref[3:]}-MLA1_012025"}]},
            "components": comps,
        },
    }


def ld_product(name: str, url: str, price: float, brand: str | None = None,
               image: str = "https://http2.mlstatic.com/D_NQ_NP_1-F.jpg") -> dict[str, Any]:
    node: dict[str, Any] = {
        "@type": "Product", "name": name, "image": image,
        "offers": {"@type": "Offer", "price": price, "priceCurrency": "ARS", "url": url},
    }
    if brand:
        node["brand"] = {"@type": "Brand", "name": brand}
    return node


def listing_html(results: list[dict[str, Any]] | None, *, ld: list[dict[str, Any]] | None = None,
                 with_state: bool = True) -> str:
    """Una página de listado. `results=None` + `with_state=False` = sin estado."""
    scripts = ['<script id="__NORDIC_SCOPE__">window._n={ctx:{s:{}}};</script>']
    if with_state:
        state = {"appProps": {"pageProps": {"initialState": {"results": results or [], "query": "x"}}}}
        scripts.append(
            '<script id="__NORDIC_RENDERING_CTX__" nonce="abc">_n.ctx.r='
            + json.dumps(state, ensure_ascii=False)
            + ';_n.ctx.r.assets.manifest=new Map([["a.css","a.123.css"]]);</script>')
    if ld is not None:
        graph = {"@context": "https://schema.org", "@graph": [*ld, {"@type": "SearchResultsPage"}]}
        scripts.append('<script type="application/ld+json" nonce="abc">'
                       + json.dumps(graph, ensure_ascii=False) + "</script>")
    return "<!DOCTYPE html><html><head>" + "".join(scripts) + "</head><body></body></html>"


def page(html: str, *, status: int | None = 200, nbytes: int = 400_000,
         final_url: str = "https://listado.mercadolibre.com.ar/x") -> ListingPage:
    return ListingPage(html=html, final_url=final_url, status=status, bytes=nbytes, elapsed_s=1.0)


ANTIBOT_HTML = ("<html><head><title>Tráfico inusual</title></head><body>"
                '<div class="suspicious-traffic">Detectamos tráfico inusual</div></body></html>')
