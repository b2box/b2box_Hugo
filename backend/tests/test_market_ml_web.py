"""Fuente "ML web": parseo del estado embebido (fixtures sintéticos), detección
de bloqueos y la fuente de la corrida (cupo, racha de fallos, bytes). Sin red
ni navegador."""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import runtime  # noqa: E402
from app.db.models import Setting  # noqa: E402
from app.db.session import engine, init_db  # noqa: E402
from app.ingest import browser_fetch  # noqa: E402
from app.pricing import daily_budget  # noqa: E402
from app.pricing import market_ml_web as web  # noqa: E402
from tests.ml_web_fixtures import ANTIBOT_HTML, ld_product, listing_html, page, polycard  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    with Session(engine) as s:
        for row in s.exec(select(Setting)).all():
            s.delete(row)
        s.commit()
    runtime.invalidate()
    browser_fetch.reset_circuit()
    yield
    runtime.invalidate()
    browser_fetch.reset_circuit()


# ─── la URL de búsqueda ───────────────────────────────────────────────────


def test_search_url_is_a_slug_on_the_listing_host():
    assert web.search_url("Organizador de Cables USB") == \
        "https://listado.mercadolibre.com.ar/organizador-de-cables-usb"


def test_slug_drops_vowel_accents_and_punctuation_but_keeps_the_enie():
    assert web.slugify("Lámpara LED, 12V (baño)") == "lampara-led-12v-baño"
    assert web.search_url("Estante de baño").endswith("estante-de-ba%C3%B1o")


def test_empty_title_has_no_url():
    assert web.search_url("  ---  ") is None
    assert web.slugify("x" * 500) == "x" * 110


# ─── el estado embebido ───────────────────────────────────────────────────

POLY = [
    polycard("MLA1904951169", "Organizador Cables Escritorio", 6925.4, user_product="MLAU4367309146",
             url="www.mercadolibre.com.ar/organizador-cables/up/MLAU4367309146#polycard_client=search",
             seller=None, sold=None),
    polycard("MLA2050479522", "Caja Organizadora De Cables Gadnic", 25999, catalog="MLA29003349",
             url="www.mercadolibre.com.ar/caja-organizadora/p/MLA29003349", sold=1000,
             picture="966376-MLA99566708076_122025"),
]
LD = [
    ld_product("Organizador Cables Escritorio", "https://www.mercadolibre.com.ar/organizador-cables/up/MLAU4367309146",
               6925.4, brand="FooFoony"),
    ld_product("Caja Organizadora De Cables Gadnic", "https://www.mercadolibre.com.ar/caja-organizadora/p/MLA29003349",
               25999, brand="Gadnic"),
]


def test_state_results_become_candidates():
    parsed = web.parse_search(listing_html(POLY, ld=LD), max_results=8)
    assert parsed.source == "state" and parsed.total == 2 and not parsed.empty
    a, b = parsed.candidates
    assert (a.id, a.origin, a.price_cents, a.currency) == ("MLA1904951169", "web", 692540, "ARS")
    assert a.permalink == "https://www.mercadolibre.com.ar/organizador-cables/up/MLAU4367309146"
    assert a.seller == "" and a.sold_quantity is None
    assert b.seller == "Tienda Uno" and b.sold_quantity == 1000 and b.catalog_id == "MLA29003349"
    assert b.image_urls == ["https://http2.mlstatic.com/D_NQ_NP_966376-MLA99566708076_122025-F.jpg"]


def test_brand_comes_from_the_json_ld_matched_by_any_id():
    a, b = web.parse_search(listing_html(POLY, ld=LD), 8).candidates
    assert (a.brand, b.brand) == ("FooFoony", "Gadnic")


def test_results_are_capped():
    many = [polycard(f"MLA{1000 + i}", f"Producto {i}", 100 + i) for i in range(20)]
    parsed = web.parse_search(listing_html(many), max_results=5)
    assert len(parsed.candidates) == 5 and parsed.total == 20


def test_a_valid_listing_with_no_results_is_empty_not_a_failure():
    parsed = web.parse_search(listing_html([]), 8)
    assert parsed.source == "state" and parsed.empty and parsed.candidates == []
    assert web.page_problem(page(listing_html([])), parsed) is None


def test_json_ld_is_the_fallback_when_the_state_is_gone():
    parsed = web.parse_search(listing_html(None, ld=LD, with_state=False), 8)
    assert parsed.source == "jsonld" and len(parsed.candidates) == 2
    a, b = parsed.candidates
    assert (a.id, a.brand, a.price_cents) == ("MLAU4367309146", "FooFoony", 692540)
    assert b.id == "MLA29003349"


def test_state_is_found_even_if_ml_moves_it():
    import json

    state = {"otra": {"rama": {"results": POLY}}}
    html = ('<script id="__NORDIC_RENDERING_CTX__">_n.ctx.r=' + json.dumps(state) + ";</script>")
    assert [c.id for c in web.parse_search(html, 8).candidates] == ["MLA1904951169", "MLA2050479522"]


@pytest.mark.parametrize("html", ["", "<html></html>", ANTIBOT_HTML,
                                  '<script id="__NORDIC_RENDERING_CTX__">_n.ctx.r={roto</script>'])
def test_unreadable_pages_parse_to_nothing_and_never_raise(html):
    parsed = web.parse_search(html, 8)
    assert parsed.source == "none" and parsed.candidates == []


def test_third_party_text_is_sanitized():
    evil = [
        polycard("MLA1", "javascript:alert(1)", 100, url="javascript:alert(1)"),
        polycard("MLA2", "Foto ajena", 100, picture="..//x"),
        polycard("MLA3", "Link ajeno", 100, url="evil.example.com/https://mercadolibre.com.ar/x"),
        polycard("../etc", "Id raro", 100),
        polycard("MLA4", "   ", 100),
        polycard("MLA5", "Precio basura", float("nan")),
        polycard("MLA6", "Moneda rara <b>x</b>", 100, currency="usd"),
    ]
    by_id = {c.id: c for c in web.parse_search(listing_html(evil), 20).candidates}
    assert set(by_id) == {"MLA1", "MLA2", "MLA3", "MLA5", "MLA6"}
    assert by_id["MLA1"].permalink == ""                     # javascript: no es un href
    assert by_id["MLA2"].image_urls == []                    # el id de foto no valida
    assert by_id["MLA3"].permalink == ""                     # host ajeno
    assert by_id["MLA5"].price_cents is None
    assert by_id["MLA6"].currency == "USD"


def test_ld_json_images_must_be_mlstatic():
    bad = [ld_product("Algo", "https://www.mercadolibre.com.ar/x/p/MLA123456", 10,
                      image="https://evil.example/x.jpg")]
    [c] = web.parse_search(listing_html(None, ld=bad, with_state=False), 8).candidates
    assert c.image_urls == []


# ─── qué pasó con la página ───────────────────────────────────────────────


def _problem(html, **kw):
    p = page(html, **kw)
    return web.page_problem(p, web.parse_search(html, 8))


def test_a_readable_listing_is_not_a_problem():
    assert _problem(listing_html(POLY)) is None


def test_antibot_interstitial_is_a_block():
    kind, reason = _problem(ANTIBOT_HTML)
    assert kind == "blocked" and "anti-bot" in reason


def test_account_verification_redirect_is_a_block():
    kind, _ = _problem("<html>ok</html>", final_url="https://www.mercadolibre.com/gz/account-verification?x=1")
    assert kind == "blocked"


@pytest.mark.parametrize("status", [403, 429])
def test_http_403_and_429_are_blocks(status):
    assert _problem("<html></html>", status=status)[0] == "blocked"


def test_a_page_in_an_unknown_format_is_an_error_not_a_block():
    kind, reason = _problem("<html><body>hola</body></html>")
    assert kind == "error" and "formato" in reason


def test_http_5xx_is_an_error():
    assert _problem("<html></html>", status=503)[0] == "error"


# ─── la fuente de la corrida ──────────────────────────────────────────────


def _source(fetcher, **kw):
    slept: list[float] = []

    async def sleep(s):
        slept.append(s)

    base = dict(budget=100, max_results=8, concurrency=1, pause_s=4.0, block_streak=3,
                fetcher=fetcher, sleep=sleep, jitter=lambda: 0.5)
    base.update(kw)
    src = web.MlWebSource(**base)
    src.slept = slept
    return src


def _fetcher(*pages):
    queue = list(pages)
    calls: list[str] = []

    async def fetch(url):
        calls.append(url)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    fetch.calls = calls
    return fetch


async def test_a_good_search_counts_bytes_and_pauses():
    fetch = _fetcher(page(listing_html(POLY, ld=LD), nbytes=350_000))
    src = _source(fetch)
    res = await src.search("Organizador de cables")
    assert res.kind == "ok" and len(res.candidates) == 2 and res.bytes == 350_000
    assert fetch.calls == ["https://listado.mercadolibre.com.ar/organizador-de-cables"]
    assert (src.searches, src.bytes, src.blocked) == (1, 350_000, 0)
    assert src.slept == [4.0]            # pausa 4 s × (0,7 + 0,6 × 0,5)


async def test_empty_listing_is_not_a_failure_and_resets_the_streak():
    src = _source(_fetcher(page(ANTIBOT_HTML), page(listing_html([]))), block_streak=2)
    assert (await src.search("a")).kind == "blocked"
    assert (await src.search("b")).kind == "empty"
    assert (await src.search("c")).kind == "empty" and src.active


async def test_a_block_is_not_retried_and_the_run_goes_on():
    fetch = _fetcher(page(ANTIBOT_HTML), page(listing_html(POLY)))
    src = _source(fetch)
    first = await src.search("uno")
    assert first.kind == "blocked" and first.candidates == [] and "anti-bot" in first.reason
    assert len(fetch.calls) == 1
    assert (await src.search("dos")).kind == "ok"
    assert (src.blocked, src.searches) == (1, 2)


async def test_consecutive_failures_cut_the_source_for_the_night():
    fetch = _fetcher(page(ANTIBOT_HTML))
    src = _source(fetch, block_streak=3)
    kinds = [(await src.search(f"p{i}")).kind for i in range(5)]
    assert kinds == ["blocked", "blocked", "blocked", "off", "off"]
    assert not src.active and "3 fallos seguidos" in src.status_text()
    assert len(fetch.calls) == 3          # una vez cortada, ni abre el browser


async def test_a_browser_failure_counts_as_an_error_streak():
    src = _source(_fetcher(browser_fetch.BrowserUnavailable("proxy caído")), block_streak=2)
    res = await src.search("x")
    assert res.kind == "error" and "proxy caído" in res.reason
    await src.search("y")
    assert not src.active


async def test_an_open_circuit_counts_as_a_block_without_opening_the_browser():
    src = _source(_fetcher(browser_fetch.CircuitOpen("en descanso")))
    res = await src.search("x")
    assert res.kind == "blocked" and "descanso" in res.reason


async def test_daily_budget_is_reserved_atomically_and_never_exceeded():
    fetch = _fetcher(page(listing_html(POLY)))
    src = _source(fetch, budget=2)
    kinds = [(await src.search(f"p{i}")).kind for i in range(4)]
    assert kinds == ["ok", "ok", "budget", "off"]
    assert daily_budget.used_today(web.WEB_COUNTER_KEY) == 2 and len(fetch.calls) == 2
    assert src.status_text() == "sin cupo diario de búsquedas web"


async def test_the_budget_is_shared_by_every_source_of_the_day():
    daily_budget.reserve(web.WEB_COUNTER_KEY, 3)
    daily_budget.reserve(web.WEB_COUNTER_KEY, 3)
    src = _source(_fetcher(page(listing_html(POLY))), budget=3)
    assert [(await src.search(f"p{i}")).kind for i in range(2)] == ["ok", "budget"]


async def test_on_reserve_runs_in_the_reservation_transaction():
    seen = []
    src = _source(_fetcher(page(listing_html(POLY))), on_reserve=lambda session: seen.append(1))
    await src.search("x")
    assert seen == [1]


async def test_concurrency_is_bounded():
    import asyncio

    running = peak = 0

    async def fetch(url):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return page(listing_html(POLY))

    src = _source(fetch, concurrency=2, pause_s=0)
    await asyncio.gather(*(src.search(f"p{i}") for i in range(6)))
    assert peak == 2


async def test_an_untitled_product_does_not_spend_a_search():
    fetch = _fetcher(page(listing_html(POLY)))
    src = _source(fetch)
    res = await src.search("---")
    assert res.kind == "empty" and fetch.calls == [] and src.searches == 0


# ─── cuándo la fuente está apagada ────────────────────────────────────────


def test_without_proxy_the_source_is_off_and_says_why(monkeypatch):
    monkeypatch.setattr(browser_fetch, "available", lambda: True)
    monkeypatch.setattr(browser_fetch, "proxy_configured", lambda: False)
    assert "BROWSER_PROXY" in web.disabled_reason()


def test_without_the_browser_it_is_off(monkeypatch):
    monkeypatch.setattr(browser_fetch, "available", lambda: False)
    assert "browser" in web.disabled_reason()


def test_zero_budget_turns_it_off_before_looking_at_the_proxy(monkeypatch):
    runtime.set_value("pm_ml_web_daily_budget", 0)
    assert "pm_ml_web_daily_budget" in web.disabled_reason()


def test_with_proxy_and_budget_it_runs(monkeypatch):
    monkeypatch.setattr(browser_fetch, "available", lambda: True)
    monkeypatch.setattr(browser_fetch, "proxy_configured", lambda: True)
    assert web.disabled_reason() is None
    runtime.set_value("pm_ml_web_max_results", 5)
    src = web.from_runtime()
    assert (src.budget, src.max_results, src.block_streak) == (2500, 5, 5)
    assert src._block_scripts is True and src.pause_s == 4.0 and src._sem._value == 1


async def test_the_default_fetch_opens_one_listing_browser_with_the_script_setting(monkeypatch):
    made = []

    class FakeLB:
        def __init__(self, **kw):
            made.append(kw)
            self.closed = 0

        async def fetch(self, url):
            return page(listing_html(POLY))

        async def close(self):
            self.closed += 1

    monkeypatch.setattr(browser_fetch, "ListingBrowser", FakeLB)
    src = web.MlWebSource(budget=10, pause_s=0, block_scripts=False)
    await src.search("a")
    await src.search("b")
    assert made == [{"block_scripts": False}]          # un solo browser para toda la corrida
    await src.aclose()
    assert src._browser is None


def test_a_spent_budget_turns_it_off(monkeypatch):
    monkeypatch.setattr(browser_fetch, "available", lambda: True)
    monkeypatch.setattr(browser_fetch, "proxy_configured", lambda: True)
    runtime.set_value("pm_ml_web_daily_budget", 1)
    daily_budget.reserve(web.WEB_COUNTER_KEY, 1)
    assert "sin cupo" in web.disabled_reason()
    assert web.web_budget_status() == {"used": 1, "budget": 1, "remaining": 0}


# ─── el proxy mal formado no tumba nada ni se filtra ──────────────────────


def test_a_malformed_proxy_turns_the_web_off_with_a_clear_reason(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(browser_fetch, "available", lambda: True)
    monkeypatch.setattr(get_settings(), "browser_proxy", "http://usr_b2b:Cl4ve/Secreta@res.proxy.io:8080",
                        raising=False)
    reason = web.disabled_reason()
    assert "mal formado" in reason and "Cl4ve" not in reason and "res.proxy.io" not in reason


async def test_error_reasons_never_carry_the_proxy_credentials(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "browser_proxy", "http://usr_b2b:Cl4veSecreta@res.proxy.io:8080",
                        raising=False)
    src = _source(_fetcher(RuntimeError("tunnel to res.proxy.io failed for usr_b2b:Cl4veSecreta")))
    res = await src.search("x")
    assert res.kind == "error"
    assert "Cl4veSecreta" not in res.reason and "res.proxy.io" not in res.reason and "usr_b2b" not in res.reason


# ─── la página tiene que terminar en ML ───────────────────────────────────


@pytest.mark.parametrize("final", [
    "https://evilmercadolibre.com.ar/organizador",
    "https://mercadolibre.com.ar.evil.example/organizador",
    "https://mercadolibre.com.ar@evil.example/organizador",
    "https://evil.example/organizador",
    "",
])
async def test_a_search_that_ends_outside_mercado_libre_is_a_block_and_is_not_parsed(final, monkeypatch):
    parsed = []
    real = web.parse_search
    monkeypatch.setattr(web, "parse_search", lambda *a, **k: (parsed.append(1), real(*a, **k))[1])
    src = _source(_fetcher(page(listing_html(POLY), final_url=final)))
    res = await src.search("organizador")
    assert res.kind == "blocked" and "otro sitio" in res.reason and res.candidates == []
    assert parsed == []                                       # ni se mira lo que trae
    assert (src.blocked, src.bytes) == (1, 400_000)


async def test_other_mercado_libre_subdomains_are_fine():
    src = _source(_fetcher(page(listing_html(POLY), final_url="https://www.mercadolibre.com/jms/mla/x")))
    assert (await src.search("organizador")).kind == "ok"


def test_a_verification_page_that_carries_an_empty_state_is_a_block_not_an_empty_listing():
    html = listing_html([])
    pg = page(html, final_url="https://www.mercadolibre.com/jms/mla/lgz/account-verification?go=x")
    assert web.page_problem(pg, web.parse_search(html, 8))[0] == "blocked"
    # y un listado vacío de verdad (misma página, URL de búsqueda) sigue siendo vacío
    assert web.page_problem(page(html), web.parse_search(html, 8)) is None


def test_the_url_decides_even_if_there_are_results():
    pg = page(listing_html(POLY), final_url="https://www.mercadolibre.com/gz/account-verification")
    assert web.page_problem(pg, web.parse_search(pg.html, 8))[0] == "blocked"
