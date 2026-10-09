"""QA independiente de feat/semaforo-tiendas, criterios 3 y 4: el color y las correcciones.

  * con `pm_stores_affect_color=0` las tiendas no mueven el color real NI el estimado, aunque tengan idénticos
    baratísimos (el mundo completo contra el dorado de b052e4f está en test_qa_tiendas_regresion.py);
  * con `pm_stores_affect_color=1` la mediana junta los idénticos de las TRES fuentes (ML, Gadnic, Casa Perfecta), y
    los similares confirmados de las tiendas suman al estimado; un precio dudoso nunca cuenta;
  * el botón «Es el mismo / No es el mismo» de una tienda deja el snapshot como lo dejaría la próxima corrida;
  * una corrección de un producto no mueve a otro;
  * el juez comparte el tope diario con ML: las tiendas no pueden dejar a ML sin juez.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlalchemy import update  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import runtime  # noqa: E402
from app.clock import utcnow  # noqa: E402
from app.db.models import (  # noqa: E402
    MarketPriceSnapshot,
    MarketStore,
    PriceMonitorRun,
    StoreCatalogItem,
    StoreMatch,
    StoreMatchFeedback,
)
from app.db.session import engine  # noqa: E402
from app.pricing import daily_budget, market_judge, market_match, price_monitor, semaforo, store_catalog  # noqa: E402
from tests.store_fixtures import store_db  # noqa: E402,F401  (fixture)
from tests.test_price_monitor import (  # noqa: E402,F401
    FakeVendure,
    _candidate,
    _listing,
    _product,
    _runs,
    _set,
    _snaps,
    world,
)
from tests.test_price_monitor_routes import _env, client  # noqa: E402,F401
from tests.test_semaforo_web import _card, _score, _web_page, webw  # noqa: E402,F401

ML_IMG = "https://http2.mlstatic.com/D_NQ_{}.jpg"
CP_IMG = "https://acdn-us.mitiendanube.com/stores/001/133/924/products/{}.webp"
GD_IMG = "https://static.bidcom.com.ar/publicacionesML/productos/{}.jpg"
CP, GD = "Casa Perfecta", "Gadnic"

SCORES: dict[tuple[str, str], float | None] = {}     # (product_id, foto) -> lo que dice CLIP
DEFAULT_SCORE = 0.25


def _store_id(name: str) -> int:
    with Session(engine) as s:
        return int(s.exec(select(MarketStore.id).where(MarketStore.name == name)).one())


def add_item(store: str, key: str, title: str, price: int | None, score: float | None, *, product: str = "1",
             brand: str | None = None, doubtful: bool = False, stock: int | None = 5, note: str | None = None) -> int:
    """Un producto indexado de la tienda. `score` es lo que CLIP dice de su foto contra `product`."""
    sid = _store_id(store)
    base = "https://www.casaperfecta.com.ar/productos" if store == CP else "https://www.gadnic.com.ar/catalogo"
    img = (CP_IMG if store == CP else GD_IMG).format(key)
    with Session(engine) as s:
        row = StoreCatalogItem(store_id=sid, url=f"{base}/{key}/", title=title, sku=key.upper(), price_cents=price,
                               price_doubtful=doubtful, price_note=note, image_url=img, brand=brand, stock=stock,
                               last_seen_at=utcnow(), last_checked_at=utcnow())
        s.add(row)
        s.commit()
        s.refresh(row)
    SCORES[(product, img)] = score
    return int(row.id)


@pytest.fixture
def sw(webw, store_db, monkeypatch):  # noqa: F811
    """El `world` del semáforo con la web de ML (P1 'Organizador cocina' a 100 pesos) + Gadnic y Casa Perfecta vacías."""
    world = webw
    SCORES.clear()
    store_catalog.seed_default_stores()
    FakeVendure.products = [_product("1", "Organizador cocina")]
    # ML: dos idénticos a 120 y 140 pesos (mediana 130 → margen 13 %: AMARILLO); nuestro precio: 100 pesos.
    world.ml.items["MLA1"] = [_listing("I1", "101", 120.0), _listing("I2", "101", 140.0)]
    world.ml.users = {"101": 1_000}

    async def scorer(our, urls):
        if not urls:
            return None
        return SCORES.get((our.id, urls[0]), DEFAULT_SCORE)

    monkeypatch.setattr(market_match, "clip_score_urls", scorer)
    runtime.set_value("pm_stores_topup_minutes", 0)
    runtime.set_value("pm_stores_affect_color", 0)
    yield world
    runtime.invalidate()


def _snap(pid: str = "1", run_id: int | None = None) -> MarketPriceSnapshot:
    with Session(engine) as s:
        stmt = select(MarketPriceSnapshot).where(MarketPriceSnapshot.product_id == pid)
        if run_id:
            stmt = stmt.where(MarketPriceSnapshot.run_id == run_id)
        return s.exec(stmt.order_by(MarketPriceSnapshot.id.desc())).first()  # type: ignore[union-attr]


def _margin(prices: list[int], our: int = 10_000) -> float:
    return semaforo.estimated_margin_pct(semaforo.median_cents(prices), our, 13.0, 0)


def _expected_color(prices: list[int], our: int = 10_000) -> str:
    return semaforo.color(_margin(prices, our), our, semaforo.median_cents(prices), 30.0, 10.0)


ML_ONLY = [12_000, 14_000]


# ─── criterio 4: con 0 las tiendas no mueven NADA ───────────────────────────


async def test_stores_off_the_color_is_ml_only_even_with_absurdly_cheap_or_expensive_identicals(sw):
    add_item(GD, "gd-org", "Organizador de cocina", 500, 0.95)          # idéntico a 5 pesos: pondría en rojo
    add_item(CP, "cp-org", "Organizador de cocina", 9_000_000, 0.95)    # idéntico a 90.000 pesos
    await price_monitor.run_price_monitor()
    s = _snap()
    assert (s.color, s.ml_median_cents, s.ml_min_cents, s.price_basis) == (_expected_color(ML_ONLY), 13_000, 12_000, "ml")
    assert s.color == "amarillo"
    assert (s.est_margin_pct, s.estimated_color) == (round(_margin(ML_ONLY), 2), None)
    with Session(engine) as ses:
        cats = {m.category for m in ses.exec(select(StoreMatch)).all()}
    assert "igual" in cats, "las tiendas tenían que ver el idéntico: sin él el test no prueba nada"


async def test_stores_off_the_estimated_color_ignores_confirmed_store_similars(sw):
    """ML solo trae un tamaño distinto (SIMILAR confirmado por medidas → estimado). Las tiendas traen otros
    similares confirmados mucho más caros: con 0 el estimado sale solo de ML."""
    from app.pricing.semaforo import PricedVariant

    prod = _product("3", "Producto raro")
    prod.priced_variants = [PricedVariant(id="v3", name="", sku="", price_with_tax_cents=10_000, currency="ARS",
                                          specs={"length": 40.0, "width": 30.0})]
    FakeVendure.products = [prod]
    sw.web.pages["producto-raro"] = _web_page(_card("MLA921", "Producto Raro 60x80 cm", 250.0))
    _score(sw, "MLA921", 0.95)
    add_item(GD, "gd-raro", "Producto raro 70x90 cm", 900_000, 0.95, product="3")
    add_item(CP, "cp-raro", "Producto raro 50x60 cm", 800_000, 0.95, product="3")
    await price_monitor.run_price_monitor()
    s = _snap("3")
    # El estimado sale de ML: mediana 250 pesos, un solo similar.
    assert (s.ml_status, s.color, s.estimated_color, s.estimated_median_cents, s.estimated_listing_count, s.price_basis) == (
        "no_data", "sin_dato", "verde", 25_000, 1, "ml")
    with Session(engine) as ses:
        sims = [m for m in ses.exec(select(StoreMatch).where(StoreMatch.product_id == "3")).all() if m.category == "similar"]
    assert sims, "los similares de las tiendas tenían que existir"


# ─── criterio 4: con 1 la mediana junta las tres fuentes ────────────────────


@pytest.mark.parametrize("gadnic,casa", [(None, None), (20_000, None), (None, 22_000), (20_000, 22_000)])
async def test_stores_on_the_median_joins_the_identicals_of_the_three_sources(sw, gadnic, casa):
    runtime.set_value("pm_stores_affect_color", 1)
    if gadnic:
        add_item(GD, "gd-org", "Organizador de cocina", gadnic, 0.95)
    if casa:
        add_item(CP, "cp-org", "Organizador de cocina", casa, 0.95)
    await price_monitor.run_price_monitor()
    prices = ML_ONLY + [p for p in (gadnic, casa) if p]
    s = _snap()
    assert s.color == _expected_color(prices)
    assert s.est_margin_pct == pytest.approx(round(_margin(prices), 2))
    assert s.price_basis == ("ml+tiendas" if len(prices) > 2 else "ml")
    # la mediana, el mínimo y las publicaciones de ML siguen siendo de ML
    assert (s.ml_median_cents, s.ml_min_cents, s.ml_listing_count) == (13_000, 12_000, 2)
    # el ejemplo cuenta algo: sumar las dos tiendas cambia de amarillo a verde, y una sola no alcanza
    assert _expected_color(ML_ONLY) == "amarillo" and _expected_color(ML_ONLY + [20_000]) == "amarillo"
    assert _expected_color(ML_ONLY + [20_000, 22_000]) == "verde"


async def test_stores_on_a_doubtful_price_never_counts_neither_flagged_by_the_indexer_nor_ten_times_ours(sw):
    runtime.set_value("pm_stores_affect_color", 1)
    add_item(GD, "gd-viejo", "Organizador de cocina", 249_00, 0.95, doubtful=True, note="precio muy bajo")
    add_item(GD, "gd-x10", "Organizador de cocina plegable", 150_000, 0.95)       # 10 veces el nuestro: se marca al comparar
    add_item(CP, "cp-x10", "Organizador de cocina premium", 800, 0.95)            # más de 10 veces menos
    await price_monitor.run_price_monitor()
    s = _snap()
    assert (s.color, s.price_basis) == (_expected_color(ML_ONLY), "ml")
    with Session(engine) as ses:
        rows = ses.exec(select(StoreMatch)).all()
    igual = [m for m in rows if m.category == "igual"]
    assert igual and all(m.price_doubtful for m in igual)


async def test_stores_on_when_only_the_stores_have_an_identical_the_color_is_real_and_says_where_it_comes_from(sw):
    runtime.set_value("pm_stores_affect_color", 1)
    FakeVendure.products = [_product("1", "Producto raro")]
    sw.ml.search["Producto raro"] = []
    add_item(GD, "gd-raro", "Producto raro", 25_000, 0.95)
    await price_monitor.run_price_monitor()
    s = _snap()
    assert (s.color, s.price_basis, s.ml_status) == (_expected_color([25_000]), "tiendas", "no_data")
    assert s.estimated_color is None
    [run] = _runs()
    assert (run.n_verde, run.n_ok, run.n_no_data) == (1, 0, 1)


async def test_stores_on_confirmed_store_similars_feed_the_estimate_but_not_the_ones_with_a_pack(sw):
    from app.pricing.semaforo import PricedVariant

    runtime.set_value("pm_stores_affect_color", 1)
    prod = _product("3", "Producto raro")
    prod.priced_variants = [PricedVariant(id="v3", name="", sku="", price_with_tax_cents=10_000, currency="ARS",
                                          specs={"length": 40.0, "width": 30.0})]
    FakeVendure.products = [prod]
    sw.ml.search["Producto raro"] = []
    add_item(GD, "gd-70", "Producto raro 70x90 cm", 30_000, 0.95, product="3")        # medida: confirmado por el chequeo
    add_item(CP, "cp-pack", "Pack x4 Producto raro", 90_000, 0.95, product="3")       # pack: su precio no es comparable
    await price_monitor.run_price_monitor()
    s = _snap("3")
    assert s.color == "sin_dato"
    assert (s.estimated_color, s.estimated_median_cents, s.estimated_listing_count) == (
        _expected_color([30_000]), 30_000, 1)


@pytest.mark.parametrize("affect,expected", [(0, [25_000]), (1, [25_000, 30_000, 40_000])])
async def test_the_estimate_joins_the_confirmed_similars_of_the_three_sources_only_when_stores_count(sw, affect, expected):
    """ML trae un tamaño distinto a 250, Gadnic otro a 300 y Casa Perfecta otro a 400 (los tres confirmados por el chequeo de
    medidas): el estimado es la mediana de los tres con 1 y la de ML sola con 0. El color REAL sigue en «sin dato»."""
    from app.pricing.semaforo import PricedVariant

    runtime.set_value("pm_stores_affect_color", affect)
    prod = _product("3", "Producto raro")
    prod.priced_variants = [PricedVariant(id="v3", name="", sku="", price_with_tax_cents=10_000, currency="ARS",
                                          specs={"length": 40.0, "width": 30.0})]
    FakeVendure.products = [prod]
    sw.web.pages["producto-raro"] = _web_page(_card("MLA921", "Producto Raro 60x80 cm", 250.0))
    _score(sw, "MLA921", 0.95)
    add_item(GD, "gd-70", "Producto raro 70x90 cm", 30_000, 0.95, product="3")
    add_item(CP, "cp-50", "Producto raro 50x60 cm", 40_000, 0.95, product="3")
    await price_monitor.run_price_monitor()
    s = _snap("3")
    assert (s.color, s.price_basis, s.estimated_from) == ("sin_dato", "ml", "similar")
    assert (s.estimated_median_cents, s.estimated_listing_count) == (semaforo.median_cents(expected), len(expected))
    assert s.estimated_color == _expected_color(expected)
    assert s.estimated_margin_pct == pytest.approx(round(_margin(expected), 2))


# ─── el botón == la próxima corrida (para tiendas) ──────────────────────────


def _shape(s: MarketPriceSnapshot) -> dict:
    return {k: getattr(s, k) for k in ("color", "est_margin_pct", "price_basis", "estimated_color", "estimated_median_cents",
                                       "ml_status", "ml_median_cents", "match_state")}


def _counters(run_id: int) -> dict:
    with Session(engine) as s:
        run = s.get(PriceMonitorRun, run_id)
        return {c: getattr(run, c) for c in ("n_ok", "n_no_data", "n_verde", "n_amarillo", "n_rojo", "n_sin_dato",
                                              "n_est_verde", "n_est_amarillo", "n_est_rojo")}


def _match_id(run_id: int, store: str, title: str) -> int:
    with Session(engine) as s:
        return int(s.exec(select(StoreMatch.id).where(
            StoreMatch.run_id == run_id, StoreMatch.store_id == _store_id(store), StoreMatch.title == title)).one())


@pytest.mark.parametrize("affect", [1, 0])
async def test_the_store_button_leaves_the_snapshot_as_the_next_run_with_the_mark_would(sw, client, affect):
    runtime.set_value("pm_stores_affect_color", affect)
    add_item(GD, "gd-org", "Organizador de cocina", 20_000, 0.95)
    add_item(CP, "cp-org", "Organizador de cocina plegable", 22_000, 0.95)
    add_item(CP, "cp-otro", "Cortina de baño impermeable", 5_000, 0.20)
    await price_monitor.run_price_monitor()
    run0 = _runs()[-1].id
    start = _shape(_snap(run_id=run0))
    steps = [("no_es", GD, "Organizador de cocina"), ("no_es", CP, "Organizador de cocina plegable"),
             ("es", CP, "Cortina de baño impermeable"), ("es", GD, "Organizador de cocina"),
             ("no_es", CP, "Cortina de baño impermeable")]
    for label, store, title in steps:
        r = client.post(f"/api/price-monitor/store-matches/{_match_id(run0, store, title)}/label", json={"label": label})
        assert r.status_code == 200, r.text
        button = _shape(_snap(run_id=run0))
        after_button = _counters(run0)
        await price_monitor.run_price_monitor()
        rerun = _runs()[-1]
        assert rerun.id != run0
        fresh = _shape(_snap(run_id=rerun.id))
        assert button == fresh, f"{label} {store} {title}: el botón y la próxima corrida no coinciden"
        assert after_button == _counters(rerun.id), f"{label} {store} {title}: contadores del botón vs corrida"
    assert start != button or affect == 0


async def test_undo_walks_back_to_the_previous_mark_and_then_to_hugos_opinion(sw, client):
    runtime.set_value("pm_stores_affect_color", 1)
    add_item(GD, "gd-org", "Organizador de cocina", 20_000, 0.95)
    add_item(CP, "cp-org", "Organizador de cocina plegable", 22_000, 0.95)
    await price_monitor.run_price_monitor()
    run0 = _runs()[-1].id
    mid = _match_id(run0, GD, "Organizador de cocina")
    base = _shape(_snap(run_id=run0))
    url = f"/api/price-monitor/store-matches/{mid}/label"
    # no_es → es → (deshacer) vuelve a no_es → (deshacer) vuelve a lo que decidió Hugo
    assert client.post(url, json={"label": "no_es"}).json()["match"]["category"] == "diferente"
    shape_no = _shape(_snap(run_id=run0))
    assert client.post(url, json={"label": "es"}).json()["match"]["category"] == "igual"
    r = client.delete(url).json()["match"]
    assert (r["category"], r["human_label"]) == ("diferente", "no_es") and _shape(_snap(run_id=run0)) == shape_no
    r = client.delete(url).json()["match"]
    assert (r["category"], r["human_label"]) == ("igual", None) and _shape(_snap(run_id=run0)) == base
    with Session(engine) as s:
        assert s.exec(select(StoreMatchFeedback)).all() == []


async def test_a_correction_on_one_product_does_not_touch_another_one_sharing_the_store_item(sw, client):
    runtime.set_value("pm_stores_affect_color", 1)
    FakeVendure.products = [_product("1", "Organizador cocina"), _product("2", "Organizador cocina")]
    sw.ml.search["Organizador cocina"] = [_candidate("MLA1", "Organizador de cocina")]
    add_item(GD, "gd-org", "Organizador de cocina", 20_000, 0.95, product="1")
    SCORES[("2", GD_IMG.format("gd-org"))] = 0.95                       # el mismo item de Gadnic es idéntico del 2 también
    await price_monitor.run_price_monitor()
    run0 = _runs()[-1].id
    before_2 = _shape(_snap("2", run0))
    with Session(engine) as s:
        m1 = s.exec(select(StoreMatch).where(StoreMatch.run_id == run0, StoreMatch.product_id == "1")).one()
        m2_before = s.exec(select(StoreMatch).where(StoreMatch.run_id == run0, StoreMatch.product_id == "2")).one()
        assert (m1.item_id, m1.category) == (m2_before.item_id, "igual") and m2_before.category == "igual"
    assert client.post(f"/api/price-monitor/store-matches/{m1.id}/label", json={"label": "no_es"}).status_code == 200
    with Session(engine) as s:
        m2 = s.exec(select(StoreMatch).where(StoreMatch.run_id == run0, StoreMatch.product_id == "2")).one()
        assert (m2.category, m2.human_label) == ("igual", None)
        fb = s.exec(select(StoreMatchFeedback)).all()
        assert [(f.product_id, f.label) for f in fb] == [("1", "no_es")]
    assert _shape(_snap("2", run0)) == before_2
    # y la próxima corrida: al 1 sigue visible pero como diferente (marcado a mano), al 2 se lo propone igual
    await price_monitor.run_price_monitor()
    run1 = _runs()[-1].id
    with Session(engine) as s:
        rows1 = {m.item_id: (m.category, m.human_label) for m in s.exec(
            select(StoreMatch).where(StoreMatch.run_id == run1, StoreMatch.product_id == "1")).all()}
        c2 = [m.category for m in s.exec(select(StoreMatch).where(StoreMatch.run_id == run1, StoreMatch.product_id == "2")).all()]
    assert rows1[m1.item_id] == ("diferente", "no_es") and c2 == ["igual"]


# ─── el tope del juez de las tiendas es propio, no el de ML ─────────────────


@pytest.fixture
def judge_world(sw, monkeypatch):
    """6 productos cuyo único candidato de ML cae en la banda AMBIGUA (0,62) y cuya mejor ficha en cada tienda también.
    Juez de mentira que reserva del MISMO contador diario que el real y confirma todo lo que le preguntan."""
    n = 6
    FakeVendure.products = [_product(str(i), f"Soporte celular auto {i}") for i in range(1, n + 1)]
    for i in range(1, n + 1):
        sw.ml.search[f"Soporte celular auto {i}"] = [_candidate(f"MLA8{i}", f"Soporte celular para auto {i}")]
        sw.ml.items[f"MLA8{i}"] = [_listing(f"I8{i}", "101", 300.0)]
        sw.image_scores[ML_IMG.format(f"MLA8{i}")] = 0.62
        add_item(GD, f"gd-{i}", f"Soporte celular auto {i}", 29_000, 0.62, product=str(i))
        add_item(CP, f"cp-{i}", f"Soporte celular auto {i} reforzado", 31_000, 0.62, product=str(i))
    calls: list[str] = []

    async def judge(our_name, our_images, candidates, *, max_calls, on_reserve=None,
                    counter_key=market_judge.LLM_COUNTER_KEY, **kw):
        if await daily_budget.reserve_async(counter_key, max_calls, None, on_reserve) is None:
            return None
        calls.append((our_name, counter_key))
        return market_judge.JudgeResult(
            verdicts={c.ml_id: market_judge.JudgeVerdict(c.ml_id, True, 0.9, "igual") for c in candidates})

    monkeypatch.setattr(market_judge, "enabled", lambda: True)
    monkeypatch.setattr(market_judge, "make_client", lambda: None)
    monkeypatch.setattr(market_judge, "judge", judge)
    sw.judge_calls = calls
    return sw


def _ml_view() -> dict[str, tuple]:
    return {pid: (s.ml_status, s.color, s.ml_median_cents, s.match_source) for pid, s in _snaps().items()}


async def test_the_judge_cap_is_separate_but_the_stores_must_not_leave_ml_without_judge(judge_world):
    """Con el tope justo para ML sola (6 productos = 6 llamadas) y las tiendas apagadas, ML juzga a los 6. Con las
    tiendas prendidas (y SIN contar para el color) el resultado de ML tiene que ser el mismo: hoy no lo es, porque
    cada producto gasta además una llamada por tienda y a los últimos productos se les acaba el tope."""
    _set("pm_vision_max_calls", 6)
    with Session(engine) as s:                                           # tiendas apagadas
        s.execute(update(MarketStore).values(enabled=False))
        s.commit()
    await price_monitor.run_price_monitor()
    off = _ml_view()
    assert {v[0] for v in off.values()} == {"ok"}, "con el tope justo ML tenía que juzgar los 6"
    used_off = daily_budget.used_today(market_judge.LLM_COUNTER_KEY)

    # segundo día: tope vacío, tiendas prendidas
    with Session(engine) as s:
        s.execute(update(MarketStore).values(enabled=True))
        for r in s.exec(select(MarketPriceSnapshot)).all():
            s.delete(r)
        s.commit()
    from app.db.models import Setting
    with Session(engine) as s:
        row = s.get(Setting, market_judge.LLM_COUNTER_KEY)
        s.delete(row)
        s.commit()
    judge_world.judge_calls.clear()
    await price_monitor.run_price_monitor()
    on = _ml_view()
    print("ML con juez: tiendas apagadas", sorted(off.items()), "| prendidas", sorted(on.items()),
          "| llamadas apagadas", used_off, "prendidas", len(judge_world.judge_calls))
    assert on == off, "las tiendas le sacaron llamadas al juez de ML: cambió el resultado de ML"
    ml_calls = [k for _n, k in judge_world.judge_calls if k == market_judge.LLM_COUNTER_KEY]
    store_calls = [k for _n, k in judge_world.judge_calls if k == market_judge.STORES_LLM_COUNTER_KEY]
    assert len(ml_calls) == 6 and store_calls, "ML gastó las suyas y las tiendas, las de su contador"
    assert daily_budget.used_today(market_judge.LLM_COUNTER_KEY) == 6


async def test_a_no_es_el_mismo_candidate_stays_visible_as_different_in_the_next_run_like_in_ml(sw, client):
    """En ML, lo que una persona marca «No es el mismo» sigue visible como DIFERENTE en las corridas siguientes (con
    su precio) y se puede dar vuelta con «Es el mismo». En las tiendas el candidato marcado desaparece de la lista en
    la corrida siguiente: ya no hay forma de verlo ni de deshacerlo desde el dashboard, y el producto puede quedar en
    «nada» para esa tienda aunque tenga una ficha que Hugo conoce."""
    add_item(GD, "gd-org", "Organizador de cocina", 20_000, 0.95)
    await price_monitor.run_price_monitor()
    run0 = _runs()[-1].id
    mid = _match_id(run0, GD, "Organizador de cocina")
    assert client.post(f"/api/price-monitor/store-matches/{mid}/label", json={"label": "no_es"}).status_code == 200
    await price_monitor.run_price_monitor()
    run1 = _runs()[-1].id
    with Session(engine) as s:
        rows = s.exec(select(StoreMatch).where(StoreMatch.run_id == run1, StoreMatch.store_id == _store_id(GD))).all()
    assert [(m.title, m.category) for m in rows] == [("Organizador de cocina", "diferente")]
