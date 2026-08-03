"""Cliente para Cloud_B2BOX (b2b-flow-pro) — formulario de consulta del app.

Cuando el b2box app manda una URL y Hugo NO encuentra el producto en el
catálogo, no hay nada que comprar todavía. En vez de dejar al cliente sin
respuesta, Hugo abre **el mismo formulario que ya existe en el app**: la edge
function `form-app-submit`, que escribe en `form_app_consultations` +
`form_app_consultation_products`. Desde ahí el equipo comercial decide si
salimos a buscar el producto (es el mismo tablero de la sección Forms).

Contrato real (supabase/functions/form-app-submit/index.ts en b2b-flow-pro):

    POST {cloud_url}/functions/v1/form-app-submit      # verify_jwt = false

    Body: {
      "client_name": "...",          // obligatorio
      "email":       "...",          // obligatorio, se valida el formato
      "phone":       "...",          // obligatorio
      "country":     "..." | null,
      "products": [{                 // obligatorio, al menos 1 (máx 30)
        "name":           "...",     // máx 200
        "quantity":       "...",     // máx 50, texto libre
        "description":    "...",     // máx 5000
        "reference_link": "...",     // máx 500  → la URL que pegó el cliente
        "notes":          "...",     // máx 2000 → contexto que agrega Hugo
        "image_urls":     ["..."]    // máx 10, deben empezar con http
      }]
    }

    Response 200: {"ok": true, "consultation_id": "<uuid>"}

Dos cosas del lado de Cloud que condicionan este cliente:

  * **Rate limit**: `checkRateLimit` permite 5 submissions cada 10 minutos POR IP.
    Está pensado para un browser, pero Hugo es un solo servidor: todos los
    lookups salen de la misma IP y a partir del 6º en 10 minutos Cloud responde
    429. Para volumen real hace falta un bypass server-to-server en la edge
    function (ver README).

  * **reCAPTCHA**: `verifyRecaptcha` corre en modo `monitor` por defecto, así que
    una llamada sin token pasa. Si alguien pone `RECAPTCHA_MODE=enforce`, Hugo
    empieza a comer 403 — no tiene forma de generar un token v3.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

# Topes de la edge function. Recortamos acá para que un texto largo no haga
# fallar el insert del lado de Cloud.
_MAX_NAME = 200
_MAX_QUANTITY = 50
_MAX_DESCRIPTION = 5000
_MAX_REFERENCE_LINK = 500
_MAX_NOTES = 2000
_MAX_IMAGES = 10

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class CloudError(RuntimeError):
    pass


@dataclass(slots=True)
class CloudRequestResult:
    request_id: str | None
    status: str
    raw: dict[str, Any]


def enabled() -> bool:
    return bool(get_settings().cloud_url)


def _headers() -> dict[str, str]:
    s = get_settings()
    h: dict[str, str] = {"Content-Type": "application/json"}
    # form-app-submit tiene verify_jwt = false, así que la anon key no es
    # obligatoria. La mandamos igual si está: el gateway de Supabase la espera
    # en algunos setups y no molesta en ninguno.
    if s.cloud_anon_key:
        h["apikey"] = s.cloud_anon_key
        h["Authorization"] = f"Bearer {s.cloud_anon_key}"
    if s.cloud_api_key:
        h["X-API-Key"] = s.cloud_api_key
    if s.cloud_bearer:
        h["Authorization"] = f"Bearer {s.cloud_bearer}"
    return h


def _clip(value: str | None, limit: int) -> str:
    return (value or "").strip()[:limit]


def missing_client_fields(client: Any) -> list[str]:
    """Campos obligatorios del formulario que faltan.

    Los chequeamos ANTES de llamar: la edge function responde 400 sin ellos, y
    un 400 evitable gasta una de las 5 submissions de la ventana de rate limit.
    """
    missing: list[str] = []
    if not _clip(getattr(client, "name", ""), _MAX_NAME):
        missing.append("client.name")
    email = _clip(getattr(client, "email", ""), 320)
    if not email or not _EMAIL_RE.match(email):
        missing.append("client.email")
    if not _clip(getattr(client, "phone", ""), 50):
        missing.append("client.phone")
    return missing


def _hugo_notes(
    *, marketplace: str, score: float, best_match: dict[str, Any] | None, source: str
) -> str:
    """Contexto que agrega Hugo para quien revisa el pedido en el tablero."""
    lines = [
        f"Generado por Hugo desde {source} — el cliente mandó un link y el "
        "producto NO está en el catálogo de Vendure.",
        f"Origen del link: {marketplace}.",
    ]
    if best_match:
        lines.append(
            f"Mejor candidato del catálogo: {best_match.get('name') or '(sin nombre)'} "
            f"(id {best_match.get('product_id')}, código {best_match.get('product_code') or '—'}) "
            f"con {float(best_match.get('score') or 0):.0%} de parecido visual. "
            "No alcanzó el umbral para darlo por encontrado — conviene mirarlo antes "
            "de salir a buscar el producto."
        )
    elif score:
        lines.append(f"Nada parecido en el catálogo (mejor parecido visual: {score:.0%}).")
    return "\n".join(lines)


def build_payload(
    *,
    client: Any,
    input_url: str,
    image_urls: list[str],
    title: str = "",
    marketplace: str = "other",
    note: str | None = None,
    score: float = 0.0,
    best_match: dict[str, Any] | None = None,
    source: str = "b2box-app",
) -> dict[str, Any]:
    """Arma el body de form-app-submit. Separado para poder testearlo sin red."""
    product_name = _clip(title, _MAX_NAME) or f"Producto de {marketplace}"
    return {
        "client_name": _clip(getattr(client, "name", ""), _MAX_NAME),
        "email": _clip(getattr(client, "email", ""), 320),
        "phone": _clip(getattr(client, "phone", ""), 50),
        "country": _clip(getattr(client, "country", None), 100) or None,
        "products": [
            {
                "name": product_name,
                "quantity": _clip(getattr(client, "quantity", ""), _MAX_QUANTITY),
                "description": _clip(note, _MAX_DESCRIPTION) or None,
                "reference_link": _clip(input_url, _MAX_REFERENCE_LINK) or None,
                "notes": _hugo_notes(
                    marketplace=marketplace, score=score,
                    best_match=best_match, source=source,
                )[:_MAX_NOTES],
                # La edge function descarta lo que no empiece con http.
                "image_urls": [u for u in image_urls if u.startswith("http")][:_MAX_IMAGES],
            }
        ],
    }


def _extract_id(data: dict[str, Any]) -> str | None:
    for key in ("consultation_id", "request_id", "id"):
        value = data.get(key)
        if value:
            return str(value)
    nested = data.get("data")
    if isinstance(nested, dict):
        return _extract_id(nested)
    return None


async def submit_request(payload: dict[str, Any]) -> CloudRequestResult:
    """Manda el formulario a Cloud. Lanza CloudError si no se pudo."""
    s = get_settings()
    if not s.cloud_url:
        raise CloudError(
            "CLOUD_URL no configurado — el pedido de producto no tiene a dónde ir. "
            "Seteá CLOUD_URL a la URL de Supabase de Cloud_B2BOX "
            "(https://<ref>.supabase.co)."
        )
    url = f"{s.cloud_url.rstrip('/')}{s.cloud_request_path}"

    try:
        async with httpx.AsyncClient(timeout=s.cloud_timeout_seconds) as client:
            resp = await client.post(url, json=payload, headers=_headers())
    except httpx.HTTPError as exc:
        raise CloudError(f"No se pudo llegar a Cloud: {type(exc).__name__}: {exc}") from exc

    if resp.status_code == 429:
        # 5 submissions por IP cada 10 min, y Hugo es una sola IP.
        raise CloudError(
            "Cloud devolvió 429 (rate limit de form-app-submit: 5 por IP cada 10 min). "
            "Hugo sale siempre desde la misma IP: hace falta un bypass "
            "server-to-server en la edge function."
        )
    if resp.status_code == 403:
        raise CloudError(
            "Cloud devolvió 403 — probablemente RECAPTCHA_MODE=enforce. "
            "Hugo no puede generar un token de reCAPTCHA v3; hace falta exceptuar "
            f"las llamadas server-to-server. Respuesta: {resp.text[:150]}"
        )
    if resp.status_code >= 400:
        log.error("Cloud %s: %s %s", url, resp.status_code, resp.text[:300])
        raise CloudError(f"HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        data = resp.json()
    except ValueError:
        # Un 2xx sin JSON igual cuenta como aceptado: no perdemos el pedido.
        return CloudRequestResult(request_id=None, status="accepted", raw={})

    if not isinstance(data, dict):
        return CloudRequestResult(request_id=None, status="accepted", raw={"response": data})

    # La edge function contesta 200 con {"error": ...} en algunos caminos.
    if data.get("error"):
        raise CloudError(str(data["error"])[:200])

    return CloudRequestResult(
        request_id=_extract_id(data),
        status="accepted" if data.get("ok") else str(data.get("status", "unknown")),
        raw=data,
    )
