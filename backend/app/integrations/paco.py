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
    if s.paco_api_key:
        h["X-API-Key"] = s.paco_api_key
    if s.paco_cf_client_id and s.paco_cf_client_secret:
        h["CF-Access-Client-Id"] = s.paco_cf_client_id
        h["CF-Access-Client-Secret"] = s.paco_cf_client_secret
    return h


async def submit(image_url: str) -> PacoSubmitResult:
    """Envía image_url a Paco y devuelve el search_id."""
    if not image_url:
        raise PacoError("image_url vacío")
    s = get_settings()
    if not s.paco_url:
        raise PacoError("PACO_URL no configurado")

    url = f"{s.paco_url.rstrip('/')}{s.paco_submit_path}"
    payload = {"image_url": image_url, "source": "hugo"}

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
