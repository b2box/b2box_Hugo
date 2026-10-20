"""QA de fix/semaforo-sin-dato: una corrida tipo #15 con muchos productos.

Cada producto trae una ficha dudosa por cada una de sus 3 búsquedas de la API (el juez dice «similar») y un candidato
dudoso de la oficina (el juez dice «igual», con precio). Con poco cupo de juez:

  * con los defaults nuevos (50 % reservado, 1 búsqueda de la API) la oficina SÍ recibe juez y hay productos `ok`;
  * con 0 / 3 (origin/main) las fichas de la API se comen el cupo y la oficina se queda sin juez.

También: casos límite de la reserva, del corta-circuito y del 404 que los tests del developer no cubren.
Vendure, ML, CLIP y el juez son dobles: nada sale a la red."""

from __future__ import annotations

import json
import logging
import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app.pricing import daily_budget, market_judge, market_query, price_monitor  # noqa: E402
from tests.test_oficina_semaforo import _store, _wire  # noqa: E402
from tests.test_price_monitor import (  # noqa: E402,F401
    ML_IMG, FakeVendure, _candidate, _listing, _product, _runs, _set, _snaps, world)
from tests.test_semaforo_sin_dato import _no_oficina_rows, _real_judge, _HttpError, _good  # noqa: E402,F401
from tests.test_semaforo_web import PHOTO  # noqa: E402

NOUNS = ["Organizador", "Lampara", "Botella", "Funda", "Cable", "Mate", "Soporte", "Cesto", "Perchero", "Espejo",
         "Alfombra", "Cortina", "Repisa", "Maceta", "Tabla", "Bandeja", "Jarra", "Termo", "Mochila", "Billetera",
         "Reloj", "Auricular", "Teclado", "Mouse", "Parlante", "Linterna", "Cuchillo", "Sarten", "Olla", "Taza"]
ADJS = ["Doble Ajustable", "Plegable Reforzado", "Premium Moderno"]


def _names(n: int) -> list[str]:
    out = []
    for i in range(n):
        noun, adj = NOUNS[i % len(NOUNS)], ADJS[i // len(NOUNS)]
        out.append(f"{noun} {adj} 3 Niveles 40x30 Blanco")
    return out


def _corrida15(world, n: int) -> list[str]:
    """`n` productos con 3 búsquedas de la API (una ficha dudosa cada una) y un candidato dudoso de la oficina."""
    names = _names(n)
    FakeVendure.products = [_product(str(i), name) for i, name in enumerate(names, start=1)]
    _set("pm_ml_query_variants", 3)
    queries: set[str] = set()
    for i, name in enumerate(names, start=1):
        plan = [st for st in market_query.query_plan(name, 3) if not st.only_if_empty]
        assert len(plan) == 3, name
        for k, step in enumerate(plan):
            assert step.query not in queries, f"búsqueda repetida entre productos: {step.query}"
            queries.add(step.query)
            ml_id = f"MLA1{i:03d}{k}"
            world.ml.search[step.query] = [_candidate(ml_id, name + " compatible")]
            world.image_scores[ML_IMG.format(ml_id)] = 0.5
        ref = f"MLA9{i:03d}0"
        _store(str(i), [_wire(ref, name, 250.0)])
        world.image_scores[PHOTO.format(f"{ref[3:]}-MLA1_012025")] = 0.5
    return names


def _judge_igual_for_oficina(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    async def judge(our_name, photos, candidates, *, max_calls, on_reserve=None, on_outcome=None,
                    counter_key=market_judge.LLM_COUNTER_KEY, **kw):
        if await daily_budget.reserve_async(counter_key, int(max_calls), None, on_reserve) is None:
            if on_outcome:
                on_outcome(market_judge.OUTCOME_CAP, f"tope {max_calls}")
            return None
        calls.append([c.ml_id for c in candidates])
        if on_outcome:
            on_outcome(market_judge.OUTCOME_OK, "")
        verdicts = {}
        for c in candidates:
            if c.ml_id.startswith("MLA9"):
                verdicts[c.ml_id] = market_judge.JudgeVerdict(c.ml_id, True, 0.9, "mismo", market_judge.CAT_IGUAL, ())
            else:
                verdicts[c.ml_id] = market_judge.JudgeVerdict(c.ml_id, False, 0.9, "otro", market_judge.CAT_SIMILAR,
                                                              ("modelo",))
        return market_judge.JudgeResult(verdicts=verdicts)

    monkeypatch.setattr(market_judge, "enabled", lambda: True)
    monkeypatch.setattr(market_judge, "make_client", lambda: None)
    monkeypatch.setattr(market_judge, "judge", judge)
    return calls


def _reset_judge_counter() -> None:
    from sqlmodel import Session

    from app.db.models import Setting
    from app.db.session import engine
    with Session(engine) as s:
        row = s.get(Setting, market_judge.LLM_COUNTER_KEY)
        if row is not None:
            s.delete(row)
            s.commit()


def _split(calls):
    api = [c for c in calls if c[0].startswith("MLA1")]
    ofi = [c for c in calls if c[0].startswith("MLA9")]
    assert len(api) + len(ofi) == len(calls)
    return api, ofi


N_PRODUCTS = 60
CAP = 20


@pytest.mark.parametrize("concurrency", [1, 4])
async def test_corrida15_with_the_new_defaults_the_oficina_gets_the_judge(world, monkeypatch, concurrency):
    calls = _judge_igual_for_oficina(monkeypatch)
    _corrida15(world, N_PRODUCTS)
    _set("pm_vision_max_calls", CAP)
    _set("pm_ml_concurrency", concurrency)

    result = await price_monitor.run_price_monitor()

    api, ofi = _split(calls)
    print(f"\n[defaults conc={concurrency}] api={len(api)} oficina={len(ofi)} counts={result['counts']}")
    assert len(api) <= CAP * 50 // 100, "la API no pasa de su parte del tope"
    assert len(ofi) >= CAP - len(api) and len(ofi) > 0
    assert daily_budget.used_today(market_judge.LLM_COUNTER_KEY) == CAP
    snaps = _snaps()
    ok = [s for s in snaps.values() if s.ml_status == "ok"]
    assert len(ok) == len(ofi) and all(s.match_origin == "oficina" for s in ok)
    run = _runs()[0]
    assert run.llm_calls == CAP and run.llm_failures == 0 and run.llm_status is None
    # Cada producto pide juez 4 veces (3 búsquedas de la API + la oficina): todas quedan contadas en algún lado.
    assert run.llm_calls + run.llm_skipped_api + run.llm_skipped_cap == N_PRODUCTS * 4
    assert result["counts"]["failed"] == 0


@pytest.mark.parametrize("concurrency", [1, 4])
async def test_corrida15_with_0_and_3_the_api_eats_the_cap_and_the_oficina_gets_at_most_a_quarter(world, monkeypatch,
                                                                                        concurrency):
    calls = _judge_igual_for_oficina(monkeypatch)
    _corrida15(world, N_PRODUCTS)
    _set("pm_vision_max_calls", CAP)
    _set("pm_ml_concurrency", concurrency)
    _set("pm_vision_web_reserve_pct", 0)
    _set("pm_vision_api_variants", 3)

    result = await price_monitor.run_price_monitor()

    api, ofi = _split(calls)
    print(f"\n[0/3 conc={concurrency}] api={len(api)} oficina={len(ofi)} counts={result['counts']}")
    assert len(api) + len(ofi) == CAP
    # Con 3 búsquedas por producto la API se lleva al menos 3 de cada 4 llamadas (con concurrencia, más): la oficina
    # recibe a lo sumo un cuarto del cupo.
    assert len(ofi) <= CAP // 4


async def test_corrida15_new_defaults_beat_0_3_in_products_with_price(world, monkeypatch):
    """La misma corrida dos veces (contador del juez reiniciado): con los defaults nuevos hay más `ok`."""
    _judge_igual_for_oficina(monkeypatch)
    _corrida15(world, N_PRODUCTS)
    _set("pm_vision_max_calls", CAP)
    _set("pm_vision_web_reserve_pct", 0)
    _set("pm_vision_api_variants", 3)
    first = (await price_monitor.run_price_monitor())["run_id"]
    ok_before = sum(1 for s in _snaps(first).values() if s.ml_status == "ok")
    _reset_judge_counter()
    _set("pm_vision_web_reserve_pct", 50)
    _set("pm_vision_api_variants", 1)
    second = (await price_monitor.run_price_monitor())["run_id"]
    assert second != first
    ok_after = sum(1 for s in _snaps(second).values() if s.ml_status == "ok")
    print(f"\nok antes={ok_before} ahora={ok_after}")
    assert ok_after > ok_before


# ─── casos límite ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("cap,reserve,want", [(1, 50, 0), (3, 50, 1), (5, 33, 3), (10, 0, 10), (10, 100, 0),
                                              (10, -5, 10), (10, 150, 0)])
def test_api_judge_cap_rounding_and_clamping(cap, reserve, want):
    ctx = price_monitor.RunContext.__new__(price_monitor.RunContext)
    ctx.judge_max_calls, ctx.judge_web_reserve_pct = cap, reserve
    assert price_monitor.api_judge_cap(ctx) == want


@pytest.mark.xfail(strict=True, reason="BUG QA: error_summary devuelve str(exc) para errores sin status_code y "
                   "browser_fetch.redact solo tapa el proxy: la key del juez puede quedar en llm_status")
async def test_c_the_cut_reason_of_a_non_http_exception_does_not_leak_the_key(world, monkeypatch):
    """Una excepción SIN status_code (red, cliente) entra a llm_status vía error_summary → str(exc). Si el mensaje
    repite la API key del juez, no tiene que quedar guardada en la corrida."""
    key = "sk-qa-supersecreta-999"
    _real_judge(monkeypatch, [RuntimeError(f"connect failed Authorization: Bearer {key}")] * 5, pm_llm_api_key=key)
    from tests.test_semaforo_sin_dato import _ambiguous_catalog
    _ambiguous_catalog(world, 6)
    _set("pm_vision_max_calls", 100)
    _set("pm_ml_concurrency", 1)

    await price_monitor.run_price_monitor()

    run = _runs()[0]
    assert run.llm_status and run.llm_status.startswith("juez cortado: 5 fallas seguidas")
    assert key not in run.llm_status, run.llm_status


async def test_c_cap_hits_do_not_reset_nor_grow_the_streak(world, monkeypatch):
    """Fallas, después tope: el tope no es falla ni respuesta buena (la racha queda donde estaba)."""
    client = _real_judge(monkeypatch, [_HttpError(500, "-")] * 3)
    from tests.test_semaforo_sin_dato import _ambiguous_catalog
    _ambiguous_catalog(world, 6)
    _set("pm_vision_max_calls", 3)
    _set("pm_vision_web_reserve_pct", 0)
    _set("pm_ml_concurrency", 1)

    await price_monitor.run_price_monitor()

    run = _runs()[0]
    assert client.calls == 3 and run.llm_failures == 3 and run.llm_status is None
    assert run.llm_skipped_cap == 3


async def test_a_404_in_one_variant_then_a_price_in_the_next_variant_is_ok(world):
    """404 en la ficha IGUAL de la primera búsqueda; la segunda búsqueda trae otra ficha IGUAL con precio."""
    name = "Organizador Doble Ajustable 3 Niveles 40x30 Blanco"
    FakeVendure.products = [_product("1", name)]
    _set("pm_ml_query_variants", 3)
    plan = [st for st in market_query.query_plan(name, 3) if not st.only_if_empty]
    world.ml.search[plan[0].query] = [_candidate("MLA501", name)]
    world.ml.search[plan[1].query] = [_candidate("MLA502", name)]
    world.image_scores[ML_IMG.format("MLA501")] = 0.95
    world.image_scores[ML_IMG.format("MLA502")] = 0.95
    world.ml.items["MLA501"] = 404
    world.ml.items["MLA502"] = [_listing("I2", "101", 300.0, sold_quantity=500)]

    result = await price_monitor.run_price_monitor()

    s = _snaps()["1"]
    assert s.ml_status == "ok" and s.ml_variant == plan[1].label and result["counts"]["failed"] == 0
    assert [u["ml_id"] for u in json.loads(s.unpriced_listings)] == ["MLA501"]


async def test_a_404_and_a_429_on_two_fichas_without_price_is_still_failed(world):
    """Una 404 y otra 429, ninguna con precio: el 429 puede esconder el precio → failed (conservador)."""
    name = "Organizador cocina"
    FakeVendure.products = [_product("1", name)]
    world.ml.search[name] = [_candidate("MLA1", "Organizador de cocina"), _candidate("MLA7", "Organizador cocina")]
    world.image_scores[ML_IMG.format("MLA7")] = 0.95
    world.ml.items["MLA7"] = 404
    world.ml.items["MLA1"] = 429

    await price_monitor.run_price_monitor()

    s = _snaps()["1"]
    assert s.ml_status == "failed" and s.ml_error.startswith("vendedores ML")
