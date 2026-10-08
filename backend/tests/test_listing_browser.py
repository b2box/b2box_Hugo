"""ListingBrowser: un Camoufox para muchas páginas de listado. El navegador es
un doble (nunca se levanta Firefox); lo que se prueba es que reusa el MISMO
lanzamiento, proxy y guard que `render()`, que corta imágenes/fuentes/media,
que mide bytes, que se relanza y que degrada sin romper a nadie."""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import asyncio  # noqa: E402

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
        self.w.in_flight += 1
        try:
            if self.w.goto_gate is not None:
                await self.w.goto_gate.wait()
        finally:
            self.w.in_flight -= 1
        if self.w.closed_while_in_flight:
            raise RuntimeError("Target closed")
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
        if self.w.page_close_hangs:
            await asyncio.sleep(3600)
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
        # Ganchos para los tests de ciclo de vida.
        self.goto_gate = None
        self.in_flight = 0
        self.open_now = 0
        self.max_open = 0
        self.closed_while_in_flight = False
        self.page_close_hangs = False
        self.exit_hangs = False


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
            w.open_now += 1
            w.max_open = max(w.max_open, w.open_now)
            return _Browser(w)

        async def __aexit__(self, *exc):
            if w.exit_hangs:
                await asyncio.sleep(3600)
            if w.in_flight:
                w.closed_while_in_flight = True
            w.open_now -= 1
            w.closed += 1

    async def public(host):  # noqa: ARG001
        return True

    monkeypatch.setattr(browser_fetch, "_camoufox", lambda: FakeCamoufox)
    monkeypatch.setattr(browser_fetch, "available", lambda: True)
    monkeypatch.setattr(browser_fetch, "assert_public_url", lambda url: None)
    monkeypatch.setattr(browser_fetch, "_host_is_public", public)
    monkeypatch.setattr(get_settings(), "browser_proxy", "http://usr:pwd@res.proxy.io:8080", raising=False)
    browser_fetch.reset_circuit()
    browser_fetch.reset_dns_cache()
    yield w
    browser_fetch.reset_circuit()
    browser_fetch.reset_dns_cache()


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


async def test_only_the_document_is_downloaded_by_default(world):
    world.subrequests.append(_Req("https://http2.mlstatic.com/s.css", "stylesheet", 110_000))
    async with browser_fetch.ListingBrowser() as lb:
        page = await lb.fetch(URL)
    aborted = {rtype for _, rtype, route in world.routed if route.aborted}
    followed = {rtype for _, rtype, route in world.routed if route.continued}
    assert aborted == {"image", "font", "media", "script", "stylesheet"} and followed == {"document"}
    # Solo se cuentan los bytes de lo que bajó: el documento.
    assert page.bytes == 100 + 400 + 300_000


async def test_scripts_can_be_let_through(world):
    async with browser_fetch.ListingBrowser(block_scripts=False) as lb:
        page = await lb.fetch(URL)
    aborted = {rtype for _, rtype, route in world.routed if route.aborted}
    assert aborted == {"image", "font", "media"}          # imágenes, fuentes y media siempre se cortan
    assert page.bytes == (100 + 400 + 300_000) + (100 + 400 + 150_000)


async def test_the_ssrf_guard_still_applies_to_every_request(world, monkeypatch):
    world.subrequests = [_Req("http://169.254.169.254/latest/meta-data/", "xhr"),
                         _Req("https://listado.mercadolibre.com.ar/x", "document")]

    async def real_dns(host):  # noqa: ARG001
        return not host.startswith("169.")

    monkeypatch.setattr(browser_fetch, "_host_is_public", real_dns)
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


# ─── solo habla con ML ────────────────────────────────────────────────────


async def test_the_listing_browser_only_talks_to_mercado_libre(world):
    world.subrequests = [
        _Req(URL, "document"),
        _Req("https://www.mercadolibre.com/jms/mla/lgz/account-verification", "document"),
        _Req("https://http2.mlstatic.com/frontend-assets/x.js", "xhr"),
        _Req("https://accounts.google.com/gsi/client", "xhr"),
        _Req("https://static.hotjar.com/c/hotjar.js", "xhr"),
        _Req("https://evilmercadolibre.com.ar/x", "xhr"),
        _Req("https://mercadolibre.com.ar.evil.example/x", "xhr"),
        _Req("https://mercadolibre.com.ar@evil.example/x", "xhr"),
    ]
    async with browser_fetch.ListingBrowser() as lb:
        page = await lb.fetch(URL)
    followed = {u for u, _, r in world.routed if r.continued}
    assert followed == {URL, "https://www.mercadolibre.com/jms/mla/lgz/account-verification",
                        "https://http2.mlstatic.com/frontend-assets/x.js"}
    assert page.blocked == 5


async def test_the_allowed_hosts_can_be_changed_by_the_caller(world):
    world.subrequests = [_Req("https://otro.example/x", "document"), _Req(URL, "document")]
    async with browser_fetch.ListingBrowser(allow_hosts=("otro.example",)) as lb:
        await lb.fetch(URL)
    assert {u for u, _, r in world.routed if r.continued} == {"https://otro.example/x"}


@pytest.mark.parametrize("host, ok", [
    ("mercadolibre.com.ar", True), ("listado.mercadolibre.com.ar", True), ("WWW.MercadoLibre.com", True),
    ("http2.mlstatic.com", True), ("mercadolibre.com.ar.", True),
    ("evilmercadolibre.com.ar", False), ("mercadolibre.com.ar.evil.com", False), ("xmlstatic.com", False),
    ("", False), ("com.ar", False),
])
def test_host_matches_has_a_boundary_check(host, ok):
    assert browser_fetch.host_matches(host, browser_fetch.ML_ALLOWED_HOSTS) is ok


@pytest.mark.parametrize("url, ok", [
    ("https://www.mercadolibre.com.ar/x", True),
    ("https://mercadolibre.com.ar@evil.example/x", False),       # userinfo: el host real es evil.example
    ("https://evilmercadolibre.com.ar/x", False),
    ("", False), (None, False), ("not a url", False), ("https://[::1/", False),
])
def test_url_host_allowed_is_fail_closed(url, ok):
    assert browser_fetch.url_host_allowed(url, ("mercadolibre.com.ar", "mercadolibre.com")) is ok


# ─── la cache DNS del guard ───────────────────────────────────────────────


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


@pytest.fixture
def dns(monkeypatch):
    """getaddrinfo con respuestas programadas y un reloj a mano."""
    import socket as _socket

    clock = _Clock()
    answers: dict[str, object] = {}
    calls: list[str] = []

    def fake_getaddrinfo(host, *a, **k):
        calls.append(host)
        result = answers[host]
        if isinstance(result, Exception):
            raise result
        return [(2, 1, 6, "", (result, 0))]

    monkeypatch.setattr(_socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(browser_fetch.time, "monotonic", clock.monotonic)
    browser_fetch.reset_dns_cache()
    yield answers, calls, clock
    browser_fetch.reset_dns_cache()


async def test_a_public_answer_is_cached_for_five_minutes(dns):
    answers, calls, clock = dns
    answers["a.example"] = "93.184.216.34"
    assert await browser_fetch._host_is_public("a.example") is True
    clock.now += 299
    assert await browser_fetch._host_is_public("a.example") is True
    assert calls == ["a.example"]
    clock.now += 2                                   # pasaron 301 s
    assert await browser_fetch._host_is_public("a.example") is True
    assert calls == ["a.example", "a.example"]


async def test_a_private_answer_is_cached_only_thirty_seconds(dns):
    answers, calls, clock = dns
    answers["b.example"] = "10.0.0.5"
    assert await browser_fetch._host_is_public("b.example") is False
    clock.now += 29
    assert await browser_fetch._host_is_public("b.example") is False and calls == ["b.example"]
    answers["b.example"] = "93.184.216.34"            # lo arreglaron (o un rebinding al revés)
    clock.now += 2
    assert await browser_fetch._host_is_public("b.example") is True
    assert calls == ["b.example", "b.example"]


async def test_a_dns_failure_is_not_cached(dns):
    answers, calls, clock = dns
    answers["c.example"] = OSError("timeout de resolución")
    assert await browser_fetch._host_is_public("c.example") is False        # fail-closed
    answers["c.example"] = "93.184.216.34"
    assert await browser_fetch._host_is_public("c.example") is True         # sin esperar 30 s
    assert calls == ["c.example", "c.example"]


async def test_the_dns_cache_has_a_size_cap_and_drops_the_oldest(dns, monkeypatch):
    answers, calls, clock = dns
    monkeypatch.setattr(browser_fetch, "DNS_CACHE_MAX", 5)
    for i in range(8):
        answers[f"h{i}.example"] = "93.184.216.34"
        await browser_fetch._host_is_public(f"h{i}.example")
        clock.now += 1
    assert len(browser_fetch._host_cache) == 5
    assert "h0.example" not in browser_fetch._host_cache and "h7.example" in browser_fetch._host_cache


async def test_the_dns_cache_prefers_dropping_expired_entries(dns, monkeypatch):
    answers, calls, clock = dns
    monkeypatch.setattr(browser_fetch, "DNS_CACHE_MAX", 3)
    for host in ("old1.example", "old2.example"):
        answers[host] = "10.0.0.1"                    # negativas: vencen a los 30 s
        await browser_fetch._host_is_public(host)
    answers["fresh.example"] = "93.184.216.34"
    await browser_fetch._host_is_public("fresh.example")
    clock.now += 60
    answers["new.example"] = "93.184.216.34"
    await browser_fetch._host_is_public("new.example")
    assert set(browser_fetch._host_cache) == {"fresh.example", "new.example"}


async def test_the_input_url_is_resolved_off_the_event_loop(world, monkeypatch):
    import threading

    seen = []
    monkeypatch.setattr(browser_fetch, "assert_public_url", lambda url: seen.append(threading.current_thread()))
    async with browser_fetch.ListingBrowser() as lb:
        await lb.fetch(URL)
    assert seen and all(t is not threading.main_thread() for t in seen)


# ─── cerrar no puede colgar la corrida ────────────────────────────────────


async def test_a_page_that_never_closes_does_not_hang_the_run(world, monkeypatch):
    monkeypatch.setattr(browser_fetch, "_CLOSE_PAGE_TIMEOUT_S", 0.05)
    world.page_close_hangs = True
    async with browser_fetch.ListingBrowser() as lb:
        page = await asyncio.wait_for(lb.fetch(URL), 2)
        assert page.html == "<html>ok</html>" and lb._broken is True
        world.page_close_hangs = False
        await lb.fetch(URL)                                   # y el siguiente relanza
        assert lb.launches == 2


async def test_a_browser_that_never_exits_does_not_hang_close(world, monkeypatch):
    monkeypatch.setattr(browser_fetch, "_CLOSE_BROWSER_TIMEOUT_S", 0.05)
    lb = browser_fetch.ListingBrowser()
    await lb.fetch(URL)
    world.exit_hangs = True
    await asyncio.wait_for(lb.close(), 2)
    assert lb._context is None and not browser_fetch.slot_busy()      # soltó el lugar igual


# ─── relanzar con concurrencia 2 ──────────────────────────────────────────


async def test_recycling_waits_for_the_pages_in_flight(world):
    """Con 2 páginas a la vez, la que cumple el tope no le cierra el browser a la otra."""
    async with browser_fetch.ListingBrowser(recycle_after=2) as lb:
        await lb.fetch(URL)                                    # página 1 de 2
        world.goto_gate = asyncio.Event()
        a = asyncio.ensure_future(lb.fetch(URL))               # página 2 (en vuelo, trabada)
        await asyncio.sleep(0.01)
        b = asyncio.ensure_future(lb.fetch(URL))               # tope cumplido: tiene que esperar
        await asyncio.sleep(0.05)
        assert world.in_flight == 1 and lb.launches == 1 and not b.done()
        assert world.closed == 0                               # NADIE cerró el browser de la página en vuelo
        world.goto_gate.set()
        pa, pb = await asyncio.gather(a, b)
        assert pa.html and pb.html
        assert lb.launches == 2 and not world.closed_while_in_flight


async def test_pages_arriving_while_draining_wait_and_use_the_new_browser(world):
    async with browser_fetch.ListingBrowser(recycle_after=1) as lb:
        world.goto_gate = asyncio.Event()
        first = asyncio.ensure_future(lb.fetch(URL))
        await asyncio.sleep(0.01)
        others = [asyncio.ensure_future(lb.fetch(URL)) for _ in range(3)]
        await asyncio.sleep(0.05)
        assert not any(t.done() for t in others) and lb.launches == 1
        world.goto_gate.set()
        await asyncio.gather(first, *others)
        assert lb.launches == 4 and world.max_open == 1 and not world.closed_while_in_flight


def test_the_recycle_limit_defaults_to_75_and_is_configurable(monkeypatch):
    assert get_settings().browser_listing_recycle_after == 75
    assert browser_fetch.ListingBrowser()._recycle_after == 75
    monkeypatch.setattr(get_settings(), "browser_listing_recycle_after", 20, raising=False)
    assert browser_fetch.ListingBrowser()._recycle_after == 20
    assert browser_fetch.ListingBrowser(recycle_after=3)._recycle_after == 3


# ─── un solo Firefox a la vez ─────────────────────────────────────────────


def _render_ready(world, monkeypatch):
    """Que render() corra contra el mismo Camoufox de mentira (cuenta cuántos hay abiertos)."""
    class RPage:
        url = "https://articulo.mercadolibre.com.ar/MLA-1"

        async def route(self, *a):
            return None

        async def goto(self, *a, **k):
            await asyncio.sleep(0.05)

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

    original = browser_fetch._camoufox()

    class RBrowser(_Browser):
        async def new_page(self):
            return RPage()

    class RCam(original):
        async def __aenter__(self):
            await super().__aenter__()
            return RBrowser(world)

    monkeypatch.setattr(browser_fetch, "_camoufox", lambda: RCam)


async def test_a_render_with_a_client_waiting_takes_the_slot_from_an_idle_listing(world, monkeypatch):
    _render_ready(world, monkeypatch)
    async with browser_fetch.ListingBrowser() as lb:
        await lb.fetch(URL)                                    # el listado tiene Firefox (y el lugar)
        assert world.open_now == 1 and browser_fetch.slot_busy()
        page = await asyncio.wait_for(browser_fetch.render("https://articulo.mercadolibre.com.ar/MLA-1"), 5)
        assert page.final_url                                  # el render salió
        assert world.max_open == 1                             # NUNCA dos Firefox a la vez
        await lb.fetch(URL)                                    # el listado se relanza solo
        assert lb.launches == 2 and world.max_open == 1


async def test_a_render_waits_for_a_busy_listing_page_to_finish(world, monkeypatch):
    _render_ready(world, monkeypatch)
    async with browser_fetch.ListingBrowser() as lb:
        world.goto_gate = asyncio.Event()
        busy = asyncio.ensure_future(lb.fetch(URL))
        await asyncio.sleep(0.01)
        rendered = asyncio.ensure_future(browser_fetch.render("https://articulo.mercadolibre.com.ar/MLA-1"))
        await asyncio.sleep(0.05)
        assert not rendered.done() and world.open_now == 1     # espera: no abre un segundo Firefox
        world.goto_gate.set()
        await busy                                             # la página en vuelo termina entera
        await asyncio.wait_for(rendered, 5)
        assert world.max_open == 1 and not world.closed_while_in_flight


async def test_two_renders_never_run_two_firefox_at_once(world, monkeypatch):
    _render_ready(world, monkeypatch)
    results = await asyncio.gather(
        *(browser_fetch.render("https://articulo.mercadolibre.com.ar/MLA-1") for _ in range(3)))
    assert len(results) == 3 and world.max_open == 1 and world.launches.__len__() == 3


async def test_a_render_gives_up_if_the_slot_never_frees(world, monkeypatch):
    monkeypatch.setattr(browser_fetch, "_SLOT_WAIT_RENDER_S", 0.05)
    await browser_fetch._take_slot(None, interactive=False)    # alguien tiene el lugar y no lo suelta
    try:
        with pytest.raises(browser_fetch.BrowserUnavailable, match="otro navegador"):
            await browser_fetch.render("https://articulo.mercadolibre.com.ar/MLA-1")
    finally:
        browser_fetch._give_slot()
    assert world.launches == []


async def test_the_slot_is_released_when_render_fails(world, monkeypatch):
    _render_ready(world, monkeypatch)
    world.launch_error = RuntimeError("no hay firefox")
    with pytest.raises(browser_fetch.BrowserUnavailable, match="no hay firefox"):
        await browser_fetch.render("https://articulo.mercadolibre.com.ar/MLA-1")
    assert not browser_fetch.slot_busy()
