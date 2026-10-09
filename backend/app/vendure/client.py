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
from dataclasses import dataclass, field
from typing import Any

import httpx
from gql import Client, gql
from gql.transport import httpx as _gql_httpx_transport
from gql.transport.exceptions import TransportError, TransportQueryError
from gql.transport.httpx import HTTPXAsyncTransport

from app.config import get_settings
from app.pricing.market_specs import our_specs_from_custom_fields
from app.pricing.semaforo import PricedVariant, PriceTier

log = logging.getLogger(__name__)


def quiet_http_loggers() -> None:
    """Sube gql, httpx y httpcore a WARNING. Con LOG_LEVEL=DEBUG el transporte de gql loguea la
    respuesta completa (ahí viajan supplierBusiness/supplierSizeModel/supplierLink de todos los
    productos) y httpcore los headers de cada pedido (el vendure-auth-token del login); httpx, en
    INFO, la URL de cada request. Se llama al importar este módulo y desde _configure_logging."""
    for name in ("gql", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


quiet_http_loggers()

# El módulo httpx que usa el transport de gql, que NO siempre es el nuestro:
# gql 4.4 importa `httpx2` si está instalado, y anthropic>=1 / openai>=3 lo
# instalan. Un httpx.Timeout de httpx 0.28 pasado a un cliente httpx2 llega
# crudo a httpcore2 y toda query revienta con "unsupported operand type(s)
# for +: 'float' and 'Timeout'" (08-oct-2026: el semáforo y todo lo que lee
# Vendure desde el build del 07-oct). El timeout y los errores del transport
# se arman/atrapan con el módulo del transport, no con el nuestro.
_gql_httpx = _gql_httpx_transport.httpx
_GQL_TIMEOUT = _gql_httpx.Timeout(60.0, connect=10.0)
_TRANSPORT_HTTP_ERRORS: tuple[type[BaseException], ...] = tuple(
    {httpx.HTTPError, _gql_httpx.HTTPError}
)


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
    # Variantes con priceWithTax + tramos (bulkPriceTiers). Solo las llena
    # fetch_all_products_priced(): es lo que compara el semáforo contra ML.
    priced_variants: list[PricedVariant] | None = None


@dataclass(slots=True)
class ProductTranslationText:
    """Nombre, slug y descripción de UNA traducción de un producto."""

    language: str
    name: str
    slug: str
    description: str


@dataclass(slots=True)
class ProductTexts:
    """Lo que la auditoría de textos (HG1) lee de un producto: todas sus
    traducciones y los datos del proveedor con los que se comparan los textos.
    Los datos del proveedor no se muestran ni se guardan: no salen en `repr`."""

    id: str
    enabled: bool
    product_code: str | None
    updated_at: str | None
    translations: list[ProductTranslationText]
    supplier_business: str | None = field(default=None, repr=False)
    supplier_size_model: str | None = field(default=None, repr=False)
    supplier_link: str | None = field(default=None, repr=False)


@dataclass(slots=True)
class TextsRead:
    """Resultado de VendureClient.fetch_texts_for_audit. `supplier_fields` es
    False si el schema de Vendure no tiene supplierBusiness/supplierSizeModel y se
    leyó sin ellos (la regla FAB queda a medias)."""

    products: list[ProductTexts]
    supplier_fields: bool = True


# Valor por defecto de `channel_token`: "el canal de VENDURE_CHANNEL_TOKEN".
# None significa otra cosa: sin header `vendure-token`, o sea el canal por defecto.
_CHANNEL_FROM_SETTINGS: Any = object()


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

    # ¿El schema de Vendure tiene los custom fields de medidas de la variante?
    # Si una query de precios revienta por un campo que no existe, se apaga y el
    # semáforo sigue sin medidas en vez de quedarse sin precios.
    _dims_supported: bool = True

    # Último bearer renovado, compartido entre TODAS las instancias del proceso.
    # Sin esto, cada VendureClient() nuevo pagaba un login completo.
    _shared_bearer: str = ""

    DEFAULT_PAGE_SIZE = 25

    def __init__(self, channel_token: str | None = _CHANNEL_FROM_SETTINGS) -> None:
        """`channel_token`: sin pasarlo, el canal de VENDURE_CHANNEL_TOKEN. Un
        token explícito elige ese canal; `None` lee el canal por defecto (sin
        header `vendure-token`)."""
        s = get_settings()
        self._url = s.vendure_api_url
        self._channel_token = (
            s.vendure_channel_token if channel_token is _CHANNEL_FROM_SETTINGS else channel_token
        )
        self._user = s.vendure_user
        self._pass = s.vendure_pass
        self._source_field = s.vendure_source_url_field
        # Bearer actual: arranca con el del .env o con el último renovado por
        # CUALQUIER instancia. Antes cada VendureClient() nuevo (uno por refresh
        # de catálogo) arrancaba sin bearer y pagaba un login entero — 8-25s
        # contra el admin de prod, en cada refresh.
        self._bearer: str = s.vendure_bearer or VendureClient._shared_bearer
        self._login_lock = asyncio.Lock()  # evita re-logins concurrentes
        if not self._bearer:
            log.info(
                "VENDURE_BEARER vacío — Hugo se va a loguear con user/pass al primer call"
            )

    def _new_client(self) -> Client:
        """Crea un gql.Client NUEVO con el bearer actual.

        Devuelve una instancia fresca (transport propio) en vez de un cliente
        compartido: fetch_all_products lanza hasta FETCH_CONCURRENCY páginas con
        asyncio.gather, y un gql.Client con un solo HTTPXAsyncTransport no
        soporta sesiones concurrentes — el 2do `async with` sobre el mismo
        transport tira TransportAlreadyConnected. Con un client por ejecución
        cada corrutina tiene su propio transport y no colisionan.
        """
        headers = {"Authorization": f"Bearer {self._bearer}"}
        if self._channel_token:
            headers["vendure-token"] = self._channel_token
        transport = HTTPXAsyncTransport(
            url=self._url,
            headers=headers,
            timeout=_GQL_TIMEOUT,
        )
        # execute_timeout: el default de gql es 10s POR QUERY y una página del
        # catálogo en prod puede tardar más que eso — moría con un
        # asyncio.TimeoutError de mensaje vacío. El tope real lo pone httpx
        # (60s por request); acá solo dejamos de serruchar por debajo.
        return Client(
            transport=transport,
            fetch_schema_from_transport=False,
            execute_timeout=90.0,
        )

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
            VendureClient._shared_bearer = new_bearer
            # No hay client compartido que reconstruir: cada _execute_with_retry
            # arma el suyo con self._bearer, que acabamos de actualizar.
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
    # Cada página de 100 productos dispara ~400 queries a PG del lado de
    # Vendure (Product.variantList es un N+1 por producto). Con 6 páginas en
    # paralelo el pool PG del admin-server (25 conexiones) quedaba encolado y
    # Login / GetOrderDetails del admin esperaban turno. 2 mantiene el full
    # refresh razonable sin ahogar al resto del admin.
    FETCH_CONCURRENCY = 2

    def _product_fields(self, with_variants: bool, pricing: bool = False) -> str:
        """Campos de un producto en los listados. Con variantes trae id/name/sku
        de cada variante; sin variantes trae solo la 1ra (para precio) — más liviano.
        `pricing` trae además los tramos de cantidad (bulkPriceTiers) de cada
        variante: es lo que necesita el semáforo para saber nuestro precio."""
        if pricing:
            # Medidas de la variante (cm y kg) para comparar con la publicación de
            # ML. Son columnas de la misma fila: no suman queries del lado de Vendure.
            dims = (" customFields { length width height weight boxLength boxWidth boxHeight boxWeight }"
                    if VendureClient._dims_supported else "")
            variant_block = (
                "variantList(options: { take: 50 }) { items { id name sku priceWithTax currencyCode"
                f"{dims} bulkPriceTiers {{ position enabled minQuantity maxQuantity salePrice }} }} totalItems }}"
            )
        elif with_variants:
            variant_block = (
                "variantList(options: { take: 100 }) { items { id name sku priceWithTax } totalItems }"
            )
        else:
            variant_block = "variantList(options: { take: 1 }) { items { priceWithTax } totalItems }"
        return f"""
                  id
                  name
                  slug
                  description
                  enabled
                  updatedAt
                  customFields {{ {self._source_field} b2boxProductCode }}
                  featuredAsset {{ source preview }}
                  {variant_block}
        """

    def _products_query(self, with_variants: bool, pricing: bool = False):
        """Query de listado paginado completo."""
        return gql(
            f"""
            query Products($skip: Int!, $take: Int!) {{
              products(options: {{ skip: $skip, take: $take }}) {{
                items {{ {self._product_fields(with_variants, pricing)} }}
                totalItems
              }}
            }}
            """
        )

    def _products_updated_since_query(self, with_variants: bool):
        """Listado filtrado por `updatedAt > $since` (refresh incremental del
        catálogo). Vendure resuelve el filtro en SQL, así que una corrida sin
        cambios cuesta 2 queries a PG en vez de ~6.000."""
        return gql(
            f"""
            query ProductsUpdatedSince($since: DateTime!, $skip: Int!, $take: Int!) {{
              products(options: {{
                skip: $skip, take: $take,
                filter: {{ updatedAt: {{ after: $since }} }},
                sort: {{ updatedAt: ASC }}
              }}) {{
                items {{ {self._product_fields(with_variants)} }}
                totalItems
              }}
            }}
            """
        )

    @staticmethod
    def _map_priced_variants(variant_items: list[dict[str, Any]]) -> list[PricedVariant]:
        """variantList.items (con bulkPriceTiers) → PricedVariant. Los tramos
        quedan ordenados por `position`; Money de Vendure ya viene en centavos."""
        out: list[PricedVariant] = []
        for v in variant_items:
            if not isinstance(v, dict):
                continue
            tiers = [
                PriceTier(
                    position=_safe_int(t.get("position")) or 0,
                    min_quantity=_safe_int(t.get("minQuantity")),
                    max_quantity=_safe_int(t.get("maxQuantity")),
                    sale_price_cents=_safe_int(t.get("salePrice")),
                    enabled=bool(t.get("enabled", True)),
                )
                for t in (v.get("bulkPriceTiers") or [])
                if isinstance(t, dict)
            ]
            tiers.sort(key=lambda t: t.position)
            specs = our_specs_from_custom_fields(v.get("customFields"))
            out.append(PricedVariant(
                id=str(v.get("id")),
                name=v.get("name") or "",
                sku=v.get("sku") or "",
                price_with_tax_cents=_safe_int(v.get("priceWithTax")),
                currency=v.get("currencyCode"),
                tiers=tuple(tiers),
                specs=specs.as_dict() if specs else {},
            ))
        return out

    def _map_page(
        self, raw_items: list[dict[str, Any]], with_variants: bool, pricing: bool = False,
    ) -> list[VendureProduct]:
        out: list[VendureProduct] = []
        for raw in raw_items:
            prod = self._map_product(raw)
            variant_items = (raw.get("variantList") or {}).get("items") or []
            if with_variants:
                prod.variants = [
                    VendureVariant(id=str(v["id"]), name=v.get("name", ""), sku=v.get("sku", ""))
                    for v in variant_items
                ]
            if pricing:
                prod.priced_variants = self._map_priced_variants(variant_items)
            out.append(prod)
        return out

    async def _fetch_page(
        self, skip: int, take: int, with_variants: bool, pricing: bool = False,
    ) -> tuple[list[VendureProduct], int]:
        """Trae una página y devuelve (productos, totalItems)."""
        data = await self._execute_with_retry(
            self._products_query(with_variants, pricing),
            {"skip": skip, "take": take},
            what=f"products(skip={skip}, take={take}, variants={with_variants}, pricing={pricing})",
        )
        block = data.get("products", {}) or {}
        items = self._map_page(block.get("items") or [], with_variants, pricing)
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

    async def count_products(self) -> int:
        """Cantidad total de productos (no borrados) en Vendure. Sin pedir
        `items`, Vendure no resuelve variantList: son 2 queries baratas a PG."""
        query = gql(
            """
            query ProductCount {
              products(options: { take: 1 }) { totalItems }
            }
            """
        )
        data = await self._execute_with_retry(query, {}, what="count_products")
        return int((data.get("products") or {}).get("totalItems") or 0)

    async def fetch_products_updated_since(
        self, since_iso: str, with_variants: bool = False, page_size: int | None = None,
    ) -> list[VendureProduct]:
        """Productos con `updatedAt` posterior a `since_iso` (ISO-8601 UTC).

        Secuencial a propósito: en un catálogo estable devuelve 0-1 páginas y
        lo que importa es no cargar a Vendure, no la velocidad."""
        take = page_size or self.BULK_PAGE_SIZE
        query = self._products_updated_since_query(with_variants)
        out: list[VendureProduct] = []
        skip = 0
        while True:
            data = await self._execute_with_retry(
                query, {"since": since_iso, "skip": skip, "take": take},
                what=f"products(updatedAt>{since_iso}, skip={skip})",
            )
            block = data.get("products", {}) or {}
            items = self._map_page(block.get("items") or [], with_variants)
            out.extend(items)
            total = int(block.get("totalItems") or len(out))
            if not items or len(items) < take or len(out) >= total:
                return out
            skip += take

    async def fetch_all_products(
        self,
        with_variants: bool = False,
        page_size: int | None = None,
        concurrency: int | None = None,
        pricing: bool = False,
    ) -> list[VendureProduct]:
        """Trae TODO el catálogo. La 1ra página da totalItems; el resto se piden
        en paralelo (semáforo `concurrency`, default FETCH_CONCURRENCY). Mucho más
        rápido que iterar secuencialmente página por página."""
        take = page_size or self.BULK_PAGE_SIZE
        first, total = await self._fetch_page(0, take, with_variants, pricing)
        if len(first) >= total or len(first) < take:
            return first

        out: list[VendureProduct | None] = list(first)
        skips = list(range(take, total, take))
        sem = asyncio.Semaphore(max(1, concurrency or self.FETCH_CONCURRENCY))

        async def _one(skip: int) -> list[VendureProduct]:
            async with sem:
                items, _ = await self._fetch_page(skip, take, with_variants, pricing)
                return items

        pages = await asyncio.gather(*(_one(s) for s in skips))
        for page in pages:
            out.extend(page)
        return [p for p in out if p is not None]

    async def fetch_all_products_priced(
        self, concurrency: int | None = None,
    ) -> list[VendureProduct]:
        """Catálogo completo con `priced_variants` (priceWithTax + tramos).

        Es la lectura FRESCA que hace el semáforo cada noche: el cache de
        app/vendure/catalog.py puede tener el precio hasta 12 h viejo. Misma
        concurrencia baja que el full refresh (2): cada página sigue siendo un
        N+1 de variantList del lado de Vendure."""
        try:
            return await self.fetch_all_products(
                with_variants=False, concurrency=concurrency or self.FETCH_CONCURRENCY, pricing=True,
            )
        except TransportQueryError as exc:
            if not VendureClient._dims_supported or "Cannot query field" not in str(exc):
                raise
            log.warning("Vendure no tiene los custom fields de medidas de la variante: "
                        "se piden los precios sin medidas (%s)", str(exc)[:160])
            VendureClient._dims_supported = False
            return await self.fetch_all_products(
                with_variants=False, concurrency=concurrency or self.FETCH_CONCURRENCY, pricing=True,
            )

    # ── Textos para la auditoría (solo lectura) ────────────────

    TEXT_AUDIT_PAGE_SIZE = 100

    def _text_audit_query(self, supplier_fields: bool):
        """Query de la auditoría de textos. Sin `variantList`: una página de 100
        productos es una sola lectura barata del lado de Vendure, no el N+1 del
        listado de precios. Siempre `query`: la auditoría no escribe."""
        custom = f"{self._source_field} b2boxProductCode"
        if supplier_fields:
            custom = f"supplierBusiness supplierSizeModel {custom}"
        text = (
            "query TextAuditProducts($skip: Int!, $take: Int!) { "
            "products(options: { skip: $skip, take: $take, sort: { id: ASC } }) { "
            "items { id enabled updatedAt "
            "translations { languageCode name slug description } "
            f"customFields {{ {custom} }} }} totalItems }} }}"
        )
        if not text.lstrip().startswith("query "):  # defensa: nunca una mutation
            raise RuntimeError("la lectura de textos tiene que ser una query")
        return gql(text)

    @staticmethod
    def _map_product_texts(raw: dict[str, Any], source_field: str) -> ProductTexts:
        custom = raw.get("customFields") or {}
        return ProductTexts(
            id=str(raw["id"]),
            enabled=bool(raw.get("enabled", True)),
            product_code=custom.get("b2boxProductCode"),
            updated_at=raw.get("updatedAt"),
            translations=[
                ProductTranslationText(
                    language=str(t.get("languageCode") or ""),
                    name=t.get("name") or "",
                    slug=t.get("slug") or "",
                    description=t.get("description") or "",
                )
                for t in (raw.get("translations") or [])
                if isinstance(t, dict)
            ],
            supplier_business=custom.get("supplierBusiness"),
            supplier_size_model=custom.get("supplierSizeModel"),
            supplier_link=custom.get(source_field),
        )

    async def _fetch_texts(self, supplier_fields: bool, page_size: int) -> list[ProductTexts]:
        query = self._text_audit_query(supplier_fields)
        out: list[ProductTexts] = []
        skip = 0
        total: int | None = None
        while True:
            data = await self._execute_with_retry(
                query, {"skip": skip, "take": page_size},
                what=f"textos(skip={skip}, take={page_size})",
            )
            block = data.get("products") or {}
            items = block.get("items") or []
            out.extend(self._map_product_texts(it, self._source_field) for it in items)
            total = int(block.get("totalItems") or len(out))
            if not items or len(items) < page_size or len(out) >= total:
                return out
            skip += page_size

    async def fetch_texts_for_audit(self, page_size: int | None = None) -> TextsRead:
        """Todos los productos (habilitados y no) del canal de este cliente, con
        todas sus traducciones y los datos del proveedor. Páginas de 100, una
        lectura por página, en secuencia. NO escribe nada."""
        take = page_size or self.TEXT_AUDIT_PAGE_SIZE
        try:
            return TextsRead(await self._fetch_texts(True, take), True)
        except TransportQueryError as exc:
            if "Cannot query field" not in str(exc):
                raise
            # Solo para esta lectura: la próxima corrida vuelve a probar (si el schema se
            # actualiza, la regla FAB recupera sus tres campos sin reiniciar Hugo).
            log.warning("Vendure no tiene supplierBusiness/supplierSizeModel: "
                        "se leen los textos sin ellos (%s)", str(exc)[:160])
        return TextsRead(await self._fetch_texts(False, take), False)

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
                async with self._new_client() as session:
                    return await session.execute(query, variable_values=variables)
            except (TransportError, TransportQueryError, *_TRANSPORT_HTTP_ERRORS) as exc:
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
