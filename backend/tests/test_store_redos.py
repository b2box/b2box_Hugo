"""Entradas hechas a propósito (un sitemap, una ficha o un robots.txt hostiles) tienen que
procesarse en una fracción de segundo: se parsean en el event loop de Hugo, que comparte proceso
con /verify, /app/lookup y el dashboard. Antes las regex las volvían cuadráticas o exponenciales."""

from __future__ import annotations

import random
import re
import time

import pytest

from app.pricing import store_parse, store_robots
from tests import store_fixtures as fx

LIMIT_S = 1.0


def _took(fn, *args, **kw):
    t0 = time.perf_counter()
    out = fn(*args, **kw)
    return out, time.perf_counter() - t0


# ─── sitemap ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("evil", [
    "<loc>" * 20_000,                                    # 10.000 sin cerrar: 6,8 s con la regex
    "<loc>" + " " * 200_000,                             # espacios: cúbico
    "<loc>" + "<![CDATA[ " * 20_000,
    ("<loc>" + "x" * 50) * 50_000,                       # muchos «abiertos» y un solo cierre al final
    "<urlset>" + "<url><loc>https://x.com/a</loc></url>" * 100_000,
    "</loc>" * 100_000,
])
def test_hostile_sitemaps_are_parsed_in_a_blink(evil):
    sm, elapsed = _took(store_parse.parse_sitemap, evil + "</loc>")
    assert elapsed < LIMIT_S, f"{elapsed:.2f} s"
    assert len(sm.locs) <= store_parse.MAX_SITEMAP_URLS


def test_a_25_mb_junk_sitemap_does_not_take_forever():
    junk = ("<loc>" + "a" * 90 + "\n") * 280_000        # ~25 MB y ningún cierre
    assert len(junk) > 25_000_000
    _sm, elapsed = _took(store_parse.parse_sitemap, junk)
    assert elapsed < LIMIT_S


def test_sitemap_parsing_still_reads_what_it_used_to():
    text = ("<urlset><url><loc> https://x.com/a </loc></url><url><loc><![CDATA[https://x.com/b?x=1&amp;y=2]]></loc></url>"
            "<url><loc>https://x.com/c?x=1&amp;y=2</loc></url></urlset>")
    # CDATA es literal; fuera de CDATA se desescapa.
    assert store_parse.parse_sitemap(text).locs == ["https://x.com/a", "https://x.com/b?x=1&amp;y=2", "https://x.com/c?x=1&y=2"]
    assert store_parse.parse_sitemap("<urlset><loc>https://x.com/sin-cierre").locs == []
    assert store_parse.parse_sitemap("<sitemapindex><sitemap><loc>https://x.com/s.xml</loc></sitemap></sitemapindex>").is_index


def test_sitemap_limit_is_applied_while_reading():
    text = "<urlset>" + "".join(f"<url><loc>https://x.com/{i}</loc></url>" for i in range(50)) + "</urlset>"
    sm = store_parse.parse_sitemap(text, max_urls=10)
    assert len(sm.locs) == 10 and sm.truncated


# ─── fichas ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name,evil", [
    ("meta sin cerrar", "<meta " * 20_000),
    ("meta property sin cerrar", '<meta property="og:title" ' * 20_000),
    ("ld+json sin cerrar", '<script type="application/ld+json">' * 20_000),
    ("single-product sin cerrar", '<div id="single-product" data-variants="' + " " * 300_000),
    ("etiquetas sin cerrar", "<div " * 100_000),
    ("anidado", "<div>" * 100_000),
    ("atributos", "<a " + 'x="1" ' * 100_000 + ">"),
    ("script gigante", '<script type="application/ld+json">' + "{" * 2_000_000 + "</script>"),
    ("comentario sin cerrar", "<!--" * 50_000),
    ("flight de Next.js", '"product" ' * 100_000 + '"product":{"sku":"' + "x" * 100_000),
])
def test_hostile_pages_are_parsed_in_a_blink(name, evil):
    for platform in ("tiendanube", "jsonld_sitemap"):
        item, elapsed = _took(store_parse.parse_product_page, platform, "<html><body>" + evil, ["https://x.com/p"])
        assert elapsed < LIMIT_S, f"{name} / {platform}: {elapsed:.2f} s"
        assert item is None or isinstance(item, store_parse.ParsedItem)


def test_a_3_mb_hostile_page_is_still_fast():
    evil = ("<meta " + "a" * 20 + " ") * 150_000
    assert len(evil) > 3_000_000
    _item, elapsed = _took(store_parse.parse_product_page, "tiendanube", evil, ["https://x.com/p"])
    assert elapsed < LIMIT_S


def test_pages_still_parse_the_same_after_moving_off_regex():
    tn = fx.tiendanube_page(f"{fx.CP}/productos/a/", "Producto A | B", [fx.variant(15600, compare=67000, stock=3)], ld_price=67000,
                            og_image="http://acdn-us.mitiendanube.com/stores/001/og-640-0.webp?a=1&b=2")
    item = store_parse.parse_product_page("tiendanube", tn, [f"{fx.CP}/productos/a/"])
    assert (item.title, item.price_cents, item.stock, item.price_doubtful) == ("Producto A | B", 1_560_000, 3, False)
    assert any(u.startswith("http://acdn-us.mitiendanube.com/") and "a=1&b=2" in u for u in item.image_urls), item.image_urls
    gd = fx.gadnic_page(f"{fx.GD}/x/y", "Mic", ld_price=25999, final_price=25999)
    item = store_parse.parse_product_page("jsonld_sitemap", gd, [f"{fx.GD}/x/y"])
    assert (item.sku, item.price_cents, item.brand) == ("MICCOND6", 2_599_900, "Gadnic")


def test_stock_is_bounded_so_the_integer_column_never_overflows():
    page = fx.tiendanube_page(f"{fx.CP}/productos/a/", "A", [fx.variant(100, stock=2_000_000_000), fx.variant(100, stock=2_000_000_000)])
    item = store_parse.parse_product_page("tiendanube", page, [f"{fx.CP}/productos/a/"])
    assert item.stock == store_parse.MAX_STOCK


# ─── robots.txt ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("wildcards", [4, 6, 12, 30])
def test_wildcard_heavy_rules_do_not_backtrack(wildcards):
    robots = store_robots.parse("User-agent: *\nDisallow: /" + "*a" * wildcards + "*b\n")
    url = "https://x.com/" + "a" * 300
    allowed, elapsed = _took(robots.allows, url)
    assert elapsed < LIMIT_S and allowed is True


def test_many_rules_against_a_long_url_stay_fast():
    rules = "".join(f"Disallow: /*{'a*' * (i % 8)}b{i}\n" for i in range(store_robots.MAX_RULES))
    robots = store_robots.parse("User-agent: *\n" + rules)
    _allowed, elapsed = _took(robots.allows, "https://x.com/" + "a" * 480)
    assert elapsed < LIMIT_S


def _reference(pattern: str):
    """La traducción vieja a regex: sirve de oráculo para comprobar que el matcheo nuevo dice lo mismo."""
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    return re.compile(".*".join(re.escape(p) for p in body.split("*")) + (r"\Z" if anchored else ""))


def test_segment_matching_agrees_with_the_regex_it_replaced():
    rnd = random.Random(7)
    alphabet = "ab/?.x"
    for _ in range(4000):
        pattern = "".join(rnd.choice(alphabet + "**$") for _ in range(rnd.randint(1, 9)))
        if "$" in pattern[:-1]:
            pattern = pattern.replace("$", "")        # el $ solo vale al final
        target = "/" + "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 14)))
        rule = store_robots._compile(False, pattern)
        assert rule.matches(target) == bool(_reference(pattern).match(target)), (pattern, target)


@pytest.mark.parametrize("status,allowed", [(404, True), (410, True), (400, True), (401, False), (403, False),
                                            (429, False), (500, False), (503, False), (None, False)])
def test_robots_status_codes(status, allowed):
    assert store_robots.from_status(status, "User-agent: *\nDisallow: /x\n" if status == 200 else "").allows("/y") is allowed


def test_a_group_for_a_substring_of_our_name_is_not_ours():
    text = "User-agent: *\nDisallow: /privado/\n\nUser-agent: o\nDisallow:\n\nUser-agent: bot\nAllow: /\n"
    robots = store_robots.parse(text, agent="HugoPriceBot/1.0 (+https://b2box.pro)")
    assert robots.allows("/publico/") and not robots.allows("/privado/x"), "«o» y «bot» no son nuestros: manda el grupo *"
    mine = store_robots.parse("User-agent: HUGOPRICEBOT\nDisallow: /nada/\n\nUser-agent: *\nDisallow:\n", agent="HugoPriceBot/1.0")
    assert not mine.allows("/nada/x") and mine.allows("/otro")
