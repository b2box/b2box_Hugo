"""POST /app/lookup — el b2box app manda una URL.

Los cuatro caminos que importan:
  1. lo tenemos           → PA (SKU de variante) + link "comprar ahora"
  2. no lo tenemos        → se abre el pedido en Cloud_B2BOX
  3. índice construyéndose→ NO se abre pedido (diría "no lo tenemos" de más)
  4. la URL no da foto    → no_image, sin pedido
"""

import numpy as np
import pytest

from app.api import app_routes
from app.api.app_routes import AppLookupClient, AppLookupRequest, app_lookup
from app.ingest.image_from_url import ExtractedProduct
from app.vendure.client import VendureProduct

PRODUCT_URL = "https://articulo.mercadolibre.com.ar/MLA-123-lampara-led"
PHOTO = "https://http2.mlstatic.com/D_NQ_NP_2X_lampara.jpg"

# El formulario de Cloud (form-app-submit) exige nombre + email + teléfono.
CLIENTE = AppLookupClient(
    name="Juan Pérez", email="juan@ejemplo.com", phone="+54 11 5555-1234",
    country="Argentina", quantity="200 u",
)


def _product(pid="42", source_url="https://detail.1688.com/offer/987.html") -> VendureProduct:
    return VendureProduct(
        id=pid,
        name="Lámpara LED táctil",
        slug="lampara-led-tactil",
        description="Lámpara de pared recargable",
        enabled=True,
        source_url=source_url,
        image_urls=["https://cdn.b2box.app/lampara-1.jpg"],
        product_code="BX-1001",
        featured_image_url="https://cdn.b2box.app/lampara-1.jpg",
        first_variant_price_cents=189900,
        variant_count=2,
    )


_FULL = {
    "id": "42",
    "name": "Lámpara LED táctil",
    "slug": "lampara-led-tactil",
    "description": "Lámpara de pared recargable",
    "enabled": True,
    "source_url": "https://detail.1688.com/offer/987.html",
    "product_code": "BX-1001",
    "featured_image_url": "https://cdn.b2box.app/lampara-1.jpg",
    "image_urls": ["https://cdn.b2box.app/lampara-1.jpg"],
    "first_variant_price_cents": 189900,
    "variant_count": 2,
    "variants": [
        {"id": "101", "name": "Blanco", "sku": "PA-1001-BL",
         "price_cents": 189900, "currency": "ARS", "stock": "IN_STOCK"},
        {"id": "102", "name": "Negro", "sku": "PA-1001-NE",
         "price_cents": 199900, "currency": "ARS", "stock": "IN_STOCK"},
    ],
}


@pytest.fixture
def env(monkeypatch):
    """Aísla el endpoint: sin red, sin DB, sin Vendure."""
    recorded: list[dict] = []
    cloud_calls: list[dict] = []

    async def fake_extract(url):  # noqa: ARG001
        return ExtractedProduct(
            image_urls=[PHOTO],
            title="Lampara LED tactil recargable",
            marketplace="mercadolibre",
            canonical_url=url,
            kind="page",
        )

    async def fake_catalog(force=False):  # noqa: ARG001
        return [_product()]

    async def fake_submit(payload):
        cloud_calls.append(payload)
        return app_routes.cloud_integration.CloudRequestResult(
            request_id="req-77", status="queued", raw={},
        )

    class FakeVendureClient:
        async def get_product_full(self, product_id):  # noqa: ARG002
            return _FULL

    class FakeSettings:
        storefront_url = "https://b2box.app"
        storefront_product_path = "/product/{slug}"

    monkeypatch.setattr(app_routes.image_from_url, "extract", fake_extract)
    monkeypatch.setattr(app_routes.vendure_catalog, "get_catalog", fake_catalog)
    monkeypatch.setattr(app_routes, "VendureClient", FakeVendureClient)
    monkeypatch.setattr(app_routes, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(app_routes.cloud_integration, "enabled", lambda: True)
    monkeypatch.setattr(app_routes.cloud_integration, "submit_request", fake_submit)
    monkeypatch.setattr(
        app_routes, "_record", lambda **kw: recorded.append(kw)
    )
    monkeypatch.setattr(
        app_routes.runtime, "get",
        lambda k: {"embed_match_threshold": 0.88, "embed_suggest_threshold": 0.78,
                   "dedup_image_threshold": 0.92}[k],
    )
    return {"recorded": recorded, "cloud_calls": cloud_calls}


def _use_embeddings(monkeypatch, *, score: float, ready: bool = True):
    """Simula el índice CLIP devolviendo un match con el score pedido."""
    monkeypatch.setattr(app_routes.image_embed, "available", lambda: True)

    async def fake_embed_url(url):  # noqa: ARG001
        return np.ones(4, dtype=np.float32)

    async def fake_ensure_index():
        return None

    monkeypatch.setattr(app_routes.image_embed, "embed_url", fake_embed_url)
    monkeypatch.setattr(app_routes.catalog_index, "ensure_index", fake_ensure_index)
    monkeypatch.setattr(app_routes.catalog_index, "is_ready", lambda: ready)
    monkeypatch.setattr(
        app_routes.catalog_index, "status",
        lambda: {"progress_done": 120, "progress_total": 1500},
    )
    monkeypatch.setattr(
        app_routes.catalog_index, "search",
        lambda q, top_k=1: [(_product(), score, "https://cdn.b2box.app/lampara-1.jpg")],
    )


# ─── 1. Lo tenemos ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_match_devuelve_pa_y_link_de_compra(env, monkeypatch):
    _use_embeddings(monkeypatch, score=0.94)

    resp = await app_lookup(AppLookupRequest(url=PRODUCT_URL))

    assert resp.status == "found"
    assert resp.found is True
    assert resp.matched_by == ["image_embed"]
    assert resp.confidence == pytest.approx(0.94)
    # PA = código de la variante.
    assert resp.product.pa == "PA-1001-BL"
    assert [v.pa for v in resp.product.variants] == ["PA-1001-BL", "PA-1001-NE"]
    assert resp.product.product_code == "BX-1001"
    assert resp.product.buy_now_url == "https://b2box.app/product/lampara-led-tactil"
    assert resp.product.price_cents == 189900
    # Si lo tenemos, NO se molesta a Cloud.
    assert env["cloud_calls"] == []
    assert env["recorded"][0]["action"] == "app_lookup_match"


@pytest.mark.asyncio
async def test_match_exacto_por_source_url_sin_bajar_imagenes(env, monkeypatch):
    """Si la URL del proveedor ya está en el catálogo, no hace falta comparar fotos."""
    called = {"embed": False}

    async def fake_embed_url(url):  # noqa: ARG001
        called["embed"] = True
        return np.ones(4, dtype=np.float32)

    monkeypatch.setattr(app_routes.image_embed, "available", lambda: True)
    monkeypatch.setattr(app_routes.image_embed, "embed_url", fake_embed_url)

    resp = await app_lookup(AppLookupRequest(url="https://detail.1688.com/offer/987.html"))

    assert resp.status == "found"
    assert resp.matched_by == ["url"]
    assert resp.confidence == pytest.approx(1.0)
    assert called["embed"] is False


# ─── 2. No lo tenemos ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sin_match_abre_pedido_en_cloud(env, monkeypatch):
    _use_embeddings(monkeypatch, score=0.41)

    resp = await app_lookup(
        AppLookupRequest(url=PRODUCT_URL, note="lo quiero en negro", client=CLIENTE)
    )

    assert resp.status == "not_found"
    assert resp.found is False
    assert resp.product is None
    assert resp.cloud_request.sent is True
    assert resp.cloud_request.request_id == "req-77"

    # Body exacto de form-app-submit (ver b2b-flow-pro/supabase/functions/).
    payload = env["cloud_calls"][0]
    assert payload["client_name"] == "Juan Pérez"
    assert payload["email"] == "juan@ejemplo.com"
    assert payload["country"] == "Argentina"
    product = payload["products"][0]
    assert product["reference_link"] == PRODUCT_URL
    assert product["image_urls"] == [PHOTO]
    assert product["quantity"] == "200 u"
    assert product["description"] == "lo quiero en negro"
    assert "NO está en el catálogo" in product["notes"]
    assert "mercadolibre" in product["notes"]
    assert env["recorded"][0]["action"] == "app_lookup_request_sent"


@pytest.mark.asyncio
async def test_casi_match_viaja_como_sugerencia(env, monkeypatch):
    """Score en la banda de sugerencia: no se muestra como encontrado, pero el
    que revisa el pedido en Cloud ve el candidato."""
    _use_embeddings(monkeypatch, score=0.82)

    resp = await app_lookup(AppLookupRequest(url=PRODUCT_URL, client=CLIENTE))

    assert resp.status == "not_found"
    assert resp.suggestion is not None
    assert resp.suggestion.product_id == "42"
    assert resp.suggestion.score == pytest.approx(0.82)
    # El candidato viaja en las notas del producto: form-app-submit no tiene un
    # campo estructurado para esto, y quien revisa la consulta lo lee ahí.
    notes = env["cloud_calls"][0]["products"][0]["notes"]
    assert "Mejor candidato" in notes
    assert "BX-1001" in notes
    assert "82%" in notes


@pytest.mark.asyncio
async def test_cloud_caido_no_rompe_la_respuesta(env, monkeypatch):
    _use_embeddings(monkeypatch, score=0.10)

    async def failing_submit(payload):  # noqa: ARG001
        raise app_routes.cloud_integration.CloudError("HTTP 503: upstream down")

    monkeypatch.setattr(app_routes.cloud_integration, "submit_request", failing_submit)

    resp = await app_lookup(AppLookupRequest(url=PRODUCT_URL, client=CLIENTE))

    assert resp.status == "not_found"
    assert resp.cloud_request.sent is False
    assert "503" in resp.cloud_request.error
    assert env["recorded"][0]["action"] == "app_lookup_request_failed"


@pytest.mark.asyncio
async def test_sin_datos_del_cliente_no_llama_a_cloud(env, monkeypatch):
    """El form exige nombre+email+teléfono. Sin ellos sería un 400 seguro, y
    encima gastaría una de las 5 submissions por ventana de rate limit."""
    _use_embeddings(monkeypatch, score=0.10)

    resp = await app_lookup(AppLookupRequest(url=PRODUCT_URL))

    assert resp.status == "not_found"
    assert resp.cloud_request.sent is False
    assert resp.cloud_request.missing_fields == ["client.name", "client.email", "client.phone"]
    assert env["cloud_calls"] == []
    assert env["recorded"][0]["action"] == "app_lookup_request_failed"


@pytest.mark.asyncio
async def test_email_invalido_se_frena_antes_de_llamar(env, monkeypatch):
    _use_embeddings(monkeypatch, score=0.10)
    cliente = AppLookupClient(name="Ana", email="ana-arroba-nada", phone="+5491155551234")

    resp = await app_lookup(AppLookupRequest(url=PRODUCT_URL, client=cliente))

    assert resp.cloud_request.missing_fields == ["client.email"]
    assert env["cloud_calls"] == []


@pytest.mark.asyncio
async def test_sin_cloud_configurado_no_falla(env, monkeypatch):
    _use_embeddings(monkeypatch, score=0.10)
    monkeypatch.setattr(app_routes.cloud_integration, "enabled", lambda: False)

    resp = await app_lookup(AppLookupRequest(url=PRODUCT_URL, client=CLIENTE))

    assert resp.status == "not_found"
    assert resp.cloud_request.sent is False
    assert "CLOUD_URL" in resp.cloud_request.error
    assert env["cloud_calls"] == []


# ─── 3. Índice a medio construir ───────────────────────────────────


@pytest.mark.asyncio
async def test_indice_construyendose_no_abre_pedido(env, monkeypatch):
    _use_embeddings(monkeypatch, score=0.99, ready=False)

    resp = await app_lookup(AppLookupRequest(url=PRODUCT_URL))

    assert resp.status == "indexing"
    assert resp.found is False
    assert "120/1500" in resp.detail
    # Clave: un índice a medias diría "no lo tenemos" sobre cosas que sí tenemos.
    assert env["cloud_calls"] == []


# ─── 4. Sin foto ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_url_sin_foto(env, monkeypatch):
    async def failing_extract(url):  # noqa: ARG001
        raise app_routes.image_from_url.ExtractError("la ficha se arma por JS")

    monkeypatch.setattr(app_routes.image_from_url, "extract", failing_extract)

    resp = await app_lookup(AppLookupRequest(url=PRODUCT_URL))

    assert resp.status == "no_image"
    assert resp.found is False
    assert env["cloud_calls"] == []
    assert env["recorded"][0]["action"] == "app_lookup_no_image"


@pytest.mark.asyncio
async def test_foto_directa_del_cliente_sirve_aunque_falle_el_scraping(env, monkeypatch):
    """El app ya subió la foto: aunque la URL no se pueda scrapear, seguimos."""
    async def failing_extract(url):  # noqa: ARG001
        raise app_routes.image_from_url.ExtractError("403")

    monkeypatch.setattr(app_routes.image_from_url, "extract", failing_extract)
    _use_embeddings(monkeypatch, score=0.95)

    resp = await app_lookup(
        AppLookupRequest(url=PRODUCT_URL, image_url="https://cdn.b2box.app/foto-cliente.jpg")
    )

    assert resp.status == "found"
    assert resp.image_url == "https://cdn.b2box.app/foto-cliente.jpg"


@pytest.mark.asyncio
async def test_sin_url_ni_imagen_es_422(env):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await app_lookup(AppLookupRequest())
    assert exc.value.status_code == 422


# ─── Fallback pHash (sin CLIP) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_fallback_phash_cuando_no_hay_modelo(env, monkeypatch):
    monkeypatch.setattr(app_routes.image_embed, "available", lambda: False)

    async def fake_image_similarity(a, b):  # noqa: ARG001
        return 0.97

    monkeypatch.setattr(app_routes, "image_similarity", fake_image_similarity)

    resp = await app_lookup(AppLookupRequest(url=PRODUCT_URL))

    assert resp.status == "found"
    assert resp.matched_by == ["image_phash"]
    assert resp.product.pa == "PA-1001-BL"
