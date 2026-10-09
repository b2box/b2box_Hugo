"""Tercera ronda de seguridad / QA de las tiendas: una excepción rara de UNA ficha no termina la pasada, las
fotos y los productos tienen tope de tiempo, el robots se lee por prefijo del token, los sufijos públicos
(com.uy, co.nz…) no se aceptan como tienda, el cupo y el descanso son por sitio y un sitemap vacío o a
medias nunca da URLs por «ya no están»."""

from __future__ import annotations

import asyncio
import gzip
import os
import random
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import net_guard, runtime  # noqa: E402
from app.db.models import MarketStore, StoreMatchFeedback  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import daily_budget, store_catalog, store_match, store_parse, store_robots, store_urls  # noqa: E402
from tests import store_fixtures as fx  # noqa: E402
from tests.store_fixtures import Clock, FakeSite, store_db  # noqa: E402,F401  (fixture)
from tests.test_price_monitor_routes import _env, client  # noqa: E402,F401
from tests.test_store_catalog import _clock, add_store, items, run_index, tn_product, tn_site  # noqa: E402,F401
from tests.test_store_hardening import seeded  # noqa: E402,F401  (fixture)
from tests.test_price_monitor import world  # noqa: E402,F401  (fixture)
from tests.test_store_match import _rows, _snap, price_monitor, stores_world  # noqa: E402,F401  (fixture)


# ─── N1: una ficha que levanta una excepción rara no termina la pasada ──────────────


def _patch_http(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(net_guard.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(net_guard, "assert_public_url", lambda url: None)
    monkeypatch.setattr(net_guard, "assert_peer_public", lambda resp: None)


class _Raw(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for c in self.chunks:
            yield c


async def _pass(sid, monkeypatch, pages: dict[str, httpx.Response | Exception | tuple], **kw):
    """Una pasada con la pila HTTP real (safe_get) y un sitio de mentira; `pages[path]` es la respuesta."""
    slugs = [p for p in pages if p.startswith("/productos/")]
    base = {"/robots.txt": (200, b"User-agent: *\nDisallow: /search/\n", {}),
            "/sitemap.xml": (200, fx.urlset(*[f"{fx.CP}{p}" for p in slugs]).encode(), {})}

    def handler(request: httpx.Request) -> httpx.Response:
        spec = pages.get(request.url.path) or base.get(request.url.path) or (404, b"no", {})
        if isinstance(spec, Exception):
            raise spec
        status, body, headers = spec
        return httpx.Response(status, headers=headers, stream=_Raw(body), request=request)

    for path, spec in base.items():
        pages.setdefault(path, spec)
    _patch_http(monkeypatch, handler)
    return await store_catalog.index_store(sid, sleep=lambda s: asyncio.sleep(0), rng=random.Random(1),
                                           monotonic=Clock(), delay=(0.0, 0.0), **kw)


def _ok_page(slug: str) -> tuple[int, bytes, dict]:
    return 200, tn_product(slug)[1].encode(), {"content-type": "text/html; charset=utf-8"}


async def test_a_page_that_claims_gzip_but_is_not_is_a_strike_and_the_pass_goes_on(store_db, monkeypatch):
    sid = add_store()
    pages = {"/productos/a/": (200, b"<html>no soy gzip</html>", {"content-encoding": "gzip"}),
             "/productos/b/": _ok_page("b")}
    report = await _pass(sid, monkeypatch, pages)
    got = items(sid)
    assert report.status == "ok" and report.ok == 1 and report.failed == 1, report.summary()
    bad = got[tn_product("a")[0]]
    assert bad.fails == 1 and bad.last_checked_at is not None, "queda marcada como leída: no vuelve a ser la primera cada noche"
    assert got[tn_product("b")[0]].title


@pytest.mark.parametrize("charset", ["utf-16", "rot13", "idna", "base64", "zlib", "no-existe", "x" * 500])
async def test_a_weird_declared_charset_does_not_break_the_page(store_db, monkeypatch, charset):
    sid = add_store()
    status, body, _h = _ok_page("a")
    pages = {"/productos/a/": (status, body, {"content-type": f"text/html; charset={charset}"})}
    report = await _pass(sid, monkeypatch, pages)
    assert report.status == "ok" and report.ok == 1, report.summary()
    assert items(sid)[tn_product("a")[0]].title


async def test_headers_that_are_not_ascii_do_not_break_the_store_pages(store_db, monkeypatch):
    sid = add_store()
    status, body, _h = _ok_page("a")
    pages = {"/productos/a/": (status, body, [(b"content-type", b"text/html"), (b"etag", b'"\xff\xfe"'),
                                              (b"content-disposition", 'inline; filename="ácido.html"'.encode("latin-1"))])}
    report = await _pass(sid, monkeypatch, pages)
    assert report.status == "ok" and report.ok == 1, report.summary()
    assert items(sid)[tn_product("a")[0]].etag


async def test_an_unexpected_exception_reading_one_page_does_not_end_the_pass(store_db, monkeypatch):
    sid = add_store()
    real = store_parse.parse_product_page

    def flaky(platform, page, urls):
        if urls[0].endswith("/productos/a/"):
            raise RuntimeError("bug del parser")
        return real(platform, page, urls)

    monkeypatch.setattr(store_parse, "parse_product_page", flaky)
    report = await _pass(sid, monkeypatch, {"/productos/a/": _ok_page("a"), "/productos/b/": _ok_page("b")})
    got = items(sid)
    assert report.status == "ok" and report.ok == 1 and report.failed == 1
    assert got[tn_product("a")[0]].fails == 1 and got[tn_product("a")[0]].last_checked_at is not None
    assert "RuntimeError" in got[tn_product("a")[0]].fail_reason and got[tn_product("b")[0]].title


async def test_an_unexpected_exception_from_the_network_layer_is_transient_and_the_pass_goes_on(store_db, monkeypatch):
    sid = add_store()
    pages = {"/productos/a/": ValueError("algo que nadie esperaba"), "/productos/b/": _ok_page("b")}
    report = await _pass(sid, monkeypatch, pages)
    assert report.status == "ok" and report.ok == 1 and report.transient == 1


@pytest.mark.parametrize("what", ["robots", "sitemap"])
async def test_an_unexpected_error_in_robots_or_the_sitemap_aborts_cleanly_not_with_an_error_status(store_db, monkeypatch, what):
    sid = add_store()
    pages = {"/productos/a/": _ok_page("a")}
    pages["/robots.txt" if what == "robots" else "/sitemap.xml"] = ValueError("raro")
    report = await _pass(sid, monkeypatch, pages)
    assert report.status == "aborted" and report.status != "error" and report.fetched == 0


async def test_robots_with_a_weird_charset_is_read(store_db, monkeypatch):
    sid = add_store()
    pages = {"/productos/a/": _ok_page("a"),
             "/robots.txt": (200, b"User-agent: *\nDisallow: /search/\n", {"content-type": "text/plain; charset=rot13"})}
    report = await _pass(sid, monkeypatch, pages)
    assert report.status == "ok" and report.ok == 1


# ─── N3: topes de tiempo por tienda y por producto ───────────────────────────────


async def test_a_store_that_never_answers_is_skipped_and_the_others_still_count(stores_world, monkeypatch):
    """Una foto que gotea o un juez colgado en UNA tienda no trabajan el slot de la corrida."""
    import time

    monkeypatch.setattr(store_match, "STORE_MATCH_TIMEOUT_S", 0.05)
    real = store_match._match_store

    async def slow(run, store, *a, **kw):
        if store.info.name == "Gadnic":
            await asyncio.sleep(30)
        return await real(run, store, *a, **kw)

    monkeypatch.setattr(store_match, "_match_store", slow)
    t0 = time.time()
    result = await price_monitor.run_price_monitor()
    assert result["status"] == "ok" and time.time() - t0 < 10
    assert _rows("1", "Gadnic") == [] and _rows("1", "Casa Perfecta"), "sin Gadnic, pero con Casa Perfecta"
    assert _snap("1").ml_status == "ok", "y lo de Mercado Libre no se toca"


async def test_a_product_stuck_on_all_the_stores_does_not_hold_the_run(stores_world, monkeypatch):
    import time

    monkeypatch.setattr(store_match, "PRODUCT_MATCH_TIMEOUT_S", 0.1)

    async def stuck(*a, **kw):
        await asyncio.sleep(30)

    monkeypatch.setattr(store_match, "_match_store", stuck)
    t0 = time.time()
    result = await price_monitor.run_price_monitor()
    assert result["status"] == "ok" and time.time() - t0 < 10
    assert _rows("1") == [] and _snap("1").color == "verde"


def test_the_time_limits_exist_and_are_sane():
    assert 30 <= store_match.STORE_MATCH_TIMEOUT_S <= 300 and store_match.PRODUCT_MATCH_TIMEOUT_S >= store_match.STORE_MATCH_TIMEOUT_S
    from app.dedup import image_hash

    assert image_hash._DEADLINE_S == 30.0 and image_hash._INTERACTIVE_DEADLINE_S == 12.0


# ─── B1: robots ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("declared", ["HugoPriceBot/1.0", "HugoPriceBot", "hugopricebot (+https://b2box.pro)", "HUGOPRICEBOT/2"])
def test_a_group_declared_with_a_version_or_a_url_is_ours(declared):
    robots = store_robots.parse(f"User-agent: {declared}\nDisallow: /nada/\n\nUser-agent: *\nDisallow:\n",
                                agent="HugoPriceBot/1.0 (+https://b2box.pro)")
    assert not robots.allows("/nada/x") and robots.allows("/otro")


@pytest.mark.parametrize("declared", ["HugoPriceBot2", "Hugo", "PriceBot", "hugopricebots", "o"])
def test_a_group_for_another_token_is_not_ours(declared):
    robots = store_robots.parse(f"User-agent: {declared}\nDisallow: /nada/\n\nUser-agent: *\nDisallow: /star/\n",
                                agent="HugoPriceBot/1.0")
    assert robots.allows("/nada/x") and not robots.allows("/star/x")


def test_the_rules_cap_counts_only_the_groups_that_apply_to_us():
    """Las reglas de otros bots no son nuestras: 2500 de «badbot» antes del `*` no pueden desplazarlo."""
    text = "User-agent: badbot\n" + "".join(f"Disallow: /bad{i}/\n" for i in range(2500)) + \
           "\nUser-agent: *\nDisallow: /secret\n"
    robots = store_robots.parse(text, agent="HugoPriceBot/1.0")
    assert not robots.blocked_all
    assert not robots.allows("/secret") and robots.allows("/ok") and robots.allows("/bad1/")


def test_many_groups_of_other_bots_do_not_displace_the_wildcard_group():
    text = "".join(f"User-agent: bot{b}\n" + "".join(f"Disallow: /b{b}/{i}\n" for i in range(300)) + "\n"
                   for b in range(10)) + "User-agent: *\nDisallow: /secret\n"
    robots = store_robots.parse(text, agent="HugoPriceBot/1.0")
    assert not robots.allows("/secret") and robots.allows("/b3/5")


def test_more_rules_for_us_than_the_cap_is_fail_closed_not_fail_open():
    """Antes se descartaban las que pasaban el tope y /secret quedaba permitido."""
    text = "User-agent: *\n" + "".join(f"Disallow: /p{i}/\n" for i in range(store_robots.MAX_RULES)) + "Disallow: /secret\n"
    robots = store_robots.parse(text, agent="HugoPriceBot/1.0")
    assert robots.blocked_all and not robots.allows("/secret") and not robots.allows("/cualquiera")
    assert "reglas" in robots.reason


def test_exactly_the_cap_of_rules_is_still_read():
    text = "User-agent: *\n" + "".join(f"Disallow: /p{i}/\n" for i in range(store_robots.MAX_RULES - 1)) + "Disallow: /secret\n"
    robots = store_robots.parse(text)
    assert not robots.blocked_all and not robots.allows("/secret") and robots.allows("/otra")


def test_a_very_long_disallow_is_shortened_not_dropped():
    long_path = "/" + "a" * 600
    robots = store_robots.parse(f"User-agent: *\nDisallow: {long_path}$\nAllow: /{'b' * 600}\n")
    assert not robots.allows(long_path) and not robots.allows(long_path + "/mas")
    assert robots.allows("/" + "b" * 600), "un Allow larguísimo se descarta: solo achica lo permitido"


def test_our_own_group_wins_over_the_wildcard_even_with_other_groups_in_between():
    text = "User-agent: *\nDisallow: /todo\n\nUser-agent: otro\nDisallow: /\n\nUser-agent: HugoPriceBot\nDisallow: /solo-esto\n"
    robots = store_robots.parse(text, agent="HugoPriceBot/1.0")
    assert not robots.allows("/solo-esto") and robots.allows("/todo")


async def test_a_robots_over_the_rules_cap_aborts_the_pass_and_says_why(store_db, monkeypatch):
    sid = add_store()
    big = ("User-agent: *\n" + "".join(f"Disallow: /p{i}/\n" for i in range(store_robots.MAX_RULES + 5))).encode()
    report = await _pass(sid, monkeypatch, {"/productos/a/": _ok_page("a"), "/robots.txt": (200, big, {})})
    assert report.status == "aborted" and report.fetched == 0
    assert "reglas" in report.message and "no se rastrea" in report.message and "HTTP" not in report.message


# ─── B2: sufijos públicos ────────────────────────────────────────────────────────

PUBLIC_SUFFIXES = ["com.uy", "com.pe", "co.nz", "org.uk", "co.uk", "com.au", "gob.ar", "gov.uk", "co.jp", "com.br", "com.mx",
                   "net.ar", "org.ar", "edu.ar", "com.co", "com.ve", "com.py", "com.bo", "com.ec", "co.za", "ac.uk", "gob.mx",
                   "gub.uy", "com.cn", "ne.jp", "or.jp", "com.tr", "com.hk", "co.il", "com.sg", "uy", "ar", "nz", "uk"]


@pytest.mark.parametrize("suffix", PUBLIC_SUFFIXES)
def test_public_suffixes_are_not_accepted_as_a_store_address(suffix):
    with pytest.raises(ValueError):
        store_catalog._clean_base_url(f"https://{suffix}")
    with pytest.raises(ValueError):
        store_catalog._clean_base_url(f"https://www.{suffix}")
    assert store_urls.parse_hosts(f"*.{suffix}, {suffix}") == ()
    assert not store_urls.valid_hostname(suffix)


@pytest.mark.parametrize("domain", ["tienda.com.uy", "mitienda.uy", "casaperfecta.com.ar", "tienda.ar", "gadnic.com.ar",
                                    "shop.co.nz", "algo.org.uk", "x1.com.pe"])
def test_real_store_domains_under_those_suffixes_are_fine(domain):
    assert store_catalog._clean_base_url(f"https://www.{domain}") == f"https://www.{domain}"


# ─── B3: el cupo y el descanso son por sitio ─────────────────────────────────────


def test_deleting_and_recreating_a_store_does_not_reset_its_daily_quota(store_db):
    first = add_store(name="Una")
    key = store_catalog.get_store(first).counter_key
    assert daily_budget.reserve(key, 100) and daily_budget.reserve(key, 100)
    assert store_catalog.delete_store(first)
    again = add_store(name="Otra con la misma dirección")
    assert store_catalog.get_store(again).counter_key == key
    assert store_catalog.pages_used_today(store_catalog.get_store(again)) == 2


@pytest.mark.usefixtures("seeded")
def test_deleting_and_recreating_a_store_does_not_skip_the_manual_cooldown(client, monkeypatch):
    async def fake_index(store_id, **kw):
        return None

    monkeypatch.setattr(store_catalog, "index_store", fake_index)
    with Session(engine) as s:
        cp = int(s.exec(select(MarketStore.id).where(MarketStore.name == "Casa Perfecta")).one())
        store_catalog._record_pass("casaperfecta.com.ar", store_catalog.utcnow() - timedelta(minutes=1))
    client.delete(f"/api/stores/{cp}")
    new = client.post("/api/stores", json={"name": "Casa Perfecta 2", "base_url": "https://www.casaperfecta.com.ar",
                                           "platform": "tiendanube"})
    assert new.status_code == 201
    r = client.post(f"/api/stores/{new.json()['id']}/index")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0


async def test_a_pass_cut_because_the_store_was_deleted_still_records_the_site_pass(store_db):
    """R2: sin esto, borrar la tienda en medio de una pasada dejaba el descanso del sitio sin anotar."""
    sid = add_store()
    apex = store_catalog.get_store(sid).apex
    site = tn_site([f"p{i}" for i in range(6)])
    seen: list[str] = []

    def on_request(url):
        if not url.endswith((".xml", "robots.txt")):
            seen.append(url)
            if len(seen) == 2:
                assert store_catalog.delete_store(sid)

    site.on_request = on_request
    assert store_catalog.last_pass_at(apex) is None
    report = await run_index(sid, site)
    assert report.status == "aborted" and len(site.fetched_pages()) == 2
    assert store_catalog.get_store(sid) is None
    assert store_catalog.last_pass_at(apex) is not None


@pytest.mark.usefixtures("seeded")
def test_index_delete_recreate_index_cycle_hits_the_cooldown(client):
    """Indexar, borrar en medio, recrear la misma dirección e indexar de nuevo: el descanso del sitio sigue."""
    with Session(engine) as s:
        cp = int(s.exec(select(MarketStore.id).where(MarketStore.name == "Casa Perfecta")).one())
    site = tn_site([f"p{i}" for i in range(6)])
    seen: list[str] = []

    def on_request(url):
        if not url.endswith((".xml", "robots.txt")):
            seen.append(url)
            if len(seen) == 2:
                assert client.delete(f"/api/stores/{cp}").status_code == 200

    site.on_request = on_request
    report = asyncio.run(run_index(cp, site))
    assert report.status == "aborted"
    new = client.post("/api/stores", json={"name": "Casa Perfecta de nuevo", "base_url": fx.CP, "platform": "tiendanube"})
    assert new.status_code == 201
    r = client.post(f"/api/stores/{new.json()['id']}/index")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0


async def test_the_indexing_lock_is_per_site_so_a_recreated_store_cannot_run_in_parallel(store_db):
    first = add_store(name="Una")
    info = store_catalog.get_store(first)
    assert not store_catalog.is_indexing(info)
    lock = store_catalog._locks.setdefault(store_catalog.lock_key(info), asyncio.Lock())
    await lock.acquire()
    try:
        # Otro id para el mismo sitio (lo que pasa al borrar y recrear en Postgres, donde los ids no se reusan).
        again = add_store(name="Otra con la misma dirección")
        assert again != first and store_catalog.is_indexing(store_catalog.get_store(again))
        report = await store_catalog.index_store(again)
        assert report.status == "skipped" and "en curso" in report.message
    finally:
        lock.release()
        store_catalog._locks.pop(store_catalog.lock_key(info), None)


# ─── B4: sitemaps vacíos o a medias nunca dan URLs por «ya no están» ─────────────


async def test_an_empty_child_sitemap_never_marks_its_urls_as_gone(store_db, _clock):
    gd = fx.GD
    sid = add_store(name="Gadnic", base_url=gd, platform="jsonld_sitemap", refresh_days=1)
    site = FakeSite()
    site.add(f"{gd}/robots.txt", "User-agent: *\nDisallow: /*?\n")
    site.add(f"{gd}/sitemap.xml", fx.sitemap_index(f"{gd}/sitemap/product-pages-1.xml", f"{gd}/sitemap/product-pages-2.xml"))
    site.add(f"{gd}/sitemap/product-pages-1.xml", fx.urlset(*[f"{gd}/cat/a{i}" for i in range(3)]))
    site.add(f"{gd}/sitemap/product-pages-2.xml", fx.urlset(*[f"{gd}/cat/b{i}" for i in range(3)]))
    await run_index(sid, site, max_pages=1)
    assert len(items(sid)) == 6
    _clock["now"] += timedelta(days=2)
    site.add(f"{gd}/sitemap/product-pages-2.xml", fx.urlset())                  # el segundo vuelve vacío (un 200 raro)
    report = await run_index(sid, site, max_pages=1)
    assert all(r.in_sitemap for r in items(sid).values()), "un sitemap vacío no da URLs por «ya no están»"
    assert report.gone_urls == 0


async def test_a_flat_sitemap_that_comes_back_empty_does_not_empty_the_catalog(store_db, _clock):
    sid = add_store(refresh_days=1)
    site = tn_site(["a", "b"])
    await run_index(sid, site, max_pages=1)
    _clock["now"] += timedelta(days=2)
    site.add(f"{fx.CP}/sitemap.xml", fx.urlset())
    await run_index(sid, site, max_pages=1)
    assert all(r.in_sitemap for r in items(sid).values())


def test_a_multi_member_gz_sitemap_is_not_read_as_if_it_were_complete():
    two = gzip.compress(fx.urlset("https://x.com/a").encode()) + gzip.compress(fx.urlset("https://x.com/b").encode())
    assert store_parse.decode_sitemap_body(two) == ""
    assert "https://x.com/a" in store_parse.decode_sitemap_body(gzip.compress(fx.urlset("https://x.com/a").encode()))


# ─── B5: patrones que no cruzan puntos, deshacer con actor ───────────────────────


@pytest.mark.parametrize("host,ok", [
    ("acdn-us.mitiendanube.com", True), ("acdn.mitiendanube.com", True), ("acdn2.mitiendanube.com", True),
    ("acdn.evil.mitiendanube.com", False), ("acdn-us.evil.mitiendanube.com", False), ("acdn-us.mitiendanube.com.evil.com", False),
    ("xacdn.mitiendanube.com", False), ("mitiendanube.com", False), ("tienda.mitiendanube.com", False),
])
def test_the_tiendanube_cdn_pattern_does_not_cross_dots(host, ok):
    assert store_urls.host_matches(host, "acdn*.mitiendanube.com") is ok
    assert (store_urls.safe_image(f"https://{host}/a.webp", ("acdn*.mitiendanube.com",)) is not None) is ok


@pytest.mark.usefixtures("seeded")
def test_undoing_a_label_records_who_undid_it(client, caplog):
    import logging

    from app.db.models import MarketPriceSnapshot, PriceMonitorRun, StoreMatch

    with Session(engine) as s:
        cp = int(s.exec(select(MarketStore.id).where(MarketStore.name == "Casa Perfecta")).one())
        run = PriceMonitorRun(status="ok", total_products=1)
        s.add(run)
        s.commit()
        s.refresh(run)
        s.add(MarketPriceSnapshot(run_id=run.id, product_id="1", color="sin_dato", ml_status="no_data", our_price_cents=10_000))
        m = StoreMatch(run_id=run.id, product_id="1", store_id=cp, item_id=1, category="diferente", auto_category="diferente",
                       source="veto", title="x", url=f"{fx.CP}/productos/x/")
        s.add(m)
        s.commit()
        s.refresh(m)
        mid = m.id
    assert client.post(f"/api/price-monitor/store-matches/{mid}/label", json={"label": "no_es"}).status_code == 200
    with Session(engine) as s:
        fb = s.exec(select(StoreMatchFeedback)).one()
        fb.actor = "pao@b2box.pro"                                   # la marcó otra persona
        s.add(fb)
        s.commit()
    assert client.post(f"/api/price-monitor/store-matches/{mid}/label", json={"label": "es"}).status_code == 200
    with caplog.at_level(logging.INFO, logger="app.api.store_routes"):
        assert client.delete(f"/api/price-monitor/store-matches/{mid}/label").status_code == 200
    with Session(engine) as s:
        fb = s.exec(select(StoreMatchFeedback)).one()
        assert (fb.label, fb.previous_label, fb.actor) == ("no_es", None, "admin"), "el que deshace queda como autor de la marca restaurada"
    assert "admin deshizo la marca" in caplog.text


# ─── QA: texto con bytes NUL ─────────────────────────────────────────────────────


@pytest.mark.usefixtures("seeded")
@pytest.mark.parametrize("field", ["name", "house_brand", "notes"])
def test_a_nul_byte_in_a_text_field_of_the_store_form_is_cleaned(client, field):
    body = {"name": "Con NUL", "base_url": "https://www.connul.com.ar", "platform": "tiendanube", field: "ab\x00cd\x07ef"}
    r = client.post("/api/stores", json=body)
    assert r.status_code == 201, r.text
    assert "\x00" not in r.text and "\x07" not in r.text
    assert (r.json()[field]) == "ab cd ef" or field == "name" and r.json()["name"] == "Con NUL"


async def test_a_multi_member_gzip_sitemap_on_the_wire_never_marks_urls_as_gone(store_db, monkeypatch, _clock):
    sid = add_store(refresh_days=1)
    await run_index(sid, tn_site(["a", "b"]), max_pages=1)
    _clock["now"] += timedelta(days=2)
    two = gzip.compress(fx.urlset(tn_product("a")[0]).encode()) + gzip.compress(fx.urlset(tn_product("b")[0]).encode())
    pages = {"/sitemap.xml": (200, two, {"content-encoding": "gzip"}), "/productos/a/": _ok_page("a")}
    report = await _pass(sid, monkeypatch, pages, max_pages=1)
    assert report.status == "aborted" and report.gone_urls == 0
    assert all(r.in_sitemap for r in items(sid).values())
