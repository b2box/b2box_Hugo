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
from app.pricing import price_monitor, store_catalog  # noqa: E402
from tests import store_fixtures as fx  # noqa: E402
from tests.store_fixtures import Clock, FakeSite, store_db  # noqa: E402,F401  (fixture)
from tests.test_price_monitor_routes import _env, client  # noqa: E402,F401
from tests.test_qa_tiendas_color import CP, GD, _expected_color, _snap, add_item, sw, webw  # noqa: E402,F401
from tests.test_store_catalog import _clock, add_store, tn_site  # noqa: E402,F401
from tests.test_price_monitor import world  # noqa: E402,F401


# ─── fichas sin stock ───────────────────────────────────────────────────────


async def test_an_out_of_stock_identical_still_wins_cheapest_outside_and_still_counts_for_the_color(sw, client):
    """Comportamiento actual (a decidir por Nico): el stock NO se mira ni en «Más barato afuera» ni en el color. La
    ficha sin stock se ve con «sin stock» solo en el detalle; en la celda y en «Más barato afuera» se ve como cualquier otra."""
    runtime.set_value("pm_stores_affect_color", 1)
    add_item(GD, "gd-agotado", "Organizador de cocina", 5_000, 0.95, stock=0)
    add_item(CP, "cp-ok", "Organizador de cocina plegable", 22_000, 0.95, stock=7)
    await price_monitor.run_price_monitor()
    body = client.get("/api/price-monitor/snapshots").json()
    [item] = [i for i in body["items"] if i["product"]["id"] == "1"]
    assert (item["cheapest_outside"]["label"], item["cheapest_outside"]["price_cents"]) == ("Gadnic", 5_000)
    assert "stock" not in item["cheapest_outside"]
    gd_matches = next(v for v in item["stores"].values() if v["label"] == "Gadnic")["matches"]
    assert [(m["stock"], m["category"]) for m in gd_matches] == [(0, "igual")]
    assert item["cells"][next(k for k in item["cells"] if k != "ml" and item["cells"][k]["label"] == "Gadnic")]["price_cents"] == 5_000
    # …y cuenta para el color: la mediana con el agotado (12.000, 14.000, 5.000, 22.000) no es la de ML sola
    assert _snap().price_basis == "ml+tiendas"
    assert _snap().color == _expected_color([12_000, 14_000, 5_000, 22_000])


# ─── Configuración → Tiendas ────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1", "169.254.169.254", "10.0.0.5"])
async def test_a_store_pointing_at_a_private_ip_can_be_saved_but_is_never_fetched(store_db, client, host):
    r = client.post("/api/stores", json={"name": f"Interna {host}", "base_url": f"https://{host}", "platform": "tiendanube"})
    assert r.status_code == 201, "si esto pasa a ser 422 mejor: este test se actualiza"
    sid = r.json()["id"]
    report = await store_catalog.index_store(sid, delay=(0.0, 0.0))
    assert report.status == "aborted" and "SsrfBlocked" in report.message and report.fetched == 0
    with Session(engine) as s:
        assert s.exec(select(StoreCatalogItem)).all() == []


@pytest.mark.xfail(strict=True, reason="Nit: «Gadnic» y «gadnic» pasan como tiendas distintas (el único es sensible a mayúsculas) y salen dos "
                                       "columnas casi iguales en el dashboard")
def test_two_store_names_that_differ_only_in_case_are_the_same_store(store_db, client):
    store_catalog.seed_default_stores()
    r = client.post("/api/stores", json={"name": "gadnic", "base_url": "https://www.gadnic2.com.ar", "platform": "jsonld_sitemap"})
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


@pytest.mark.xfail(strict=True, reason="Nit: borrar (o apagar) una tienda mientras se la indexa no frena la pasada en curso: sigue pidiéndole "
                                       "páginas a la tienda borrada y deja filas huérfanas en store_catalog_item")
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
