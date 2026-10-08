"""QA independiente de feat/semaforo-tiendas, criterio 2: el indexador se porta bien con las tiendas.

Se prueba contra COPIAS REALES de lo que publican Gadnic y Casa Perfecta el 08-oct-2026 (robots.txt, sitemap de
Tiendanube, dos fichas de Gadnic recortadas), no solo contra páginas sintéticas:

  * `golden/robots_gadnic_2026-10-08.txt`, `golden/robots_casaperfecta_2026-10-08.txt`
  * `golden/sitemap_casaperfecta_2026-10-08.xml` (recortado: 25 fichas, las ~44 URLs `/ar/search/?q=…` y el resto)
  * `golden/gadnic_tripode_2026-10-08.html` (precio 87.849, stock 10) y `golden/gadnic_sin_stock_2026-10-08.html`
    (JSON-LD con `price: 2147483647` y sin stock): lo que la tienda de verdad manda.

Casa Perfecta devolvía HTTP 500 en TODAS sus fichas de producto al momento del QA (la home y el sitemap sí
andaban), así que el parser de Tiendanube no se pudo confrontar con una ficha real de hoy; ver las pruebas con
fixtures sintéticos en test_store_parse.py.

Los tests marcados xfail(strict) son bugs confirmados: cuando se arreglen van a fallar a propósito (XPASS) y hay
que sacarles la marca.
"""

from __future__ import annotations

import asyncio
import os
import random
from datetime import timedelta
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import net_guard  # noqa: E402
from app.clock import utcnow  # noqa: E402
from app.db.models import MarketStore, StoreCatalogItem  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import daily_budget, store_catalog, store_parse, store_robots, store_urls  # noqa: E402
from tests import store_fixtures as fx  # noqa: E402
from tests.store_fixtures import Clock, FakeSite, store_db  # noqa: E402,F401  (fixture)
from tests.test_store_catalog import (  # noqa: E402,F401
    ROBOTS_TN,
    _clock,
    add_store,
    items,
    run_index,
    tn_product,
    tn_site,
)

GOLDEN = Path(__file__).parent / "golden"
UA = "HugoPriceBot/1.0 (+https://b2box.pro)"
GD_ROBOTS = (GOLDEN / "robots_gadnic_2026-10-08.txt").read_text()
CP_ROBOTS = (GOLDEN / "robots_casaperfecta_2026-10-08.txt").read_text()
CP_SITEMAP = (GOLDEN / "sitemap_casaperfecta_2026-10-08.xml").read_text()


def _gd_page(name: str) -> tuple[str, str]:
    raw = (GOLDEN / name).read_text()
    url = raw.split("-->", 1)[0].replace("<!-- URL:", "").strip()
    return url, raw


def _add_gadnic() -> int:
    return add_store(name="Gadnic", base_url=fx.GD, platform="jsonld_sitemap", refresh_days=11, max_pages_per_day=2000,
                     image_hosts="gadnic.com.ar,bidcom.com.ar", house_brand="Gadnic")


# ─── robots.txt reales ───────────────────────────────────────────────────────

GADNIC_CASES = [
    ("https://www.gadnic.com.ar/tripodes/tripod-gadnic-tripode3", True),
    ("https://www.gadnic.com.ar/tripodes/tripod-gadnic-tripode3?color=rojo", False),
    ("https://www.gadnic.com.ar/tripodes/tripod-gadnic-tripode3?s=x", False),
    ("https://www.gadnic.com.ar/search?q=tripode", False),
    ("https://www.gadnic.com.ar/?orden=precio", False),
    ("https://www.gadnic.com.ar/celulares?item_menu=1", False),
    ("https://www.gadnic.com.ar/sitemap.xml", True),
    ("https://www.gadnic.com.ar/sitemap/product-pages.xml", True),
]
CP_CASES = [
    ("https://www.casaperfecta.com.ar/productos/picador-de-ajo/", True),
    ("https://www.casaperfecta.com.ar/search/?q=tapas", False),
    ("https://www.casaperfecta.com.ar/ar/search/?q=tapas", False),
    ("https://www.casaperfecta.com.ar/search/", False),
    ("https://www.casaperfecta.com.ar/productos/picador-de-ajo/?srsltid=AbC", False),
    ("https://www.casaperfecta.com.ar/productos/picador-de-ajo/?view=grid", False),
    ("https://www.casaperfecta.com.ar/productos/picador-de-ajo/?a=1&view=grid", False),
    ("https://www.casaperfecta.com.ar/checkout/", False),
    ("https://www.casaperfecta.com.ar/comprar/", False),
    ("https://www.casaperfecta.com.ar/admin/", False),
    ("https://www.casaperfecta.com.ar/sitemap.xml", True),
]


@pytest.mark.parametrize("url,allowed", GADNIC_CASES)
def test_the_real_gadnic_robots_is_applied_as_published(url, allowed):
    assert store_robots.parse(GD_ROBOTS, UA).allows(url) is allowed


@pytest.mark.parametrize("url,allowed", CP_CASES)
def test_the_real_casa_perfecta_robots_is_applied_as_published(url, allowed):
    assert store_robots.parse(CP_ROBOTS, UA).allows(url) is allowed


def test_the_real_gadnic_robots_has_no_group_for_us_so_the_star_group_rules():
    robots = store_robots.parse(GD_ROBOTS, UA)
    assert robots.crawl_delay is None and {r.pattern for r in robots.rules} == {"/*?", "/*?s=", "/*?item_menu=", "/*?orden="}


# ─── el indexador no pide lo que la tienda prohíbe, ni con el sitemap lleno de trampas ─


async def test_gadnic_never_asks_for_a_url_with_a_query_even_if_the_sitemap_lists_them(store_db):
    sid = _add_gadnic()
    site = FakeSite()
    tripod, page = _gd_page("gadnic_tripode_2026-10-08.html")
    clean2 = f"{fx.GD}/tripodes/tripode-dos"
    site.add(f"{fx.GD}/robots.txt", GD_ROBOTS)
    # el índice real de Gadnic: cuatro hijos, solo uno es de productos
    site.add(f"{fx.GD}/sitemap.xml", fx.sitemap_index(f"{fx.GD}/sitemap/institutional-pages.xml", f"{fx.GD}/sitemap/pages.xml",
                                                      f"{fx.GD}/sitemap/category-pages.xml", f"{fx.GD}/sitemap/product-pages.xml"))
    site.add(f"{fx.GD}/sitemap/product-pages.xml", fx.urlset(
        tripod, clean2, f"{tripod}?color=rojo", f"{clean2}?s=x", f"{fx.GD}/search?q=tripode", f"{fx.GD}/celulares?orden=precio",
        f"{fx.GD}/x?item_menu=2", f"{tripod}?", f"{tripod}#opiniones"))
    for u in (tripod, clean2):
        site.add(u, page)
    report = await run_index(sid, site)
    assert report.status == "ok", report.summary()
    asked = site.urls
    assert [u for u in asked if "?" in u] == [], "pidió una URL con «?» en Gadnic"
    assert [u for u in asked if "search" in u] == []
    assert sorted(site.fetched_pages()) == sorted([tripod, clean2])
    # ni un hijo del sitemap que no sea de productos
    assert not any(x in u for u in asked for x in ("institutional", "/pages.xml", "category-pages"))
    assert asked[0] == f"{fx.GD}/robots.txt"


async def test_casa_perfecta_real_sitemap_has_dozens_of_search_urls_and_none_is_requested(store_db):
    sid = add_store()
    site = FakeSite()
    site.add(f"{fx.CP}/robots.txt", CP_ROBOTS)
    site.add(f"{fx.CP}/sitemap.xml", CP_SITEMAP)
    sm = store_parse.parse_sitemap(CP_SITEMAP)
    product_urls = [u for u in sm.locs if "/productos/" in u and "search" not in u and not u.rstrip("/").endswith("/productos")]
    searches = [u for u in sm.locs if "search" in u]
    assert len(searches) >= 30 and product_urls, "el recorte del sitemap real perdió lo que prueba el test"
    for u in product_urls:
        site.add(u, fx.tiendanube_page(u, "Producto", [fx.variant(10000)]))
    report = await run_index(sid, site)
    assert report.status == "ok", report.summary()
    assert [u for u in site.urls if "search" in u or "?" in u] == []
    # solo fichas de producto: ni la home, ni /contacto/, ni /productos/ (el listado), ni el sitemap del blog
    assert sorted(site.fetched_pages()) == sorted(set(product_urls))


# ─── precios: lo que la tienda de verdad manda ──────────────────────────────


def _parse_gadnic(raw: str, url: str):
    return store_parse.parse_product_page("jsonld_sitemap", raw, [url, url])


def test_real_gadnic_page_with_stock_reads_price_brand_stock_and_photos():
    url, raw = _gd_page("gadnic_tripode_2026-10-08.html")
    it = _parse_gadnic(raw, url)
    assert (it.price_cents, it.price_doubtful, it.brand, it.stock, it.sku) == (8_784_900, False, "Gadnic", 10, "TRIPODE3")
    hosts = ("gadnic.com.ar", "bidcom.com.ar")
    photos = [store_urls.safe_image(u, hosts) for u in it.image_urls]
    assert photos[0] == "https://static.bidcom.com.ar/publicacionesML/productos/TRIPODE3/1000x1000-TRIPODE3-A.jpg"
    assert all(p and p.startswith("https://") for p in photos)


def test_real_gadnic_out_of_stock_page_has_a_placeholder_price_that_is_not_taken_as_real():
    """La ficha sin stock trae `price: 2147483647` (INT_MAX) en el JSON-LD y en el estado de Next.js."""
    url, raw = _gd_page("gadnic_sin_stock_2026-10-08.html")
    assert "2147483647" in raw
    it = _parse_gadnic(raw, url)
    assert it.price_cents is None and it.stock == 0


@pytest.mark.parametrize("ld,visible,expect_price,doubtful", [
    (87849, 87849, 8_784_900, False),          # coinciden
    (87849, 87850, 8_784_900, False),          # dentro de la tolerancia (0,5 %): vale el JSON-LD
    (87849, 99999, 9_999_900, True),           # el JSON-LD no coincide con lo que se ve: manda lo visible, dudoso
    (999.99, 999.99, 99_999, True),            # justo por debajo de ARS 1.000: dato viejo
    (1000, 1000, 100_000, False),              # ARS 1.000 exactos ya vale
    (249, 249, 24_900, True),                  # el mini teclado de ARS 249
])
def test_gadnic_price_rules_on_the_real_page(ld, visible, expect_price, doubtful):
    url, raw = _gd_page("gadnic_tripode_2026-10-08.html")
    raw = raw.replace('"price":87849', f'"price":{ld}').replace('\\"finalPrice\\":87849', f'\\"finalPrice\\":{visible}') \
        .replace('"finalPrice":87849', f'"finalPrice":{visible}')
    it = _parse_gadnic(raw, url)
    assert (it.price_cents, it.price_doubtful) == (expect_price, doubtful), it.price_note


def test_casa_perfecta_strikethrough_json_ld_price_is_ignored_on_a_promo():
    """JSON-LD dice 9.000 (el tachado) y la página vende a 5.000: manda `data-variants`, sin marca de dudoso."""
    url = f"{fx.CP}/productos/olla-promo/"
    page = fx.tiendanube_page(url, "Olla promo", [fx.variant(5000, compare=9000, sku="OLLA")], ld_price=9000, sku="OLLA")
    it = store_parse.parse_product_page("tiendanube", page, [url])
    assert (it.price_cents, it.price_doubtful) == (500_000, False)


def test_casa_perfecta_without_variants_the_json_ld_price_could_be_the_strikethrough_so_it_is_flagged():
    url = f"{fx.CP}/productos/olla-sin-variantes/"
    page = fx.tiendanube_page(url, "Olla", [], ld_price=9000, sku="OLLA")
    it = store_parse.parse_product_page("tiendanube", page, [url])
    assert it.price_cents == 900_000 and it.price_doubtful is True and "sin variantes" in it.price_note


def test_casa_perfecta_json_ld_equal_to_another_variants_regular_price_is_not_flagged_but_the_cheapest_in_stock_wins():
    url = f"{fx.CP}/productos/olla-dos-variantes/"
    variants = [fx.variant(9000, sku="A", option="Grande"), fx.variant(5000, compare=9000, sku="B", option="Chica")]
    page = fx.tiendanube_page(url, "Olla", variants, ld_price=9000, sku="A")
    it = store_parse.parse_product_page("tiendanube", page, [url])
    assert (it.price_cents, it.price_doubtful) == (500_000, False)


async def test_the_promo_price_reaches_the_catalog_and_not_the_strikethrough_one(store_db):
    sid = add_store()
    url = f"{fx.CP}/productos/olla-promo/"
    site = FakeSite()
    site.add(f"{fx.CP}/robots.txt", ROBOTS_TN)
    site.add(f"{fx.CP}/sitemap.xml", fx.urlset(url))
    site.add(url, fx.tiendanube_page(url, "Olla promo", [fx.variant(5000, compare=9000, sku="OLLA")], ld_price=9000, sku="OLLA"))
    await run_index(sid, site)
    row = items(sid)[url]
    assert (row.price_cents, row.price_doubtful, row.title) == (500_000, False, "Olla promo")


# ─── ritmo: una página cada 2-3 segundos, también con errores ───────────────


class TimedSite(FakeSite):
    def __init__(self, clock: Clock) -> None:
        super().__init__()
        self.clock = clock
        self.times: list[float] = []

    async def get(self, url, **kw):
        self.times.append(self.clock())
        return await super().get(url, **kw)


async def test_every_request_of_a_store_waits_two_to_three_seconds_robots_sitemap_pages_errors_included(store_db):
    sid = add_store()
    clock = Clock()
    site = TimedSite(clock)
    slugs = [f"p{i}" for i in range(12)]
    base = tn_site(slugs)
    site.pages = base.pages
    site.errors[tn_product("p3")[0]] = httpx.ConnectTimeout("lento")            # un error de red
    site.pages[tn_product("p5")[0]] = (500, "boom", {})                          # un 500
    site.pages[tn_product("p7")[0]] = (404, "no", {})
    clock.attach(site)
    report = await store_catalog.index_store(sid, get=site.get, sleep=site.sleep, rng=random.Random(7), monotonic=clock,
                                             delay=(2.0, 3.0))
    assert report.fetched == 12
    gaps = [b - a for a, b in zip(site.times, site.times[1:])]
    assert len(gaps) >= 13
    assert all(2.0 - 1e-9 <= g <= 3.0 + 1e-9 for g in gaps), sorted(gaps)[:3] + sorted(gaps)[-3:]
    assert len({round(g, 3) for g in gaps}) > 3, "el ritmo tiene que variar entre 2 y 3 s, no ser fijo"


async def test_two_stores_in_parallel_each_keep_their_own_pace_and_cap(store_db):
    a = add_store()
    b = _add_gadnic()
    clock = Clock()
    sa, sb = TimedSite(clock), TimedSite(clock)
    base = tn_site([f"p{i}" for i in range(5)])
    sa.pages = base.pages
    tripod, page = _gd_page("gadnic_tripode_2026-10-08.html")
    sb.add(f"{fx.GD}/robots.txt", GD_ROBOTS)
    sb.add(f"{fx.GD}/sitemap.xml", fx.urlset(tripod))
    sb.add(tripod, page)

    def get(url, **kw):
        return (sa if url.startswith(fx.CP) else sb).get(url, **kw)

    async def sleep(seconds):
        await asyncio.sleep(0)
        clock.t += seconds

    reports = await asyncio.gather(
        store_catalog.index_store(a, get=get, sleep=sleep, rng=random.Random(1), monotonic=clock, delay=(2.0, 3.0)),
        store_catalog.index_store(b, get=get, sleep=sleep, rng=random.Random(2), monotonic=clock, delay=(2.0, 3.0)))
    assert [r.status for r in reports] == ["ok", "ok"]
    for site in (sa, sb):
        gaps = [y - x for x, y in zip(site.times, site.times[1:])]
        assert gaps and all(g >= 2.0 - 1e-9 for g in gaps), gaps


# ─── dead: 404/500 dos veces → 30 días ──────────────────────────────────────


@pytest.mark.parametrize("status", [404, 500, 410])
async def test_a_page_failing_twice_is_dead_for_exactly_30_days(store_db, _clock, status):
    sid = add_store(refresh_days=7)
    url, _ = tn_product("muerta")
    site = tn_site(["muerta"])
    site.pages[url] = (status, "x", {})
    await run_index(sid, site)
    assert items(sid)[url].dead is False and items(sid)[url].fails == 1
    _clock["now"] += timedelta(days=8)
    await run_index(sid, site)
    row = items(sid)[url]
    assert (row.dead, row.fails) == (True, 2)
    died = row.dead_since
    # a los 29 días y 23 horas de estar muerta no se vuelve a pedir; pasados los 30, sí
    _clock["now"] = died + timedelta(days=29, hours=23)
    site.requests.clear()
    await run_index(sid, site)
    assert url not in site.fetched_pages()
    _clock["now"] = died + timedelta(days=30, minutes=1)
    site.requests.clear()
    await run_index(sid, site)
    assert url in site.fetched_pages()


async def test_a_success_between_two_failures_resets_the_strike_count(store_db, _clock):
    sid = add_store(refresh_days=7)
    url, body = tn_product("intermitente")
    site = tn_site(["intermitente"])
    for status, expect in ((500, (False, 1)), (200, (False, 0)), (500, (False, 1))):
        site.pages[url] = (status, body if status == 200 else "x", {})
        await run_index(sid, site)
        row = items(sid)[url]
        assert (row.dead, row.fails) == expect
        _clock["now"] += timedelta(days=8)


# ─── GET condicional ────────────────────────────────────────────────────────


async def test_conditional_get_works_with_only_last_modified_and_a_304_never_rewrites_the_data(store_db, _clock):
    sid = add_store(refresh_days=7)
    url, body = tn_product("cond")
    site = tn_site(["cond"])
    site.add(url, body, last_modified="Wed, 07 Oct 2026 10:00:00 GMT")
    await run_index(sid, site)
    first = items(sid)[url]
    assert first.last_modified == "Wed, 07 Oct 2026 10:00:00 GMT" and first.etag is None
    _clock["now"] += timedelta(days=8)
    site.requests.clear()
    calls = []

    async def get(u, *, timeout=None, headers=None, max_bytes=None, **kw):
        calls.append((u, dict(headers or {})))
        if u == url:
            assert headers.get("If-Modified-Since") == "Wed, 07 Oct 2026 10:00:00 GMT"
            return httpx.Response(304, request=httpx.Request("GET", u))
        return await FakeSite.get(site, u, timeout=timeout, headers=headers, max_bytes=max_bytes, **kw)

    clock = Clock()
    await store_catalog.index_store(sid, get=get, sleep=site.sleep, rng=random.Random(1), monotonic=clock, delay=(0.0, 0.0))
    again = items(sid)[url]
    assert (again.title, again.price_cents, again.fails, again.dead) == (first.title, first.price_cents, 0, False)
    assert again.last_checked_at > first.last_checked_at
    assert any(u == url for u, _ in calls)


# ─── bugs confirmados ───────────────────────────────────────────────────────


async def test_a_429_on_robots_txt_stops_the_pass(store_db):
    sid = add_store()
    site = tn_site(["a", "b", "c"])
    site.add(f"{fx.CP}/robots.txt", "Too Many Requests", status=429)
    report = await run_index(sid, site)
    assert report.status == "aborted"
    assert site.fetched_pages() == []


@pytest.mark.xfail(strict=True, reason="BUG: safe_get sigue los redirects sin consultar robots.txt: una ficha que redirige a una URL que "
                                       "la tienda prohíbe (con «?» en Gadnic, /search/ en Tiendanube) se pide igual")
@pytest.mark.parametrize("target", ["/tripodes/otro?utm=1", "/search?q=x"])
async def test_a_redirect_into_a_forbidden_url_is_not_followed(store_db, monkeypatch, target):
    sid = _add_gadnic()
    hits: list[str] = []
    page_url, page = _gd_page("gadnic_tripode_2026-10-08.html")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        hits.append(path)
        if path == "/robots.txt":
            return httpx.Response(200, text=GD_ROBOTS)
        if path == "/sitemap.xml":
            return httpx.Response(200, text=fx.urlset(page_url))
        if request.url.path == "/tripodes/tripod-gadnic-tripode3":
            return httpx.Response(301, headers={"Location": fx.GD + target})
        return httpx.Response(200, text=page)

    real = httpx.AsyncClient
    monkeypatch.setattr(net_guard.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(net_guard, "assert_public_url", lambda url: None)
    monkeypatch.setattr(net_guard, "assert_peer_public", lambda resp: None)
    clock = Clock()
    report = await store_catalog.index_store(sid, sleep=lambda s: asyncio.sleep(0), rng=random.Random(1), monotonic=clock,
                                             delay=(0.0, 0.0))
    assert report.fetched == 1
    forbidden = [h for h in hits if "?" in h or h.startswith("/search")]
    assert forbidden == [], f"se pidió lo que robots.txt prohíbe: {forbidden}"
