"""Cache del catálogo Vendure para /verify y /app/lookup.

Antes, cada llamada de Luis a /verify descargaba hasta 500 productos (y sus
imágenes) de Vendure. Con muchos 👍 seguidos eso es lento y caro, y además el
tope de 500 hacía que catálogos más grandes NO detectaran duplicados.

Acá cacheamos el catálogo completo (paginado, sin tope). Un lock async evita
que N requests concurrentes disparen N refetches a la vez (thundering herd).

Cómo se mantiene fresco
-----------------------
Bajar TODO el catálogo es caro del lado de Vendure: `Product.variantList` es un
N+1 por producto (~400 queries a PG por página de 100). Hacerlo cada
`verify_catalog_ttl_seconds` (5 min) tenía al pool PG del admin-server
encolado la mitad del día y a Login/GetOrderDetails del admin esperando turno.

Por eso hay dos modos de refresh:

* **incremental** (el normal, cada TTL): pide solo los productos con
  `updatedAt` posterior al último que ya tenemos (Vendure lo filtra en SQL) y
  los mergea en el cache. Después chequea `totalItems` contra el tamaño del
  cache: si no coincide (alguien borró un producto), cae a un full.
  En un catálogo sin cambios cuesta ~4 queries a PG.
* **full** (cada `catalog_full_refresh_seconds`, default 12 h, o cuando el
  incremental detecta drift): baja todo, con concurrencia baja para no ahogar
  al resto del admin.

Trade-off asumido: un cambio de precio en una variante NO toca el `updatedAt`
del producto, así que `first_variant_price_cents` puede quedar hasta 12 h viejo.
Para dedup/"lo tenemos" el precio es secundario; las auditorías de precio
bajan su propio listado.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from gql.transport.exceptions import TransportQueryError

from app.config import get_settings
from app.vendure.client import VendureClient, VendureProduct

log = logging.getLogger(__name__)

_cache: list[VendureProduct] = []
_loaded_at: float = 0.0      # último refresh OK (full o incremental), monotonic
_full_at: float | None = None  # último refresh FULL OK, monotonic (None = nunca / forzado)
_watermark: str | None = None  # max updatedAt visto en el cache (ISO-8601)
_lock = asyncio.Lock()

# Tras un refresh fallido, cuánto esperar antes de volver a intentar contra
# Vendure. Sin esto, cada request pagaría el timeout completo (60s) mientras
# Vendure siga lento.
_RETRY_AFTER_FAILURE_SECONDS = 60.0

# Margen que restamos al watermark al pedir "updatedAt > since". Cubre
# productos que compartan timestamp con el último visto o skew de reloj. El
# merge es idempotente, así que re-traer un par de productos no molesta.
_WATERMARK_MARGIN = timedelta(minutes=2)

# Concurrencia del full refresh de fondo. 1 página = ~400 queries a PG en
# Vendure; 2 en paralelo es el punto donde el admin sigue respondiendo.
_FULL_REFRESH_CONCURRENCY = 2


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _max_updated_at(products: list[VendureProduct], current: str | None = None) -> str | None:
    best_dt = _parse_iso(current) if current else None
    best = current if best_dt else None
    for p in products:
        if not p.updated_at:
            continue
        dt = _parse_iso(p.updated_at)
        if dt and (best_dt is None or dt > best_dt):
            best_dt, best = dt, p.updated_at
    return best


def _since_for_incremental(watermark: str) -> str | None:
    dt = _parse_iso(watermark)
    if dt is None:
        return None
    return (dt - _WATERMARK_MARGIN).isoformat().replace("+00:00", "Z")


async def _fetch_all(client: VendureClient) -> list[VendureProduct]:
    # Páginas de 100 en paralelo (ver VendureClient.fetch_all_products).
    return await client.fetch_all_products(
        with_variants=False, concurrency=_FULL_REFRESH_CONCURRENCY,
    )


def _full_interval() -> float:
    return float(max(60, getattr(get_settings(), "catalog_full_refresh_seconds", 43200)))


def _needs_full() -> bool:
    if not _cache or not _watermark or _full_at is None:
        return True
    return (time.monotonic() - _full_at) >= _full_interval()


async def _do_full(client: VendureClient) -> None:
    global _cache, _loaded_at, _full_at, _watermark
    products = await _fetch_all(client)
    _cache = products
    _watermark = _max_updated_at(products)
    _full_at = _loaded_at = time.monotonic()
    log.info(
        "Catálogo Vendure cacheado (full): %d productos (watermark %s)",
        len(products), _watermark,
    )


async def _do_incremental(client: VendureClient) -> None:
    """Mergea productos cambiados desde el watermark. Cae a full si el conteo
    de Vendure no coincide con el cache (borrados) o si el watermark no sirve."""
    global _cache, _loaded_at, _watermark
    since = _since_for_incremental(_watermark or "")
    if since is None:
        await _do_full(client)
        return

    changed = await client.fetch_products_updated_since(since, with_variants=False)
    total = await client.count_products()

    if changed:
        by_id = {p.id: p for p in _cache}
        for p in changed:
            by_id[p.id] = p
        # Preservar el orden original; los nuevos van al final.
        merged = [by_id[p.id] for p in _cache]
        seen = {p.id for p in _cache}
        merged.extend(p for p in changed if p.id not in seen)
        _cache = merged
        _watermark = _max_updated_at(changed, _watermark)

    if total != len(_cache):
        log.info(
            "Catálogo: Vendure reporta %d productos y el cache tiene %d — full refresh",
            total, len(_cache),
        )
        await _do_full(client)
        return

    _loaded_at = time.monotonic()
    log.info(
        "Catálogo Vendure refrescado (incremental): %d cambiados, %d en cache",
        len(changed), len(_cache),
    )


async def _refresh(client: VendureClient) -> None:
    if _needs_full():
        await _do_full(client)
        return
    try:
        await _do_incremental(client)
    except TransportQueryError as exc:
        # Vendure respondió pero rechazó la query (schema distinto, filtro no
        # soportado…). No es un problema de red: el full sigue funcionando y
        # es mejor pagarlo que quedarse con un cache viejo para siempre.
        log.warning("Refresh incremental rechazado por Vendure (%s); hago full", exc)
        await _do_full(client)


def invalidate() -> None:
    """Fuerza el próximo get_catalog() a re-consultar Vendure.

    Llamar después de deshabilitar/rehabilitar productos, para que /verify no
    compare contra un estado viejo cacheado. (Un producto tocado cambia su
    `updatedAt`, así que el refresh incremental lo trae.)
    """
    global _loaded_at
    _loaded_at = 0.0


async def get_catalog(force: bool = False, full: bool = False) -> list[VendureProduct]:
    """Devuelve el catálogo completo, cacheado por TTL.

    `force=True` refresca aunque el TTL no haya vencido (incremental si se
    puede). `full=True` obliga un full refresh."""
    global _cache, _loaded_at, _full_at
    ttl = get_settings().verify_catalog_ttl_seconds

    def _fresh() -> bool:
        return bool(_cache) and (time.monotonic() - _loaded_at) < ttl

    if not force and not full and _fresh():
        return _cache
    async with _lock:
        # Re-chequear adentro del lock: otro request pudo refrescar mientras esperábamos.
        if not force and not full and _fresh():
            return _cache
        if full:
            _full_at = None
        client = VendureClient()
        try:
            await _refresh(client)
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
        return _cache
