"""El semáforo con las búsquedas web de la Mac de la oficina (`ml_web_result`).

Si hay un resultado fresco de la oficina para el producto se usa en lugar de la búsqueda web del servidor, por el
MISMO filtro (CLIP, nombre, juez, medidas); el color real sigue saliendo solo de idénticos."""

from __future__ import annotations

import json
import os
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.clock import utcnow  # noqa: E402
from app.db.models import MarketMatchFeedback, MlWebResult  # noqa: E402
from app.db.session import engine, init_db  # noqa: E402
from app.pricing import daily_budget, market_ml_web, oficina_ml, price_monitor  # noqa: E402
from tests.test_price_monitor import FakeVendure, _product, _runs, _set, _snaps, world  # noqa: E402,F401
from tests.test_price_monitor_routes import _env, client  # noqa: E402,F401
from tests.test_semaforo_web import PHOTO, _card, _score, _web_page, webw  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _clean_results():
    init_db()
    with Session(engine) as s:
        for row in s.exec(select(MlWebResult)).all():
            s.delete(row)
        s.commit()
    yield


def _wire(ref: str, title: str, price: float | None = 250.0, *, sold: int | None = 500, seller: str = "Tienda Uno",
          currency: str = "ARS", **over) -> dict:
    cand = {"id": ref, "name": title, "image_urls": [PHOTO.format(f"{ref[3:]}-MLA1_012025")],
            "permalink": f"https://articulo.mercadolibre.com.ar/MLA-{ref[3:]}-x", "domain_id": "MLA-ORGANIZERS",
            "price_cents": None if price is None else round(price * 100), "currency": currency, "brand": "",
            "seller": seller, "sold_quantity": sold, "catalog_id": ""}
    cand.update(over)
    return cand


def _store(pid: str, candidates: list[dict], *, ago: timedelta = timedelta(hours=3), status: str = "ok",
           query: str = "producto raro") -> None:
    with Session(engine) as s:
        s.add(MlWebResult(product_id=pid, query=query, fetched_at=(utcnow() - ago).replace(microsecond=0),
                          candidates=json.dumps(candidates), n_candidates=len(candidates), status=status))
        s.commit()


# ─── se usa en lugar de la búsqueda web del servidor ───────────────────────


async def test_a_fresh_result_replaces_the_server_search(webw):
    webw.web.pages["producto-raro"] = _web_page(_card("MLA777", "Producto Raro", 9999.0))      # no tiene que leerse
    _score(webw, "MLA777", 0.95)
    _store("3", [_wire("MLA901", "Producto Raro Premium", 250.0), _wire("MLA902", "Producto Raro", 300.0, seller="Dos")])
    _score(webw, "MLA901", 0.91)
    _score(webw, "MLA902", 0.88)

    result = await price_monitor.run_price_monitor()

    s = _snaps()["3"]
    assert s.ml_status == "ok" and s.match_origin == "oficina" and s.color == "verde"
    assert (s.ml_min_cents, s.ml_median_cents, s.ml_listing_count, s.ml_seller_count) == (25_000, 27_500, 2, 2)
    assert (s.web_state, s.web_via, s.web_searches, s.web_bytes, s.ml_variant) == ("ok", "oficina", 0, 0, None)
    assert s.candidates_count == 2 and s.match_source == "clip"
    assert {m["ml_id"]: m["origin"] for m in json.loads(s.matched_listings)} == {"MLA901": "oficina", "MLA902": "oficina"}
    assert webw.web.calls == [], "el servidor no busca en ML si la oficina ya buscó"
    assert daily_budget.used_today(market_ml_web.WEB_COUNTER_KEY) == 0
    [run] = _runs()
    assert (run.n_oficina_ok, run.oficina_fresh, run.n_web_ok, run.web_searches, run.web_blocked) == (1, 1, 0, 0, 0)
    assert result["counts"]["ok"] == 1


async def test_it_works_with_the_server_web_off(world, monkeypatch):
    """Sin proxy ni navegador en el servidor (lo normal hoy) la oficina igual aporta."""
    FakeVendure.products = [_product("3", "Producto raro")]
    _store("3", [_wire("MLA901", "Producto Raro", 250.0)])
    world.image_scores[PHOTO.format("901-MLA1_012025")] = 0.9
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert s.ml_status == "ok" and s.match_origin == "oficina" and s.ml_median_cents == 25_000
    assert _runs()[0].web_status.startswith("apagada")


async def test_an_igual_from_the_api_wins_and_the_oficina_is_not_even_looked_at(webw):
    FakeVendure.products = [_product("1", "Organizador cocina")]
    _store("1", [_wire("MLA901", "Organizador cocina", 100.0)])
    _score(webw, "MLA901", 0.95)
    await price_monitor.run_price_monitor()
    s = _snaps()["1"]
    assert s.ml_status == "ok" and s.match_origin == "api" and s.web_via is None and s.web_state is None
    assert _runs()[0].n_oficina_ok == 0


async def test_a_stale_result_is_ignored_and_the_server_search_goes_on_as_before(webw):
    webw.web.pages["producto-raro"] = _web_page(_card("MLA777", "Producto Raro", 400.0))
    _score(webw, "MLA777", 0.95)
    _store("3", [_wire("MLA901", "Producto Raro", 250.0)], ago=timedelta(days=7, hours=1))
    _score(webw, "MLA901", 0.95)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert s.match_origin == "web" and s.ml_median_cents == 40_000 and s.web_via is None
    assert len(webw.web.calls) == 1
    assert _runs()[0].oficina_fresh == 0


async def test_a_result_inside_the_ttl_still_counts(webw):
    _store("3", [_wire("MLA901", "Producto Raro", 250.0)], ago=timedelta(days=6, hours=23))
    _score(webw, "MLA901", 0.95)
    await price_monitor.run_price_monitor()
    assert _snaps()["3"].match_origin == "oficina" and webw.web.calls == []


async def test_a_blocked_or_error_report_is_not_a_result(webw):
    webw.web.pages["producto-raro"] = _web_page(_card("MLA777", "Producto Raro", 400.0))
    _score(webw, "MLA777", 0.95)
    _store("3", [], status="blocked")
    await price_monitor.run_price_monitor()
    assert _snaps()["3"].match_origin == "web" and len(webw.web.calls) == 1


async def test_the_newest_ok_or_empty_result_is_the_one_that_counts(webw):
    _store("3", [_wire("MLA801", "Producto Raro viejo", 999.0)], ago=timedelta(days=3))
    _store("3", [_wire("MLA901", "Producto Raro", 250.0)], ago=timedelta(hours=2))
    _store("3", [], ago=timedelta(minutes=5), status="blocked")          # el último intento de la noche fue un bloqueo
    _score(webw, "MLA801", 0.95)
    _score(webw, "MLA901", 0.95)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert s.ml_median_cents == 25_000 and [m["ml_id"] for m in json.loads(s.matched_listings)] == ["MLA901"]


async def test_an_empty_result_means_the_oficina_looked_and_ml_has_nothing(webw):
    webw.web.pages["producto-raro"] = _web_page(_card("MLA777", "Producto Raro", 400.0))
    _score(webw, "MLA777", 0.95)
    _store("3", [], status="empty")
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert s.ml_status == "no_data" and s.color == "sin_dato" and s.match_state == "ninguno"
    assert (s.web_state, s.web_via) == ("empty", "oficina") and "oficina" in s.ml_error
    assert webw.web.calls == []                     # no se le pregunta de nuevo a ML desde el servidor


# ─── mismo filtro que la web ───────────────────────────────────────────────


async def test_the_same_pipeline_classifies_igual_similar_and_different(webw):
    _store("3", [
        _wire("MLA901", "Producto Raro", 250.0),
        _wire("MLA902", "Pack X6 Producto Raro", 900.0, seller="Dos"),          # pack distinto → similar
        _wire("MLA903", "Heladera no frost", 5000.0, seller="Tres"),            # otro producto → diferente
        _wire("MLA904", "Producto Raro", 80.0, seller="Cuatro", sold=2),        # pocas ventas → idéntico sin precio
        _wire("MLA905", "Producto Raro", 70.0, seller="Cinco", currency="USD"),  # dólares → no se comparan
    ])
    for ref, v in {"MLA901": 0.92, "MLA902": 0.95, "MLA903": 0.2, "MLA904": 0.9, "MLA905": 0.9}.items():
        _score(webw, ref, v)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert s.ml_status == "ok" and (s.ml_min_cents, s.ml_median_cents, s.ml_listing_count) == (25_000, 25_000, 1)
    assert [m["ml_id"] for m in json.loads(s.matched_listings)] == ["MLA901"]
    assert {m["ml_id"] for m in json.loads(s.unpriced_listings)} == {"MLA904", "MLA905"}
    assert [m["ml_id"] for m in json.loads(s.similar_listings)] == ["MLA902"]
    assert [m["ml_id"] for m in json.loads(s.other_listings)] == ["MLA903"]
    assert {m["origin"] for key in ("similar_listings", "other_listings", "unpriced_listings", "matched_listings")
            for m in json.loads(getattr(s, key))} == {"oficina"}


async def test_only_similars_give_an_estimate_never_the_real_color(webw):
    _store("3", [_wire("MLA902", "Pack X6 Producto Raro", 900.0)])
    _score(webw, "MLA902", 0.95)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert s.ml_status == "no_data" and s.color == "sin_dato" and s.match_state == "similar"
    assert s.match_origin is None and json.loads(s.similar_listings)[0]["origin"] == "oficina"


async def test_people_corrections_apply_to_oficina_listings_too(webw):
    """(Incluso sobre la plausibilidad de precios: una persona que marcó «Es el mismo» una publicación de 50 veces nuestro
    precio sabe lo que hace; su precio cuenta.)"""
    with Session(engine) as s:
        s.add(MarketMatchFeedback(product_id="3", ml_id="MLA901", label=0, category="igual"))      # «No es el mismo»
        s.add(MarketMatchFeedback(product_id="3", ml_id="MLA903", label=1, category="diferente"))  # «Es el mismo»
        s.commit()
    _store("3", [_wire("MLA901", "Producto Raro", 250.0), _wire("MLA903", "Heladera no frost", 5000.0, seller="Tres")])
    _score(webw, "MLA901", 0.95)
    _score(webw, "MLA903", 0.2)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert [m["ml_id"] for m in json.loads(s.matched_listings)] == ["MLA903"] and s.ml_median_cents == 500_000
    assert [m["ml_id"] for m in json.loads(s.other_listings)] == ["MLA901"]


async def test_oficina_gives_column_by_column_what_the_server_search_would_have(webw, monkeypatch):
    """La misma página, leída por el servidor o traída por la Mac: el semáforo es el mismo salvo el origen (y salvo el chequeo
    de plausibilidad de precios, que es solo de la oficina y tiene sus tests más abajo)."""
    monkeypatch.setattr(price_monitor, "_implausible_price", lambda c, our_price: False)
    cards = [_card("MLA901", "Producto Raro", 250.0), _card("MLA902", "Pack X6 Producto Raro", 900.0, seller="Dos"),
             _card("MLA903", "Heladera no frost", 5000.0, seller="Tres"), _card("MLA904", "Producto Raro", 80.0, sold=2)]
    html_page = _web_page(*cards)
    for ref, v in {"MLA901": 0.92, "MLA902": 0.95, "MLA903": 0.2, "MLA904": 0.9}.items():
        _score(webw, ref, v)
    webw.web.pages["producto-raro"] = html_page
    await price_monitor.run_price_monitor()
    via_server = _snaps()["3"]

    # ahora la Mac "leyó" la misma página y mandó sus candidatos
    parsed = market_ml_web.parse_search(html_page.html, 8)
    with Session(engine) as s:
        for row in s.exec(select(MlWebResult)).all():
            s.delete(row)
        s.commit()
    _store("3", [oficina_ml.candidate_to_wire(c) for c in parsed.candidates])
    webw.web.calls.clear()
    await price_monitor.run_price_monitor()
    via_oficina = _snaps(_runs()[-1].id)["3"]

    assert webw.web.calls == []
    skip = {"id", "run_id", "captured_at", "match_origin", "web_state", "web_via", "web_searches", "web_bytes", "prev_color",
            "matched_listings", "unpriced_listings", "similar_listings", "other_listings", "ml_error"}
    cols = [c.name for c in type(via_server).__table__.columns if c.name not in skip]
    assert {c: getattr(via_oficina, c) for c in cols} == {c: getattr(via_server, c) for c in cols}
    assert (via_server.match_origin, via_oficina.match_origin) == ("web", "oficina")
    for key in ("matched_listings", "unpriced_listings", "similar_listings", "other_listings"):
        a = [{k: v for k, v in m.items() if k != "origin"} for m in json.loads(getattr(via_server, key) or "[]")]
        b = [{k: v for k, v in m.items() if k != "origin"} for m in json.loads(getattr(via_oficina, key) or "[]")]
        assert a == b, key


async def test_hostile_rows_in_the_table_are_cleaned_again_when_read(webw):
    """La tabla no es de fiar más que la entrada: se vuelve a sanear al leer."""
    _store("3", [_wire("MLA901", "Producto Raro", 250.0, permalink="https://evil.com/x",
                       image_urls=["https://evil.com/a.jpg", PHOTO.format("901-MLA1_012025")]),
                 _wire("MLA٣", "Producto Raro", 1.0), {"id": "MLA7", "name": "x\x00\n" * 3000, "price_cents": 10**30}])
    _score(webw, "MLA901", 0.95)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    [m] = json.loads(s.matched_listings)
    assert m["permalink"] == "https://articulo.mercadolibre.com.ar/MLA-901" and m["ml_id"] == "MLA901"
    assert s.candidates_count <= 2


# ─── dashboard ─────────────────────────────────────────────────────────────


async def test_the_dashboard_sees_the_origin_and_the_run_counters(webw, client):
    _store("3", [_wire("MLA901", "Producto Raro", 250.0)])
    _score(webw, "MLA901", 0.95)
    await price_monitor.run_price_monitor()
    items = client.get("/api/price-monitor/snapshots?origin=oficina").json()["items"]
    assert [i["product"]["id"] for i in items] == ["3"] and items[0]["match_origin"] == "oficina"
    assert items[0]["web_via"] == "oficina" and items[0]["matched_listings"][0]["origin"] == "oficina"
    assert client.get("/api/price-monitor/snapshots?origin=web").json()["items"] == []
    assert client.get("/api/price-monitor/snapshots?origin=otra").status_code == 400
    run = client.get("/api/price-monitor/runs").json()["items"][0]
    assert run["oficina"] == {"fresh": 1, "n_ok": 1}
    summary = client.get("/api/price-monitor/summary").json()["oficina"]
    assert summary["fresh_products"] == 1 and summary["last_24h"]["ok"] == 1 and summary["ttl_days"] == 7


def test_the_status_of_the_card_when_nothing_ever_came_in():
    with Session(engine) as s:
        for row in s.exec(select(MlWebResult)).all():
            s.delete(row)
        s.commit()
    status = oficina_ml.status()
    assert status["fresh_products"] == 0 and status["last_received_at"] is None
    assert status["last_24h"] == {"ok": 0, "empty": 0, "blocked": 0, "error": 0}


async def test_a_failure_reading_the_oficina_table_does_not_take_the_run_down(webw, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("tabla rota")

    monkeypatch.setattr(oficina_ml, "load_fresh", boom)
    webw.web.pages["producto-raro"] = _web_page(_card("MLA777", "Producto Raro", 400.0))
    _score(webw, "MLA777", 0.95)
    await price_monitor.run_price_monitor()
    assert _runs()[0].status == "ok" and _snaps()["3"].match_origin == "web"       # siguió como antes


def test_the_health_card_survives_a_broken_oficina_status(client, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("tabla rota")

    monkeypatch.setattr(oficina_ml, "status", boom)
    r = client.get("/api/price-monitor/summary")
    assert r.status_code == 200 and r.json()["oficina"]["enabled"] is False


# ─── plausibilidad: una key robada no puede fijar el color ─────────────────


async def test_an_oficina_price_ten_times_off_ours_is_doubtful_and_never_counts_for_the_color(webw):
    """Nuestro precio es ARS 100. Un «idéntico» con el título nuestro, una foto que se parece y ARS 1.500 (o ARS 5) no mueve nada."""
    _store("3", [_wire("MLA901", "Producto Raro", 1500.0), _wire("MLA902", "Producto Raro", 5.0, seller="Dos")])
    _score(webw, "MLA901", 0.95)
    _score(webw, "MLA902", 0.95)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert s.ml_status == "no_data" and s.color == "sin_dato" and s.ml_median_cents is None and s.match_origin is None
    assert "precio dudoso" in s.ml_error
    unpriced = {m["ml_id"]: m for m in json.loads(s.unpriced_listings)}
    assert set(unpriced) == {"MLA901", "MLA902"}                       # se ven, con el aviso, pero sin precio que cuente
    assert all(price_monitor.PRICE_DOUBT_NOTE in m["notes"] for m in unpriced.values())
    assert s.match_state == "igual_sin_precio" and s.estimated_color is None


@pytest.mark.parametrize("price, counts", [(1000.0, True), (1000.01, False), (10.0, True), (9.99, False), (250.0, True)])
async def test_the_plausibility_edges_are_the_ones_of_the_stores(webw, price, counts):
    _store("3", [_wire("MLA901", "Producto Raro", price)])
    _score(webw, "MLA901", 0.95)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert (s.ml_status == "ok") is counts
    if counts:
        assert s.match_origin == "oficina" and s.ml_median_cents == round(price * 100)


async def test_a_doubtful_listing_does_not_move_the_median_of_the_good_ones(webw):
    _store("3", [_wire("MLA901", "Producto Raro", 250.0), _wire("MLA902", "Producto Raro", 9000.0, seller="Dos"),
                 _wire("MLA903", "Producto Raro", 300.0, seller="Tres")])
    for ref in ("MLA901", "MLA902", "MLA903"):
        _score(webw, ref, 0.95)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert s.ml_status == "ok" and (s.ml_min_cents, s.ml_median_cents, s.ml_listing_count) == (25_000, 27_500, 2)
    assert [m["ml_id"] for m in json.loads(s.unpriced_listings)] == ["MLA902"]


async def test_a_doubtful_similar_does_not_feed_the_estimated_color(webw):
    _store("3", [_wire("MLA902", "Pack X6 Producto Raro", 9000.0)])
    _score(webw, "MLA902", 0.95)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert s.ml_status == "no_data" and s.estimated_color is None
    [sim] = json.loads(s.similar_listings)
    assert sim["est_ok"] is False and price_monitor.PRICE_DOUBT_NOTE in sim["notes"]


async def test_the_server_web_is_not_subject_to_this_check_and_works_as_before(webw):
    """Solo la oficina (la única fuente que llega por un canal con key) pasa por el chequeo de plausibilidad."""
    webw.web.pages["producto-raro"] = _web_page(_card("MLA901", "Producto Raro", 1500.0))
    _score(webw, "MLA901", 0.95)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert s.ml_status == "ok" and s.match_origin == "web" and s.ml_median_cents == 150_000
