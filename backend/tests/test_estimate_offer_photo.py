"""La foto de la oferta, limpia por el modelo de imagen, como data URL chica."""
from __future__ import annotations

import base64
import io
from types import SimpleNamespace

from PIL import Image

from app.estimate import offer_photo


def _png(w: int, h: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), "red").save(out, "PNG")
    return out.getvalue()


def _settings(**kw):
    base = dict(pm_llm_base_url="https://openrouter.ai/api/v1", pm_llm_api_key="k",
                estimate_photo_model="google/gemini-2.5-flash-image")
    return SimpleNamespace(**{**base, **kw})


class _Client:
    def __init__(self, status: int, payload: dict):
        self.status, self.payload, self.sent = status, payload, None

    async def post(self, url, json, headers):
        self.sent = (url, json, headers)
        return SimpleNamespace(status_code=self.status, json=lambda: self.payload)


def _answer(raw: bytes) -> dict:
    url = "data:image/png;base64," + base64.b64encode(raw).decode()
    return {"choices": [{"message": {"images": [{"image_url": {"url": url}}]}}]}


def test_queda_cuadrada_chica_y_en_webp():
    data_url = offer_photo.to_card_data_url(_png(1600, 800))
    assert data_url and data_url.startswith("data:image/webp;base64,")
    img = Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1])))
    assert img.size == (480, 480)
    assert offer_photo.to_card_data_url(b"no es una imagen") is None


async def test_pide_la_edicion_con_la_foto_de_la_oferta(monkeypatch):
    monkeypatch.setattr(offer_photo, "get_settings", lambda: _settings())
    client = _Client(200, _answer(_png(300, 300)))
    out = await offer_photo.clean("https://cbu01.alicdn.com/x.jpg", client=client)
    assert out and out.startswith("data:image/webp;base64,")
    url, body, headers = client.sent
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert body["modalities"] == ["image", "text"]
    assert body["messages"][0]["content"][1]["image_url"]["url"] == "https://cbu01.alicdn.com/x.jpg"
    assert headers["Authorization"] == "Bearer k"


async def test_sin_imagen_en_la_respuesta_o_con_error_no_hay_foto(monkeypatch):
    monkeypatch.setattr(offer_photo, "get_settings", lambda: _settings())
    assert await offer_photo.clean("u", client=_Client(200, {"choices": [{"message": {}}]})) is None
    assert await offer_photo.clean("u", client=_Client(429, {})) is None
    monkeypatch.setattr(offer_photo, "get_settings", lambda: _settings(pm_llm_api_key=""))
    assert await offer_photo.clean("u", client=_Client(200, _answer(_png(10, 10)))) is None
