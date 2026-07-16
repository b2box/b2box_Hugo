"""Cliente para Paco (paco.b2box.app) — búsqueda de similares por imagen.

Cuando Hugo decide que un candidato NO es duplicado en Vendure, le pasa la
imagen a Paco para que arme el job de similarity search en 1688.

Contrato (espejo del cliente que tiene Luis):
  POST {paco_url}{paco_submit_path}
  Headers:  X-API-Key: ...
            CF-Access-Client-Id / Secret (opcional)
            Content-Type: application/json
  Body:    {"image_url": "...", "source": "hugo"}
  Response: {"search_id" | "id": "...", "status": "queued"}
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class PacoError(RuntimeError):
    pass


@dataclass(slots=True)
class PacoSubmitResult:
    search_id: str
    status: str
    raw: dict[str, Any]


def _headers() -> dict[str, str]:
    s = get_settings()
    h: dict[str, str] = {"Content-Type": "application/json"}
    # Middleware de Paco APP/PRO (PACO_SUPABASE_AUTH): auth máquina vía Bearer estático.
    if s.paco_admin_token:
        h["Authorization"] = f"Bearer {s.paco_admin_token}"
    # Gate del endpoint (PACO_INGEST_API_KEY, solo image_url).
    if s.paco_api_key:
        h["X-API-Key"] = s.paco_api_key
    if s.paco_cf_client_id and s.paco_cf_client_secret:
        h["CF-Access-Client-Id"] = s.paco_cf_client_id
        h["CF-Access-Client-Secret"] = s.paco_cf_client_secret
    return h


async def submit(image_url: str, product_url: str | None = None) -> PacoSubmitResult:
    """Envía image_url a Paco y devuelve el search_id.

    `product_url` es el link de origen (1688) cuando lo conocemos: los productos
    de Luis vienen de OTAPI, así que el item ya está identificado y Paco puede
    resolverlo por ID en vez de re-buscarlo por imagen contra todo 1688. Paco
    parsea la URL él mismo (detail/m.1688.com/offer/NNN, ?offerId=, ?num_iid=).
    Es opcional y aditivo: si Paco no lo soporta, ignora el campo y busca por
    imagen como siempre.
    """
    if not image_url:
        raise PacoError("image_url vacío")
    s = get_settings()
    if not s.paco_url:
        raise PacoError("PACO_URL no configurado")

    url = f"{s.paco_url.rstrip('/')}{s.paco_submit_path}"
    payload = {"image_url": image_url, "source": "hugo"}
    if product_url:
        payload["product_url"] = product_url

    # Paco puede tardar 1-2 min en responder el submit (procesa + devuelve id).
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(url, json=payload, headers=_headers())

    if resp.status_code >= 400:
        log.error("Paco %s: %s %s", url, resp.status_code, resp.text[:300])
        raise PacoError(f"HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        data = resp.json()
    except ValueError:
        raise PacoError(f"respuesta no-JSON: {resp.text[:200]}")

    search_id = data.get("search_id") or data.get("id")
    if not search_id:
        raise PacoError(f"respuesta sin 'search_id' ni 'id': {data}")

    return PacoSubmitResult(
        search_id=str(search_id),
        status=str(data.get("status", "unknown")),
        raw=data,
    )


async def submit_pro(
    image_url: str,
    callback_ctx: dict[str, Any] | None = None,
    text_specs: str = "",
) -> PacoSubmitResult:
    """Envía a Paco PRO (b2box_sourcing) POST /api/tech/start como multipart form.

    A diferencia de submit() (Paco APP, JSON), acá mandamos:
      - image_url + text_specs (specs libres del producto)
      - callback_ctx (JSON) → Paco escribe de vuelta al quotation_item vía paco-ingest

    Destino = paco_pro_url + paco_pro_submit_path. NO cae a paco_url (Paco APP):
    un job PRO (Requests) en la Paco equivocada es peor que un error visible.
    """
    if not image_url:
        raise PacoError("image_url vacío")
    s = get_settings()
    base = (s.paco_pro_url or "").rstrip("/")
    if not base:
        raise PacoError(
            "PACO_PRO_URL no configurado — un job PRO (Requests) NO debe caer a Paco APP. "
            "Seteá PACO_PRO_URL a la URL de b2box_sourcing (ej. https://paco-pro.b2box.pro)."
        )

    url = f"{base}{s.paco_pro_submit_path}"
    form: dict[str, str] = {"text_specs": text_specs or "", "source": "hugo-pro"}
    if image_url:
        form["image_url"] = image_url
    if callback_ctx:
        form["callback_ctx"] = json.dumps(callback_ctx)

    # b2box_sourcing autentica la máquina por X-API-Key (== su PACO_API_KEY);
    # NO usa Bearer. Headers propios, distintos a los de Paco APP.
    headers: dict[str, str] = {}
    if s.paco_pro_api_key:
        headers["X-API-Key"] = s.paco_pro_api_key
    elif s.paco_api_key:
        # Fallback dev si PRO y APP comparten key.
        headers["X-API-Key"] = s.paco_api_key

    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(url, data=form, headers=headers)

    if resp.status_code >= 400:
        log.error("Paco PRO %s: %s %s", url, resp.status_code, resp.text[:300])
        raise PacoError(f"HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        data = resp.json()
    except ValueError:
        raise PacoError(f"respuesta no-JSON: {resp.text[:200]}")

    search_id = data.get("search_id") or data.get("sid") or data.get("id")
    if not search_id:
        raise PacoError(f"respuesta sin 'search_id' ni 'id': {data}")

    return PacoSubmitResult(
        search_id=str(search_id),
        status=str(data.get("status", "unknown")),
        raw=data,
    )


async def get_status(search_id: str) -> dict[str, Any]:
    """Consulta el progreso de un search en Paco."""
    if not search_id:
        raise PacoError("search_id vacío")
    s = get_settings()
    url = f"{s.paco_url.rstrip('/')}{s.paco_status_path}/{search_id}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=_headers())

    if resp.status_code >= 400:
        raise PacoError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError:
        raise PacoError(f"respuesta no-JSON: {resp.text[:200]}")
