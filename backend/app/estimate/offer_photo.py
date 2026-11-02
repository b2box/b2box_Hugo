"""La foto de la oferta de 1688, limpia, para la mini card del chat.

Una sola foto (la de la oferta que el juez vio más parecida), sin texto chino,
sin marcas de agua, sin logos ni precios, sobre fondo blanco. La limpia un
modelo de imagen por el mismo endpoint OpenAI-compatible del juez (OpenRouter).

Corre en paralelo con medidas y precio: casi no suma tiempo. Vuelve como data
URL WebP chica (~30 KB): Hugo no guarda archivos, y así el navegador nunca ve
el link de alicdn (nada de proveedores). Si algo falla o no llega a tiempo,
None y el chat muestra la foto que mandó el cliente; la foto sin limpiar no
sale nunca.
"""
from __future__ import annotations

import base64
import io
import logging
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)

_SIDE_PX = 480
_WEBP_QUALITY = 78
_PROMPT = (
    "Edit this product photo for an online store card. Remove ALL text, Chinese characters, "
    "watermarks, logos, stickers, price tags, badges, borders and decorations. Put the product "
    "alone, centered, on a plain pure white background. Keep the product itself exactly as it is "
    "(shape, color, details). Do not add anything. Square image."
)


def available() -> bool:
    s = get_settings()
    return bool(s.pm_llm_base_url.strip() and s.pm_llm_api_key.strip()
                and s.estimate_photo_model.strip())


def to_card_data_url(raw: bytes) -> str | None:
    """Cuadrada, a lo sumo _SIDE_PX, WebP. None si no es una imagen."""
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    img.thumbnail((_SIDE_PX, _SIDE_PX))
    side = max(img.size)
    card = Image.new("RGB", (side, side), "white")
    card.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    out = io.BytesIO()
    card.save(out, "WEBP", quality=_WEBP_QUALITY)
    return "data:image/webp;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def _image_from(data: dict[str, Any]) -> bytes | None:
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return None
    for image in message.get("images") or []:
        url = ((image or {}).get("image_url") or {}).get("url") or ""
        if url.startswith("data:image/") and "," in url:
            try:
                return base64.b64decode(url.split(",", 1)[1])
            except ValueError:
                return None
    return None


async def clean(offer_image_url: str, *, client: Any | None = None) -> str | None:
    """Data URL de la foto limpia, o None. Nunca lanza."""
    import httpx

    if not offer_image_url or not available():
        return None
    s = get_settings()
    body = {
        "model": s.estimate_photo_model,
        "modalities": ["image", "text"],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": _PROMPT},
            {"type": "image_url", "image_url": {"url": offer_image_url}},
        ]}],
    }
    own = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        r = await client.post(s.pm_llm_base_url.rstrip("/") + "/chat/completions", json=body,
                              headers={"Authorization": f"Bearer {s.pm_llm_api_key.strip()}"})
        data = r.json() if r.status_code == 200 else None
    except Exception as exc:  # noqa: BLE001
        log.warning("estimado: limpiar la foto falló (%s)", type(exc).__name__)
        return None
    finally:
        if own:
            await client.aclose()
    if data is None:
        log.warning("estimado: limpiar la foto devolvió HTTP %s", r.status_code)
        return None
    raw = _image_from(data)
    return to_card_data_url(raw) if raw else None
