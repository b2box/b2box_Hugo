"""Cliente GraphQL para la Admin API de Vendure.

Hugo necesita:
  - leer productos (con custom fields e imagen principal)
  - desactivar duplicados (updateProduct → enabled: false)

Hugo NO actualiza precios de variantes — eso es trabajo del sistema interno
con tasa de cambio + margen + IVA.

La query "lite" (sin variants ni todos los assets) es lo que evita que Vendure
se cuelgue cuando el catálogo tiene cientos de productos.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from gql import Client, gql
from gql.transport.exceptions import TransportError, TransportQueryError
from gql.transport.httpx import HTTPXAsyncTransport

from app.config import get_settings

log = logging.getLogger(__name__)


# ─── DTOs ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class VendureProduct:
    """Vista mínima de un producto Vendure que Hugo necesita."""

    id: str
    name: str
    slug: str
    description: str
    enabled: bool
    source_url: str | None
    image_urls: list[str]
    product_code: str | None  # b2boxProductCode (BX)
    featured_image_url: str | None  # primera imagen (preview/source)


# ─── Cliente ───────────────────────────────────────────────────────


class VendureClient:
    """Wrapper async sobre la Admin API de Vendure."""

    DEFAULT_PAGE_SIZE = 25  # vendure se estrangula con páginas más grandes

    def __init__(self) -> None:
        s = get_settings()
        headers = {"Authorization": f"Bearer {s.vendure_bearer}"}
        if s.vendure_channel_token:
            headers["vendure-token"] = s.vendure_channel_token
        transport = HTTPXAsyncTransport(
            url=s.vendure_api_url,
            headers=headers,
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        self._client = Client(transport=transport, fetch_schema_from_transport=False)
        self._source_field = s.vendure_source_url_field

    # ── Lectura ────────────────────────────────────────────────

    async def list_products(
        self,
        skip: int = 0,
        take: int = DEFAULT_PAGE_SIZE,
    ) -> list[VendureProduct]:
        """Lista productos paginados, con lo MÍNIMO que Hugo necesita.

        Sin variants, sin todos los assets (solo featured). Esto evita que
        Vendure se cuelgue cuando el catálogo es grande.
        """
        query = gql(
            f"""
            query Products($skip: Int!, $take: Int!) {{
              products(options: {{ skip: $skip, take: $take }}) {{
                items {{
                  id
                  name
                  slug
                  description
                  enabled
                  customFields {{ {self._source_field} b2boxProductCode }}
                  featuredAsset {{ source preview }}
                }}
                totalItems
              }}
            }}
            """
        )
        data = await self._execute_with_retry(
            query, {"skip": skip, "take": take}, what=f"list_products(skip={skip})"
        )
        return [self._map_product(p) for p in (data.get("products", {}).get("items") or [])]

    async def get_product(self, product_id: str) -> VendureProduct | None:
        query = gql(
            f"""
            query Product($id: ID!) {{
              product(id: $id) {{
                id
                name
                slug
                description
                enabled
                customFields {{ {self._source_field} }}
                featuredAsset {{ source }}
              }}
            }}
            """
        )
        data = await self._execute_with_retry(
            query, {"id": product_id}, what=f"get_product({product_id})"
        )
        return self._map_product(data["product"]) if data.get("product") else None

    # ── Escritura ──────────────────────────────────────────────

    async def disable_product(self, product_id: str) -> None:
        """Marca un producto como deshabilitado (soft-delete para duplicados)."""
        mutation = gql(
            """
            mutation DisableProduct($input: UpdateProductInput!) {
              updateProduct(input: $input) { id enabled }
            }
            """
        )
        await self._execute_with_retry(
            mutation,
            {"input": {"id": product_id, "enabled": False}},
            what=f"disable_product({product_id})",
        )

    # ── Helpers ────────────────────────────────────────────────

    async def _execute_with_retry(
        self,
        query,
        variables: dict[str, Any],
        what: str,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        """Ejecuta la query con retry exponencial ante timeout/transport errors."""
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                async with self._client as session:
                    return await session.execute(query, variable_values=variables)
            except (TransportError, TransportQueryError, httpx.HTTPError) as exc:
                last_exc = exc
                if attempt < max_attempts:
                    backoff = 2 ** (attempt - 1)
                    log.warning(
                        "%s falló (intento %d/%d): %s — reintento en %ds",
                        what, attempt, max_attempts, type(exc).__name__, backoff,
                    )
                    await asyncio.sleep(backoff)
        log.error("%s falló definitivamente tras %d intentos", what, max_attempts)
        raise last_exc  # type: ignore[misc]

    def _map_product(self, raw: dict[str, Any]) -> VendureProduct:
        custom = raw.get("customFields") or {}
        featured = raw.get("featuredAsset") or {}
        # Para mostrar usamos preview (más liviana); para image_hash usamos source.
        featured_preview = featured.get("preview") or featured.get("source")
        image_urls: list[str] = []
        if featured.get("source"):
            image_urls.append(featured["source"])
        return VendureProduct(
            id=str(raw["id"]),
            name=raw.get("name", ""),
            slug=raw.get("slug", ""),
            description=raw.get("description", "") or "",
            enabled=bool(raw.get("enabled", True)),
            source_url=custom.get(self._source_field),
            image_urls=image_urls,
            product_code=custom.get("b2boxProductCode"),
            featured_image_url=featured_preview,
        )
