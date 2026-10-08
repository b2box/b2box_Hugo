"""QA independiente de la rama feat/semaforo-ml-web (criterios 1 a 4).

Complementa los tests del developer con tres cosas que ellos no hacen:

  * un DORADO sacado de origin/main (tests/golden/semaforo_origin_main.json): con
    la web apagada (cupo 0, o sin BROWSER_PROXY) y el chequeo de medidas en 0, la
    rama tiene que producir EXACTAMENTE lo que producía main para el mismo mundo
    (snapshots, corrida y requests a ML, con y sin juez de sí/no);
  * propiedades: un SIMILAR a cualquier precio nunca mueve mediana, mínimo,
    ganancia ni color; el cupo y la concurrencia web se respetan con carreras;
  * el camino de Vendure a nivel TRANSPORTE con productos deshabilitados, la web y
    el juez prendidos (nada que no sea una query viaja hacia Vendure).

Todo con dobles: no sale nada a la red ni se abre un navegador.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app import runtime  # noqa: E402
from app.ingest import browser_fetch  # noqa: E402
from app.pricing import daily_budget, market_judge, market_ml_web, price_monitor  # noqa: E402
from app.vendure import client as vendure_client_mod  # noqa: E402
from tests.ml_web_fixtures import ANTIBOT_HTML, page  # noqa: E402
from tests.test_price_monitor import (  # noqa: E402,F401
    _REAL_EXECUTE,
    FakeVendure,
    _candidate,
    _listing,
    _product,
    _raw_vendure_product,
    _runs,
    _set,
    _snaps,
    world,
)
from tests.test_semaforo_web import _card, _score, _web_page, webw  # noqa: E402,F401

ML_IMG = "https://http2.mlstatic.com/D_NQ_{}.jpg"
GOLDEN = json.loads((Path(__file__).parent / "golden" / "semaforo_origin_main.json").read_text())

SNAP_COLS = [
    "product_id", "ml_status", "ml_error", "ml_median_cents", "ml_min_cents", "ml_listing_count",
    "ml_seller_count", "ml_currency", "match_source", "match_confidence", "image_score_max",
    "name_score_max", "candidates_count", "ambiguous_count", "our_price_cents", "variant_id",
    "tier_used", "commission_pct", "shipping_cents", "est_margin_pct", "color", "prev_color",
    "product_name", "product_code"]
RUN_COLS = [
    "status", "mode", "total_products", "processed", "n_ok", "n_no_data", "n_failed", "n_skipped",
    "n_verde", "n_amarillo", "n_rojo", "n_sin_dato", "ml_requests_used", "llm_calls",
    "llm_input_tokens", "llm_output_tokens", "error"]
# Las claves de cada publicación que ya existían en main (la rama agrega más).
MATCH_KEYS = ["ml_id", "title", "permalink", "listings", "min_cents", "median_cents", "source",
              "image_score", "name_score", "confidence"]


# ─── el mundo "rico" (el mismo con el que se sacó el dorado de main) ───────


def _rich(world):
    FakeVendure.products = [
        _product("1", "Organizador cocina"),
        _product("2", "Lampara LED escritorio"),
        _product("3", "Producto raro"),
        _product("5", "Sin precio", price=None),
        _product("6", "Taza ceramica"),
        _product("7", "Soporte celular auto magnetico"),
        _product("8", "Cable usb tipo c reforzado"),
        _product("9", "Mate de calabaza"),
        _product("10", "Botella termica acero", price=50_000),
        _product("11", "Funda silicona celular"),
        _product("12", "Cargador inalambrico rapido"),
        _product("13", "Alfombra bano antideslizante gris oscuro 40x60 extra"),
        _product("14", "Reloj digital", currency="USD"),
        _product("15", "Organizador cocina", enabled=False),
        _product("16", "Pack x3 Vaso termico 500 ml"),
    ]
    ml = world.ml
    ml.search.update({
        "Soporte celular auto magnetico": [_candidate("MLA7", "Holder smartphone coche")],
        "Cable usb tipo c reforzado": [_candidate("MLA8", "Cable de datos celular")],
        "Mate de calabaza": [_candidate("MLA9", "Mate de calabaza")],
        "Botella termica acero": [_candidate("MLA10", "Botella termica de acero")],
        "Funda silicona celular": [_candidate(f"MLA11{i}", f"Funda silicona celular {i}") for i in range(6)],
        "Cargador inalambrico rapido": [_candidate("MLA12", "Cargador inalambrico rapido")],
        "Alfombra bano antideslizante gris oscuro 40x60 extra": [],
        "Alfombra bano antideslizante gris": [_candidate("MLA13", "Alfombra bano antideslizante")],
        "Pack x3 Vaso termico 500 ml": [_candidate("MLA16", "Vaso termico 500 ml")],
        "Organizador cocina": [_candidate("MLA1", "Organizador de cocina")],
    })
    ml.items.update({
        "MLA7": [_listing("I7", "201", 100.0), _listing("I7b", "202", 130.0)],
        "MLA8": [_listing("I8", "203", 40.0)],
        "MLA9": [_listing("I9", "204", 50.0), {**_listing("I9b", "205", 20.0), "currency_id": "USD"}],
        "MLA10": [_listing("I10", "206", 600.0)],
        "MLA110": [_listing("I110", "207", 70.0)], "MLA111": [_listing("I111", "208", 75.0)],
        "MLA112": [_listing("I112", "209", 80.0)], "MLA113": [_listing("I113", "210", 85.0)],
        "MLA114": [_listing("I114", "211", 90.0)], "MLA115": [_listing("I115", "212", 95.0)],
        "MLA12": [_listing("I12", "213", 300.0)],
        "MLA13": [_listing("I13", "214", 120.0)],
        "MLA16": [_listing("I16", "215", 100.0)],
    })
    ml.users.update({str(s): 500 for s in range(201, 216)})
    ml.users["213"] = 5                       # vendedor con pocas ventas
    for ref, v in {"MLA7": 0.70, "MLA8": 0.70, "MLA9": 0.85, "MLA10": 0.9, "MLA110": 0.9, "MLA111": 0.88,
                   "MLA112": 0.86, "MLA113": 0.84, "MLA114": 0.82, "MLA115": 0.81, "MLA12": 0.9,
                   "MLA13": 0.9, "MLA16": 0.95}.items():
        world.image_scores[ML_IMG.format(ref)] = v


def _fake_client():
    return type("C", (), {"close": lambda s: None})()


@pytest.fixture
def judge_yes_no(monkeypatch):
    """Juez del formato viejo (sí/no): MLA7 es el mismo, el resto no."""
    monkeypatch.setattr(market_judge, "enabled", lambda: True)
    monkeypatch.setattr(market_judge, "make_client", _fake_client)
    _set("pm_vision_max_calls", 50)

    async def judge(our_name, photos, candidates, *, max_calls, on_reserve=None, **kw):
        assert await daily_budget.reserve_async(market_judge.LLM_COUNTER_KEY, max_calls, None, on_reserve)
        verdicts = {}
        for c in candidates:
            same = c.ml_id == "MLA7"
            verdicts[c.ml_id] = market_judge.JudgeVerdict(c.ml_id, same, 0.9 if same else 0.8, "x")
        return market_judge.JudgeResult(verdicts=verdicts, input_tokens=100, output_tokens=10, cost_usd=0.001)

    monkeypatch.setattr(market_judge, "judge", judge)


@pytest.fixture
def judge_igual(monkeypatch):
    """Juez de tres valores que dice «igual» a todo."""
    monkeypatch.setattr(market_judge, "enabled", lambda: True)
    monkeypatch.setattr(market_judge, "make_client", _fake_client)
    _set("pm_vision_max_calls", 5)

    async def judge(our_name, photos, candidates, *, max_calls, on_reserve=None, **kw):
        assert await daily_budget.reserve_async(market_judge.LLM_COUNTER_KEY, max_calls, None, on_reserve)
        return market_judge.JudgeResult(verdicts={
            c.ml_id: market_judge.JudgeVerdict(c.ml_id, True, 0.9, "ok", "igual", ()) for c in candidates})

    monkeypatch.setattr(market_judge, "judge", judge)


@pytest.fixture(params=["budget_0", "no_proxy"])
def web_off(request, monkeypatch):
    """La web apagada de las dos formas. Si algo intentara abrir un navegador, falla."""
    launched: list[str] = []

    def boom(*a, **k):
        launched.append("browser")
        raise AssertionError("se intentó lanzar un navegador con la web apagada")

    monkeypatch.setattr(browser_fetch, "ListingBrowser", boom)
    monkeypatch.setattr(browser_fetch, "_camoufox", boom)
    monkeypatch.setattr(browser_fetch, "available", lambda: True)
    if request.param == "budget_0":
        monkeypatch.setattr(browser_fetch, "_proxy_config", lambda: {"server": "http://proxy.invalid:1"})
        _set("pm_ml_web_daily_budget", 0)
    else:
        monkeypatch.setattr(browser_fetch, "_proxy_config", lambda: None)
        _set("pm_ml_web_daily_budget", 2000)
    yield request.param
    assert launched == []


def _dump(world) -> dict:
    snaps = {pid: {c: getattr(s, c) for c in SNAP_COLS} for pid, s in sorted(_snaps().items())}
    for pid, s in _snaps().items():
        snaps[pid]["matched_listings"] = [
            {k: m.get(k) for k in MATCH_KEYS} for m in json.loads(s.matched_listings or "[]")]
    run = _runs()[-1]
    return {"snaps": snaps, "run": {c: getattr(run, c) for c in RUN_COLS},
            "ml_calls": sorted(world.ml.calls), "forbidden": FakeVendure.forbidden}


def _roundtrip(d: dict) -> dict:
    return json.loads(json.dumps(d, sort_keys=True, default=str))


# ─── criterio 1: web apagada = lo mismo que origin/main ────────────────────


async def test_web_off_reproduces_origin_main_with_a_yes_no_judge(world, web_off, judge_yes_no):
    """Mundo de 14 productos (API, 429, fichas ambiguas con juez, USD, ventas bajas,
    segunda búsqueda corta, sin precio, 6 fichas) comparado columna a columna con lo
    que dio origin/main. El chequeo de medidas va en 0 porque es una función NUEVA
    que no depende de la web (ver el test siguiente)."""
    _set("pm_spec_check", 0)
    _rich(world)
    _set("pm_ml_concurrency", 1)
    await price_monitor.run_price_monitor()
    assert _roundtrip(_dump(world)) == GOLDEN["rich_judge"]
    run = _runs()[-1]
    assert run.web_searches == 0 and run.web_bytes == 0 and run.web_blocked == 0
    assert run.web_status.startswith("apagada:")
    assert all(s.web_state is None and s.web_searches == 0 for s in _snaps().values())
    assert all(s.match_origin in (None, "api") for s in _snaps().values())


async def test_web_off_with_a_tight_ml_budget_reproduces_origin_main(world, web_off):
    _set("pm_spec_check", 0)
    _rich(world)
    _set("pm_ml_concurrency", 1)
    _set("pm_ml_daily_budget", 9)
    await price_monitor.run_price_monitor()
    assert _roundtrip(_dump(world)) == GOLDEN["rich_budget9"]


async def test_web_off_with_the_default_spec_check_only_differs_on_a_different_pack(world, web_off, judge_yes_no):
    """Con pm_spec_check=1 (default) la diferencia con main es SOLO el producto
    «Pack x3 Vaso termico 500 ml» contra una publicación sin cantidad: ahora es
    SIMILAR (pedido de Nico), antes era IGUAL. Nada más cambia."""
    _rich(world)
    _set("pm_ml_concurrency", 1)
    await price_monitor.run_price_monitor()
    got, want = _roundtrip(_dump(world)), GOLDEN["rich_judge"]
    changed = {pid for pid in want["snaps"] if got["snaps"][pid] != want["snaps"][pid]}
    assert changed == {"16"}
    s16 = got["snaps"]["16"]
    assert (s16["ml_status"], s16["color"], s16["ml_median_cents"]) == ("no_data", "sin_dato", None)
    similar = json.loads(_snaps()["16"].similar_listings)
    assert [(x["ml_id"], x["differences"], x["source"]) for x in similar] == [("MLA16", ["cantidad"], "specs")]
    # La publicación que se descarta es la única que ya no se pide.
    assert set(want["ml_calls"]) - set(got["ml_calls"]) <= {"items:MLA16", "users:215"}


# ─── criterio 2: un SIMILAR a cualquier precio no mueve nada ───────────────


FIELDS = ("ml_status", "ml_median_cents", "ml_min_cents", "ml_listing_count", "ml_seller_count",
          "est_margin_pct", "color", "match_origin", "ml_error")


@pytest.mark.parametrize("similar_price", [1.0, 9_999_999.0])
@pytest.mark.parametrize("n_similar", [1, 3])
async def test_similar_publications_at_any_price_leave_every_number_alone(webw, similar_price, n_similar):
    igual = [_card("MLA901", "Producto Raro", 250.0), _card("MLA902", "Producto Raro Premium", 300.0, seller="Dos")]
    webw.web.pages["producto-raro"] = _web_page(*igual)
    for ref in ("MLA901", "MLA902"):
        _score(webw, ref, 0.9)
    await price_monitor.run_price_monitor()
    before = _snaps()["3"]

    sims = [_card(f"MLA91{i}", f"Pack X{6 + i} Producto Raro", similar_price, seller=f"Pack{i}")
            for i in range(n_similar)]
    webw.web.pages["producto-raro"] = _web_page(*igual, *sims)
    for i in range(n_similar):
        _score(webw, f"MLA91{i}", 0.97)
    _set("pm_ml_concurrency", 1)
    await price_monitor.run_price_monitor()
    after = _snaps(_runs()[1].id)["3"]

    assert after.similar_count == n_similar
    for f in FIELDS:
        assert getattr(after, f) == getattr(before, f), f
    assert {m["ml_id"] for m in json.loads(after.matched_listings)} == {"MLA901", "MLA902"}
    assert after.color == "verde"          # ni el de 1 peso ni el de 10 millones lo tocan


async def test_only_similars_never_give_a_price_or_a_color(webw):
    sims = [_card(f"MLA92{i}", f"Set X{3 + i} Producto Raro", 123.0 + i) for i in range(4)]
    webw.web.pages["producto-raro"] = _web_page(*sims)
    for i in range(4):
        _score(webw, f"MLA92{i}", 0.95)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert (s.ml_status, s.color, s.ml_median_cents, s.ml_min_cents, s.est_margin_pct) == (
        "no_data", "sin_dato", None, None, None)
    assert s.similar_count == 4 and s.ml_listing_count == 0 and s.match_origin is None


# ─── criterio 3: cupo atómico, concurrencia, pausa, bloqueos ────────────────


def _page_ok():
    return _web_page(_card("MLA990", "Algo", 100.0))


async def test_budget_is_exact_under_a_race_and_concurrency_is_bounded(world):
    in_flight = peak = calls = 0

    async def fetch(url):
        nonlocal in_flight, peak, calls
        calls += 1
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.005)
        in_flight -= 1
        return _page_ok()

    async def sleep(_):
        await asyncio.sleep(0)

    src = market_ml_web.MlWebSource(budget=7, concurrency=2, pause_s=1, fetcher=fetch, sleep=sleep)
    res = await asyncio.gather(*(src.search(f"producto numero {i}") for i in range(40)))
    kinds = [r.kind for r in res]
    assert kinds.count("ok") == 7 and calls == 7
    assert set(kinds) - {"ok"} <= {"budget", "off"}
    assert peak <= 2
    assert daily_budget.used_today(market_ml_web.WEB_COUNTER_KEY) == 7
    assert src.exhausted and src.status_text() == "sin cupo diario de búsquedas web"


async def test_two_sources_racing_for_the_same_day_never_overspend(world):
    async def fetch(url):
        await asyncio.sleep(0.001)
        return _page_ok()

    async def sleep(_):
        return None

    a = market_ml_web.MlWebSource(budget=11, concurrency=2, pause_s=0, fetcher=fetch, sleep=sleep)
    b = market_ml_web.MlWebSource(budget=11, concurrency=2, pause_s=0, fetcher=fetch, sleep=sleep)
    res = await asyncio.gather(*(s.search(f"producto numero {i}") for i in range(30) for s in (a, b)))
    assert sum(r.kind == "ok" for r in res) == 11
    assert daily_budget.used_today(market_ml_web.WEB_COUNTER_KEY) == 11


@pytest.mark.parametrize("jitter, factor", [(0.0, 0.7), (0.5, 1.0), (1.0, 1.3)])
async def test_the_pause_happens_inside_the_slot_between_every_pair_of_searches(world, jitter, factor):
    events: list[str] = []
    pauses: list[float] = []

    async def fetch(url):
        events.append("fetch")
        await asyncio.sleep(0.002)
        events.append("done")
        return _page_ok()

    async def sleep(s):
        pauses.append(s)
        events.append("pause")
        await asyncio.sleep(0.01)         # tiempo de sobra para que otra búsqueda se cuele si pudiera
        events.append("woke")

    src = market_ml_web.MlWebSource(budget=100, concurrency=1, pause_s=4.0, fetcher=fetch, sleep=sleep,
                                    jitter=lambda: jitter)
    await asyncio.gather(*(src.search(f"producto numero {i}") for i in range(4)))
    # con UN solo lugar nunca se solapan: fetch, done, pause, woke, fetch, ...
    assert events == ["fetch", "done", "pause", "woke"] * 4
    assert pauses == pytest.approx([4.0 * factor] * 4)       # 4 s ± 30 %


async def test_a_blocked_search_is_not_retried_nor_followed_by_the_short_query(webw):
    FakeVendure.products = [_product("3", "Organizador Doble Ajustable 3 Niveles 40x30 Blanco")]
    webw.web.pages["organizador-doble-ajustable-3-niveles-40x30-blanco"] = page(ANTIBOT_HTML)
    webw.web.pages["organizador-doble-ajustable-3"] = _web_page(_card("MLA904", "Organizador doble", 150.0))
    _score(webw, "MLA904", 0.9)
    await price_monitor.run_price_monitor()
    assert len(webw.web.calls) == 1               # ni reintento ni búsqueda corta
    s = _snaps()["3"]
    assert s.ml_status == "no_data" and s.web_state == "blocked" and s.web_searches == 1
    assert "ML web:" in s.ml_error and s.color == "sin_dato"


async def test_five_blocks_in_a_row_with_the_default_streak_cut_the_night_and_the_run_is_ok(webw):
    assert runtime.get("pm_ml_web_block_streak") == 5
    FakeVendure.products = [_product(str(i), f"Producto raro {i}") for i in range(20, 32)]
    for p in FakeVendure.products:
        webw.ml.search[p.name] = []
        webw.web.pages[p.name.lower().replace(" ", "-")] = page(ANTIBOT_HTML)
    _set("pm_ml_concurrency", 4)                   # varios productos pidiendo a la vez
    await price_monitor.run_price_monitor()
    states = sorted(s.web_state for s in _snaps().values())
    assert states == ["blocked"] * 5 + ["off"] * 7
    assert len(webw.web.calls) == 5
    assert daily_budget.used_today(market_ml_web.WEB_COUNTER_KEY) == 5     # lo cortado no gasta cupo
    [run] = _runs()
    assert run.status == "ok" and run.n_failed == 0 and run.web_blocked == 5
    assert "cortada por esta noche" in run.web_status
    assert all(s.ml_status == "no_data" for s in _snaps().values())


async def test_an_empty_listing_in_the_middle_resets_the_streak(webw):
    FakeVendure.products = [_product(str(i), f"Producto raro {i}") for i in range(20, 29)]
    for p in FakeVendure.products:
        webw.ml.search[p.name] = []
    # 4 bloqueos, 1 listado vacío (reinicia la racha), 4 bloqueos más: nunca 5 seguidos
    for i in range(20, 29):
        webw.web.pages[f"producto-raro-{i}"] = _web_page() if i == 24 else page(ANTIBOT_HTML)
    _set("pm_ml_concurrency", 1)
    await price_monitor.run_price_monitor()
    assert "off" not in {s.web_state for s in _snaps().values()}
    [run] = _runs()
    assert run.web_status == "ok" and run.web_blocked == 8


async def test_the_web_blocked_after_the_api_failed_keeps_the_api_reason(webw):
    """La API devolvió 429 (failed) y la web está bloqueada: el producto queda
    `failed` por la API, con el motivo de la web agregado, y no se rompe nada."""
    FakeVendure.products = [_product("2", "Lampara LED escritorio")]
    webw.web.pages["lampara-led-escritorio"] = page(ANTIBOT_HTML)
    await price_monitor.run_price_monitor()
    s = _snaps()["2"]
    assert s.ml_status == "failed" and s.web_state == "blocked"
    assert "429" in s.ml_error and "ML web:" in s.ml_error


# ─── criterio 4: deshabilitados, cero escrituras a nivel transporte ────────


def _raw_with_dims(p, dims):
    raw = _raw_vendure_product(p)
    if dims is not None:
        for v in raw["variantList"]["items"]:
            v["customFields"] = dims
    return raw


DIMS_IN_QUERY = re.compile(r"customFields\s*\{\s*length")


class _NoSleepAsyncio:
    """`asyncio` con `sleep` instantáneo, solo para el módulo del cliente de Vendure
    (sus reintentos con backoff no tienen que demorar el test)."""
    async def sleep(self, _):
        return None

    def __getattr__(self, name):
        return getattr(asyncio, name)


@pytest.fixture
def real_vendure(world, monkeypatch):
    """El VendureClient REAL con el HTTP hacia Vendure interceptado."""
    monkeypatch.setattr(price_monitor, "VendureClient", vendure_client_mod.VendureClient)
    monkeypatch.setattr(vendure_client_mod.VendureClient, "_execute_with_retry", _REAL_EXECUTE)
    monkeypatch.setattr(vendure_client_mod.VendureClient, "_shared_bearer", "qa-bearer")
    monkeypatch.setattr(vendure_client_mod.VendureClient, "_dims_supported", True)
    monkeypatch.setattr(vendure_client_mod, "asyncio", _NoSleepAsyncio())
    sent: list[dict] = []
    state = {"mode": "ok", "dims": {"length": 40.0, "width": 30.0, "height": 5.0, "weight": 0.5,
                                    "boxLength": 60.0, "boxWidth": 40.0, "boxHeight": 40.0, "boxWeight": 12.0}}
    gx = vendure_client_mod._gql_httpx
    real_handle = gx.AsyncHTTPTransport.handle_async_request

    async def intercept(self, request):
        if request.url.host != "example.invalid":
            return await real_handle(self, request)
        body = json.loads(request.content)
        sent.append(body)
        q = body["query"]
        if not q.lstrip().startswith("query"):
            return gx.Response(500, json={"errors": [{"message": "escritura en sombra"}]}, request=request)
        with_dims = bool(DIMS_IN_QUERY.search(q))
        if with_dims and state["mode"] in ("errors_200", "errors_400"):
            err = {"errors": [{"message": 'Cannot query field "length" on type "ProductVariantCustomFields".',
                               "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}}]}
            return gx.Response(200 if state["mode"] == "errors_200" else 400, json=err, request=request)
        items = [_raw_with_dims(p, state["dims"] if with_dims else None) for p in FakeVendure.products]
        return gx.Response(200, json={"data": {"products": {"items": items, "totalItems": len(items)}}},
                           request=request)

    monkeypatch.setattr(gx.AsyncHTTPTransport, "handle_async_request", intercept)
    yield sent, state
    vendure_client_mod.VendureClient._dims_supported = True


async def test_disabled_products_with_web_judge_and_active_mode_only_read_from_vendure(
        webw, real_vendure, judge_igual):
    """pm_include_disabled=1 + pm_mode=1 + web + juez: lo único que viaja a Vendure
    son queries de lectura, y el deshabilitado queda medido y marcado."""
    sent, _ = real_vendure
    _set("pm_include_disabled", 1)
    _set("pm_mode", 1)
    FakeVendure.products = [_product("3", "Producto raro"), _product("4", "Producto raro", enabled=False)]
    webw.web.pages["producto-raro"] = _web_page(_card("MLA901", "Producto Raro", 250.0))
    _score(webw, "MLA901", 0.9)
    result = await price_monitor.run_price_monitor()
    assert result["status"] == "ok"
    assert sent, "el job tenía que leer el catálogo"
    for body in sent:
        q = body["query"]
        assert q.lstrip().startswith("query") and "mutation" not in q.lower(), q
        for verb in ("updateProduct", "updateProductVariants", "login", "createAsset", "deleteProduct"):
            assert verb not in q
    snaps = _snaps()
    assert set(snaps) == {"3", "4"}
    assert snaps["3"].product_enabled is True and snaps["4"].product_enabled is False
    assert snaps["4"].ml_status == "ok" and snaps["4"].match_origin == "web"
    assert json.loads(snaps["4"].our_specs) == {
        "length": 40.0, "width": 30.0, "height": 5.0, "weight": 0.5,
        "box_length": 60.0, "box_width": 40.0, "box_height": 40.0, "box_weight": 12.0}
    assert FakeVendure.forbidden == [] and webw.graphql_calls == []


@pytest.mark.parametrize("mode", ["errors_200", "errors_400"])
async def test_a_vendure_without_the_measure_fields_still_gives_prices(webw, real_vendure, mode):
    """Si el schema no tiene los custom fields de medidas, la query se repite sin
    ellos (con 200+errors o con el 400 de validación de Apollo) y el semáforo corre."""
    sent, state = real_vendure
    state["mode"] = mode
    FakeVendure.products = [_product("3", "Producto raro")]
    webw.web.pages["producto-raro"] = _web_page(_card("MLA901", "Producto Raro", 250.0))
    _score(webw, "MLA901", 0.9)
    result = await price_monitor.run_price_monitor()
    assert result["status"] == "ok", result
    assert any(DIMS_IN_QUERY.search(b["query"]) for b in sent)
    assert not DIMS_IN_QUERY.search(sent[-1]["query"])
    s = _snaps()["3"]
    assert s.ml_status == "ok" and s.our_specs is None
    assert vendure_client_mod.VendureClient._dims_supported is False


# ─── deshabilitados: el score al vuelo vale lo mismo que dentro del índice ──


async def test_a_disabled_product_scores_exactly_like_the_same_photo_inside_the_index(monkeypatch):
    """Los umbrales (0,65 / 0,80 / 0,40) están calibrados para el espacio CENTRADO
    del índice. Un deshabilitado no está en el índice: su foto se proyecta a ese
    mismo espacio. Con la MISMA foto tiene que dar el mismo score que un habilitado
    que sí está indexado, sin importar cuál sea la publicación de ML."""
    import numpy as np

    from app.dedup import catalog_index, image_embed
    from app.pricing import market_match

    rng = np.random.default_rng(11)
    dim = image_embed.EMBED_DIM
    raw = rng.normal(size=(24, dim)).astype(np.float32)
    raw /= np.linalg.norm(raw, axis=1, keepdims=True)
    matrix, mean = catalog_index._center(raw)
    assert mean.any(), "el índice de prueba tiene que estar centrado"
    pids = [str(i) for i in range(24)]
    ml_photo = "https://http2.mlstatic.com/D_NQ_NP_777-F.jpg"
    own_photo = "https://cdn.b2box/99.jpg"
    enabled, disabled = _product("3", "Taza"), _product("99", "Taza", enabled=False)
    disabled.featured_image_url, disabled.image_urls = own_photo, [own_photo]
    state = catalog_index._state
    for attr, value in (("matrix", matrix), ("mean", mean), ("product_ids", pids),
                        ("image_urls", [f"https://cdn.b2box/{p}.jpg" for p in pids]),
                        ("products", {p: _product(p, "x") for p in pids}), ("vector_ids", frozenset(pids))):
        monkeypatch.setattr(state, attr, value)
    monkeypatch.setattr(catalog_index, "is_ready", lambda: True)
    monkeypatch.setattr(image_embed, "available", lambda: True)

    for trial in range(5):
        q = rng.normal(size=dim).astype(np.float32)
        q /= np.linalg.norm(q)
        vectors = {ml_photo: q, own_photo: raw[3]}

        async def embed(urls, *, concurrency=4, interactive=False, _v=vectors):  # noqa: ARG001
            return [_v.get(u) for u in urls]

        monkeypatch.setattr(image_embed, "embed_urls_aligned", embed)
        inside = await market_match.clip_index_scorer(enabled, [ml_photo])
        on_the_fly = await market_match.clip_index_scorer(disabled, [ml_photo])
        assert inside is not None and on_the_fly is not None
        assert on_the_fly == pytest.approx(inside, abs=1e-5), trial


async def test_only_pesos_count_a_dollar_listing_never_enters_the_median(webw):
    """Criterio 7: precio en ARS solamente. Una publicación en USD que es IGUAL en
    foto y nombre no mueve ni la mediana ni el mínimo (un 10 «dólares» sería 10 pesos)."""
    webw.web.pages["producto-raro"] = _web_page(
        _card("MLA901", "Producto Raro", 250.0),
        _card("MLA902", "Producto Raro Premium", 10.0, currency="USD", seller="Dos"),
        _card("MLA903", "Producto Raro Plus", 300.0, currency="usd", seller="Tres"))
    for ref in ("MLA901", "MLA902", "MLA903"):
        _score(webw, ref, 0.9)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert (s.ml_status, s.ml_median_cents, s.ml_min_cents, s.ml_listing_count) == ("ok", 25_000, 25_000, 1)
    assert [m["ml_id"] for m in json.loads(s.matched_listings)] == ["MLA901"]
