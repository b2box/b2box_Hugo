"""API de la bandeja (PR2): solo lectura, con sesión del dashboard.

Cubre los criterios 2-5 (contenido de la ficha) y 2-6 («No es el mismo» cierra la ficha en la misma respuesta).
"""

from __future__ import annotations

import json
import os
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app import auth  # noqa: E402
from app.clock import utcnow  # noqa: E402
from app import main as main_mod  # noqa: E402
from app.api import price_review_routes as routes  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.models import MarketStore, PriceReviewItem, StoreMatch  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import price_review as pr  # noqa: E402
from app.vendure.client import VendureClient  # noqa: E402
from tests import review_world as w  # noqa: E402
from tests.review_fixtures import FakeReviewVendure, engine_variant, install  # noqa: E402
from tests.test_price_monitor_routes import _use_settings  # noqa: E402

NOW = w.NOW
BASE = "/api/price-review"
SPECS = {"box_length": 62, "box_width": 48, "box_height": 40, "box_weight": 18}


@pytest.fixture(autouse=True)
def world(monkeypatch):
    w.reset_db()
    w.set_setting("pm_mode", 1)
    _use_settings(monkeypatch)
    fake = FakeReviewVendure()
    install(monkeypatch, fake)
    for i in range(3):
        v = engine_variant(i)
        fake.breakdowns[v["variant"]] = v["breakdowns"]
        fake.variants[v["variant"]] = {
            "id": v["variant"], "name": "", "sku": "", "priceWithTax": v["priceWithTax"], "currencyCode": "ARS",
            "customFields": v["customFields"], "bulkPriceTiers": v["tiers"]}
    return fake


@pytest.fixture
def client() -> TestClient:
    c = TestClient(main_mod.app, raise_server_exceptions=False)
    c.cookies.set(auth.COOKIE_NAME, auth.issue_session_token("pao@b2box.pro"))
    return c


async def open_items(pids: list[str] | dict[str, dict]) -> int:
    """Abre una ficha por producto (racha de 2 noches) y devuelve el id de la corrida de hoy."""
    kw = pids if isinstance(pids, dict) else {p: {} for p in pids}
    prev = w.make_run(started=NOW - timedelta(days=1))
    today = w.make_run()
    for pid, extra in kw.items():
        w.add_snap(prev, pid, flag="firme", data_at=NOW - timedelta(days=1), captured_at=NOW - timedelta(days=1))
        w.add_snap(today, pid, specs=SPECS, **extra)
    await pr.process_run(today, now=NOW)
    return today


# ─── sesión ───────────────────────────────────────────────────────


def test_every_endpoint_needs_a_dashboard_session():
    anon = TestClient(main_mod.app)
    for path in ("/summary", "/items", "/items/1"):
        assert anon.get(BASE + path).status_code == 401


def test_there_is_nothing_to_write_in_this_stage():
    methods = {(r.path, m) for r in routes.router.routes for m in r.methods}
    assert methods == {(BASE + "/summary", "GET"), (BASE + "/items", "GET"), (BASE + "/items/{item_id}", "GET")}


# ─── resumen ──────────────────────────────────────────────────────


async def test_summary_counts_pending_age_and_last_pass(client):
    await open_items(["a", "b", "c"])
    with Session(engine) as s:
        old = s.get(PriceReviewItem, w.items()[0].id)
        old.state_since = w.NOW - timedelta(days=9)
        s.add(old)
        s.add(PriceReviewItem(product_id="z", state="cerrado_auto", closed_at=NOW))
        s.commit()
    d = client.get(BASE + "/summary").json()
    assert d["mode"] == 1 and d["mode_label"] == "propuesta" and d["read_only"] is True
    assert d["counts"]["propuesto"] == 3 and d["counts"]["cerrado_auto"] == 1
    assert d["groups"] == {"para_decidir": 3, "en_proceso": 0, "mantenidos": 0, "cerrados": 1} and d["pending"] == 3
    assert d["oldest_pending_days"] > 8
    lp = d["last_pass"]
    assert lp["new"] == 3 and lp["firm"] == 3 and lp["doubtful"] == 0 and lp["vendure_reads"] > 0
    assert d["params"]["confirm_nights"] == 2


def test_summary_without_any_pass_is_empty_and_honest(client):
    d = client.get(BASE + "/summary").json()
    assert d["last_pass"] is None and d["pending"] == 0 and d["oldest_pending_days"] is None


async def test_summary_says_when_the_last_pass_failed(client):
    run = w.make_run()
    from app.pricing import price_monitor
    price_monitor._update_run(run, review_summary=json.dumps({"error": "RuntimeError: se cayó"}))
    assert "se cayó" in client.get(BASE + "/summary").json()["last_pass_error"]


async def test_summary_mode_2_is_shown_as_propuesta_for_now(client):
    w.set_setting("pm_mode", 2)
    d = client.get(BASE + "/summary").json()
    assert d["mode"] == 2 and d["mode_effective"] == 1 and "propuesta" in d["mode_label"]


# ─── listado ──────────────────────────────────────────────────────


async def test_list_filters_by_tab_cause_and_search(client):
    await open_items({"a": dict(name="Organizador de cocina"), "b": dict(name="Taza térmica"), "c": dict(name="100% algodón_x")})
    with Session(engine) as s:
        rows = {i.product_id: i for i in w.items()}
        rows["a"].probable_cause, rows["b"].probable_cause, rows["c"].probable_cause = "a", "d", "ninguna"
        rows["c"].diag_status = "pendiente"
        for r in rows.values():
            s.add(s.merge(r))
        s.commit()
    items = client.get(BASE + "/items").json()
    assert items["total"] == 3 and {i["product"]["id"] for i in items["items"]} == {"a", "b", "c"}
    assert items["causes"]["a"] == 1 and items["causes"]["d"] == 1 and items["causes"]["ninguna"] == 1
    assert [i["product"]["id"] for i in client.get(BASE + "/items?cause=a").json()["items"]] == ["a"]
    assert [i["product"]["id"] for i in client.get(BASE + "/items?cause=pendiente").json()["items"]] == ["c"]
    assert [i["product"]["id"] for i in client.get(BASE + "/items?q=taza").json()["items"]] == ["b"]
    assert [i["product"]["id"] for i in client.get(BASE + "/items?q=BXb").json()["items"]] == ["b"]
    assert client.get(BASE + "/items?state=en_proceso").json()["total"] == 0
    assert client.get(BASE + "/items?state=cerrados").json()["total"] == 0
    assert client.get(BASE + "/items?state=todas").json()["total"] == 3
    # los comodines del LIKE no son comodines
    assert [i["product"]["id"] for i in client.get(BASE + "/items?q=100%25").json()["items"]] == ["c"]
    assert client.get(BASE + "/items?q=%25").json()["total"] == 1
    assert client.get(BASE + "/items?q=_").json()["total"] == 1 or client.get(BASE + "/items?q=_").json()["total"] == 0


async def test_list_is_paginated_oldest_first_for_open_ones(client):
    await open_items([f"p{i}" for i in range(7)])
    with Session(engine) as s:
        for n, it in enumerate(w.items()):
            it.state_since = utcnow() - timedelta(days=n)
            s.add(s.merge(it))
        s.commit()
    p0 = client.get(BASE + "/items?page=0&page_size=3").json()
    p2 = client.get(BASE + "/items?page=2&page_size=3").json()
    assert p0["total"] == 7 and len(p0["items"]) == 3 and p0["has_more"] is True
    assert len(p2["items"]) == 1 and p2["has_more"] is False
    assert p0["items"][0]["product"]["id"] == "p6" and p0["items"][0]["waiting_days"] >= 5.9    # la que más espera


def test_list_rejects_what_makes_no_sense(client):
    assert client.get(BASE + "/items?state=nada").status_code == 400
    assert client.get(BASE + "/items?cause=z").status_code == 400
    assert client.get(BASE + "/items?page=-1").status_code == 422
    assert client.get(BASE + "/items?page_size=1000").status_code == 422


# ─── 2-5: contenido de la ficha ───────────────────────────────────


async def test_detail_shows_cause_ml_link_breakdown_that_adds_up_and_the_date_and_origin(client):
    await open_items(["a"])
    [item] = w.items()
    d = client.get(f"{BASE}/items/{item.id}").json()
    assert d["state"] == "propuesto" and d["probable_cause"] in list("abcde") + ["ninguna"]
    assert d["cause_summary"], "la ficha tiene que explicar la causa en una oración"
    # al menos un link de ML, https y de un dominio permitido
    links = [m["permalink"] for m in d["snapshot"]["matched_listings"]]
    assert links and all(l.startswith("https://") and "mercadolibre.com" in l for l in links)
    # desglose cuyas partes suman la factura (0,01 USD)
    bd = d["breakdown"]
    assert len(bd["components"]) == 10 and abs(sum(c["usd"] for c in bd["components"]) - bd["factura_usd"]) <= 0.01
    assert [t["min_quantity"] for t in d["tiers"]] == [25, 50, 100] and d["tiers"][0]["price_cents"] > 0
    # fecha y origen del dato
    assert d["data"]["origin"] == "api" and d["data"]["at"].startswith("2026-10-14")
    # diagnóstico a-e completo
    assert [r["code"] for r in d["diagnosis"]["rules"]] == list("abcde") and d["diag_status"] == "completo"
    assert d["events"][0]["kind"] == "propuesto" and d["events"][0]["actor"] == "hugo:auto"
    assert d["decisions_enabled"] is False
    assert d["snapshot"]["product"]["id"] == "a" and "cells" in d["snapshot"]


async def test_detail_sanitizes_what_comes_from_third_parties(client):
    evil = [dict(w.listing(i, p), permalink="javascript:alert(1)", image_url="data:text/html,<script>", title="<img src=x onerror=alert(1)>")
            for i, p in enumerate((900_000, 905_000, 895_000), 1)]
    await open_items({"a": dict(matched=evil, name="<script>alert(1)</script>")})
    [item] = w.items()
    d = client.get(f"{BASE}/items/{item.id}").json()
    for m in d["snapshot"]["matched_listings"]:
        assert not (m["permalink"] or "").startswith("javascript:") and m["image_url"] is None
    assert d["product"]["name"] == "<script>alert(1)</script>"           # texto plano: el front nunca lo renderiza como HTML


async def test_detail_shows_the_store_links_of_the_run(client):
    await open_items(["a"])
    [item] = w.items()
    with Session(engine) as s:
        store = MarketStore(name="Gadnic", base_url="https://www.gadnic.com.ar", platform="tiendanube")
        s.add(store)
        s.commit()
        s.refresh(store)
        s.add(StoreMatch(run_id=item.last_run_id, product_id="a", store_id=store.id, item_id=1, category="igual",
                         auto_category="igual", source="clip", title="Organizador Gadnic",
                         url="https://www.gadnic.com.ar/p/org", price_cents=950_000))
        s.commit()
    d = client.get(f"{BASE}/items/{item.id}").json()
    [(sid, block)] = d["snapshot"]["stores"].items()
    assert block["label"] == "Gadnic" and block["matches"][0]["url"] == "https://www.gadnic.com.ar/p/org"


async def test_detail_404_and_pending_diagnosis_is_not_hidden(client, world):
    assert client.get(BASE + "/items/999999").status_code == 404
    world.http_fail.add("TierBreakdowns")
    await open_items(["a"])
    [item] = w.items()
    d = client.get(f"{BASE}/items/{item.id}").json()
    assert d["diag_status"] == "pendiente" and d["breakdown"] is None and "diag_pendiente" in d["flags"]
    assert [r["code"] for r in d["diagnosis"]["rules"]] == list("abcde")


def test_links_come_only_from_https_templates_with_their_marker():
    assert routes._link("https://admin.example.com/products/{id}", id="7") == "https://admin.example.com/products/7"
    assert routes._link("https://shop.example.com/{slug}", slug="a b/c") == "https://shop.example.com/a%20b%2Fc"
    assert routes._link("http://admin.example.com/{id}", id="7") is None
    assert routes._link("", id="7") is None
    assert routes._link("https://x/{id}/{slug}", id="7") is None           # falta un valor
    assert routes._link("javascript:{id}", id="1") is None


async def test_item_links_use_the_configured_templates(client, monkeypatch):
    cfg = Settings(vendure_api_url="https://example.invalid/admin-api", vendure_admin_product_url="https://admin.b2box.test/products/{id}",
                   storefront_product_url="https://b2box.test/producto/{slug}")
    monkeypatch.setattr(routes, "get_settings", lambda: cfg)
    await open_items(["a"])
    [item] = w.items()
    d = client.get(f"{BASE}/items/{item.id}").json()
    assert d["links"] == {"vendure_admin": "https://admin.b2box.test/products/a",
                          "storefront": "https://b2box.test/producto/producto-a"}


# ─── 2-6: «No es el mismo» cierra la ficha en la misma respuesta ────


async def test_not_the_same_closes_the_ficha_in_the_same_response_and_recounts_the_run(client):
    cheap_two_and_one_dear = [w.listing(1, 800_000), w.listing(2, 810_000), w.listing(3, 1_500_000)]
    today = await open_items({"a": dict(matched=cheap_two_and_one_dear)})
    [item] = w.items()
    snap = w.snap_of(today, "a")
    assert snap.review_flag == "firme"

    # 1.º: sacar la más barata deja 2 publicaciones: sigue siendo rojo (pero ya no firme) y la ficha sigue abierta
    r1 = client.post(f"/api/price-monitor/snapshots/{snap.id}/not-same", json={"ml_id": "MLA1001"})
    assert r1.status_code == 200
    assert r1.json()["review"] == {"item_id": item.id, "state": "propuesto", "closed": False}
    assert w.snap_of(today, "a").review_flag == "dudoso:pocas_publicaciones"
    assert w.items()[0].state == "propuesto"

    # 2.º: sacar otra deja una sola publicación cara → verde: la ficha se cierra en ESTA respuesta
    r2 = client.post(f"/api/price-monitor/snapshots/{snap.id}/not-same", json={"ml_id": "MLA1002"})
    body = r2.json()
    assert r2.status_code == 200 and body["snapshot"]["color"] != "rojo"
    assert body["review"] == {"item_id": item.id, "state": "cerrado_auto", "closed": True}
    [closed] = w.items()
    assert closed.state == "cerrado_auto" and closed.open_key is None and closed.closed_at is not None
    ev = w.events("cerrado_auto")[0]
    assert ev.actor == "pao@b2box.pro" and ev.motive_code == "no_es_el_mismo" and ev.from_state == "propuesto"
    # los contadores de la corrida se recalcularon
    run = w.run_of(today)
    assert run.n_rojo == 0 and run.n_verde + run.n_amarillo + run.n_sin_dato == 1
    assert w.snap_of(today, "a").review_flag is None                      # ya no es rojo
    detail = client.get(f"{BASE}/items/{item.id}").json()
    assert detail["state"] == "cerrado_auto" and {e["kind"] for e in detail["events"]} == {"propuesto", "cerrado_auto"}
    assert client.get(BASE + "/summary").json()["groups"]["cerrados"] == 1


async def test_not_the_same_on_a_product_without_ficha_changes_nothing_in_the_inbox(client):
    run = w.make_run()
    sid = w.add_snap(run, "solo", flag=None)
    r = client.post(f"/api/price-monitor/snapshots/{sid}/not-same", json={"ml_id": "MLA1001"})
    assert r.status_code == 200 and "review" not in r.json() and w.items() == []


async def test_a_correction_on_an_old_snapshot_does_not_touch_the_open_ficha(client):
    today = await open_items(["a"])
    old = w.make_run(started=NOW - timedelta(days=5))
    sid = w.add_snap(old, "a", matched=[w.listing(1, 800_000), w.listing(2, 810_000), w.listing(3, 1_500_000)])
    client.post(f"/api/price-monitor/snapshots/{sid}/not-same", json={"ml_id": "MLA1001"})
    assert w.items()[0].state == "propuesto" and w.items()[0].last_run_id == today


# ─── el filtro de rojos firmes y dudosos en el Semáforo ───────────


async def test_the_semaforo_can_filter_firm_and_doubtful_reds(client):
    prev = w.make_run(started=NOW - timedelta(days=1))
    today = w.make_run()
    w.add_snap(today, "firm")
    w.add_snap(today, "juez", sources=("llm",) * 3)
    w.add_snap(today, "drone", prices=(90_000,) * 3, our=9_000_000)
    w.add_snap(today, "verde", color="verde", our=500_000)
    await pr.process_run(today, now=NOW)

    def ids(q: str) -> set[str]:
        d = client.get(f"/api/price-monitor/snapshots?run_id={today}&{q}").json()
        return {i["product"]["id"] for i in d["items"]}

    assert ids("review=firme") == {"firm"}
    assert ids("review=dudoso") == {"juez", "drone"}
    assert ids("review=dudoso:precio_extremo") == {"drone"}
    assert ids("") == {"firm", "juez", "drone", "verde"}
    assert client.get(f"/api/price-monitor/snapshots?run_id={today}&review=cualquier;cosa").status_code == 400
    item = client.get(f"/api/price-monitor/snapshots?run_id={today}&review=dudoso").json()["items"][0]
    assert item["review_flag"].startswith("dudoso:") and item["data_at"]
