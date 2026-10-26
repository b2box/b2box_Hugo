"""Links de 1688: se lee la oferta por id en OTAPI (sin búsqueda por foto).

Base: `pricing/source_check.Detail1688Fetcher` (BatchGetItemFullInfo, cupo
reservado ANTES del request porque RapidAPI cobra el intento). Acá además se
leen la escalera de precios (`QuantityRanges`), el MOQ, el título y las fotos.

Dos cupos, los dos atómicos y fail-closed: el general de OTAPI de Hugo
(`otapi_daily_budget`, el mismo contador que source_check) y uno propio del
estimado (`estimate_otapi_daily_budget`), para que el comparador público no se
coma el cupo de la auditoría.

Lo que vuelve es input de un tercero: id solo dígitos, fotos https de un CDN de
Alibaba, precios finitos y en rango. Nunca lanza.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field

import httpx

from app import runtime
from app.config import get_settings
from app.ingest.apify_1688 import safe_image_url
from app.pricing import daily_budget, source_check
from app.pricing.market_ml_web import clean_line

log = logging.getLogger(__name__)

COUNTER_KEY = "_meta:estimate_otapi_calls_today"
USD_PER_CALL = 0.0009
_OFFER_RE = re.compile(r"(?:^|[./])1688\.com/offer/(\d{5,20})\.html", re.I | re.ASCII)
_MAX_PRICE_CNY = 100_000.0
_MAX_PICTURES = 6
_MAX_TIERS = 10
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(slots=True)
class Offer1688:
    offer_id: str
    title: str = ""
    price_cny: float | None = None
    # [(cantidad mínima, precio CNY)] ordenada por cantidad.
    ladder: list[tuple[int, float]] = field(default_factory=list)
    moq: int | None = None
    pictures: list[str] = field(default_factory=list)

    @property
    def detail_url(self) -> str:
        return f"https://detail.1688.com/offer/{self.offer_id}.html"

    def price_ladder(self) -> list[tuple[int, float]]:
        """La escalera, o un solo tramo con el precio de la oferta."""
        if self.ladder:
            return self.ladder
        return [(self.moq or 1, self.price_cny)] if self.price_cny else []


@dataclass(slots=True)
class OfferRead:
    status: str           # ok | not_found | over_budget | disabled | error
    offer: Offer1688 | None = None
    calls: int = 0


def offer_id_from_url(url: str) -> str | None:
    """Id de una ficha de 1688 (detail.1688.com/offer/123.html, m.1688.com…)."""
    m = _OFFER_RE.search((url or "").strip()[:2048])
    return m.group(1) if m else None


def _price(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 < number < _MAX_PRICE_CNY else None


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return number if 0 < number < 10**9 else None


def _price_of(entry: object) -> float | None:
    if not isinstance(entry, dict):
        return None
    price = entry.get("Price")
    if isinstance(price, dict):
        return _price(price.get("OriginalPrice"))
    return _price(price)


def parse_item(offer_id: str, item: object) -> Offer1688 | None:
    """`Result.Item` de BatchGetItemFullInfo → Offer1688 validada."""
    if not isinstance(item, dict):
        return None
    offer = Offer1688(offer_id=offer_id)
    offer.title = clean_line(item.get("OriginalTitle") or item.get("Title"), 300)
    offer.price_cny = _price_of(item)
    ranges = item.get("QuantityRanges")
    if isinstance(ranges, list):
        ladder: dict[int, float] = {}
        for entry in ranges[:_MAX_TIERS]:
            qty = _int(entry.get("MinQuantity")) if isinstance(entry, dict) else None
            price = _price_of(entry)
            if qty is not None and price is not None:
                ladder[qty] = price
        offer.ladder = sorted(ladder.items())
    offer.moq = _int(item.get("FirstLotQuantity")) or (offer.ladder[0][0] if offer.ladder else None)
    pictures: list[str] = []
    main = safe_image_url(item.get("MainPictureUrl"))
    if main:
        pictures.append(main)
    for pic in item.get("Pictures") or []:
        if not isinstance(pic, dict):
            continue
        large = pic.get("Large") if isinstance(pic.get("Large"), dict) else {}
        url = safe_image_url(large.get("Url") or pic.get("Url"))
        if url and url not in pictures:
            pictures.append(url)
        if len(pictures) >= _MAX_PICTURES:
            break
    offer.pictures = pictures
    if not offer.price_ladder():
        return None
    return offer


async def _reserve() -> bool:
    s = get_settings()
    # 0 en el panel tiene que frenar: `or` lo trataba como "sin valor" y volvía al default del env.
    raw = runtime.get("otapi_daily_budget")
    general = int(s.otapi_daily_budget if raw is None else raw)
    own = int(s.estimate_otapi_daily_budget)
    if own <= 0 or general <= 0:
        return False
    if await daily_budget.reserve_async(COUNTER_KEY, own) is None:
        return False
    # El general es el mismo contador que source_check (lo que factura RapidAPI).
    return await daily_budget.reserve_async(source_check._OTAPI_COUNTER_KEY, general) is not None


async def read_offer(offer_id: str, *, timeout_s: float = 4.0) -> OfferRead:
    """Lee la oferta. Nunca lanza."""
    s = get_settings()
    if not s.rapidapi_key:
        return OfferRead("disabled")
    if not re.fullmatch(r"\d{5,20}", offer_id or "", re.ASCII):
        return OfferRead("error")
    if not await _reserve():
        log.info("estimado: sin cupo de OTAPI para leer la oferta de 1688")
        return OfferRead("over_budget")
    endpoint = f"https://{s.otapi_1688_host}/BatchGetItemFullInfo"
    params = {"itemId": f"abb-{offer_id}", "blockList": "Description,Videos"}
    headers = {"X-RapidAPI-Key": s.rapidapi_key, "X-RapidAPI-Host": s.otapi_1688_host}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 3.0))) as c:
            r = await c.get(endpoint, params=params, headers=headers)
        if r.status_code != 200 or len(r.content) > _MAX_RESPONSE_BYTES:
            log.warning("estimado: OTAPI devolvió HTTP %s", r.status_code)
            return OfferRead("error", calls=1)
        data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("estimado: OTAPI falló (%s)", type(exc).__name__)
        return OfferRead("error", calls=1)
    if not isinstance(data, dict) or data.get("ErrorCode") != "Ok":
        return OfferRead("not_found", calls=1)
    result = data.get("Result") if isinstance(data.get("Result"), dict) else {}
    offer = parse_item(offer_id, result.get("Item"))
    return OfferRead("ok" if offer else "not_found", offer, calls=1)
