"""«Siempre trae algo»: lo que ML devuelve nunca se descarta.

Cada producto guarda hasta `pm_ml_keep_listings` publicaciones clasificadas en
idénticas / similares / diferentes. El color REAL sigue saliendo solo de lo
idéntico; sin idéntico pero con similares hay un color ESTIMADO aparte; con solo
diferentes no hay color pero se ven; "sin dato" es solo cuando ML no devolvió nada.

Todo con dobles (Vendure, ML API y web, CLIP y el juez): sin red ni navegador."""

from __future__ import annotations

import json
import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app import runtime  # noqa: E402
from app.db.models import MarketMatchFeedback  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import daily_budget, market_judge, price_monitor  # noqa: E402
from tests.test_price_monitor import (  # noqa: E402,F401
    FakeVendure,
    _ambiguous_world,
    _candidate,
    _listing,
    _product,
    _runs,
    _set,
    _snaps,
    world,
)
from tests.test_semaforo_web import _card, _score, _web_page, webw  # noqa: E402,F401

ML_IMG = "https://http2.mlstatic.com/D_NQ_{}.jpg"


def _entries(raw):
    return json.loads(raw) if raw else []


# ─── solo similares: color ESTIMADO, sin color real ───────────────────────


@pytest.fixture
def only_similars(webw):
    """Dos packs distintos del mismo producto (SIMILAR por cantidad), a 250 y 300 pesos."""
    webw.web.pages["producto-raro"] = _web_page(
        _card("MLA921", "Pack X6 Producto Raro", 250.0), _card("MLA922", "Set X3 Producto Raro", 300.0, seller="Dos"))
    _score(webw, "MLA921", 0.95)
    _score(webw, "MLA922", 0.93)
    return webw


async def test_only_similars_get_an_estimated_color_and_no_real_one(only_similars):
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    # el color REAL no existe: lo único que lo sacaría es un idéntico
    assert (s.ml_status, s.color, s.est_margin_pct, s.ml_median_cents, s.ml_min_cents) == (
        "no_data", "sin_dato", None, None, None)
    # el ESTIMADO sale de la mediana de los similares con la misma fórmula de ganancia
    assert s.estimated_median_cents == 27_500 and s.estimated_listing_count == 2
    assert s.estimated_margin_pct == pytest.approx((27_500 * 0.87 - 10_000) / 10_000 * 100, abs=0.01)
    assert (s.estimated_color, s.estimated_from, s.match_state) == ("verde", "similar", "similar")
    assert (s.similar_count, s.other_count) == (2, 0)
    assert {e["ml_id"] for e in _entries(s.similar_listings)} == {"MLA921", "MLA922"}


async def test_the_real_color_counters_do_not_move_with_estimates(only_similars):
    await price_monitor.run_price_monitor()
    [run] = _runs()
    assert (run.n_verde, run.n_amarillo, run.n_rojo, run.n_sin_dato) == (0, 0, 0, 1)    # los reales, intactos
    assert (run.n_ok, run.n_no_data) == (0, 1)
    assert (run.n_est_verde, run.n_est_amarillo, run.n_est_rojo, run.n_solo_diferentes) == (1, 0, 0, 0)
    assert price_monitor.run_to_dict(run)["estimated"] == {"verde": 1, "amarillo": 0, "rojo": 0}


async def test_the_estimate_follows_the_same_thresholds_as_the_real_color(only_similars):
    _set("pm_green_min_pct", 200)                     # 139 % ya no alcanza para verde
    await price_monitor.run_price_monitor()
    assert _snaps()["3"].estimated_color == "amarillo"
    assert _snaps()["3"].color == "sin_dato"


async def test_a_similar_without_a_price_in_pesos_gives_no_estimate(webw):
    webw.web.pages["producto-raro"] = _web_page(
        _card("MLA921", "Pack X6 Producto Raro", 250.0, currency="USD"))
    _score(webw, "MLA921", 0.95)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert s.similar_count == 1 and s.estimated_color is None and s.match_state == "similar"
    assert _entries(s.similar_listings)[0]["price_cents"] is None          # sin precio en pesos: no se inventa


async def test_a_similar_with_few_sales_is_shown_but_does_not_count_for_the_estimate(webw):
    webw.web.pages["producto-raro"] = _web_page(
        _card("MLA921", "Pack X6 Producto Raro", 250.0, sold=3),            # pocas ventas
        _card("MLA922", "Set X3 Producto Raro", 400.0, sold=900))
    _score(webw, "MLA921", 0.95)
    _score(webw, "MLA922", 0.93)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert s.estimated_median_cents == 40_000 and s.estimated_listing_count == 1
    assert {e["ml_id"]: e["price_cents"] for e in _entries(s.similar_listings)} == {"MLA921": 25_000, "MLA922": 40_000}


async def test_with_an_identical_there_is_no_estimate(webw):
    webw.web.pages["producto-raro"] = _web_page(
        _card("MLA901", "Producto Raro", 250.0), _card("MLA921", "Pack X6 Producto Raro", 900.0))
    _score(webw, "MLA901", 0.92)
    _score(webw, "MLA921", 0.95)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert (s.ml_status, s.color, s.ml_median_cents, s.match_state) == ("ok", "verde", 25_000, "igual")
    assert (s.estimated_color, s.estimated_margin_pct, s.estimated_median_cents, s.estimated_from) == (
        None, None, None, None)
    assert s.similar_count == 1 and s.estimated_listing_count == 0


# ─── solo diferentes: sin color, pero se ven ──────────────────────────────


@pytest.fixture
def only_different(webw):
    webw.web.pages["producto-raro"] = _web_page(
        _card("MLA931", "Zapatilla running hombre", 990.0), _card("MLA932", "Heladera no frost 400 L", 1500.0))
    _score(webw, "MLA931", 0.20)
    _score(webw, "MLA932", 0.15)
    return webw


async def test_only_different_publications_have_no_color_but_are_shown_with_price_and_reason(only_different):
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert (s.ml_status, s.color, s.estimated_color, s.match_state) == ("no_data", "sin_dato", None, "diferente")
    assert (s.similar_count, s.other_count) == (0, 2)
    others = {e["ml_id"]: e for e in _entries(s.other_listings)}
    assert set(others) == {"MLA931", "MLA932"}
    zap = others["MLA931"]
    assert zap["category"] == "diferente" and zap["origin"] == "web"
    assert zap["price_cents"] == 99_000 and zap["image_score"] == pytest.approx(0.2)
    assert zap["reason"] == "otro producto: la foto no se parece"
    assert zap["permalink"].startswith("https://articulo.mercadolibre.com.ar/")
    [run] = _runs()
    assert run.n_solo_diferentes == 1 and (run.n_sin_dato, run.n_est_verde) == (1, 0)


async def test_a_different_by_name_says_so(webw):
    webw.web.pages["producto-raro"] = _web_page(_card("MLA933", "Zapatilla running hombre", 5000.0))
    _score(webw, "MLA933", 0.55)                            # la foto no la veta: la veta el nombre
    await price_monitor.run_price_monitor()
    [e] = _entries(_snaps()["3"].other_listings)
    assert e["reason"] == "otro producto: el nombre no tiene relación"


async def test_without_any_photo_score_the_results_are_still_shown(webw):
    webw.web.pages["producto-raro"] = _web_page(_card("MLA934", "Producto Raro", 100.0))     # sin score de foto
    webw.image_scores.clear()
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    # no se puede decir si se parecen (CLIP caído): el producto no se evalúa como idéntico…
    assert (s.ml_status, s.color, s.estimated_color) == ("no_data", "sin_dato", None)
    # …pero lo que devolvió ML no se pierde
    [e] = _entries(s.other_listings)
    assert (e["ml_id"], e["price_cents"], e["image_score"]) == ("MLA934", 10_000, None)
    assert e["reason"] == "no se pudo comparar la foto (sin foto o sin CLIP)"


# ─── sin resultados / fallas ──────────────────────────────────────────────


async def test_no_results_at_all_is_the_only_no_data(webw):
    webw.web.pages["producto-raro"] = _web_page()
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert (s.ml_status, s.color, s.match_state) == ("no_data", "sin_dato", "ninguno")
    assert (s.similar_count, s.other_count, s.estimated_color) == (0, 0, None)
    assert s.similar_listings is None and s.other_listings is None
    assert price_monitor.snapshot_to_dict(s)["match_state"] == "ninguno"


async def test_a_failure_is_not_no_results(webw):
    webw.ml.search["Producto raro"] = 429
    webw.web.pages["producto-raro"] = _web_page()
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert s.ml_status == "failed" or s.match_state == "ninguno"
    if s.ml_status == "failed":
        assert s.match_state is None


async def test_a_failed_search_with_nothing_else_has_no_state(world):
    FakeVendure.products = [_product("2", "Lampara LED escritorio")]
    await price_monitor.run_price_monitor()
    s = _snaps()["2"]
    assert (s.ml_status, s.match_state, s.estimated_color) == ("failed", None, None)


async def test_a_web_block_without_results_stays_a_no_data_with_the_reason(webw):
    from tests.ml_web_fixtures import ANTIBOT_HTML, page

    webw.web.pages["producto-raro"] = page(ANTIBOT_HTML)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert (s.ml_status, s.match_state, s.web_state) == ("no_data", "ninguno", "blocked")
    assert "verificación anti-bot" in s.ml_error


# ─── qué se guarda, y cuántas ─────────────────────────────────────────────


async def test_it_keeps_at_most_n_publications_most_similar_first_identical_then_similar_then_different(webw):
    _set("pm_ml_web_max_results", 12)
    _set("pm_ml_keep_listings", 5)
    cards = [_card("MLA901", "Producto Raro", 250.0)]                                     # idéntico
    cards += [_card(f"MLA94{i}", f"Pack X{3 + i} Producto Raro", 300.0 + i) for i in range(4)]     # similares
    cards += [_card(f"MLA95{i}", f"Heladera modelo {i}", 800.0 + i) for i in range(5)]    # diferentes
    webw.web.pages["producto-raro"] = _web_page(*cards)
    _score(webw, "MLA901", 0.92)
    for i in range(4):
        _score(webw, f"MLA94{i}", 0.96 - i * 0.02)          # 0,96 0,94 0,92 0,90
    for i in range(5):
        _score(webw, f"MLA95{i}", 0.10 + i * 0.05)          # 0,10 … 0,30
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    igual, similar, other = _entries(s.matched_listings), _entries(s.similar_listings), _entries(s.other_listings)
    assert [e["ml_id"] for e in igual] == ["MLA901"]
    assert [e["ml_id"] for e in similar] == ["MLA940", "MLA941", "MLA942", "MLA943"]       # el más parecido primero
    assert other == [] and len(igual) + len(similar) + len(other) == 5                       # tope: 5, sin lugar para diferentes
    assert (s.similar_count, s.other_count) == (4, 0)


async def test_the_cap_leaves_room_for_the_closest_different_ones(webw):
    _set("pm_ml_web_max_results", 12)
    _set("pm_ml_keep_listings", 4)
    cards = [_card("MLA941", "Pack X6 Producto Raro", 300.0)]
    cards += [_card(f"MLA95{i}", f"Heladera modelo {i}", 800.0 + i) for i in range(5)]
    webw.web.pages["producto-raro"] = _web_page(*cards)
    _score(webw, "MLA941", 0.95)
    for i in range(5):
        _score(webw, f"MLA95{i}", 0.10 + i * 0.05)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert [e["ml_id"] for e in _entries(s.similar_listings)] == ["MLA941"]
    # 4 en total: 1 similar + las 3 diferentes más parecidas (foto 0,30 / 0,25 / 0,20)
    assert [e["ml_id"] for e in _entries(s.other_listings)] == ["MLA954", "MLA953", "MLA952"]
    assert [e["image_score"] for e in _entries(s.other_listings)] == pytest.approx([0.3, 0.25, 0.2])


async def test_identical_ones_are_never_cut_because_they_define_the_price(webw):
    _set("pm_ml_keep_listings", 2)
    webw.web.pages["producto-raro"] = _web_page(
        *[_card(f"MLA90{i}", "Producto Raro", 250.0 + i, seller=f"S{i}") for i in range(4)])
    for i in range(4):
        _score(webw, f"MLA90{i}", 0.9 - i * 0.01)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert len(_entries(s.matched_listings)) == 4 and s.ml_listing_count == 4 and s.match_state == "igual"


def test_the_keep_setting_defaults_to_8_and_is_validated():
    from app.config import get_settings

    assert get_settings().pm_ml_keep_listings == 8
    assert runtime.set_value("pm_ml_keep_listings", 12) == 12
    for bad in (0, 25, "x"):
        with pytest.raises(ValueError):
            runtime.set_value("pm_ml_keep_listings", bad)
    runtime.reset_to_default("pm_ml_keep_listings")


async def test_every_stored_publication_has_what_the_dashboard_needs(only_similars):
    await price_monitor.run_price_monitor()
    for e in _entries(_snaps()["3"].similar_listings):
        assert e["category"] == "similar" and e["origin"] == "web"
        assert e["reason"] and e["differences"] == ["cantidad"]              # motivo corto: pack
        assert isinstance(e["image_score"], float) and isinstance(e["name_score"], float)
        assert e["price_cents"] > 0 and e["permalink"].startswith("https://")


# ─── API: ambiguas, juez, iguales sin precio ──────────────────────────────


async def test_an_ambiguous_card_without_a_judge_is_a_similar_marked_unconfirmed(world):
    _ambiguous_world(world)
    await price_monitor.run_price_monitor()
    s = _snaps()["8"]
    assert (s.ml_status, s.color, s.ambiguous_count) == ("no_data", "sin_dato", 1)      # como antes
    assert s.ml_error == "ninguna ficha es el mismo producto"
    [e] = _entries(s.similar_listings)
    assert (e["ml_id"], e["category"], e["source"], e["origin"]) == ("MLA8", "similar", "ambiguo", "api")
    assert e["reason"] == "parecido en foto y nombre, sin confirmar" and e["price_cents"] is None
    assert (s.match_state, s.estimated_color) == ("similar", None)       # sin precio no hay estimado (y sin pedirlo)


async def test_a_judged_similar_card_brings_its_price_and_the_estimate(world, monkeypatch):
    _ambiguous_world(world)
    _set("pm_vision_max_calls", 5)
    monkeypatch.setattr(market_judge, "enabled", lambda: True)
    monkeypatch.setattr(market_judge, "make_client", lambda: type("C", (), {"close": lambda s: None})())

    async def judge(our_name, photos, candidates, *, max_calls, on_reserve=None, **kw):
        assert await daily_budget.reserve_async(market_judge.LLM_COUNTER_KEY, max_calls, None, on_reserve)
        return market_judge.JudgeResult(verdicts={
            c.ml_id: market_judge.JudgeVerdict(c.ml_id, False, 0.8, "otra marca", "similar", ("marca",))
            for c in candidates})

    monkeypatch.setattr(market_judge, "judge", judge)
    await price_monitor.run_price_monitor()
    s = _snaps()["8"]
    [e] = _entries(s.similar_listings)
    assert (e["source"], e["differences"], e["reason"], e["price_cents"]) == ("llm", ["marca"], "otra marca", 30_000)
    assert s.estimated_color == "verde" and s.color == "sin_dato" and s.match_state == "similar"
    assert s.estimated_median_cents == 30_000


async def test_a_card_the_judge_rejects_is_a_different_with_the_judge_reason(world, monkeypatch):
    _ambiguous_world(world)
    _set("pm_vision_max_calls", 5)
    monkeypatch.setattr(market_judge, "enabled", lambda: True)
    monkeypatch.setattr(market_judge, "make_client", lambda: type("C", (), {"close": lambda s: None})())

    async def judge(our_name, photos, candidates, *, max_calls, on_reserve=None, **kw):
        assert await daily_budget.reserve_async(market_judge.LLM_COUNTER_KEY, max_calls, None, on_reserve)
        return market_judge.JudgeResult(verdicts={
            c.ml_id: market_judge.JudgeVerdict(c.ml_id, False, 0.9, "es un accesorio", "diferente", ())
            for c in candidates})

    monkeypatch.setattr(market_judge, "judge", judge)
    await price_monitor.run_price_monitor()
    s = _snaps()["8"]
    assert (s.similar_count, s.other_count, s.match_state) == (0, 1, "diferente")
    [e] = _entries(s.other_listings)
    assert (e["source"], e["reason"], e["price_cents"]) == ("llm", "es un accesorio", 30_000)


async def test_api_cards_far_from_our_product_are_kept_as_different(world):
    FakeVendure.products = [_product("8", "Soporte celular auto")]
    world.ml.search["Soporte celular auto"] = [_candidate("MLA8", "Soporte celular para auto"),
                                                _candidate("MLA9", "Heladera no frost")]
    world.image_scores[ML_IMG.format("MLA8")] = 0.62
    world.image_scores[ML_IMG.format("MLA9")] = 0.12
    await price_monitor.run_price_monitor()
    s = _snaps()["8"]
    assert [e["ml_id"] for e in _entries(s.similar_listings)] == ["MLA8"]
    [other] = _entries(s.other_listings)
    assert (other["ml_id"], other["origin"], other["reason"]) == ("MLA9", "api", "otro producto: la foto no se parece")
    assert s.candidates_count == 2 and s.match_state == "similar"


async def test_an_identical_without_a_price_that_counts_is_kept_apart_from_the_priced_ones(world):
    FakeVendure.products = [_product("1", "Organizador cocina")]
    world.ml.items["MLA1"] = []                                           # ficha igual, sin vendedores
    await price_monitor.run_price_monitor()
    s = _snaps()["1"]
    assert (s.ml_status, s.color, s.ml_error) == ("no_data", "sin_dato", "las fichas no tienen vendedores que cuenten")
    assert _entries(s.matched_listings) == []                              # igual que main: solo lo igual CON precio
    [e] = _entries(s.unpriced_listings)
    assert (e["ml_id"], e["category"], e["listings"], e["prices_cents"]) == ("MLA1", "igual", 0, [])
    assert "sin vendedores que cuenten" in e["notes"][0]
    assert s.match_state == "igual_sin_precio"
    d = price_monitor.snapshot_to_dict(s)
    assert [m["ml_id"] for m in d["unpriced_listings"]] == ["MLA1"] and d["matched_listings"] == []


async def test_with_the_web_off_an_identical_by_api_keeps_exactly_its_real_numbers(world):
    FakeVendure.products = [_product("1", "Organizador cocina")]
    await price_monitor.run_price_monitor()
    s = _snaps()["1"]
    assert (s.ml_status, s.color, s.ml_median_cents, s.ml_min_cents, s.est_margin_pct) == (
        "ok", "verde", 21_000, 20_000, pytest.approx(82.7))
    assert (s.match_state, s.estimated_color, s.estimated_from, s.other_count, s.unpriced_listings) == (
        "igual", None, None, 0, None)


# ─── lo que una persona marcó, la próxima corrida lo respeta ──────────────


def _mark(product_id: str, ml_id: str, label: int) -> None:
    with Session(engine) as s:
        s.add(MarketMatchFeedback(product_id=product_id, ml_id=ml_id, label=label, actor="pao@b2box.pro"))
        s.commit()


async def test_a_card_marked_is_the_same_is_identical_next_run_whatever_the_judge_said(webw):
    webw.web.pages["producto-raro"] = _web_page(
        _card("MLA931", "Zapatilla running hombre", 990.0), _card("MLA941", "Pack X6 Producto Raro", 300.0))
    _score(webw, "MLA931", 0.20)                            # por foto y nombre es DIFERENTE
    _score(webw, "MLA941", 0.95)
    _mark("3", "MLA931", 1)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert (s.ml_status, s.match_state, s.match_source, s.match_origin) == ("ok", "igual", "manual", "web")
    assert s.ml_median_cents == 99_000 and s.color in ("verde", "amarillo", "rojo")
    [m] = _entries(s.matched_listings)
    assert (m["ml_id"], m["category"], m["source"], m["reason"]) == (
        "MLA931", "igual", "manual", "una persona la marcó «Es el mismo»")
    assert [e["ml_id"] for e in _entries(s.similar_listings)] == ["MLA941"]      # el pack sigue similar
    assert s.estimated_color is None                                              # con idéntico no hay estimado


async def test_a_promoted_card_is_not_demoted_by_the_spec_check(webw):
    webw.web.pages["producto-raro"] = _web_page(_card("MLA941", "Pack X6 Producto Raro", 300.0))
    _score(webw, "MLA941", 0.95)
    _mark("3", "MLA941", 1)                                  # sin la marca sería SIMILAR por cantidad
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert (s.ml_status, s.match_source, s.similar_count) == ("ok", "manual", 0)


async def test_a_promoted_api_card_outside_the_scored_ones_still_gets_in(world):
    FakeVendure.products = [_product("1", "Organizador cocina")]
    world.ml.search["Organizador cocina"] = [_candidate(f"MLA7{i}", f"Organizador cocina variante {i}") for i in range(8)]
    world.ml.items["MLA77"] = [_listing("I77", "101", 777.0)]
    for i in range(8):
        world.image_scores[ML_IMG.format(f"MLA7{i}")] = 0.2
    _mark("1", "MLA77", 1)
    await price_monitor.run_price_monitor()
    s = _snaps()["1"]
    assert (s.ml_status, s.match_source, s.ml_median_cents) == ("ok", "manual", 77_700)


async def test_a_card_marked_not_the_same_is_shown_as_a_different_with_its_price(webw):
    webw.web.pages["producto-raro"] = _web_page(
        _card("MLA901", "Producto Raro", 250.0), _card("MLA902", "Producto Raro Premium", 400.0, seller="Dos"))
    _score(webw, "MLA901", 0.92)
    _score(webw, "MLA902", 0.9)
    _mark("3", "MLA901", 0)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert [m["ml_id"] for m in _entries(s.matched_listings)] == ["MLA902"] and s.ml_median_cents == 40_000
    [e] = _entries(s.other_listings)
    assert (e["ml_id"], e["source"], e["reason"], e["price_cents"]) == (
        "MLA901", "manual", "una persona la marcó «No es el mismo»", 25_000)
    assert s.candidates_count == 1                                               # no se vuelve a puntuar


async def test_marks_of_another_product_do_not_leak(webw):
    webw.web.pages["producto-raro"] = _web_page(_card("MLA901", "Producto Raro", 250.0))
    _score(webw, "MLA901", 0.92)
    _mark("99", "MLA901", 0)
    await price_monitor.run_price_monitor()
    assert _snaps()["3"].ml_status == "ok"


# ─── Vendure: nada se escribe ─────────────────────────────────────────────


async def test_nothing_is_written_to_vendure_with_the_new_lists(only_similars):
    _set("pm_include_disabled", 1)
    FakeVendure.products = [_product("3", "Producto raro"), _product("4", "Producto raro", enabled=False)]
    await price_monitor.run_price_monitor()
    assert FakeVendure.forbidden == [] and only_similars.graphql_calls == []
    assert {s.product_enabled for s in _snaps().values()} == {True, False}
