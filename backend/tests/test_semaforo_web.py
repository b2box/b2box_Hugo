"""El semáforo con la fuente "ML web", los SIMILARES, los deshabilitados y
"No es el mismo", de punta a punta. Vendure, ML (API y web), CLIP y el juez son
dobles: nada sale a la red ni se abre un navegador.

Reusa el mundo de test_price_monitor.py (catálogo de 6 productos, ML de mentira,
scorer por URL de foto)."""

from __future__ import annotations

import json
import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.db.models import MarketMatchFeedback, PriceMonitorRun  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.ingest import browser_fetch  # noqa: E402
from app.pricing import daily_budget, market_judge, market_match, market_ml_web, price_monitor  # noqa: E402
from tests.ml_web_fixtures import ANTIBOT_HTML, ld_product, listing_html, page, polycard  # noqa: E402
from tests.test_price_monitor import (  # noqa: E402,F401
    FakeVendure, _candidate, _listing, _product, _runs, _set, _snaps, world)

PHOTO = "https://http2.mlstatic.com/D_NQ_NP_{}-F.jpg"


class WebWorld:
    """Qué devuelve la web de ML por slug de búsqueda, y qué se le pidió."""

    def __init__(self):
        self.pages: dict[str, object] = {}
        self.calls: list[str] = []
        self.sources: list[market_ml_web.MlWebSource] = []

    async def fetch(self, url):
        self.calls.append(url)
        slug = url.rsplit("/", 1)[-1]
        item = self.pages.get(slug, page(listing_html([])))
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def webw(world, monkeypatch):
    w = WebWorld()

    def from_runtime(on_reserve=None):
        async def sleep(_):
            return None

        src = market_ml_web.MlWebSource(
            budget=int(price_monitor.runtime.get("pm_ml_web_daily_budget")),
            max_results=int(price_monitor.runtime.get("pm_ml_web_max_results")),
            concurrency=1, pause_s=0, block_streak=int(price_monitor.runtime.get("pm_ml_web_block_streak")),
            on_reserve=on_reserve, fetcher=w.fetch, sleep=sleep)
        w.sources.append(src)
        return src

    monkeypatch.setattr(market_ml_web, "disabled_reason", lambda: None)
    monkeypatch.setattr(market_ml_web, "from_runtime", from_runtime)
    browser_fetch.reset_circuit()
    FakeVendure.products = [_product("3", "Producto raro")]
    world.web = w
    yield world
    browser_fetch.reset_circuit()


def _web_page(*cards, nbytes=350_000, ld=None):
    return page(listing_html(list(cards), ld=ld), nbytes=nbytes)


def _card(ref, title, price, *, photo=None, seller="Tienda Uno", sold=500, **kw):
    return polycard(ref, title, price, picture=photo or f"{ref[3:]}-MLA1_012025", seller=seller, sold=sold, **kw)


def _score(world, ref, score, photo=None):
    world.image_scores[PHOTO.format(photo or f"{ref[3:]}-MLA1_012025")] = score


# ─── la web resuelve lo que la API no tiene ───────────────────────────────


async def test_web_fills_in_what_the_api_has_no_card_for(webw):
    webw.web.pages["producto-raro"] = _web_page(
        _card("MLA901", "Producto Raro Premium", 250.0), _card("MLA902", "Producto Raro", 300.0, seller="Tienda Dos"))
    _score(webw, "MLA901", 0.91)
    _score(webw, "MLA902", 0.88)

    result = await price_monitor.run_price_monitor()

    s = _snaps()["3"]
    assert s.ml_status == "ok" and s.match_origin == "web" and s.color == "verde"
    assert (s.ml_min_cents, s.ml_median_cents, s.ml_listing_count, s.ml_seller_count) == (25_000, 27_500, 2, 2)
    assert s.est_margin_pct == pytest.approx(139.25)         # (275 − 13 % − 100) / 100
    assert s.match_source == "clip"
    assert (s.web_searches, s.web_bytes, s.web_state) == (1, 350_000, "ok")
    assert s.candidates_count == 2
    ids = {m["ml_id"]: m for m in json.loads(s.matched_listings)}
    assert set(ids) == {"MLA901", "MLA902"}
    first = ids["MLA901"]
    assert (first["origin"], first["category"], first["prices_cents"], first["seller"]) == (
        "web", "igual", [25_000], "Tienda Uno")
    assert first["image_score"] == pytest.approx(0.91) and first["name_score"] > 0.5
    [run] = _runs()
    assert (run.web_searches, run.web_bytes, run.n_web_ok, run.web_status) == (1, 350_000, 1, "ok")
    assert daily_budget.used_today(market_ml_web.WEB_COUNTER_KEY) == 1
    assert result["counts"]["ok"] == 1
    assert FakeVendure.forbidden == [] and world_graphql(webw) == []


def world_graphql(w):
    return w.graphql_calls


async def test_the_web_is_not_touched_when_the_api_already_has_an_igual(webw):
    FakeVendure.products = [_product("1", "Organizador cocina")]
    await price_monitor.run_price_monitor()
    s = _snaps()["1"]
    assert s.ml_status == "ok" and s.match_origin == "api" and s.web_state is None
    assert webw.web.calls == [] and daily_budget.used_today(market_ml_web.WEB_COUNTER_KEY) == 0


async def test_the_web_runs_when_api_matches_have_no_usable_sellers(webw):
    FakeVendure.products = [_product("1", "Organizador cocina")]
    webw.ml.items["MLA1"] = []                    # ficha igual, sin vendedores
    webw.web.pages["organizador-cocina"] = _web_page(_card("MLA903", "Organizador de cocina", 200.0))
    _score(webw, "MLA903", 0.9)
    await price_monitor.run_price_monitor()
    s = _snaps()["1"]
    assert s.ml_status == "ok" and s.match_origin == "web" and s.ml_median_cents == 20_000


async def test_the_shorter_query_is_tried_when_the_listing_is_empty(webw):
    FakeVendure.products = [_product("3", "Organizador Doble Ajustable 3 Niveles 40x30 Blanco")]
    webw.web.pages["organizador-doble-ajustable-3"] = _web_page(_card("MLA904", "Organizador doble", 150.0))
    _score(webw, "MLA904", 0.9)
    await price_monitor.run_price_monitor()
    assert [c.rsplit("/", 1)[-1] for c in webw.web.calls] == [
        "organizador-doble-ajustable-3-niveles-40x30-blanco", "organizador-doble-ajustable-3"]
    s = _snaps()["3"]
    assert s.ml_status == "ok" and s.web_searches == 2


# ─── solo lo IGUAL cuenta; lo SIMILAR se guarda aparte ────────────────────


async def test_similar_publications_never_touch_price_or_color(webw):
    cards_equal = [_card("MLA901", "Producto Raro", 250.0)]
    webw.web.pages["producto-raro"] = _web_page(*cards_equal)
    _score(webw, "MLA901", 0.91)
    await price_monitor.run_price_monitor()
    without = _snaps()["3"]

    # La misma búsqueda, más un pack x6 (mismo producto, otra cantidad) a mitad de precio.
    webw.web.pages["producto-raro"] = _web_page(*cards_equal, _card("MLA905", "Pack X6 Producto Raro", 90.0))
    _score(webw, "MLA905", 0.95)
    _set("pm_ml_concurrency", 1)
    await price_monitor.run_price_monitor()
    with_similar = _snaps(_runs()[1].id)["3"]

    assert with_similar.similar_count == 1
    similar = {m["ml_id"]: m for m in json.loads(with_similar.similar_listings)}
    assert similar["MLA905"]["differences"] == ["cantidad"] and similar["MLA905"]["source"] == "specs"
    assert similar["MLA905"]["price_cents"] == 9_000       # se muestra, pero no se usa
    assert "MLA905" not in with_similar.matched_listings
    for field in ("ml_median_cents", "ml_min_cents", "ml_listing_count", "ml_seller_count",
                  "est_margin_pct", "color", "ml_status"):
        assert getattr(with_similar, field) == getattr(without, field), field
    [_, run] = _runs()
    assert run.n_con_similares == 1


async def test_a_product_with_only_similar_publications_stays_without_data(webw):
    webw.web.pages["producto-raro"] = _web_page(_card("MLA905", "Pack X6 Producto Raro", 90.0))
    _score(webw, "MLA905", 0.95)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert s.ml_status == "no_data" and s.color == "sin_dato" and s.ml_median_cents is None
    assert "ML web: ninguna publicación es igual (1 similares)" in s.ml_error
    assert s.similar_count == 1 and s.match_origin is None


async def test_measures_from_vendure_make_a_publication_similar(webw):
    from app.pricing.semaforo import PricedVariant

    prod = _product("3", "Producto raro")
    prod.priced_variants = [PricedVariant(id="v3", name="", sku="", price_with_tax_cents=10_000,
                                          currency="ARS", specs={"length": 40.0, "width": 30.0, "weight": 0.5})]
    FakeVendure.products = [prod]
    webw.web.pages["producto-raro"] = _web_page(
        _card("MLA901", "Producto Raro 30x40 cm", 250.0), _card("MLA902", "Producto Raro 60x80 cm", 400.0))
    _score(webw, "MLA901", 0.91)
    _score(webw, "MLA902", 0.9)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert [m["ml_id"] for m in json.loads(s.matched_listings)] == ["MLA901"]
    [sim] = json.loads(s.similar_listings)
    assert (sim["ml_id"], sim["differences"]) == ("MLA902", ["medida"])
    assert json.loads(s.our_specs) == {"length": 40.0, "width": 30.0, "weight": 0.5}


async def test_the_spec_check_can_be_turned_off(webw):
    _set("pm_spec_check", 0)
    webw.web.pages["producto-raro"] = _web_page(_card("MLA905", "Pack X6 Producto Raro", 90.0))
    _score(webw, "MLA905", 0.95)
    await price_monitor.run_price_monitor()
    assert _snaps()["3"].ml_status == "ok"


async def test_api_cards_with_another_pack_are_similar_too(world):
    FakeVendure.products = [_product("1", "Organizador cocina")]
    world.ml.search["Organizador cocina"] = [
        _candidate("MLA1", "Organizador de cocina"), _candidate("MLA7", "Pack x6 Organizador de cocina")]
    world.image_scores["https://http2.mlstatic.com/D_NQ_MLA7.jpg"] = 0.93
    world.ml.items["MLA7"] = [_listing("I7", "101", 90.0)]
    await price_monitor.run_price_monitor()
    s = _snaps()["1"]
    assert [m["ml_id"] for m in json.loads(s.matched_listings)] == ["MLA1"]
    [sim] = json.loads(s.similar_listings)
    assert sim["ml_id"] == "MLA7" and sim["origin"] == "api" and sim["differences"] == ["cantidad"]
    assert s.ml_min_cents == 20_000                 # los 90 pesos del pack no entraron


# ─── marca, con el juez ───────────────────────────────────────────────────


@pytest.fixture
def judge(monkeypatch):
    calls = []
    monkeypatch.setattr(market_judge, "enabled", lambda: True)
    monkeypatch.setattr(market_judge, "make_client", lambda: type("C", (), {"close": lambda s: None})())
    _set("pm_vision_max_calls", 5)

    async def fake_judge(our_name, photos, candidates, *, max_calls, on_reserve=None, **kw):
        calls.append([(c.ml_id, c.brand) for c in candidates])
        assert await daily_budget.reserve_async(market_judge.LLM_COUNTER_KEY, max_calls, None, on_reserve)
        return market_judge.JudgeResult(verdicts={
            c.ml_id: market_judge.JudgeVerdict(
                c.ml_id, c.brand != "Stanley", 0.9, "marca conocida" if c.brand == "Stanley" else "genérica",
                "similar" if c.brand == "Stanley" else "igual", ("marca",) if c.brand == "Stanley" else ())
            for c in candidates})

    monkeypatch.setattr(market_judge, "judge", fake_judge)
    return calls


async def test_a_known_brand_on_the_web_is_similar_when_the_judge_is_on(webw, judge):
    ld = [ld_product("Producto Raro Termo", "https://www.mercadolibre.com.ar/x/p/MLA29003349", 400, brand="Stanley"),
          ld_product("Producto Raro Genérico", "https://articulo.mercadolibre.com.ar/MLA-902902-x", 200, brand="Genérica")]
    webw.web.pages["producto-raro"] = _web_page(
        _card("MLA901", "Producto Raro Termo", 400.0, catalog="MLA29003349", url="www.mercadolibre.com.ar/x/p/MLA29003349"),
        _card("MLA902902", "Producto Raro Genérico", 200.0, url="articulo.mercadolibre.com.ar/MLA-902902-x"), ld=ld)
    _score(webw, "MLA901", 0.92)
    _score(webw, "MLA902902", 0.91)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert [m["ml_id"] for m in json.loads(s.matched_listings)] == ["MLA902902"]
    [sim] = json.loads(s.similar_listings)
    assert (sim["ml_id"], sim["brand"], sim["differences"], sim["source"]) == ("MLA901", "Stanley", ["marca"], "llm")
    assert s.ml_median_cents == 20_000
    # "Genérica" no es una marca con valor propio: ni se le pregunta al juez por ella.
    assert judge == [[("MLA901", "Stanley")]]


async def test_without_the_judge_a_branded_rule_match_stays_igual(webw):
    ld = [ld_product("Producto Raro Termo", "https://www.mercadolibre.com.ar/x/p/MLA29003349", 400, brand="Stanley")]
    webw.web.pages["producto-raro"] = _web_page(
        _card("MLA901", "Producto Raro Termo", 400.0, catalog="MLA29003349", url="www.mercadolibre.com.ar/x/p/MLA29003349"), ld=ld)
    _score(webw, "MLA901", 0.92)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert s.ml_status == "ok" and json.loads(s.matched_listings)[0]["brand"] == "Stanley"


# ─── bloqueos, corte nocturno y cupo ──────────────────────────────────────


async def test_a_block_leaves_the_product_without_data_and_the_run_goes_on(webw):
    FakeVendure.products = [_product("3", "Producto raro"), _product("6", "Taza ceramica")]
    webw.ml.search["Taza ceramica"] = []
    webw.web.pages["producto-raro"] = page(ANTIBOT_HTML)
    webw.web.pages["taza-ceramica"] = _web_page(_card("MLA910", "Taza Ceramica", 80.0))
    _score(webw, "MLA910", 0.9)
    _set("pm_ml_concurrency", 1)
    await price_monitor.run_price_monitor()
    blocked, fine = _snaps()["3"], _snaps()["6"]
    assert blocked.ml_status == "no_data" and blocked.color == "sin_dato" and blocked.web_state == "blocked"
    assert "ML web: ML pidió verificación anti-bot" in blocked.ml_error
    assert fine.ml_status == "ok" and fine.match_origin == "web"
    [run] = _runs()
    assert run.status == "ok" and run.web_blocked == 1 and run.web_status == "ok"


async def test_consecutive_blocks_cut_the_web_for_the_night(webw):
    FakeVendure.products = [_product(str(i), f"Producto raro {i}") for i in range(10, 18)]
    for p in FakeVendure.products:
        webw.ml.search[p.name] = []
        webw.web.pages[p.name.lower().replace(" ", "-")] = page(ANTIBOT_HTML)
    _set("pm_ml_concurrency", 1)
    _set("pm_ml_web_block_streak", 3)
    await price_monitor.run_price_monitor()
    states = [s.web_state for s in sorted(_snaps().values(), key=lambda s: int(s.product_id))]
    assert states == ["blocked"] * 3 + ["off"] * 5
    assert len(webw.web.calls) == 3
    [run] = _runs()
    assert run.web_blocked == 3 and "cortada por esta noche" in run.web_status
    assert run.status == "ok"                                    # no es culpa de ML: no hay degraded
    assert all(s.ml_status == "no_data" for s in _snaps().values())


async def test_the_proxy_failing_is_a_clear_reason_not_a_crash(webw):
    webw.web.pages["producto-raro"] = browser_fetch.BrowserUnavailable("proxy 407")
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert s.ml_status == "no_data" and s.web_state == "error" and "proxy 407" in s.ml_error


async def test_the_daily_web_budget_is_never_exceeded(webw):
    FakeVendure.products = [_product(str(i), f"Producto raro {i}") for i in range(10, 15)]
    for p in FakeVendure.products:
        webw.ml.search[p.name] = []
    _set("pm_ml_concurrency", 1)
    _set("pm_ml_web_daily_budget", 2)
    await price_monitor.run_price_monitor()
    assert len(webw.web.calls) == 2 and daily_budget.used_today(market_ml_web.WEB_COUNTER_KEY) == 2
    assert sorted(s.web_state for s in _snaps().values()).count("budget") + \
        sorted(s.web_state for s in _snaps().values()).count("off") == 3
    assert "sin cupo" in _runs()[0].web_status


async def test_web_unmeasured_products_go_first_next_night(webw):
    FakeVendure.products = [_product("3", "Producto raro"), _product("6", "Taza ceramica")]
    webw.ml.search["Taza ceramica"] = []
    webw.web.pages["producto-raro"] = page(ANTIBOT_HTML)
    webw.web.pages["taza-ceramica"] = _web_page()           # listado vacío: medido de verdad
    _set("pm_ml_concurrency", 1)
    await price_monitor.run_price_monitor()
    webw.ml.calls.clear()
    await price_monitor.run_price_monitor()
    assert [c for c in webw.ml.calls if c.startswith("search:")][0] == "search:Producto raro"


# ─── sin proxy no se intenta ──────────────────────────────────────────────


async def test_without_proxy_the_web_is_off_with_a_warning(world, monkeypatch, caplog):
    called = []
    monkeypatch.setattr(browser_fetch, "available", lambda: True)
    monkeypatch.setattr(browser_fetch, "proxy_configured", lambda: False)
    monkeypatch.setattr(market_ml_web, "from_runtime", lambda *a, **k: called.append(1))
    FakeVendure.products = [_product("3", "Producto raro")]
    import logging

    with caplog.at_level(logging.WARNING, logger="app.pricing.price_monitor"):
        await price_monitor.run_price_monitor()
    assert called == []
    assert "ML web apagada: falta BROWSER_PROXY" in caplog.text
    [run] = _runs()
    assert run.web_status.startswith("apagada: falta BROWSER_PROXY") and run.web_searches == 0
    assert _snaps()["3"].ml_status == "no_data" and _snaps()["3"].web_state is None


# ─── deshabilitados ───────────────────────────────────────────────────────


async def test_disabled_products_are_measured_and_marked_by_default(world):
    _set("pm_include_disabled", 1)
    FakeVendure.products = [_product("1", "Organizador cocina"),
                            _product("4", "Taza ceramica", enabled=False)]
    await price_monitor.run_price_monitor()
    snaps = _snaps()
    assert set(snaps) == {"1", "4"}
    assert snaps["1"].product_enabled is True and snaps["4"].product_enabled is False
    # Sombra de verdad: ni siquiera un deshabilitado lleva a una escritura.
    assert FakeVendure.forbidden == [] and world.graphql_calls == []


async def test_disabled_products_can_be_left_out(world):
    _set("pm_include_disabled", 0)
    FakeVendure.products = [_product("1", "Organizador cocina"),
                            _product("4", "Taza ceramica", enabled=False)]
    await price_monitor.run_price_monitor()
    assert set(_snaps()) == {"1"}


def test_the_setting_defaults_to_measuring_disabled_products(monkeypatch):
    from app.config import get_settings

    assert get_settings().pm_include_disabled == 1
    assert get_settings().pm_ml_web_daily_budget == 2000


# ─── "No es el mismo" ─────────────────────────────────────────────────────


async def test_not_the_same_drops_the_publication_and_recalculates(webw):
    webw.web.pages["producto-raro"] = _web_page(
        _card("MLA901", "Producto Raro Premium", 250.0), _card("MLA902", "Producto Raro", 400.0, seller="Tienda Dos"))
    _score(webw, "MLA901", 0.91)
    _score(webw, "MLA902", 0.9)
    await price_monitor.run_price_monitor()
    snap = _snaps()["3"]
    assert snap.ml_median_cents == 32_500

    removed = price_monitor.drop_listing(snap, "MLA901")
    assert removed["ml_id"] == "MLA901" and removed["prices_cents"] == [25_000]
    assert (snap.ml_median_cents, snap.ml_min_cents, snap.ml_listing_count, snap.ml_seller_count) == (
        40_000, 40_000, 1, 1)
    assert snap.est_margin_pct == pytest.approx(248.0) and snap.color == "verde"
    assert [m["ml_id"] for m in json.loads(snap.matched_listings)] == ["MLA902"]

    price_monitor.drop_listing(snap, "MLA902")
    assert snap.ml_status == "no_data" and snap.color == "sin_dato" and snap.ml_median_cents is None
    assert snap.ml_listing_count == 0 and "No es el mismo" in snap.ml_error
    assert price_monitor.drop_listing(snap, "MLA777") is None


async def test_dropping_a_similar_leaves_the_price_alone(webw):
    webw.web.pages["producto-raro"] = _web_page(
        _card("MLA901", "Producto Raro", 250.0), _card("MLA905", "Pack X6 Producto Raro", 90.0))
    _score(webw, "MLA901", 0.91)
    _score(webw, "MLA905", 0.95)
    await price_monitor.run_price_monitor()
    snap = _snaps()["3"]
    before = (snap.ml_median_cents, snap.color, snap.est_margin_pct)
    assert price_monitor.drop_listing(snap, "MLA905")["category"] == "similar"
    assert (snap.ml_median_cents, snap.color, snap.est_margin_pct) == before
    assert snap.similar_count == 0 and snap.similar_listings is None


async def test_an_excluded_publication_is_ignored_in_the_next_runs(webw):
    webw.web.pages["producto-raro"] = _web_page(
        _card("MLA901", "Producto Raro Premium", 250.0), _card("MLA902", "Producto Raro", 400.0))
    _score(webw, "MLA901", 0.91)
    _score(webw, "MLA902", 0.9)
    with Session(engine) as s:
        s.add(MarketMatchFeedback(product_id="3", ml_id="MLA901"))
        s.add(MarketMatchFeedback(product_id="99", ml_id="MLA902"))      # de OTRO producto
        s.commit()
    await price_monitor.run_price_monitor()
    snap = _snaps()["3"]
    assert [m["ml_id"] for m in json.loads(snap.matched_listings)] == ["MLA902"]
    assert snap.candidates_count == 1 and snap.ml_median_cents == 40_000


async def test_an_excluded_api_card_is_ignored_too(world):
    FakeVendure.products = [_product("1", "Organizador cocina")]
    with Session(engine) as s:
        s.add(MarketMatchFeedback(product_id="1", ml_id="MLA1"))
        s.commit()
    await price_monitor.run_price_monitor()
    s1 = _snaps()["1"]
    assert s1.ml_status == "no_data" and "no devolvió fichas" in s1.ml_error
