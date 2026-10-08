"""API de las tiendas y de la tabla por fuente: columnas, "más barato afuera", filtros,
etiquetas "Es el mismo / No es el mismo" y alta de tiendas desde Configuración."""

from __future__ import annotations

import json
import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import main as main_mod  # noqa: E402
from app import runtime  # noqa: E402
from app.api import store_routes  # noqa: E402
from app.db.models import (  # noqa: E402
    MarketPriceSnapshot,
    MarketStore,
    PriceMonitorRun,
    StoreCatalogItem,
    StoreMatch,
    StoreMatchFeedback,
)
from app.db.session import engine  # noqa: E402
from app.pricing import store_catalog, store_match, store_urls  # noqa: E402
from tests import store_fixtures as fx  # noqa: E402
from tests.store_fixtures import store_db  # noqa: E402,F401  (fixture)
from tests.test_price_monitor_routes import _env, client  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _stores(_env, store_db):  # noqa: F811
    store_catalog.seed_default_stores()
    runtime.invalidate()
    yield
    store_urls.set_allowed_image_hosts([])
    runtime.invalidate()


def _sid(name: str) -> int:
    with Session(engine) as s:
        return int(s.exec(select(MarketStore.id).where(MarketStore.name == name)).one())


def _match(run_id, pid, store, n, category, price, *, doubtful=False, url=None, human=None, auto=None, **kw) -> StoreMatch:
    sid = _sid(store)
    base = fx.GD if store == "Gadnic" else fx.CP
    return StoreMatch(
        run_id=run_id, product_id=pid, store_id=sid, item_id=1000 * sid + n, rank=n, category=category,
        auto_category=auto or category, source="clip", title=f"{store} {category} {n}",
        url=url or f"{base}/productos/{store[0].lower()}{n}/", image_url=None, price_cents=price,
        price_doubtful=doubtful, image_score=0.9, name_score=0.8, human_label=human, **kw)


def _seed() -> int:
    """Una corrida con 4 productos:
      1 verde: ML igual (20.000), Gadnic igual (9.000 y uno dudoso de 100), Casa Perfecta similar (15.000)
      2 amarillo: solo ML similar; Gadnic solo diferente; Casa Perfecta igual (30.000)
      3 sin dato: nada en ML; Casa Perfecta igual con precio dudoso; Gadnic nada
      4 sin dato: nada en ningún lado
    """
    with Session(engine) as s:
        run = PriceMonitorRun(status="ok", total_products=4)
        s.add(run)
        s.commit()
        s.refresh(run)
        common = dict(run_id=run.id, our_price_cents=10_000, commission_pct=13.0, shipping_cents=0)
        listing = {"ml_id": "MLA1", "title": "Org ML", "permalink": "https://www.mercadolibre.com.ar/p/MLA1",
                   "origin": "api", "category": "igual", "source": "clip", "image_score": 0.9, "name_score": 0.8,
                   "min_cents": 19_000, "median_cents": 20_000, "prices_cents": [19_000, 21_000], "listings": 2,
                   "differences": []}
        s.add(MarketPriceSnapshot(product_id="1", product_name="Uno", color="verde", ml_status="ok", est_margin_pct=50.0,
                                  ml_median_cents=20_000, ml_min_cents=19_000, ml_listing_count=2, candidates_count=3,
                                  matched_listings=json.dumps([listing]), **common))
        s.add(MarketPriceSnapshot(product_id="2", product_name="Dos", color="amarillo", ml_status="no_data", similar_count=1,
                                  candidates_count=2, similar_listings=json.dumps([{**listing, "ml_id": "MLA2", "category": "similar",
                                                                                   "price_cents": 12_345}]), **common))
        s.add(MarketPriceSnapshot(product_id="3", product_name="Tres", color="sin_dato", ml_status="no_data", **common))
        s.add(MarketPriceSnapshot(product_id="4", product_name="Cuatro", color="sin_dato", ml_status="no_data", **common))
        s.add_all([
            _match(run.id, "1", "Gadnic", 1, "igual", 9_000), _match(run.id, "1", "Gadnic", 2, "igual", 100, doubtful=True),
            _match(run.id, "1", "Casa Perfecta", 1, "similar", 15_000, differences=json.dumps(["cantidad"])),
            _match(run.id, "1", "Casa Perfecta", 2, "diferente", 5_000),
            _match(run.id, "2", "Gadnic", 1, "diferente", 7_000),
            _match(run.id, "2", "Casa Perfecta", 1, "igual", 30_000),
            _match(run.id, "3", "Casa Perfecta", 1, "igual", 249, doubtful=True, price_note="precio muy bajo"),
        ])
        s.commit()
        run.source_stats = json.dumps(store_match.source_stats(s, run.id))
        s.add(run)
        s.commit()
        return run.id


def _items(resp) -> dict[str, dict]:
    return {i["product"]["id"]: i for i in resp.json()["items"]}


def _ids(resp) -> list[str]:
    return sorted(_items(resp))


# ─── la tabla por fuente ─────────────────────────────────────────────────────


def test_snapshots_list_the_sources_in_column_order(client):
    _seed()
    body = client.get("/api/price-monitor/snapshots").json()
    assert [s["label"] for s in body["sources"]] == ["Mercado Libre", "Gadnic", "Casa Perfecta"]
    assert [s["key"] for s in body["sources"]] == ["ml", f"store:{_sid('Gadnic')}", f"store:{_sid('Casa Perfecta')}"]


def test_each_row_has_the_best_result_of_every_source(client):
    _seed()
    items = _items(client.get("/api/price-monitor/snapshots"))
    gd, cp = f"store:{_sid('Gadnic')}", f"store:{_sid('Casa Perfecta')}"

    one = items["1"]["cells"]
    assert one["ml"]["category"] == "igual" and one["ml"]["price_cents"] == 20_000
    assert one["ml"]["url"] == "https://www.mercadolibre.com.ar/p/MLA1"
    assert one[gd]["category"] == "igual" and one[gd]["price_cents"] == 9_000 and one[gd]["price_doubtful"] is False
    assert one[gd]["counts"] == {"igual": 2, "similar": 0, "diferente": 0}
    assert one[cp]["category"] == "similar" and one[cp]["price_cents"] == 15_000, "sin idéntico: el similar, marcado como tal"

    two = items["2"]["cells"]
    assert two["ml"]["category"] == "similar" and two["ml"]["price_cents"] == 12_345
    assert two[gd]["category"] == "diferente" and two[gd]["price_cents"] == 7_000, "lo más parecido, marcado diferente"
    assert two[cp]["category"] == "igual"

    four = items["4"]["cells"]
    assert four["ml"]["category"] is None and four[gd]["category"] is None and four[cp]["category"] is None


def test_cheapest_outside_is_the_lowest_identical_price_ignoring_doubtful_ones(client):
    _seed()
    items = _items(client.get("/api/price-monitor/snapshots"))
    cheapest = items["1"]["cheapest_outside"]
    assert (cheapest["label"], cheapest["price_cents"]) == ("Gadnic", 9_000), "el de 100 es dudoso y no gana"
    assert cheapest["url"].startswith(fx.GD)
    assert items["2"]["cheapest_outside"]["label"] == "Casa Perfecta"
    assert items["3"]["cheapest_outside"] is None, "un único idéntico dudoso no es el más barato de nadie"
    assert items["4"]["cheapest_outside"] is None


def test_ml_can_be_the_cheapest_outside(client):
    run_id = _seed()
    with Session(engine) as s:
        snap = s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.run_id == run_id,
                                                          MarketPriceSnapshot.product_id == "2")).one()
        snap.ml_status, snap.ml_min_cents = "ok", 11_000
        snap.matched_listings = json.dumps([{"ml_id": "MLA9", "title": "ML barato", "min_cents": 11_000, "median_cents": 12_000,
                                             "permalink": "https://www.mercadolibre.com.ar/p/MLA9", "origin": "api"}])
        s.add(snap)
        s.commit()
    cheapest = _items(client.get("/api/price-monitor/snapshots"))["2"]["cheapest_outside"]
    assert (cheapest["label"], cheapest["price_cents"], cheapest["title"]) == ("Mercado Libre", 11_000, "ML barato")


def test_row_detail_has_every_candidate_of_every_store_sanitized(client):
    run_id = _seed()
    with Session(engine) as s:
        s.add(_match(run_id, "4", "Gadnic", 1, "similar", 5_000, url="javascript:alert(1)"))
        s.commit()
    items = _items(client.get("/api/price-monitor/snapshots"))
    detail = items["1"]["stores"][str(_sid("Casa Perfecta"))]
    assert detail["label"] == "Casa Perfecta" and [m["category"] for m in detail["matches"]] == ["similar", "diferente"]
    assert detail["matches"][0]["differences"] == ["cantidad"]
    assert items["4"]["stores"][str(_sid("Gadnic"))]["matches"][0]["url"] is None


def test_a_disabled_store_disappears_from_the_table(client):
    _seed()
    with Session(engine) as s:
        row = s.exec(select(MarketStore).where(MarketStore.name == "Gadnic")).one()
        row.enabled = False
        s.add(row)
        s.commit()
    body = client.get("/api/price-monitor/snapshots").json()
    assert [s["label"] for s in body["sources"]] == ["Mercado Libre", "Casa Perfecta"]
    assert all(str(_sid("Gadnic")) not in i["stores"] for i in body["items"])


def test_old_snapshots_without_store_data_still_serve(client):
    with Session(engine) as s:
        run = PriceMonitorRun(status="ok", total_products=1)
        s.add(run)
        s.commit()
        s.refresh(run)
        s.add(MarketPriceSnapshot(run_id=run.id, product_id="9", color="sin_dato", ml_status="no_data"))
        s.commit()
    item = _items(client.get("/api/price-monitor/snapshots"))["9"]
    assert item["price_basis"] == "ml" and item["cheapest_outside"] is None
    assert all(c["category"] is None for c in item["cells"].values())


# ─── filtros por fuente ──────────────────────────────────────────────────────


def test_source_filter_keeps_products_with_something_from_that_source(client):
    _seed()
    gd, cp = _sid("Gadnic"), _sid("Casa Perfecta")
    assert _ids(client.get(f"/api/price-monitor/snapshots?source={gd}")) == ["1", "2"]
    assert _ids(client.get(f"/api/price-monitor/snapshots?source=store:{cp}")) == ["1", "2", "3"]
    assert _ids(client.get("/api/price-monitor/snapshots?source=ml")) == ["1", "2"]


def test_igual_in_keeps_products_with_an_identical_in_any_of_the_sources(client):
    _seed()
    gd, cp = _sid("Gadnic"), _sid("Casa Perfecta")
    assert _ids(client.get("/api/price-monitor/snapshots?igual_in=ml")) == ["1"]
    assert _ids(client.get(f"/api/price-monitor/snapshots?igual_in={gd}")) == ["1"]
    assert _ids(client.get(f"/api/price-monitor/snapshots?igual_in={cp}")) == ["2", "3"]
    assert _ids(client.get(f"/api/price-monitor/snapshots?igual_in=ml,{cp}")) == ["1", "2", "3"]


def test_source_filters_combine_with_color_and_narrow_the_color_chips(client):
    _seed()
    cp = _sid("Casa Perfecta")
    body = client.get(f"/api/price-monitor/snapshots?igual_in={cp}").json()
    assert body["colors"] == {"verde": 0, "amarillo": 1, "rojo": 0, "sin_dato": 1}
    assert _ids(client.get(f"/api/price-monitor/snapshots?igual_in={cp}&color=amarillo")) == ["2"]
    assert body["total"] == 2


@pytest.mark.parametrize("qs", ["source=zzz", "source=store:x", "source=-1", "igual_in=ml,zzz", "igual_in=%27%3B--"])
def test_invalid_source_filters_are_a_400(client, qs):
    _seed()
    assert client.get(f"/api/price-monitor/snapshots?{qs}").status_code == 400


# ─── contadores en la corrida y en Salud ─────────────────────────────────────


def test_runs_and_summary_expose_the_per_source_counters_and_the_index_state(client):
    _seed()
    run = client.get("/api/price-monitor/runs").json()["items"][0]
    gd = f"store:{_sid('Gadnic')}"
    assert run["sources"]["ml"] == {"label": "Mercado Libre", "total": 4, "igual": 1, "similar": 1, "diferente": 0, "nada": 2}
    assert run["sources"][gd]["igual"] == 1 and run["sources"][gd]["diferente"] == 1 and run["sources"][gd]["nada"] == 2
    summary = client.get("/api/price-monitor/summary").json()
    assert [s["name"] for s in summary["stores"]] == ["Gadnic", "Casa Perfecta"]
    assert summary["stores"][0]["max_pages_per_day"] == 2000 and summary["stores_affect_color"] is False
    assert summary["last_run"]["sources"] == run["sources"]


# ─── "Es el mismo" / "No es el mismo" ────────────────────────────────────────


def _match_id(pid: str, store: str, n: int) -> int:
    with Session(engine) as s:
        return int(s.exec(select(StoreMatch.id).where(StoreMatch.product_id == pid, StoreMatch.store_id == _sid(store),
                                                       StoreMatch.rank == n)).one())


def test_label_a_match_and_undo_it(client):
    run_id = _seed()
    mid = _match_id("1", "Casa Perfecta", 1)                        # similar
    r = client.post(f"/api/price-monitor/store-matches/{mid}/label", json={"label": "es"})
    assert r.status_code == 200
    body = r.json()
    assert body["match"]["category"] == "igual" and body["match"]["human_label"] == "es"
    assert body["match"]["auto_category"] == "similar" and body["snapshot"]["color"] == "verde"
    with Session(engine) as s:
        fb = s.exec(select(StoreMatchFeedback)).one()
        assert (fb.label, fb.product_id, fb.actor) == ("es", "1", "admin")
        stats = json.loads(s.get(PriceMonitorRun, run_id).source_stats)
    assert stats[f"store:{_sid('Casa Perfecta')}"]["igual"] == 3          # 1 (era similar), 2 y 3

    r = client.delete(f"/api/price-monitor/store-matches/{mid}/label")
    assert r.status_code == 200 and r.json()["match"]["category"] == "similar" and r.json()["match"]["human_label"] is None
    with Session(engine) as s:
        assert s.exec(select(StoreMatchFeedback)).all() == []
        assert json.loads(s.get(PriceMonitorRun, run_id).source_stats)[f"store:{_sid('Casa Perfecta')}"]["igual"] == 2


def test_labeling_twice_does_not_duplicate_the_correction(client):
    _seed()
    mid = _match_id("1", "Gadnic", 1)
    for label in ("no_es", "no_es", "es"):
        assert client.post(f"/api/price-monitor/store-matches/{mid}/label", json={"label": label}).status_code == 200
    with Session(engine) as s:
        [fb] = s.exec(select(StoreMatchFeedback)).all()
        assert fb.label == "es"


def test_label_validation_and_missing_matches(client):
    _seed()
    mid = _match_id("1", "Gadnic", 1)
    assert client.post(f"/api/price-monitor/store-matches/{mid}/label", json={"label": "quizas"}).status_code == 400
    assert client.post(f"/api/price-monitor/store-matches/{mid}/label", json={}).status_code == 422
    assert client.post(f"/api/price-monitor/store-matches/{mid}/label", json={"label": "es", "x": 1}).status_code == 422
    assert client.post("/api/price-monitor/store-matches/999999/label", json={"label": "es"}).status_code == 404
    assert client.delete("/api/price-monitor/store-matches/999999/label").status_code == 404
    assert client.post("/api/price-monitor/store-matches/0/label", json={"label": "es"}).status_code == 422


def test_labeling_recolors_when_the_stores_count(client):
    _seed()
    runtime.set_value("pm_stores_affect_color", 1)
    mid = _match_id("2", "Casa Perfecta", 1)                        # 30.000, idéntico creíble de "Dos" (sin ML)
    r = client.post(f"/api/price-monitor/store-matches/{mid}/label", json={"label": "no_es"})
    assert r.status_code == 200
    # "Dos" no tiene ML ok y se quedó sin idénticos de tienda: sin dato.
    assert r.json()["snapshot"]["color"] == "sin_dato"
    r = client.delete(f"/api/price-monitor/store-matches/{mid}/label")
    assert r.json()["snapshot"]["color"] == "verde" and r.json()["snapshot"]["price_basis"] == "tiendas"


def test_endpoints_need_a_dashboard_session():
    anon = TestClient(main_mod.app)
    assert anon.get("/api/stores").status_code == 401
    assert anon.post("/api/stores", json={}).status_code == 401
    assert anon.put("/api/stores/1", json={}).status_code == 401
    assert anon.delete("/api/stores/1").status_code == 401
    assert anon.post("/api/stores/1/index").status_code == 401
    assert anon.post("/api/price-monitor/store-matches/1/label", json={"label": "es"}).status_code == 401


# ─── alta de tiendas desde Configuración ─────────────────────────────────────


def test_list_stores_has_the_two_seeded_stores_and_their_index_state(client):
    body = client.get("/api/stores").json()
    assert [s["name"] for s in body["items"]] == ["Gadnic", "Casa Perfecta"]
    assert body["platforms"] == ["tiendanube", "jsonld_sitemap"] and body["affect_color"] is False
    gd = body["items"][0]
    assert gd["platform"] == "jsonld_sitemap" and gd["max_pages_per_day"] == 2000 and gd["house_brand"] == "Gadnic"
    assert gd["index"]["urls"] == 0 and gd["index"]["pages_today"] == 0


def test_adding_another_tiendanube_store_needs_only_a_row(client):
    r = client.post("/api/stores", json={"name": "Otra Tienda", "base_url": "https://www.otratienda.com.ar",
                                         "platform": "tiendanube", "notes": "pedida por Nico"})
    assert r.status_code == 201
    new = r.json()
    assert new["enabled"] is True and new["refresh_days"] == 7 and new["max_pages_per_day"] == 1000
    assert "mitiendanube.com" in store_urls.allowed_image_hosts()
    assert [s["label"] for s in client.get("/api/price-monitor/snapshots").json()["sources"]][-1] == "Otra Tienda"
    assert client.post("/api/stores", json={"name": "Otra Tienda", "base_url": "https://x.com.ar",
                                            "platform": "tiendanube"}).status_code == 409


@pytest.mark.parametrize("payload", [
    {"name": "x", "base_url": "http://x.com.ar", "platform": "tiendanube"},
    {"name": "x", "base_url": "https://x.com.ar", "platform": "magento"},
    {"name": "x", "base_url": "https://x.com.ar", "platform": "tiendanube", "max_pages_per_day": 0},
    {"name": "x", "base_url": "https://x.com.ar", "platform": "tiendanube", "image_hosts": "com.ar"},
    {"name": "x", "base_url": "https://x.com.ar", "platform": "tiendanube", "sitemap_url": "https://evil.com/s.xml"},
    {"name": "", "base_url": "https://x.com.ar", "platform": "tiendanube"},
    {"name": "x", "base_url": "https://x.com.ar", "platform": "tiendanube", "id": 5},
    {},
])
def test_invalid_stores_are_rejected(client, payload):
    assert client.post("/api/stores", json=payload).status_code in (422,)


def test_edit_and_disable_a_store(client):
    gd = _sid("Gadnic")
    r = client.put(f"/api/stores/{gd}", json={"enabled": False, "max_pages_per_day": 500, "house_brand": ""})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False and body["max_pages_per_day"] == 500 and body["house_brand"] is None
    assert body["name"] == "Gadnic" and body["platform"] == "jsonld_sitemap"      # lo no mandado no se toca
    assert "bidcom.com.ar" not in store_urls.allowed_image_hosts()
    assert client.put(f"/api/stores/{gd}", json={"refresh_days": 500}).status_code == 422
    assert client.put(f"/api/stores/{gd}", json={"name": "Casa Perfecta"}).status_code == 409
    assert client.put("/api/stores/999999", json={"enabled": True}).status_code == 404


def test_delete_a_store_removes_what_was_saved_of_it(client):
    run_id = _seed()
    gd = _sid("Gadnic")
    with Session(engine) as s:
        s.add(StoreCatalogItem(store_id=gd, url=f"{fx.GD}/x"))
        s.commit()
    assert client.delete(f"/api/stores/{gd}").json() == {"removed": True}
    with Session(engine) as s:
        assert s.exec(select(StoreCatalogItem).where(StoreCatalogItem.store_id == gd)).all() == []
        assert s.exec(select(StoreMatch).where(StoreMatch.store_id == gd, StoreMatch.run_id == run_id)).all() == []
    assert client.delete(f"/api/stores/{gd}").status_code == 404


def test_index_now_runs_in_background_and_refuses_a_second_one(client, monkeypatch):
    calls: list[int] = []

    async def fake_index(store_id, **kw):
        calls.append(store_id)

    monkeypatch.setattr(store_catalog, "index_store", fake_index)
    cp = _sid("Casa Perfecta")
    r = client.post(f"/api/stores/{cp}/index")
    assert r.status_code == 202 and r.json() == {"status": "scheduled", "store": "Casa Perfecta"}
    assert client.post("/api/stores/999999/index").status_code == 404
    import asyncio

    lock = asyncio.Lock()
    store_catalog._locks[cp] = lock
    try:
        async def hold():
            await lock.acquire()
        asyncio.run(hold())
        assert client.post(f"/api/stores/{cp}/index").status_code == 409
    finally:
        store_catalog._locks.pop(cp, None)
    assert store_routes._background is not None


# ─── Mercado Libre con "siempre trae algo" (match_state, no priced, diferentes) ──────────


def _ml_listing(ml_id, category, title, price=None, **kw) -> dict:
    return {"ml_id": ml_id, "title": title, "permalink": f"https://www.mercadolibre.com.ar/p/{ml_id}", "origin": "api",
            "category": category, "source": "clip", "image_score": 0.5, "name_score": 0.5, "price_cents": price, **kw}


def _seed_ml_states() -> int:
    with Session(engine) as s:
        run = PriceMonitorRun(status="ok", total_products=5)
        s.add(run)
        s.commit()
        s.refresh(run)
        common = dict(run_id=run.id, our_price_cents=10_000, commission_pct=13.0, shipping_cents=0, ml_status="no_data")
        s.add(MarketPriceSnapshot(product_id="1", color="sin_dato", match_state="igual_sin_precio", candidates_count=2,
                                  unpriced_listings=json.dumps([_ml_listing("MLA1", "igual", "Sin vendedores")]), **common))
        s.add(MarketPriceSnapshot(product_id="2", color="sin_dato", match_state="diferente", candidates_count=2, other_count=1,
                                  other_listings=json.dumps([_ml_listing("MLA2", "diferente", "Otra cosa", 7_777)]), **common))
        s.add(MarketPriceSnapshot(product_id="3", color="sin_dato", match_state="similar", candidates_count=1, similar_count=1,
                                  similar_listings=json.dumps([_ml_listing("MLA3", "similar", "Casi", 8_888)]), **common))
        s.add(MarketPriceSnapshot(product_id="4", color="sin_dato", match_state="ninguno", **common))
        s.add(MarketPriceSnapshot(product_id="5", color="sin_dato", ml_status="failed", match_state=None, run_id=run.id))
        s.commit()
        run.source_stats = json.dumps(store_match.source_stats(s, run.id))
        s.add(run)
        s.commit()
        return run.id


def test_the_ml_cell_follows_the_match_state_of_siempre_trae_algo(client):
    _seed_ml_states()
    cells = {pid: i["cells"]["ml"] for pid, i in _items(client.get("/api/price-monitor/snapshots")).items()}
    assert (cells["1"]["category"], cells["1"]["price_cents"], cells["1"]["title"]) == ("igual", None, "Sin vendedores")
    assert (cells["2"]["category"], cells["2"]["price_cents"], cells["2"]["title"]) == ("diferente", 7_777, "Otra cosa")
    assert (cells["3"]["category"], cells["3"]["price_cents"]) == ("similar", 8_888)
    assert cells["4"]["category"] is None and cells["5"]["category"] is None
    assert cells["2"]["counts"] == {"igual": 0, "similar": 0, "diferente": 1}


def test_ml_counters_use_the_match_state(client):
    _seed_ml_states()
    ml = client.get("/api/price-monitor/runs").json()["items"][0]["sources"]["ml"]
    assert ml == {"label": "Mercado Libre", "total": 5, "igual": 1, "similar": 1, "diferente": 1, "nada": 2}


def test_ml_source_filters_use_the_match_state(client):
    _seed_ml_states()
    assert _ids(client.get("/api/price-monitor/snapshots?igual_in=ml")) == ["1"]       # idéntico aunque sin precio
    assert _ids(client.get("/api/price-monitor/snapshots?source=ml")) == ["1", "2", "3"]
