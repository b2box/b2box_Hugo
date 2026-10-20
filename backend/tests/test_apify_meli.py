"""Apify como último recurso para links de Mercado Libre (app.ingest.apify_meli).

Sin red: la API de Apify se reemplaza con un MockTransport de httpx. Lo que se
cubre es lo que puede salir caro o inseguro: que solo vayan links de ML, que
el token no aparezca nunca en un log, que el cupo diario corte, que las fotos
que vuelven pasen por la lista blanca y que cualquier falla de Apify deje el
flujo como estaba (BlockedByCaptcha).
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from sqlmodel import Session, SQLModel, select

from app.config import get_settings
from app.db.models import Setting
from app.db.session import engine
from app.ingest import apify_meli, image_from_url
from app.ingest.image_from_url import BlockedByCaptcha, extract

TOKEN = "apify_api_SECRETO_no_tiene_que_salir_1234"
UP_URL = (
    "https://www.mercadolibre.com.ar/masajeador-cervical-zevya/up/MLAU3916488174"
    "?pdp_filters=item_id:MLA1755665491"
)
PIC_ID = "735512-MLA74859284738_032024"
PIC_ID_2 = "991122-MLA80000000001_012025"
THUMB = f"https://http2.mlstatic.com/D_NQ_NP_{PIC_ID}-R.webp"
BIG = f"https://http2.mlstatic.com/D_NQ_NP_2X_{PIC_ID}-F.webp"
THUMB_2 = f"https://http2.mlstatic.com/D_NQ_NP_{PIC_ID_2}-R.webp"
BIG_2 = f"https://http2.mlstatic.com/D_NQ_NP_2X_{PIC_ID_2}-F.webp"



def _up(n: int) -> str:
    return f"https://www.mercadolibre.com.ar/x/up/MLAU{3916488170 + n}"


KHADINAKBAR_ITEM = {
    "itemId": "MLA1755665491",
    "title": "Masajeador Cervical Inteligente Zevya",
    "price": None,
    "currencyId": "ARS",
    "pictures": [THUMB, BIG, THUMB_2, BIG_2],
    "thumbnail": THUMB,
    "url": "https://articulo.mercadolibre.com.ar/MLA-1755665491-masajeador-_JM",
}


@pytest.fixture
def apify_on(monkeypatch):
    """Apify prendido con cupo, contador del día en cero."""
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        for row in s.exec(select(Setting)).all():
            s.delete(row)
        s.commit()
    settings = get_settings()
    monkeypatch.setattr(settings, "apify_token", TOKEN, raising=False)
    monkeypatch.setattr(settings, "apify_meli_actor",
                        "khadinakbar/mercadolibre-all-in-one-scraper", raising=False)
    monkeypatch.setattr(settings, "apify_meli_timeout_s", 90, raising=False)
    monkeypatch.setattr(apify_meli, "_daily_budget", lambda: 100)
    apify_meli._reset_for_tests()
    yield settings
    apify_meli._reset_for_tests()


class FakeApify:
    """Hace de la API de Apify y anota cada request que le llega."""

    def __init__(self, *, status: int = 201, payload: object = None,
                 raw: bytes | None = None, exc: Exception | None = None,
                 delay_s: float = 0.0):
        self.status = status
        self.payload = [KHADINAKBAR_ITEM] if payload is None else payload
        self.raw = raw
        self.exc = exc
        self.delay_s = delay_s
        self.requests: list[httpx.Request] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.exc is not None:
            raise self.exc
        if self.status not in (200, 201):
            # Un body con datos que NO pueden terminar en el log.
            return httpx.Response(self.status, json={"error": {"message": f"token {TOKEN}"}})
        body = self.raw if self.raw is not None else json.dumps(self.payload).encode()
        return httpx.Response(self.status, content=body,
                              headers={"content-type": "application/json"})

    def install(self, monkeypatch) -> "FakeApify":
        def factory(total_s):  # noqa: ARG001
            return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

        monkeypatch.setattr(apify_meli, "_new_client", factory)
        return self


def _no_safe_get(monkeypatch):
    calls: list[str] = []

    async def fake(url, **kwargs):  # noqa: ARG001
        calls.append(url)
        return httpx.Response(
            200, headers={"content-type": "text/html"},
            content=b"<html><head><title>suspicious-traffic</title></head></html>",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(image_from_url, "safe_get", fake)
    return calls


def _no_browser(monkeypatch):
    calls: list[str] = []

    async def fake(url, marketplace):  # noqa: ARG001
        calls.append(url)
        return None

    monkeypatch.setattr(image_from_url, "_via_browser", fake)
    return calls


def _meli_api(monkeypatch, candidates, price=(1999900, "ARS", 3)):
    async def fake_fetch(url):  # noqa: ARG001
        return list(candidates)

    price_calls: list[str] = []

    async def fake_price(product_id):
        price_calls.append(product_id)
        return price

    monkeypatch.setattr(image_from_url.meli, "enabled", lambda: True)
    monkeypatch.setattr(image_from_url.meli, "fetch_from_url", fake_fetch)
    monkeypatch.setattr(image_from_url.meli, "fetch_market_price", fake_price)
    return price_calls


# ─── El caso que motivó todo: /up/ que la API no deja leer ─────────


@pytest.mark.asyncio
async def test_link_up_resuelto_por_apify(apify_on, monkeypatch):
    fake = FakeApify().install(monkeypatch)
    price_calls = _meli_api(monkeypatch, [])
    plain = _no_safe_get(monkeypatch)
    rendered = _no_browser(monkeypatch)

    out = await extract(UP_URL)

    assert out.source == "apify"
    assert out.approximate is False
    assert out.image_urls == [BIG, BIG_2]
    assert out.title == "Masajeador Cervical Inteligente Zevya"
    assert out.canonical_url == KHADINAKBAR_ITEM["url"]
    # Apify no trajo precio: sale de /products/{MLAU}/items, como antes.
    assert price_calls == ["MLAU3916488174"]
    assert (out.market_price_cents, out.market_currency, out.market_seller_count) == (
        1999900, "ARS", 3)
    # Ni fetch plano ni browser: Apify ya resolvió.
    assert plain == [] and rendered == []

    (req,) = fake.requests
    assert req.method == "POST"
    assert req.url.host == "api.apify.com"
    assert req.url.path == (
        "/v2/acts/khadinakbar~mercadolibre-all-in-one-scraper/run-sync-get-dataset-items")
    assert req.url.params["timeout"] == "90"
    assert "maxTotalChargeUsd" in req.url.params
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(req.url)
    body = json.loads(req.content)
    assert body == {"startUrls": [{"url": UP_URL}], "mode": "product", "maxResults": 1}


@pytest.mark.asyncio
async def test_apify_con_precio_no_pregunta_a_la_api(apify_on, monkeypatch):
    item = dict(KHADINAKBAR_ITEM, price=25999.5)
    FakeApify(payload=[item]).install(monkeypatch)
    price_calls = _meli_api(monkeypatch, [])
    _no_safe_get(monkeypatch)
    _no_browser(monkeypatch)

    out = await extract(UP_URL)

    assert (out.market_price_cents, out.market_currency, out.market_seller_count) == (
        2599950, "ARS", 1)
    assert price_calls == []


@pytest.mark.asyncio
async def test_apify_le_gana_a_la_foto_prestada_del_catalogo(apify_on, monkeypatch):
    """La búsqueda por nombre trae fotos de OTROS productos: la ficha real leída
    por Apify vale más, y el browser (que desde el datacenter no puede) ni corre."""
    from app.ingest.meli import MeliItem

    FakeApify().install(monkeypatch)
    _meli_api(monkeypatch, [MeliItem(id="MLA9", title="Otro", resolved_by="catalog",
                                     image_urls=["https://http2.mlstatic.com/prestada.jpg"])])
    rendered = _no_browser(monkeypatch)

    out = await extract(UP_URL)

    assert out.source == "apify"
    assert out.approximate is False
    assert out.image_urls == [BIG, BIG_2]
    assert rendered == []


@pytest.mark.asyncio
async def test_ficha_directa_no_gasta_apify(apify_on, monkeypatch):
    from app.ingest.meli import MeliItem

    fake = FakeApify().install(monkeypatch)
    _meli_api(monkeypatch, [MeliItem(id="MLAU3916488174", title="Directa",
                                     image_urls=["https://http2.mlstatic.com/a.jpg"])])

    out = await extract(UP_URL)

    assert out.source == ""
    assert fake.requests == []


# ─── Apagado y cupo ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sin_token_no_llama_y_sigue_el_flujo_de_hoy(apify_on, monkeypatch):
    monkeypatch.setattr(apify_on, "apify_token", "", raising=False)
    fake = FakeApify().install(monkeypatch)
    _meli_api(monkeypatch, [])
    plain = _no_safe_get(monkeypatch)
    rendered = _no_browser(monkeypatch)

    assert apify_meli.enabled() is False
    with pytest.raises(BlockedByCaptcha):
        await extract(UP_URL)

    assert fake.requests == []
    # Sin Apify el camino es el de siempre: fetch plano y browser.
    assert plain == [UP_URL] and rendered == [UP_URL]


@pytest.mark.asyncio
async def test_actor_con_forma_rara_no_se_usa(apify_on, monkeypatch):
    monkeypatch.setattr(apify_on, "apify_meli_actor", "../../users/me", raising=False)
    fake = FakeApify().install(monkeypatch)

    assert await apify_meli.fetch(UP_URL) is None
    assert fake.requests == []


@pytest.mark.asyncio
async def test_budget_cero_es_apagado(apify_on, monkeypatch):
    monkeypatch.setattr(apify_meli, "_daily_budget", lambda: 0)
    fake = FakeApify().install(monkeypatch)

    assert await apify_meli.fetch(UP_URL) is None
    assert fake.requests == []


@pytest.mark.asyncio
async def test_cupo_diario_agotado(apify_on, monkeypatch, caplog):
    monkeypatch.setattr(apify_meli, "_daily_budget", lambda: 2)
    fake = FakeApify().install(monkeypatch)

    # Tres links distintos: el mismo dos veces saldría de la caché sin gastar.
    assert (await apify_meli.fetch(_up(1))).found
    assert (await apify_meli.fetch(_up(2))).found
    with caplog.at_level(logging.INFO, logger="app.ingest.apify_meli"):
        assert await apify_meli.fetch(_up(3)) is None

    assert len(fake.requests) == 2
    assert "cupo diario agotado" in caplog.text


@pytest.mark.asyncio
async def test_cupo_agotado_deja_el_browser_como_hoy(apify_on, monkeypatch):
    """Sin cupo no se esperó nada: el browser sigue teniendo su chance."""
    monkeypatch.setattr(apify_meli, "_daily_budget", lambda: 1)
    with Session(engine) as s:
        s.add(Setting(key=apify_meli.COUNTER_KEY, value=f"{apify_meli.daily_budget.today_utc()}:1"))
        s.commit()
    fake = FakeApify().install(monkeypatch)
    _meli_api(monkeypatch, [])
    _no_safe_get(monkeypatch)
    rendered = _no_browser(monkeypatch)

    with pytest.raises(BlockedByCaptcha):
        await extract(UP_URL)

    assert fake.requests == []
    assert rendered == [UP_URL]


# ─── Fallas de Apify: se loguean y el flujo sigue ──────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 402, 404, 408, 500])
async def test_errores_http_de_apify_caen_al_flujo_actual(apify_on, monkeypatch, caplog, status):
    FakeApify(status=status).install(monkeypatch)
    _meli_api(monkeypatch, [])
    _no_safe_get(monkeypatch)
    rendered = _no_browser(monkeypatch)

    with caplog.at_level(logging.DEBUG), pytest.raises(BlockedByCaptcha):
        await extract(UP_URL)

    assert f"HTTP {status}" in caplog.text
    # Apify ya se comió la espera: no se suma un render que tampoco puede.
    assert rendered == []
    # Ni el token ni el body (que acá trae el token a propósito).
    assert TOKEN not in caplog.text
    assert "SECRETO" not in caplog.text


@pytest.mark.asyncio
async def test_timeout_de_red_cae_al_flujo_actual(apify_on, monkeypatch, caplog):
    FakeApify(exc=httpx.ReadTimeout(f"timeout leyendo con {TOKEN}")).install(monkeypatch)
    _meli_api(monkeypatch, [])
    _no_safe_get(monkeypatch)
    _no_browser(monkeypatch)

    with caplog.at_level(logging.DEBUG), pytest.raises(BlockedByCaptcha):
        await extract(UP_URL)

    assert "ReadTimeout" in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_timeout_total_corta_aunque_apify_no_conteste(apify_on, monkeypatch, caplog):
    monkeypatch.setattr(apify_on, "apify_meli_timeout_s", 0, raising=False)
    monkeypatch.setattr(apify_meli, "_NETWORK_MARGIN_S", 0.05)
    FakeApify(delay_s=5).install(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="app.ingest.apify_meli"):
        result = await asyncio.wait_for(apify_meli.fetch(UP_URL), timeout=2)

    assert result is not None and not result.found
    assert "sin respuesta" in caplog.text


@pytest.mark.asyncio
async def test_respuesta_gigante_se_descarta(apify_on, monkeypatch):
    monkeypatch.setattr(apify_meli, "_MAX_RESPONSE_BYTES", 1000)
    item = dict(KHADINAKBAR_ITEM, description="x" * 5000)
    FakeApify(payload=[item]).install(monkeypatch)

    result = await apify_meli.fetch(UP_URL)

    assert result is not None and not result.found


@pytest.mark.asyncio
async def test_respuesta_no_json(apify_on, monkeypatch):
    FakeApify(raw=b"<html>oops</html>").install(monkeypatch)

    result = await apify_meli.fetch(UP_URL)

    assert result is not None and not result.found


@pytest.mark.asyncio
async def test_token_nunca_en_logs_ni_en_el_exito(apify_on, monkeypatch, caplog):
    FakeApify().install(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        result = await apify_meli.fetch(UP_URL)

    assert result.found
    assert TOKEN not in caplog.text


# ─── Qué va a Apify y qué vuelve ───────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://evil.example.com/MLA-123-x?u=mercadolibre.com.ar",
    "https://mercadolibre.com.ar@evil.example.com/up/MLAU1",
    "https://evilmercadolibre.com.ar/up/MLAU1234567",
    "https://click1.mercadolibre.com.ar/mclics/clicks/external/MLA/count?a=1",
    "javascript:alert(1)//mercadolibre.com.ar",
    "",
])
async def test_url_que_no_es_de_ml_nunca_va_a_apify(apify_on, monkeypatch, url):
    fake = FakeApify().install(monkeypatch)

    assert await apify_meli.fetch(url) is None
    assert fake.requests == []


@pytest.mark.asyncio
async def test_extract_de_otro_marketplace_no_toca_apify(apify_on, monkeypatch):
    fake = FakeApify().install(monkeypatch)
    url = "https://spanish.alibaba.com/product-detail/x_1.html"
    html = b'<html><head><meta property="og:image" content="https://s.alicdn.com/a.jpg"></head></html>'

    async def fake_get(u, **kwargs):  # noqa: ARG001
        return httpx.Response(200, headers={"content-type": "text/html"}, content=html,
                              request=httpx.Request("GET", u))

    monkeypatch.setattr(image_from_url, "safe_get", fake_get)

    out = await extract(url)

    assert out.image_urls == ["https://s.alicdn.com/a.jpg"]
    assert fake.requests == []


def test_http_de_ml_se_sube_a_https_y_sin_fragmento():
    assert apify_meli.allowed_url("http://www.mercadolibre.com.ar/p/MLA123#fotos") == (
        "https://www.mercadolibre.com.ar/p/MLA123")


@pytest.mark.asyncio
async def test_fotos_de_hosts_no_permitidos_se_descartan(apify_on, monkeypatch):
    item = dict(KHADINAKBAR_ITEM, thumbnail=None, pictures=[
        "https://evil.example.com/foto.jpg",
        "https://mlstatic.com.evil.example.com/foto.jpg",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://http2.mlstatic.com@evil.example.com/foto.jpg",
        {"url": "http://http2.mlstatic.com/D_NQ_NP_2X_111111-MLA1_012025-F.webp"},
        BIG,
    ])
    FakeApify(payload=[item]).install(monkeypatch)

    result = await apify_meli.fetch(UP_URL)

    assert result.image_urls == [
        "https://http2.mlstatic.com/D_NQ_NP_2X_111111-MLA1_012025-F.webp",
        BIG,
    ]


@pytest.mark.asyncio
async def test_solo_fotos_ajenas_es_como_no_encontrar(apify_on, monkeypatch):
    item = dict(KHADINAKBAR_ITEM, thumbnail=None, pictures=["https://evil.example.com/a.jpg"])
    FakeApify(payload=[item]).install(monkeypatch)
    _meli_api(monkeypatch, [])
    _no_safe_get(monkeypatch)
    _no_browser(monkeypatch)

    with pytest.raises(BlockedByCaptcha):
        await extract(UP_URL)


def test_miniatura_r_se_descarta_a_favor_de_la_f():
    # Venga primero la miniatura o la grande, queda la grande y en el lugar
    # donde apareció esa foto por primera vez.
    assert apify_meli.pick_pictures([THUMB, BIG, BIG_2, THUMB_2]) == [BIG, BIG_2]
    # Si solo hay miniatura, mejor eso que nada.
    assert apify_meli.pick_pictures([THUMB]) == [THUMB]


def test_titulo_saneado_en_una_linea():
    item = dict(KHADINAKBAR_ITEM, title="Masajeador\n\x00Cervical\u202e  {b}Zevya" + "x" * 400)
    out = apify_meli.normalize([item], UP_URL)
    assert "\n" not in out.title and "\x00" not in out.title and "\u202e" not in out.title
    assert out.title.startswith("Masajeador Cervical Zevya")
    assert len(out.title) <= 300


def test_permalink_ajeno_no_se_usa():
    item = dict(KHADINAKBAR_ITEM, url="https://evil.example.com/MLA1")
    assert apify_meli.normalize([item], UP_URL).permalink == UP_URL


def test_formato_memo23():
    item = {
        "productId": "MLA19807123",
        "title": "Licuadora Portátil",
        "price": 15999,
        "currency": "ARS",
        "primaryImage": BIG,
        "images": [BIG, THUMB_2, "https://evil.example.com/x.jpg"],
    }
    out = apify_meli.normalize([{"error": "otro item"}, item], UP_URL)
    assert out.image_urls == [BIG, THUMB_2]
    assert out.title == "Licuadora Portátil"
    assert (out.price_cents, out.currency) == (1599900, "ARS")


@pytest.mark.asyncio
async def test_input_de_memo23(apify_on, monkeypatch):
    monkeypatch.setattr(apify_on, "apify_meli_actor", "memo23/mercadolibre-scraper", raising=False)
    fake = FakeApify(payload=[{"title": "x", "images": [BIG]}]).install(monkeypatch)
    url = "https://www.mercadolibre.com.ar/licuadora/p/MLA19807123"

    result = await apify_meli.fetch(url)

    assert result.image_urls == [BIG]
    (req,) = fake.requests
    assert req.url.path == "/v2/acts/memo23~mercadolibre-scraper/run-sync-get-dataset-items"
    assert json.loads(req.content) == {"startUrls": [url], "country": "MLA",
                                       "enrichDetail": True, "maxReviews": 0, "maxItems": 1}


def test_precio_basura_se_ignora():
    for bad in (True, -5, 0, "1000", float("inf"), 10**12):
        item = dict(KHADINAKBAR_ITEM, price=bad)
        assert apify_meli.normalize([item], UP_URL).price_cents is None


# ─── Revisión de seguridad del PR #26 ──────────────────────────────


@pytest.mark.asyncio
async def test_json_anidado_no_revienta_el_lookup(apify_on, monkeypatch, caplog):
    """`json.loads` de cien mil corchetes lanza RecursionError: antes era un 500."""
    FakeApify(raw=b"[" * 100_000).install(monkeypatch)
    _meli_api(monkeypatch, [])
    _no_safe_get(monkeypatch)
    _no_browser(monkeypatch)

    with caplog.at_level(logging.DEBUG), pytest.raises(BlockedByCaptcha):
        await extract(UP_URL)

    assert TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_cualquier_sorpresa_termina_en_resultado_vacio(apify_on, monkeypatch, caplog):
    def roto():
        raise RuntimeError(f"la DB se cayó con {TOKEN}")

    monkeypatch.setattr(apify_meli, "_daily_budget", roto)
    fake = FakeApify().install(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        result = await apify_meli.fetch(UP_URL)

    assert result is not None and not result.found
    assert "RuntimeError" in caplog.text
    assert TOKEN not in caplog.text
    assert fake.requests == []


@pytest.mark.asyncio
async def test_memoria_del_actor_acotada(apify_on, monkeypatch):
    fake = FakeApify().install(monkeypatch)

    await apify_meli.fetch(UP_URL)

    assert fake.requests[0].url.params["memory"] == "1024"


@pytest.mark.asyncio
async def test_cache_por_link_no_gasta_cupo(apify_on, monkeypatch):
    fake = FakeApify().install(monkeypatch)

    first = await apify_meli.fetch(UP_URL)
    # Mismo producto con tracking distinto y fragmento: la clave son los ids.
    again = await apify_meli.fetch(UP_URL + "&matt_tool=123&utm_source=x#fotos")

    assert first.found and again.image_urls == first.image_urls
    assert len(fake.requests) == 1
    assert apify_meli.daily_budget.used_today(apify_meli.COUNTER_KEY) == 1


@pytest.mark.asyncio
async def test_otra_publicacion_de_la_misma_ficha_no_es_la_misma_clave(apify_on, monkeypatch):
    fake = FakeApify().install(monkeypatch)
    otra = UP_URL.replace("MLA1755665491", "MLA1755665492")

    await apify_meli.fetch(UP_URL)
    await apify_meli.fetch(otra)

    assert len(fake.requests) == 2


@pytest.mark.asyncio
async def test_link_que_no_sirvio_no_se_vuelve_a_mandar(apify_on, monkeypatch):
    fake = FakeApify(payload=[{"title": "sin fotos"}]).install(monkeypatch)

    first = await apify_meli.fetch(UP_URL)
    second = await apify_meli.fetch(UP_URL)

    assert first is not None and not first.found
    # None: no se mandó ni se esperó, así el browser conserva su chance.
    assert second is None
    assert len(fake.requests) == 1


@pytest.mark.asyncio
async def test_error_de_apify_se_cachea_poco(apify_on, monkeypatch):
    fake = FakeApify(status=402).install(monkeypatch)

    await apify_meli.fetch(UP_URL)
    assert await apify_meli.fetch(UP_URL) is None
    assert len(fake.requests) == 1

    # Vencido el TTL corto de error (p. ej. después de cargar saldo), se reintenta.
    monkeypatch.setattr(apify_meli, "_CACHE_ERROR_TTL_S", 0)
    apify_meli._reset_for_tests()
    await apify_meli.fetch(UP_URL)
    await apify_meli.fetch(UP_URL)
    assert len(fake.requests) == 3


def test_cache_acotada_lru(monkeypatch):
    apify_meli._reset_for_tests()
    monkeypatch.setattr(apify_meli, "_CACHE_MAX", 2)
    hit = apify_meli.ApifyMeliResult(image_urls=[BIG])
    apify_meli._cache_put("a", hit, 60)
    apify_meli._cache_put("b", hit, 60)
    assert apify_meli._cache_get("a") is hit       # "a" pasa a ser el más reciente
    apify_meli._cache_put("c", hit, 60)
    assert apify_meli._cache_get("b") is None      # se fue el menos usado
    assert apify_meli._cache_get("a") is hit and apify_meli._cache_get("c") is hit
    apify_meli._reset_for_tests()


@pytest.mark.asyncio
async def test_concurrencia_maxima_y_salteo_rapido(apify_on, monkeypatch):
    monkeypatch.setattr(apify_meli, "_SLOT_WAIT_S", 0.05)
    fake = FakeApify(delay_s=0.3).install(monkeypatch)

    results = await asyncio.gather(*(apify_meli.fetch(_up(n)) for n in range(4)))

    assert len(fake.requests) == 2
    assert sum(1 for r in results if r is None) == 2
    # El que se salteó no reservó cupo.
    assert apify_meli.daily_budget.used_today(apify_meli.COUNTER_KEY) == 2


@pytest.mark.asyncio
async def test_extract_sin_fuentes_pagas_no_toca_apify(apify_on, monkeypatch):
    fake = FakeApify().install(monkeypatch)
    _meli_api(monkeypatch, [])
    _no_safe_get(monkeypatch)
    _no_browser(monkeypatch)

    with pytest.raises(BlockedByCaptcha):
        await extract(UP_URL, allow_paid=False)

    assert fake.requests == []


@pytest.mark.asyncio
async def test_vision_compare_no_dispara_apify(monkeypatch):
    from fastapi import HTTPException

    from app.api import app_routes

    calls: list[dict] = []

    async def fake_extract(url, **kwargs):
        calls.append(kwargs)
        raise image_from_url.ExtractError("nada")

    monkeypatch.setattr(app_routes.image_from_url, "extract", fake_extract)

    with pytest.raises(HTTPException):
        await app_routes.run_vision_compare(app_routes.AppLookupRequest(url=UP_URL))

    assert calls == [{"allow_paid": False}]


def test_vision_compare_tiene_rate_limit():
    from app.api import app_routes
    from app.security import verify_rate_limit

    (route,) = [r for r in app_routes.router.routes if r.path.endswith("/vision-compare")]
    assert any(d.call is verify_rate_limit for d in route.dependant.dependencies)
