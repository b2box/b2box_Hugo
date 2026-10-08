"""Mercado Libre para el monitor nocturno de precios (semáforo).

Reusa el token y las formas de `app/ingest/meli.py` (ese módulo resuelve el
link que pega UN cliente; este recorre ~1.500 productos por noche), y le suma
lo que una pasada masiva necesita y el lookup interactivo no:

  * budget diario de requests con reserva atómica fail-closed
    (`pm_ml_daily_budget`, misma mecánica que el de OTAPI);
  * backoff exponencial ante 429/5xx, con tope de intentos: un producto que
    ML no quiere contestar queda `failed` y el job SIGUE con el siguiente;
  * un solo `httpx.AsyncClient` por corrida (keep-alive) en vez de uno por
    request;
  * cache de 30 días de las ventas de cada vendedor (`/users/{id}`), porque
    los mismos vendedores aparecen en cientos de fichas;
  * dos sondas de 1 request que se corren la primera noche y se guardan en
    `settings` para decidir después (no se usan todavía).

Endpoints:
    GET /products/search?status=active&site_id=MLA&q=…&limit=20  → fichas de catálogo
    GET /products/{id}/items?limit=20                            → vendedores + precio
    GET /users/{seller_id}                                       → reputación, ventas
    GET /sites/MLA/listing_prices?price=…&category_id=…          → comisión/envío (sonda)

ML no tiene búsqueda por foto en su API: la foto se usa para FILTRAR lo que
devuelve la búsqueda por título (ver market_match.py), no para buscar.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import quote

import httpx
from sqlmodel import Session

from app.clock import utcnow
from app.db.models import MlSellerCache, Setting
from app.db.session import engine
from app.ingest import meli
from app.pricing import daily_budget

log = logging.getLogger(__name__)

ML_COUNTER_KEY = "_meta:pm_ml_calls_today"
PROBE_LISTING_PRICES_KEY = "_meta:pm_probe_listing_prices"
PROBE_SOLD_QUANTITY_KEY = "_meta:pm_probe_sold_quantity"

SELLER_CACHE_DAYS = 30
SITE = "MLA"
_SEARCH_LIMIT = 20
_ITEMS_LIMIT = 20
_TIMEOUT = httpx.Timeout(20.0, connect=6.0)

# Códigos que vale la pena reintentar: ML limitó (429) o se cayó (5xx).
# Cualquier otro 4xx es definitivo para ese path.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 4
_BACKOFF_BASE_S = 1.0
_BACKOFF_MAX_S = 30.0
# Si ML manda Retry-After lo respetamos, pero acotado: una noche no puede
# quedar colgada esperando un header que diga "volvé en una hora".
_RETRY_AFTER_CAP_S = 60.0


class BudgetExhausted(RuntimeError):
    """Se llegó a `pm_ml_daily_budget` (o no se pudo confirmar el cupo)."""


class MlUnavailable(meli.MeliError):
    """ML siguió en 429/5xx después de todos los reintentos."""


@dataclass(slots=True)
class MlCandidate:
    """Una ficha de catálogo de ML que devolvió la búsqueda por título."""
    id: str
    name: str
    image_urls: list[str] = field(default_factory=list)
    permalink: str = ""
    domain_id: str = ""


@dataclass(slots=True)
class MlListing:
    """Una publicación (vendedor) dentro de una ficha de catálogo."""
    item_id: str
    seller_id: str
    price_cents: int | None
    currency: str | None
    category_id: str | None = None
    # Solo si el payload de /products/{id}/items lo trae (sonda). Con esto
    # presente, el filtro de "pocas ventas" no necesita pegarle a /users.
    sold_quantity: int | None = None
    free_shipping: bool | None = None


# ─── Parseo (puro) ─────────────────────────────────────────────────


def _int_or_none(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_candidates(raw: dict) -> list[MlCandidate]:
    out: list[MlCandidate] = []
    for r in (raw.get("results") or []):
        if not isinstance(r, dict) or not r.get("id"):
            continue
        out.append(MlCandidate(
            id=str(r["id"]),
            name=str(r.get("name") or r.get("title") or ""),
            image_urls=meli._pictures_from(r),
            permalink=str(r.get("permalink") or ""),
            domain_id=str(r.get("domain_id") or ""),
        ))
    return out


def parse_listings(raw: dict) -> list[MlListing]:
    out: list[MlListing] = []
    for r in (raw.get("results") or []):
        if not isinstance(r, dict):
            continue
        price = r.get("price")
        cents = round(float(price) * 100) if isinstance(price, (int, float)) and price > 0 else None
        shipping = r.get("shipping") if isinstance(r.get("shipping"), dict) else {}
        out.append(MlListing(
            item_id=str(r.get("item_id") or r.get("id") or ""),
            seller_id=str(r.get("seller_id") or ""),
            price_cents=cents,
            currency=str(r.get("currency_id") or "") or None,
            category_id=str(r.get("category_id") or "") or None,
            sold_quantity=_int_or_none(r.get("sold_quantity")),
            free_shipping=shipping.get("free_shipping") if shipping else None,
        ))
    return out


def payload_has_sold_quantity(raw: dict) -> bool:
    """Sonda: ¿`/products/{id}/items` trae `sold_quantity`? Mira la CLAVE, no el
    valor: un 0 también cuenta como presente."""
    return any(
        isinstance(r, dict) and "sold_quantity" in r
        for r in (raw.get("results") or [])
    )


def completed_sales_from_user(raw: dict) -> int | None:
    """`/users/{id}` → seller_reputation.transactions.completed, o None."""
    rep = raw.get("seller_reputation")
    if not isinstance(rep, dict):
        return None
    tx = rep.get("transactions")
    if not isinstance(tx, dict):
        return None
    return _int_or_none(tx.get("completed"))


def retry_delay(attempt: int, retry_after: str | None) -> float:
    """Espera antes del intento `attempt + 1`: exponencial con tope, o el
    Retry-After de ML si lo mandó (acotado)."""
    if retry_after:
        try:
            return max(0.0, min(float(retry_after), _RETRY_AFTER_CAP_S))
        except ValueError:
            pass
    return min(_BACKOFF_BASE_S * (2 ** (attempt - 1)), _BACKOFF_MAX_S)


# ─── Cache de vendedores ──────────────────────────────────────────


def _seller_cache_get(seller_id: str) -> tuple[bool, int | None]:
    """(hay dato fresco, ventas). `None` como ventas también se cachea: ML no
    dio el número y no vale la pena repreguntar cada noche."""
    try:
        with Session(engine) as s:
            row = s.get(MlSellerCache, seller_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo leer ml_seller_cache: %s", exc)
        return False, None
    if row is None:
        return False, None
    if row.fetched_at < utcnow() - timedelta(days=SELLER_CACHE_DAYS):
        return False, None
    return True, row.completed_sales


def _seller_cache_put(seller_id: str, sales: int | None) -> None:
    try:
        with Session(engine) as s:
            row = s.get(MlSellerCache, seller_id)
            if row is None:
                s.add(MlSellerCache(seller_id=seller_id, completed_sales=sales))
            else:
                row.completed_sales = sales
                row.fetched_at = utcnow()
                s.add(row)
            s.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo guardar ml_seller_cache(%s): %s", seller_id, exc)


# ─── Sondas ───────────────────────────────────────────────────────


def probe_recorded(key: str) -> bool:
    try:
        with Session(engine) as s:
            return s.get(Setting, key) is not None
    except Exception:  # noqa: BLE001
        return True  # si la DB no contesta, no gastamos el request


def record_probe(key: str, payload: dict) -> None:
    payload = {**payload, "at": utcnow().isoformat()}
    try:
        with Session(engine) as s:
            row = s.get(Setting, key)
            if row is None:
                s.add(Setting(key=key, value=json.dumps(payload)))
            else:
                row.value = json.dumps(payload)
                row.updated_at = utcnow()
                s.add(row)
            s.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo guardar la sonda %s: %s", key, exc)
    log.info("Sonda ML %s: %s", key, payload)


# ─── Cliente de la corrida ────────────────────────────────────────


TokenGetter = Callable[[], Awaitable[str]]


class MlMarket:
    """Acceso a ML para UNA corrida: cuenta sus requests y comparte conexión.

    `budget` es el tope diario (`pm_ml_daily_budget`); el contador vive en
    `settings` y lo comparten todas las corridas del día. `sleep` y
    `token_getter` se inyectan para que los tests no esperen ni pidan token.
    """

    def __init__(
        self,
        *,
        budget: int,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        token_getter: TokenGetter = meli.get_token,
    ) -> None:
        self.budget = int(budget)
        self._client = client
        self._own_client = client is None
        self._sleep = sleep
        self._token = token_getter
        self.requests_used = 0
        self.retries = 0
        # Una vez agotado el cupo del día no se vuelve a consultar la DB por
        # cada producto que falta: el resto de la corrida sale `skipped` al toque.
        self.exhausted = False

    async def __aenter__(self) -> "MlMarket":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _reserve(self) -> None:
        if self.exhausted or daily_budget.reserve(ML_COUNTER_KEY, self.budget) is None:
            self.exhausted = True
            raise BudgetExhausted(f"budget ML del día agotado ({self.budget})")
        self.requests_used += 1

    async def request(self, path: str) -> httpx.Response:
        """GET autenticado con budget y backoff. Devuelve la respuesta (cualquier
        status que no sea reintentable); lanza BudgetExhausted o MlUnavailable."""
        assert self._client is not None, "usar como `async with MlMarket(...) as ml`"
        last_status: int | None = None
        last_error = ""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            self._reserve()
            token = await self._token()
            try:
                resp = await self._client.get(
                    f"{meli.API_BASE}{path}",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                last_status, last_error = None, f"{type(exc).__name__}: {exc}"
                retry_after = None
            else:
                if resp.status_code not in _RETRY_STATUSES:
                    return resp
                last_status, last_error = resp.status_code, resp.text[:200]
                retry_after = resp.headers.get("Retry-After")
            if attempt < _MAX_ATTEMPTS:
                delay = retry_delay(attempt, retry_after)
                self.retries += 1
                log.info("ML %s → %s (intento %d/%d), reintento en %.0fs",
                         path.split("?")[0], last_status or last_error, attempt, _MAX_ATTEMPTS, delay)
                await self._sleep(delay)
        raise MlUnavailable(f"ML no respondió {path.split('?')[0]}: {last_status or last_error}")

    async def get_json(self, path: str) -> dict:
        resp = await self.request(path)
        if resp.status_code == 404:
            raise meli.MeliError(f"ML no encontró {path}")
        if resp.status_code >= 400:
            raise meli.MeliError(f"HTTP {resp.status_code} en {path.split('?')[0]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise meli.MeliError(f"Respuesta no-JSON en {path}") from exc
        if not isinstance(data, dict):
            raise meli.MeliError(f"Respuesta inesperada en {path}")
        return data

    async def search(self, query: str, site: str = SITE) -> list[MlCandidate]:
        """Fichas de catálogo activas para `query`. Lista vacía si no hay."""
        if not query:
            return []
        raw = await self.get_json(
            f"/products/search?status=active&site_id={site}&q={quote(query)}&limit={_SEARCH_LIMIT}"
        )
        return parse_candidates(raw)

    async def listings(self, product_id: str) -> tuple[list[MlListing], dict]:
        """Vendedores de la ficha con su precio. Devuelve también el payload
        crudo para la sonda de `sold_quantity`."""
        raw = await self.get_json(f"/products/{quote(product_id)}/items?limit={_ITEMS_LIMIT}")
        return parse_listings(raw), raw

    async def seller_sales(self, seller_id: str) -> int | None:
        """Ventas concretadas del vendedor, con cache de 30 días. None = ML no
        lo dice (o el request falló): el llamador decide qué hacer con eso."""
        if not seller_id:
            return None
        fresh, cached = _seller_cache_get(seller_id)
        if fresh:
            return cached
        try:
            raw = await self.get_json(f"/users/{quote(seller_id)}")
        except meli.MeliError as exc:
            log.info("Sin reputación para el vendedor %s: %s", seller_id, exc)
            return None
        sales = completed_sales_from_user(raw)
        _seller_cache_put(seller_id, sales)
        return sales

    async def probe_listing_prices(self, category_id: str) -> dict:
        """Sonda: ¿`/sites/MLA/listing_prices` contesta con el token de app?
        Solo guarda el status y un recorte del body; no se usa todavía."""
        path = f"/sites/{SITE}/listing_prices?price=10000&category_id={quote(category_id)}"
        try:
            resp = await self.request(path)
            payload = {"status": resp.status_code, "category_id": category_id,
                       "sample": resp.text[:300]}
        except (BudgetExhausted, meli.MeliError) as exc:
            payload = {"status": None, "category_id": category_id, "error": str(exc)[:200]}
        record_probe(PROBE_LISTING_PRICES_KEY, payload)
        return payload


def ml_budget_status() -> dict:
    """Consumo del día para el dashboard."""
    from app import runtime
    from app.config import get_settings

    used = daily_budget.used_today(ML_COUNTER_KEY)
    budget = int(runtime.get("pm_ml_daily_budget") or get_settings().pm_ml_daily_budget)
    return {"used": used, "budget": budget, "remaining": max(0, budget - used)}
