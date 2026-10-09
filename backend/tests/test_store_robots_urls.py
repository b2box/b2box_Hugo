"""robots.txt con comodines y saneado de links/fotos de tiendas. Sin red."""

from __future__ import annotations

import pytest

from app.pricing import store_robots, store_urls

# El robots.txt real de Gadnic (recortado): varios User-agent seguidos comparten reglas.
GADNIC = """\
Sitemap: https://www.gadnic.com.ar/sitemap.xml
# === Bots de entrenamiento de IA: bloqueo total ===
User-agent: GPTBot
User-agent: ClaudeBot
Disallow: /
# === Bots de preview social ===
User-agent: facebookexternalhit
Allow: /
Allow: /*?
User-agent: Googlebot
Disallow:
User-agent: Bingbot
Crawl-delay: 5
Disallow: /*?
# === Cualquier otro bot no listado ===
User-agent: *
Disallow: /*?
Disallow: /*?s=
Disallow: /*?item_menu=
Disallow: /*?orden=
"""

# El de Tiendanube (Casa Perfecta), recortado.
TIENDANUBE = """\
User-agent: *
Disallow: /admin/
Disallow: /checkout/
Disallow: /*?*preview_theme_installation_id*
Disallow: /*?view=
Disallow: /*?*srsltid=*
Disallow: /comprar/
Disallow: /search/
Disallow: /ar/search/

User-agent: WBSearchBot
Disallow: /

User-agent: MJ12bot
Crawl-Delay: 10
Sitemap: https://www.casaperfecta.com.ar/sitemap.xml.gz
"""


def test_gadnic_blocks_every_url_with_a_query_but_not_clean_product_pages():
    r = store_robots.parse(GADNIC)
    assert r.allows("https://www.gadnic.com.ar/microfonos-profesionales/microfono-condenser-profesional-gamer-stream")
    assert r.allows("/")
    assert not r.allows("https://www.gadnic.com.ar/microfonos-profesionales?brand=gadnic")
    assert not r.allows("https://www.gadnic.com.ar/?s=teclado")
    assert not r.allows("/accesorios?orden=precio")


def test_our_bot_falls_into_the_star_group_not_into_the_ai_training_group():
    r = store_robots.parse(GADNIC, agent="HugoPriceBot/1.0")
    assert r.allows("/aspiradoras-robot/robot-irobot-aspiradora-roomba-650")      # no cae en "Disallow: /"
    assert r.crawl_delay is None                                                   # el de Bingbot no es nuestro
    assert r.sitemaps == ("https://www.gadnic.com.ar/sitemap.xml",)


def test_a_group_named_after_us_wins_over_the_star_group():
    text = "User-agent: *\nDisallow: /\n\nUser-agent: HugoPriceBot\nDisallow: /privado/\n"
    r = store_robots.parse(text, agent="HugoPriceBot/1.0")
    assert r.allows("/productos/x") and not r.allows("/privado/x")


def test_tiendanube_allows_products_and_blocks_search_in_every_form():
    r = store_robots.parse(TIENDANUBE)
    assert r.allows("https://www.casaperfecta.com.ar/productos/picador-de-ajo/")
    assert not r.allows("https://www.casaperfecta.com.ar/search/?q=tapa")
    assert not r.allows("https://www.casaperfecta.com.ar/ar/search/?q=Tapas")      # las que trae el sitemap
    assert not r.allows("/productos/x/?view=grid")
    assert not r.allows("/productos/x/?utm=1&srsltid=abc")                         # comodín en el medio
    assert not r.allows("/checkout/start") and not r.allows("/admin/")
    assert r.crawl_delay is None                                                   # MJ12bot no es nuestro


def test_longest_match_wins_and_allow_wins_a_tie():
    text = "User-agent: *\nDisallow: /a/\nAllow: /a/b/\nDisallow: /c\nAllow: /c\n"
    r = store_robots.parse(text)
    assert r.allows("/a/b/x") and not r.allows("/a/x")
    assert r.allows("/c")                                                          # empate: gana Allow


def test_dollar_anchors_the_end_of_the_pattern():
    r = store_robots.parse("User-agent: *\nDisallow: /*.pdf$\n")
    assert not r.allows("/manual.pdf") and r.allows("/manual.pdf?x=1") and r.allows("/manual.pdf.html")


def test_crawl_delay_for_our_group_is_read_and_capped():
    assert store_robots.parse("User-agent: *\nCrawl-delay: 7\n").crawl_delay == 7.0
    assert store_robots.parse("User-agent: *\nCrawl-delay: 9999\n").crawl_delay == store_robots.MAX_CRAWL_DELAY_S
    assert store_robots.parse("User-agent: *\nCrawl-delay: abc\n").crawl_delay is None


def test_empty_disallow_and_garbage_do_not_block_anything():
    r = store_robots.parse("User-agent: *\nDisallow:\n\nesto no es una linea\n:\n")
    assert r.allows("/lo-que-sea")
    assert store_robots.parse("").allows("/x")


@pytest.mark.parametrize("status,text,expected", [
    (200, "User-agent: *\nDisallow: /x\n", False),
    (404, "", True),            # no hay robots.txt: sin restricciones
    (410, "", True),
    (500, "", False),           # no se pudo confirmar: no se rastrea
    (503, "", False),
    (None, "", False),          # sin respuesta
])
def test_robots_fetch_failures_follow_rfc_9309(status, text, expected):
    assert store_robots.from_status(status, text).allows("/x") is expected


def test_a_huge_robots_file_is_bounded():
    text = "User-agent: *\n" + "".join(f"Disallow: /p{i}/\n" for i in range(50_000))
    r = store_robots.parse(text)
    assert len(r.rules) <= store_robots.MAX_RULES


# ─── saneado de links, fotos y hosts ────────────────────────────────────────

CP = "https://www.casaperfecta.com.ar"
TN_HOSTS = store_urls.default_image_hosts("tiendanube", CP)
GD_HOSTS = ("gadnic.com.ar", "*.bidcom.com.ar")


def test_default_image_hosts():
    assert TN_HOSTS == ("casaperfecta.com.ar", "acdn*.mitiendanube.com")
    assert store_urls.default_image_hosts("jsonld_sitemap", "https://www.gadnic.com.ar") == ("gadnic.com.ar",)


@pytest.mark.parametrize("url,ok", [
    ("https://www.casaperfecta.com.ar/productos/picador-de-ajo/", True),
    ("https://casaperfecta.com.ar/productos/x/", True),
    ("https://tienda.casaperfecta.com.ar/x", False),                        # solo el host de la tienda y su www
    ("https://www.casaperfecta.com.ar/x", True),
    ("http://www.casaperfecta.com.ar/productos/x/", False),                  # solo https
    ("https://casaperfecta.com.ar.evil.com/productos/x/", False),
    ("https://evil.com/?r=casaperfecta.com.ar", False),
    ("https://casaperfecta.com.ar@evil.com/x", False),                       # userinfo
    ("https://www.casaperfecta.com.ar:8443/x", False),
    ("javascript:alert(1)", False),
    ("//evil.com/x", False),
    ("https://www.casaperfecta.com.ar/x y", False),                          # espacio / control
    ("https://www.casaperfecta.com.ar\\@evil.com/", False),
    ("", False), (None, False), (123, False),
])
def test_safe_link_only_keeps_https_links_to_the_store(url, ok):
    assert bool(store_urls.safe_link(url, CP)) is ok


def test_safe_link_drops_the_fragment():
    assert store_urls.safe_link("https://www.casaperfecta.com.ar/productos/x/#reviews", CP) == \
        "https://www.casaperfecta.com.ar/productos/x/"


@pytest.mark.parametrize("url,expected", [
    ("https://acdn-us.mitiendanube.com/stores/001/133/924/products/a-480-0.webp",
     "https://acdn-us.mitiendanube.com/stores/001/133/924/products/a-480-0.webp"),
    ("//acdn-us.mitiendanube.com/stores/001/products/a-1024-1024.webp",
     "https://acdn-us.mitiendanube.com/stores/001/products/a-1024-1024.webp"),   # protocolo relativo
    ("http://acdn-us.mitiendanube.com/stores/001/products/a.webp",
     "https://acdn-us.mitiendanube.com/stores/001/products/a.webp"),             # se sube a https
    ("https://evil.com/mitiendanube.com/a.webp", None),
    ("https://mitiendanube.com.evil.com/a.webp", None),
    ("https://acdn-us.mitiendanube.com@evil.com/a.webp", None),
    ("data:image/png;base64,AAAA", None), ("file:///etc/passwd", None), ("", None), (None, None),
])
def test_safe_image_tiendanube(url, expected):
    assert store_urls.safe_image(url, TN_HOSTS) == expected


def test_safe_image_for_a_cdn_resizer_checks_the_inner_url_too():
    ok = "https://images.bidcom.com.ar/resize?src=https://static.bidcom.com.ar/publicacionesML/productos/X/1000x1000-X.jpg&w=800&q=100"
    assert store_urls.safe_image(ok, GD_HOSTS) == ok
    assert store_urls.safe_image("https://static.bidcom.com.ar/publicacionesML/productos/X/1000x1000-X.jpg", GD_HOSTS)
    evil = "https://images.bidcom.com.ar/resize?src=http://169.254.169.254/latest/meta-data&w=800"
    assert store_urls.safe_image(evil, GD_HOSTS) is None
    assert store_urls.safe_image("https://images.bidcom.com.ar/resize?src=https://evil.com/a.jpg", GD_HOSTS) is None


def test_hosts_csv_rejects_broad_or_malformed_domains():
    assert store_urls.parse_hosts("Gadnic.com.ar, *.bidcom.com.ar ; gadnic.com.ar") == ("gadnic.com.ar", "*.bidcom.com.ar")
    assert store_urls.parse_hosts("com.ar, localhost, 10.0.0.1, a b, cloudfront.net, ok-host.com") == ("ok-host.com",)
    assert store_urls.invalid_hosts("gadnic.com.ar, com.ar, 10.0.0.1") == ["com.ar", "10.0.0.1"]


def test_judge_images_accepts_store_photos_only_for_registered_hosts():
    from app.pricing import judge_images

    photo = "https://acdn-us.mitiendanube.com/stores/001/products/a.webp"
    store_urls.set_allowed_image_hosts([])
    assert judge_images.allowed_url(photo) is None                       # sin tiendas activas: nada pasa
    store_urls.set_allowed_image_hosts(["acdn*.mitiendanube.com"])
    try:
        assert judge_images.allowed_url(photo) == photo
        assert judge_images.allowed_url("https://evil.com/a.webp") is None
    finally:
        store_urls.set_allowed_image_hosts([])
