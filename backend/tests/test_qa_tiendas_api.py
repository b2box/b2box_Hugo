"""QA independiente de feat/semaforo-tiendas, criterio 5: dashboard, API y Configuración → Tiendas.

Lo que los tests del developer no cubren: qué pasa con las fichas SIN STOCK, las direcciones de tienda raras, los
nombres repetidos y una tienda que se borra mientras se está indexando.
"""

from __future__ import annotations

import os
import random

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import runtime  # noqa: E402
from app.db.models import MarketStore, StoreCatalogItem  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import daily_budget, price_monitor, store_catalog  # noqa: E402
from tests import store_fixtures as fx  # noqa: E402
from tests.store_fixtures import Clock, FakeSite, store_db  # noqa: E402,F401  (fixture)
from tests.test_price_monitor_routes import _env, client  # noqa: E402,F401
from tests.test_qa_tiendas_color import CP, GD, _expected_color, _snap, add_item, sw, webw  # noqa: E402,F401
from tests.test_store_catalog import _clock, add_store, tn_site  # noqa: E402,F401
from tests.test_price_monitor import world  # noqa: E402,F401


# ─── fichas sin stock ───────────────────────────────────────────────────────


async def test_an_out_of_stock_identical_is_shown_but_neither_wins_cheapest_outside_nor_counts_for_the_color(sw, client):
    """Decisión de Nico (08-oct-2026): lo sin stock se muestra con la etiqueta «sin stock» (en la celda y en el
    detalle) pero NO cuenta para decir que afuera es más barato ni para la mediana del color."""
    runtime.set_value("pm_stores_affect_color", 1)
    add_item(GD, "gd-agotado", "Organizador de cocina", 5_000, 0.95, stock=0)
    add_item(CP, "cp-ok", "Organizador de cocina plegable", 22_000, 0.95, stock=7)
    await price_monitor.run_price_monitor()
    body = client.get("/api/price-monitor/snapshots").json()
    [item] = [i for i in body["items"] if i["product"]["id"] == "1"]
    gd_key = next(k for k in item["cells"] if k != "ml" and item["cells"][k]["label"] == "Gadnic")
    assert item["cells"][gd_key]["price_cents"] == 5_000 and item["cells"][gd_key]["stock"] == 0, "se ve, con su stock"
    gd_matches = next(v for v in item["stores"].values() if v["label"] == "Gadnic")["matches"]
    assert [(m["stock"], m["category"]) for m in gd_matches] == [(0, "igual")]
    # «Más barato afuera»: el agotado de 5.000 no gana; gana el mínimo de ML (12.000)
    assert (item["cheapest_outside"]["label"], item["cheapest_outside"]["price_cents"]) == ("Mercado Libre", 12_000)
    assert item["cheapest_outside"]["out_of_stock"] is False
    # …ni cuenta para el color: la mediana de ML + Casa Perfecta (12.000, 14.000, 22.000), sin el agotado
    assert _snap().price_basis == "ml+tiendas"
    assert _snap().color == _expected_color([12_000, 14_000, 22_000])


async def test_when_the_only_cheaper_option_is_out_of_stock_cheapest_outside_shows_it_labelled(sw, client):
    from sqlmodel import update

    runtime.set_value("pm_stores_affect_color", 1)
    add_item(GD, "gd-agotado", "Organizador de cocina", 5_000, 0.95, stock=0)
    await price_monitor.run_price_monitor()
    with Session(engine) as s:                                  # ML sin precio: lo único idéntico de afuera está agotado
        from app.db.models import MarketPriceSnapshot

        s.execute(update(MarketPriceSnapshot).values(ml_status="no_data", ml_min_cents=None))
        s.commit()
    [item] = [i for i in client.get("/api/price-monitor/snapshots").json()["items"] if i["product"]["id"] == "1"]
    assert item["cheapest_outside"]["price_cents"] == 5_000 and item["cheapest_outside"]["out_of_stock"] is True


async def test_an_identical_confirmed_only_by_the_judge_is_shown_as_identical_but_does_not_paint_the_color(sw, client, monkeypatch):
    """Mitigación del riesgo de L1: el juez IA solo (que lee texto de la tienda) no alcanza para que un precio de
    tienda cambie el color. La foto + el nombre, las medidas o una persona sí."""
    from app.pricing import market_judge

    runtime.set_value("pm_stores_affect_color", 1)
    runtime.set_value("pm_vision_max_calls", 50)
    monkeypatch.setattr(market_judge, "enabled", lambda: True)
    monkeypatch.setattr(market_judge, "make_client", lambda: None)

    async def judge(our_name, our_images, candidates, *, max_calls, on_reserve=None,
                    counter_key=market_judge.LLM_COUNTER_KEY, **kw):
        await daily_budget.reserve_async(counter_key, max_calls, None, on_reserve)
        return market_judge.JudgeResult(verdicts={c.ml_id: market_judge.JudgeVerdict(c.ml_id, True, 0.95, "mismo producto")
                                                  for c in candidates})

    monkeypatch.setattr(market_judge, "judge", judge)
    add_item(GD, "gd-juez", "Organizador de cocina", 3_000, 0.60)           # banda ambigua: lo decide el juez
    add_item(CP, "cp-reglas", "Organizador de cocina", 22_000, 0.95)         # foto + nombre
    await price_monitor.run_price_monitor()
    [item] = [i for i in client.get("/api/price-monitor/snapshots").json()["items"] if i["product"]["id"] == "1"]
    gd = next(v for v in item["stores"].values() if v["label"] == "Gadnic")["matches"]
    assert [(m["category"], m["source"]) for m in gd] == [("igual", "llm")], "se ve como idéntico"
    assert _snap().color == _expected_color([12_000, 14_000, 22_000]), "pero su precio (3.000) no mueve el color"


# ─── Configuración → Tiendas ────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1", "169.254.169.254", "10.0.0.5", "localhost", "[::1]", "metadata", "intranet"])
def test_a_store_pointing_at_an_ip_or_an_internal_name_is_refused_when_it_is_saved(store_db, client, host):
    r = client.post("/api/stores", json={"name": f"Interna {host}", "base_url": f"https://{host}", "platform": "tiendanube"})
    assert r.status_code == 422, r.text


async def test_a_store_whose_name_resolves_to_a_private_ip_is_saved_but_never_fetched(store_db, client, monkeypatch):
    from app import net_guard

    monkeypatch.setattr(net_guard.socket, "getaddrinfo", lambda host, *a, **kw: [(0, 0, 0, "", ("10.0.0.5", 0))])
    r = client.post("/api/stores", json={"name": "Rebind", "base_url": "https://rebind.example.com", "platform": "tiendanube"})
    assert r.status_code == 201
    report = await store_catalog.index_store(r.json()["id"], delay=(0.0, 0.0))
    assert report.status == "aborted" and "SsrfBlocked" in report.message and report.fetched == 0
    with Session(engine) as s:
        assert s.exec(select(StoreCatalogItem)).all() == []


def test_two_store_names_that_differ_only_in_case_are_the_same_store(store_db, client):
    store_catalog.seed_default_stores()
    r = client.post("/api/stores", json={"name": "gadnic", "base_url": "https://www.gadnic2.com.ar", "platform": "jsonld_sitemap"})
    assert r.status_code == 409
    r = client.post("/api/stores", json={"name": "GADNIC ", "base_url": "https://www.gadnic3.com.ar", "platform": "jsonld_sitemap"})
    assert r.status_code == 409


async def test_a_disabled_store_is_skipped_by_the_night_job_and_the_top_up(store_db):
    sid = add_store()
    with Session(engine) as s:
        s.get(MarketStore, sid).enabled = False
        s.commit()
    site = tn_site(["a", "b"])
    assert await store_catalog.index_all(get=site.get, sleep=site.sleep, delay=(0.0, 0.0)) == []
    assert await store_catalog.topup_for_run(15, get=site.get, sleep=site.sleep, delay=(0.0, 0.0)) == []
    assert site.requests == []


async def test_deleting_a_store_while_it_is_being_indexed_stops_the_pass(store_db):
    sid = add_store()
    site = tn_site(["a", "b", "c"])
    site.on_request = lambda url: store_catalog.delete_store(sid) if url.endswith("sitemap.xml") else None
    clock = Clock()
    clock.attach(site)
    await store_catalog.index_store(sid, get=site.get, sleep=site.sleep, rng=random.Random(1), monotonic=clock, delay=(0.0, 0.0))
    assert site.fetched_pages() == []
    with Session(engine) as s:
        assert s.exec(select(StoreCatalogItem)).all() == []
