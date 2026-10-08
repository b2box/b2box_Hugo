"""API del semáforo para lo nuevo: deshabilitados, origen, similares y el botón
"No es el mismo". Mismo entorno que test_price_monitor_routes.py."""

from __future__ import annotations

import json
import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import main as main_mod  # noqa: E402
from app.db.models import MarketMatchFeedback, MarketPriceSnapshot, PriceMonitorRun  # noqa: E402
from app.db.session import engine  # noqa: E402
from tests.test_price_monitor_routes import _env, client  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _clean_feedback(_env):
    with Session(engine) as s:
        for row in s.exec(select(MarketMatchFeedback)).all():
            s.delete(row)
        s.commit()
    yield


def _igual(ml_id="MLA901", price=25_000, **kw):
    return {"ml_id": ml_id, "title": "Producto Raro", "permalink": f"https://www.mercadolibre.com.ar/p/{ml_id}",
            "origin": "web", "category": "igual", "source": "clip", "image_score": 0.91, "name_score": 0.8,
            "confidence": None, "reason": "", "differences": [], "brand": None,
            "image_url": "https://http2.mlstatic.com/D_NQ_NP_1-F.jpg", "listings": 1, "min_cents": price,
            "median_cents": price, "prices_cents": [price], "sellers": ["Tienda"], "seller": "Tienda", **kw}


def _seed() -> int:
    with Session(engine) as s:
        run = PriceMonitorRun(status="ok", total_products=5, web_status="ok", web_searches=3,
                              web_bytes=600_000, web_blocked=1, n_web_ok=1, n_con_similares=1)
        s.add(run)
        s.commit()
        s.refresh(run)
        common = dict(run_id=run.id, our_price_cents=10_000, commission_pct=13.0, shipping_cents=0)
        s.add(MarketPriceSnapshot(product_id="1", product_name="Con API", color="verde", ml_status="ok",
                                  match_origin="api", est_margin_pct=50.0, ml_median_cents=20_000,
                                  ml_min_cents=20_000, ml_listing_count=1, ml_seller_count=1,
                                  matched_listings=json.dumps([_igual("MLA1", origin="api")]), **common))
        s.add(MarketPriceSnapshot(product_id="2", product_name="Con web", color="amarillo", ml_status="ok",
                                  match_origin="web", est_margin_pct=20.0, ml_median_cents=25_000,
                                  ml_min_cents=25_000, ml_listing_count=1, ml_seller_count=1,
                                  web_searches=1, web_bytes=200_000, web_state="ok",
                                  matched_listings=json.dumps([_igual()]),
                                  similar_count=1, similar_listings=json.dumps([
                                      _igual("MLA905", category="similar", differences=["cantidad", "<script>"],
                                             permalink="javascript:alert(1)", image_url="https://evil.example/x.jpg")]),
                                  **common))
        s.add(MarketPriceSnapshot(product_id="3", product_name="Solo parecidos", color="sin_dato",
                                  ml_status="no_data", web_state="ok", similar_count=2,
                                  similar_listings=json.dumps([_igual("MLA906", category="similar"),
                                                               _igual("MLA907", category="similar")]), **common))
        s.add(MarketPriceSnapshot(product_id="4", product_name="Deshabilitado", color="rojo", ml_status="ok",
                                  match_origin="api", est_margin_pct=2.0, product_enabled=False, **common))
        s.add(MarketPriceSnapshot(product_id="5", product_name="Deshabilitado sin dato", color="sin_dato",
                                  ml_status="no_data", product_enabled=False, **common))
        s.commit()
        return run.id


def _ids(resp):
    return sorted(i["product"]["id"] for i in resp.json()["items"])


def test_enabled_filter(client):
    _seed()
    assert _ids(client.get("/api/price-monitor/snapshots")) == ["1", "2", "3", "4", "5"]
    assert _ids(client.get("/api/price-monitor/snapshots?enabled=enabled")) == ["1", "2", "3"]
    assert _ids(client.get("/api/price-monitor/snapshots?enabled=disabled")) == ["4", "5"]
    assert _ids(client.get("/api/price-monitor/snapshots?enabled=all")) == ["1", "2", "3", "4", "5"]


def test_color_counters_follow_the_enabled_filter(client):
    _seed()
    all_c = client.get("/api/price-monitor/snapshots").json()["colors"]
    dis = client.get("/api/price-monitor/snapshots?enabled=disabled").json()["colors"]
    assert all_c == {"verde": 1, "amarillo": 1, "rojo": 1, "sin_dato": 2}
    assert dis == {"verde": 0, "amarillo": 0, "rojo": 1, "sin_dato": 1}


def test_every_item_says_whether_the_product_is_enabled(client):
    _seed()
    items = {i["product"]["id"]: i["product"]["enabled"]
             for i in client.get("/api/price-monitor/snapshots").json()["items"]}
    assert items == {"1": True, "2": True, "3": True, "4": False, "5": False}


def test_match_and_origin_filters(client):
    _seed()
    assert _ids(client.get("/api/price-monitor/snapshots?match=igual")) == ["1", "2", "4"]
    assert _ids(client.get("/api/price-monitor/snapshots?match=similar")) == ["2", "3"]
    assert _ids(client.get("/api/price-monitor/snapshots?match=solo_similar")) == ["3"]
    assert _ids(client.get("/api/price-monitor/snapshots?origin=web")) == ["2"]
    assert _ids(client.get("/api/price-monitor/snapshots?origin=api")) == ["1", "4"]
    assert _ids(client.get("/api/price-monitor/snapshots?origin=web&enabled=disabled")) == []


@pytest.mark.parametrize("qs", ["enabled=maybe", "match=casi", "origin=otra"])
def test_invalid_filters_are_a_400(client, qs):
    _seed()
    assert client.get(f"/api/price-monitor/snapshots?{qs}").status_code == 400


def test_old_fields_are_still_there_and_the_new_ones_are_added(client):
    _seed()
    item = next(i for i in client.get("/api/price-monitor/snapshots").json()["items"] if i["product"]["id"] == "2")
    for field in ("id", "run_id", "product", "variant_id", "captured_at", "ml_status", "ml_error",
                  "ml_median_cents", "ml_min_cents", "ml_listing_count", "ml_seller_count", "ml_currency",
                  "matched_listings", "match_source", "match_confidence", "image_score_max",
                  "name_score_max", "candidates_count", "ambiguous_count", "our_price_cents", "tier_used",
                  "commission_pct", "shipping_cents", "est_margin_pct", "color", "prev_color"):
        assert field in item, field
    for field in ("similar_count", "similar_listings", "match_origin", "web_state", "web_searches",
                  "web_bytes", "our_specs"):
        assert field in item, field
    [m] = item["matched_listings"]
    assert (m["origin"], m["category"], m["image_score"], m["name_score"]) == ("web", "igual", 0.91, 0.8)
    assert "prices_cents" not in m and "sellers" not in m          # interno, no se sirve


def test_third_party_data_is_sanitized_when_served(client):
    _seed()
    item = next(i for i in client.get("/api/price-monitor/snapshots").json()["items"] if i["product"]["id"] == "2")
    [sim] = item["similar_listings"]
    assert sim["permalink"] == "" and sim["image_url"] is None
    assert sim["differences"] == ["cantidad"]


def test_the_summary_and_runs_carry_the_web_numbers(client, monkeypatch):
    _seed()
    run = client.get("/api/price-monitor/runs").json()["items"][0]
    assert run["web"] == {"status": "ok", "searches": 3, "bytes": 600_000, "blocked": 1, "n_ok": 1,
                          "bytes_per_search": 200_000}
    assert run["n_con_similares"] == 1
    summary = client.get("/api/price-monitor/summary").json()
    assert summary["include_disabled"] is True
    assert set(summary["web"]) == {"used", "budget", "remaining", "off_reason"}
    assert summary["web"]["budget"] == 2000


# ─── No es el mismo ───────────────────────────────────────────────────────


def _snap_id(product_id):
    with Session(engine) as s:
        return s.exec(select(MarketPriceSnapshot.id).where(MarketPriceSnapshot.product_id == product_id)).first()


def test_not_the_same_removes_recalculates_and_excludes(client):
    _seed()
    sid = _snap_id("2")
    r = client.post(f"/api/price-monitor/snapshots/{sid}/not-same", json={"ml_id": "MLA901"})
    assert r.status_code == 200 and r.json()["already"] is False
    snap = r.json()["snapshot"]
    assert snap["ml_status"] == "no_data" and snap["color"] == "sin_dato" and snap["matched_listings"] == []
    assert snap["ml_median_cents"] is None and "No es el mismo" in snap["ml_error"]
    assert snap["similar_count"] == 1                       # los parecidos no se tocan
    with Session(engine) as s:
        [fb] = s.exec(select(MarketMatchFeedback)).all()
        assert (fb.product_id, fb.ml_id, fb.category, fb.origin, fb.source) == ("2", "MLA901", "igual", "web", "clip")
        assert fb.image_score == 0.91 and fb.snapshot_id == sid and fb.product_name == "Con web"
        stored = s.get(MarketPriceSnapshot, sid)
        assert stored.color == "sin_dato" and stored.ml_median_cents is None


def test_not_the_same_on_a_similar_publication_only_removes_it(client):
    _seed()
    sid = _snap_id("2")
    r = client.post(f"/api/price-monitor/snapshots/{sid}/not-same", json={"ml_id": "MLA905"})
    snap = r.json()["snapshot"]
    assert snap["similar_count"] == 0 and snap["similar_listings"] == []
    assert (snap["ml_status"], snap["color"], snap["ml_median_cents"]) == ("ok", "amarillo", 25_000)
    with Session(engine) as s:
        assert s.exec(select(MarketMatchFeedback)).one().category == "similar"


def test_not_the_same_twice_is_idempotent(client):
    _seed()
    sid = _snap_id("2")
    client.post(f"/api/price-monitor/snapshots/{sid}/not-same", json={"ml_id": "MLA901"})
    again = client.post(f"/api/price-monitor/snapshots/{sid}/not-same", json={"ml_id": "MLA901"})
    assert again.status_code == 200 and again.json()["already"] is True
    with Session(engine) as s:
        assert len(s.exec(select(MarketMatchFeedback)).all()) == 1


def test_recalculation_keeps_the_other_publications(client):
    sid = None
    _seed()
    with Session(engine) as s:
        snap = s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.product_id == "2")).one()
        sid = snap.id
        snap.matched_listings = json.dumps([_igual("MLA901", 25_000), _igual("MLA902", 45_000, seller="Otra"),
                                            _igual("MLA903", 65_000, seller="Otra más")])
        snap.ml_median_cents, snap.ml_min_cents, snap.ml_listing_count = 45_000, 25_000, 3
        s.add(snap)
        s.commit()
    r = client.post(f"/api/price-monitor/snapshots/{sid}/not-same", json={"ml_id": "MLA901"})
    snap = r.json()["snapshot"]
    assert (snap["ml_min_cents"], snap["ml_median_cents"], snap["ml_listing_count"]) == (45_000, 55_000, 2)
    assert snap["ml_status"] == "ok" and snap["color"] in ("verde", "amarillo", "rojo")
    # 55.000 − 13 % − 10.000 sobre 10.000
    assert snap["est_margin_pct"] == pytest.approx(378.5)


@pytest.mark.parametrize("body, code", [
    ({"ml_id": "../etc/passwd"}, 400), ({"ml_id": "MLA<x>"}, 400), ({"ml_id": "x"}, 422), ({}, 422),
    ({"ml_id": "MLA99999999"}, 404),
])
def test_not_the_same_validates_its_input(client, body, code):
    _seed()
    assert client.post(f"/api/price-monitor/snapshots/{_snap_id('2')}/not-same", json=body).status_code == code


def test_not_the_same_on_an_unknown_snapshot_is_a_404(client):
    assert client.post("/api/price-monitor/snapshots/99999/not-same", json={"ml_id": "MLA901"}).status_code == 404


def test_not_the_same_needs_a_session():
    anon = TestClient(main_mod.app)
    assert anon.post("/api/price-monitor/snapshots/1/not-same", json={"ml_id": "MLA1"}).status_code == 401
    assert anon.delete("/api/price-monitor/products/1/not-same/MLA1").status_code == 401


def test_undoing_the_exclusion(client):
    _seed()
    client.post(f"/api/price-monitor/snapshots/{_snap_id('2')}/not-same", json={"ml_id": "MLA901"})
    assert client.delete("/api/price-monitor/products/2/not-same/MLA901").json() == {"removed": True}
    assert client.delete("/api/price-monitor/products/2/not-same/MLA901").status_code == 404
    with Session(engine) as s:
        assert s.exec(select(MarketMatchFeedback)).all() == []
