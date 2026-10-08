"""Indexador de tiendas: robots, ritmo, cupo diario, GET condicional, 404/500 → dead,
rotación y cortes. Todo contra un sitio de mentira: nada sale a la red."""

from __future__ import annotations

import random
from datetime import timedelta

import httpx
import pytest
from sqlmodel import Session, select

from app.clock import utcnow
from app.config import get_settings
from app.db.models import MarketStore, Setting, StoreCatalogItem
from app.db.session import engine
from app.pricing import daily_budget, store_catalog
from tests import store_fixtures as fx
from tests.store_fixtures import Clock, FakeSite, store_db  # noqa: F401  (fixture)

ROBOTS_TN = ("User-agent: *\nDisallow: /admin/\nDisallow: /search/\nDisallow: /ar/search/\n"
             "Disallow: /*?*srsltid=*\n")
ROBOTS_GD = "User-agent: *\nDisallow: /*?\n"


def add_store(**kw) -> int:
    data = dict(name="Casa Perfecta", base_url=fx.CP, platform="tiendanube", refresh_days=7,
                max_pages_per_day=1000)
    data.update(kw)
    with Session(engine) as s:
        row = MarketStore(**data)
        s.add(row)
        s.commit()
        s.refresh(row)
        return int(row.id)


def tn_product(slug: str, price: float = 11000, name: str | None = None) -> tuple[str, str]:
    url = f"{fx.CP}/productos/{slug}/"
    return url, fx.tiendanube_page(url, name or f"Producto {slug}", [fx.variant(price, sku=slug.upper())], sku=slug.upper())


def tn_site(slugs: list[str], *, robots: str = ROBOTS_TN, extra_locs: tuple[str, ...] = (), **headers) -> FakeSite:
    site = FakeSite()
    site.add(f"{fx.CP}/robots.txt", robots)
    locs = [f"{fx.CP}/", f"{fx.CP}/productos/", *[tn_product(s)[0] for s in slugs], *extra_locs]
    site.add(f"{fx.CP}/sitemap.xml", fx.urlset(*locs, with_hreflang=True))
    for slug in slugs:
        url, body = tn_product(slug)
        site.add(url, body, **headers)
    return site


async def run_index(store_id: int, site: FakeSite, *, delay=(0.0, 0.0), clock: Clock | None = None, **kw):
    clock = clock or Clock()
    clock.attach(site)
    return await store_catalog.index_store(
        store_id, get=site.get, sleep=site.sleep, rng=random.Random(1), monotonic=clock, delay=delay, **kw)


def items(store_id: int) -> dict[str, StoreCatalogItem]:
    with Session(engine) as s:
        return {r.url: r for r in s.exec(select(StoreCatalogItem).where(StoreCatalogItem.store_id == store_id)).all()}


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    """`utcnow` del indexador y el día UTC del cupo, controlables."""
    state = {"now": utcnow().replace(microsecond=0)}
    monkeypatch.setattr(store_catalog, "utcnow", lambda: state["now"])
    monkeypatch.setattr(daily_budget, "today_utc", lambda: state["now"].strftime("%Y-%m-%d"))
    return state


# ─── lo básico ───────────────────────────────────────────────────────────────


async def test_indexes_a_tiendanube_store_and_saves_the_product_data(store_db):
    sid = add_store()
    site = tn_site(["picador-de-ajo", "frasco-hermetico"])
    rep = await run_index(sid, site)
    assert rep.status == "ok" and rep.ok == 2 and rep.new_urls == 2 and rep.sitemap_urls == 2
    got = items(sid)[f"{fx.CP}/productos/picador-de-ajo/"]
    assert got.title == "Producto picador-de-ajo" and got.price_cents == 1_100_000 and not got.price_doubtful
    assert got.sku == "PICADOR-DE-AJO" and got.stock == 5
    # La foto sale saneada: https y del CDN de Tiendanube.
    assert got.image_url and got.image_url.startswith("https://acdn-us.mitiendanube.com/")
    assert got.last_seen_at and got.last_checked_at and got.fails == 0 and not got.dead


async def test_requests_are_honest_and_stateless(store_db):
    sid = add_store()
    site = tn_site(["picador-de-ajo"])
    await run_index(sid, site)
    assert site.requests, "no hizo ningún pedido"
    for _url, headers in site.requests:
        assert headers["User-Agent"] == get_settings().store_user_agent
        assert "Mozilla" not in headers["User-Agent"] and "@" not in headers["User-Agent"]
        assert "Cookie" not in headers and "Authorization" not in headers


async def test_only_product_pages_allowed_by_robots_are_read(store_db):
    """El sitemap de Tiendanube trae /ar/search/?q=… (vedado) y páginas que no son productos."""
    sid = add_store()
    site = tn_site(["picador-de-ajo"], extra_locs=(
        f"{fx.CP}/ar/search/?q=Tapas", f"{fx.CP}/search/?q=a", f"{fx.CP}/productos/x/?srsltid=abc",
        f"{fx.CP}/contacto/", f"{fx.CP}/cocina1/", "https://evil.com/productos/x/", "http://www.casaperfecta.com.ar/productos/y/"))
    rep = await run_index(sid, site)
    assert rep.sitemap_urls == 1
    assert list(items(sid)) == [f"{fx.CP}/productos/picador-de-ajo/"]
    assert site.fetched_pages() == [f"{fx.CP}/productos/picador-de-ajo/"]


async def test_gadnic_urls_with_a_query_are_never_requested(store_db):
    gd = fx.GD
    sid = add_store(name="Gadnic", base_url=gd, platform="jsonld_sitemap", image_hosts="gadnic.com.ar,*.bidcom.com.ar")
    site = FakeSite()
    site.add(f"{gd}/robots.txt", ROBOTS_GD)
    site.add(f"{gd}/sitemap.xml", fx.sitemap_index(f"{gd}/sitemap/pages.xml", f"{gd}/sitemap/product-pages.xml"))
    ok = f"{gd}/microfonos-profesionales/microfono-condenser-profesional-gamer-stream"
    site.add(f"{gd}/sitemap/product-pages.xml", fx.urlset(ok, f"{gd}/microfonos-profesionales?brand=gadnic", f"{gd}/?s=teclado"))
    site.add(ok, fx.gadnic_page(ok, "Microfono Condenser", ld_price=25999, final_price=25999))
    rep = await run_index(sid, site)
    assert rep.ok == 1 and list(items(sid)) == [ok]
    assert not any("?" in u for u in site.urls)
    assert f"{gd}/sitemap/pages.xml" not in site.urls, "solo se sigue el sitemap de productos"
    got = items(sid)[ok]
    assert got.brand == "Gadnic" and got.price_cents == 2_599_900
    assert got.image_url == f"{fx.GD_STATIC}/MICCOND6/1000x1000-MICCOND6.jpg"


async def test_gadnic_doubtful_price_is_saved_with_the_flag(store_db):
    gd = fx.GD
    sid = add_store(name="Gadnic", base_url=gd, platform="jsonld_sitemap", image_hosts="gadnic.com.ar,*.bidcom.com.ar")
    url = f"{gd}/mouse-y-teclados/mini-teclado-inalambrico"
    site = FakeSite()
    site.add(f"{gd}/robots.txt", ROBOTS_GD)
    site.add(f"{gd}/sitemap.xml", fx.sitemap_index(f"{gd}/sitemap/product-pages.xml"))
    site.add(f"{gd}/sitemap/product-pages.xml", fx.urlset(url))
    site.add(url, fx.gadnic_page(url, "Mini Teclado", ld_price=249, final_price=249, sku="SMTV0006"))
    await run_index(sid, site)
    got = items(sid)[url]
    assert got.price_cents == 24_900 and got.price_doubtful and "muy bajo" in (got.price_note or "")


async def test_photos_outside_the_allowed_hosts_are_dropped(store_db):
    sid = add_store(platform="jsonld_sitemap", image_hosts="casaperfecta.com.ar")        # sin el CDN de Tiendanube
    site = tn_site(["picador-de-ajo"])
    await run_index(sid, site)
    assert items(sid)[f"{fx.CP}/productos/picador-de-ajo/"].image_url is None


# ─── ritmo ───────────────────────────────────────────────────────────────────


async def test_one_request_every_two_to_three_seconds(store_db):
    sid = add_store()
    site = tn_site(["a", "b", "c", "d"])
    await run_index(sid, site, delay=(2.0, 3.0))
    assert len(site.sleeps) >= 5, "robots + sitemap + 4 fichas llevan pausas entre sí"
    assert all(2.0 <= s <= 3.0 + 1e-9 for s in site.sleeps), site.sleeps


async def test_crawl_delay_from_robots_slows_the_pace(store_db):
    sid = add_store()
    site = tn_site(["a", "b"], robots="User-agent: *\nCrawl-delay: 9\n")
    await run_index(sid, site, delay=(2.0, 3.0))
    assert site.sleeps and min(site.sleeps[1:]) >= 9.0 - 1e-9


# ─── cupo diario y rotación ─────────────────────────────────────────────────


async def test_daily_cap_and_rotation_of_a_big_catalog(store_db, _clock):
    sid = add_store(max_pages_per_day=3, refresh_days=3)
    slugs = [f"p{i}" for i in range(8)]
    site = tn_site(slugs)
    first = await run_index(sid, site)
    assert first.fetched == 3 and len(site.fetched_pages()) == 3
    again = await run_index(sid, site)                         # mismo día: no hay más cupo
    assert again.fetched == 0 and "tope diario" in again.message
    assert len(site.fetched_pages()) == 3
    assert daily_budget.used_today(f"{store_catalog.PAGES_COUNTER_PREFIX}{sid}") == 3

    _clock["now"] += timedelta(days=1)                         # otro día: sigue por las que nunca se leyeron
    await run_index(sid, site)
    _clock["now"] += timedelta(days=1)
    await run_index(sid, site)
    read = [u for u, r in items(sid).items() if r.last_checked_at]
    assert len(read) == 8, "en tres días rota todo el catálogo de 8 con cupo 3"
    assert len(set(site.fetched_pages())) == 8 and len(site.fetched_pages()) == 8, "nadie se leyó dos veces"


async def test_new_urls_go_first_then_the_least_recently_checked(store_db, _clock):
    sid = add_store(max_pages_per_day=100, refresh_days=1)
    site = tn_site(["viejo", "medio"])
    await run_index(sid, site)
    _clock["now"] += timedelta(days=2)
    # Ahora hay una URL nueva y las dos viejas vencidas, pero el cupo del día es de 2.
    site.add(f"{fx.CP}/sitemap.xml", fx.urlset(*[tn_product(s)[0] for s in ("viejo", "medio", "nuevo")]))
    url, body = tn_product("nuevo")
    site.add(url, body)
    with Session(engine) as s:
        rows = {r.url: r for r in s.exec(select(StoreCatalogItem)).all()}
        rows[tn_product("medio")[0]].last_checked_at = _clock["now"] - timedelta(days=3)
        rows[tn_product("viejo")[0]].last_checked_at = _clock["now"] - timedelta(days=9)
        s.add_all(rows.values())
        s.commit()
    site.requests.clear()
    await run_index(sid, site, max_pages=2)
    assert site.fetched_pages() == [tn_product("nuevo")[0], tn_product("viejo")[0]]


async def test_pass_max_pages_and_max_seconds_limit_a_single_pass(store_db):
    sid = add_store()
    site = tn_site([f"p{i}" for i in range(6)])
    rep = await run_index(sid, site, max_pages=2)
    assert rep.fetched == 2
    site2 = tn_site([f"q{i}" for i in range(6)])
    sid2 = add_store(name="Otra", base_url=fx.CP)
    clock = Clock()
    rep2 = await run_index(sid2, site2, delay=(10.0, 10.0), clock=clock, max_seconds=35)
    assert 1 <= rep2.fetched <= 5 and "tiempo" in rep2.message


# ─── dead ────────────────────────────────────────────────────────────────────


async def _drive_to_death(sid, site, clock, url, status, *, days_between=2, max_passes=6):
    """Pasadas separadas hasta que la ficha quede `dead`: devuelve cuántas hicieron falta."""
    for n in range(1, max_passes + 1):
        await run_index(sid, site)
        if items(sid)[url].dead:
            return n
        clock["now"] += timedelta(days=days_between)
    return None


@pytest.mark.parametrize("status", [404, 410])
async def test_a_page_answering_404_or_410_twice_is_dead_and_not_retried_for_30_days(store_db, _clock, status):
    sid = add_store(refresh_days=1)
    site = tn_site(["viva", "muerta"])
    dead_url = tn_product("muerta")[0]
    site.add(dead_url, "<html>error</html>", status=status, etag='"error-page"')
    rep1 = await run_index(sid, site)
    assert rep1.ok == 1 and rep1.failed == 1 and rep1.newly_dead == 0
    row = items(sid)[dead_url]
    assert row.fails == 1 and not row.dead and row.fail_reason == f"HTTP {status}"
    assert row.etag is None, "el ETag de una página de error no se guarda"

    _clock["now"] += timedelta(days=2)
    rep2 = await run_index(sid, site)
    assert rep2.newly_dead == 1
    row = items(sid)[dead_url]
    assert row.dead and row.fails == 2 and row.dead_since == _clock["now"]

    for days in (3, 10, 29):                                   # dentro de los 30 días: ni se toca
        _clock["now"] = row.dead_since + timedelta(days=days)
        site.requests.clear()
        await run_index(sid, site)
        assert dead_url not in site.urls, f"se reintentó a los {days} días"

    _clock["now"] = row.dead_since + timedelta(days=31)        # pasados los 30: otra oportunidad
    site.requests.clear()
    site.add(dead_url, tn_product("muerta")[1])                # ahora vive
    await run_index(sid, site)
    revived = items(sid)[dead_url]
    assert dead_url in site.urls and not revived.dead and revived.fails == 0 and revived.title


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_a_5xx_page_is_dead_only_after_days_of_failing_not_after_two_passes(store_db, _clock, status):
    """Una caída de la tienda no puede marcarlo todo como muerto de golpe: además de dos fallos tienen que
    pasar `store_dead_min_days_5xx` días desde el primero."""
    sid = add_store(refresh_days=1)
    site = tn_site(["viva", "rota"])
    url = tn_product("rota")[0]
    site.add(url, "<html>error</html>", status=status, etag='"error-page"')
    await run_index(sid, site)                                 # día 0: primer fallo
    assert items(sid)[url].fails == 1 and not items(sid)[url].dead
    _clock["now"] += timedelta(days=1)                         # se reintenta al día siguiente (backoff de 1 día)
    await run_index(sid, site)
    row = items(sid)[url]
    assert row.fails == 2 and not row.dead, "dos fallos en dos días todavía no son «muerta»"
    _clock["now"] += timedelta(days=2)                         # backoff de 2 días: ya van 3 desde el primero
    rep = await run_index(sid, site)
    row = items(sid)[url]
    assert row.fails == 3 and row.dead and rep.newly_dead == 1 and row.first_fail_at is not None
    assert row.etag is None


async def test_a_5xx_page_that_recovers_is_never_dead(store_db, _clock):
    sid = add_store(refresh_days=1)
    site = tn_site(["rota"])
    url, body = tn_product("rota")
    site.add(url, "boom", status=503)
    await run_index(sid, site)
    _clock["now"] += timedelta(days=1)
    await run_index(sid, site)
    assert items(sid)[url].fails == 2
    site.add(url, body)                                        # la tienda se recupera
    _clock["now"] += timedelta(days=2)
    await run_index(sid, site)
    row = items(sid)[url]
    assert row.fails == 0 and not row.dead and row.first_fail_at is None and row.title


async def test_failing_pages_are_retried_with_backoff_but_never_later_than_refresh_days(store_db, _clock):
    sid = add_store(refresh_days=7)
    site = tn_site(["rota"])
    url = tn_product("rota")[0]
    site.add(url, "boom", status=500)
    await run_index(sid, site)
    for waited, expected_fails in ((0.5, 1), (1.0, 2), (1.5, 2), (2.0, 3)):
        _clock["now"] += timedelta(days=waited)
        site.requests.clear()
        await run_index(sid, site)
        assert items(sid)[url].fails == expected_fails, (waited, expected_fails)


async def test_a_dead_page_that_keeps_failing_waits_another_30_days(store_db, _clock):
    sid = add_store(refresh_days=1)
    url = tn_product("muerta")[0]
    site = tn_site(["muerta"])
    site.add(url, "x", status=404)
    for _ in range(2):
        await run_index(sid, site)
        _clock["now"] += timedelta(days=2)
    first_death = items(sid)[url].dead_since
    _clock["now"] = first_death + timedelta(days=31)
    await run_index(sid, site)
    row = items(sid)[url]
    assert row.dead and row.dead_since == _clock["now"]


async def test_a_200_page_that_is_not_a_product_counts_as_a_failure(store_db, _clock):
    sid = add_store(refresh_days=1)
    url = tn_product("vacia")[0]
    site = tn_site(["vacia"])
    site.add(url, "<html><body>Esta tienda cerró</body></html>")
    await run_index(sid, site)
    _clock["now"] += timedelta(days=2)
    await run_index(sid, site)
    row = items(sid)[url]
    assert row.dead and row.fail_reason == "la página no es un producto"


async def test_a_huge_page_is_rejected_and_counts_as_a_failure(store_db):
    sid = add_store()
    site = tn_site(["gigante"])
    site.too_large.add(tn_product("gigante")[0])
    rep = await run_index(sid, site)
    assert rep.failed == 1 and items(sid)[tn_product("gigante")[0]].fail_reason == "página demasiado grande"


async def test_redirect_to_another_site_is_a_failure_not_a_product(store_db):
    sid = add_store()
    site = tn_site(["a"])
    url = tn_product("a")[0]
    real_get = site.get

    async def redirected(u, **kw):
        resp = await real_get(u, **kw)
        if u == url:
            return httpx.Response(200, content=resp.content, request=httpx.Request("GET", "https://evil.com/productos/a/"))
        return resp

    rep = await store_catalog.index_store(sid, get=redirected, sleep=site.sleep, rng=random.Random(1),
                                          monotonic=Clock(), delay=(0, 0))
    assert rep.failed == 1 and items(sid)[url].fail_reason == "redirige a otro sitio"


# ─── GET condicional ─────────────────────────────────────────────────────────


async def test_conditional_get_sends_etag_and_a_304_keeps_the_data(store_db, _clock):
    sid = add_store(refresh_days=1)
    site = tn_site(["picador"], etag='"abc123"', last_modified="Thu, 08 Oct 2026 18:00:00 GMT")
    url = tn_product("picador")[0]
    await run_index(sid, site)
    first = items(sid)[url]
    assert first.etag == '"abc123"' and first.last_modified == "Thu, 08 Oct 2026 18:00:00 GMT"
    assert "If-None-Match" not in site.requests[-1][1], "la primera vez no hay validador"

    _clock["now"] += timedelta(days=2)
    site.requests.clear()
    rep = await run_index(sid, site)
    sent = next(h for u, h in site.requests if u == url)
    assert sent["If-None-Match"] == '"abc123"' and sent["If-Modified-Since"] == "Thu, 08 Oct 2026 18:00:00 GMT"
    assert rep.not_modified == 1 and rep.ok == 0
    after = items(sid)[url]
    assert after.title == first.title and after.price_cents == first.price_cents
    assert after.last_seen_at == _clock["now"] and after.last_checked_at == _clock["now"]


async def test_a_changed_page_updates_the_price_and_the_etag(store_db, _clock):
    sid = add_store(refresh_days=1)
    site = tn_site(["picador"], etag='"v1"')
    url = tn_product("picador")[0]
    await run_index(sid, site)
    _clock["now"] += timedelta(days=2)
    site.add(url, fx.tiendanube_page(url, "Picador", [fx.variant(13000, sku="PICADOR")], sku="PICADOR"), etag='"v2"')
    rep = await run_index(sid, site)
    assert rep.ok == 1 and rep.not_modified == 0
    row = items(sid)[url]
    assert row.price_cents == 1_300_000 and row.etag == '"v2"'


async def test_pages_are_not_reread_before_refresh_days(store_db, _clock):
    sid = add_store(refresh_days=7)
    site = tn_site(["a", "b"])
    await run_index(sid, site)
    _clock["now"] += timedelta(days=3)
    site.requests.clear()
    rep = await run_index(sid, site)
    assert rep.fetched == 0 and site.fetched_pages() == []


# ─── cortes de la pasada ─────────────────────────────────────────────────────


async def test_429_stops_the_pass_and_does_not_kill_pages(store_db):
    sid = add_store()
    site = tn_site(["a", "b", "c", "d"])
    site.add(tn_product("b")[0], "slow down", status=429)
    rep = await run_index(sid, site)
    assert rep.status == "aborted" and "429" in rep.message
    assert len(site.fetched_pages()) == 2, "después del 429 no se pide nada más"
    assert all(not r.dead and r.fails == 0 for r in items(sid).values())
    with Session(engine) as s:
        assert s.get(MarketStore, sid).last_index_status.startswith("aborted")


async def test_three_403s_in_a_row_stop_the_pass(store_db):
    sid = add_store()
    site = tn_site([f"p{i}" for i in range(6)])
    for i in range(6):
        site.add(tn_product(f"p{i}")[0], "forbidden", status=403)
    rep = await run_index(sid, site)
    assert rep.status == "aborted" and "403" in rep.message and rep.fetched == 3
    assert all(not r.dead for r in items(sid).values())


async def test_network_errors_in_a_row_stop_the_pass_without_marking_dead(store_db):
    sid = add_store()
    site = tn_site([f"p{i}" for i in range(8)])
    for i in range(8):
        site.errors[tn_product(f"p{i}")[0]] = httpx.ConnectTimeout("timeout")
    rep = await run_index(sid, site)
    assert rep.status == "aborted" and rep.fetched == store_catalog.MAX_CONSECUTIVE_TRANSIENT
    assert all(not r.dead and r.fails == 0 for r in items(sid).values())


async def test_an_isolated_network_error_does_not_stop_anything(store_db):
    sid = add_store()
    site = tn_site(["a", "b", "c"])
    site.errors[tn_product("b")[0]] = httpx.ReadTimeout("lento")
    rep = await run_index(sid, site)
    assert rep.status == "ok" and rep.ok == 2 and rep.transient == 1


@pytest.mark.parametrize("status", [500, 503, None])
async def test_robots_unreachable_means_no_crawling(store_db, status):
    sid = add_store()
    site = tn_site(["a"])
    if status is None:
        site.errors[f"{fx.CP}/robots.txt"] = httpx.ConnectError("caído")
    else:
        site.add(f"{fx.CP}/robots.txt", "boom", status=status)
    rep = await run_index(sid, site)
    assert rep.status == "aborted" and rep.fetched == 0
    assert site.fetched_pages() == [] and f"{fx.CP}/sitemap.xml" not in site.urls


async def test_missing_robots_means_everything_is_allowed(store_db):
    sid = add_store()
    site = tn_site(["a"])
    site.add(f"{fx.CP}/robots.txt", "not found", status=404)
    assert (await run_index(sid, site)).ok == 1


async def test_a_page_forbidden_by_robots_after_being_indexed_is_not_requested_again(store_db, _clock):
    sid = add_store(refresh_days=1)
    site = tn_site(["a", "b"])
    await run_index(sid, site)
    _clock["now"] += timedelta(days=2)
    site.add(f"{fx.CP}/robots.txt", ROBOTS_TN + f"Disallow: {tn_product('b')[0].removeprefix(fx.CP)}\n")
    site.requests.clear()
    await run_index(sid, site)
    assert tn_product("b")[0] not in site.urls


# ─── sitemap ─────────────────────────────────────────────────────────────────


async def test_urls_that_leave_the_sitemap_stop_being_read_and_come_back(store_db, _clock):
    sid = add_store(refresh_days=1)
    site = tn_site(["a", "b"])
    await run_index(sid, site)
    _clock["now"] += timedelta(days=2)
    site.add(f"{fx.CP}/sitemap.xml", fx.urlset(tn_product("a")[0]))
    site.requests.clear()
    rep = await run_index(sid, site)
    assert rep.gone_urls == 1 and not items(sid)[tn_product("b")[0]].in_sitemap
    assert tn_product("b")[0] not in site.urls

    _clock["now"] += timedelta(days=2)
    site.add(f"{fx.CP}/sitemap.xml", fx.urlset(tn_product("a")[0], tn_product("b")[0]))
    rep = await run_index(sid, site)
    assert items(sid)[tn_product("b")[0]].in_sitemap


async def test_a_failed_sitemap_never_marks_urls_as_gone(store_db, _clock):
    sid = add_store(refresh_days=1)
    site = tn_site(["a", "b"])
    await run_index(sid, site)
    _clock["now"] += timedelta(days=2)
    site.add(f"{fx.CP}/sitemap.xml", "boom", status=500)
    rep = await run_index(sid, site)
    assert rep.status == "aborted" and all(r.in_sitemap for r in items(sid).values())


async def test_a_truncated_sitemap_never_marks_urls_as_gone(store_db, monkeypatch, _clock):
    from app.pricing import store_parse

    sid = add_store(refresh_days=1)
    site = tn_site(["a", "b", "c"])
    await run_index(sid, site)
    _clock["now"] += timedelta(days=2)
    monkeypatch.setattr(store_parse, "MAX_SITEMAP_URLS", 2)
    await run_index(sid, site)
    assert all(r.in_sitemap for r in items(sid).values())


# ─── top-up del semáforo y estado ───────────────────────────────────────────


async def test_topup_refreshes_only_stores_with_something_due_and_quota_left(store_db, _clock):
    fresh = add_store(name="Fresca", refresh_days=7)
    full = add_store(name="Llena", base_url="https://www.otra.com.ar", max_pages_per_day=1)
    # "Fresca" ya está al día; "Llena" tiene cosas vencidas pero ya gastó el cupo de hoy.
    site = tn_site(["a"])
    await run_index(fresh, site)
    daily_budget.reserve(f"{store_catalog.PAGES_COUNTER_PREFIX}{full}", 1)
    with Session(engine) as s:
        s.add(StoreCatalogItem(store_id=full, url="https://www.otra.com.ar/productos/x/"))
        s.commit()
    reports = await store_catalog.index_all(only_if_due=True, get=site.get, sleep=site.sleep, monotonic=Clock(), delay=(0, 0))
    assert reports == []

    _clock["now"] += timedelta(days=8)                         # "Fresca" vence y "Llena" estrena cupo (otro día)
    reports = await store_catalog.index_all(only_if_due=True, get=site.get, sleep=site.sleep, monotonic=Clock(), delay=(0, 0))
    assert sorted(r.store for r in reports) == ["Fresca", "Llena"]


async def test_topup_skips_a_store_that_was_blocked_a_moment_ago(store_db):
    sid = add_store()
    site = tn_site(["a", "b"])
    site.add(tn_product("a")[0], "slow", status=429)
    await run_index(sid, site)                                 # termina "aborted"
    assert await store_catalog.index_all(only_if_due=True, get=site.get, sleep=site.sleep,
                                         monotonic=Clock(), delay=(0, 0)) == []


async def test_a_second_pass_of_the_same_store_is_refused_while_one_runs(store_db):
    import asyncio

    sid = add_store()
    site = tn_site(["a", "b", "c"])
    gate = asyncio.Event()
    real_get = site.get

    async def slow(u, **kw):
        await gate.wait()
        return await real_get(u, **kw)

    first = asyncio.create_task(store_catalog.index_store(
        sid, get=slow, sleep=site.sleep, rng=random.Random(1), monotonic=Clock(), delay=(0, 0)))
    await asyncio.sleep(0.05)
    second = await store_catalog.index_store(sid, get=site.get, sleep=site.sleep, monotonic=Clock(), delay=(0, 0))
    assert second.status == "skipped" and "en curso" in second.message
    gate.set()
    assert (await first).status == "ok"


async def test_index_status_counts(store_db, _clock):
    sid = add_store(refresh_days=1)
    site = tn_site(["viva", "muerta", "nunca"])
    site.add(tn_product("muerta")[0], "x", status=500)
    await run_index(sid, site, max_pages=2)
    st = next(x for x in store_catalog.index_status() if x["id"] == sid)
    assert st["urls"] == 3 and st["never_read"] == 1 and st["indexed"] == 1
    assert st["pages_today"] == 2 and st["max_pages_per_day"] == 1000 and st["last_index_status"]


# ─── alta de tiendas ─────────────────────────────────────────────────────────


def test_default_stores_are_seeded_once_and_never_come_back(store_db):
    assert store_catalog.seed_default_stores() == 2
    with Session(engine) as s:
        rows = {r.name: r for r in s.exec(select(MarketStore)).all()}
    assert set(rows) == {"Casa Perfecta", "Gadnic"}
    assert rows["Casa Perfecta"].platform == "tiendanube" and rows["Casa Perfecta"].max_pages_per_day >= 500
    assert rows["Gadnic"].platform == "jsonld_sitemap" and rows["Gadnic"].max_pages_per_day == 2000
    assert "*.bidcom.com.ar" in rows["Gadnic"].image_hosts and rows["Gadnic"].house_brand == "Gadnic"
    assert store_catalog.seed_default_stores() == 0
    store_catalog.delete_store(rows["Gadnic"].id)
    assert store_catalog.seed_default_stores() == 0, "una tienda borrada a propósito no reaparece"
    with Session(engine) as s:
        assert [r.name for r in s.exec(select(MarketStore)).all()] == ["Casa Perfecta"]
        assert s.get(Setting, store_catalog.SEEDED_KEY) is not None


def test_clean_store_fields_validates_what_a_person_types():
    ok = store_catalog.clean_store_fields({"name": "  Mi  Tienda ", "base_url": "https://MiTienda.com.ar/productos/",
                                           "platform": "tiendanube", "refresh_days": "7", "max_pages_per_day": 500,
                                           "image_hosts": "mitienda.com.ar, acdn*.mitiendanube.com", "house_brand": " Propia "})
    assert ok["name"] == "Mi Tienda" and ok["base_url"] == "https://mitienda.com.ar"
    assert ok["image_hosts"] == "mitienda.com.ar,acdn*.mitiendanube.com" and ok["house_brand"] == "Propia"
    bad = [
        {"name": "", "base_url": "https://x.com", "platform": "tiendanube"},
        {"name": "x", "base_url": "http://x.com", "platform": "tiendanube"},
        {"name": "x", "base_url": "https://user:pw@x.com", "platform": "tiendanube"},
        {"name": "x", "base_url": "https://x.com:8443", "platform": "tiendanube"},
        {"name": "x", "base_url": "https://localhost", "platform": "tiendanube"},
        {"name": "x", "base_url": "https://x.com", "platform": "magento"},
        {"name": "x", "base_url": "https://x.com", "platform": "tiendanube", "refresh_days": 0},
        {"name": "x", "base_url": "https://x.com", "platform": "tiendanube", "max_pages_per_day": 10**6},
        {"name": "x", "base_url": "https://x.com", "platform": "tiendanube", "max_pages_per_day": "muchas"},
        {"name": "x", "base_url": "https://x.com", "platform": "tiendanube", "image_hosts": "com.ar"},
        {"name": "x", "base_url": "https://x.com", "platform": "tiendanube", "sitemap_url": "https://evil.com/s.xml"},
    ]
    for payload in bad:
        with pytest.raises(ValueError):
            store_catalog.clean_store_fields(payload)


def test_a_new_tiendanube_store_is_just_a_row(store_db):
    """Agregar otra tienda Tiendanube = cargar una fila: no hace falta código."""
    fields = store_catalog.clean_store_fields({"name": "Otra Tienda", "base_url": "https://www.otra.com.ar",
                                               "platform": "tiendanube"})
    with Session(engine) as s:
        s.add(MarketStore(**fields))
        s.commit()
    [info] = store_catalog.active_stores()
    assert info.name == "Otra Tienda" and info.image_hosts == ("otra.com.ar", "acdn*.mitiendanube.com")
    assert info.max_pages_per_day == 1000 and info.refresh_days == 7
