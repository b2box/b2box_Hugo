"""Cliente de Cloud_B2BOX — formulario del app (edge function form-app-submit).

El contrato lo fija b2b-flow-pro/supabase/functions/form-app-submit/index.ts.
Estos tests lo congelan: si Cloud cambia campos o topes, acá se rompe.
"""

import httpx
import pytest

from app.api.app_routes import AppLookupClient
from app.integrations import cloud


def _client(**over) -> AppLookupClient:
    base = {"name": "Juan Pérez", "email": "juan@ejemplo.com",
            "phone": "+54 11 5555-1234", "country": "Argentina", "quantity": "200 u"}
    base.update(over)
    return AppLookupClient(**base)


def _payload(**over):
    kwargs = {
        "client": _client(),
        "input_url": "https://articulo.mercadolibre.com.ar/MLA-123",
        "image_urls": ["https://http2.mlstatic.com/foto.jpg"],
        "title": "Lámpara LED táctil",
        "marketplace": "mercadolibre",
    }
    kwargs.update(over)
    return cloud.build_payload(**kwargs)


# ─── Forma del body ────────────────────────────────────────────────


def test_payload_tiene_la_forma_que_espera_form_app_submit():
    body = _payload(note="lo quiero en negro")

    assert set(body) == {"client_name", "email", "phone", "country", "products"}
    assert body["client_name"] == "Juan Pérez"
    assert body["email"] == "juan@ejemplo.com"
    assert body["phone"] == "+54 11 5555-1234"

    assert len(body["products"]) == 1
    p = body["products"][0]
    assert p["name"] == "Lámpara LED táctil"
    assert p["quantity"] == "200 u"
    assert p["description"] == "lo quiero en negro"
    assert p["reference_link"] == "https://articulo.mercadolibre.com.ar/MLA-123"
    assert p["image_urls"] == ["https://http2.mlstatic.com/foto.jpg"]


def test_sin_titulo_usa_el_marketplace_como_nombre():
    # `name` es NOT NULL del lado de Cloud: nunca puede salir vacío.
    p = _payload(title="")["products"][0]
    assert p["name"] == "Producto de mercadolibre"


def test_pais_vacio_viaja_como_null():
    assert _payload(client=_client(country=""))["country"] is None


def test_urls_no_http_se_descartan():
    # form-app-submit filtra lo que no empiece con http; lo hacemos acá también
    # para no mandar basura.
    p = _payload(image_urls=["data:image/png;base64,AAA", "https://ok.com/a.jpg"])["products"][0]
    assert p["image_urls"] == ["https://ok.com/a.jpg"]


def test_se_respetan_los_topes_de_la_edge_function():
    p = _payload(
        title="L" * 500,
        note="D" * 9000,
        input_url="https://x.com/" + "y" * 900,
        image_urls=[f"https://cdn.com/{i}.jpg" for i in range(25)],
        client=_client(quantity="Q" * 120),
    )["products"][0]

    assert len(p["name"]) == 200
    assert len(p["description"]) == 5000
    assert len(p["reference_link"]) == 500
    assert len(p["quantity"]) == 50
    assert len(p["image_urls"]) == 10
    assert len(p["notes"]) <= 2000


def test_notas_llevan_el_contexto_de_hugo():
    notes = _payload(
        score=0.82,
        best_match={"product_id": "42", "name": "Lámpara LED",
                    "product_code": "BX-1001", "score": 0.82},
    )["products"][0]["notes"]

    assert "Hugo" in notes
    assert "Mejor candidato" in notes
    assert "BX-1001" in notes
    assert "82%" in notes


# ─── Validación previa de campos obligatorios ──────────────────────


@pytest.mark.parametrize(
    "over,expected",
    [
        ({"name": ""}, ["client.name"]),
        ({"email": ""}, ["client.email"]),
        ({"email": "sin-arroba"}, ["client.email"]),
        ({"phone": ""}, ["client.phone"]),
        ({}, []),
    ],
)
def test_missing_client_fields(over, expected):
    assert cloud.missing_client_fields(_client(**over)) == expected


# ─── Respuestas de la edge function ────────────────────────────────


def _patch_post(monkeypatch, response: httpx.Response):
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):  # noqa: A002, ARG002
            return response

    monkeypatch.setattr(cloud.httpx, "AsyncClient", lambda **kw: FakeClient())

    class FakeSettings:
        cloud_url = "https://ref.supabase.co"
        cloud_request_path = "/functions/v1/form-app-submit"
        cloud_anon_key = "anon-key"
        cloud_api_key = ""
        cloud_bearer = ""
        cloud_timeout_seconds = 30.0

    monkeypatch.setattr(cloud, "get_settings", lambda: FakeSettings())


def _resp(status: int, body) -> httpx.Response:
    return httpx.Response(
        status_code=status, json=body,
        request=httpx.Request("POST", "https://ref.supabase.co/functions/v1/form-app-submit"),
    )


@pytest.mark.asyncio
async def test_respuesta_ok_devuelve_consultation_id(monkeypatch):
    _patch_post(monkeypatch, _resp(200, {"ok": True, "consultation_id": "uuid-123"}))

    result = await cloud.submit_request(_payload())

    assert result.request_id == "uuid-123"
    assert result.status == "accepted"


@pytest.mark.asyncio
async def test_429_explica_el_rate_limit_por_ip(monkeypatch):
    _patch_post(monkeypatch, _resp(429, {"error": "Too many submissions. Try again later."}))

    with pytest.raises(cloud.CloudError) as exc:
        await cloud.submit_request(_payload())
    assert "429" in str(exc.value)
    assert "misma IP" in str(exc.value)


@pytest.mark.asyncio
async def test_403_apunta_a_recaptcha_enforce(monkeypatch):
    _patch_post(monkeypatch, _resp(403, {"error": "Verificación de seguridad fallida."}))

    with pytest.raises(cloud.CloudError) as exc:
        await cloud.submit_request(_payload())
    assert "RECAPTCHA_MODE=enforce" in str(exc.value)


@pytest.mark.asyncio
async def test_200_con_error_en_el_body_es_error(monkeypatch):
    _patch_post(monkeypatch, _resp(200, {"error": "Missing required fields"}))

    with pytest.raises(cloud.CloudError) as exc:
        await cloud.submit_request(_payload())
    assert "Missing required fields" in str(exc.value)


@pytest.mark.asyncio
async def test_sin_cloud_url_falla_con_mensaje_util(monkeypatch):
    class FakeSettings:
        cloud_url = ""

    monkeypatch.setattr(cloud, "get_settings", lambda: FakeSettings())

    with pytest.raises(cloud.CloudError) as exc:
        await cloud.submit_request(_payload())
    assert "CLOUD_URL" in str(exc.value)


def test_headers_mandan_la_anon_key(monkeypatch):
    class FakeSettings:
        cloud_anon_key = "anon-key"
        cloud_api_key = ""
        cloud_bearer = ""

    monkeypatch.setattr(cloud, "get_settings", lambda: FakeSettings())
    headers = cloud._headers()
    assert headers["apikey"] == "anon-key"
    assert headers["Authorization"] == "Bearer anon-key"
