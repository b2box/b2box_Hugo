"""Tareas programadas de Hugo.

Jobs:
  1. audit_duplicates    → recorre Vendure y deshabilita duplicados.
  2. audit_source_prices → snapshot de precios fuente + alerta cuando cambian.
  3. daily_digest        → email/webhook con el resumen de las últimas 24h.
  4. price_monitor       → semáforo de precios contra Mercado Libre y tiendas (sombra).
  5. store_index         → indexa las tiendas (Casa Perfecta, Gadnic…) de madrugada.
  6. seo_text_audit      → audita los textos del catálogo (SEO); solo lee Vendure.

Optimizaciones clave:
  · Streaming  — procesa cada página de Vendure apenas llega.
  · Paralelo   — asyncio.Semaphore(N) para consultar OTAPI a N productos a la vez.
  · Lock       — un asyncio.Lock por job evita que dos auditorías corran a la vez.

Hugo NO modifica el precio de venta de Vendure.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import delete
from sqlmodel import Session, select

from app.clock import utcnow
from app.config import get_settings
from app.db.models import AuditLog, ImageEmbedCache, PriceHistory
from app.db.session import engine
from app.dedup.orchestrator import find_duplicate_pairs
from app.dedup.url_match import normalize_url
from app.notifier.dispatcher import notify, notify_digest
from app.pricing import price_monitor as price_monitor_mod
from app.pricing import store_catalog, store_match
from app.pricing.diff import compare_source_snapshots
from app.pricing.source_check import fetch_source_price
from app.seo import text_audit as text_audit_mod
from app.vendure.client import VendureClient, VendureProduct, VendureVariant

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

# Locks que evitan dos auditorías concurrentes del mismo tipo
audit_prices_lock = asyncio.Lock()
audit_dupes_lock = asyncio.Lock()
audit_quality_lock = asyncio.Lock()
audit_pa_variants_lock = asyncio.Lock()
audit_bx_no_image_lock = asyncio.Lock()

# Cuántos productos consultar a OTAPI a la vez (1688 / RapidAPI tolera bien esto)
PARALLEL_OTAPI = 10


# ─── Helpers ───────────────────────────────────────────────────────


async def _iter_product_pages(
    client: VendureClient, page_size: int = 100,
) -> AsyncIterator[list[VendureProduct]]:
    """Itera por páginas de productos. Hace streaming: yield apenas llega cada página.

    Páginas de 100 (antes 25) → ~4x menos round-trips a Vendure.
    """
    skip = 0
    while True:
        page = await client.list_products(skip=skip, take=page_size)
        if not page:
            return
        yield page
        if len(page) < page_size:
            return
        skip += page_size


async def _flatten_products(client: VendureClient) -> list[VendureProduct]:
    """Para casos donde necesitamos TODO el catálogo en memoria (ej. duplicados).

    Trae todas las páginas en paralelo (ver VendureClient.fetch_all_products).
    """
    return await client.fetch_all_products(with_variants=False)


def _audit_already_logged(
    session: Session,
    action: str,
    product_id: str | None,
    related_product_id: str | None = None,
) -> bool:
    """¿Ya hay una fila de AuditLog para este (action, product_id[, related_product_id])?

    Incluye filas dismissed a propósito: si el usuario ya descartó el alerta,
    no re-creamos otra en la próxima corrida — eso justamente fue el bug que
    inflaba la tabla cada vez que corría el scheduler.
    """
    if product_id is None:
        return False
    stmt = select(AuditLog.id).where(
        AuditLog.action == action,
        AuditLog.product_id == product_id,
    )
    if related_product_id is not None:
        stmt = stmt.where(AuditLog.related_product_id == related_product_id)
    return session.exec(stmt.limit(1)).first() is not None


# ─── Job 1: duplicados ────────────────────────────────────────────


def _get_meta(key: str) -> str | None:
    """Lee un valor kv de la tabla settings (para marcadores internos de Hugo)."""
    from app.db.models import Setting

    with Session(engine) as session:
        row = session.get(Setting, key)
        return row.value if row else None


def _set_meta(key: str, value: str) -> None:
    from app.db.models import Setting

    with Session(engine) as session:
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value
            row.updated_at = utcnow()
            session.add(row)
        session.commit()


_DEDUP_MARKER_KEY = "_meta:last_dedup_updated_at"

# ── Reloj persistente de las auditorías ───────────────────────────
# IntervalTrigger arranca a contar desde que se registra el job, o sea desde
# cada arranque del proceso. Con AUDIT_INTERVAL_HOURS=336 (14 días) y un
# redeploy cada tanto, las auditorías NO corrían nunca: el reloj se reiniciaba
# antes de llegar. Por eso cada job deja en `settings` cuándo terminó su última
# corrida y register_jobs() calcula la próxima a partir de eso.
_LAST_RUN_PREFIX = "_meta:last_run:"
# Si al arrancar la próxima corrida ya venció, no se dispara en el mismo
# segundo en que levanta el proceso (warm del catálogo, índice CLIP, etc.):
# se le da este margen.
_STARTUP_GRACE = timedelta(minutes=5)


def _mark_job_run(job_id: str) -> None:
    """Guarda que `job_id` acaba de terminar una corrida (UTC, ISO-8601).

    Se llama al final del camino feliz del job. Si el job revienta no se marca:
    la próxima vez que arranque el proceso la corrida pendiente vuelve a
    quedar programada (con _STARTUP_GRACE) en vez de perderse 14 días.
    """
    try:
        _set_meta(f"{_LAST_RUN_PREFIX}{job_id}", datetime.now(timezone.utc).isoformat())
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo guardar la última corrida de %s: %s", job_id, exc)


def _last_job_run(job_id: str) -> datetime | None:
    """Cuándo terminó la última corrida de `job_id`, o None si nunca / no se puede leer."""
    try:
        raw = _get_meta(f"{_LAST_RUN_PREFIX}{job_id}")
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo leer la última corrida de %s: %s", job_id, exc)
        return None
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _interval_next_run(last_run: datetime | None, interval: timedelta, now: datetime) -> datetime:
    """Próxima corrida de un job de intervalo respetando el reloj persistido.

    - Sin marcador (primera vez): now + interval, igual que IntervalTrigger.
    - Con marcador: last_run + interval, pero nunca antes de now + grace (si
      ya venció, corre apenas el proceso termine de levantar).
    - Marcador en el FUTURO (skew de reloj, edición manual de la fila): se toma
      como "ahora". Si no, una fecha mal escrita postergaba la auditoría más
      allá de un intervalo entero, sin que nadie lo notara.
    """
    if last_run is None:
        return now + interval
    if last_run > now:
        log.warning(
            "Última corrida registrada en el futuro (%s > %s); la tomo como ahora",
            last_run.isoformat(timespec="minutes"), now.isoformat(timespec="minutes"),
        )
        last_run = now
    return max(last_run + interval, now + _STARTUP_GRACE)


def _auto_archive_sent_to_paco(products: list[VendureProduct]) -> int:
    """Archiva los eventos 'verify_passed_to_paco' cuyo producto YA entró a Vendure.

    Cuando Paco termina de enriquecer un producto, este aparece en el catálogo.
    El evento "enviado a Paco" ya cumplió su función → lo descartamos para que la
    tab no se acumule. Match por source_url normalizado.
    """
    catalog_urls = {
        normalize_url(p.source_url) for p in products if p.source_url
    }
    catalog_urls.discard(None)
    if not catalog_urls:
        return 0
    archived = 0
    with Session(engine) as session:
        rows = session.exec(
            select(AuditLog).where(
                AuditLog.action == "verify_passed_to_paco",
                AuditLog.dismissed.is_not(True),  # type: ignore[union-attr]
                AuditLog.product_source_url.is_not(None),  # type: ignore[union-attr]
            )
        )
        for row in rows:
            if normalize_url(row.product_source_url) in catalog_urls:
                row.dismissed = True
                row.dismissed_at = utcnow()
                session.add(row)
                archived += 1
        if archived:
            session.commit()
    if archived:
        log.info("auto-archive: %d eventos 'enviado a Paco' archivados (ya en Vendure)", archived)
    return archived


async def audit_duplicates() -> None:
    if audit_dupes_lock.locked():
        log.warning("audit_duplicates ya está corriendo, ignoro la nueva invocación")
        return
    async with audit_dupes_lock:
        log.info("Iniciando audit_duplicates")
        client = VendureClient()
        products = await _flatten_products(client)
        log.info("Catálogo: %d productos a comparar", len(products))

        # Cierre de loop: archivar los "enviado a Paco" que ya entraron a Vendure.
        _auto_archive_sent_to_paco(products)

        # Dedup incremental: si ya corrimos antes, solo comparamos pares que
        # incluyan un producto nuevo/cambiado (updatedAt > marcador). Los pares
        # viejo-viejo ya se compararon. La 1ra corrida (sin marcador) es full.
        marker = _get_meta(_DEDUP_MARKER_KEY)
        changed_ids: set[str] | None = None
        if marker:
            changed_ids = {
                p.id for p in products
                if not p.updated_at or p.updated_at > marker
            }
            log.info("dedup incremental: %d productos nuevos/cambiados de %d",
                     len(changed_ids), len(products))

        try:
            pairs = await find_duplicate_pairs(products, changed_ids=changed_ids)
        except Exception as exc:  # noqa: BLE001
            log.exception("find_duplicate_pairs falló: %s", exc)
            return

        flagged = 0
        with Session(engine) as session:
            for pair in pairs:
                drop, keep, verdict = pair.drop, pair.keep, pair.verdict
                # Idempotencia: si ya flagueamos este par antes (dismissed o no),
                # no creamos una fila nueva. Sin esto el scheduler infla la tabla.
                if _audit_already_logged(
                    session, "duplicate_flagged", product_id=drop.id, related_product_id=keep.id
                ):
                    continue
                # MODO SOLO-FLAGEAR: NO deshabilitamos en Vendure, solo logueamos.
                # El usuario revisa la lista y decide manualmente cuáles deshabilitar.
                session.add(AuditLog(
                    action="duplicate_flagged",
                    source="audit",
                    product_id=drop.id,
                    related_product_id=keep.id,
                    # Acá SÍ están los dos en Vendure: confirmar apaga al más
                    # nuevo (drop) y conserva al canónico (keep). Explícito para
                    # que el confirm nunca tenga que adivinar.
                    disable_target_id=drop.id,
                    canonical_product_id=keep.id,
                    confidence=verdict.confidence,
                    detail=(
                        f"Posible duplicado de #{keep.id} por {','.join(verdict.matched_by)} "
                        f"(confianza {verdict.confidence:.0%}). Revisalo manualmente."
                    ),
                    after=json.dumps({
                        "per_strategy_scores": verdict.per_strategy_scores,
                        "matched_by": verdict.matched_by,
                    }),
                    product_name=drop.name,
                    product_code=drop.product_code,
                    product_image_url=drop.featured_image_url,
                    product_source_url=drop.source_url,
                    related_product_name=keep.name,
                    related_product_code=keep.product_code,
                ))
                session.commit()
                flagged += 1
        # Marcador para el próximo run incremental: el updatedAt más nuevo visto.
        updated_ats = [p.updated_at for p in products if p.updated_at]
        if updated_ats:
            _set_meta(_DEDUP_MARKER_KEY, max(updated_ats))

        log.info("audit_duplicates terminado: %d pares flagueados de %d candidatos",
                 flagged, len(pairs))
        _mark_job_run("audit_duplicates")


# ─── Job 2: precios fuente (streaming + paralelo) ─────────────────


async def _process_pricing_for_one(
    prod: VendureProduct, semaphore: asyncio.Semaphore,
) -> tuple[str, str | None]:
    """Procesa un producto. Devuelve (status, alert_text|None).

    status ∈ {"processed", "skipped", "failed"}
    """
    async with semaphore:
        if not prod.source_url:
            return ("skipped", None)
        try:
            quote = await fetch_source_price(prod.source_url)
        except Exception as exc:  # noqa: BLE001
            with Session(engine) as session:
                session.add(AuditLog(
                    action="error",
                    source="audit",
                    product_id=prod.id,
                    detail=f"No se pudo consultar la fuente: {type(exc).__name__}: {exc}"[:300],
                    product_name=prod.name,
                    product_code=prod.product_code,
                    product_image_url=prod.featured_image_url,
                ))
                session.commit()
            log.warning("fetch_source_price falló para %s: %s", prod.id, exc)
            return ("failed", None)
        if not quote:
            return ("skipped", None)

        try:
            with Session(engine) as session:
                # 1) snapshot
                session.add(PriceHistory(
                    product_id=prod.id,
                    source=quote.source,
                    price_cents=quote.price_cents,
                    currency=quote.currency,
                    extra=json.dumps({"usd_price_cents": quote.usd_price_cents}),
                ))
                session.commit()

                # 2) snapshot anterior
                stmt = (
                    select(PriceHistory)
                    .where(
                        PriceHistory.product_id == prod.id,
                        PriceHistory.source == quote.source,
                    )
                    .order_by(PriceHistory.captured_at.desc())
                    .offset(1)
                    .limit(1)
                )
                previous = session.exec(stmt).first()

                # 3) decisión
                decision = compare_source_snapshots(
                    current_price_cents=quote.price_cents,
                    current_currency=quote.currency,
                    previous_price_cents=previous.price_cents if previous else None,
                    previous_currency=previous.currency if previous else None,
                )
                if decision.action in ("first_observation", "ok"):
                    return ("processed", None)

                action_label = "price_flagged" if decision.action != "skip_currency" else "error"
                session.add(AuditLog(
                    action=action_label,
                    source="audit",
                    product_id=prod.id,
                    detail=decision.reason,
                    before=json.dumps({"price_cents": decision.previous_price_cents,
                                       "currency": decision.currency}),
                    after=json.dumps({"price_cents": decision.current_price_cents,
                                      "currency": decision.currency}),
                    product_name=prod.name,
                    product_code=prod.product_code,
                    product_image_url=prod.featured_image_url,
                ))
                session.commit()

                if decision.action in ("alert", "alert_critical"):
                    marker = "[!]" if decision.action == "alert" else "[!!]"
                    return ("processed", (
                        f"{marker} [{prod.id}] {prod.name[:50]} "
                        f"{decision.previous_price_cents/100:.2f} → "
                        f"{decision.current_price_cents/100:.2f} {decision.currency} "
                        f"({decision.drift_pct:+.1%})"
                    ))
                return ("processed", None)
        except Exception as exc:  # noqa: BLE001
            log.exception("Error guardando snapshot para %s: %s", prod.id, exc)
            return ("failed", None)


async def audit_source_prices() -> None:
    if audit_prices_lock.locked():
        log.warning("audit_source_prices ya está corriendo, ignoro la nueva invocación")
        return
    async with audit_prices_lock:
        log.info("Iniciando audit_source_prices (streaming + paralelo)")
        client = VendureClient()
        sem = asyncio.Semaphore(PARALLEL_OTAPI)
        tasks: list[asyncio.Task[tuple[str, str | None]]] = []

        # Streaming: arrancamos a procesar cada página apenas llega
        async for page in _iter_product_pages(client):
            for prod in page:
                tasks.append(asyncio.create_task(_process_pricing_for_one(prod, sem)))

        # Esperamos a que todos terminen
        results = await asyncio.gather(*tasks, return_exceptions=True)

        processed = sum(1 for r in results if isinstance(r, tuple) and r[0] == "processed")
        skipped = sum(1 for r in results if isinstance(r, tuple) and r[0] == "skipped")
        failed = sum(
            1 for r in results
            if isinstance(r, Exception) or (isinstance(r, tuple) and r[0] == "failed")
        )
        alerts = [r[1] for r in results if isinstance(r, tuple) and r[1]]

        if alerts:
            body = "Cambios de costo detectados en proveedores:\n\n" + "\n".join(alerts)
            await notify("Cambios de precio en 1688", body)

        log.info(
            "audit_source_prices terminado: %d procesados, %d sin fuente, %d errores, %d alertas",
            processed, skipped, failed, len(alerts),
        )
        _mark_job_run("audit_source_prices")


# ─── Job 3: calidad del catálogo (precio 0, sin imagen, etc.) ─────


def _detect_quality_issues(prod: VendureProduct) -> list[str]:
    """Devuelve la lista de problemas detectados en un producto, o lista vacía."""
    issues: list[str] = []
    if not prod.featured_image_url:
        issues.append("sin imagen")
    if prod.first_variant_price_cents == 0:
        issues.append("precio = 0")
    if prod.first_variant_price_cents is None and prod.variant_count == 0:
        issues.append("sin variantes")
    if not prod.name or len(prod.name.strip()) < 3:
        issues.append("nombre vacío o muy corto")
    if not prod.source_url:
        issues.append("sin link de proveedor")
    return issues


async def audit_catalog_quality() -> None:
    if audit_quality_lock.locked():
        log.warning("audit_catalog_quality ya está corriendo, ignoro")
        return
    async with audit_quality_lock:
        log.info("Iniciando audit_catalog_quality")
        client = VendureClient()
        products = await _flatten_products(client)
        log.info("Catálogo: %d productos a revisar", len(products))

        flagged = 0
        with Session(engine) as session:
            for prod in products:
                if not prod.enabled:
                    continue
                issues = _detect_quality_issues(prod)
                if not issues:
                    continue
                # Idempotencia: si ya hay un quality_issue_found para este producto,
                # no creamos uno nuevo. El usuario corrige o descarta manualmente.
                if _audit_already_logged(session, "quality_issue_found", product_id=prod.id):
                    continue
                price_str = (
                    f"{prod.first_variant_price_cents/100:.2f}"
                    if prod.first_variant_price_cents is not None
                    else "—"
                )
                detail = (
                    f"Producto con problemas: {', '.join(issues)}. "
                    f"Precio actual: {price_str}, imágenes: "
                    f"{'sí' if prod.featured_image_url else 'NO'}"
                )
                session.add(AuditLog(
                    action="quality_issue_found",
                    source="audit",
                    product_id=prod.id,
                    detail=detail[:500],
                    product_name=prod.name,
                    product_code=prod.product_code,
                    product_image_url=prod.featured_image_url,
                    product_source_url=prod.source_url,
                ))
                session.commit()
                flagged += 1

        if flagged:
            await notify(
                f"{flagged} productos con problemas de calidad",
                f"Hugo revisó {len(products)} productos en Vendure y encontró {flagged} con problemas "
                "(sin imagen, precio 0, sin link de proveedor, etc.). "
                "Revisalos en el dashboard → tab 'Problemas de calidad'.",
            )

        log.info(
            "audit_catalog_quality terminado: %d productos revisados, %d flagged",
            len(products), flagged,
        )
        _mark_job_run("audit_catalog_quality")


# ─── Job 4: variantes con nombre "PA…" ───────────────────────────


async def _iter_product_pages_with_variants(
    client: VendureClient, page_size: int = 100,
) -> AsyncIterator[list[VendureProduct]]:
    """Igual que _iter_product_pages pero trae variantes con nombre/SKU."""
    skip = 0
    while True:
        page = await client.list_products_with_variants(skip=skip, take=page_size)
        if not page:
            return
        yield page
        if len(page) < page_size:
            return
        skip += page_size


async def audit_pa_variants() -> None:
    """Detecta productos con variantes cuyo nombre empieza por 'PA'.

    Crea un AuditLog con action='pa_variant_flagged' por cada producto
    que tenga al menos una variante con nombre que empiece por 'PA'.
    """
    if audit_pa_variants_lock.locked():
        log.warning("audit_pa_variants ya está corriendo, ignoro")
        return
    async with audit_pa_variants_lock:
        log.info("Iniciando audit_pa_variants")
        client = VendureClient()
        flagged = 0
        total_products = 0

        with Session(engine) as session:
            async for page in _iter_product_pages_with_variants(client):
                for prod in page:
                    total_products += 1
                    if not prod.enabled or not prod.variants:
                        continue
                    pa_variants = [
                        v for v in prod.variants
                        if v.name.strip().upper().startswith("PA")
                    ]
                    if not pa_variants:
                        continue
                    # Idempotencia: no flagueamos dos veces el mismo producto.
                    if _audit_already_logged(session, "pa_variant_flagged", product_id=prod.id):
                        continue
                    variant_names = ", ".join(v.name for v in pa_variants[:5])
                    extra = f" (y {len(pa_variants) - 5} más)" if len(pa_variants) > 5 else ""
                    session.add(AuditLog(
                        action="pa_variant_flagged",
                        source="audit",
                        product_id=prod.id,
                        detail=(
                            f"Producto con {len(pa_variants)} variante(s) que empiezan por 'PA': "
                            f"{variant_names}{extra}. Revisalo manualmente."
                        )[:500],
                        after=json.dumps({
                            "pa_variants": [
                                {"id": v.id, "name": v.name, "sku": v.sku}
                                for v in pa_variants
                            ],
                        }),
                        product_name=prod.name,
                        product_code=prod.product_code,
                        product_image_url=prod.featured_image_url,
                        product_source_url=prod.source_url,
                    ))
                    session.commit()
                    flagged += 1

        if flagged:
            await notify(
                f"{flagged} productos con variantes 'PA…'",
                f"Hugo revisó {total_products} productos y encontró {flagged} con variantes "
                "cuyo nombre empieza por 'PA'. Revisalos en el dashboard → tab 'Variantes PA'.",
            )

        log.info(
            "audit_pa_variants terminado: %d productos revisados, %d flagged",
            total_products, flagged,
        )
        _mark_job_run("audit_pa_variants")


# ─── Job 5: productos con nombre 'BX…' y sin imagen ───────────────


async def audit_bx_no_image() -> None:
    """Detecta productos cuyo nombre empieza por 'BX' y no tiene imagen.

    Solo flaguea. El usuario revisa en el dashboard y aprieta
    "Confirmar deshabilitar" para que se deshabilite en Vendure.
    """
    if audit_bx_no_image_lock.locked():
        log.warning("audit_bx_no_image ya está corriendo, ignoro")
        return
    async with audit_bx_no_image_lock:
        log.info("Iniciando audit_bx_no_image")
        client = VendureClient()
        flagged = 0
        total_products = 0

        with Session(engine) as session:
            async for page in _iter_product_pages(client):
                for prod in page:
                    total_products += 1
                    if not prod.enabled:
                        continue
                    name = (prod.name or "").strip()
                    if not name.upper().startswith("BX"):
                        continue
                    if prod.featured_image_url:
                        continue
                    # Idempotencia: no re-flagear el mismo producto.
                    if _audit_already_logged(session, "bx_no_image_flagged", product_id=prod.id):
                        continue
                    session.add(AuditLog(
                        action="bx_no_image_flagged",
                        source="audit",
                        product_id=prod.id,
                        detail=(
                            f"Nombre '{name}' empieza con 'BX' y no tiene imagen destacada. "
                            "Confirmá para deshabilitarlo en Vendure."
                        )[:500],
                        product_name=prod.name,
                        product_code=prod.product_code,
                        product_image_url=None,
                        product_source_url=prod.source_url,
                    ))
                    session.commit()
                    flagged += 1

        if flagged:
            await notify(
                f"{flagged} productos 'BX…' sin imagen",
                f"Hugo revisó {total_products} productos y encontró {flagged} con nombre 'BX…' "
                "y sin imagen. Revisalos en el dashboard → tab 'BX sin imagen' y confirmá para "
                "deshabilitarlos en Vendure.",
            )

        log.info(
            "audit_bx_no_image terminado: %d productos revisados, %d flagged",
            total_products, flagged,
        )
        _mark_job_run("audit_bx_no_image")


# ─── Job 6: digest diario ─────────────────────────────────────────


async def refresh_catalog() -> None:
    """Refresca el cache del catálogo Vendure para que /verify nunca lo baje en
    frío (cold-start). Mantiene el cache caliente entre auditorías.

    Es un refresh INCREMENTAL (solo productos con updatedAt nuevo); el full se
    hace solo cada `catalog_full_refresh_seconds`. Ver app/vendure/catalog.py."""
    from app.vendure import catalog as vendure_catalog

    try:
        await vendure_catalog.get_catalog(force=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("refresh_catalog falló: %s", exc)


def _prune_ml_embed_cache() -> int:
    """Poda del cache L2 de CLIP para fotos de Mercado Libre (mlstatic) más
    viejas que `pm_embed_cache_days`. El semáforo embebe miles de fotos de ML
    por noche y la mayoría no se vuelve a ver; las del catálogo propio (otro
    host) se quedan. Devuelve cuántas borró."""
    from app import runtime

    days = int(runtime.get("pm_embed_cache_days") or 0)
    if days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    # DELETE en bloque: el semáforo deja miles de filas por noche y traerlas a
    # memoria para borrarlas de a una sería la parte cara de la poda.
    with Session(engine) as session:
        result = session.execute(
            delete(ImageEmbedCache).where(
                ImageEmbedCache.updated_at < cutoff,  # type: ignore[arg-type]
                ImageEmbedCache.url.like("%mlstatic.com/%"),  # type: ignore[attr-defined]
            )
        )
        session.commit()
    deleted = int(result.rowcount or 0)
    if deleted:
        log.info("prune: %d embeddings de mlstatic más viejos que %d días", deleted, days)
    return deleted


async def prune_price_history() -> None:
    """Borra snapshots de precio más viejos que `price_history_retention_days`.

    Evita que la tabla `price_history` crezca sin techo en Supabase (storage $ +
    queries más lentas). Con 0 días, la retención queda deshabilitada.

    También poda el cache de fotos de ML y el historial del semáforo
    (`market_price_snapshot` y `price_monitor_run`, `price_monitor_retention_days`,
    180 por default): se conserva el último snapshot de cada producto.
    """
    try:
        _prune_ml_embed_cache()
    except Exception as exc:  # noqa: BLE001
        log.warning("prune del cache de fotos de ML falló: %s", exc)
    try:
        price_monitor_mod.prune_snapshots(get_settings().price_monitor_retention_days)
    except Exception as exc:  # noqa: BLE001
        log.warning("prune del historial del semáforo falló: %s", exc)
    try:
        from app import runtime

        store_match.prune(get_settings().price_monitor_retention_days)
        store_match.prune_embed_cache(int(runtime.get("pm_embed_cache_days") or 0))
    except Exception as exc:  # noqa: BLE001
        log.warning("prune de las tiendas falló: %s", exc)
    days = get_settings().price_history_retention_days
    if days <= 0:
        return
    cutoff = utcnow() - timedelta(days=days)
    with Session(engine) as session:
        stmt = select(PriceHistory).where(PriceHistory.captured_at < cutoff)
        rows = list(session.exec(stmt))
        if not rows:
            return
        for r in rows:
            session.delete(r)
        session.commit()
    log.info("prune_price_history: borrados %d snapshots < %s", len(rows), cutoff.date())


# ─── Job 7: semáforo de precios contra Mercado Libre ──────────────

PRICE_MONITOR_JOB_ID = price_monitor_mod.JOB_ID
_PRICE_MONITOR_DEFAULT_CRON = "0 6 * * *"


async def price_monitor(trigger: str = "cron") -> dict | None:
    """Corre el semáforo (ver app/pricing/price_monitor.py) y, si terminó,
    deja el marcador de última corrida. Una corrida `failed` no lo mueve: así
    el próximo arranque la ve pendiente. Lo usan el cron y el botón del
    dashboard (POST /api/price-monitor/run)."""
    result = await price_monitor_mod.run_price_monitor(trigger=trigger)
    if result and result.get("status") in (price_monitor_mod.RUN_OK, price_monitor_mod.RUN_DEGRADED):
        _mark_job_run(PRICE_MONITOR_JOB_ID)
    return result


# ─── Job 8: indexado de las tiendas (Casa Perfecta, Gadnic…) ───────

STORE_INDEX_JOB_ID = store_catalog.JOB_ID
_STORE_INDEX_DEFAULT_CRON = "20 3 * * *"


async def store_index() -> list[dict]:
    """Lee los sitemaps de las tiendas activas y actualiza su catálogo local (ver
    app/pricing/store_catalog.py). Va de madrugada y ANTES del semáforo, que solo
    compara contra lo que quedó indexado. Una tienda que corta (429, red) no frena a las demás."""
    reports = await store_catalog.index_all()
    _mark_job_run(STORE_INDEX_JOB_ID)
    return [{"store": r.store, "status": r.status, "summary": r.summary()} for r in reports]


# ─── Job 9: auditoría de textos del catálogo (SEO, solo lectura) ───

SEO_TEXT_AUDIT_JOB_ID = text_audit_mod.JOB_ID
# Con el nombre del día: en APScheduler el 1 de un cron es martes, no lunes.
_SEO_TEXT_AUDIT_DEFAULT_CRON = "30 7 * * mon"


async def seo_text_audit(trigger: str = "cron") -> dict | None:
    """Corre la auditoría de textos (ver app/seo/text_audit.py): lee Vendure, no
    escribe nada en él. Lo usan el cron y el botón del dashboard
    (POST /api/seo/text-audit/run). Solo una corrida terminada mueve el marcador."""
    result = await text_audit_mod.run_text_audit(trigger=trigger)
    if result and result.get("status") in (text_audit_mod.RUN_OK, text_audit_mod.RUN_DEGRADED):
        _mark_job_run(SEO_TEXT_AUDIT_JOB_ID)
    return result


def _seo_text_audit_trigger(expr: str) -> CronTrigger | None:
    """None = sin corrida programada (SEO_TEXT_AUDIT_CRON_UTC vacío)."""
    if not expr.strip():
        return None
    try:
        return CronTrigger.from_crontab(expr, timezone="UTC")
    except ValueError as exc:
        log.error("SEO_TEXT_AUDIT_CRON_UTC=%r inválido (%s); uso %r",
                  expr, exc, _SEO_TEXT_AUDIT_DEFAULT_CRON)
        return CronTrigger.from_crontab(_SEO_TEXT_AUDIT_DEFAULT_CRON, timezone="UTC")


def _price_monitor_trigger(expr: str) -> CronTrigger:
    try:
        return CronTrigger.from_crontab(expr, timezone="UTC")
    except ValueError as exc:
        log.error("PRICE_MONITOR_CRON_UTC=%r inválido (%s); uso %r", expr, exc, _PRICE_MONITOR_DEFAULT_CRON)
        return CronTrigger.from_crontab(_PRICE_MONITOR_DEFAULT_CRON, timezone="UTC")


def _cron_missed(trigger: CronTrigger, last_run: datetime | None, now: datetime) -> bool:
    """¿El cron tenía que disparar entre la última corrida terminada y ahora?

    Es el reloj persistente de un job con CronTrigger: si el proceso estaba
    caído (o redeployando) justo a las 06:00, APScheduler no la recupera sola.
    Sin marcador (primera vez) NO se recupera nada y se espera al cron: no
    queremos que un deploy a media tarde lance una corrida de horas.
    """
    if last_run is None or last_run >= now:
        return False
    due = trigger.get_next_fire_time(None, last_run + timedelta(seconds=1))
    return due is not None and due <= now


async def daily_digest() -> None:
    cutoff = utcnow() - timedelta(hours=24)
    with Session(engine) as session:
        stmt = (
            select(AuditLog)
            .where(AuditLog.created_at >= cutoff, AuditLog.notified == False)  # noqa: E712
        )
        rows = list(session.exec(stmt))
        if not rows:
            return
        await notify_digest(rows)
        for r in rows:
            r.notified = True
            session.add(r)
        session.commit()


# ─── Registro ─────────────────────────────────────────────────────


_AUDIT_JOBS = (
    ("audit_duplicates", audit_duplicates),
    ("audit_source_prices", audit_source_prices),
    ("audit_catalog_quality", audit_catalog_quality),
    ("audit_pa_variants", audit_pa_variants),
    ("audit_bx_no_image", audit_bx_no_image),
)


def register_jobs() -> None:
    s = get_settings()
    interval = timedelta(hours=s.audit_interval_hours)
    now = datetime.now(timezone.utc)
    for job_id, fn in _AUDIT_JOBS:
        last = _last_job_run(job_id)
        next_run = _interval_next_run(last, interval, now)
        log.info(
            "%s: última corrida %s → próxima %s",
            job_id, last.isoformat(timespec="minutes") if last else "nunca",
            next_run.isoformat(timespec="minutes"),
        )
        scheduler.add_job(
            fn,
            IntervalTrigger(hours=s.audit_interval_hours),
            id=job_id,
            next_run_time=next_run,
            replace_existing=True, coalesce=True, max_instances=1,
        )
    scheduler.add_job(
        daily_digest,
        CronTrigger(hour=9, minute=0),
        id="daily_digest",
        replace_existing=True,
    )
    scheduler.add_job(
        prune_price_history,
        CronTrigger(hour=4, minute=30),
        id="prune_price_history",
        replace_existing=True, coalesce=True, max_instances=1,
    )
    # Semáforo de precios: cron nocturno (UTC). Si quedó una corrida a medias
    # (el proceso se reinició mientras recorría el catálogo), se retoma apenas
    # termine de levantar en vez de esperar a la próxima noche.
    # Si en cambio el cron no disparó porque el proceso estaba caído a esa
    # hora, también se recupera al arrancar (reloj persistente).
    pm_trigger = _price_monitor_trigger(
        getattr(s, "price_monitor_cron_utc", None) or _PRICE_MONITOR_DEFAULT_CRON
    )
    pm_kwargs: dict = {}
    if price_monitor_mod.has_unfinished_run():
        pm_kwargs["next_run_time"] = now + _STARTUP_GRACE
        log.info("price_monitor: hay una corrida sin terminar → se retoma en %s", _STARTUP_GRACE)
    elif _cron_missed(pm_trigger, _last_job_run(PRICE_MONITOR_JOB_ID), now):
        pm_kwargs["next_run_time"] = now + _STARTUP_GRACE
        log.info("price_monitor: se perdió la corrida programada → corre en %s", _STARTUP_GRACE)
    scheduler.add_job(
        price_monitor,
        pm_trigger,
        id=PRICE_MONITOR_JOB_ID,
        replace_existing=True, coalesce=True, max_instances=1,
        **pm_kwargs,
    )
    # Tiendas: indexado de madrugada (UTC), antes del semáforo de las 06:00 UTC.
    try:
        store_trigger = CronTrigger.from_crontab(
            getattr(s, "store_index_cron_utc", None) or _STORE_INDEX_DEFAULT_CRON, timezone="UTC")
    except ValueError as exc:
        log.error("STORE_INDEX_CRON_UTC inválido (%s); uso %r", exc, _STORE_INDEX_DEFAULT_CRON)
        store_trigger = CronTrigger.from_crontab(_STORE_INDEX_DEFAULT_CRON, timezone="UTC")
    scheduler.add_job(
        store_index,
        store_trigger,
        id=STORE_INDEX_JOB_ID,
        replace_existing=True, coalesce=True, max_instances=1,
    )
    # Auditoría de textos del catálogo: semanal, solo lectura. Si el proceso estaba
    # caído a la hora del cron, se recupera al arrancar (reloj persistente).
    seo_trigger = _seo_text_audit_trigger(getattr(s, "seo_text_audit_cron_utc", None) or "")
    if seo_trigger is not None:
        seo_kwargs: dict = {}
        if _cron_missed(seo_trigger, _last_job_run(SEO_TEXT_AUDIT_JOB_ID), now):
            seo_kwargs["next_run_time"] = now + _STARTUP_GRACE
            log.info("seo_text_audit: se perdió la corrida programada → corre en %s", _STARTUP_GRACE)
        scheduler.add_job(
            seo_text_audit,
            seo_trigger,
            id=SEO_TEXT_AUDIT_JOB_ID,
            replace_existing=True, coalesce=True, max_instances=1,
            **seo_kwargs,
        )
    # Mantener el catálogo caliente: refresca un poco antes de que expire el TTL
    # de /verify, para que Luis/admin nunca esperen un cold-fetch. Es barato:
    # el refresh es incremental (ver app/vendure/catalog.py).
    ttl = max(60, s.verify_catalog_ttl_seconds)
    scheduler.add_job(
        refresh_catalog,
        IntervalTrigger(seconds=max(60, ttl - 30)),
        id="refresh_catalog",
        replace_existing=True, coalesce=True, max_instances=1,
    )
