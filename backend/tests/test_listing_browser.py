"""ListingBrowser: un Camoufox para muchas páginas de listado. El navegador es
un doble (nunca se levanta Firefox); lo que se prueba es que reusa el MISMO
lanzamiento, proxy y guard que `render()`, que corta imágenes/fuentes/media,
que mide bytes, que se relanza y que degrada sin romper a nadie."""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.ingest import browser_fetch  # noqa: E402
from app.net_guard import SsrfBlocked  # noqa: E402

URL = "https://listado.mercadolibre.com.ar/organizador-de-cables"


class _Req:
    def __init__(self, url: str, rtype: str = "document", size: int = 0):
        self.url, self.resource_type, self._size = url, rtype, size

    async def sizes(self):
        return {"requestHeadersSize": 100, "requestBodySize": 0,
                "responseHeadersSize": 400, "responseBodySize": self._size}


class _Route:
    def __init__(self):
        self.continued = self.aborted = False

    async def continue_(self):
        self.continued = True

    async def abort(self):
        self.aborted = True


class _Resp:
    def __init__(self, status):
        self.status = status


class _Page:
    def __init__(self, world, ctx):
        self.w, self.ctx, self.url, self.closed, self.handlers = world, ctx, URL, False, {}

    def on(self, event, cb):
        self.handlers[event] = cb

    async def goto(self, url, wait_until, timeout):
        self.w.visits.append(url)
        self.w.goto_args.append((wait_until, timeout))
        if self.w.goto_error:
            raise self.w.goto_error
        self.url = self.w.final_url or url
        # El browser pide el documento y subrecursos; el handler del contexto
        # decide si siguen o se abortan.
        for req in self.w.subrequests:
            route = _Route()
            await self.ctx.route_handler(route, req)
            self.w.routed.append((req.url, req.resource_type, route))
            if route.continued:
                self.handlers["requestfinished"](req)
        return _Resp(self.w.status)

    async def content(self):
        return self.w.html

    async def close(self):
        self.closed = True


class _Context:
    def __init__(self, world):
        self.w, self.route_handler = world, None

    async def route(self, pattern, handler):
        assert pattern == "**/*"
        self.route_handler = handler

    async def new_page(self):
        page = _Page(self.w, self)
        self.w.pages.append(page)
        return page


class _Browser:
    def __init__(self, world):
        self.w = world

    async def new_context(self):
        ctx = _Context(self.w)
        self.w.contexts.append(ctx)
        return ctx


class _World:
    def __init__(self):
        self.launches: list[dict] = []
        self.closed = 0
        self.visits, self.goto_args, self.routed, self.pages, self.contexts = [], [], [], [], []
        self.subrequests = [
            _Req(URL, "document", 300_000),
            _Req("https://http2.mlstatic.com/app.js", "script", 150_000),
            _Req("https://http2.mlstatic.com/foto.jpg", "image", 80_000),
            _Req("https://http2.mlstatic.com/f.woff2", "font", 30_000),
            _Req("https://http2.mlstatic.com/v.mp4", "media", 900_000),
        ]
        self.html, self.status, self.final_url, self.goto_error = "<html>ok</html>", 200, None, None
        self.launch_error = None


@pytest.fixture
def world(monkeypatch):
    w = _World()

    class FakeCamoufox:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            if w.launch_error:
                raise w.launch_error
            w.launches.append(self.kwargs)
            return _Browser(w)

        async def __aexit__(self, *exc):
            w.closed += 1

    async def public(host):  # noqa: ARG001
        return True

    monkeypatch.setattr(browser_fetch, "_camoufox", lambda: FakeCamoufox)
    monkeypatch.setattr(browser_fetch, "available", lambda: True)
    monkeypatch.setattr(browser_fetch, "assert_public_url", lambda url: None)
    monkeypatch.setattr(browser_fetch, "_host_is_public", public)
    monkeypatch.setattr(get_settings(), "browser_proxy", "http://usr:pwd@res.proxy.io:8080", raising=False)
    browser_fetch.reset_circuit()
    yield w
    browser_fetch.reset_circuit()


async def test_one_browser_serves_many_pages_through_one_context(world):
    async with browser_fetch.ListingBrowser() as lb:
        for _ in range(3):
            await lb.fetch(URL)
    assert len(world.launches) == 1 and len(world.contexts) == 1 and len(world.pages) == 3
    assert all(p.closed for p in world.pages)
    assert world.closed == 1             # se cierra al salir


async def test_it_launches_exactly_like_render_with_the_same_proxy(world):
    async with browser_fetch.ListingBrowser() as lb:
        await lb.fetch(URL)
    [kwargs] = world.launches
    assert kwargs == browser_fetch._launch_kwargs()
    assert kwargs["proxy"] == {"server": "http://res.proxy.io:8080", "username": "usr", "password": "pwd"}
    assert kwargs["headless"] and kwargs["humanize"] and kwargs["geoip"]


async def test_render_uses_the_same_launch_kwargs(world, monkeypatch):
    """Una sola implementación del lanzamiento (y del proxy): `render()` de una
    ficha y el listado del semáforo salen con los mismos argumentos."""
    captured: dict = {}

    class RPage:
        url = "https://articulo.mercadolibre.com.ar/MLA-1"

        async def route(self, *a):
            return None

        async def goto(self, *a, **k):
            return None

        async def evaluate(self, *a):
            return []

        async def wait_for_timeout(self, *a):
            return None

        async def wait_for_load_state(self, *a, **k):
            return None

        async def title(self):
            return ""

        async def content(self):
            return ""

    class RBrowser:
        async def new_page(self):
            return RPage()

    class RCamoufox:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return RBrowser()

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(browser_fetch, "_camoufox", lambda: RCamoufox)
    await browser_fetch.render("https://articulo.mercadolibre.com.ar/MLA-1")
    assert captured == browser_fetch._launch_kwargs()
    assert captured["proxy"]["server"] == "http://res.proxy.io:8080"


async def test_images_fonts_and_media_are_not_downloaded(world):
    async with browser_fetch.ListingBrowser() as lb:
        page = await lb.fetch(URL)
    aborted = {rtype for _, rtype, route in world.routed if route.aborted}
    followed = {rtype for _, rtype, route in world.routed if route.continued}
    assert aborted == {"image", "font", "media"} and followed == {"document", "script"}
    # Solo se cuentan los bytes de lo que bajó: documento + script.
    assert page.bytes == (100 + 400 + 300_000) + (100 + 400 + 150_000)


async def test_the_ssrf_guard_still_applies_to_every_request(world):
    world.subrequests = [_Req("http://169.254.169.254/latest/meta-data/", "xhr"),
                         _Req("https://listado.mercadolibre.com.ar/x", "document")]

    async def real_dns(host):  # noqa: ARG001
        return not host.startswith("169.")

    browser_fetch._host_ok.clear()
    import app.ingest.browser_fetch as bf
    bf._host_is_public = real_dns            # restaurado por monkeypatch al salir del fixture
    async with browser_fetch.ListingBrowser() as lb:
        page = await lb.fetch(URL)
    blocked = [(u, r.aborted) for u, _, r in world.routed if "169.254" in u]
    assert blocked == [("http://169.254.169.254/latest/meta-data/", True)]
    assert page.blocked == 1


async def test_it_navigates_with_domcontentloaded_and_the_shared_timeout(world):
    async with browser_fetch.ListingBrowser() as lb:
        await lb.fetch(URL)
    assert world.goto_args == [("domcontentloaded", get_settings().browser_fetch_timeout_ms)]


async def test_the_page_reports_status_final_url_and_html(world):
    world.status, world.final_url = 403, "https://www.mercadolibre.com/gz/account-verification"
    async with browser_fetch.ListingBrowser() as lb:
        page = await lb.fetch(URL)
    assert (page.status, page.html) == (403, "<html>ok</html>")
    assert page.final_url.endswith("account-verification") and page.elapsed_s >= 0


async def test_it_relaunches_after_the_recycle_limit(world):
    async with browser_fetch.ListingBrowser(recycle_after=2) as lb:
        for _ in range(5):
            await lb.fetch(URL)
        assert lb.launches == 3          # páginas 1-2, 3-4, 5
    assert world.closed == 3


async def test_a_failed_page_raises_unavailable_and_the_next_one_relaunches(world):
    async with browser_fetch.ListingBrowser() as lb:
        await lb.fetch(URL)
        world.goto_error = RuntimeError("net::ERR_PROXY_CONNECTION_FAILED")
        with pytest.raises(browser_fetch.BrowserUnavailable, match="ERR_PROXY"):
            await lb.fetch(URL)
        world.goto_error = None
        await lb.fetch(URL)
        assert lb.launches == 2
    assert all(p.closed for p in world.pages)


async def test_launch_failure_is_browser_unavailable(world):
    world.launch_error = RuntimeError("no hay firefox")
    async with browser_fetch.ListingBrowser() as lb:
        with pytest.raises(browser_fetch.BrowserUnavailable, match="no hay firefox"):
            await lb.fetch(URL)


async def test_without_the_browser_it_raises_unavailable(world, monkeypatch):
    monkeypatch.setattr(browser_fetch, "available", lambda: False)
    async with browser_fetch.ListingBrowser() as lb:
        with pytest.raises(browser_fetch.BrowserUnavailable):
            await lb.fetch(URL)
    assert world.launches == []


async def test_a_non_public_url_is_rejected_before_launching(world, monkeypatch):
    def deny(url):
        raise SsrfBlocked("privada")

    monkeypatch.setattr(browser_fetch, "assert_public_url", deny)
    async with browser_fetch.ListingBrowser() as lb:
        with pytest.raises(SsrfBlocked):
            await lb.fetch("http://127.0.0.1/admin")
    assert world.launches == []


async def test_a_host_in_cooldown_is_skipped_without_launching(world):
    for _ in range(get_settings().browser_fetch_zero_streak):
        browser_fetch.note_listing_result(URL, results=0)
    async with browser_fetch.ListingBrowser() as lb:
        with pytest.raises(browser_fetch.CircuitOpen):
            await lb.fetch(URL)
    assert world.launches == []
    browser_fetch.note_listing_result(URL, results=5)   # un éxito lo reabre
    assert browser_fetch.circuit_open_for(URL) is False


def test_proxy_configured_reflects_browser_proxy(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "browser_proxy", "", raising=False)
    assert browser_fetch.proxy_configured() is False
    monkeypatch.setattr(s, "browser_proxy", "http://u:p@h:1", raising=False)
    assert browser_fetch.proxy_configured() is True
