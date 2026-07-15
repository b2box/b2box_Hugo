"""Cliente GraphQL para la Admin API de Vendure.

Hugo necesita:
  - leer productos (con custom fields e imagen principal)
  - desactivar duplicados (updateProduct → enabled: false)

El bearer de Vendure expira (típicamente cada 12h). Cuando recibimos un error
de auth, automáticamente hacemos login con VENDURE_USER/VENDURE_PASS, obtenemos
un bearer nuevo del header `vendure-auth-token`, actualizamos el transport, y
reintentamos la request original. El bearer renovado vive en memoria (si Hugo
restartea, hace login al primer call de nuevo).
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


def _safe_int(v: Any) -> int | None:
    """priceWithTax/stock a int, o None si no parsea."""
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class VendureVariant:
    """Vista mínima de una variante Vendure."""

    id: str
    name: str
    sku: str


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
    first_variant_price_cents: int | None  # precio de la 1ra variante (centavos)
    variant_count: int  # cuántas variantes tiene
    variants: list[VendureVariant] | None = None  # solo se llena con list_products_with_variants
    updated_at: str | None = None  # ISO8601 (Vendure updatedAt) — para dedup incremental


# ─── Cliente ───────────────────────────────────────────────────────


# Mensajes de error de auth devueltos por Vendure cuando el bearer expiró
_AUTH_ERROR_HINTS = (
    "FORBIDDEN",
    "UNAUTHORIZED",
    "NOT_VERIFIED",
    "no token",
    "session has expired",
    "invalid token",
)


class VendureClient:
    """Wrapper async sobre la Admin API de Vendure con auto-renovación del bearer."""

    DEFAULT_PAGE_SIZE = 25

    def __init__(self) -> None:
        s = get_settings()
        self._url = s.vendure_api_url
        self._channel_token = s.vendure_channel_token
        self._user = s.vendure_user
        self._pass = s.vendure_pass
        self._source_field = s.vendure_source_url_field
        # Bearer actual: arranca con el del .env, se reemplaza cuando se renueva
        self._bearer: str = s.vendure_bearer or ""
        self._login_lock = asyncio.Lock()  # evita re-logins concurrentes
        if not self._bearer:
            log.info(
                "VENDURE_BEARER vacío — Hugo se va a loguear con user/pass al primer call"
            )
        self._build_client()

    def _build_client(self) -> None:
        """(Re)crea el gql.Client con el bearer actual."""
        headers = {"Authorization": f"Bearer {self._bearer}"}
        if self._channel_token:
            headers["vendure-token"] = self._channel_token
        transport = HTTPXAsyncTransport(
            url=self._url,
            headers=headers,
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        self._client = Client(transport=transport, fetch_schema_from_transport=False)

    async def _login(self) -> bool:
        """Hace login con VENDURE_USER/PASS y guarda el nuevo bearer.

        Devuelve True si el login fue exitoso, False si falla (típicamente
        porque no hay credenciales configuradas).
        """
        if not self._user or not self._pass:
            log.error("VENDURE_USER/PASS no configurados — no puedo renovar el bearer")
            return False

        async with self._login_lock:
            log.info("Renovando bearer de Vendure (login con usuario %s)", self._user)
            mutation = (
                "mutation Login($u: String!, $p: String!) { "
                "  login(username: $u, password: $p, rememberMe: true) { "
                "    __typename "
                "    ... on CurrentUser { id identifier } "
                "    ... on InvalidCredentialsError { message } "
                "    ... on NativeAuthStrategyError { message } "
                "  } "
                "}"
            )
            headers = {"Content-Type": "application/json"}
            if self._channel_token:
                headers["vendure-token"] = self._channel_token

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        self._url,
                        json={
                            "query": mutation,
                            "variables": {"u": self._user, "p": self._pass},
                        },
                        headers=headers,
                    )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.error("Login a Vendure falló: %s", exc)
                return False

            new_bearer = resp.headers.get("vendure-auth-token")
            if not new_bearer:
                log.error(
                    "Login OK pero sin header vendure-auth-token. "
                    "Vendure tiene que estar configurado con bearer auth (no cookie). Body: %s",
                    resp.text[:200],
                )
                return False

            data = resp.json().get("data", {}).get("login", {})
            if data.get("__typename") != "CurrentUser":
                log.error("Login devolvió error: %s", data.get("message") or data)
                return False

            self._bearer = new_bearer
            self._build_client()
            log.info("Bearer de Vendure renovado OK")
            return True

    @staticmethod
    def _is_auth_error(exc: Exception) -> bool:
        """Detecta si la excepción es por auth/token expirado."""
        msg = str(exc).upper()
        return any(hint.upper() in msg for hint in _AUTH_ERROR_HINTS) or "401" in msg

    # ── Lectura ────────────────────────────────────────────────

    # Tamaño de página para lecturas masivas del catálogo. 100 baja los
    # round-trips ~4x vs 25 (1494 productos: ~15 páginas en vez de ~60).
    BULK_PAGE_SIZE = 100
    # Cuántas páginas pedir a Vendure en paralelo al traer todo el catálogo.
    FETCH_CONCURRENCY = 6

    def _products_query(self, with_variants: bool):
        """Query de listado. Con variantes trae id/name/sku de cada variante;
        sin variantes trae solo la 1ra (para precio) — más liviano."""
        variant_block = (
            "variantList(options: { take: 100 }) { items { id name sku priceWithTax } totalItems }"
            if with_variants
            else "variantList(options: { take: 1 }) { items { priceWithTax } totalItems }"
        )
        return gql(
            f"""
            query Products($skip: Int!, $take: Int!) {{
              products(options: {{ skip: $skip, take: $take }}) {{
                items {{
                  id
                  name
                  slug
                  description
                  enabled
                  updatedAt
                  customFields {{ {self._source_field} b2boxProductCode }}
                  featuredAsset {{ source preview }}
                  {variant_block}
                }}
                totalItems
              }}
            }}
            """
        )

    def _map_page(self, raw_items: list[dict[str, Any]], with_variants: bool) -> list[VendureProduct]:
        out: list[VendureProduct] = []
        for raw in raw_items:
            prod = self._map_product(raw)
            if with_variants:
                variant_items = (raw.get("variantList") or {}).get("items") or []
                prod.variants = [
                    VendureVariant(id=str(v["id"]), name=v.get("name", ""), sku=v.get("sku", ""))
                    for v in variant_items
                ]
            out.append(prod)
        return out

    async def _fetch_page(
        self, skip: int, take: int, with_variants: bool,
    ) -> tuple[list[VendureProduct], int]:
        """Trae una página y devuelve (productos, totalItems)."""
        data = await self._execute_with_retry(
            self._products_query(with_variants),
            {"skip": skip, "take": take},
            what=f"products(skip={skip}, take={take}, variants={with_variants})",
        )
        block = data.get("products", {}) or {}
        items = self._map_page(block.get("items") or [], with_variants)
        total = int(block.get("totalItems") or len(items))
        return items, total

    async def list_products(
        self, skip: int = 0, take: int = DEFAULT_PAGE_SIZE,
    ) -> list[VendureProduct]:
        items, _ = await self._fetch_page(skip, take, with_variants=False)
        return items

    async def list_products_with_variants(
        self, skip: int = 0, take: int = DEFAULT_PAGE_SIZE,
    ) -> list[VendureProduct]:
        """Como list_products pero trae TODAS las variantes con nombre y SKU."""
        items, _ = await self._fetch_page(skip, take, with_variants=True)
        return items

    async def fetch_all_products(
        self, with_variants: bool = False, page_size: int | None = None,
    ) -> list[VendureProduct]:
        """Trae TODO el catálogo. La 1ra página da totalItems; el resto se piden
        en paralelo (semáforo FETCH_CONCURRENCY). Mucho más rápido que iterar
        secuencialmente página por página."""
        take = page_size or self.BULK_PAGE_SIZE
        first, total = await self._fetch_page(0, take, with_variants)
        if len(first) >= total or len(first) < take:
            return first

        out: list[VendureProduct | None] = list(first)
        skips = list(range(take, total, take))
        sem = asyncio.Semaphore(self.FETCH_CONCURRENCY)

        async def _one(skip: int) -> list[VendureProduct]:
            async with sem:
                items, _ = await self._fetch_page(skip, take, with_variants)
                return items

        pages = await asyncio.gather(*(_one(s) for s in skips))
        for page in pages:
            out.extend(page)
        return [p for p in out if p is not None]

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
                customFields {{ {self._source_field} b2boxProductCode }}
                featuredAsset {{ source preview }}
              }}
            }}
            """
        )
        data = await self._execute_with_retry(
            query, {"id": product_id}, what=f"get_product({product_id})"
        )
        return self._map_product(data["product"]) if data.get("product") else None

    async def get_product_full(self, product_id: str) -> dict[str, Any] | None:
        """Data COMPLETA de un producto para devolver en /verify cuando es duplicado.

        A diferencia de get_product (mínimo), trae TODAS las fotos (assets),
        TODAS las variantes con precio/sku/stock y los customFields. Devuelve un
        dict listo para serializar en la respuesta HTTP (no un VendureProduct).
        """
        query = gql(
            f"""
            query ProductFull($id: ID!) {{
              product(id: $id) {{
                id
                name
                slug
                description
                enabled
                customFields {{ {self._source_field} b2boxProductCode }}
                featuredAsset {{ source preview }}
                assets {{ source preview }}
                variantList(options: {{ take: 100 }}) {{
                  items {{ id name sku priceWithTax currencyCode stockLevel }}
                  totalItems
                }}
              }}
            }}
            """
        )
        data = await self._execute_with_retry(
            query, {"id": product_id}, what=f"get_product_full({product_id})"
        )
        raw = data.get("product")
        if not raw:
            return None
        custom = raw.get("customFields") or {}
        featured = raw.get("featuredAsset") or {}
        assets = raw.get("assets") or []
        image_urls = [a.get("source") for a in assets if a.get("source")]
        if featured.get("source") and featured["source"] not in image_urls:
            image_urls.insert(0, featured["source"])
        vlist = raw.get("variantList") or {}
        variants = [
            {
                "id": str(v.get("id")),
                "name": v.get("name", ""),
                "sku": v.get("sku", ""),
                "price_cents": _safe_int(v.get("priceWithTax")),
                "currency": v.get("currencyCode"),
                "stock": v.get("stockLevel"),
            }
            for v in (vlist.get("items") or [])
        ]
        first_price = variants[0]["price_cents"] if variants else None
        return {
            "id": str(raw["id"]),
            "name": raw.get("name", ""),
            "slug": raw.get("slug", ""),
            "description": raw.get("description", "") or "",
            "enabled": bool(raw.get("enabled", True)),
            "source_url": custom.get(self._source_field),
            "product_code": custom.get("b2boxProductCode"),
            "featured_image_url": featured.get("preview") or featured.get("source"),
            "image_urls": image_urls,
            "first_variant_price_cents": first_price,
            "variant_count": int(vlist.get("totalItems") or len(variants)),
            "variants": variants,
        }

    # ── Escritura ──────────────────────────────────────────────

    async def get_enabled_status(self, product_id: str) -> bool | None:
        """Devuelve True/False según el flag `enabled` actual del producto, o None si no existe."""
        query = gql(
            """
            query GetEnabled($id: ID!) {
              product(id: $id) { id enabled }
            }
            """
        )
        data = await self._execute_with_retry(
            query, {"id": product_id}, what=f"get_enabled_status({product_id})"
        )
        prod = data.get("product")
        if not prod:
            return None
        return bool(prod.get("enabled"))

    async def disable_product(self, product_id: str) -> None:
        await self._set_enabled(product_id, False)

    async def enable_product(self, product_id: str) -> None:
        await self._set_enabled(product_id, True)

    async def _set_enabled(self, product_id: str, enabled: bool) -> None:
        mutation = gql(
            """
            mutation SetEnabled($input: UpdateProductInput!) {
              updateProduct(input: $input) { id enabled }
            }
            """
        )
        await self._execute_with_retry(
            mutation,
            {"input": {"id": product_id, "enabled": enabled}},
            what=f"set_enabled({product_id}, {enabled})",
        )

    # ── Helpers ────────────────────────────────────────────────

    async def _execute_with_retry(
        self,
        query,
        variables: dict[str, Any],
        what: str,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        """Ejecuta la query con retry exponencial + auto-renovación del bearer."""
        # Si arrancamos sin bearer, hacemos login proactivo antes del primer call
        if not self._bearer:
            await self._login()

        last_exc: Exception | None = None
        relogged_in = False
        for attempt in range(1, max_attempts + 1):
            try:
                async with self._client as session:
                    return await session.execute(query, variable_values=variables)
            except (TransportError, TransportQueryError, httpx.HTTPError) as exc:
                last_exc = exc
                # Si el error parece ser de auth y todavía no intentamos renovar,
                # hacemos login y retry SIN consumir attempts del backoff
                if self._is_auth_error(exc) and not relogged_in:
                    log.warning(
                        "%s tiró error de auth, intento renovar el bearer", what
                    )
                    relogged_in = True
                    if await self._login():
                        continue  # retry inmediato con bearer nuevo
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
        featured_preview = featured.get("preview") or featured.get("source")
        image_urls: list[str] = []
        if featured.get("source"):
            image_urls.append(featured["source"])
        # Precio + cantidad de variantes (puede no venir en queries antiguas)
        variant_list = raw.get("variantList") or {}
        variant_items = variant_list.get("items") or []
        first_price = None
        if variant_items:
            try:
                first_price = int(variant_items[0].get("priceWithTax") or 0)
            except (TypeError, ValueError):
                first_price = None
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
            first_variant_price_cents=first_price,
            variant_count=int(variant_list.get("totalItems") or len(variant_items)),
            updated_at=raw.get("updatedAt"),
        )
