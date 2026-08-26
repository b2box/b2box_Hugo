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

# Tras un refresh fallido, cuánto esperar antes de volver a intentar contra
# Vendure. Sin esto, cada request pagaría el timeout completo (60s) mientras
# Vendure siga lento.
_RETRY_AFTER_FAILURE_SECONDS = 60.0


async def _fetch_all(client: VendureClient) -> list[VendureProduct]:
    # Páginas de 100 en paralelo (ver VendureClient.fetch_all_products).
    return await client.fetch_all_products(with_variants=False)


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
        try:
            products = await _fetch_all(client)
        except Exception as exc:  # noqa: BLE001
            # Vendure lento o caído. Un catálogo de hace unos minutos sigue
            # siendo perfectamente comparable — devolver eso es infinitamente
            # mejor que tirar el lookup entero con un 502. Sin cache previo no
            # hay nada que devolver: ahí sí se propaga.
            if _cache:
                age = int(time.monotonic() - _loaded_at)
                log.warning(
                    "Vendure no respondió (%s: %s); uso catálogo cacheado de hace %ds (%d productos)",
                    type(exc).__name__, exc, age, len(_cache),
                )
                # Correr la ventana para no martillar a Vendure en cada request
                # mientras siga caído, pero reintentar pronto.
                _loaded_at = time.monotonic() - ttl + _RETRY_AFTER_FAILURE_SECONDS
                return _cache
            raise
        _cache = products
        _loaded_at = time.monotonic()
        log.info("Catálogo Vendure cacheado: %d productos (TTL %ds)", len(products), ttl)
        return _cache
