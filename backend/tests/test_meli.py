"""API oficial de MercadoLibre — parseo de URLs, token y lectura de fotos.

ML no le contesta a un servidor por HTTP normal, así que este camino es el único
que funciona para el marketplace más importante en Argentina. Los tests congelan
las formas de URL reales: si ML agrega una nueva, acá se ve.
"""

import httpx
import pytest

from app.ingest import meli


# ─── Parseo de URLs ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected_id,expected_kind",
    [
        # Publicación clásica
        ("https://articulo.mercadolibre.com.ar/MLA-1755665491-masajeador-_JM",
         "MLA1755665491", "item"),
        # Ficha de catálogo (URL /up/)
        ("https://www.mercadolibre.com.ar/masajeador-cervical/up/MLAU3916488174",
         "MLAU3916488174", "product"),
        # Ficha de catálogo (URL /p/)
        ("https://www.mercadolibre.com.ar/algo/p/MLA987654321",
         "MLA987654321", "product"),
        # Brasil
        ("https://produto.mercadolivre.com.br/MLB-2233445566-coisa-_JM",
         "MLB2233445566", "item"),
        # México
        ("https://articulo.mercadolibre.com.mx/MLM-1122334455-cosa-_JM",
         "MLM1122334455", "item"),
    ],
)
def test_parse_url_formas_conocidas(url, expected_id, expected_kind):
    ref = meli.parse_url(url)
    assert ref is not None
    assert ref.id == expected_id
    assert ref.kind == expected_kind


def test_item_id_del_query_le_gana_al_id_de_catalogo():
    """La URL real que llega desde una búsqueda trae los dos ids.

    El del query apunta a la publicación concreta que el cliente estaba mirando;
    el de la ruta es la ficha de catálogo genérica. Queremos la publicación.
    """
    url = (
        "https://www.mercadolibre.com.ar/masajeador-cervical-shiatsu/up/MLAU3916488174"
        "?pdp_filters=item_id:MLA1755665491#is_advertising=true&position=4"
    )
    ref = meli.parse_url(url)
    assert ref.id == "MLA1755665491"
    assert ref.kind == "item"


def test_item_id_url_encodeado_en_el_query():
    url = (
        "https://www.mercadolibre.com.ar/algo/up/MLAU391"
        "?pdp_filters=item_id%3AMLA1755665491"
    )
    ref = meli.parse_url(url)
    assert ref.id == "MLA1755665491"


@pytest.mark.parametrize("url", [
    "",
    "https://www.mercadolibre.com.ar/ofertas",
    "https://articulo.mercadolibre.com.ar/sin-id-aca",
    "https://es.made-in-china.com/producto.html",
])
def test_parse_url_sin_id(url):
    assert meli.parse_url(url) is None


# ─── Token ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_token():
    meli._reset_token_for_tests()
    yield
    meli._reset_token_for_tests()


def _fake_settings(client_id="cid", client_secret="sec"):
    class S:
        meli_client_id = client_id
        meli_client_secret = client_secret
    return S()


def test_enabled_depende_de_las_credenciales(monkeypatch):
    monkeypatch.setattr(meli, "get_settings", lambda: _fake_settings())
    assert meli.enabled() is True
    monkeypatch.setattr(meli, "get_settings", lambda: _fake_settings("", ""))
    assert meli.enabled() is False


def _patch_http(monkeypatch, handler):
    """Reemplaza httpx.AsyncClient por uno que enruta con `handler(method, url)`."""
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, headers=None):  # noqa: ARG002
            return handler("POST", url)

        async def get(self, url, headers=None):  # noqa: ARG002
            return handler("GET", url)

    monkeypatch.setattr(meli.httpx, "AsyncClient", lambda **kw: FakeClient())


def _resp(status, body, url="https://api.mercadolibre.com/x"):
    return httpx.Response(status_code=status, json=body,
                          request=httpx.Request("GET", url))


@pytest.mark.asyncio
async def test_token_se_pide_una_sola_vez(monkeypatch):
    """El token dura ~6 h: pedirlo en cada lookup sería un round-trip al pedo."""
    calls = {"n": 0}

    def handler(method, url):
        if "oauth/token" in url:
            calls["n"] += 1
            return _resp(200, {"access_token": "tok-123", "expires_in": 21600})
        return _resp(200, {})

    monkeypatch.setattr(meli, "get_settings", lambda: _fake_settings())
    _patch_http(monkeypatch, handler)

    assert await meli.get_token() == "tok-123"
    assert await meli.get_token() == "tok-123"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_sin_credenciales_el_token_falla_claro(monkeypatch):
    monkeypatch.setattr(meli, "get_settings", lambda: _fake_settings("", ""))
    with pytest.raises(meli.MeliError) as exc:
        await meli.get_token()
    assert "MELI_CLIENT_ID" in str(exc.value)


# ─── Lectura de publicaciones ──────────────────────────────────────


_ITEM = {
    "id": "MLA1755665491",
    "title": "Masajeador Cervical Shiatsu Eléctrico",
    "permalink": "https://articulo.mercadolibre.com.ar/MLA-1755665491-x-_JM",
    "thumbnail": "https://http2.mlstatic.com/thumb.jpg",
    "pictures": [
        {"id": "1", "url": "http://http2.mlstatic.com/a.jpg",
         "secure_url": "https://http2.mlstatic.com/a.jpg"},
        {"id": "2", "url": "http://http2.mlstatic.com/b.jpg",
         "secure_url": "https://http2.mlstatic.com/b.jpg"},
    ],
}


@pytest.mark.asyncio
async def test_fetch_from_url_devuelve_fotos_y_titulo(monkeypatch):
    def handler(method, url):
        if "oauth/token" in url:
            return _resp(200, {"access_token": "t", "expires_in": 21600})
        if "/items/MLA1755665491" in url:
            return _resp(200, _ITEM)
        return _resp(404, {"error": "not found"})

    monkeypatch.setattr(meli, "get_settings", lambda: _fake_settings())
    _patch_http(monkeypatch, handler)

    item = (
        await meli.fetch_from_url(
            "https://www.mercadolibre.com.ar/x/up/MLAU391?pdp_filters=item_id:MLA1755665491"
        )
        or [None]
    )[0]
    assert item is not None
    assert item.title.startswith("Masajeador Cervical")
    # https por sobre http cuando vienen las dos.
    assert item.image_urls == [
        "https://http2.mlstatic.com/a.jpg",
        "https://http2.mlstatic.com/b.jpg",
    ]


@pytest.mark.asyncio
async def test_catalogo_que_falla_reintenta_como_publicacion(monkeypatch):
    """Una URL /up/ puede apuntar a algo que no existe como ficha de catálogo."""
    vistos: list[str] = []

    def handler(method, url):
        if "oauth/token" in url:
            return _resp(200, {"access_token": "t", "expires_in": 21600})
        vistos.append(url)
        if "/products/" in url:
            return _resp(404, {"error": "not found"})
        return _resp(200, _ITEM)

    monkeypatch.setattr(meli, "get_settings", lambda: _fake_settings())
    _patch_http(monkeypatch, handler)

    item = (
        await meli.fetch_from_url(
            "https://www.mercadolibre.com.ar/x/up/MLAU3916488174"
        )
        or [None]
    )[0]
    assert item is not None
    assert any("/products/" in u for u in vistos)
    assert any("/items/" in u for u in vistos)


@pytest.mark.asyncio
async def test_sin_fotos_devuelve_none(monkeypatch):
    """Un item sin fotos no sirve: mejor None que un match contra nada."""
    def handler(method, url):
        if "oauth/token" in url:
            return _resp(200, {"access_token": "t", "expires_in": 21600})
        return _resp(200, {"id": "MLA1", "title": "x", "pictures": []})

    monkeypatch.setattr(meli, "get_settings", lambda: _fake_settings())
    _patch_http(monkeypatch, handler)

    assert await meli.fetch_from_url("https://articulo.mercadolibre.com.ar/MLA-123456-x") == []


@pytest.mark.asyncio
async def test_api_caida_no_rompe_el_lookup(monkeypatch):
    """Si la API de ML falla, el llamador tiene que poder seguir con scraping."""
    def handler(method, url):
        if "oauth/token" in url:
            return _resp(500, {"error": "boom"})
        return _resp(500, {})

    monkeypatch.setattr(meli, "get_settings", lambda: _fake_settings())
    _patch_http(monkeypatch, handler)

    assert await meli.fetch_from_url("https://articulo.mercadolibre.com.ar/MLA-123456-x") == []


@pytest.mark.asyncio
async def test_usa_thumbnail_si_no_hay_pictures(monkeypatch):
    def handler(method, url):
        if "oauth/token" in url:
            return _resp(200, {"access_token": "t", "expires_in": 21600})
        return _resp(200, {"id": "MLA1", "title": "x",
                           "thumbnail": "https://http2.mlstatic.com/t.jpg"})

    monkeypatch.setattr(meli, "get_settings", lambda: _fake_settings())
    _patch_http(monkeypatch, handler)

    item = (
        await meli.fetch_from_url(
            "https://articulo.mercadolibre.com.ar/MLA-123456-x"
        )
        or [None]
    )[0]
    assert item.image_urls == ["https://http2.mlstatic.com/t.jpg"]


# ─── Precio de mercado ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_precio_de_mercado_es_el_mas_barato(monkeypatch):
    """Una ficha de catálogo tiene varios vendedores con precios distintos.

    Devolvemos el más barato: es contra ese contra el que compite quien quiera
    revender, así que es el que hace honesto el cálculo de margen.
    """
    def handler(method, url):
        if "oauth/token" in url:
            return _resp(200, {"access_token": "t", "expires_in": 21600})
        return _resp(200, {"results": [
            {"item_id": "MLA1", "price": 11055.07, "currency_id": "ARS"},
            {"item_id": "MLA2", "price": 8937.12, "currency_id": "ARS"},
            {"item_id": "MLA3", "price": 11456.2, "currency_id": "ARS"},
        ]})

    monkeypatch.setattr(meli, "get_settings", lambda: _fake_settings())
    _patch_http(monkeypatch, handler)

    cents, currency, sellers = await meli.fetch_market_price("MLA2062278024")

    assert cents == 893712
    assert currency == "ARS"
    assert sellers == 3


@pytest.mark.asyncio
async def test_sin_vendedores_no_hay_precio(monkeypatch):
    def handler(method, url):
        if "oauth/token" in url:
            return _resp(200, {"access_token": "t", "expires_in": 21600})
        return _resp(200, {"results": []})

    monkeypatch.setattr(meli, "get_settings", lambda: _fake_settings())
    _patch_http(monkeypatch, handler)

    assert await meli.fetch_market_price("MLA1") == (None, None, 0)


@pytest.mark.asyncio
async def test_si_falla_el_precio_el_lookup_sigue(monkeypatch):
    """Sin precio de mercado el resultado sigue siendo útil: foto y título."""
    def handler(method, url):
        if "oauth/token" in url:
            return _resp(200, {"access_token": "t", "expires_in": 21600})
        if "/items?" in url:
            return _resp(500, {})
        return _resp(200, {**_ITEM, "id": "MLA2062278024"})

    monkeypatch.setattr(meli, "get_settings", lambda: _fake_settings())
    _patch_http(monkeypatch, handler)

    item = (
        await meli.fetch_from_url(
            "https://www.mercadolibre.com.ar/x/p/MLA2062278024"
        )
        or [None]
    )[0]

    assert item is not None
    assert item.image_urls
    assert item.price_cents is None


@pytest.mark.asyncio
async def test_una_publicacion_trae_su_propio_precio(monkeypatch):
    """Las publicaciones sueltas no tienen lista de vendedores: el precio viene
    en el mismo payload."""
    def handler(method, url):
        if "oauth/token" in url:
            return _resp(200, {"access_token": "t", "expires_in": 21600})
        return _resp(200, {**_ITEM, "price": 8937.12, "currency_id": "ARS"})

    monkeypatch.setattr(meli, "get_settings", lambda: _fake_settings())
    _patch_http(monkeypatch, handler)

    item = (
        await meli.fetch_from_url(
            "https://articulo.mercadolibre.com.ar/MLA-123456-x"
        )
        or [None]
    )[0]

    assert item.price_cents == 893712
    assert item.seller_count == 1


# ─── Precio de una ficha que la API no deja leer ───────────────────

_CATALOGO = {
    "results": [{
        "id": "MLA9999",
        "name": "Set Decantador De Whisky Con Globo Y 4 Vasos",
        "pictures": [{"id": "9", "secure_url": "https://http2.mlstatic.com/c.jpg"}],
        "permalink": "https://www.mercadolibre.com.ar/p/MLA9999",
    }]
}


@pytest.mark.asyncio
async def test_ficha_bloqueada_igual_da_el_precio_real(monkeypatch):
    """403 en la ficha no significa 403 en su lista de vendedores.

    Medido contra ML: `/products/MLAU…` cae en la política que bloquea las
    publicaciones ajenas, pero `/products/MLAU…/items` —los vendedores de esa
    misma ficha, con su precio— responde 200. El link llega igual al catálogo
    por las fotos, pero el precio que mostramos es el de la publicación real,
    no el del candidato parecido. Antes salía `None`.
    """
    pedidos: list[str] = []

    def handler(method, url):
        pedidos.append(url)
        if "oauth/token" in url:
            return _resp(200, {"access_token": "t", "expires_in": 21600})
        if "/products/search" in url:
            return _resp(200, _CATALOGO)
        if "/products/MLAU3227241523/items" in url:
            return _resp(200, {"results": [{"price": 150000, "currency_id": "ARS"}]})
        return _resp(403, {"message": "forbidden"})

    monkeypatch.setattr(meli, "get_settings", lambda: _fake_settings())
    _patch_http(monkeypatch, handler)

    items = await meli.fetch_from_url(
        "https://www.mercadolibre.com.ar/set-decantador-globo-2-vasos/up/MLAU3227241523"
    )

    assert len(items) == 1
    assert items[0].price_cents == 15_000_000
    assert items[0].currency == "ARS"
    assert items[0].seller_count == 1
    assert items[0].resolved_by == "catalog"
    # Y no gastamos un request por candidato pidiendo un precio que ya sabemos.
    assert not any("/products/MLA9999/items" in u for u in pedidos)


@pytest.mark.asyncio
async def test_sin_precio_de_la_ficha_cada_candidato_pone_el_suyo(monkeypatch):
    """Si tampoco se puede leer la lista de vendedores, el candidato aporta lo
    que tenga: es aproximado, pero es mejor que no mostrar precio."""
    def handler(method, url):
        if "oauth/token" in url:
            return _resp(200, {"access_token": "t", "expires_in": 21600})
        if "/products/search" in url:
            return _resp(200, _CATALOGO)
        if "/products/MLA9999/items" in url:
            return _resp(200, {"results": [{"price": 99000, "currency_id": "ARS"}]})
        return _resp(403, {"message": "forbidden"})

    monkeypatch.setattr(meli, "get_settings", lambda: _fake_settings())
    _patch_http(monkeypatch, handler)

    items = await meli.fetch_from_url(
        "https://www.mercadolibre.com.ar/set-decantador-globo-2-vasos/up/MLAU3227241523"
    )

    assert items[0].price_cents == 9_900_000
    assert items[0].seller_count == 1
