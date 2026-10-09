"""QA de cierre de feat/semaforo-tiendas, criterio 4: la extracción real sigue andando con los cambios de seguridad.

El indexador corre con `net_guard.safe_get` REAL (httpx + httpcore + sockets, tope de bytes en streaming, gzip de una
capa, redirect_ok, chequeo de la IP del peer) contra un servidor HTTP local que sirve los archivos reales de
`tests/golden` (robots.txt y fichas de Gadnic bajados el 08-oct-2026, robots.txt y sitemap completo de Casa Perfecta).
Es lo que no se puede probar con el `FakeSite` del resto de la suite, que se salta toda esa pila.

Lo que se corrió contra los sitios de verdad (con red, UA HugoPriceBot, ~18 requests por tienda) está en el reporte de QA;
acá queda la parte repetible.
"""

from __future__ import annotations

import os
import re
from datetime import timedelta
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlalchemy import update  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.db.models import MarketStore, StoreCatalogItem  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import store_catalog, store_urls  # noqa: E402
from tests import qa2_http  # noqa: E402
from tests import store_fixtures as fx  # noqa: E402
from tests.store_fixtures import store_db  # noqa: E402,F401  (fixture)
from tests.test_store_catalog import _clock, add_store, items, run_index, tn_product, tn_site  # noqa: E402,F401

GOLDEN = Path(__file__).parent / "golden"
UA = "HugoPriceBot/1.0 (+https://b2box.pro)"
GD_ROBOTS = (GOLDEN / "robots_gadnic_2026-10-08.txt").read_text()
CP_ROBOTS = (GOLDEN / "robots_casaperfecta_2026-10-08.txt").read_text()
CP_SITEMAP = (GOLDEN / "sitemap_casaperfecta_completo_2026-10-08.xml").read_text()
ALLOWED_HEADERS = {"host", "user-agent", "accept", "accept-language", "accept-encoding", "connection", "if-none-match",
                   "if-modified-since"}

# (archivo, precio en centavos, stock, sku, es de verdad un precio dudoso) tal como los leyó el parser el 08-oct-2026
GD_PAGES = {
    "gadnic_tripode_2026-10-08.html": (8_784_900, 10, "TRIPODE3"),
    "gadnic_sin_stock_2026-10-08.html": (None, 0, "FUN00003"),
    "gadnic_real_sdhc32_2026-10-08.html": (4_024_900, 10, "MEM00016"),
    "gadnic_real_camara_pro_hunter_2026-10-08.html": (12_599_900, 0, "MC000099"),
    "gadnic_real_gps_cargo_2026-10-08.html": (344_900, 0, "KGPS002A"),
}


def _page(name: str) -> tuple[str, str]:
    raw = (GOLDEN / name).read_text()
    return raw.split("-->", 1)[0].replace("<!-- URL:", "").strip(), raw


def _gadnic_site(server: qa2_http.Local) -> dict[str, str]:
    """Gadnic como es: robots real, índice de sitemaps → sitemap de productos con URLs buenas, muertas (500), con «?» (vedadas
    por robots) y una que redirige a una URL con «?»."""
    server.route("/robots.txt", GD_ROBOTS, Content_Type="text/plain")
    server.route("/sitemap.xml", fx.sitemap_index(f"{fx.GD}/sitemap/product-pages.xml", f"{fx.GD}/sitemap/blog.xml"),
                 Content_Type="application/xml")
    urls = {}
    for i, name in enumerate(GD_PAGES):
        url, raw = _page(name)
        path = url.removeprefix(fx.GD)
        urls[name] = url
        etag = f'"gd{i}"'

        def serve(seen, raw=raw, etag=etag, i=i):
            if seen.headers.get("if-none-match") == etag:
                return 304, {"ETag": etag}, b""
            if i % 2:
                return 200, {"Content-Type": "text/html", "ETag": etag, "Content-Encoding": "gzip"}, qa2_http.gz(raw)
            return 200, {"Content-Type": "text/html", "ETag": etag, "X-Chunked": "1"}, raw
        server.routes[path] = serve
    dead = f"{fx.GD}/aspiradoras/roomba-650"
    withq = f"{fx.GD}/tripodes/tripod-gadnic-tripode3?utm=1"
    redirecting = f"{fx.GD}/camaras/camara-redirect"
    server.route("/aspiradoras/roomba-650", "Internal Server Error", 500)
    server.redirect("/camaras/camara-redirect", "/tripodes/tripod-gadnic-tripode3?utm=1", 301)
    server.route("/sitemap/product-pages.xml",
                 fx.urlset(*urls.values(), dead, withq, redirecting), Content_Type="application/xml")
    return {"dead": dead, "withq": withq, "redirecting": redirecting, **urls}


def _add_gadnic() -> int:
    return add_store(name="Gadnic", base_url=fx.GD, platform="jsonld_sitemap", refresh_days=11, max_pages_per_day=2000,
                     image_hosts="gadnic.com.ar,*.bidcom.com.ar", house_brand="Gadnic")


@pytest.fixture
def server(monkeypatch):
    s = qa2_http.Local().start()
    s.install(monkeypatch)
    yield s
    s.stop()


async def test_gadnic_through_the_real_http_stack_reads_the_real_pages_as_the_live_run_did(store_db, server):
    sid = _add_gadnic()
    urls = _gadnic_site(server)
    report = await store_catalog.index_store(sid, delay=(0.0, 0.0))
    assert report.status == "ok" and report.health == "ok", report.summary()
    assert report.ok == len(GD_PAGES) and report.failed == 2         # la muerta y la que redirige a «?»
    rows = items(sid)
    for name, (price, stock, sku) in GD_PAGES.items():
        r = rows[urls[name]]
        assert (r.price_cents, r.stock, r.sku, r.brand, r.price_doubtful) == (price, stock, sku, "Gadnic", False), name
        assert r.title and r.last_seen_at is not None and r.fails == 0
        assert r.image_url and store_urls.safe_image(r.image_url, ("gadnic.com.ar", "*.bidcom.com.ar")) == r.image_url
        assert r.image_url.startswith("https://static.bidcom.com.ar/publicacionesML/productos/")
    assert rows[urls["dead"]].fails == 1 and not rows[urls["dead"]].dead, "una 500 sola no la mata"
    assert rows[urls["redirecting"]].fail_reason == "redirige fuera del sitio o a una URL vedada"
    assert urls["withq"] not in rows, "las URLs con «?» ni se guardan (robots las veda)"
    paths = server.paths()
    assert not any("?" in p for p in paths), f"se pidió una URL con «?»: {[p for p in paths if '?' in p]}"
    assert paths[0] == "/robots.txt" and paths[1] == "/sitemap.xml" and "/sitemap/product-pages.xml" in paths
    assert "/sitemap/blog.xml" not in paths, "el sitemap de blog no es de productos"
    assert len(paths) == 3 + len(GD_PAGES) + 2, "robots + índice + productos + fichas + muerta + redirigida"


async def test_every_request_to_a_store_is_honest_and_carries_nothing_personal(store_db, server):
    sid = _add_gadnic()
    _gadnic_site(server)
    await store_catalog.index_store(sid, delay=(0.0, 0.0))
    assert server.requests
    for r in server.requests:
        assert r.headers["user-agent"] == UA, r.path
        assert set(r.headers) <= ALLOWED_HEADERS, f"{r.path}: headers inesperados {set(r.headers) - ALLOWED_HEADERS}"
        assert r.headers["host"] == "www.gadnic.com.ar"
        assert r.headers["accept-encoding"] == "gzip"
        blob = " ".join(r.headers.values()).lower()
        assert "@" not in blob and "cookie" not in r.headers and "authorization" not in r.headers and "referer" not in r.headers
        assert "tech@b2box" not in blob and "python-httpx" not in blob and "mozilla" not in blob


async def test_the_second_pass_uses_conditional_gets_and_the_304_keeps_the_data(store_db, server, _clock):
    sid = _add_gadnic()
    urls = _gadnic_site(server)
    await store_catalog.index_store(sid, delay=(0.0, 0.0))
    before = {u: (r.title, r.price_cents, r.stock) for u, r in items(sid).items() if r.last_seen_at}
    n = len(server.requests)
    _clock["now"] += timedelta(days=12)                    # pasó refresh_days (11)
    report = await store_catalog.index_store(sid, delay=(0.0, 0.0))
    second = server.requests[n:]
    cond = [r for r in second if "if-none-match" in r.headers]
    assert len(cond) == len(GD_PAGES), "cada ficha ya leída se pide con If-None-Match"
    assert report.not_modified == len(GD_PAGES) and report.ok == 0
    after = {u: (r.title, r.price_cents, r.stock) for u, r in items(sid).items() if r.last_seen_at}
    assert after == before
    assert not any("?" in r.path for r in second) and urls["dead"].endswith("roomba-650")


# ─── Casa Perfecta: sus fichas dan HTTP 500 ──────────────────────────────────


def _cp_down(server: qa2_http.Local) -> list[str]:
    """Casa Perfecta como está hoy: robots y sitemap bien, y TODA ficha de producto en HTTP 500."""
    server.route("/robots.txt", CP_ROBOTS, Content_Type="text/plain")
    server.route("/sitemap.xml", CP_SITEMAP, Content_Type="application/xml")
    server.default = (500, {"Content-Type": "text/html"}, "<html>Inconvenientes con el servidor</html>")
    return [u for u in re.findall(r"<loc>(.*?)</loc>", CP_SITEMAP)
            if "/productos/" in u and "?" not in u and u != f"{fx.CP}/productos/"]


async def test_casa_perfecta_down_is_cut_as_caida_after_25_server_errors_and_nothing_is_marked_dead(store_db, server):
    sid = add_store()
    products = _cp_down(server)
    assert len(products) == 152, "el sitemap real de hoy tiene 152 productos: más que el corte"
    report = await store_catalog.index_store(sid, delay=(0.0, 0.0))
    assert (report.status, report.health) == ("aborted", "caida"), report.summary()
    assert report.fetched == store_catalog.OUTAGE_STREAK == 25 and report.server_errors == 25
    assert report.ok == 0 and report.newly_dead == 0
    paths = server.paths()
    assert len([p for p in paths if p.startswith("/productos/")]) == 25, "ni una ficha más después del corte"
    assert paths[:2] == ["/robots.txt", "/sitemap.xml"] and len(paths) == 27
    assert not any("/search" in p or "?" in p for p in paths), "el sitemap real trae URLs de búsqueda: no se piden"
    rows = items(sid)
    assert len(rows) == len(products)
    assert sum(1 for r in rows.values() if r.fails == 1) == 25 and not any(r.dead for r in rows.values())
    assert all(r.fail_reason == "HTTP 500" for r in rows.values() if r.fails)
    with Session(engine) as s:
        store = s.get(MarketStore, sid)
        assert store.health == "caida" and store.last_index_status.startswith("aborted")
    status = next(x for x in store_catalog.index_status() if x["id"] == sid)
    assert (status["health"], status["failing"], status["errors_5xx"], status["dead"], status["indexed"]) == ("caida", 25, 25, 0, 0)
    assert all(r.headers["user-agent"] == UA for r in server.requests)


async def test_a_down_store_keeps_being_cut_every_night_and_the_pages_read_are_never_the_same_25_twice_in_a_row(store_db, server, _clock):
    sid = add_store()
    _cp_down(server)
    seen_by_night = []
    for night in range(3):
        n = len(server.requests)
        report = await store_catalog.index_store(sid, delay=(0.0, 0.0))
        assert (report.status, report.health, report.fetched, report.newly_dead) == ("aborted", "caida", 25, 0), night
        seen_by_night.append({r.path for r in server.requests[n:] if r.path.startswith("/productos/")})
        _clock["now"] += timedelta(days=1, minutes=5)
        with Session(engine) as s:                               # el cooldown de 6 h del top-up no aplica al job de la noche
            s.execute(update(MarketStore).values(last_indexed_at=None))
            s.commit()
    assert all(len(x) == 25 for x in seen_by_night)
    assert not (seen_by_night[0] & seen_by_night[1]) and not (seen_by_night[1] & seen_by_night[2]), \
        "primero las que nunca se leyeron: cada noche son 25 fichas nuevas"
    assert not any(r.dead for r in items(sid).values())


async def test_when_the_store_comes_back_the_next_pass_reads_pages_again_and_clears_the_outage(store_db, server, _clock):
    sid = add_store()
    products = _cp_down(server)
    await store_catalog.index_store(sid, delay=(0.0, 0.0))
    _clock["now"] += timedelta(days=1, minutes=5)
    for u in products[:60]:                                  # vuelve la tienda: sus fichas contestan bien
        path = u.removeprefix(fx.CP)
        server.route(path, fx.tiendanube_page(u, "Producto " + path.strip("/"), [fx.variant(11000)], sku=path.strip("/")[:20]),
                     Content_Type="text/html")
    report = await store_catalog.index_store(sid, delay=(0.0, 0.0), max_pages=60)
    assert report.health == "ok" and report.ok >= 25 and report.status == "ok", report.summary()
    with Session(engine) as s:
        assert s.get(MarketStore, sid).health == "ok"


async def test_a_five_hundred_outage_that_lasts_more_than_a_week_ends_up_marking_the_pages_dead(store_db, server, _clock):
    """No es un error de la regla («2 fallos y 3 días»), es lo que implica: con la tienda caída más de ~7 días cada ficha se
    pide dos veces con más de 3 días de distancia y queda `dead` 30 días, aunque la tienda vuelva antes. Queda documentado."""
    sid = add_store()
    _cp_down(server)
    dead_by_day = {}
    for day in range(10):
        with Session(engine) as s:
            s.execute(update(MarketStore).values(last_indexed_at=None))
            s.commit()
        await store_catalog.index_store(sid, delay=(0.0, 0.0))
        dead_by_day[day] = sum(1 for r in items(sid).values() if r.dead)
        _clock["now"] += timedelta(days=1, minutes=5)
    print("muertas por día de caída:", dead_by_day)
    assert dead_by_day[0] == dead_by_day[1] == dead_by_day[2] == 0
    assert dead_by_day[5] == 0 and dead_by_day[6] > 0, "las primeras mueren al releerse, con más de 3 días de distancia (día 6)"
    assert dead_by_day[9] > 0, "con 10 días de caída las fichas ya están muertas"
    assert max(dead_by_day.values()) <= 152


async def test_failing_pages_are_retried_after_1_then_2_days_even_when_a_real_pass_takes_hours(store_db, _clock, monkeypatch):
    """Una tienda viva con fichas que dan 500, con un cron diario y una pasada que tarda lo que tarda de verdad."""
    sid = add_store(refresh_days=7, max_pages_per_day=1000)
    slugs = ["viva"] + [f"p{i}" for i in range(100)]
    site = tn_site(slugs)
    for slug in slugs[1:]:
        site.add(tn_product(slug)[0], "boom", status=500)
    start = _clock["now"]
    tick = {"t": start}

    def now():                                             # cada sello de tiempo del indexador cuesta 3 s de reloj
        tick["t"] += timedelta(seconds=3)
        return tick["t"]

    monkeypatch.setattr(store_catalog, "utcnow", now)
    days_read: list[int] = []
    for day in range(6):
        tick["t"] = start + timedelta(days=day)
        before = len(site.requests)
        await run_index(sid, site)
        if any(u.endswith("/productos/p99/") for u, _h in site.requests[before:]):
            days_read.append(day)
    assert days_read == [0, 1, 3], f"la última ficha se leyó los días {days_read}"
