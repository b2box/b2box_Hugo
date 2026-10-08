"""API de «Siempre trae algo»: los estados de lo que devolvió ML (idéntico / solo
similares / solo diferentes / sin resultados), los filtros y conteos nuevos, y el
botón "Es el mismo" (con "No es el mismo" y "Deshacer")."""

from __future__ import annotations

import json
import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import auth, runtime  # noqa: E402
from app import main as main_mod  # noqa: E402
from app.db.models import MarketMatchFeedback, MarketPriceSnapshot, PriceMonitorRun, Setting  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import price_monitor  # noqa: E402
from tests.test_price_monitor_routes import _env, client  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _clean(_env):
    with Session(engine) as s:
        for model in (MarketMatchFeedback, Setting):
            for row in s.exec(select(model)).all():
                s.delete(row)
        s.commit()
    runtime.invalidate()
    yield
    runtime.invalidate()


def _pub(ml_id, category, price=None, *, image=0.9, name=0.8, est_ok=True, **kw):
    e = {"ml_id": ml_id, "title": f"Publicación {ml_id}", "permalink": f"https://www.mercadolibre.com.ar/p/{ml_id}",
         "origin": "web", "category": category, "source": "specs" if category == "similar" else "clip",
         "image_score": image, "name_score": name, "confidence": None,
         "reason": "difiere en cantidad" if category == "similar" else "otro producto: la foto no se parece",
         "differences": ["cantidad"] if category == "similar" else [], "notes": [], "brand": None,
         "image_url": None, "seller": "Tienda", "sold_quantity": 500, "price_cents": price, "est_ok": est_ok}
    e.update(kw)
    return e


def _igual(ml_id, price):
    return _pub(ml_id, "igual", price, listings=1, min_cents=price, median_cents=price,
                prices_cents=[price], sellers=["Tienda"], source="clip")


def _seed():
    """Una corrida con un producto de cada estado. Nuestro precio: $100 en todos."""
    with Session(engine) as s:
        run = PriceMonitorRun(status="ok", total_products=6)
        s.add(run)
        s.commit()
        s.refresh(run)
        base = dict(run_id=run.id, our_price_cents=10_000, commission_pct=13.0, shipping_cents=0, ml_status="no_data")

        def product(pid, name, *, igual=(), unpriced=(), similar=(), other=(), ok=False, color="sin_dato",
                    enabled=True, origin=None):
            snap = MarketPriceSnapshot(product_id=pid, product_name=name, product_enabled=enabled,
                                       **{**base, **({"ml_status": "ok", "color": color, "match_origin": origin or "web",
                                                      "ml_median_cents": 25_000, "ml_min_cents": 25_000,
                                                      "ml_listing_count": 1, "ml_seller_count": 1,
                                                      "est_margin_pct": 117.5} if ok else {"color": "sin_dato"})})
            price_monitor._store_listings(snap, list(igual), list(unpriced), list(similar), list(other),
                                          keep=8, green_min=30.0, yellow_min=10.0)
            s.add(snap)

        product("1", "Con idéntico", igual=[_igual("MLA101", 25_000)], ok=True, color="verde")
        product("2", "Solo similares", similar=[_pub("MLA221", "similar", 25_000), _pub("MLA222", "similar", 30_000)])
        product("3", "Similares caros", similar=[_pub("MLA331", "similar", 9_000)])          # rojo estimado
        product("4", "Solo diferentes", other=[_pub("MLA441", "diferente", 99_000, image=0.2, name=0.2),
                                               _pub("MLA442", "diferente", 12_000, image=0.1, name=0.1)])
        product("5", "Sin resultados")
        product("6", "Deshabilitado con similares", similar=[_pub("MLA661", "similar", 14_000)], enabled=False)
        s.commit()
        return run.id


def _snap(product_id) -> MarketPriceSnapshot:
    with Session(engine) as s:
        return s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.product_id == product_id)).one()


def _ids(resp):
    return sorted(i["product"]["id"] for i in resp.json()["items"])


def _get(client, **params):
    return client.get("/api/price-monitor/snapshots", params=params)


# ─── los cuatro estados ───────────────────────────────────────────────────


def test_each_product_has_its_state_and_only_similars_have_an_estimated_color(client):
    _seed()
    items = {i["product"]["id"]: i for i in _get(client).json()["items"]}
    assert {pid: i["match_state"] for pid, i in items.items()} == {
        "1": "igual", "2": "similar", "3": "similar", "4": "diferente", "5": "ninguno", "6": "similar"}
    # color estimado: solo sin idéntico y con precios de similares; el real queda "sin dato"
    assert [(pid, i["estimated_color"], i["color"]) for pid, i in sorted(items.items())] == [
        ("1", None, "verde"), ("2", "verde", "sin_dato"), ("3", "rojo", "sin_dato"),
        ("4", None, "sin_dato"), ("5", None, "sin_dato"), ("6", "amarillo", "sin_dato")]
    two = items["2"]
    assert two["estimated_from"] == "similar" and two["estimated_median_cents"] == 27_500
    assert two["estimated_listing_count"] == 2 and two["estimated_margin_pct"] == pytest.approx(139.25, abs=0.01)
    assert two["est_margin_pct"] is None and two["ml_median_cents"] is None       # el real, vacío


def test_the_real_color_chips_do_not_change_and_the_estimated_ones_are_separate(client):
    _seed()
    body = _get(client).json()
    assert body["colors"] == {"verde": 1, "amarillo": 0, "rojo": 0, "sin_dato": 5}       # solo lo real
    assert body["estimated_colors"] == {"verde": 1, "amarillo": 1, "rojo": 1}
    assert body["states"] == {"igual": 1, "solo_similar": 3, "solo_diferente": 1, "ninguno": 1}


def test_the_new_filters(client):
    _seed()
    assert _ids(_get(client, match="igual")) == ["1"]
    assert _ids(_get(client, match="solo_similar")) == ["2", "3", "6"]
    assert _ids(_get(client, match="similar")) == ["2", "3", "6"]
    assert _ids(_get(client, match="solo_diferente")) == ["4"]
    assert _ids(_get(client, match="diferente")) == ["4"]
    assert _ids(_get(client, match="ninguno")) == ["5"]
    assert _ids(_get(client, estimated="verde")) == ["2"]
    assert _ids(_get(client, estimated="amarillo")) == ["6"]
    assert _ids(_get(client, estimated="rojo")) == ["3"]
    assert _ids(_get(client, estimated="any")) == ["2", "3", "6"]
    assert _ids(_get(client, match="solo_similar", enabled="disabled")) == ["6"]


def test_filters_only_accept_the_whitelist(client):
    _seed()
    for qs in ({"match": "casi"}, {"match": "ninguno;drop"}, {"estimated": "azul"}, {"estimated": "ANY"}):
        assert _get(client, **qs).status_code == 400, qs


def test_estimated_color_filter_never_shows_products_with_a_real_color(client):
    run_id = _seed()
    with Session(engine) as s:         # un producto con idéntico al que le quedó un estimado viejo: no cuenta
        snap = s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.product_id == "1")).one()
        snap.estimated_color = "rojo"
        s.add(snap)
        s.commit()
    assert "1" not in _ids(_get(client, estimated="any"))
    assert _get(client).json()["estimated_colors"]["rojo"] == 1                 # solo el 3


def test_rows_with_a_real_margin_sort_first_then_the_estimated_ones(client):
    _seed()
    order = [i["product"]["id"] for i in _get(client).json()["items"]]
    assert order[0] == "1" and order.index("3") < order.index("2")             # estimado: peor margen primero
    assert order.index("2") < order.index("4")                                  # y antes de los que no tienen nada


def test_the_snapshot_serves_the_three_lists_with_sanitized_links(client):
    _seed()
    with Session(engine) as s:
        snap = s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.product_id == "4")).one()
        bad = _pub("MLA443", "diferente", 5_000, permalink="javascript:alert(1)", image_url="https://evil.example/x.jpg",
                   price_cents="no es un numero", differences=["marca", "<script>"])
        snap.other_listings = json.dumps([*json.loads(snap.other_listings), bad])
        s.add(snap)
        s.commit()
    item = next(i for i in _get(client).json()["items"] if i["product"]["id"] == "4")
    assert item["other_count"] == 2                                              # el contador guardado
    others = {o["ml_id"]: o for o in item["other_listings"]}
    assert others["MLA441"]["price_cents"] == 99_000 and others["MLA441"]["category"] == "diferente"
    assert others["MLA441"]["reason"] and others["MLA441"]["origin"] == "web"
    assert others["MLA443"]["permalink"] == "" and others["MLA443"]["image_url"] is None
    assert others["MLA443"]["price_cents"] is None and others["MLA443"]["differences"] == ["marca"]
    assert "est_ok" not in others["MLA441"] and "prices_cents" not in others["MLA441"]
    for field in ("unpriced_listings", "similar_listings", "matched_listings", "estimated_color", "estimated_from",
                  "estimated_margin_pct", "estimated_median_cents", "estimated_listing_count", "match_state"):
        assert field in item


def test_old_rows_get_a_state_from_what_they_had(client):
    run_id = _seed()
    with Session(engine) as s:
        for pid, state in (("1", None), ("2", None), ("4", None), ("5", None)):
            snap = s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.product_id == pid)).one()
            snap.match_state = state
            s.add(snap)
        s.commit()
    got = {i["product"]["id"]: i["match_state"] for i in _get(client).json()["items"]}
    assert got["1"] == "igual" and got["2"] == "similar" and got["5"] == "ninguno" and got["4"] == "diferente"


def test_the_run_carries_the_estimated_and_different_counts(client):
    run_id = _seed()
    with Session(engine) as s:
        price_monitor.recount_run(s, run_id)
        s.commit()
    run = client.get("/api/price-monitor/runs").json()["items"][0]
    assert run["estimated"] == {"verde": 1, "amarillo": 1, "rojo": 1} and run["solo_diferentes"] == 1
    assert run["colors"] == {"verde": 1, "amarillo": 0, "rojo": 0, "sin_dato": 5}
    assert (run["counts"]["ok"], run["counts"]["no_data"]) == (1, 5)


# ─── "Es el mismo" ────────────────────────────────────────────────────────


def _post(client, product_id, ml_id, path="same"):
    return client.post(f"/api/price-monitor/snapshots/{_snap(product_id).id}/{path}", json={"ml_id": ml_id})


def test_it_is_the_same_promotes_a_similar_and_recalculates_the_real_color(client):
    run_id = _seed()
    r = _post(client, "2", "MLA221")
    assert r.status_code == 200 and r.json()["already"] is False
    snap = r.json()["snapshot"]
    assert (snap["ml_status"], snap["match_state"], snap["color"], snap["match_origin"]) == ("ok", "igual", "verde", "web")
    assert (snap["ml_median_cents"], snap["ml_min_cents"], snap["ml_listing_count"]) == (25_000, 25_000, 1)
    assert snap["est_margin_pct"] == pytest.approx((25_000 * 0.87 - 10_000) / 10_000 * 100, abs=0.01)
    assert snap["match_source"] == "manual" and snap["ml_error"] is None
    assert [m["ml_id"] for m in snap["matched_listings"]] == ["MLA221"]
    assert snap["matched_listings"][0]["category"] == "igual" and snap["matched_listings"][0]["reason"]
    assert [m["ml_id"] for m in snap["similar_listings"]] == ["MLA222"] and snap["similar_count"] == 1
    # con idéntico el estimado desaparece (no hay doble color)
    assert (snap["estimated_color"], snap["estimated_median_cents"], snap["estimated_from"]) == (None, None, None)
    with Session(engine) as s:
        [fb] = s.exec(select(MarketMatchFeedback)).all()
        assert (fb.product_id, fb.ml_id, fb.label, fb.actor, fb.category) == ("2", "MLA221", 1, "admin", "similar")
        run = s.get(PriceMonitorRun, run_id)
        assert (run.n_verde, run.n_sin_dato, run.n_ok) == (2, 4, 2)               # el contador real sí cambia
        assert (run.n_est_verde, run.n_est_amarillo, run.n_est_rojo) == (0, 1, 1)   # y el 2 dejó de ser "estimado"
    assert match_feedback_state() == {"2": {"MLA221"}}


def match_feedback_state():
    from app.pricing import match_feedback

    return {p: set(ids) for p, ids in match_feedback.load_promoted().items()}


def test_it_is_the_same_also_works_on_a_different_one(client):
    _seed()
    snap = _post(client, "4", "MLA442").json()["snapshot"]
    assert (snap["ml_status"], snap["ml_median_cents"], snap["match_state"]) == ("ok", 12_000, "igual")
    assert [o["ml_id"] for o in snap["other_listings"]] == ["MLA441"] and snap["other_count"] == 1
    assert snap["color"] in ("verde", "amarillo", "rojo")


def test_promoting_one_more_identical_updates_the_median_and_the_minimum(client):
    _seed()
    _post(client, "2", "MLA221")                      # 25.000
    snap = _post(client, "2", "MLA222").json()["snapshot"]                              # 30.000
    assert (snap["ml_min_cents"], snap["ml_median_cents"], snap["ml_listing_count"]) == (25_000, 27_500, 2)
    assert snap["similar_listings"] == [] and snap["match_state"] == "igual"


def test_a_promoted_publication_without_a_price_is_identical_but_cannot_color(client):
    _seed()
    with Session(engine) as s:
        snap = s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.product_id == "2")).one()
        price_monitor._store_listings(snap, [], [], [_pub("MLA221", "similar", None, est_ok=False)], [],
                                      keep=8, green_min=30.0, yellow_min=10.0)
        s.add(snap)
        s.commit()
    snap = _post(client, "2", "MLA221").json()["snapshot"]
    assert (snap["ml_status"], snap["color"], snap["ml_median_cents"]) == ("no_data", "sin_dato", None)
    assert snap["match_state"] == "igual_sin_precio"
    assert [m["ml_id"] for m in snap["unpriced_listings"]] == ["MLA221"] and snap["matched_listings"] == []
    with Session(engine) as s:
        assert s.exec(select(MarketMatchFeedback)).one().label == 1       # igual queda anotado para la próxima corrida


def test_it_is_the_same_is_idempotent_and_validates(client):
    _seed()
    assert _post(client, "2", "MLA221").json()["already"] is False
    again = _post(client, "2", "MLA221")
    assert again.status_code == 200 and again.json()["already"] is True
    with Session(engine) as s:
        assert len(s.exec(select(MarketMatchFeedback)).all()) == 1
    assert _post(client, "2", "MLA99999999").status_code == 404
    assert _post(client, "2", "../etc").status_code == 400
    assert client.post("/api/price-monitor/snapshots/99999/same", json={"ml_id": "MLA221"}).status_code == 404
    assert client.post(f"/api/price-monitor/snapshots/{_snap('2').id}/same", json={}).status_code == 422


def test_it_is_the_same_needs_a_session():
    anon = TestClient(main_mod.app)
    assert anon.post("/api/price-monitor/snapshots/1/same", json={"ml_id": "MLA101"}).status_code == 401
    assert anon.delete("/api/price-monitor/products/1/feedback/MLA101").status_code == 401


def test_the_actor_is_the_session_user(client):
    _seed()
    client.cookies.set(auth.COOKIE_NAME, auth.issue_session_token("pao@b2box.pro"))
    _post(client, "2", "MLA221")
    with Session(engine) as s:
        assert s.exec(select(MarketMatchFeedback)).one().actor == "pao@b2box.pro"


def test_changing_your_mind_flips_the_one_row_and_the_publication_goes_back_and_forth(client):
    _seed()
    _post(client, "2", "MLA221")                                              # Es el mismo
    snap = _post(client, "2", "MLA221", path="not-same").json()["snapshot"]   # …y no
    assert (snap["ml_status"], snap["match_state"]) == ("no_data", "diferente" if not snap["similar_count"] else "similar")
    assert [o["ml_id"] for o in snap["other_listings"]] == ["MLA221"]
    assert snap["other_listings"][0]["source"] == "manual" and snap["other_listings"][0]["price_cents"] == 25_000
    with Session(engine) as s:
        [fb] = s.exec(select(MarketMatchFeedback)).all()
        assert fb.label == 0                                                  # una sola fila: da vuelta
    snap = _post(client, "2", "MLA221").json()["snapshot"]                    # y otra vez, es el mismo
    assert snap["ml_status"] == "ok" and snap["other_listings"] == []
    with Session(engine) as s:
        assert s.exec(select(MarketMatchFeedback)).one().label == 1


def test_not_the_same_no_longer_throws_the_publication_away(client):
    _seed()
    snap = _post(client, "1", "MLA101", path="not-same").json()["snapshot"]
    assert (snap["ml_status"], snap["color"], snap["match_state"]) == ("no_data", "sin_dato", "diferente")
    [o] = snap["other_listings"]
    assert (o["ml_id"], o["category"], o["source"], o["price_cents"]) == ("MLA101", "diferente", "manual", 25_000)


def test_when_the_last_identical_goes_the_similars_give_the_estimate(client):
    _seed()
    with Session(engine) as s:
        snap = s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.product_id == "1")).one()
        price_monitor._store_listings(snap, json.loads(snap.matched_listings), [], [_pub("MLA111", "similar", 40_000)],
                                      [], keep=8, green_min=30.0, yellow_min=10.0)
        s.add(snap)
        s.commit()
    snap = _post(client, "1", "MLA101", path="not-same").json()["snapshot"]
    assert (snap["color"], snap["estimated_color"], snap["estimated_median_cents"]) == ("sin_dato", "verde", 40_000)


def test_undo_removes_the_mark_whichever_it_was(client):
    _seed()
    _post(client, "2", "MLA221")
    assert client.delete("/api/price-monitor/products/2/feedback/MLA221").json() == {"removed": True}
    assert client.delete("/api/price-monitor/products/2/feedback/MLA221").status_code == 404
    _post(client, "1", "MLA101", path="not-same")
    assert client.delete("/api/price-monitor/products/1/not-same/MLA101").json() == {"removed": True}    # la ruta vieja sigue
    assert match_feedback_state() == {}


def test_the_whole_correction_is_one_transaction(client, monkeypatch):
    _seed()
    sid = _snap("2").id

    def boom(session, run_id):
        raise RuntimeError("se cayó la base")

    monkeypatch.setattr(price_monitor, "recount_run", boom)
    quiet = TestClient(main_mod.app, raise_server_exceptions=False)
    quiet.cookies.set(auth.COOKIE_NAME, auth.issue_session_token("admin"))
    assert quiet.post(f"/api/price-monitor/snapshots/{sid}/same", json={"ml_id": "MLA221"}).status_code == 500
    with Session(engine) as s:
        assert s.exec(select(MarketMatchFeedback)).all() == []
        assert s.get(MarketPriceSnapshot, sid).ml_status == "no_data"


# ─── Deshacer después de cambiar de opinión ───────────────────────────────


def _row(product_id, ml_id):
    with Session(engine) as s:
        return s.exec(select(MarketMatchFeedback).where(
            MarketMatchFeedback.product_id == product_id, MarketMatchFeedback.ml_id == ml_id)).first()


def test_undo_after_changing_your_mind_goes_back_to_the_previous_mark(client):
    _seed()
    _post(client, "2", "MLA221")                                              # Es el mismo   (1)
    _post(client, "2", "MLA221", path="not-same")                             # y no          (0, antes 1)
    row = _row("2", "MLA221")
    assert (row.label, row.previous_label) == (0, 1)
    assert client.delete("/api/price-monitor/products/2/feedback/MLA221").json() == {"removed": False, "restored": 1}
    row = _row("2", "MLA221")
    assert (row.label, row.previous_label) == (1, None)                       # volvió a "Es el mismo", la fila sigue
    assert match_feedback_state() == {"2": {"MLA221"}}
    # un segundo "Deshacer" ya no tiene a qué volver: borra la marca
    assert client.delete("/api/price-monitor/products/2/feedback/MLA221").json() == {"removed": True}
    assert _row("2", "MLA221") is None
    assert client.delete("/api/price-monitor/products/2/feedback/MLA221").status_code == 404


def test_undo_of_a_first_mark_still_deletes_the_row(client):
    _seed()
    _post(client, "1", "MLA101", path="not-same")
    assert _row("1", "MLA101").previous_label is None
    assert client.delete("/api/price-monitor/products/1/not-same/MLA101").json() == {"removed": True}
    assert _row("1", "MLA101") is None


def test_repeating_the_same_mark_does_not_become_a_previous_one(client):
    _seed()
    _post(client, "2", "MLA221")
    _post(client, "2", "MLA221")                                              # idempotente: no cambia nada
    assert (_row("2", "MLA221").label, _row("2", "MLA221").previous_label) == (1, None)


def test_changing_your_mind_twice_remembers_only_the_last_mark(client):
    _seed()
    _post(client, "2", "MLA221")                                              # 1
    _post(client, "2", "MLA221", path="not-same")                             # 0 (antes 1)
    _post(client, "2", "MLA221")                                              # 1 (antes 0)
    row = _row("2", "MLA221")
    assert (row.label, row.previous_label) == (1, 0)
    assert client.delete("/api/price-monitor/products/2/feedback/MLA221").json() == {"removed": False, "restored": 0}
    assert _row("2", "MLA221").label == 0


def test_the_snapshot_flags_which_similars_count_for_the_estimate(client):
    _seed()
    with Session(engine) as s:
        snap = s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.product_id == "2")).one()
        price_monitor._store_listings(
            snap, [], [], [_pub("MLA221", "similar", 25_000, differences=["medida"]),
                           _pub("MLA222", "similar", 30_000, est_ok=False, source="ambiguo", differences=[])],
            [], keep=8, green_min=30.0, yellow_min=10.0)
        s.add(snap)
        s.commit()
    item = next(i for i in _get(client).json()["items"] if i["product"]["id"] == "2")
    assert {m["ml_id"]: m["in_estimate"] for m in item["similar_listings"]} == {"MLA221": True, "MLA222": False}
    assert item["estimated_listing_count"] == 1 and item["estimated_median_cents"] == 25_000
