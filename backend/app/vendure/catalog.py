"""Cache TTL del catálogo Vendure para /verify.

Antes, cada llamada de Luis a /verify descargaba hasta 500 productos (y sus
imágenes) de Vendure. Con muchos 👍 seguidos eso es lento y caro, y además el
tope de 500 hacía que catálogos más grandes NO detectaran duplicados.

Acá cacheamos el catálogo completo (paginado, sin tope) por
`verify_catalog_ttl_seconds`. Un lock async evita que N requests concurrentes
disparen N refetches del catálogo a la vez (thundering herd).
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.config import get_settings
from app.vendure.client import VendureClient, VendureProduct

log = logging.getLogger(__name__)

_cache: list[VendureProduct] = []
_loaded_at: float = 0.0
_lock = asyncio.Lock()


async def _fetch_all(client: VendureClient) -> list[VendureProduct]:
    out: list[VendureProduct] = []
    skip = 0
    page_size = VendureClient.DEFAULT_PAGE_SIZE
    while True:
        page = await client.list_products(skip=skip, take=page_size)
        if not page:
            break
        out.extend(page)
        if len(page) < page_size:
            break
        skip += page_size
    return out


def invalidate() -> None:
    """Fuerza el próximo get_catalog() a re-consultar Vendure.

    Llamar después de deshabilitar/rehabilitar productos, para que /verify no
    compare contra un estado viejo cacheado.
    """
    global _loaded_at
    _loaded_at = 0.0


async def get_catalog(force: bool = False) -> list[VendureProduct]:
    """Devuelve el catálogo completo, cacheado por TTL."""
    global _cache, _loaded_at
    ttl = get_settings().verify_catalog_ttl_seconds
    if not force and _cache and (time.monotonic() - _loaded_at) < ttl:
        return _cache
    async with _lock:
        # Re-chequear adentro del lock: otro request pudo refrescar mientras esperábamos.
        if not force and _cache and (time.monotonic() - _loaded_at) < ttl:
            return _cache
        client = VendureClient()
        products = await _fetch_all(client)
        _cache = products
        _loaded_at = time.monotonic()
        log.info("Catálogo Vendure cacheado: %d productos (TTL %ds)", len(products), ttl)
        return _cache
