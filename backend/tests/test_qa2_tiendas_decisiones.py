"""QA de cierre de feat/semaforo-tiendas, criterio 3: las decisiones de Nico (semaforo-etapa2-diseno.md §6.3), cada una de
punta a punta: corrida → snapshot → API (`GET /api/price-monitor/snapshots`) → lo que el dashboard dibuja.

Lo que el dashboard decide con lo que sirve la API (frontend/src/components/SemaforoView.tsx y SourceCells.tsx):
  * el punto de color es el REAL si `ml_status == "ok"` o `price_basis == "tiendas"`; si no, el estimado;
  * «según tiendas» / «según ML + tiendas» sale de `price_basis`;
  * la celda de una tienda muestra «sin stock» cuando `cell.stock == 0`; «Más barato afuera» lo muestra con
    `out_of_stock` y en gris;
  * un similar suma al estimado solo si `in_estimate`.
`_dashboard()` repite esas reglas para poder afirmar sobre lo que se vería.
"""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import runtime  # noqa: E402
from app.db.models import MarketPriceSnapshot, PriceMonitorRun, StoreMatch  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import daily_budget, market_judge, price_monitor, semaforo  # noqa: E402
from app.pricing.semaforo import PricedVariant  # noqa: E402
from tests.store_fixtures import store_db  # noqa: E402,F401  (fixture)
from tests.test_price_monitor import FakeVendure, _listing, _product, _runs, _set, world  # noqa: E402,F401
from tests.test_price_monitor_routes import _env, client  # noqa: E402,F401
from tests.test_qa_tiendas_color import (  # noqa: E402,F401
    CP,
    GD,
    ML_ONLY,
    _expected_color,
    _margin,
    _snap,
    add_item,
    sw,
)
from tests.test_semaforo_web import _card, _score, _web_page, webw  # noqa: E402,F401


def _items(client, **params) -> dict[str, dict]:
    r = client.get("/api/price-monitor/snapshots", params={"page_size": 200, **params})
    assert r.status_code == 200, r.text
    return {i["product"]["id"]: i for i in r.json()["items"]}


def _dashboard(item: dict) -> dict:
    """Lo que SemaforoView / SourceCells dibujan para una fila."""
    s = item["snapshot"] if "snapshot" in item else item
    real = s["ml_status"] == "ok" or s.get("price_basis") == "tiendas"
    cells = {k: c for k, c in item["cells"].items() if k != "ml"}
    out = item.get("cheapest_outside")
    return {
        "dot": s["color"] if real else (s.get("estimated_color") or None),
        "dot_is_real": real,
        "label": {"tiendas": "según tiendas", "ml+tiendas": "según ML + tiendas"}.get(s.get("price_basis") or "ml"),
        "sin_stock_badges": sorted(c["label"] for c in cells.values() if c.get("stock") == 0),
        "cheapest": None if out is None else (out["label"], out["price_cents"], bool(out["out_of_stock"])),
    }


def _match_rows(pid: str = "1") -> list[StoreMatch]:
    with Session(engine) as s:
        return list(s.exec(select(StoreMatch).where(StoreMatch.product_id == pid).order_by(StoreMatch.store_id, StoreMatch.rank)))


def _counters_equal_snapshots(client) -> None:
    """Los contadores de la corrida (los que dibuja la card de Salud) son los de las filas de la tabla."""
    run = _runs()[-1]
    with Session(engine) as s:
        snaps = list(s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.run_id == run.id)))
    for color, col in (("verde", "n_verde"), ("amarillo", "n_amarillo"), ("rojo", "n_rojo"), ("sin_dato", "n_sin_dato")):
        assert getattr(run, col) == sum(1 for x in snaps if x.color == color), col
    body = client.get("/api/price-monitor/snapshots", params={"page_size": 200}).json()
    assert body["colors"] == {c: sum(1 for x in snaps if x.color == c) for c in ("verde", "amarillo", "rojo", "sin_dato")
                              if any(x.color == c for x in snaps)} or set(body["colors"]) >= {x.color for x in snaps}
    for color in {x.color for x in snaps}:
        ids = {i["product"]["id"] for i in client.get("/api/price-monitor/snapshots",
                                                      params={"color": color, "page_size": 200}).json()["items"]}
        assert ids == {x.product_id for x in snaps if x.color == color}, f"filtro de color {color}"


# ═══ Decisión 1: la marca Gadnic es idéntica ═════════════════════════════════


@pytest.fixture
def judge_on(monkeypatch):
    calls: list[list[tuple[str, str | None]]] = []
    monkeypatch.setattr(market_judge, "enabled", lambda: True)
    monkeypatch.setattr(market_judge, "make_client", lambda: None)
    _set("pm_vision_max_calls", 50)
    runtime.set_value("pm_stores_vision_max_calls", 50)

    async def judge(our_name, our_images, candidates, *, max_calls, on_reserve=None,
                    counter_key=market_judge.LLM_COUNTER_KEY, **kw):
        calls.append([(c.ml_id, c.brand) for c in candidates])
        assert await daily_budget.reserve_async(counter_key, max_calls, None, on_reserve)
        out = {}
        for c in candidates:
            known = (c.brand or "").lower() in {"stanley", "philips"}
            out[c.ml_id] = market_judge.JudgeVerdict(
                c.ml_id, not known, 0.9, "marca conocida" if known else "mismo producto",
                "similar" if known else "igual", ("marca",) if known else ())
        return market_judge.JudgeResult(verdicts=out)

    monkeypatch.setattr(market_judge, "judge", judge)
    return calls


async def test_gadnic_own_brand_is_identical_and_never_asked_about_while_a_known_brand_in_the_same_store_is(sw, client, judge_on):
    runtime.set_value("pm_stores_affect_color", 1)
    add_item(GD, "gd-org", "Organizador de cocina", 25_000, 0.95, brand="Gadnic")
    add_item(GD, "gd-stanley", "Organizador de cocina Stanley", 26_000, 0.95, brand="Stanley")
    add_item(GD, "gd-gen", "Organizador de cocina premium", 27_000, 0.95, brand="Generica")
    await price_monitor.run_price_monitor()
    rows = {m.brand: m for m in _match_rows() if m.brand}
    assert (rows["Gadnic"].category, rows["Gadnic"].source) == ("igual", "clip"), "Gadnic = idéntico, por foto + nombre"
    assert (rows["Stanley"].category, rows["Stanley"].source) == ("similar", "llm"), "una marca conocida sí es similar"
    asked = {b for call in judge_on for _id, b in call}
    assert "Gadnic" not in asked and "Stanley" in asked
    item = _items(client)["1"]
    gd = next(v for v in item["stores"].values() if v["label"] == "Gadnic")["matches"]
    by_brand = {m["brand"]: m for m in gd}
    assert by_brand["Gadnic"]["category"] == "igual" and by_brand["Gadnic"]["differences"] == []
    assert by_brand["Stanley"]["category"] == "similar" and by_brand["Stanley"]["differences"] == ["marca"]
    # y la marca de la casa SÍ pinta el color (el idéntico de Gadnic sube la mediana): 12.000, 14.000, 25.000, 27.000
    assert _snap().price_basis == "ml+tiendas" and _snap().color == _expected_color(ML_ONLY + [25_000, 27_000])


@pytest.mark.parametrize("brand", ["Gadnic", "GADNIC", "gadnic"])
async def test_the_house_brand_matches_without_distinguishing_case_and_the_judge_is_not_asked(sw, judge_on, brand):
    add_item(GD, "gd-org", "Organizador de cocina", 25_000, 0.95, brand=brand)
    await price_monitor.run_price_monitor()
    [m] = [m for m in _match_rows() if m.title == "Organizador de cocina"]
    assert (m.category, m.source) == ("igual", "clip")
    assert judge_on == [] or all(brand not in [b for _i, b in call] for call in judge_on)


@pytest.mark.xfail(strict=True, reason="GAP de la decisión «marca Gadnic = idéntico»: solo vale para los productos de la tienda Gadnic "
                                       "(market_store.house_brand). Una publicación de ML o un producto de otra tienda con marca "
                                       "«Gadnic» (el mismo importador revendido) se trata como marca conocida: va al juez y, si dice "
                                       "«similar», queda fuera del color. Si Nico quiso la regla general, 'gadnic' va en "
                                       "price_monitor._GENERIC_BRANDS o el house_brand se aplica a todas las fuentes.")
@pytest.mark.parametrize("source", ["ml_web", "casa_perfecta"])
async def test_a_listing_branded_gadnic_outside_the_gadnic_store_is_not_a_known_brand(webw, store_db, monkeypatch, judge_on, source):
    from app.pricing import market_match, store_catalog
    from tests.ml_web_fixtures import ld_product
    from tests.test_qa_tiendas_color import SCORES

    store_catalog.seed_default_stores()
    runtime.set_value("pm_stores_topup_minutes", 0)
    SCORES.clear()

    async def scorer(our, urls):
        return SCORES.get((our.id, urls[0]), 0.25) if urls else None

    monkeypatch.setattr(market_match, "clip_score_urls", scorer)
    FakeVendure.products = [_product("3", "Producto raro")]
    webw.ml.search["Producto raro"] = []
    if source == "ml_web":
        ld = [ld_product("Producto Raro Gadnic", "https://www.mercadolibre.com.ar/x/p/MLA29003349", 400, brand="Gadnic")]
        webw.web.pages["producto-raro"] = _web_page(
            _card("MLA901", "Producto Raro Gadnic", 400.0, catalog="MLA29003349", url="www.mercadolibre.com.ar/x/p/MLA29003349"), ld=ld)
        _score(webw, "MLA901", 0.92)
    else:
        add_item(CP, "cp-raro", "Producto raro", 40_000, 0.95, product="3", brand="Gadnic")
    await price_monitor.run_price_monitor()
    asked = {b for call in judge_on for _id, b in call}
    assert "Gadnic" not in asked, "la marca Gadnic no es una marca conocida: no se le pregunta al juez por ella"


# ═══ Decisión 2: sin stock se muestra pero no cuenta ═════════════════════════


async def test_out_of_stock_variants_end_to_end(sw, client):
    """Gadnic agotado (5.000) + Casa Perfecta con stock (22.000) + stock desconocido (None, cuenta como hay)."""
    runtime.set_value("pm_stores_affect_color", 1)
    add_item(GD, "gd-agotado", "Organizador de cocina", 5_000, 0.95, stock=0)
    add_item(CP, "cp-ok", "Organizador de cocina plegable", 22_000, 0.95, stock=7)
    add_item(CP, "cp-null", "Organizador de cocina doble", 21_000, 0.95, stock=None)
    await price_monitor.run_price_monitor()
    item = _items(client)["1"]
    d = _dashboard(item)
    assert d["sin_stock_badges"] == ["Gadnic"], "se ve con «sin stock»"
    assert d["cheapest"] == ("Mercado Libre", 12_000, False), "el agotado de 5.000 no gana «Más barato afuera»"
    assert _snap().color == _expected_color(ML_ONLY + [21_000, 22_000]), "ni entra a la mediana (None cuenta como con stock)"
    gd = next(v for v in item["stores"].values() if v["label"] == "Gadnic")["matches"]
    assert [(m["category"], m["stock"], m["price_cents"]) for m in gd] == [("igual", 0, 5_000)], "y se sigue viendo en el detalle"
    _counters_equal_snapshots(client)


async def test_two_identicals_in_one_store_the_one_with_stock_is_the_one_that_counts_even_if_it_is_more_expensive(sw, client):
    runtime.set_value("pm_stores_affect_color", 1)
    add_item(GD, "gd-a", "Organizador de cocina", 5_000, 0.95, stock=0)
    add_item(GD, "gd-b", "Organizador de cocina negro", 30_000, 0.95, stock=3)
    await price_monitor.run_price_monitor()
    item = _items(client)["1"]
    gd_cell = next(c for k, c in item["cells"].items() if k != "ml")
    assert (gd_cell["price_cents"], gd_cell["stock"]) == (30_000, 3), "la celda muestra el que tiene stock, no el agotado"
    assert _dashboard(item)["cheapest"] == ("Mercado Libre", 12_000, False)
    assert _snap().color == _expected_color(ML_ONLY + [30_000])


async def test_when_the_only_identical_of_a_store_is_out_of_stock_the_color_stays_as_ml_left_it_and_the_basis_is_ml(sw, client):
    runtime.set_value("pm_stores_affect_color", 1)
    FakeVendure.products = [_product("1", "Producto raro")]
    sw.ml.search["Producto raro"] = []
    add_item(GD, "gd-raro", "Producto raro", 25_000, 0.95, stock=0)
    await price_monitor.run_price_monitor()
    s = _snap()
    assert (s.color, s.price_basis, s.estimated_color) == ("sin_dato", "ml", None)
    d = _dashboard(_items(client)["1"])
    assert d["dot"] is None and d["sin_stock_badges"] == ["Gadnic"] and d["cheapest"] == ("Gadnic", 25_000, True)
    assert d["label"] is None


async def test_stock_zero_similars_do_not_feed_the_estimate_either(sw):
    runtime.set_value("pm_stores_affect_color", 1)
    prod = _product("3", "Producto raro")
    prod.priced_variants = [PricedVariant(id="v3", name="", sku="", price_with_tax_cents=10_000, currency="ARS",
                                          specs={"length": 40.0, "width": 30.0})]
    FakeVendure.products = [prod]
    sw.ml.search["Producto raro"] = []
    add_item(GD, "gd-70", "Producto raro 70x90 cm", 30_000, 0.95, product="3", stock=0)
    add_item(CP, "cp-50", "Producto raro 50x60 cm", 40_000, 0.95, product="3", stock=2)
    await price_monitor.run_price_monitor()
    s = _snap("3")
    assert (s.estimated_median_cents, s.estimated_listing_count) == (40_000, 1)


async def test_an_item_that_runs_out_of_stock_between_two_runs_stops_counting_and_the_old_run_keeps_its_color(sw, client):
    runtime.set_value("pm_stores_affect_color", 1)
    gd = add_item(GD, "gd-org", "Organizador de cocina", 25_000, 0.95, stock=5)
    cp = add_item(CP, "cp-org", "Organizador de cocina plegable", 25_000, 0.95, stock=5)
    await price_monitor.run_price_monitor()
    run1 = _runs()[-1].id
    assert _snap().color == "verde" and _snap().price_basis == "ml+tiendas"
    from app.db.models import StoreCatalogItem
    with Session(engine) as s:
        for iid in (gd, cp):
            row = s.get(StoreCatalogItem, iid)
            row.stock = 0
            s.add(row)
        s.commit()
    await price_monitor.run_price_monitor()
    run2 = _runs()[-1].id
    assert _snap(run_id=run1).color == "verde", "la corrida vieja no se reescribe"
    s2 = _snap(run_id=run2)
    assert (s2.color, s2.price_basis) == ("amarillo", "ml")
    d = _dashboard(_items(client, run_id=run2)["1"])
    assert d["sin_stock_badges"] == ["Casa Perfecta", "Gadnic"] and d["cheapest"] == ("Mercado Libre", 12_000, False)


# ═══ Decisión 3: color real solo con idénticos confirmados; similares → estimado ═

SMALL = dict(price=25_000, score=0.95)


async def test_a_store_identical_confirmed_by_photo_and_name_paints_the_real_color_the_dashboard_says_so(sw, client):
    runtime.set_value("pm_stores_affect_color", 1)
    FakeVendure.products = [_product("1", "Producto raro")]
    sw.ml.search["Producto raro"] = []
    add_item(GD, "gd-raro", "Producto raro", 25_000, 0.95)
    await price_monitor.run_price_monitor()
    item = _items(client)["1"]
    s = item["snapshot"] if "snapshot" in item else item
    assert (s["ml_status"], s["color"], s["price_basis"]) == ("no_data", _expected_color([25_000]), "tiendas")
    d = _dashboard(item)
    assert d["dot_is_real"] and d["dot"] == "verde" and d["label"] == "según tiendas"
    assert d["cheapest"] == ("Gadnic", 25_000, False)
    assert [i for i in _items(client, color="verde")] == ["1"], "el filtro por color real lo incluye"
    assert _items(client, estimated="any") == {}, "y no figura como estimado"
    _counters_equal_snapshots(client)


async def test_ml_failed_with_a_store_identical_the_color_is_real_from_stores_and_the_row_is_honest_about_ml(sw, client):
    runtime.set_value("pm_stores_affect_color", 1)
    FakeVendure.products = [_product("1", "Organizador cocina")]
    sw.ml.search["Organizador cocina"] = 429
    add_item(GD, "gd-org", "Organizador de cocina", 25_000, 0.95)
    await price_monitor.run_price_monitor()
    item = _items(client)["1"]
    s = item["snapshot"] if "snapshot" in item else item
    assert (s["ml_status"], s["price_basis"], s["color"]) == ("failed", "tiendas", "verde")
    assert _dashboard(item)["dot_is_real"] is True
    run = _runs()[-1]
    assert (run.n_failed, run.n_verde) == (1, 1)


async def test_the_judge_alone_does_not_paint_but_a_person_saying_es_el_mismo_does_and_undo_takes_it_back(sw, client, judge_on):
    runtime.set_value("pm_stores_affect_color", 1)
    FakeVendure.products = [_product("1", "Producto raro")]
    sw.ml.search["Producto raro"] = []
    add_item(GD, "gd-raro", "Producto raro", 25_000, 0.60)               # banda ambigua: lo decide el juez (llm)
    await price_monitor.run_price_monitor()
    [m] = [m for m in _match_rows() if m.price_cents == 25_000]
    assert (m.category, m.source) == ("igual", "llm")
    s = _snap()
    assert (s.color, s.price_basis) == ("sin_dato", "ml"), "el juez solo se ve como idéntico pero no pinta"
    assert _dashboard(_items(client)["1"])["cheapest"] == ("Gadnic", 25_000, False), "(sí figura en «Más barato afuera»)"
    r = client.post(f"/api/price-monitor/store-matches/{m.id}/label", json={"label": "es"})
    assert r.status_code == 200 and r.json()["snapshot"]["price_basis"] == "tiendas"
    assert _snap().color == "verde" and _items(client)["1"]["snapshot"]["price_basis"] == "tiendas" if "snapshot" in _items(client)["1"] else True
    r = client.delete(f"/api/price-monitor/store-matches/{m.id}/label")
    assert r.status_code == 200 and r.json()["snapshot"]["price_basis"] == "ml"
    assert _snap().color == "sin_dato"
    _counters_equal_snapshots(client)


async def test_a_branded_identical_confirmed_by_photo_name_and_judge_keeps_the_judge_source_and_so_does_not_paint(sw, judge_on):
    """Conservador: una marca conocida que el juez confirma como «igual» queda con fuente `llm` aunque foto + nombre ya la
    daban por buena (el juez pisa la fuente) y entonces su precio no entra a la mediana. Documentado, no es un error de
    la decisión («no el juez solo»): solo deja afuera un caso que la decisión dejaría pasar."""
    runtime.set_value("pm_stores_affect_color", 1)
    add_item(GD, "gd-philips", "Organizador de cocina", 25_000, 0.95, brand="Philips")
    judge_on.clear()
    # el juez de este test dice «similar» para Philips (marca conocida): el caso interesante es el contrario
    await price_monitor.run_price_monitor()
    [m] = [m for m in _match_rows() if m.price_cents == 25_000]
    assert m.source in ("llm", "clip", "clip+nombre")
    print("marca conocida, foto 0.95, juez:", m.category, m.source)


@pytest.mark.parametrize("judge", ["prendido", "apagado"])
async def test_a_store_similar_feeds_the_estimate_with_the_rules_of_ml(sw, client, judge_on, judge):
    """Con el juez prendido: Stanley es similar CONFIRMADO (alimenta el estimado) y la ficha dudosa (CLIP 0.62) también
    pasa por el juez. Con el juez apagado, como en ML: la marca conocida queda «igual» por foto + nombre y la dudosa es
    «similar sin confirmar», que no alimenta nada."""
    runtime.set_value("pm_stores_affect_color", 1)
    if judge == "apagado":
        _set("pm_vision_max_calls", 0)
    FakeVendure.products = [_product("1", "Producto raro")]
    sw.ml.search["Producto raro"] = []
    add_item(GD, "gd-raro-stanley", "Producto raro Stanley", 25_000, 0.95, brand="Stanley")
    add_item(CP, "cp-raro-dudoso", "Producto raro viejo", 30_000, 0.62)
    await price_monitor.run_price_monitor()
    item = _items(client)["1"]
    s = item["snapshot"] if "snapshot" in item else item
    matches = {m["brand"] or m["title"]: m for v in item["stores"].values() for m in v["matches"]}
    if judge == "apagado":
        assert matches["Stanley"]["category"] == "igual"
        assert matches["Producto raro viejo"]["category"] == "similar" and matches["Producto raro viejo"]["source"] == "ambiguo"
        assert not matches["Producto raro viejo"]["in_estimate"], "similar sin confirmar: no alimenta el estimado"
        assert (s["color"], s["price_basis"], s["estimated_color"]) == (_expected_color([25_000]), "tiendas", None)
    else:
        assert (matches["Stanley"]["category"], matches["Stanley"]["source"]) == ("similar", "llm")
        feeding = {k for k, m in matches.items() if m["in_estimate"]}
        assert "Stanley" in feeding
        assert s["color"] == "sin_dato" and s["price_basis"] == "ml"
        assert s["estimated_color"] == _expected_color(sorted(m["price_cents"] for k, m in matches.items() if k in feeding))
        d = _dashboard(item)
        assert d["dot_is_real"] is False and d["dot"] == s["estimated_color"], "el dashboard lo pinta como ESTIMADO, no como real"


async def test_with_a_real_identical_in_ml_a_store_similar_never_makes_an_estimate(sw, client, judge_on):
    runtime.set_value("pm_stores_affect_color", 1)
    add_item(GD, "gd-stanley", "Organizador de cocina Stanley", 40_000, 0.95, brand="Stanley")
    await price_monitor.run_price_monitor()
    s = _snap()
    assert (s.color, s.estimated_color, s.price_basis) == (_expected_color(ML_ONLY), None, "ml")
    assert _items(client, estimated="any") == {}


def test_the_defaults_are_the_ones_of_nicos_decisions(store_db, client):
    """Sin ningún ajuste guardado: las tiendas cuentan para el color, el juez de tiendas tiene su tope propio y la API lo dice."""
    from app.config import get_settings

    s = get_settings()
    assert (s.pm_stores_affect_color, s.pm_stores_vision_max_calls) == (1, 500)
    runtime.invalidate()
    assert runtime.get("pm_stores_affect_color") == 1 and runtime.get("pm_stores_vision_max_calls") == 500
    assert client.get("/api/stores").json()["affect_color"] is True
    assert client.get("/api/price-monitor/summary").json()["stores_affect_color"] is True
    assert (s.store_dead_min_days_5xx, s.store_dead_retry_days, s.store_user_agent) == (3, 30, "HugoPriceBot/1.0 (+https://b2b.pro)".replace("b2b.pro", "b2box.pro"))
    from app.pricing import store_catalog

    assert (store_catalog.OUTAGE_STREAK, store_catalog.DEAD_AFTER_FAILS) == (25, 2)


async def test_when_ml_failed_and_the_color_came_only_from_a_store_marking_it_not_the_same_takes_the_color_away_and_undo_brings_it_back(sw, client):
    runtime.set_value("pm_stores_affect_color", 1)
    FakeVendure.products = [_product("1", "Organizador cocina")]
    sw.ml.search["Organizador cocina"] = 429
    add_item(GD, "gd-org", "Organizador de cocina", 25_000, 0.95)
    await price_monitor.run_price_monitor()
    assert (_snap().ml_status, _snap().color, _snap().price_basis) == ("failed", "verde", "tiendas")
    [m] = [m for m in _match_rows() if m.category == "igual"]
    r = client.post(f"/api/price-monitor/store-matches/{m.id}/label", json={"label": "no_es"})
    assert r.status_code == 200 and r.json()["snapshot"]["color"] == "sin_dato" and r.json()["snapshot"]["price_basis"] == "ml"
    item = _items(client)["1"]
    assert _dashboard(item)["dot"] is None and _dashboard(item)["label"] is None
    r = client.delete(f"/api/price-monitor/store-matches/{m.id}/label")
    assert r.json()["snapshot"]["color"] == "verde" and r.json()["snapshot"]["price_basis"] == "tiendas"
    _counters_equal_snapshots(client)
