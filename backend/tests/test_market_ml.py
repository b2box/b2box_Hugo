"""Mercado Libre para el semáforo: budget, backoff, vendedores y sondas.

Sin red: cada test arma un `httpx.MockTransport`, el token es fijo y el sleep
del backoff se registra en vez de esperar.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

from app.clock import utcnow  # noqa: E402
from app.db.models import MlSellerCache, Setting  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.ingest import meli  # noqa: E402
from app.pricing import daily_budget, market_ml  # noqa: E402
from app.pricing.market_ml import BudgetExhausted, MlMarket, MlUnavailable  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db():
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        for model in (Setting, MlSellerCache):
            for row in s.exec(select(model)).all():
                s.delete(row)
        s.commit()
    yield


async def _token() -> str:
    return "tok"


class Recorder:
    def __init__(self, responder):
        self.responder = responder
        self.paths: list[str] = []
        self.sleeps: list[float] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "api.mercadolibre.com"
            assert request.headers["Authorization"] == "Bearer tok"
            self.paths.append(request.url.path)
            return self.responder(request)
        return httpx.MockTransport(handler)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def market(self, budget: int = 100) -> MlMarket:
        return MlMarket(
            budget=budget,
            client=httpx.AsyncClient(transport=self.transport()),
            sleep=self.sleep,
            token_getter=_token,
        )


def _search_payload() -> dict:
    return {"results": [
        {"id": "MLA1", "name": "Organizador de cocina", "permalink": "https://ml/p/MLA1",
         "domain_id": "MLA-ORG", "pictures": [{"url": "http://http2.mlstatic.com/a.jpg",
                                                "secure_url": "https://http2.mlstatic.com/a.jpg"}]},
        {"name": "sin id: se descarta"},
        {"id": "MLA2", "name": "Otro", "pictures": []},
    ]}


def _items_payload(with_sold: bool = False) -> dict:
    rows = [
        {"item_id": "MLA100", "seller_id": 11, "price": 12500.5, "currency_id": "ARS",
         "category_id": "MLA1234", "shipping": {"free_shipping": True}},
        {"item_id": "MLA101", "seller_id": 12, "price": 0, "currency_id": "ARS"},
        {"item_id": "MLA102", "seller_id": 13, "price": 99, "currency_id": "USD"},
    ]
    if with_sold:
        for r in rows:
            r["sold_quantity"] = 0
    return {"results": rows}


# ─── parseo ─────────────────────────────────────────────────────────────────


def test_parse_candidates_keeps_only_rows_with_id():
    cands = market_ml.parse_candidates(_search_payload())
    assert [c.id for c in cands] == ["MLA1", "MLA2"]
    assert cands[0].image_urls == ["https://http2.mlstatic.com/a.jpg"]
    assert cands[0].permalink == "https://ml/p/MLA1"


def test_parse_listings_converts_to_cents_and_drops_zero_prices():
    listings = market_ml.parse_listings(_items_payload())
    assert listings[0].price_cents == 1_250_050
    assert listings[0].seller_id == "11"
    assert listings[0].free_shipping is True
    assert listings[1].price_cents is None
    assert listings[2].currency == "USD"


def test_sold_quantity_probe_looks_at_the_key_not_the_value():
    assert market_ml.payload_has_sold_quantity(_items_payload(with_sold=True)) is True
    assert market_ml.payload_has_sold_quantity(_items_payload()) is False


def test_completed_sales_from_user_payload():
    assert market_ml.completed_sales_from_user(
        {"seller_reputation": {"transactions": {"completed": 1234}}}) == 1234
    assert market_ml.completed_sales_from_user({"seller_reputation": None}) is None
    assert market_ml.completed_sales_from_user({}) is None


def test_retry_delay_is_exponential_capped_and_respects_retry_after():
    assert [market_ml.retry_delay(a, None) for a in (1, 2, 3)] == [1.0, 2.0, 4.0]
    assert market_ml.retry_delay(20, None) == 30.0
    assert market_ml.retry_delay(1, "7") == 7.0
    assert market_ml.retry_delay(1, "3600") == 60.0  # acotado
    assert market_ml.retry_delay(2, "mañana") == 2.0  # header roto → exponencial


# ─── backoff ───────────────────────────────────────────────────────────────


async def test_persistent_429_raises_unavailable_after_backing_off():
    rec = Recorder(lambda r: httpx.Response(429, text="slow down"))
    async with rec.market() as ml:
        with pytest.raises(MlUnavailable):
            await ml.search("organizador")
    assert len(rec.paths) == market_ml._MAX_ATTEMPTS
    assert rec.sleeps == [1.0, 2.0, 4.0]
    # Cada intento sale del budget: ML factura/limita requests, no éxitos.
    assert ml.requests_used == market_ml._MAX_ATTEMPTS
    assert daily_budget.used_today(market_ml.ML_COUNTER_KEY) == market_ml._MAX_ATTEMPTS


async def test_429_then_success_returns_the_data():
    calls = {"n": 0}

    def responder(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        if calls["n"] == 2:
            return httpx.Response(503)
        return httpx.Response(200, json=_search_payload())

    rec = Recorder(responder)
    async with rec.market() as ml:
        cands = await ml.search("organizador")
    assert [c.id for c in cands] == ["MLA1", "MLA2"]
    assert rec.sleeps == [2.0, 2.0]
    assert ml.retries == 2


async def test_network_errors_are_retried_too():
    calls = {"n": 0}

    def responder(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"results": []})

    rec = Recorder(responder)
    async with rec.market() as ml:
        assert await ml.search("x") == []
    assert calls["n"] == 2


async def test_403_is_final_and_not_retried():
    rec = Recorder(lambda r: httpx.Response(403, json={"message": "forbidden"}))
    async with rec.market() as ml:
        with pytest.raises(meli.MeliError) as exc:
            await ml.search("x")
    assert not isinstance(exc.value, MlUnavailable)
    assert len(rec.paths) == 1 and rec.sleeps == []


async def test_search_quotes_the_query():
    seen: list[str] = []

    def responder(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"results": []})

    rec = Recorder(responder)
    async with rec.market() as ml:
        await ml.search("taza & plato 10%")
    assert "q=taza%20%26%20plato%2010%25" in seen[0]
    assert "site_id=MLA" in seen[0] and "status=active" in seen[0]


# ─── budget ────────────────────────────────────────────────────────────────


async def test_budget_is_never_exceeded_with_concurrent_requests():
    rec = Recorder(lambda r: httpx.Response(200, json={"results": []}))
    async with rec.market(budget=5) as ml:
        results = await asyncio.gather(*(ml.search(f"q{i}") for i in range(12)),
                                       return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    exhausted = [r for r in results if isinstance(r, BudgetExhausted)]
    assert len(ok) == 5 and len(exhausted) == 7
    assert len(rec.paths) == 5
    assert daily_budget.used_today(market_ml.ML_COUNTER_KEY) == 5


async def test_once_exhausted_it_stops_asking_the_db(monkeypatch):
    rec = Recorder(lambda r: httpx.Response(200, json={"results": []}))
    reserves = {"n": 0}
    real = daily_budget.reserve

    def counting(*a, **kw):
        reserves["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(daily_budget, "reserve", counting)
    async with rec.market(budget=1) as ml:
        await ml.search("a")
        for _ in range(5):
            with pytest.raises(BudgetExhausted):
                await ml.search("b")
    assert reserves["n"] == 2  # 1 OK + 1 que descubre el tope; el resto ni consulta


async def test_budget_is_shared_across_runs_of_the_same_day():
    rec = Recorder(lambda r: httpx.Response(200, json={"results": []}))
    async with rec.market(budget=3) as ml:
        await ml.search("a")
        await ml.search("b")
    async with rec.market(budget=3) as ml2:
        await ml2.search("c")
        with pytest.raises(BudgetExhausted):
            await ml2.search("d")


# ─── vendedores ────────────────────────────────────────────────────────────


async def test_seller_sales_are_cached_for_30_days():
    rec = Recorder(lambda r: httpx.Response(
        200, json={"seller_reputation": {"transactions": {"completed": 321}}}))
    async with rec.market() as ml:
        assert await ml.seller_sales("77") == 321
        assert await ml.seller_sales("77") == 321
    assert rec.paths == ["/users/77"]


async def test_stale_seller_cache_is_refetched():
    with Session(engine) as s:
        s.add(MlSellerCache(seller_id="77", completed_sales=5,
                            fetched_at=utcnow() - timedelta(days=31)))
        s.commit()
    rec = Recorder(lambda r: httpx.Response(
        200, json={"seller_reputation": {"transactions": {"completed": 900}}}))
    async with rec.market() as ml:
        assert await ml.seller_sales("77") == 900
    with Session(engine) as s:
        assert s.get(MlSellerCache, "77").completed_sales == 900


async def test_seller_without_reputation_is_unknown_not_an_error():
    rec = Recorder(lambda r: httpx.Response(404))
    async with rec.market() as ml:
        assert await ml.seller_sales("88") is None


# ─── sondas ────────────────────────────────────────────────────────────────


async def test_listing_prices_probe_is_recorded_once():
    rec = Recorder(lambda r: httpx.Response(403, json={"message": "forbidden"}))
    assert market_ml.probe_recorded(market_ml.PROBE_LISTING_PRICES_KEY) is False
    async with rec.market() as ml:
        payload = await ml.probe_listing_prices("MLA1234")
    assert payload["status"] == 403
    assert rec.paths == ["/sites/MLA/listing_prices"]
    assert market_ml.probe_recorded(market_ml.PROBE_LISTING_PRICES_KEY) is True
    with Session(engine) as s:
        stored = json.loads(s.get(Setting, market_ml.PROBE_LISTING_PRICES_KEY).value)
    assert stored["category_id"] == "MLA1234" and "at" in stored


def test_budget_status_for_the_dashboard(monkeypatch):
    from app import runtime

    monkeypatch.setattr(runtime, "get", lambda key: 10 if key == "pm_ml_daily_budget" else None)
    daily_budget.reserve(market_ml.ML_COUNTER_KEY, 10)
    assert market_ml.ml_budget_status() == {"used": 1, "budget": 10, "remaining": 9}
