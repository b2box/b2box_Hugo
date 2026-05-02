"""Scraping de precios en webs de competidores.

A diferencia de `source_check` (donde sabemos exactamente la URL del producto),
acá tenemos que BUSCAR el producto por nombre en cada competidor. La interfaz:

  - `CompetitorScraper.search(query)` → lista de PriceQuote (mejores matches)

Empezamos con un scraper genérico que sirve de plantilla. Para producción,
agregar uno por cada competidor real y ajustar selectores.

Idea de diseño: NO romper Hugo si un competidor cambia su HTML. Cada scraper
captura sus propios errores y devuelve `[]`. El orquestador promedia/agrega
los precios de los que sí funcionaron.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass(slots=True)
class CompetitorQuote:
    competitor: str
    title: str
    price_cents: int
    currency: str
    url: str


class CompetitorScraper(ABC):
    name: ClassVar[str]

    @abstractmethod
    async def search(self, query: str, limit: int = 5) -> list[CompetitorQuote]: ...

    async def _get(self, url: str) -> str:
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": _USER_AGENT, "Accept-Language": "es-AR,es;q=0.9"},
            follow_redirects=True,
        ) as c:
            r = await c.get(url)
            r.raise_for_status()
            return r.text


# ─── Scraper de ejemplo: MercadoLibre Argentina ────────────────────


class MercadoLibreScraper(CompetitorScraper):
    """Búsqueda en MercadoLibre AR. Usar como plantilla."""

    name = "mercadolibre_ar"

    async def search(self, query: str, limit: int = 5) -> list[CompetitorQuote]:
        try:
            url = f"https://listado.mercadolibre.com.ar/{quote_plus(query)}"
            html = await self._get(url)
        except httpx.HTTPError:
            return []

        soup = BeautifulSoup(html, "lxml")
        out: list[CompetitorQuote] = []
        # Selectores estables al momento de escribir esto. Pueden romper.
        for card in soup.select("li.ui-search-layout__item")[:limit]:
            title_el = card.select_one("h2.ui-search-item__title, .poly-component__title")
            price_int = card.select_one("span.andes-money-amount__fraction")
            link_el = card.select_one("a.ui-search-link, a.poly-component__title")
            if not (title_el and price_int and link_el):
                continue
            try:
                whole = int(price_int.get_text(strip=True).replace(".", "").replace(",", ""))
            except ValueError:
                continue
            cents_el = card.select_one("span.andes-money-amount__cents")
            cents = 0
            if cents_el:
                try:
                    cents = int(cents_el.get_text(strip=True))
                except ValueError:
                    cents = 0
            price_cents = whole * 100 + cents
            out.append(
                CompetitorQuote(
                    competitor=self.name,
                    title=title_el.get_text(strip=True),
                    price_cents=price_cents,
                    currency="ARS",
                    url=link_el.get("href", ""),
                )
            )
        return out


# Registro — agregar más scrapers acá a medida que los necesitemos
SCRAPERS: list[CompetitorScraper] = [MercadoLibreScraper()]


async def gather_competitor_prices(query: str, limit_per_site: int = 5) -> list[CompetitorQuote]:
    """Corre todos los scrapers y junta los resultados."""
    import asyncio

    results = await asyncio.gather(
        *(s.search(query, limit_per_site) for s in SCRAPERS),
        return_exceptions=True,
    )
    out: list[CompetitorQuote] = []
    for r in results:
        if isinstance(r, list):
            out.extend(r)
    return out
