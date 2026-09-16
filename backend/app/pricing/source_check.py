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
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ClassVar

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from app import runtime as runtime_settings
from app.config import get_settings
from app.db.models import PriceHistory
from app.db.session import engine
from app.net_guard import safe_get

log = logging.getLogger(__name__)


# ── Contador diario de llamadas a OTAPI ────────────────────────────────────
# BUG 2026-09-16 (mails de "85% de la cuota" con el dashboard marcando 0/120):
# esto contaba filas de PriceHistory, o sea REQUESTS QUE TERMINARON BIEN. Una
# llamada que OTAPI contesta con ErrorCode != Ok, o que muere en la red, se paga
# igual pero no deja snapshot — así que el contador no se movía y el budget
# diario NUNCA cortaba. Con el item delistado (el caso normal en un catálogo
# viejo) el guard quedaba desactivado justo cuando más falta hacía.
#
# Ahora se cuenta ANTES de cada request HTTP, salga bien o mal, que es lo que
# RapidAPI factura. Vive en una sola fila de `settings` con formato
# "YYYY-MM-DD:N": se resetea sola al cambiar el día UTC y no deja basura.
_OTAPI_COUNTER_KEY = "_meta:otapi_calls_today"
_otapi_counter_lock = threading.Lock()


def _otapi_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _parse_day_counter(raw: str | None, today: str) -> int:
    """"YYYY-MM-DD:N" → N si la fila es de hoy; 0 si es de otro día o está rota.

    Fail-safe hacia 0: un valor corrupto hace que se vuelva a contar desde cero,
    nunca que el budget quede bloqueado para siempre.
    """
    if not raw:
        return 0
    day, _, count = str(raw).partition(":")
    if day != today:
        return 0
    try:
        return max(0, int(count))
    except (TypeError, ValueError):
        return 0


def _snapshots_today() -> int:
    """Snapshots 1688_otapi de hoy. Piso histórico del contador.

    Solo sirve el día que se despliega este fix: el contador nuevo arranca en 0
    aunque ya se hayan gastado requests, y sin este piso el budget del día
    quedaría corrido. Un snapshot SIEMPRE implica un request, así que tomar el
    máximo entre los dos nunca subestima.
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
        log.warning("No se pudo leer el contador de snapshots OTAPI: %s", exc)
        return 0


def _otapi_calls_today() -> int:
    """Requests a OTAPI facturados hoy (UTC), exitosos o no."""
    today = _otapi_day()
    try:
        from app.db.models import Setting

        with Session(engine) as s:
            row = s.get(Setting, _OTAPI_COUNTER_KEY)
            counted = _parse_day_counter(row.value if row else None, today)
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo leer contador OTAPI: %s", exc)
        return _snapshots_today()
    if counted:
        # Contador vivo para hoy: es la fuente de verdad. Ya viene sembrado con
        # el piso de snapshots (ver _reserve_otapi_call), así que no hace falta
        # el COUNT(*) acá — con ~850 productos y 10 fetchers en paralelo, esa
        # consulta por llamada sería carga de DB al pedo.
        return counted
    # Todavía no se contó nada hoy: el piso son los snapshots del día.
    return _snapshots_today()


def _reserve_otapi_call(budget: int) -> int | None:
    """Reserva cupo para UN request contra el budget del día.

    Devuelve el total del día (ya incluyendo este request) si había lugar, o
    None si el budget está agotado o no se pudo reservar.

    Reservar y chequear tienen que pasar JUNTOS. Antes esto eran dos pasos —
    `_otapi_calls_today()` y después incrementar— con varios statements en el
    medio: los 10 fetchers en paralelo de `audit_source_prices` leían todos el
    mismo "todavía hay lugar" antes de que ninguno incrementara y se pasaban
    juntos del tope.

    Adentro es compare-and-swap sobre la fila: se lee el valor y se escribe
    condicionado a que siga siendo el mismo. Si otro proceso lo movió en el
    medio, se reintenta. El lock de threading ordena a los fetchers de ESTE
    proceso; el CAS cubre el caso multi-worker, donde el lock no llega.

    FAIL-CLOSED: si la DB no contesta, devuelve None y el request no se manda.
    Un contador que no se puede leer significa presupuesto desconocido, y
    desconocido no puede querer decir "gastá tranquilo" — con la DB degradada y
    reintentos en loop era justo cuando más falta hacía el freno.
    """
    from sqlalchemy import update

    from app.db.models import Setting

    today = _otapi_day()
    try:
        with _otapi_counter_lock, Session(engine) as s:
            for _ in range(5):  # reintentos del CAS ante carrera entre procesos
                row = s.get(Setting, _OTAPI_COUNTER_KEY)
                if row is not None:
                    s.refresh(row)
                previous = row.value if row is not None else None
                base = _parse_day_counter(previous, today)
                if base == 0:
                    # Primer request del día: se siembra con los snapshots que
                    # ya haya de hoy. Importa el día que se despliega esto sobre
                    # una jornada ya empezada — sin el piso, el budget arrancaría
                    # de cero y el día podría gastar el doble. Un solo COUNT(*)
                    # por día; del segundo request en adelante manda el contador.
                    base = _snapshots_today()
                if base >= budget:
                    return None  # sin cupo: no se reserva ni se incrementa
                total = base + 1
                value = f"{today}:{total}"
                if row is None:
                    s.add(Setting(key=_OTAPI_COUNTER_KEY, value=value))
                    try:
                        s.commit()
                    except IntegrityError:
                        # Otro worker creó la fila primero: se reintenta el CAS.
                        s.rollback()
                        continue
                    return total
                done = s.execute(
                    update(Setting)
                    .where(Setting.key == _OTAPI_COUNTER_KEY,
                           Setting.value == previous)
                    .values(value=value, updated_at=datetime.now(timezone.utc))
                )
                s.commit()
                if done.rowcount == 1:
                    return total
                # rowcount 0 = alguien más escribió entre la lectura y el UPDATE.
                s.expire_all()
            log.warning("No se pudo reservar cupo OTAPI tras 5 intentos — no mando el request")
            return None
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo reservar cupo OTAPI (%s) — no mando el request", exc)
        return None


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
        # safe_get valida scheme + IP pública + cada redirect (anti-SSRF): la URL
        # viene de un campo custom de Vendure, no es de confianza.
        r = await safe_get(
            url,
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        )
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
        #
        # Reservar el cupo ES el chequeo: si vuelve None no hay lugar (o no se
        # pudo confirmar que lo hubiera) y no se manda nada. Se reserva ANTES
        # del request porque RapidAPI cobra el intento, salga bien o mal.
        budget = int(runtime_settings.get("otapi_daily_budget") or s.otapi_daily_budget)
        reserved = _reserve_otapi_call(budget)
        if reserved is None:
            log.warning(
                "Sin cupo OTAPI para hoy (budget %d) — skipping fetch para %s",
                budget, item_id,
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
