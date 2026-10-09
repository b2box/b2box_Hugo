"""Variantes de búsqueda en la API de ML, de punta a punta (Vendure, ML, CLIP y el juez son dobles).

  * se prueban en orden y se corta en la primera que da un IGUAL con precio;
  * las fichas de todas las variantes probadas se juntan sin repetir y pasan por el mismo filtro;
  * cada búsqueda cuenta contra `pm_ml_daily_budget`;
  * el snapshot guarda qué variante encontró el match y la corrida cuántos resolvió cada una;
  * con `pm_ml_query_variants` = 1 el semáforo da, columna a columna, lo mismo que origin/main (dorado).
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app import runtime  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.models import Setting  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import daily_budget, market_match, market_ml, market_query, price_monitor  # noqa: E402
from tests import qa2_world as qw  # noqa: E402
from tests.store_fixtures import store_db  # noqa: E402,F401  (fixture)
from tests.test_price_monitor import (  # noqa: E402,F401
    ML_IMG, FakeVendure, _candidate, _listing, _product, _runs, _set, _snaps, world)
from tests.test_price_monitor_routes import _env, client  # noqa: E402,F401
from tests.test_qa2_tiendas_regresion import GOLDEN, assert_ml_equal, run_variant, run_world  # noqa: E402,F401
from tests.test_semaforo_web import webw  # noqa: E402,F401

LONG = "Organizador Doble Ajustable 3 Niveles 40x30 Blanco"
V1 = market_match.search_query(LONG)
V2 = "Organizador Doble Ajustable Niveles"
V3 = "Organizador Ajustable Niveles"


@pytest.fixture
def three(world):
    """Un solo producto de nombre largo y las tres variantes prendidas."""
    FakeVendure.products = [_product("7", LONG)]
    _set("pm_ml_query_variants", 3)
    _set("pm_ml_concurrency", 1)
    world.ml.search.clear()
    world.ml.items.update({"MLA11": [_listing("I11", "101", 200.0), _listing("I12", "103", 220.0)],
                           "MLA12": [_listing("I21", "101", 300.0)]})
    world.ml.users.update({"101": 1_000, "103": 900})
    world.image_scores.update({ML_IMG.format("MLA11"): 0.90, ML_IMG.format("MLA12"): 0.92,
                               ML_IMG.format("MLA99"): 0.10})
    return world


def _searches(w) -> list[str]:
    return [c.removeprefix("search:") for c in w.ml.calls if c.startswith("search:")]


# ─── el setting ────────────────────────────────────────────────────────────


def test_the_default_is_three_and_it_is_editable_between_one_and_three():
    assert Settings(vendure_api_url="https://example.invalid/x").pm_ml_query_variants == 3
    meta = next(m for m in runtime.SETTINGS_SCHEMA if m.key == "pm_ml_query_variants")
    assert (meta.min, meta.max, meta.type, meta.group) == (1, 3, "int", "monitor")
    for bad in (0, 4, -1):
        with pytest.raises(ValueError):
            runtime.set_value("pm_ml_query_variants", bad)
    try:
        assert runtime.set_value("pm_ml_query_variants", 2) == 2 and runtime.get("pm_ml_query_variants") == 2
    finally:
        with Session(engine) as s:
            row = s.get(Setting, "pm_ml_query_variants")
            if row is not None:
                s.delete(row)
                s.commit()
        runtime.invalidate()


# ─── se prueban en orden y se corta en la primera que da un IGUAL con precio ─


async def test_the_first_variant_that_resolves_stops_the_others(three):
    three.ml.search[V1] = [_candidate("MLA11", "Organizador doble ajustable")]
    await price_monitor.run_price_monitor()
    s = _snaps()["7"]
    assert s.ml_status == "ok" and s.match_origin == "api" and s.ml_variant == "titulo"
    assert _searches(three) == [V1]


async def test_the_second_variant_finds_what_the_title_did_not(three):
    three.ml.search[V2] = [_candidate("MLA11", "Organizador doble ajustable")]
    await price_monitor.run_price_monitor()
    s = _snaps()["7"]
    assert s.ml_status == "ok" and s.ml_variant == "corto" and (s.ml_min_cents, s.ml_listing_count) == (20_000, 2)
    assert _searches(three) == [V1, V2]                         # la tercera ni se pide
    [run] = _runs()
    assert json.loads(run.variant_stats) == {"corto": 1}


async def test_the_third_variant_is_the_last_resort(three):
    three.ml.search[V3] = [_candidate("MLA11", "Organizador ajustable")]
    await price_monitor.run_price_monitor()
    s = _snaps()["7"]
    assert s.ml_status == "ok" and s.ml_variant == "claves"
    assert _searches(three) == [V1, V2, V3]
    assert json.loads(_runs()[0].variant_stats) == {"claves": 1}


async def test_two_variants_never_search_the_third(three):
    _set("pm_ml_query_variants", 2)
    three.ml.search[V3] = [_candidate("MLA11", "Organizador ajustable")]
    await price_monitor.run_price_monitor()
    s = _snaps()["7"]
    assert s.ml_status == "no_data" and s.ml_variant is None and _searches(three) == [V1, V2]


async def test_an_identical_ficha_without_sellers_does_not_cut_it(three):
    """El IGUAL tiene que tener precio: una ficha igual sin vendedores deja seguir a la variante siguiente."""
    three.ml.search[V1] = [_candidate("MLA13", "Organizador doble ajustable")]
    three.ml.search[V2] = [_candidate("MLA12", "Organizador doble ajustable")]
    three.image_scores[ML_IMG.format("MLA13")] = 0.91
    await price_monitor.run_price_monitor()
    s = _snaps()["7"]
    assert s.ml_status == "ok" and s.ml_variant == "corto" and s.ml_median_cents == 30_000
    assert [m["ml_id"] for m in json.loads(s.matched_listings)] == ["MLA12"]
    assert [m["ml_id"] for m in json.loads(s.unpriced_listings)] == ["MLA13"]     # no se perdió, queda sin precio
    assert s.candidates_count == 2


async def test_nothing_in_any_variant_is_no_data_with_the_old_reason(three):
    await price_monitor.run_price_monitor()
    s = _snaps()["7"]
    assert s.ml_status == "no_data" and "no devolvió fichas" in s.ml_error and s.ml_variant is None
    assert _searches(three) == [V1, V2, V3]
    assert _runs()[0].variant_stats is None


# ─── las fichas de todas las variantes se juntan y pasan por el mismo filtro ──


async def test_candidates_of_every_tried_variant_are_pooled_and_judged_once(three, monkeypatch):
    scored: list[str] = []

    async def counting(our, urls):  # noqa: ARG001
        scored.append(urls[0])
        return three.image_scores.get(urls[0])

    monkeypatch.setattr(market_match, "clip_index_scorer", counting)
    impostor = _candidate("MLA99", "Zapatillas running")
    three.ml.search[V1] = [impostor]
    three.ml.search[V2] = [impostor, _candidate("MLA11", "Organizador doble ajustable")]   # MLA99 repetida
    await price_monitor.run_price_monitor()
    s = _snaps()["7"]
    assert s.ml_status == "ok" and s.ml_variant == "corto"
    assert s.candidates_count == 2                                   # sin repetir por id
    assert sorted(scored) == [ML_IMG.format("MLA11"), ML_IMG.format("MLA99")]       # cada una puntuada UNA vez
    others = json.loads(s.other_listings)
    assert [o["ml_id"] for o in others] == ["MLA99"] and others[0]["reason"]      # el impostor se guarda, con motivo
    assert s.name_score_max is not None and s.image_score_max == pytest.approx(0.90)


async def test_a_listing_a_person_excluded_stays_excluded_in_every_variant(three):
    from app.db.models import MarketMatchFeedback

    with Session(engine) as s:
        s.add(MarketMatchFeedback(product_id="7", ml_id="MLA11", label=0, category="igual"))
        s.commit()
    three.ml.search[V1] = [_candidate("MLA11", "Organizador doble ajustable")]
    three.ml.search[V2] = [_candidate("MLA11", "Organizador doble ajustable")]
    await price_monitor.run_price_monitor()
    s = _snaps()["7"]
    assert s.ml_status == "no_data" and [o["ml_id"] for o in json.loads(s.other_listings)] == ["MLA11"]
    assert s.candidates_count == 0                                   # excluida: no se vuelve a juzgar


# ─── cada búsqueda cuenta contra el budget diario ─────────────────────────


async def test_every_variant_search_counts_against_the_daily_budget(three):
    three.ml.search[V2] = [_candidate("MLA11", "Organizador doble ajustable")]
    await price_monitor.run_price_monitor()
    [run] = _runs()
    calls = len(three.ml.calls)                                       # 2 búsquedas + items + users + sonda
    assert run.ml_requests_used == calls == daily_budget.used_today(market_ml.ML_COUNTER_KEY)
    assert calls >= 3


async def test_running_out_of_budget_between_variants_skips_the_product(three):
    _set("pm_ml_daily_budget", 2)
    await price_monitor.run_price_monitor()
    s = _snaps()["7"]
    assert s.ml_status == "skipped" and "budget" in s.ml_error
    assert _searches(three) == [V1, V2]                                # la tercera no se pidió
    assert daily_budget.used_today(market_ml.ML_COUNTER_KEY) == 2 == _runs()[0].ml_requests_used


async def test_a_variant_that_fails_fails_the_product(three):
    three.ml.search[V2] = 503
    await price_monitor.run_price_monitor()
    s = _snaps()["7"]
    assert s.ml_status == "failed" and "búsqueda ML" in s.ml_error
    assert _searches(three)[:2] == [V1, V2]


# ─── con una sola variante es lo de siempre (incluido el "inicio" de respaldo) ─


async def test_one_variant_keeps_the_old_four_words_fallback(three):
    _set("pm_ml_query_variants", 1)
    three.ml.search["Organizador Doble Ajustable 3"] = [_candidate("MLA11", "Organizador doble")]
    await price_monitor.run_price_monitor()
    s = _snaps()["7"]
    assert s.ml_status == "ok" and s.ml_variant == "inicio"
    assert _searches(three)[:2] == [V1, "Organizador Doble Ajustable 3"] and V2 not in _searches(three)


async def test_with_more_variants_the_old_fallback_is_not_used(three):
    three.ml.search["Organizador Doble Ajustable 3"] = [_candidate("MLA11", "Organizador doble")]
    await price_monitor.run_price_monitor()
    assert "Organizador Doble Ajustable 3" not in _searches(three)
    assert _snaps()["7"].ml_status == "no_data"


# ─── lo que ve el dashboard ───────────────────────────────────────────────


async def test_the_dashboard_api_serves_the_variant_that_found_the_match(three, client):
    three.ml.search[V2] = [_candidate("MLA11", "Organizador doble ajustable")]
    await price_monitor.run_price_monitor()
    item = client.get("/api/price-monitor/snapshots").json()["items"][0]
    assert item["ml_variant"] == "corto" and item["match_origin"] == "api"
    run = client.get("/api/price-monitor/runs").json()["items"][0]
    assert run["variants"] == {"corto": 1}


# ─── dorado: con 1 variante, columna a columna lo mismo que origin/main ──────


@pytest.mark.parametrize("name", list(qw.VARIANTS))
async def test_with_one_variant_ml_is_column_by_column_what_origin_main_gives(run_world, name):
    runtime.set_value("pm_ml_query_variants", 1)                      # explícito: este test no depende del fixture
    w, opts = await run_variant(name, run_world)
    assert runtime.get("pm_ml_query_variants") == 1
    await price_monitor.run_price_monitor()
    assert_ml_equal(name, qw.dump_all(w, run_world[1]["calls"]), where=f"{name}/1 variante")


@pytest.mark.parametrize("name", ["base_spec1", "base_spec0", "con_feedback"])
async def test_with_three_variants_the_products_the_first_search_resolved_do_not_change(run_world, name):
    """Las variantes solo agregan búsquedas a lo que la primera no resolvió: el producto que ya tenía su IGUAL por la
    API, el que falló y el que está fuera de ML salen exactamente igual (las búsquedas de más devuelven vacío)."""
    w, opts = await run_variant(name, run_world)
    runtime.set_value("pm_ml_query_variants", 3)
    await price_monitor.run_price_monitor()
    got = qw.dump_all(w, run_world[1]["calls"])
    want = GOLDEN[name]
    api_ok = [pid for pid, row in want["snaps"].items() if row["ml_status"] == "ok" and row["match_origin"] == "api"]
    assert api_ok, "el mundo tiene que tener productos resueltos por la API"
    for pid in api_ok:
        for col, value in want["snaps"][pid].items():
            assert got["snaps"][pid][col] == value, (pid, col)
        assert got["snaps"][pid]["ml_variant"] == "titulo"
    assert len(got["ml_calls"]) >= len(want["ml_calls"])
