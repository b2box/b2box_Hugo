"""Fallback de render con browser real (Camoufox) para las fichas bloqueadas.

Lo que importa cubrir:
  1. El guard anti-SSRF del browser: un redirect o un subrecurso hacia una red
     interna tiene que abortarse. Es el riesgo nuevo que introduce este camino.
  2. Que `extract()` degrade igual que antes cuando el browser no está: sin
     camoufox instalado nada cambia.
  3. Que cuando el browser SÍ rescata la ficha, `extract()` devuelva las fotos
     en vez del `BlockedByCaptcha` que devolvía antes.

Nunca se levanta un browser de verdad: el módulo se stubbea. Los tests corren
en CI sin Firefox ni red.
"""

import httpx
import pytest

from app.ingest import browser_fetch, image_from_url
from app.ingest.image_from_url import BlockedByCaptcha, extract
from app.net_guard import SsrfBlocked

# HTML de la página anti-bot que devuelve MercadoLibre a un servidor: HTTP 200,
# sin og:image ni JSON-LD. Es lo que hoy termina en site_blocked.
_ANTIBOT_HTML = b"""
<html><head><title>Trafico inusual</title></head>
<body><div class="suspicious-traffic">Detectamos trafico inusual</div></body></html>
"""


def _response(url: str, *, body: bytes = b"", content_type: str = "text/html",
              status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers={"content-type": content_type},
        content=body,
        request=httpx.Request("GET", url),
    )


def _patch_get(monkeypatch, response: httpx.Response):
    async def fake_safe_get(url, **kwargs):  # noqa: ARG001
        return response

    monkeypatch.setattr(image_from_url, "safe_get", fake_safe_get)


# ─── Guard anti-SSRF del browser ───────────────────────────────────


class _FakeRoute:
    """Doble de playwright.Route: registra si el request siguió o se abortó."""

    def __init__(self):
        self.continued = False
        self.aborted = False

    async def continue_(self):
        self.continued = True

    async def abort(self):
        self.aborted = True


class _FakeRequest:
    def __init__(self, url: str):
        self.url = url


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # metadata cloud
        "http://127.0.0.1:8000/admin",               # loopback
        "http://10.0.0.5/internal",                  # red privada
        "http://[::1]/",                             # loopback v6
        "file:///etc/passwd",                        # scheme no permitido
        "ftp://example.com/x",                       # scheme no permitido
    ],
)
async def test_guard_aborta_requests_internos(url):
    """Cada request del browser pasa por el guard, no solo la URL de entrada.

    Sin esto un redirect 302 a 169.254.169.254 saldría desde el browser sin que
    `assert_public_url` —que solo vio la URL original— se entere.
    """
    route, blocked = _FakeRoute(), []
    await browser_fetch._guard_route(route, _FakeRequest(url), blocked)
    assert route.aborted, f"deberia abortar {url}"
    assert not route.continued
    assert blocked == [url]


async def test_guard_deja_pasar_hosts_publicos(monkeypatch):
    async def fake_public(host):  # noqa: ARG001
        return True

    monkeypatch.setattr(browser_fetch, "_host_is_public", fake_public)
    route, blocked = _FakeRoute(), []
    await browser_fetch._guard_route(
        route, _FakeRequest("https://http2.mlstatic.com/foto.jpg"), blocked
    )
    assert route.continued
    assert not route.aborted
    assert blocked == []


async def test_guard_falla_cerrado_si_el_dns_no_resuelve(monkeypatch):
    """DNS que falla → se aborta. Ante la duda no se sale a la red."""
    async def boom(host):  # noqa: ARG001
        raise OSError("dns down")

    monkeypatch.setattr(browser_fetch, "_host_is_public", boom)
    route, blocked = _FakeRoute(), []
    await browser_fetch._guard_route(
        route, _FakeRequest("https://lo-que-sea.example/x.jpg"), blocked
    )
    assert route.aborted


async def test_render_rechaza_url_no_publica(monkeypatch):
    monkeypatch.setattr(browser_fetch, "available", lambda: True)
    with pytest.raises(SsrfBlocked):
        await browser_fetch.render("http://127.0.0.1/admin")


# ─── Degradación cuando no hay browser ─────────────────────────────


async def test_sin_browser_sigue_dando_blocked(monkeypatch):
    """Sin camoufox el comportamiento es el de siempre: site_blocked."""
    monkeypatch.setattr(browser_fetch, "available", lambda: False)
    monkeypatch.setattr(image_from_url.meli, "enabled", lambda: False)
    _patch_get(monkeypatch, _response(
        "https://articulo.mercadolibre.com.ar/MLA-1", body=_ANTIBOT_HTML))

    with pytest.raises(BlockedByCaptcha):
        await extract("https://articulo.mercadolibre.com.ar/MLA-1")


async def test_browser_que_falla_no_empeora_el_error(monkeypatch):
    """Si el render explota, el llamador recibe el error original, no el del browser."""
    monkeypatch.setattr(browser_fetch, "available", lambda: True)
    monkeypatch.setattr(image_from_url.meli, "enabled", lambda: False)

    async def boom(url, **kwargs):  # noqa: ARG001
        raise browser_fetch.BrowserUnavailable("segfault")

    monkeypatch.setattr(browser_fetch, "render", boom)
    _patch_get(monkeypatch, _response(
        "https://articulo.mercadolibre.com.ar/MLA-1", body=_ANTIBOT_HTML))

    with pytest.raises(BlockedByCaptcha):
        await extract("https://articulo.mercadolibre.com.ar/MLA-1")


# ─── El browser rescata la ficha ───────────────────────────────────


def _stub_render(monkeypatch, page: browser_fetch.RenderedPage):
    monkeypatch.setattr(browser_fetch, "available", lambda: True)

    async def fake_render(url, **kwargs):  # noqa: ARG001
        return page

    monkeypatch.setattr(browser_fetch, "render", fake_render)


async def test_browser_rescata_una_ficha_bloqueada(monkeypatch):
    """El caso que motiva todo: ML bloquea el fetch plano, el browser sí la lee."""
    monkeypatch.setattr(image_from_url.meli, "enabled", lambda: False)
    _stub_render(monkeypatch, browser_fetch.RenderedPage(
        html="<html><head><title>Auricular X</title></head><body></body></html>",
        image_urls=[
            "https://http2.mlstatic.com/D_NQ_1-F.jpg",
            "https://http2.mlstatic.com/D_NQ_2-F.jpg",
        ],
        title="Auricular X",
        final_url="https://articulo.mercadolibre.com.ar/MLA-1",
    ))
    _patch_get(monkeypatch, _response(
        "https://articulo.mercadolibre.com.ar/MLA-1", body=_ANTIBOT_HTML))

    result = await extract("https://articulo.mercadolibre.com.ar/MLA-1")

    assert result.image_urls == [
        "https://http2.mlstatic.com/D_NQ_1-F.jpg",
        "https://http2.mlstatic.com/D_NQ_2-F.jpg",
    ]
    assert result.marketplace == "mercadolibre"
    assert result.title == "Auricular X"


async def test_browser_prefiere_json_ld_sobre_los_img_sueltos(monkeypatch):
    """El DOM renderizado también trae JSON-LD, y es más confiable que los <img>."""
    _stub_render(monkeypatch, browser_fetch.RenderedPage(
        html="""
        <html><head><script type="application/ld+json">
        {"@type":"Product","name":"Masajeador",
         "image":["https://cdn.example.com/oficial-1.jpg"]}
        </script></head><body></body></html>
        """,
        image_urls=["https://cdn.example.com/thumb-chico.jpg"],
        final_url="https://detail.1688.com/offer/9.html",
    ))
    _patch_get(monkeypatch, _response(
        "https://detail.1688.com/offer/9.html", body=b"<html><body></body></html>"))

    result = await extract("https://detail.1688.com/offer/9.html")

    assert result.image_urls[0] == "https://cdn.example.com/oficial-1.jpg"
    assert "https://cdn.example.com/thumb-chico.jpg" in result.image_urls
    assert result.title == "Masajeador"


async def test_tope_de_diez_fotos(monkeypatch):
    """La galería entera entra, pero con tope: 10 es lo que compara el matcher."""
    _stub_render(monkeypatch, browser_fetch.RenderedPage(
        html="<html><body></body></html>",
        image_urls=[f"https://cdn.example.com/foto-{i}.jpg" for i in range(30)],
        final_url="https://detail.1688.com/offer/9.html",
    ))
    _patch_get(monkeypatch, _response(
        "https://detail.1688.com/offer/9.html", body=b"<html><body></body></html>"))

    result = await extract("https://detail.1688.com/offer/9.html")

    assert len(result.image_urls) == 10
    assert result.image_urls[0] == "https://cdn.example.com/foto-0.jpg"


async def test_browser_descarta_logos_y_sprites(monkeypatch):
    """El DOM trae de todo; los logos no son la foto del producto."""
    _stub_render(monkeypatch, browser_fetch.RenderedPage(
        html="<html><body></body></html>",
        image_urls=[
            "https://cdn.example.com/logo-header.png",
            "https://cdn.example.com/producto-real.jpg",
            "https://cdn.example.com/sprite-icons.png",
        ],
        final_url="https://detail.1688.com/offer/9.html",
    ))
    _patch_get(monkeypatch, _response(
        "https://detail.1688.com/offer/9.html", body=b"<html><body></body></html>"))

    result = await extract("https://detail.1688.com/offer/9.html")

    assert result.image_urls == ["https://cdn.example.com/producto-real.jpg"]


async def test_browser_tambien_cubre_http_500(monkeypatch):
    """Un 5xx al fetch plano también merece el reintento con browser."""
    _stub_render(monkeypatch, browser_fetch.RenderedPage(
        html="<html><body></body></html>",
        image_urls=["https://cdn.example.com/producto.jpg"],
        final_url="https://spanish.alibaba.com/p/1.html",
    ))
    _patch_get(monkeypatch, _response(
        "https://spanish.alibaba.com/p/1.html", body=b"nope", status=503))

    result = await extract("https://spanish.alibaba.com/p/1.html")

    assert result.image_urls == ["https://cdn.example.com/producto.jpg"]
