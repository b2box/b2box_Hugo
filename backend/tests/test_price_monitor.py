"""Job `price_monitor` (semáforo en modo sombra) de punta a punta.

Vendure, Mercado Libre, CLIP y el juez LLM son dobles: nada sale a la red. El
transporte GraphQL del VendureClient real queda interceptado, así que si el
job intentara cualquier operación contra Vendure (updateProduct, disable…)
además de la lectura de precios, el test lo ve.

Criterios de la etapa sombra que se cubren acá:
  * una fila de `price_monitor_run` por corrida y una `market_price_snapshot`
    por producto habilitado, siempre con `ml_status`;
  * cero escrituras en Vendure;
  * mediana/mínimo/cantidad/color con dato, `sin_dato` sin match;
  * 429 simulado → `failed` sin abortar; >20 % failed → `degraded`;
  * reinicio a mitad → retoma el mismo run_id;
  * requests ≤ budget;
  * cambiar pm_green_min_pct recolorea sin redeploy;
  * prune_price_history no toca las tablas del semáforo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import runtime  # noqa: E402
from app.clock import utcnow  # noqa: E402
from app.db.models import (  # noqa: E402
    ImageEmbedCache,
    MarketPriceSnapshot,
    MlSellerCache,
    PriceHistory,
    PriceMonitorRun,
    Setting,
)
from app.db.session import engine, init_db  # noqa: E402
from app.ingest import meli  # noqa: E402
from app.pricing import daily_budget, market_judge, market_match, market_ml, price_monitor  # noqa: E402
from app.pricing.market_ml import MlMarket  # noqa: E402
from app.pricing.semaforo import PricedVariant  # noqa: E402
from app.scheduler import jobs  # noqa: E402
from app.vendure import client as vendure_client_mod  # noqa: E402
from app.vendure.client import VendureProduct  # noqa: E402

ML_IMG = "https://http2.mlstatic.com/D_NQ_{}.jpg"
# Las reales, antes de que el fixture `world` las reemplace.
_REAL_INDEXED = market_match.indexed


# ─── dobles ─────────────────────────────────────────────────────────────────


def _product(pid: str, name: str, price: int | None = 10_000, enabled: bool = True,
             currency: str = "ARS") -> VendureProduct:
    variants = [PricedVariant(id=f"v{pid}", name="", sku="", price_with_tax_cents=price,
                              currency=currency)] if price else []
    return VendureProduct(
        id=pid, name=name, slug=f"p-{pid}", description="", enabled=enabled, source_url=None,
        image_urls=[f"https://cdn.b2box/{pid}.jpg"], product_code=f"BX{pid}",
        featured_image_url=f"https://cdn.b2box/{pid}.jpg", first_variant_price_cents=price,
        variant_count=1, priced_variants=variants,
    )


class FakeVendure:
    """Lo único que el semáforo le puede pedir a Vendure en sombra: la lista
    de precios. Cualquier otro método queda registrado y revienta."""
    products: list[VendureProduct] = []
    forbidden: list[str] = []
    fail_with: Exception | None = None

    def __init__(self, *a, **kw):
        pass

    async def fetch_all_products_priced(self, concurrency=None):  # noqa: ARG002
        if FakeVendure.fail_with:
            raise FakeVendure.fail_with
        return list(FakeVendure.products)

    def __getattr__(self, name):
        async def _forbidden(*a, **kw):
            FakeVendure.forbidden.append(name)
            raise AssertionError(f"el semáforo en sombra no puede llamar VendureClient.{name}")
        return _forbidden


class FakeML:
    """Mercado Libre de mentira, por path. Valores int = status HTTP."""

    def __init__(self):
        self.search: dict[str, list[dict] | int] = {}
        self.items: dict[str, list[dict] | int] = {}
        self.users: dict[str, int] = {}
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/products/search":
            q = request.url.params["q"]
            self.calls.append(f"search:{q}")
            resp = self.search.get(q, [])
            return httpx.Response(resp) if isinstance(resp, int) else httpx.Response(200, json={"results": resp})
        if path.startswith("/products/") and path.endswith("/items"):
            pid = path.split("/")[2]
            self.calls.append(f"items:{pid}")
            resp = self.items.get(pid, [])
            return httpx.Response(resp) if isinstance(resp, int) else httpx.Response(200, json={"results": resp})
        if path.startswith("/users/"):
            sid = path.split("/")[2]
            self.calls.append(f"users:{sid}")
            sales = self.users.get(sid)
            if sales is None:
                return httpx.Response(404)
            return httpx.Response(200, json={"seller_reputation": {"transactions": {"completed": sales}}})
        if path == "/sites/MLA/listing_prices":
            self.calls.append("listing_prices")
            return httpx.Response(403, json={"message": "forbidden"})
        raise AssertionError(f"path de ML inesperado: {path}")


def _candidate(ml_id: str, name: str) -> dict:
    return {"id": ml_id, "name": name, "permalink": f"https://www.mercadolibre.com.ar/p/{ml_id}",
            "pictures": [{"url": ML_IMG.format(ml_id)}]}


def _listing(item: str, seller: str, price: float, **extra) -> dict:
    return {"item_id": item, "seller_id": seller, "price": price, "currency_id": "ARS",
            "category_id": "MLA5725", **extra}


@pytest.fixture
def world(monkeypatch):
    """Catálogo + ML de referencia. Cada test lo ajusta a lo que necesita.

    P1 tiene match claro (verde), P2 recibe 429 siempre, P3 no está en ML,
    P4 está deshabilitado, P5 no tiene precio y P6 solo trae impostores.
    """
    init_db()
    with Session(engine) as s:
        for model in (MarketPriceSnapshot, PriceMonitorRun, MlSellerCache, Setting,
                      ImageEmbedCache, PriceHistory):
            for row in s.exec(select(model)).all():
                s.delete(row)
        s.commit()
    runtime.invalidate()
    # Estos tests describen la sombra de siempre (solo habilitados); los
    # deshabilitados tienen sus propios tests en test_semaforo_web.py.
    runtime.set_value("pm_include_disabled", 0)

    FakeVendure.products = [
        _product("1", "Organizador cocina"),
        _product("2", "Lampara LED escritorio"),
        _product("3", "Producto raro"),
        _product("4", "Deshabilitado", enabled=False),
        _product("5", "Sin precio", price=None),
        _product("6", "Taza ceramica"),
    ]
    FakeVendure.forbidden = []
    FakeVendure.fail_with = None
    monkeypatch.setattr(price_monitor, "VendureClient", FakeVendure)

    # Red de seguridad: si algo usara el VendureClient REAL, el test lo ve.
    graphql_calls: list[str] = []

    async def _no_vendure(self, query, variables=None, *, what="", **kw):  # noqa: ARG001
        graphql_calls.append(what or "query")
        raise AssertionError(f"llamada real a Vendure: {what}")

    monkeypatch.setattr(vendure_client_mod.VendureClient, "_execute_with_retry", _no_vendure)

    ml = FakeML()
    ml.search = {
        "Organizador cocina": [_candidate("MLA1", "Organizador de cocina")],
        "Lampara LED escritorio": 429,
        "Producto raro": [],
        "Taza ceramica": [_candidate("MLA6", "Taza de ceramica")],
    }
    ml.items = {"MLA1": [_listing("I1", "101", 200.0), _listing("I2", "102", 240.0),
                         _listing("I3", "103", 220.0)]}
    ml.users = {"101": 1_000, "102": 10}  # 103 sin reputación → cuenta igual
    image_scores = {ML_IMG.format("MLA1"): 0.90, ML_IMG.format("MLA6"): 0.30}

    sleeps: list[float] = []

    async def _sleep(seconds):
        sleeps.append(seconds)

    async def _token():
        return "tok"

    def _market(budget, on_reserve=None):
        transport = httpx.MockTransport(ml.handler)
        return MlMarket(budget=budget, client=httpx.AsyncClient(transport=transport),
                        sleep=_sleep, token_getter=_token, on_reserve=on_reserve)

    async def _scorer(our, urls):  # noqa: ARG001
        return image_scores.get(urls[0])

    async def _no_index():
        return None

    monkeypatch.setattr(price_monitor, "MlMarket", _market)
    monkeypatch.setattr(meli, "enabled", lambda: True)
    monkeypatch.setattr(price_monitor, "_ensure_clip_index", _no_index)
    monkeypatch.setattr(market_match, "clip_index_scorer", _scorer)
    monkeypatch.setattr(market_match, "indexed", lambda product: True)

    class World:
        pass

    w = World()
    w.ml, w.image_scores, w.sleeps, w.graphql_calls = ml, image_scores, sleeps, graphql_calls
    yield w
    runtime.reset_to_default("pm_include_disabled")   # que no le pise el default a otros módulos
    runtime.invalidate()


def _runs() -> list[PriceMonitorRun]:
    with Session(engine) as s:
        return list(s.exec(select(PriceMonitorRun).order_by(PriceMonitorRun.id)))


def _snaps(run_id: int | None = None) -> dict[str, MarketPriceSnapshot]:
    with Session(engine) as s:
        stmt = select(MarketPriceSnapshot)
        if run_id is not None:
            stmt = stmt.where(MarketPriceSnapshot.run_id == run_id)
        return {r.product_id: r for r in s.exec(stmt)}


def _set(key: str, value) -> None:
    runtime.set_value(key, value)


# ─── la corrida ─────────────────────────────────────────────────────────────


async def test_one_run_one_snapshot_per_enabled_product_and_nothing_written(world, caplog):
    with caplog.at_level(logging.ERROR, logger="app.pricing.price_monitor"):
        result = await price_monitor.run_price_monitor(trigger="manual")
    # Regresión: leer el snapshot después de guardarlo reventaba (instancia
    # expirada) y los conteos/checkpoints nunca se actualizaban.
    assert "error no atrapado" not in caplog.text
    assert result["counts"] == {"ok": 1, "no_data": 2, "failed": 1, "skipped": 1}

    [run] = _runs()
    assert result["run_id"] == run.id and run.status == "ok" and run.trigger == "manual"
    snaps = _snaps(run.id)
    assert set(snaps) == {"1", "2", "3", "5", "6"}  # el 4 está deshabilitado
    assert {pid: s.ml_status for pid, s in snaps.items()} == {
        "1": "ok", "2": "failed", "3": "no_data", "5": "skipped", "6": "no_data",
    }
    assert run.total_products == 5 and run.processed == 5
    assert (run.n_ok, run.n_failed, run.n_no_data, run.n_skipped) == (1, 1, 2, 1)
    assert (run.n_verde, run.n_sin_dato) == (1, 4)
    assert run.finished_at is not None
    # Sombra: Vendure solo se leyó.
    assert FakeVendure.forbidden == [] and world.graphql_calls == []


async def test_ok_snapshot_has_prices_margin_color_and_links(world):
    await price_monitor.run_price_monitor()
    s = _snaps()["1"]
    # s2 tiene 10 ventas (< 50) y no cuenta; s3 no tiene reputación y cuenta.
    assert (s.ml_min_cents, s.ml_median_cents, s.ml_listing_count, s.ml_seller_count) == (20_000, 21_000, 2, 2)
    # (21.000 − 13 % − 0 − 10.000) / 10.000
    assert s.est_margin_pct == pytest.approx(82.7)
    assert s.color == "verde" and s.prev_color is None
    assert (s.our_price_cents, s.variant_id, s.tier_used) == (10_000, "v1", "priceWithTax")
    assert (s.commission_pct, s.shipping_cents) == (13.0, 0)
    assert s.match_source == "clip" and s.match_confidence is None
    assert s.image_score_max == pytest.approx(0.90)
    [matched] = json.loads(s.matched_listings)
    assert matched["permalink"] == "https://www.mercadolibre.com.ar/p/MLA1"
    assert matched["listings"] == 2 and matched["source"] == "clip"
    assert (s.product_name, s.product_code) == ("Organizador cocina", "BX1")


async def test_no_match_is_sin_dato_with_the_reason(world):
    await price_monitor.run_price_monitor()
    snaps = _snaps()
    assert snaps["3"].color == "sin_dato" and "no devolvió fichas" in snaps["3"].ml_error
    assert snaps["6"].color == "sin_dato" and "ninguna ficha" in snaps["6"].ml_error
    assert snaps["6"].ml_median_cents is None and snaps["6"].candidates_count == 1


async def test_a_429_marks_the_product_failed_and_the_run_goes_on(world):
    await price_monitor.run_price_monitor()
    s = _snaps()["2"]
    assert s.ml_status == "failed" and s.color == "sin_dato" and "429" in s.ml_error
    assert world.sleeps == [1.0, 2.0, 4.0]  # backoff, sin esperar de verdad
    assert _snaps()["1"].ml_status == "ok"


async def test_more_than_20_percent_failed_makes_the_run_degraded(world):
    world.ml.search["Producto raro"] = 503
    await price_monitor.run_price_monitor()
    [run] = _runs()
    assert run.n_failed == 2 and run.status == "degraded"  # 2/5 = 40 %


async def test_requests_never_exceed_the_daily_budget(world):
    _set("pm_ml_daily_budget", 3)
    _set("pm_ml_concurrency", 1)
    await price_monitor.run_price_monitor()
    [run] = _runs()
    assert run.ml_requests_used <= 3
    assert daily_budget.used_today(market_ml.ML_COUNTER_KEY) <= 3
    skipped = [s for s in _snaps().values() if s.ml_status == "skipped" and "budget" in (s.ml_error or "")]
    assert skipped, "lo que no entra en el budget queda skipped, no failed"
    assert run.status in ("ok", "degraded")


async def test_changing_the_green_cut_recolors_without_redeploy(world):
    await price_monitor.run_price_monitor()
    assert _snaps()["1"].color == "verde"

    _set("pm_green_min_pct", 90)  # desde el dashboard
    await price_monitor.run_price_monitor()
    first, second = _runs()
    again = _snaps(second.id)["1"]
    assert again.color == "amarillo" and again.prev_color == "verde"
    assert first.id != second.id


async def test_restart_mid_run_resumes_the_same_run_id(world):
    # Corrida que quedó a medias: P1 ya tiene snapshot, el proceso murió.
    with Session(engine) as s:
        run = PriceMonitorRun(status="running", trigger="cron", total_products=5, processed=1,
                              ml_requests_used=7)
        s.add(run)
        s.commit()
        s.refresh(run)
        s.add(MarketPriceSnapshot(run_id=run.id, product_id="1", ml_status="ok", color="verde"))
        s.commit()
        run_id = run.id

    assert price_monitor.has_unfinished_run() is True
    result = await price_monitor.run_price_monitor()

    assert result["run_id"] == run_id
    [run] = _runs()
    assert run.status == "ok" and run.resumed_count == 1 and run.trigger == "resume"
    assert set(_snaps(run_id)) == {"1", "2", "3", "5", "6"}
    assert "search:Organizador cocina" not in world.ml.calls  # P1 no se repite
    assert run.ml_requests_used > 7  # suma a lo que ya se había gastado


async def test_an_abandoned_run_is_closed_and_a_new_one_starts(world):
    with Session(engine) as s:
        old = PriceMonitorRun(status="running", started_at=utcnow() - timedelta(hours=25))
        s.add(old)
        s.commit()
        s.refresh(old)
        old_id = old.id
    assert price_monitor.has_unfinished_run() is False  # no se retoma al arrancar

    result = await price_monitor.run_price_monitor()

    old_run, new_run = _runs()
    assert old_run.id == old_id and old_run.status == "failed" and "abandonada" in old_run.error
    assert result["run_id"] == new_run.id and new_run.status == "ok"


async def _cut_after(n_done: int, monkeypatch) -> None:
    """Corre el job y lo corta (como un kill del proceso) cuando arranca el
    producto n_done + 1. Los n_done primeros quedan guardados."""
    gate = asyncio.Event()
    real_eval = price_monitor.evaluate_product
    started = {"n": 0}

    async def slow_eval(ctx, product):
        started["n"] += 1
        if started["n"] == n_done + 1:
            await gate.wait()  # el proceso "muere" acá
        return await real_eval(ctx, product)

    monkeypatch.setattr(price_monitor, "evaluate_product", slow_eval)
    task = asyncio.create_task(price_monitor.run_price_monitor())
    for _ in range(300):
        await asyncio.sleep(0.01)
        if started["n"] > n_done:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(price_monitor, "evaluate_product", real_eval)


async def test_a_cut_keeps_exactly_what_was_spent_and_resume_adds_up(world, monkeypatch):
    """QA bug 7: el consumo de la corrida se perdía si el proceso moría entre
    checkpoints. Ahora cada request se suma a la corrida en la misma
    transacción que reserva el cupo del día."""
    FakeVendure.products = [_product(str(i), "Organizador cocina") for i in range(1, 7)]
    _set("pm_ml_concurrency", 1)

    await _cut_after(3, monkeypatch)

    [run] = _runs()
    spent = daily_budget.used_today(market_ml.ML_COUNTER_KEY)
    assert run.status == "running" and len(_snaps(run.id)) == 3
    assert spent > 0 and run.ml_requests_used == spent  # nada perdido en el corte
    assert run.processed == 3

    await price_monitor.run_price_monitor()

    [run] = _runs()
    assert run.status == "ok" and run.resumed_count == 1 and len(_snaps(run.id)) == 6
    assert run.processed == 6
    assert run.ml_requests_used == daily_budget.used_today(market_ml.ML_COUNTER_KEY) > spent


async def test_pm_mode_1_only_logs_and_stays_in_shadow(world, caplog):
    _set("pm_mode", 1)
    with caplog.at_level(logging.WARNING, logger="app.pricing.price_monitor"):
        await price_monitor.run_price_monitor()
    assert "modo activo todavía no implementado" in caplog.text
    [run] = _runs()
    assert run.mode == 1 and run.status == "ok"
    assert FakeVendure.forbidden == [] and world.graphql_calls == []


async def test_vendure_down_closes_the_run_as_failed(world):
    FakeVendure.fail_with = RuntimeError("connection refused")
    result = await price_monitor.run_price_monitor()
    [run] = _runs()
    assert result["status"] == "failed" and run.status == "failed" and "Vendure" in run.error
    assert _snaps() == {}
    assert price_monitor.has_unfinished_run() is False


async def test_without_ml_credentials_the_run_fails_cleanly(world, monkeypatch):
    monkeypatch.setattr(meli, "enabled", lambda: False)
    await price_monitor.run_price_monitor()
    [run] = _runs()
    assert run.status == "failed" and "MELI_CLIENT_ID" in run.error
    assert world.ml.calls == []


async def test_a_bug_does_not_leave_the_run_running_forever(world, monkeypatch):
    async def boom(*a, **kw):
        raise ValueError("bug")

    monkeypatch.setattr(price_monitor, "_evaluate_catalog", boom)
    result = await price_monitor.run_price_monitor()
    assert result["status"] == "failed"
    assert price_monitor.open_run() is None


async def test_a_second_invocation_while_running_is_ignored(world):
    async with price_monitor.price_monitor_lock:
        assert await price_monitor.run_price_monitor() is None
    assert _runs() == []


async def test_short_query_fallback_when_the_full_name_finds_nothing(world):
    FakeVendure.products = [_product("7", "Organizador Doble Ajustable 3 Niveles 40x30 Blanco")]
    world.ml.search["Organizador Doble Ajustable 3"] = [_candidate("MLA1", "Organizador doble")]
    await price_monitor.run_price_monitor()
    assert _snaps()["7"].ml_status == "ok"
    assert world.ml.calls[:2] == ["search:Organizador Doble Ajustable 3 Niveles 40x30 Blanco",
                                  "search:Organizador Doble Ajustable 3"]


async def test_product_outside_the_clip_index_is_skipped_before_spending_requests(world, monkeypatch):
    monkeypatch.setattr(market_match, "indexed", lambda product: product.id != "1")
    await price_monitor.run_price_monitor()
    s = _snaps()["1"]
    assert s.ml_status == "skipped" and "índice CLIP" in s.ml_error
    assert "search:Organizador cocina" not in world.ml.calls


async def test_our_price_in_another_currency_is_skipped(world):
    FakeVendure.products = [_product("9", "Organizador cocina", currency="USD")]
    await price_monitor.run_price_monitor()
    s = _snaps()["9"]
    assert s.ml_status == "skipped" and "USD" in s.ml_error
    assert world.ml.calls == []


async def test_sold_quantity_in_the_payload_skips_the_users_endpoint(world):
    world.ml.items["MLA1"] = [_listing("I1", "101", 200.0, sold_quantity=500),
                              _listing("I2", "102", 240.0, sold_quantity=3)]
    await price_monitor.run_price_monitor()
    s = _snaps()["1"]
    assert (s.ml_listing_count, s.ml_median_cents) == (1, 20_000)
    assert not any(c.startswith("users:") for c in world.ml.calls)


async def test_first_run_records_the_two_probes_once(world):
    await price_monitor.run_price_monitor()
    with Session(engine) as s:
        sold = json.loads(s.get(Setting, market_ml.PROBE_SOLD_QUANTITY_KEY).value)
        prices = json.loads(s.get(Setting, market_ml.PROBE_LISTING_PRICES_KEY).value)
    assert sold["present"] is False and sold["product"] == "MLA1"
    assert prices["status"] == 403 and prices["category_id"] == "MLA5725"
    assert world.ml.calls.count("listing_prices") == 1

    await price_monitor.run_price_monitor()
    assert world.ml.calls.count("listing_prices") == 1  # no se repite


# ─── juez LLM en la banda ambigua ───────────────────────────────────────────


def _ambiguous_world(world):
    FakeVendure.products = [_product("8", "Soporte celular auto")]
    world.ml.search["Soporte celular auto"] = [_candidate("MLA8", "Soporte celular para auto")]
    world.ml.items["MLA8"] = [_listing("I8", "101", 300.0)]
    world.image_scores[ML_IMG.format("MLA8")] = 0.62  # entre veto y umbral: ambiguo


async def test_ambiguous_band_without_judge_is_no_data(world, monkeypatch):
    _ambiguous_world(world)
    called = []

    async def judge(*a, **kw):
        called.append(1)

    monkeypatch.setattr(market_judge, "judge", judge)
    await price_monitor.run_price_monitor()
    s = _snaps()["8"]
    assert s.ml_status == "no_data" and s.ambiguous_count == 1
    assert called == []  # pm_vision_max_calls = 0 por default


async def test_judge_confirms_the_ambiguous_match_and_its_cost_is_recorded(world, monkeypatch):
    _ambiguous_world(world)
    _set("pm_vision_max_calls", 5)
    seen = {}

    async def judge(our_name, our_images, candidates, *, max_calls, on_reserve=None, **kw):
        seen.update(name=our_name, ids=[c.ml_id for c in candidates], max_calls=max_calls)
        # Como el juez real: reserva el cupo y cuenta la llamada en la corrida.
        assert await daily_budget.reserve_async(market_judge.LLM_COUNTER_KEY, max_calls, None, on_reserve)
        return market_judge.JudgeResult(
            verdicts={"MLA8": market_judge.JudgeVerdict("MLA8", True, 0.8, "mismo soporte")},
            input_tokens=1_000, output_tokens=100, cost_usd=0.00036, model="qwen3-vl-plus",
        )

    monkeypatch.setattr(market_judge, "judge", judge)
    await price_monitor.run_price_monitor()

    s = _snaps()["8"]
    assert s.ml_status == "ok" and s.match_source == "llm" and s.match_confidence == 0.8
    assert seen == {"name": "Soporte celular auto", "ids": ["MLA8"], "max_calls": 5}
    [run] = _runs()
    assert (run.llm_calls, run.llm_input_tokens, run.llm_output_tokens) == (1, 1_000, 100)
    assert run.llm_cost_usd == pytest.approx(0.00036)


class _DummyJudgeClient:
    def __init__(self, registry):
        registry.append(self)
        self.closed = 0

    async def close(self):
        self.closed += 1


@pytest.fixture
def judge_on(monkeypatch):
    """Juez "configurado" (https + key) con un cliente de mentira."""
    clients: list[_DummyJudgeClient] = []
    monkeypatch.setattr(market_judge, "enabled", lambda: True)
    monkeypatch.setattr(market_judge, "make_client", lambda: _DummyJudgeClient(clients))
    _set("pm_vision_max_calls", 5)
    return clients


async def test_the_judge_sees_the_ml_price_and_listings_are_not_fetched_twice(world, monkeypatch, judge_on):
    _ambiguous_world(world)
    seen = {}

    async def judge(our_name, our_images, candidates, *, max_calls, on_reserve=None, client=None, **kw):
        seen["prices"] = {c.ml_id: c.price_cents for c in candidates}
        seen["client"] = client
        await daily_budget.reserve_async(market_judge.LLM_COUNTER_KEY, max_calls, None, on_reserve)
        return market_judge.JudgeResult(
            verdicts={"MLA8": market_judge.JudgeVerdict("MLA8", True, 0.9, "igual")})

    monkeypatch.setattr(market_judge, "judge", judge)
    await price_monitor.run_price_monitor()
    assert seen["prices"] == {"MLA8": 30_000}
    assert world.ml.calls.count("items:MLA8") == 1  # el prefetch se reusa para la mediana
    assert _snaps()["8"].ml_status == "ok"
    [client] = judge_on
    assert seen["client"] is client and client.closed == 1


async def test_one_judge_client_per_run_closed_at_the_end(world, monkeypatch, judge_on):
    _ambiguous_world(world)
    FakeVendure.products.append(_product("10", "Soporte celular auto"))

    async def judge(*a, **kw):
        return None

    monkeypatch.setattr(market_judge, "judge", judge)
    await price_monitor.run_price_monitor()
    assert len(judge_on) == 1 and judge_on[0].closed == 1


async def test_no_prefetch_when_the_judge_will_not_answer(world, monkeypatch):
    _ambiguous_world(world)
    _set("pm_vision_max_calls", 5)  # pero sin base URL / key: enabled() es False

    async def judge(*a, **kw):
        return None

    monkeypatch.setattr(market_judge, "judge", judge)
    await price_monitor.run_price_monitor()
    assert "items:MLA8" not in world.ml.calls


async def test_a_failed_judge_call_counts_in_the_run(world, monkeypatch):
    """Con el juez REAL: la llamada que revienta igual suma en llm_calls."""
    from types import SimpleNamespace

    from app.config import Settings

    _ambiguous_world(world)
    _set("pm_vision_max_calls", 5)
    monkeypatch.setattr(market_judge, "get_settings", lambda: Settings(
        vendure_api_url="https://example.invalid/admin-api", pm_llm_base_url="https://llm.invalid/v1",
        pm_llm_api_key="k"))
    closed = []

    async def boom(**kw):
        raise TimeoutError("30s")

    async def close():
        closed.append(1)

    monkeypatch.setattr(market_judge, "make_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=boom)), close=close))
    await price_monitor.run_price_monitor()
    [run] = _runs()
    assert run.llm_calls == 1 and run.llm_input_tokens == 0
    assert _snaps()["8"].ml_status == "no_data" and closed == [1]


async def test_judge_low_confidence_does_not_promote(world, monkeypatch):
    _ambiguous_world(world)
    _set("pm_vision_max_calls", 5)

    async def judge(*a, **kw):
        return market_judge.JudgeResult(
            verdicts={"MLA8": market_judge.JudgeVerdict("MLA8", True, 0.4, "no sé")})

    monkeypatch.setattr(market_judge, "judge", judge)
    await price_monitor.run_price_monitor()
    assert _snaps()["8"].ml_status == "no_data"


# ─── dashboard ──────────────────────────────────────────────────────────────


async def test_summary_for_the_health_card(world):
    await price_monitor.run_price_monitor()
    out = price_monitor.summary()
    last = out["last_run"]
    assert last["status"] == "ok" and last["total_products"] == 5
    assert last["pct_no_data"] == 40.0 and last["pct_failed"] == 20.0
    assert last["ml_requests_used"] > 0 and last["llm"]["calls"] == 0
    assert out["mode"] == 0 and out["running"] is False and out["judge_enabled"] is False
    assert out["ml_budget"]["used"] == last["ml_requests_used"]


# ─── poda ───────────────────────────────────────────────────────────────────


async def test_prune_price_history_leaves_the_monitor_tables_alone(world, monkeypatch):
    long_ago = utcnow() - timedelta(days=400)
    with Session(engine) as s:
        run = PriceMonitorRun(status="ok", started_at=long_ago)
        s.add(run)
        s.commit()
        s.refresh(run)
        s.add(MarketPriceSnapshot(run_id=run.id, product_id="1", captured_at=long_ago))
        s.add(PriceHistory(product_id="1", source="vendure", price_cents=1, currency="ARS",
                           captured_at=long_ago))
        old = utcnow() - timedelta(days=90)
        s.add(ImageEmbedCache(url="https://http2.mlstatic.com/D_viejo.jpg", vector_b64="x", updated_at=old))
        s.add(ImageEmbedCache(url="https://http2.mlstatic.com/D_nuevo.jpg", vector_b64="x"))
        s.add(ImageEmbedCache(url="https://cdn.b2box/propia.jpg", vector_b64="x", updated_at=old))
        s.commit()

    monkeypatch.setattr(jobs.get_settings(), "price_history_retention_days", 120)
    await jobs.prune_price_history()

    with Session(engine) as s:
        assert s.exec(select(PriceHistory)).all() == []
        assert len(s.exec(select(MarketPriceSnapshot)).all()) == 1
        assert len(s.exec(select(PriceMonitorRun)).all()) == 1
        urls = {r.url for r in s.exec(select(ImageEmbedCache))}
    assert urls == {"https://http2.mlstatic.com/D_nuevo.jpg", "https://cdn.b2box/propia.jpg"}


# ─── scheduler ──────────────────────────────────────────────────────────────


@pytest.fixture
def clean_scheduler():
    yield
    for job in jobs.scheduler.get_jobs():
        jobs.scheduler.remove_job(job.id)


def test_cron_defaults_to_06_utc_and_bad_expressions_fall_back(caplog):
    trigger = jobs._price_monitor_trigger("0 6 * * *")
    nxt = trigger.get_next_fire_time(None, datetime(2026, 10, 8, 7, 0, tzinfo=timezone.utc))
    assert nxt == datetime(2026, 10, 9, 6, 0, tzinfo=timezone.utc)
    with caplog.at_level(logging.ERROR, logger="app.scheduler.jobs"):
        bad = jobs._price_monitor_trigger("cualquier cosa")
    assert "inválido" in caplog.text
    assert bad.get_next_fire_time(None, datetime(2026, 10, 8, 7, 0, tzinfo=timezone.utc)).hour == 6


@pytest.mark.parametrize("last,now,missed", [
    (None, datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc), False),                       # nunca corrió
    (datetime(2026, 10, 8, 7, 30, tzinfo=timezone.utc), datetime(2026, 10, 8, 23, 0, tzinfo=timezone.utc), False),
    (datetime(2026, 10, 7, 7, 30, tzinfo=timezone.utc), datetime(2026, 10, 8, 6, 10, tzinfo=timezone.utc), True),
    (datetime(2026, 10, 9, 7, 30, tzinfo=timezone.utc), datetime(2026, 10, 8, 6, 10, tzinfo=timezone.utc), False),
])
def test_cron_missed(last, now, missed):
    trigger = jobs._price_monitor_trigger("0 6 * * *")
    assert jobs._cron_missed(trigger, last, now) is missed


def test_register_jobs_adds_the_nightly_price_monitor(world, clean_scheduler):
    jobs.register_jobs()
    job = jobs.scheduler.get_job(jobs.PRICE_MONITOR_JOB_ID)
    assert job is not None and job.max_instances == 1
    assert str(job.trigger.fields[5]) == "6"  # hour


def test_register_jobs_resumes_an_unfinished_run_soon(world, clean_scheduler):
    with Session(engine) as s:
        s.add(PriceMonitorRun(status="running"))
        s.commit()
    jobs.register_jobs()
    job = jobs.scheduler.get_job(jobs.PRICE_MONITOR_JOB_ID)
    delta = job.next_run_time - datetime.now(timezone.utc)
    assert timedelta(minutes=4) < delta <= jobs._STARTUP_GRACE


async def test_job_wrapper_marks_the_clock_only_when_the_run_finished(world):
    await jobs.price_monitor(trigger="manual")
    assert jobs._last_job_run(jobs.PRICE_MONITOR_JOB_ID) is not None

    with Session(engine) as s:
        s.delete(s.get(Setting, jobs._LAST_RUN_PREFIX + jobs.PRICE_MONITOR_JOB_ID))
        s.commit()
    FakeVendure.fail_with = RuntimeError("caído")
    await jobs.price_monitor()
    assert jobs._last_job_run(jobs.PRICE_MONITOR_JOB_ID) is None


# ─── QA: bordes que faltaban ────────────────────────────────────────────────

# El fixture `world` reemplaza `_execute_with_retry`; para el test de transporte
# hace falta el método real, capturado al importar (antes de cualquier fixture).
_REAL_EXECUTE = vendure_client_mod.VendureClient._execute_with_retry


def _raw_vendure_product(p: VendureProduct) -> dict:
    variants = [
        {"id": v.id, "name": v.name, "sku": v.sku, "priceWithTax": v.price_with_tax_cents,
         "currencyCode": v.currency, "bulkPriceTiers": []}
        for v in (p.priced_variants or [])
    ]
    return {
        "id": p.id, "name": p.name, "slug": p.slug, "description": "", "enabled": p.enabled,
        "updatedAt": "2026-10-01T00:00:00Z",
        "customFields": {"b2boxProductCode": p.product_code},
        "featuredAsset": {"preview": p.featured_image_url, "source": p.featured_image_url},
        "variantList": {"items": variants, "totalItems": len(variants)},
    }


async def test_shadow_run_with_the_real_vendure_client_only_reads_over_http(world, monkeypatch):
    """Criterio 'cero escrituras' a nivel TRANSPORTE: el job usa el VendureClient
    real y se intercepta el HTTP hacia Vendure. Lo único que puede viajar es la
    query de listado; ni una mutation, tampoco con pm_mode=1."""
    monkeypatch.setattr(price_monitor, "VendureClient", vendure_client_mod.VendureClient)
    monkeypatch.setattr(vendure_client_mod.VendureClient, "_execute_with_retry", _REAL_EXECUTE)
    monkeypatch.setattr(vendure_client_mod.VendureClient, "_shared_bearer", "qa-bearer")  # sin login
    _set("pm_mode", 1)

    sent: list[dict] = []
    # Se intercepta en el transport HTTP del httpx que usa gql (httpx o httpx2,
    # según lo instalado: ver app/vendure/client.py). ML usa su MockTransport y
    # no pasa por acá.
    gx = vendure_client_mod._gql_httpx
    real_handle = gx.AsyncHTTPTransport.handle_async_request

    async def intercept(self, request):
        if request.url.host != "example.invalid":
            return await real_handle(self, request)
        body = json.loads(request.content)
        sent.append(body)
        if not body["query"].lstrip().startswith("query"):
            return gx.Response(500, json={"errors": [{"message": "escritura en sombra"}]}, request=request)
        items = [_raw_vendure_product(p) for p in FakeVendure.products]
        return gx.Response(200, json={"data": {"products": {"items": items, "totalItems": len(items)}}},
                           request=request)

    monkeypatch.setattr(gx.AsyncHTTPTransport, "handle_async_request", intercept)
    result = await price_monitor.run_price_monitor()

    assert result["status"] == "ok"
    assert sent, "el job tenía que leer el catálogo por HTTP"
    for body in sent:
        q = body["query"]
        assert q.lstrip().startswith("query") and "mutation" not in q.lower(), q
        assert "updateProduct" not in q and "bulkPriceTiers" in q
    snaps = _snaps()
    assert set(snaps) == {"1", "2", "3", "5", "6"} and snaps["1"].ml_status == "ok"


async def test_zero_budget_spends_nothing_and_leaves_one_mark_on_the_run(world):
    # Security M2: sin cupo no se escribe un snapshot `skipped` por producto
    # (1.500 filas de ruido); queda una sola marca en la corrida.
    _set("pm_ml_daily_budget", 0)
    result = await price_monitor.run_price_monitor()
    assert world.ml.calls == []
    assert _snaps() == {}
    [run] = _runs()
    assert result["status"] == run.status == "skipped"
    assert "sin cupo" in run.error and run.total_products == 5
    assert run.ml_requests_used == 0 and run.processed == 0
    assert daily_budget.used_today(market_ml.ML_COUNTER_KEY) == 0


async def test_budget_already_spent_today_also_skips_the_run(world):
    _set("pm_ml_daily_budget", 2)
    daily_budget.reserve(market_ml.ML_COUNTER_KEY, 2)
    daily_budget.reserve(market_ml.ML_COUNTER_KEY, 2)
    await price_monitor.run_price_monitor()
    [run] = _runs()
    assert run.status == "skipped" and "usados 2 de 2" in run.error
    assert _snaps() == {} and world.ml.calls == []


def test_budget_status_with_zero_budget_is_zero_not_the_default(world):
    # QA bug 1: con `or`, 0 caía al default del .env (15000).
    _set("pm_ml_daily_budget", 0)
    assert market_ml.ml_budget_status() == {"used": 0, "budget": 0, "remaining": 0}


async def test_usd_listings_are_ignored_and_one_listing_is_its_own_median(world):
    FakeVendure.products = [_product("1", "Organizador cocina")]
    world.ml.items["MLA1"] = [_listing("I1", "101", 200.0),
                              {**_listing("I2", "101", 5.0), "currency_id": "USD"}]
    await price_monitor.run_price_monitor()
    s = _snaps()["1"]
    assert s.ml_status == "ok"
    assert (s.ml_median_cents, s.ml_min_cents, s.ml_listing_count, s.ml_seller_count) == (20_000, 20_000, 1, 1)
    # (20.000 − 13 % de 20.000 − 0 − 10.000) / 10.000 = 74 %: la comisión sale del precio de ML.
    assert s.est_margin_pct == 74.0 and s.color == "verde"


async def test_only_usd_listings_is_no_data(world):
    FakeVendure.products = [_product("1", "Organizador cocina")]
    world.ml.items["MLA1"] = [{**_listing("I1", "101", 5.0), "currency_id": "USD"}]
    await price_monitor.run_price_monitor()
    s = _snaps()["1"]
    assert s.ml_status == "no_data" and s.color == "sin_dato" and s.ml_median_cents is None


async def test_ml_200_with_no_sellers_is_no_data_not_failed(world):
    FakeVendure.products = [_product("1", "Organizador cocina")]
    world.ml.items["MLA1"] = []  # 200 {"results": []}
    await price_monitor.run_price_monitor()
    s = _snaps()["1"]
    assert s.ml_status == "no_data" and s.color == "sin_dato"
    assert s.ml_median_cents is None and s.est_margin_pct is None


async def test_embed_cache_prune_follows_the_runtime_days(world, monkeypatch):
    _set("pm_embed_cache_days", 7)
    now = utcnow()
    with Session(engine) as s:
        s.add(ImageEmbedCache(url="https://http2.mlstatic.com/D_10d.jpg", vector_b64="x",
                              updated_at=now - timedelta(days=10)))
        s.add(ImageEmbedCache(url="https://http2.mlstatic.com/D_5d.jpg", vector_b64="x",
                              updated_at=now - timedelta(days=5)))
        s.add(ImageEmbedCache(url="https://cdn.b2box/propia-vieja.jpg", vector_b64="x",
                              updated_at=now - timedelta(days=400)))
        s.commit()
    # Con la retención de price_history apagada, la poda de fotos de ML corre igual.
    monkeypatch.setattr(jobs.get_settings(), "price_history_retention_days", 0)
    await jobs.prune_price_history()
    with Session(engine) as s:
        urls = {r.url for r in s.exec(select(ImageEmbedCache))}
    assert urls == {"https://http2.mlstatic.com/D_5d.jpg", "https://cdn.b2box/propia-vieja.jpg"}


# ─── QA: productos que no se pueden puntuar, duplicados, rotación, cache ─────


async def test_product_with_photos_but_no_vectors_spends_no_ml_requests(world, monkeypatch):
    """QA bug 2: estar en el índice con foto no alcanza; tiene que haber vector."""
    import numpy as np

    from app.dedup import catalog_index, image_embed

    st = catalog_index._state
    saved = (st.matrix, st.product_ids, st.products, st.vector_ids)
    try:
        st.matrix = np.ones((1, image_embed.EMBED_DIM), dtype=np.float32)
        st.product_ids = ["99"]
        st.products = {"1": _product("1", "Organizador cocina"), "99": _product("99", "Otro")}
        st.vector_ids = frozenset({"99"})
        monkeypatch.setattr(image_embed, "available", lambda: True)
        monkeypatch.setattr(market_match, "indexed", _REAL_INDEXED)
        FakeVendure.products = [_product("1", "Organizador cocina")]
        await price_monitor.run_price_monitor()
    finally:
        st.matrix, st.product_ids, st.products, st.vector_ids = saved
    s = _snaps()["1"]
    assert s.ml_status == "skipped" and "índice CLIP" in s.ml_error
    assert world.ml.calls == []


async def test_a_product_listed_twice_is_evaluated_once(world):
    FakeVendure.products = [_product("1", "Organizador cocina"), _product("1", "Organizador cocina")]
    await price_monitor.run_price_monitor()
    [run] = _runs()
    assert run.total_products == 1 and run.processed == 1
    assert world.ml.calls.count("search:Organizador cocina") == 1


async def test_color_is_decided_on_the_exact_margin(world):
    # Mediana 1.299,96 sin comisión contra nuestro 1.000: 29,996 % → amarillo,
    # aunque guardado/mostrado sea 30,0.
    _set("pm_ml_commission_pct", 0)
    FakeVendure.products = [_product("1", "Organizador cocina", price=100_000)]
    world.ml.items["MLA1"] = [_listing("I1", "101", 1299.96)]
    await price_monitor.run_price_monitor()
    s = _snaps()["1"]
    assert s.est_margin_pct == 30.0 and s.color == "amarillo"


async def test_least_recently_measured_products_go_first(world):
    """Con budget corto, la corrida empieza por lo que hace más que no se mide
    (ok o no_data), y lo nunca medido primero: así todo rota."""
    FakeVendure.products = [_product("1", "Organizador cocina"), _product("3", "Producto raro"),
                            _product("6", "Taza ceramica")]
    with Session(engine) as s:
        old = PriceMonitorRun(status="ok", started_at=utcnow() - timedelta(days=3))
        s.add(old)
        s.commit()
        s.refresh(old)
        s.add(MarketPriceSnapshot(run_id=old.id, product_id="1", ml_status="ok",
                                  captured_at=utcnow() - timedelta(days=1)))
        s.add(MarketPriceSnapshot(run_id=old.id, product_id="3", ml_status="no_data",
                                  captured_at=utcnow() - timedelta(days=2)))
        # Un failed no cuenta como medido.
        s.add(MarketPriceSnapshot(run_id=old.id, product_id="6", ml_status="failed",
                                  captured_at=utcnow() - timedelta(hours=1)))
        s.commit()
    _set("pm_ml_concurrency", 1)
    await price_monitor.run_price_monitor()
    searches = [c for c in world.ml.calls if c.startswith("search:")]
    assert searches == ["search:Taza ceramica", "search:Producto raro", "search:Organizador cocina"]


def test_an_embedding_cache_hit_keeps_the_row_alive():
    """Una foto de ML que se usa todas las noches no se poda ni se re-embebe."""
    import numpy as np

    from app.dedup import image_embed

    url = "https://http2.mlstatic.com/D_usada.jpg"
    vec = np.ones(image_embed.EMBED_DIM, dtype=np.float32)
    vec /= np.linalg.norm(vec)
    with Session(engine) as s:
        s.add(ImageEmbedCache(url=url, vector_b64=image_embed.encode_vector(vec),
                              updated_at=utcnow() - timedelta(days=59)))
        s.commit()
    assert image_embed._db_get(url) is not None
    with Session(engine) as s:
        row = s.get(ImageEmbedCache, url)
        assert row.updated_at > utcnow() - timedelta(minutes=1)
        s.delete(row)
        s.commit()


def test_our_photos_is_one_version_of_the_featured_photo():
    """Vendure da la misma foto como preview y como source: al juez va una."""
    pm = price_monitor
    p = replace(_product("1", "x"), featured_image_url="https://cdn.b2box/assets/preview/1.jpg",
                image_urls=["https://cdn.b2box/assets/source/1.jpg"])
    assert pm._our_photos(p) == ["https://cdn.b2box/assets/preview/1.jpg"]
    no_preview = replace(p, featured_image_url=None)
    assert pm._our_photos(no_preview) == ["https://cdn.b2box/assets/source/1.jpg"]
    http_preview = replace(p, featured_image_url="http://cdn.b2box/assets/preview/1.jpg")
    assert pm._our_photos(http_preview) == ["https://cdn.b2box/assets/source/1.jpg"]
    assert pm._our_photos(replace(p, featured_image_url=None, image_urls=[])) == []
    assert pm._our_photos(replace(p, featured_image_url="http://x/1.jpg", image_urls=["ftp://x/1.jpg"])) == []
