"""Endurecimiento del indexador y de la carga de tiendas: tiempos máximos por pedido, cupo antes de
bajar nada, una ficha rara que no frena a la tienda, tienda caída sin marcar todo como muerto,
dominios de foto controlados, nombres y sitios únicos y borrado en caliente."""

from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import net_guard, runtime  # noqa: E402
from app.db.models import ImageEmbedCache, MarketStore, StoreCatalogItem, StoreMatch  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import daily_budget, store_catalog, store_match, store_urls  # noqa: E402
from tests import store_fixtures as fx  # noqa: E402
from tests.store_fixtures import Clock, FakeSite, store_db  # noqa: E402,F401  (fixture)
from tests.test_price_monitor_routes import _env, client  # noqa: E402,F401
from tests.test_store_catalog import _clock, add_store, items, run_index, tn_product, tn_site  # noqa: E402,F401


# ─── tiempos máximos por pedido ──────────────────────────────────────────────


async def test_a_page_that_never_finishes_is_cut_by_the_request_deadline(store_db, monkeypatch):
    monkeypatch.setattr(store_catalog, "PAGE_TIMEOUT_S", 0.05)
    sid = add_store()
    site = tn_site(["a", "b", "c"])
    hang_url = tn_product("b")[0]
    real_get = site.get

    async def get(url, **kw):
        if url == hang_url:
            await asyncio.sleep(30)                 # un servidor que gotea un byte cada 20 s
        return await real_get(url, **kw)

    clock = Clock()
    clock.attach(site)
    report = await store_catalog.index_store(sid, get=get, sleep=site.sleep, rng=random.Random(1), monotonic=clock,
                                             delay=(0.0, 0.0))
    assert report.status == "ok" and report.ok == 2 and report.transient == 1
    assert items(sid)[hang_url].fails == 0, "un pedido colgado no es un fallo de la ficha"


async def test_a_sitemap_that_never_finishes_aborts_the_pass_instead_of_hanging_it(store_db, monkeypatch):
    monkeypatch.setattr(store_catalog, "SITEMAP_TIMEOUT_S", 0.05)
    sid = add_store()
    site = tn_site(["a"])
    real_get = site.get

    async def get(url, **kw):
        if url.endswith("sitemap.xml"):
            await asyncio.sleep(30)
        return await real_get(url, **kw)

    report = await store_catalog.index_store(sid, get=get, sleep=site.sleep, rng=random.Random(1), monotonic=Clock(),
                                             delay=(0.0, 0.0))
    assert report.status == "aborted" and report.fetched == 0 and "TimeoutError" in report.message


async def test_robots_that_never_finishes_aborts_the_pass(store_db, monkeypatch):
    monkeypatch.setattr(store_catalog, "ROBOTS_TIMEOUT_S", 0.05)
    sid = add_store()
    site = tn_site(["a"])

    async def get(url, **kw):
        await asyncio.sleep(30)

    report = await store_catalog.index_store(sid, get=get, sleep=site.sleep, rng=random.Random(1), monotonic=Clock(),
                                             delay=(0.0, 0.0))
    assert report.status == "aborted" and "robots.txt" in report.message


async def test_the_top_up_cannot_hold_the_run_longer_than_its_budget(store_db, monkeypatch):
    runtime.set_value("pm_stores_topup_minutes", 1)

    async def never(**kw):
        await asyncio.sleep(30)

    monkeypatch.setattr(store_catalog, "index_all", never)
    monkeypatch.setattr(store_match, "_topup_budget_s", lambda minutes: 0.05)
    add_store()
    t0 = asyncio.get_running_loop().time()
    await store_match.prepare()
    assert asyncio.get_running_loop().time() - t0 < 2.0
    runtime.invalidate()


# ─── cupo y cortes ───────────────────────────────────────────────────────────


async def test_with_the_daily_cap_spent_nothing_is_requested_not_even_robots_or_the_sitemap(store_db):
    sid = add_store(max_pages_per_day=2)
    site = tn_site(["a", "b", "c"])
    await run_index(sid, site)
    site.requests.clear()
    again = await run_index(sid, site)
    assert again.fetched == 0 and "tope diario" in again.message
    assert site.requests == [], "ni robots.txt ni sitemaps: cada pasada bajaba hasta 9 sitemaps de 25 MB para nada"


async def test_a_store_deleted_or_switched_off_while_being_read_stops_the_pass(store_db):
    sid = add_store()
    site = tn_site([f"p{i}" for i in range(6)])
    seen = []

    def on_request(url):
        if not url.endswith((".xml", "robots.txt")):
            seen.append(url)
            if len(seen) == 2:
                with Session(engine) as s:
                    row = s.get(MarketStore, sid)
                    row.enabled = False
                    s.add(row)
                    s.commit()

    site.on_request = on_request
    report = await run_index(sid, site)
    assert report.status == "aborted" and "se apagó" in report.message
    assert len(site.fetched_pages()) == 2


# ─── una ficha rara no frena a la tienda ─────────────────────────────────────


async def test_a_page_that_cannot_be_saved_is_marked_read_and_the_pass_continues(store_db, monkeypatch):
    sid = add_store()
    site = tn_site(["a", "rara", "c"])
    rare = tn_product("rara")[0]
    real = store_catalog._save_outcome

    def flaky(item_id, out, now):
        with Session(engine) as s:
            if s.get(StoreCatalogItem, item_id).url == rare and out.kind == store_catalog.OK:
                raise RuntimeError("DataError simulado")
        return real(item_id, out, now)

    monkeypatch.setattr(store_catalog, "_save_outcome", flaky)
    report = await run_index(sid, site)
    got = items(sid)
    assert report.status == "ok" and report.ok == 3, "las tres se leyeron; una no se pudo guardar"
    assert got[tn_product("a")[0]].title and got[tn_product("c")[0]].title
    assert got[rare].last_checked_at is not None and got[rare].fail_reason == "no se pudo guardar la ficha"


async def test_values_that_do_not_fit_the_columns_are_bounded_before_saving(store_db):
    sid = add_store()
    url = f"{fx.CP}/productos/rara/"
    page = fx.tiendanube_page(url, "Producto " + "x" * 400, [fx.variant(5000, stock=999_999_999, option="a", image=""),
                                                              fx.variant(5000, stock=999_999_999, option="b", image="")],
                              og_image="https://acdn-us.mitiendanube.com/stores/001/" + "a" * 600 + ".webp")
    import re

    page = re.sub(r'"image": "[^"]*",\n', "", page)
    site = FakeSite()
    site.add(f"{fx.CP}/robots.txt", "User-agent: *\n")
    site.add(f"{fx.CP}/sitemap.xml", fx.urlset(url))
    site.add(url, page)
    report = await run_index(sid, site)
    row = items(sid)[url]
    assert report.ok == 1 and len(row.title) == 300
    assert row.stock == store_catalog.store_parse.MAX_STOCK
    assert row.image_url is None, "una foto de más de 500 caracteres no cabe en la columna: se descarta"


def test_links_and_photos_longer_than_their_columns_are_refused():
    assert store_urls.safe_link(f"{fx.CP}/productos/" + "a" * 600, fx.CP) == ""
    assert store_urls.safe_image("https://acdn-us.mitiendanube.com/" + "a" * 600, ("acdn*.mitiendanube.com",)) is None


# ─── una tienda caída no se marca muerta de golpe ────────────────────────────


async def test_a_store_answering_5xx_everywhere_is_cut_after_a_streak_and_nothing_is_marked_dead(store_db):
    sid = add_store()
    slugs = [f"p{i}" for i in range(60)]
    site = tn_site(slugs)
    for slug in slugs:
        site.add(tn_product(slug)[0], "<html>Inconvenientes con el servidor</html>", status=500)
    report = await run_index(sid, site)
    assert report.status == "aborted" and report.fetched == store_catalog.OUTAGE_STREAK
    assert report.health == "caida" and "parece caída" in report.message and report.newly_dead == 0
    assert all(not r.dead for r in items(sid).values())
    with Session(engine) as s:
        assert s.get(MarketStore, sid).health == "caida"
    status = next(x for x in store_catalog.index_status() if x["id"] == sid)
    assert status["health"] == "caida" and status["errors_5xx"] == store_catalog.OUTAGE_STREAK
    assert status["failing"] == store_catalog.OUTAGE_STREAK


async def test_dead_pages_in_a_store_that_still_works_do_not_look_like_an_outage(store_db, _clock):
    """Gadnic tiene la mitad de las fichas muertas: 25 fallos seguidos en un sitio que dio fichas bien esta
    semana son un bloque de muertas, no una caída."""
    sid = add_store(refresh_days=7)
    good = tn_site(["viva"])
    await run_index(sid, good)
    _clock["now"] += timedelta(days=1)
    slugs = [f"m{i}" for i in range(40)]
    site = tn_site(slugs + ["viva2"])
    for slug in slugs:
        site.add(tn_product(slug)[0], "boom", status=500)
    report = await run_index(sid, site)
    assert report.status == "ok" and report.fetched == 41 and report.ok == 1
    assert report.health == "degradada"


async def test_a_healthy_pass_leaves_the_store_ok_and_clears_a_previous_outage(store_db):
    sid = add_store()
    with Session(engine) as s:
        row = s.get(MarketStore, sid)
        row.health = "caida"
        s.add(row)
        s.commit()
    await run_index(sid, tn_site(["a", "b"]))
    assert next(x for x in store_catalog.index_status() if x["id"] == sid)["health"] == "ok"


# ─── redirects de robots y sitemap ───────────────────────────────────────────


async def test_a_sitemap_or_robots_redirecting_off_site_is_not_followed(store_db, monkeypatch):
    sid = add_store()
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(f"{request.url.host}{request.url.path}")
        if request.url.host == "evil.example.com":
            return httpx.Response(200, text="User-agent: *\n")
        return httpx.Response(301, headers={"Location": "https://evil.example.com/robots.txt"})

    real = httpx.AsyncClient
    monkeypatch.setattr(net_guard.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(net_guard, "assert_public_url", lambda url: None)
    monkeypatch.setattr(net_guard, "assert_peer_public", lambda resp: None)
    report = await store_catalog.index_store(sid, rng=random.Random(1), monotonic=Clock(), delay=(0.0, 0.0),
                                             sleep=lambda s: asyncio.sleep(0))
    assert report.status == "aborted" and "RedirectBlocked" in report.message
    assert not any(h.startswith("evil.example.com") for h in hits), "no se le pidió nada al sitio ajeno"


# ─── memoria: el índice de sitemaps tiene tope total ─────────────────────────


async def test_the_urls_of_the_child_sitemaps_are_capped_while_they_are_read(store_db, monkeypatch):
    monkeypatch.setattr(store_catalog, "MAX_STORE_URLS", 50)
    gd = fx.GD
    sid = add_store(name="Gadnic", base_url=gd, platform="jsonld_sitemap")
    site = FakeSite()
    site.add(f"{gd}/robots.txt", "User-agent: *\nDisallow: /*?\n")
    site.add(f"{gd}/sitemap.xml", fx.sitemap_index(*[f"{gd}/sitemap/product-pages-{i}.xml" for i in range(4)]))
    for i in range(4):
        site.add(f"{gd}/sitemap/product-pages-{i}.xml", fx.urlset(*[f"{gd}/cat/p{i}-{n}" for n in range(40)]))
    report = await run_index(sid, site, max_pages=1)
    assert report.sitemap_urls == 50 and len(items(sid)) == 50


# ─── Configuración → Tiendas: dominios, nombres, sitios y tope ──────────────


def _post(client, **over):
    body = {"name": "Nueva", "base_url": "https://www.nueva.com.ar", "platform": "tiendanube"}
    body.update(over)
    return client.post("/api/stores", json=body)


@pytest.fixture
def seeded(_env, store_db):
    """Las dos tiendas de siempre y la sesión del dashboard, para los tests de la API."""
    store_catalog.seed_default_stores()
    runtime.invalidate()
    yield
    store_urls.set_allowed_image_hosts([])


@pytest.mark.parametrize("image_hosts", ["s3.amazonaws.com", "storage.googleapis.com", "raw.githubusercontent.com",
                                         "*.mitiendanube.com", "otro-dominio.com", "*.com.ar", "com.ar", "abc*.com.ar",
                                         "cdn.nueva.com.ar.evil.com"])
@pytest.mark.usefixtures("seeded")
def test_a_person_cannot_add_arbitrary_photo_domains(client, image_hosts):
    r = _post(client, image_hosts=image_hosts)
    assert r.status_code == 422, r.text
    assert "dominio" in r.json()["detail"]


@pytest.mark.parametrize("image_hosts", ["nueva.com.ar", "cdn.nueva.com.ar", "*.nueva.com.ar", "acdn*.mitiendanube.com",
                                         "*.bidcom.com.ar"])
@pytest.mark.usefixtures("seeded")
def test_a_person_can_add_the_store_own_domain_the_platform_cdn_and_the_trusted_list(client, image_hosts):
    r = _post(client, image_hosts=image_hosts)
    assert r.status_code == 201, r.text


@pytest.mark.usefixtures("seeded")
def test_the_trusted_list_is_an_admin_setting_not_something_a_person_types(client, monkeypatch):
    from app.config import Settings

    base = dict(vendure_api_url="https://example.invalid/admin-api")
    monkeypatch.setattr(store_catalog, "get_settings", lambda: Settings(**base, store_trusted_image_hosts="cdn.otra.com"))
    assert _post(client, name="A", base_url="https://a.com.ar", image_hosts="cdn.otra.com").status_code == 201
    assert _post(client, name="B", base_url="https://b.com.ar", image_hosts="*.bidcom.com.ar").status_code == 422


@pytest.mark.parametrize("base_url", ["https://com.ar", "https://github.io", "https://s3.amazonaws.com", "https://x",
                                      "https://" + ".".join(["b" * 60] * 4) + ".com", "https://1.2.3.4", "https://mitiendanube.com",
                                      "https://www.com.ar"])
@pytest.mark.usefixtures("seeded")
def test_a_store_address_must_be_a_common_domain_not_a_public_suffix_or_a_platform(client, base_url):
    r = _post(client, base_url=base_url)
    assert r.status_code == 422, (base_url, r.status_code)


@pytest.mark.usefixtures("seeded")
def test_two_stores_cannot_share_the_same_site(client):
    assert _post(client, name="Uno", base_url="https://www.misitio.com.ar").status_code == 201
    r = _post(client, name="Dos", base_url="https://misitio.com.ar")
    assert r.status_code == 409 and "dirección" in r.json()["detail"]
    other = _post(client, name="Tres", base_url="https://www.otro.com.ar").json()["id"]
    assert client.put(f"/api/stores/{other}", json={"base_url": "https://www.misitio.com.ar"}).status_code == 409


@pytest.mark.usefixtures("seeded")
def test_the_number_of_stores_is_capped(client, monkeypatch):
    from app.config import Settings

    monkeypatch.setattr(store_catalog, "get_settings", lambda: Settings(vendure_api_url="https://example.invalid/admin-api",
                                                                       store_max_stores=3))
    assert _post(client, name="Tres", base_url="https://tres.com.ar").status_code == 201      # ya hay 2 sembradas
    r = _post(client, name="Cuatro", base_url="https://cuatro.com.ar")
    assert r.status_code == 409 and "tope" in r.json()["detail"]


@pytest.mark.usefixtures("seeded")
def test_a_partial_update_validates_the_sitemap_against_the_stored_address(client):
    with Session(engine) as s:
        gd = int(s.exec(select(MarketStore.id).where(MarketStore.name == "Gadnic")).one())
    bad = client.put(f"/api/stores/{gd}", json={"sitemap_url": "https://evil.example.com/sitemap.xml"})
    assert bad.status_code == 422
    ok = client.put(f"/api/stores/{gd}", json={"sitemap_url": "https://www.gadnic.com.ar/sitemap/product-pages.xml"})
    assert ok.status_code == 200 and ok.json()["sitemap_url"].endswith("product-pages.xml")
    assert client.put(f"/api/stores/{gd}", json={"image_hosts": "s3.amazonaws.com"}).status_code == 422


@pytest.mark.usefixtures("seeded")
def test_manual_indexing_cannot_be_repeated_in_a_loop(client, monkeypatch):
    async def fake_index(store_id, **kw):
        return None

    monkeypatch.setattr(store_catalog, "index_store", fake_index)
    with Session(engine) as s:
        cp = int(s.exec(select(MarketStore.id).where(MarketStore.name == "Casa Perfecta")).one())
        row = s.get(MarketStore, cp)
        row.last_indexed_at = store_catalog.utcnow() - timedelta(minutes=2)
        s.add(row)
        s.commit()
    r = client.post(f"/api/stores/{cp}/index")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    with Session(engine) as s:
        row = s.get(MarketStore, cp)
        row.last_indexed_at = store_catalog.utcnow() - timedelta(minutes=store_catalog.MANUAL_COOLDOWN_MIN + 1)
        s.add(row)
        s.commit()
    assert client.post(f"/api/stores/{cp}/index").status_code == 202


@pytest.mark.usefixtures("seeded")
def test_who_changed_a_store_is_logged(client, caplog):
    with caplog.at_level(logging.INFO, logger="app.api.store_routes"):
        sid = _post(client, name="Auditada", base_url="https://auditada.com.ar").json()["id"]
        client.put(f"/api/stores/{sid}", json={"enabled": False})
        client.delete(f"/api/stores/{sid}")
    text = caplog.text
    assert "admin cargó la tienda «Auditada»" in text and "admin editó la tienda «Auditada»" in text
    assert "admin BORRÓ la tienda «Auditada»" in text


# ─── borrar una tienda ───────────────────────────────────────────────────────


@pytest.mark.usefixtures("seeded")
def test_deleting_a_store_removes_its_rows_and_photo_embeddings_but_not_a_shared_cdn(client):
    with Session(engine) as s:
        gd = int(s.exec(select(MarketStore.id).where(MarketStore.name == "Gadnic")).one())
        cp = int(s.exec(select(MarketStore.id).where(MarketStore.name == "Casa Perfecta")).one())
        for url in ("https://static.bidcom.com.ar/a.jpg", "https://images.bidcom.com.ar/resize?src=x",
                    "https://acdn-us.mitiendanube.com/b.webp", "https://www.gadnic.com.ar/c.jpg",
                    "https://m.mlstatic.com/d.jpg", "https://static.bidcomXcom.ar/trampa.jpg"):
            s.add(ImageEmbedCache(url=url, model="clip-vit-b32", dim=512, vector_b64="AA=="))
        s.add(StoreCatalogItem(store_id=gd, url=f"{fx.GD}/x"))
        s.add(StoreMatch(run_id=1, product_id="1", store_id=gd, item_id=1, category="igual", auto_category="igual"))
        s.commit()
    assert client.delete(f"/api/stores/{gd}").json() == {"removed": True}
    with Session(engine) as s:
        left = {r.url for r in s.exec(select(ImageEmbedCache)).all()}
        assert s.exec(select(StoreCatalogItem)).all() == [] and s.exec(select(StoreMatch)).all() == []
        assert s.get(MarketStore, cp) is not None
    assert left == {"https://acdn-us.mitiendanube.com/b.webp", "https://m.mlstatic.com/d.jpg",
                    "https://static.bidcomXcom.ar/trampa.jpg"}, left


# ─── retención ───────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("seeded")
def test_prune_removes_items_out_of_the_sitemap_that_were_never_read_and_orphans(store_db, client):
    store_catalog.seed_default_stores()
    with Session(engine) as s:
        gd = int(s.exec(select(MarketStore.id).where(MarketStore.name == "Gadnic")).one())
        old = store_catalog.utcnow() - timedelta(days=120)
        s.add(StoreCatalogItem(store_id=gd, url=f"{fx.GD}/nunca-leida", in_sitemap=False, first_seen_at=old))
        s.add(StoreCatalogItem(store_id=gd, url=f"{fx.GD}/reciente", in_sitemap=False, first_seen_at=store_catalog.utcnow()))
        s.add(StoreCatalogItem(store_id=gd, url=f"{fx.GD}/viva", in_sitemap=True, first_seen_at=old))
        s.add(StoreCatalogItem(store_id=99999, url="https://huerfana.example.com/x"))
        s.add(StoreMatch(run_id=1, product_id="1", store_id=99999, item_id=1, category="igual", auto_category="igual"))
        s.commit()
    out = store_match.prune(180)
    with Session(engine) as s:
        urls = {r.url for r in s.exec(select(StoreCatalogItem)).all()}
        assert s.exec(select(StoreMatch).where(StoreMatch.store_id == 99999)).all() == []
    assert urls == {f"{fx.GD}/reciente", f"{fx.GD}/viva"} and out["items"] == 2


def test_the_embedding_cache_of_a_disabled_store_is_still_pruned(store_db):
    store_catalog.seed_default_stores()
    with Session(engine) as s:
        for row in s.exec(select(MarketStore)).all():
            row.enabled = False
            s.add(row)
        old = store_catalog.utcnow() - timedelta(days=100)
        s.add(ImageEmbedCache(url="https://static.bidcom.com.ar/vieja.jpg", model="m", dim=512, vector_b64="AA==", updated_at=old))
        s.add(ImageEmbedCache(url="https://static.bidcom.com.ar/nueva.jpg", model="m", dim=512, vector_b64="AA=="))
        s.commit()
    store_urls.set_allowed_image_hosts([])
    assert store_match.prune_embed_cache(60) == 1


def test_the_like_patterns_escape_wildcards_in_hosts():
    assert store_urls.like_patterns("a_b.com") == ["https://a\\_b.com/%", "https://www.a\\_b.com/%"]
    assert store_urls.like_patterns("*.x.com") == ["https://x.com/%", "https://%.x.com/%"]
    assert store_urls.like_patterns("acdn*.mitiendanube.com") == ["https://acdn%.mitiendanube.com/%"]


# ─── qué idéntico de tienda puede pintar el color (decisión de Nico + mitigación del juez) ─────


def _row(**over) -> StoreMatch:
    base = dict(run_id=1, product_id="1", store_id=1, item_id=1, category="igual", auto_category="igual", source="clip",
                price_cents=9_000, price_doubtful=False, stock=5, differences=None, human_label=None)
    base.update(over)
    return StoreMatch(**base)


@pytest.mark.parametrize("over,counts", [
    ({}, True),
    ({"source": "clip+nombre"}, True),
    ({"source": "specs"}, True),
    ({"source": "manual", "human_label": "es"}, True),
    ({"source": "veto", "human_label": "es"}, True),             # recién marcado «Es el mismo» (la fuente se actualiza en la próxima corrida)
    ({"source": "llm"}, False),                                  # el juez solo no alcanza
    ({"source": "ambiguo"}, False),
    ({"stock": 0}, False),                                       # agotado: no cuenta
    ({"stock": None}, True),                                     # sin dato de stock: se asume que hay
    ({"price_doubtful": True}, False),
    ({"price_cents": None}, False),
    ({"category": "similar"}, False),
    ({"category": "diferente"}, False),
])
def test_which_store_identicals_can_paint_the_real_color(over, counts):
    assert store_match.counts_for_color(_row(**over)) is counts
    assert store_match.counting_prices([_row(**over)]) == ([9_000] if counts else [])


@pytest.mark.parametrize("over,feeds", [
    ({"category": "similar", "source": "specs", "differences": '["medida"]'}, True),
    ({"category": "similar", "source": "llm", "differences": '["marca"]'}, True),
    ({"category": "similar", "source": "specs", "differences": '["cantidad"]'}, False),
    ({"category": "similar", "source": "specs", "differences": '["capacidad"]'}, False),
    ({"category": "similar", "source": "ambiguo"}, False),
    ({"category": "similar", "source": "specs", "stock": 0}, False),
    ({"category": "similar", "source": "specs", "price_doubtful": True}, False),
])
def test_which_store_similars_can_feed_the_estimated_color(over, feeds):
    assert store_match.feeds_estimate(_row(**over)) is feeds


async def test_gadnic_own_brand_is_generic_so_a_rules_confirmed_gadnic_item_is_identical_and_counts():
    """Decisión de Nico: la marca «Gadnic» es de importador, como la nuestra: no baja un idéntico a similar."""
    info = store_catalog.StoreInfo(2, "Gadnic", fx.GD, "jsonld_sitemap", 11, 2000, "", ("gadnic.com.ar",), "Gadnic")
    entry = store_match.CatalogEntry(1, f"{fx.GD}/x", "Microfono", 9_000, False, "", None, "Gadnic", 5)
    other = store_match.CatalogEntry(2, f"{fx.GD}/y", "Microfono", 9_000, False, "", None, "Stanley", 5)
    assert store_match._candidate(info, entry).brand == "" and store_match._candidate(info, other).brand == "Stanley"
