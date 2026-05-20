"""Re-fetch del precio en la fuente original (Alibaba, AliExpress, etc.).

Diseño: cada fuente es un `SourceFetcher` con dos métodos:
  - matches(url) → True si esta clase sabe parsear la URL
  - fetch_price(url) → devuelve PriceQuote o None

Hay un registro `FETCHERS`. Para agregar soporte a un nuevo proveedor:
  1. crear una clase nueva
  2. agregarla a la lista FETCHERS

Implementación inicial: dos fetchers genéricos para Alibaba/AliExpress que
buscan JSON-LD `Product.offers.price`. Si la fuente cambia su markup, hay que
adaptar el parser. Este es un módulo pensado para evolucionar.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ClassVar

import httpx
from bs4 import BeautifulSoup
from sqlmodel import Session, func, select

from app import runtime as runtime_settings
from app.config import get_settings
from app.db.models import PriceHistory
from app.db.session import engine

log = logging.getLogger(__name__)


def _otapi_calls_today() -> int:
    """Cuántos snapshots 1688_otapi llevamos hoy (UTC).

    Sirve para gatear el budget diario antes de cada call.
    """
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        with Session(engine) as s:
            return int(s.exec(
                select(func.count(PriceHistory.id))  # type: ignore[arg-type]
                .where(PriceHistory.source == "1688_otapi",
                       PriceHistory.captured_at >= start)
            ).one() or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo leer contador OTAPI: %s", exc)
        return 0


def otapi_budget_status() -> dict:
    """Helper para que el dashboard muestre el consumo del día."""
    used = _otapi_calls_today()
    budget = int(runtime_settings.get("otapi_daily_budget") or get_settings().otapi_daily_budget)
    return {"used": used, "budget": budget, "remaining": max(0, budget - used)}

_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass(slots=True)
class PriceQuote:
    price_cents: int           # precio en centavos de la moneda original
    currency: str              # ISO: CNY, USD, etc.
    source: str                # nombre del fetcher
    raw_url: str
    # Conversión opcional a USD (cuando la fuente la provee, e.g. OTAPI)
    usd_price_cents: int | None = None


class SourceFetcher(ABC):
    name: ClassVar[str]
    domain_pattern: ClassVar[re.Pattern[str]]

    @classmethod
    def matches(cls, url: str) -> bool:
        return bool(cls.domain_pattern.search(url))

    @abstractmethod
    async def fetch_price(self, url: str) -> PriceQuote | None: ...

    # helpers compartidos
    async def _get(self, url: str) -> str:
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            follow_redirects=True,
        ) as c:
            r = await c.get(url)
            r.raise_for_status()
            return r.text

    @staticmethod
    def _extract_json_ld_price(html: str) -> tuple[float, str] | None:
        """Busca un bloque JSON-LD con Product.offers.price."""
        soup = BeautifulSoup(html, "lxml")
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(tag.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            for entry in data if isinstance(data, list) else [data]:
                if not isinstance(entry, dict):
                    continue
                ld_type = entry.get("@type")
                # @type puede venir como string ("Product") o lista (["Product"])
                is_product = (
                    ld_type == "Product"
                    or (isinstance(ld_type, list) and "Product" in ld_type)
                )
                if not is_product:
                    continue
                offers = entry.get("offers")
                if isinstance(offers, dict):
                    price = offers.get("price") or offers.get("lowPrice")
                    currency = offers.get("priceCurrency") or "USD"
                    if price is not None:
                        try:
                            return float(price), str(currency)
                        except (TypeError, ValueError):
                            pass
                elif isinstance(offers, list) and offers:
                    first = offers[0]
                    price = first.get("price") or first.get("lowPrice")
                    currency = first.get("priceCurrency") or "USD"
                    if price is not None:
                        try:
                            return float(price), str(currency)
                        except (TypeError, ValueError):
                            pass
        return None


class AliExpressFetcher(SourceFetcher):
    name = "aliexpress"
    domain_pattern = re.compile(r"aliexpress\.(com|us|ru|es)", re.IGNORECASE)

    async def fetch_price(self, url: str) -> PriceQuote | None:
        html = await self._get(url)
        # Intento 1: JSON-LD
        jsonld = self._extract_json_ld_price(html)
        if jsonld:
            price, currency = jsonld
            return PriceQuote(int(round(price * 100)), currency, self.name, url)
        # Intento 2: regex sobre runParams (formato típico de AliExpress)
        m = re.search(r'"formatedActivityPrice"\s*:\s*"([^"]+)"', html)
        if m:
            number = re.search(r"([\d.,]+)", m.group(1))
            if number:
                price = float(number.group(1).replace(",", "."))
                return PriceQuote(int(round(price * 100)), "USD", self.name, url)
        return None


class AlibabaFetcher(SourceFetcher):
    name = "alibaba"
    domain_pattern = re.compile(r"alibaba\.com", re.IGNORECASE)

    async def fetch_price(self, url: str) -> PriceQuote | None:
        html = await self._get(url)
        jsonld = self._extract_json_ld_price(html)
        if jsonld:
            price, currency = jsonld
            return PriceQuote(int(round(price * 100)), currency, self.name, url)
        # Alibaba muestra rangos. Tomamos el precio mínimo.
        m = re.search(r'"priceRange"\s*:\s*\[\s*([\d.]+)', html)
        if m:
            return PriceQuote(int(round(float(m.group(1)) * 100)), "USD", self.name, url)
        return None


class Detail1688Fetcher(SourceFetcher):
    """Fetcher para 1688.com vía OTAPI (RapidAPI).

    1688 bloquea scraping directo con captcha geográfico, así que vamos por el
    proxy oficial OTAPI. Item IDs de 1688 se prefijan con 'abb-' en OTAPI.

    Devuelve el precio en su moneda original (CNY) y, cuando OTAPI provee la
    conversión, también el equivalente en USD.
    """

    name = "1688_otapi"
    domain_pattern = re.compile(r"1688\.com/offer/(\d+)", re.IGNORECASE)

    @classmethod
    def _extract_item_id(cls, url: str) -> str | None:
        m = cls.domain_pattern.search(url)
        return m.group(1) if m else None

    async def fetch_price(self, url: str) -> PriceQuote | None:
        item_id = self._extract_item_id(url)
        if not item_id:
            return None
        s = get_settings()
        if not s.rapidapi_key:
            return None  # API no configurada

        # Budget guard: corta antes de hacer la HTTP call si ya pasamos el
        # límite diario de OTAPI. Esto previene billazos por loops o triggers
        # manuales repetidos.
        budget = int(runtime_settings.get("otapi_daily_budget") or s.otapi_daily_budget)
        used_today = _otapi_calls_today()
        if used_today >= budget:
            log.warning(
                "OTAPI daily budget alcanzado (%d/%d) — skipping fetch para %s",
                used_today, budget, item_id,
            )
            return None

        endpoint = f"https://{s.otapi_1688_host}/BatchGetItemFullInfo"
        params = {
            "itemId": f"abb-{item_id}",
            "blockList": "Description,Properties,Attributes,Videos",
        }
        headers = {
            "X-RapidAPI-Key": s.rapidapi_key,
            "X-RapidAPI-Host": s.otapi_1688_host,
        }
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
                r = await c.get(endpoint, params=params, headers=headers)
                r.raise_for_status()
                data = r.json()
        except (httpx.HTTPError, ValueError):
            return None

        if data.get("ErrorCode") != "Ok":
            return None
        item = (data.get("Result") or {}).get("Item") or {}
        price = item.get("Price") or {}

        # Precio original (CNY típicamente)
        original = price.get("OriginalPrice")
        currency = price.get("OriginalCurrencyCode") or price.get("CurrencyName") or "CNY"
        if original is None:
            return None
        try:
            price_cents = int(round(float(original) * 100))
        except (TypeError, ValueError):
            return None

        # Conversión a USD (opcional, OTAPI la suele dar)
        usd_cents: int | None = None
        try:
            internal = (price.get("ConvertedPriceList") or {}).get("Internal") or {}
            if internal.get("Code") == "USD" and internal.get("Price") is not None:
                usd_cents = int(round(float(internal["Price"]) * 100))
        except (TypeError, ValueError):
            usd_cents = None

        return PriceQuote(
            price_cents=price_cents,
            currency=currency,
            source=self.name,
            raw_url=url,
            usd_price_cents=usd_cents,
        )


# Registro — orden importa: el primer fetcher que matchee, gana
FETCHERS: list[SourceFetcher] = [Detail1688Fetcher(), AliExpressFetcher(), AlibabaFetcher()]


async def fetch_source_price(url: str) -> PriceQuote | None:
    """Devuelve el precio actual en la fuente original, o None si no podemos."""
    if not url:
        return None
    for fetcher in FETCHERS:
        if fetcher.matches(url):
            try:
                return await fetcher.fetch_price(url)
            except (httpx.HTTPError, ValueError):
                return None
    return None
