"""Corrida #15 (20-oct-2026): 1.810 de 1.869 productos quedaron «sin dato». Lo que se corrige acá:

  a) un 404 al pedir los vendedores de UNA ficha de la API no deja el producto `failed`: esa ficha queda idéntica
     sin precio, con su motivo, y el producto sigue (otras fichas, la web, la oficina). 429/5xx siguen siendo fallo.
  b) el juez se gastaba el cupo en fichas de la API (hasta 3 búsquedas por producto) antes de llegar a los
     candidatos de la web y la oficina, que son los que traen precio: ahora la API se juzga solo en la primera
     búsqueda con dudas (`pm_vision_api_variants`) y no puede pasar de su parte del tope (`pm_vision_web_reserve_pct`).
  c) corta-circuito: tras `pm_vision_fail_streak` fallas seguidas el juez se apaga por el resto de la corrida, con
     el motivo en la corrida; lo que no se consultó (tope, fotos, corte) queda contado.
  d) «No medible» aparte de «Sin dato»: lo que no se puede medir por cómo está cargado (sin precio en Vendure, sin
     foto, sin nombre, otra moneda) no infla el chip «Sin dato». Un `skipped` por cupo de ML sigue siendo sin dato.

Vendure, ML (API y web), CLIP y el juez son dobles: nada sale a la red."""

from __future__ import annotations

import json
import logging
import os
from types import SimpleNamespace

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db.models import MlWebResult  # noqa: E402
from app.db.session import engine, init_db  # noqa: E402
from app.pricing import daily_budget, judge_images, market_judge, market_match, market_query, price_monitor  # noqa: E402
from tests.test_oficina_semaforo import _store, _wire  # noqa: E402
from tests.test_price_monitor import (  # noqa: E402,F401
    ML_IMG, FakeVendure, _candidate, _listing, _product, _runs, _set, _snaps, world)
from tests.test_price_monitor_routes import _env, client  # noqa: E402,F401
from tests.test_semaforo_web import PHOTO  # noqa: E402


def _clear_oficina() -> None:
    with Session(engine) as s:
        for row in s.exec(select(MlWebResult)).all():
            s.delete(row)
        s.commit()


@pytest.fixture(autouse=True)
def _no_oficina_rows():
    """Antes y DESPUÉS: un resultado fresco de la oficina que quede le cambia el camino a los tests de otros módulos."""
    init_db()
    _clear_oficina()
    yield
    _clear_oficina()


def _only(world, *products):
    FakeVendure.products = list(products)


# ─── a) 404 de una ficha ─────────────────────────────────────────────────────


async def test_a_404_on_the_sellers_of_the_only_igual_is_igual_sin_precio_not_failed(world):
    _only(world, _product("1", "Organizador cocina"))
    world.ml.items["MLA1"] = 404

    result = await price_monitor.run_price_monitor()

    s = _snaps()["1"]
    assert s.ml_status == "no_data" and s.match_state == "igual_sin_precio"
    assert "404" in s.ml_error and "vendedores ML" not in s.ml_error
    [unpriced] = json.loads(s.unpriced_listings)
    assert unpriced["ml_id"] == "MLA1" and "ML no tiene vendedores para esta ficha (404)" in unpriced["notes"]
    assert result["counts"]["failed"] == 0 and _runs()[0].status == "ok"


async def test_a_404_on_one_ficha_does_not_hide_the_price_of_another(world):
    _only(world, _product("1", "Organizador cocina"))
    world.ml.search["Organizador cocina"] = [_candidate("MLA1", "Organizador de cocina"),
                                             _candidate("MLA7", "Organizador cocina")]
    world.image_scores[ML_IMG.format("MLA7")] = 0.95          # la más parecida: se le pide primero
    world.ml.items["MLA7"] = 404

    await price_monitor.run_price_monitor()

    s = _snaps()["1"]
    assert s.ml_status == "ok" and s.match_origin == "api" and s.ml_median_cents == 21_000
    assert [u["ml_id"] for u in json.loads(s.unpriced_listings)] == ["MLA7"]


async def test_a_404_then_the_oficina_without_an_igual_is_no_data_with_both_reasons(world):
    _only(world, _product("1", "Organizador cocina"))
    world.ml.items["MLA1"] = 404
    _store("1", [_wire("MLA901", "Heladera no frost", 250.0)])
    world.image_scores[PHOTO.format("901-MLA1_012025")] = 0.10

    await price_monitor.run_price_monitor()

    s = _snaps()["1"]
    assert s.ml_status == "no_data" and s.match_state == "igual_sin_precio"
    assert "404" in s.ml_error and "ML web (oficina)" in s.ml_error


async def test_a_404_then_the_oficina_with_an_igual_with_price_is_ok(world):
    _only(world, _product("1", "Organizador cocina"))
    world.ml.items["MLA1"] = 404
    _store("1", [_wire("MLA901", "Organizador cocina", 250.0)])
    world.image_scores[PHOTO.format("901-MLA1_012025")] = 0.92

    await price_monitor.run_price_monitor()

    s = _snaps()["1"]
    assert s.ml_status == "ok" and s.match_origin == "oficina" and s.ml_median_cents == 25_000


async def test_a_429_on_the_sellers_is_still_a_failure_even_if_the_oficina_ran_without_a_price(world):
    """Conservador: un 429/5xx persistente puede esconder el precio de una ficha que ES el producto. Sin ningún
    precio, el producto queda `failed` (y vuelve a la cabeza de la fila la noche siguiente), aunque la oficina
    haya corrido."""
    _only(world, _product("1", "Organizador cocina"))
    world.ml.items["MLA1"] = 429
    _store("1", [_wire("MLA901", "Heladera no frost", 250.0)])
    world.image_scores[PHOTO.format("901-MLA1_012025")] = 0.10

    await price_monitor.run_price_monitor()

    s = _snaps()["1"]
    assert s.ml_status == "failed" and s.ml_error.startswith("vendedores ML: ML no respondió")
    assert "ML web (oficina)" in s.ml_error


async def test_a_429_on_the_sellers_but_the_oficina_has_the_price_is_ok(world):
    _only(world, _product("1", "Organizador cocina"))
    world.ml.items["MLA1"] = 503
    _store("1", [_wire("MLA901", "Organizador cocina", 250.0)])
    world.image_scores[PHOTO.format("901-MLA1_012025")] = 0.92

    await price_monitor.run_price_monitor()

    assert _snaps()["1"].ml_status == "ok"


# ─── b) prioridad del juez para web / oficina ────────────────────────────────

NAMES = ["Organizador Doble Ajustable 3 Niveles 40x30 Blanco", "Organizador cocina doble", "Lampara escritorio plegable", "Botella termica acero",
         "Funda silicona tablet", "Cable usb reforzado", "Mate calabaza grande"]


def _ambiguous_catalog(world, n: int, *, variants: int = 1, oficina: bool = False) -> None:
    """`n` productos cuyas búsquedas de la API traen UNA ficha dudosa cada una (foto a medias, mismo nombre) y,
    con `oficina`, un candidato dudoso de la oficina. Ids: API = MLA1{producto}{búsqueda}, oficina = MLA9{producto}0."""
    FakeVendure.products = [_product(str(i), NAMES[i - 1]) for i in range(1, n + 1)]
    _set("pm_ml_query_variants", variants)
    for i in range(1, n + 1):
        name = NAMES[i - 1]
        for k, step in enumerate(st for st in market_query.query_plan(name, variants) if not st.only_if_empty):
            ml_id = f"MLA1{i}{k}"
            world.ml.search[step.query] = [_candidate(ml_id, name + " compatible")]
            world.image_scores[ML_IMG.format(ml_id)] = 0.5
        if oficina:
            _store(str(i), [_wire(f"MLA9{i}0", name + " compatible", 250.0)])
            world.image_scores[PHOTO.format(f"9{i}0-MLA1_012025")] = 0.5


def _install_judge(monkeypatch) -> list[list[str]]:
    """Un juez que se porta como el real con el cupo (reserva atómica y `on_outcome`) y dice «similar» a todo."""
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
        return market_judge.JudgeResult(verdicts={
            c.ml_id: market_judge.JudgeVerdict(c.ml_id, False, 0.9, "otro modelo", market_judge.CAT_SIMILAR, ("modelo",))
            for c in candidates})

    monkeypatch.setattr(market_judge, "enabled", lambda: True)
    monkeypatch.setattr(market_judge, "make_client", lambda: None)
    monkeypatch.setattr(market_judge, "judge", judge)
    return calls


def _api(calls):
    return [c for c in calls if c[0].startswith("MLA1")]


def _web(calls):
    return [c for c in calls if c[0].startswith("MLA9")]


async def test_b_by_default_the_api_is_judged_only_in_the_first_search_with_doubts(world, monkeypatch):
    calls = _install_judge(monkeypatch)
    _ambiguous_catalog(world, 1, variants=3)
    _set("pm_vision_max_calls", 100)

    await price_monitor.run_price_monitor()

    assert calls == [["MLA110"]], "solo la primera búsqueda de la API va al juez"
    run = _runs()[0]
    assert (run.llm_calls, run.llm_skipped_api) == (1, 2)
    similars = json.loads(_snaps()["1"].similar_listings)
    assert {e["ml_id"]: e["source"] for e in similars} == {
        "MLA110": "llm", "MLA111": market_match.SOURCE_UNCONFIRMED, "MLA112": market_match.SOURCE_UNCONFIRMED}


async def test_b_with_three_api_variants_it_is_like_before(world, monkeypatch):
    calls = _install_judge(monkeypatch)
    _ambiguous_catalog(world, 1, variants=3)
    _set("pm_vision_max_calls", 100)
    _set("pm_vision_api_variants", 3)

    await price_monitor.run_price_monitor()

    assert calls == [["MLA110"], ["MLA111"], ["MLA112"]] and _runs()[0].llm_skipped_api == 0


async def test_b_the_api_cannot_eat_the_part_of_the_cap_reserved_for_web_and_oficina(world, monkeypatch):
    """Tope 4, 50 % reservado: las fichas de la API llegan hasta 2 en el contador del día; la oficina, hasta 4."""
    calls = _install_judge(monkeypatch)
    _ambiguous_catalog(world, 4, oficina=True)
    _set("pm_vision_max_calls", 4)
    _set("pm_ml_concurrency", 1)

    await price_monitor.run_price_monitor()

    assert _api(calls) == [["MLA110"]]
    assert _web(calls) == [["MLA910"], ["MLA920"], ["MLA930"]]
    assert daily_budget.used_today(market_judge.LLM_COUNTER_KEY) == 4
    run = _runs()[0]
    # 3 productos sin juez para sus fichas de la API (su parte del tope) y el último sin juez para la oficina (tope).
    assert (run.llm_calls, run.llm_skipped_api, run.llm_skipped_cap) == (4, 3, 1)


async def test_b_without_reserve_the_api_eats_the_cap_as_before(world, monkeypatch):
    calls = _install_judge(monkeypatch)
    _ambiguous_catalog(world, 4, oficina=True)
    _set("pm_vision_max_calls", 4)
    _set("pm_ml_concurrency", 1)
    _set("pm_vision_web_reserve_pct", 0)

    await price_monitor.run_price_monitor()

    assert _api(calls) == [["MLA110"], ["MLA120"]] and _web(calls) == [["MLA910"], ["MLA920"]]


async def test_b_with_all_the_cap_reserved_the_api_never_goes_to_the_judge(world, monkeypatch):
    calls = _install_judge(monkeypatch)
    _ambiguous_catalog(world, 2, oficina=True)
    _set("pm_vision_max_calls", 10)
    _set("pm_vision_web_reserve_pct", 100)

    await price_monitor.run_price_monitor()

    assert _api(calls) == [] and sorted(_web(calls)) == [["MLA910"], ["MLA920"]]
    assert _runs()[0].llm_skipped_api == 2


def test_b_the_new_settings_are_runtime_editable_with_sane_defaults():
    from app import runtime
    metas = {m.key: m for m in runtime.SETTINGS_SCHEMA}
    for key, default, lo, hi in (("pm_vision_web_reserve_pct", 50, 0, 100), ("pm_vision_api_variants", 1, 0, 3),
                                 ("pm_vision_fail_streak", 5, 0, 100)):
        m = metas[key]
        assert (Settings.model_fields[m.default_attr].default, m.min, m.max, m.group) == (default, lo, hi, "monitor")


# ─── c) corta-circuito del juez ──────────────────────────────────────────────


class _HttpError(Exception):
    """Como openai.APIStatusError: status y código, y un mensaje con el body del proveedor (que no tiene que salir)."""

    def __init__(self, status: int, code: str):
        super().__init__(f"Error code: {status} - {{'error': 'Bearer sk-secreta-123 rechazada'}}")
        self.status_code, self.code = status, code


class _ScriptedClient:
    """Cliente OpenAI-compatible de mentira: cada llamada toma el siguiente paso del guion (una excepción o un texto)."""

    def __init__(self, script):
        self.script, self.calls = list(script), 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def close(self):
        return None

    async def _create(self, **kwargs):
        self.calls += 1
        step = self.script.pop(0) if self.script else _HttpError(500, "-")
        if isinstance(step, Exception):
            raise step
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=step))],
                               usage=SimpleNamespace(prompt_tokens=500, completion_tokens=40,
                                                     completion_tokens_details=None))


def _real_judge(monkeypatch, script, **settings) -> _ScriptedClient:
    """El juez REAL (market_judge.judge) con un proveedor de mentira."""
    base = dict(vendure_api_url="https://example.invalid/admin-api", pm_llm_base_url="https://llm.invalid/v1",
                pm_llm_api_key="k", pm_llm_model="qwen3-vl-plus")
    base.update(settings)
    monkeypatch.setattr(market_judge, "get_settings", lambda: Settings(**base))
    client = _ScriptedClient(script)
    monkeypatch.setattr(market_judge, "make_client", lambda: client)
    return client


def _good(i: int) -> str:
    return json.dumps({"results": [{"ml_id": f"MLA1{i}0", "verdict": "similar", "confidence": 0.9, "reason": "x"}]})


async def test_c_five_failures_in_a_row_cut_the_judge_for_the_rest_of_the_run(world, monkeypatch, caplog):
    client = _real_judge(monkeypatch, [_HttpError(401, "invalid_api_key")] * 10)
    _ambiguous_catalog(world, 7)
    _set("pm_vision_max_calls", 100)
    _set("pm_ml_concurrency", 1)

    with caplog.at_level(logging.WARNING, logger="app.pricing.price_monitor"):
        await price_monitor.run_price_monitor()

    assert client.calls == 5, "después del corte no se llama más"
    run = _runs()[0]
    assert run.llm_status == "juez cortado: 5 fallas seguidas (motivo: HTTP 401, code=invalid_api_key)"
    assert (run.llm_calls, run.llm_failures, run.llm_skipped_cut) == (5, 5, 2)
    assert daily_budget.used_today(market_judge.LLM_COUNTER_KEY) == 5
    assert "sk-secreta" not in (run.llm_status or "") + caplog.text
    assert caplog.text.count("juez cortado") == 1
    # Los productos sin juez quedan «sin confirmar», como siempre.
    for snap in _snaps().values():
        assert snap.ml_status == "no_data" and snap.match_state == "similar"
        assert {e["source"] for e in json.loads(snap.similar_listings)} == {market_match.SOURCE_UNCONFIRMED}
    llm = price_monitor.run_to_dict(run)["llm"]
    assert llm["cut"] == run.llm_status and llm["failures"] == 5 and llm["skipped"]["cut"] == 2


async def test_c_a_good_answer_resets_the_streak(world, monkeypatch):
    script = [_HttpError(503, "-")] * 4 + [_good(5)] + [_HttpError(503, "-")] * 2
    client = _real_judge(monkeypatch, script)
    _ambiguous_catalog(world, 7)
    _set("pm_vision_max_calls", 100)
    _set("pm_ml_concurrency", 1)

    await price_monitor.run_price_monitor()

    run = _runs()[0]
    assert client.calls == 7 and run.llm_status is None
    assert (run.llm_failures, run.llm_skipped_cut) == (6, 0)


async def test_c_empty_and_unreadable_answers_count_as_failures(world, monkeypatch):
    client = _real_judge(monkeypatch, ["", "no es json", "", "tampoco", ""])
    _ambiguous_catalog(world, 6)
    _set("pm_vision_max_calls", 100)
    _set("pm_ml_concurrency", 1)

    await price_monitor.run_price_monitor()

    run = _runs()[0]
    assert client.calls == 5 and run.llm_status.startswith("juez cortado: 5 fallas seguidas")
    assert run.llm_skipped_cut == 1


async def test_c_with_streak_zero_it_never_cuts(world, monkeypatch):
    client = _real_judge(monkeypatch, [_HttpError(429, "rate_limit")] * 10)
    _ambiguous_catalog(world, 7)
    _set("pm_vision_max_calls", 100)
    _set("pm_vision_fail_streak", 0)

    await price_monitor.run_price_monitor()

    run = _runs()[0]
    assert client.calls == 7 and run.llm_status is None and run.llm_failures == 7


async def test_c_the_daily_cap_is_counted_and_warned_once_per_run(world, monkeypatch, caplog):
    client = _real_judge(monkeypatch, [_good(i) for i in range(1, 8)])
    _ambiguous_catalog(world, 5, oficina=True)
    _set("pm_vision_max_calls", 2)
    _set("pm_vision_web_reserve_pct", 0)
    _set("pm_ml_concurrency", 1)

    with caplog.at_level(logging.WARNING, logger="app.pricing.price_monitor"):
        await price_monitor.run_price_monitor()

    run = _runs()[0]
    assert client.calls == 2 and run.llm_calls == 2
    # 5 productos × (API + oficina) = 10 consultas; 2 se hicieron y 8 chocaron con el tope.
    assert run.llm_skipped_cap == 8 and run.llm_failures == 0 and run.llm_status is None
    assert caplog.text.count("tope diario del juez alcanzado para ML") == 1


async def test_c_no_downloadable_photo_of_ours_is_counted(world, monkeypatch):
    client = _real_judge(monkeypatch, [], pm_llm_base_url="https://api.xiaomimimo.com/v1")   # MiMo → base64

    async def nothing(urls):
        return {}

    monkeypatch.setattr(judge_images, "inline_images", nothing)
    _ambiguous_catalog(world, 2)
    _set("pm_vision_max_calls", 100)

    await price_monitor.run_price_monitor()

    run = _runs()[0]
    assert client.calls == 0 and run.llm_calls == 0
    assert (run.llm_skipped_photos, run.llm_failures, run.llm_status) == (2, 0, None)


async def test_c_the_breaker_is_shared_with_the_stores_and_a_raising_double_counts_as_failure(world, monkeypatch):
    """Un juez que revienta (no el real: un doble que lanza) también suma a la racha."""
    _ambiguous_catalog(world, 6)
    _set("pm_vision_max_calls", 100)
    _set("pm_vision_fail_streak", 2)
    _set("pm_ml_concurrency", 1)
    calls = []

    async def judge(*a, on_reserve=None, max_calls=0, **kw):
        assert await daily_budget.reserve_async(market_judge.LLM_COUNTER_KEY, max_calls, None, on_reserve)
        calls.append(1)
        raise _HttpError(400, "bad_request")

    monkeypatch.setattr(market_judge, "enabled", lambda: True)
    monkeypatch.setattr(market_judge, "make_client", lambda: None)
    monkeypatch.setattr(market_judge, "judge", judge)

    await price_monitor.run_price_monitor()

    run = _runs()[0]
    assert len(calls) == 2 and run.llm_status == "juez cortado: 2 fallas seguidas (motivo: HTTP 400, code=bad_request)"
    assert run.llm_skipped_cut == 4


# ─── d) «No medible» aparte de «Sin dato» ────────────────────────────────────


def _unmeasurable_catalog(world) -> None:
    no_photo = _product("4", "Accesorio sin foto")
    no_photo.image_urls, no_photo.featured_image_url = [], None
    FakeVendure.products = [
        _product("1", "Producto raro"),                          # medible, ML no tiene nada: sin dato de verdad
        _product("2", "Sin precio", price=None),
        _product("3", "Reloj importado", currency="USD"),
        no_photo,
        _product("5", ""),                                       # sin nombre para buscar
        _product("6", "Taza ceramica"),                          # medible, pero se acaba el cupo de ML
    ]
    world.ml.search["Producto raro"] = []
    _set("pm_ml_daily_budget", 1)
    _set("pm_ml_concurrency", 1)


async def test_d_unmeasurable_products_are_marked_and_budget_skips_are_not(world):
    _unmeasurable_catalog(world)

    await price_monitor.run_price_monitor()

    snaps = _snaps()
    assert {pid: (s.ml_status, s.unmeasurable) for pid, s in snaps.items()} == {
        "1": ("no_data", None), "2": ("skipped", "sin_precio"), "3": ("skipped", "moneda"),
        "4": ("skipped", "sin_foto"), "5": ("skipped", "sin_nombre"), "6": ("skipped", None)}
    assert snaps["6"].ml_error.startswith("budget ML agotado")
    assert {s.color for s in snaps.values()} == {"sin_dato"}              # en la base, el color no cambia
    run = _runs()[0]
    assert (run.n_sin_dato, run.n_no_medible, run.n_skipped) == (6, 4, 5)
    colors = price_monitor.run_to_dict(run)["colors"]
    assert colors == {"verde": 0, "amarillo": 0, "rojo": 0, "sin_dato": 2, "no_medible": 4}


async def test_d_the_api_has_a_no_medible_chip_and_filter_and_sin_dato_leaves_them_out(world, client):
    _unmeasurable_catalog(world)
    await price_monitor.run_price_monitor()

    def ids(query: str) -> list[str]:
        r = client.get(f"/api/price-monitor/snapshots?page_size=50{query}")
        assert r.status_code == 200
        return sorted(i["product"]["id"] for i in r.json()["items"])

    body = client.get("/api/price-monitor/snapshots").json()
    assert body["colors"] == {"verde": 0, "amarillo": 0, "rojo": 0, "sin_dato": 2, "no_medible": 4}
    assert ids("&color=sin_dato") == ["1", "6"]
    assert ids("&color=no_medible") == ["2", "3", "4", "5"]
    assert ids("&status=skipped") == ["2", "3", "4", "5", "6"]          # el estado de ML no cambia
    by_id = {i["product"]["id"]: i for i in body["items"]}
    assert by_id["2"]["unmeasurable"] == "sin_precio" and by_id["6"]["unmeasurable"] is None
    assert client.get("/api/price-monitor/snapshots?color=violeta").status_code == 400
    # La card de Salud y el historial de corridas, igual.
    summary = client.get("/api/price-monitor/summary").json()
    assert summary["last_run"]["colors"]["no_medible"] == 4 and summary["last_run"]["colors"]["sin_dato"] == 2
    assert client.get("/api/price-monitor/runs").json()["items"][0]["colors"]["no_medible"] == 4


async def test_d_the_no_medible_chip_follows_the_other_filters(world, client):
    _unmeasurable_catalog(world)
    _set("pm_include_disabled", 1)
    FakeVendure.products.append(_product("7", "Sin precio viejo", price=None, enabled=False))
    await price_monitor.run_price_monitor()

    only_disabled = client.get("/api/price-monitor/snapshots?enabled=disabled").json()
    assert only_disabled["colors"]["no_medible"] == 1 and only_disabled["colors"]["sin_dato"] == 0
    assert client.get("/api/price-monitor/snapshots?enabled=enabled").json()["colors"]["no_medible"] == 4


# ─── migración automática (init_db) ──────────────────────────────────────────


def test_the_new_columns_are_added_to_the_existing_tables_with_defaults(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect, text
    from sqlmodel import SQLModel

    from app.db import session

    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE market_price_snapshot (id INTEGER PRIMARY KEY, run_id INTEGER, "
                          "product_id VARCHAR(64), ml_status VARCHAR(16), color VARCHAR(16))"))
        conn.execute(text("INSERT INTO market_price_snapshot (run_id, product_id, ml_status, color) "
                          "VALUES (1, '7', 'skipped', 'sin_dato')"))
        conn.execute(text("CREATE TABLE price_monitor_run (id INTEGER PRIMARY KEY, status VARCHAR, llm_calls INTEGER)"))
        conn.execute(text("INSERT INTO price_monitor_run (status, llm_calls) VALUES ('ok', 2280)"))
    monkeypatch.setattr(session, "engine", eng)

    SQLModel.metadata.create_all(eng)
    session._add_missing_columns()

    assert "unmeasurable" in {c["name"] for c in inspect(eng).get_columns("market_price_snapshot")}
    new_run_cols = ("llm_failures", "llm_skipped_cap", "llm_skipped_api", "llm_skipped_photos", "llm_skipped_cut",
                    "llm_status", "n_no_medible")
    assert set(new_run_cols) <= {c["name"] for c in inspect(eng).get_columns("price_monitor_run")}
    with eng.connect() as conn:
        assert conn.execute(text("SELECT unmeasurable FROM market_price_snapshot")).one() == (None,)
        row = conn.execute(text(f"SELECT {', '.join(new_run_cols)} FROM price_monitor_run")).one()
        assert tuple(row) == (0, 0, 0, 0, 0, None, 0)
