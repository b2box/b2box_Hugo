"""Endpoints REST de Hugo.

POST /verify           ← Paco/Luis preguntan: "¿este candidato es duplicado?"
POST /audit            ← dispara auditoría completa on-demand
GET  /products/{id}/check  ← chequea un producto específico (precio + duplicado)
GET  /audit-log        ← últimas N acciones (para dashboard)
GET  /health           ← liveness probe
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session, func, select

from app import runtime
from app.clock import utcnow
from app.db.models import AuditLog, PriceHistory
from app.db.session import engine, get_session
from app.dedup.orchestrator import CandidateInput, find_duplicate_in
from app.integrations import paco as paco_integration
from app.pricing.source_check import fetch_source_price
from app.security import verify_api_key, verify_rate_limit
from app.vendure import catalog as vendure_catalog
from app.vendure.client import VendureClient

log = logging.getLogger(__name__)

router = APIRouter()


# ─── Traducción de acciones técnicas a texto humano ─────────────

_ACTION_LABELS: dict[str, dict[str, str]] = {
    "duplicate_disabled": {
        "icon": "duplicate",
        "title": "Duplicado deshabilitado en Vendure",
        "tone": "warning",
    },
    "duplicate_flagged": {
        "icon": "duplicate",
        "title": "Ya existe en el catálogo (no se reenvió a Paco)",
        "tone": "warning",
    },
    "verify_passed_to_paco": {
        "icon": "send",
        "title": "Producto nuevo · enviado a Paco para enriquecer",
        "tone": "info",
    },
    "paco_failed": {
        "icon": "alert",
        "title": "Producto nuevo · pero Paco no respondió",
        "tone": "danger",
    },
    "verify_no_match": {
        "icon": "info",
        "title": "Producto nuevo · sin imagen para mandar a Paco",
        "tone": "muted",
    },
    "price_updated": {
        "icon": "price",
        "title": "Precio actualizado",
        "tone": "info",
    },
    "price_flagged": {
        "icon": "price",
        "title": "Cambio de precio detectado",
        "tone": "warning",
    },
    "no_change": {
        "icon": "check",
        "title": "Sin cambios",
        "tone": "muted",
    },
    "error": {
        "icon": "alert",
        "title": "Error",
        "tone": "danger",
    },
    "quality_issue_found": {
        "icon": "alert",
        "title": "Producto con problemas (revisar/eliminar)",
        "tone": "warning",
    },
    "pa_variant_flagged": {
        "icon": "alert",
        "title": "Variante con nombre 'PA…' detectada",
        "tone": "warning",
    },
    "bx_no_image_flagged": {
        "icon": "alert",
        "title": "Nombre 'BX…' sin imagen (confirmar para deshabilitar)",
        "tone": "danger",
    },
    "bx_no_image_disabled": {
        "icon": "duplicate",
        "title": "Producto 'BX…' sin imagen deshabilitado en Vendure",
        "tone": "warning",
    },
}


def _humanize(entry: AuditLog) -> dict[str, Any]:
    meta = _ACTION_LABELS.get(entry.action, {"icon": "info", "title": entry.action, "tone": "muted"})
    before = json.loads(entry.before) if entry.before else None
    after = json.loads(entry.after) if entry.after else None
    return {
        "id": entry.id,
        "action": entry.action,
        "source": entry.source,
        "title": meta["title"],
        "icon": meta["icon"],
        "tone": meta["tone"],
        "dismissed": entry.dismissed,
        "product": {
            "id": entry.product_id,
            "name": entry.product_name,
            "code": entry.product_code,
            "image_url": entry.product_image_url,
            "source_url": entry.product_source_url,
        },
        "related_product": (
            {
                "id": entry.related_product_id,
                "name": entry.related_product_name,
                "code": entry.related_product_code,
            }
            if entry.related_product_id
            else None
        ),
        "detail": entry.detail,
        "before": before,
        "after": after,
        "confidence": entry.confidence,
        # ISO con "Z" para que el frontend lo interprete inequívocamente como UTC
        "created_at": (entry.created_at.isoformat() + "Z") if entry.created_at else None,
    }


# ─── Definición de las "secciones"/tabs del dashboard ──────────────
# Cada sección filtra el AuditLog por (source, actions). Usado por
# /api/sections y /audit-log.

SECTIONS: dict[str, dict[str, Any]] = {
    "inbox_luis": {
        "label": "Llegan de Luis",
        "source": "luis",
        "actions": None,
    },
    "inbox_orders": {
        "label": "Llegan de Orders",
        # 'orders' = botón HUGO de Forms (→ Paco APP). 'orders-pro' = Requests
        # "Enviar a Paco" / "Re-buscar" (→ Paco PRO). Ambos son "pedidos" → misma tab.
        "source": ["orders", "orders-pro"],
        "actions": None,
    },
    "duplicates": {
        "label": "Duplicados",
        "source": None,
        "actions": ["duplicate_disabled", "duplicate_flagged"],
    },
    "price_changes": {
        "label": "Cambios de precio",
        "source": None,
        "actions": ["price_flagged"],
    },
    "sent_to_paco": {
        "label": "Enviados a Paco",
        "source": None,
        "actions": ["verify_passed_to_paco"],
    },
    "paco_errors": {
        "label": "Errores con Paco",
        "source": None,
        "actions": ["paco_failed"],
    },
    "quality_issues": {
        "label": "Problemas de calidad",
        "source": None,
        "actions": ["quality_issue_found"],
    },
    "quality_no_image": {
        "label": "Sin imagen",
        "source": None,
        "actions": ["quality_issue_found"],
        "detail_contains": "sin imagen",
    },
    "quality_zero_price": {
        "label": "Precio en 0",
        "source": None,
        "actions": ["quality_issue_found"],
        "detail_contains": "precio = 0",
    },
    "pa_variants": {
        "label": "Variantes PA",
        "source": None,
        "actions": ["pa_variant_flagged"],
    },
    "bx_no_image": {
        "label": "BX sin imagen",
        "source": None,
        "actions": ["bx_no_image_flagged", "bx_no_image_disabled"],
    },
    "all": {
        "label": "Todo",
        "source": None,
        "actions": None,
    },
}


def _apply_section_filter(stmt, section_key: str | None):
    """Aplica filtros source/actions de una sección a un statement select(AuditLog)."""
    if not section_key or section_key not in SECTIONS:
        return stmt
    s = SECTIONS[section_key]
    if s["source"]:
        src = s["source"]
        if isinstance(src, (list, tuple, set)):
            stmt = stmt.where(AuditLog.source.in_(list(src)))  # type: ignore[union-attr]
        else:
            stmt = stmt.where(AuditLog.source == src)
    if s["actions"]:
        stmt = stmt.where(AuditLog.action.in_(s["actions"]))  # type: ignore[attr-defined]
    if s.get("detail_contains"):
        stmt = stmt.where(AuditLog.detail.ilike(f"%{s['detail_contains']}%"))  # type: ignore[union-attr]
    return stmt


# ─── DTOs ────────────────────────────────────────────────────────


MAX_IMAGE_URLS = 5


class VerifyRequest(BaseModel):
    name: str = ""
    description: str = ""
    source_url: str | None = None
    image_urls: list[str] = Field(default_factory=list)

    @field_validator("image_urls")
    @classmethod
    def _cap_images(cls, v: list[str]) -> list[str]:
        # Tope defensivo: sin rechazar el request (no rompemos a Luis), nos
        # quedamos con las primeras MAX_IMAGE_URLS válidas para no disparar
        # descargas masivas (costo de red / DoS).
        clean = [u for u in (v or []) if u and u.strip()]
        return clean[:MAX_IMAGE_URLS]
    # De qué sistema viene este verify. Se usa en el dashboard para tabs Y para
    # rutear a la Paco correcta: "b2box-pro"/"admin" → Paco PRO; el resto → Paco APP.
    # Valores típicos: "luis" (default), "orders", "manual", "b2box-pro".
    source: str = "luis"
    # Solo para el flujo PRO: lo reenviamos a Paco PRO para que escriba de vuelta
    # al quotation_item correcto (paco-ingest) cuando NO es duplicado.
    callback_ctx: dict[str, Any] | None = None
    # Specs libres del producto (nombre/desc/colores…) → text_specs de Paco PRO.
    text_specs: str | None = None


# Orígenes que van a Paco PRO (b2box_sourcing) en vez de Paco APP.
_PRO_SOURCES = {"b2box-pro", "admin", "pro", "orders-pro"}


def _is_pro_source(source: str | None) -> bool:
    s = (source or "").strip().lower()
    return s in _PRO_SOURCES


class VerifyResponse(BaseModel):
    is_duplicate: bool
    confidence: float
    matched_by: list[str]
    per_strategy_scores: dict[str, float]
    candidate_id: str | None
    # Si no era duplicado y Hugo le pasó el job a Paco, devolvemos el search_id
    paco_search_id: str | None = None
    paco_status: str | None = None
    paco_error: str | None = None
    # Si ERA duplicado, la data COMPLETA del producto del catálogo que matcheó
    # (fotos, variantes con precio, código BX, descripción). El admin la usa para
    # llenar el quotation_item sin gastar en Paco.
    matched_product: dict[str, Any] | None = None


# ─── Endpoints ───────────────────────────────────────────────────


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "agent": "hugo"}


def _source_already_sent_to_paco(source_url: str | None) -> str | None:
    """¿Ya mandamos este producto (por source_url) a Paco y sigue vigente?

    El dedup de /verify compara contra el catálogo de Vendure. Un producto que
    todavía NO entró a Vendure (lo está enriqueciendo Paco) da "nuevo" en cada
    verify → se re-enviaba a Paco N veces. Este chequeo corta eso: si ya hay un
    `verify_passed_to_paco` NO descartado con el mismo source_url, no reenviamos.

    Devuelve el product_id de la fila existente (o None si no hay).
    """
    if not source_url:
        return None
    try:
        with Session(engine) as session:
            row = session.exec(
                select(AuditLog)
                .where(
                    AuditLog.product_source_url == source_url,
                    AuditLog.action == "verify_passed_to_paco",
                    AuditLog.dismissed.is_not(True),  # type: ignore[union-attr]
                )
                .order_by(AuditLog.created_at.desc())
                .limit(1)
            ).first()
            return row.product_id if row else None
    except Exception:  # noqa: BLE001
        log.exception("Chequeo de idempotencia Paco falló")
        return None


def _record_verify(
    payload: "VerifyRequest",
    verdict,
    *,
    action: str,
    detail: str,
) -> None:
    """Escribe la fila de AuditLog de un verify. Sync (corre en background task)."""
    try:
        valid_imgs = [u for u in (payload.image_urls or []) if u and u.strip()]
        with Session(engine) as session:
            session.add(AuditLog(
                action=action,
                source=payload.source or "luis",
                product_id=verdict.candidate_id or "(nuevo)",
                detail=detail[:500],
                confidence=verdict.confidence,
                product_name=payload.name[:200] if payload.name else None,
                product_image_url=valid_imgs[0] if valid_imgs else None,
                product_source_url=payload.source_url,
            ))
            session.commit()
    except Exception:  # noqa: BLE001
        log.exception("No se pudo registrar AuditLog del verify")


@router.post(
    "/verify",
    response_model=VerifyResponse,
    dependencies=[Depends(verify_api_key), Depends(verify_rate_limit)],
)
async def verify(payload: VerifyRequest) -> VerifyResponse:
    """Llamado por Luis (app) o el admin (pro) cuando aprueban un producto viral.

    Hugo:
      1. Compara contra el catálogo Vendure (URL + imagen + texto).
      2. Si ES duplicado → devuelve la data completa del match (fotos, variantes,
         precio, código BX) para llenar el item sin gastar en Paco.
      3. Si NO es duplicado → lo reenvía a Paco (APP o PRO según `source`) y
         devuelve el `paco_search_id` en la MISMA respuesta.

    Ruteo Paco: `source` decide el destino (ver _PRO_SOURCES). Luis manda
    source="luis" → Paco APP. El admin manda "b2box-pro"/"admin"/"orders-pro"
    → Paco PRO.

    IMPORTANTE: el envío a Paco es SÍNCRONO a propósito — las integraciones
    (send-to-hugo, convert-pro-to-paco) leen `paco_search_id` de esta respuesta
    para guardarlo y evitar reenviar. No lo pases a background sin actualizarlas.

    Idempotencia: si el mismo source_url ya se mandó a Paco y sigue vigente
    (no descartado), NO reenvía — devuelve paco_status="already_sent".
    """
    # Catálogo cacheado (TTL): no re-descargamos todo Vendure en cada verify.
    try:
        existing = await vendure_catalog.get_catalog()
    except Exception as exc:  # noqa: BLE001
        log.warning("verify: no se pudo traer el catálogo de Vendure: %s", exc)
        raise HTTPException(502, f"No se pudo consultar Vendure: {type(exc).__name__}")

    verdict = await find_duplicate_in(
        CandidateInput(
            name=payload.name,
            description=payload.description,
            source_url=payload.source_url,
            image_urls=payload.image_urls,
        ),
        existing,
    )

    response = VerifyResponse(
        is_duplicate=verdict.is_duplicate,
        confidence=verdict.confidence,
        matched_by=list(verdict.matched_by),
        per_strategy_scores=dict(verdict.per_strategy_scores),
        candidate_id=verdict.candidate_id,
    )

    is_pro = _is_pro_source(payload.source)
    valid_imgs = [u for u in (payload.image_urls or []) if u and u.strip()]
    short_name = payload.name[:60] if payload.name else "(sin nombre)"

    # ── DUPLICADO ──────────────────────────────────────────────────
    if verdict.is_duplicate and verdict.candidate_id:
        try:
            response.matched_product = await VendureClient().get_product_full(
                verdict.candidate_id
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("get_product_full(%s) falló: %s", verdict.candidate_id, exc)
            cached = next((p for p in existing if p.id == verdict.candidate_id), None)
            if cached is not None:
                response.matched_product = {
                    "id": cached.id,
                    "name": cached.name,
                    "description": cached.description,
                    "source_url": cached.source_url,
                    "product_code": cached.product_code,
                    "featured_image_url": cached.featured_image_url,
                    "image_urls": cached.image_urls,
                    "first_variant_price_cents": cached.first_variant_price_cents,
                    "variant_count": cached.variant_count,
                }
        _record_verify(
            payload, verdict, action="duplicate_flagged",
            detail=(
                f"'{short_name}' ya existe en Vendure como producto "
                f"#{verdict.candidate_id}. Match por {','.join(verdict.matched_by)} "
                f"(confianza {verdict.confidence:.0%})"
            ),
        )
        return response

    # ── PRODUCTO NUEVO ─────────────────────────────────────────────
    # Idempotencia: si este source_url ya se mandó a Paco y sigue vigente, NO
    # reenviamos (evita duplicar el job cuando el producto aún no entró a Vendure).
    if _source_already_sent_to_paco(payload.source_url) is not None:
        response.paco_status = "already_sent"
        return response

    if not valid_imgs:
        _record_verify(
            payload, verdict, action="verify_no_match",
            detail=f"'{short_name}' es nuevo, pero no llegó imagen para mandarle a Paco",
        )
        return response

    # Reenvío SÍNCRONO a Paco (APP o PRO). Devolvemos el search_id en la respuesta.
    try:
        if is_pro:
            result = await paco_integration.submit_pro(
                valid_imgs[0],
                callback_ctx=payload.callback_ctx,
                text_specs=payload.text_specs or "",
            )
        else:
            result = await paco_integration.submit(valid_imgs[0])
        response.paco_search_id = result.search_id
        response.paco_status = result.status
        _record_verify(
            payload, verdict, action="verify_passed_to_paco",
            detail=(
                f"'{short_name}' es nuevo. Hugo lo envió a Paco "
                f"{'PRO' if is_pro else 'APP'} (search_id={result.search_id})"
            ),
        )
    except paco_integration.PacoError as exc:
        response.paco_error = str(exc)
        log.warning("Paco submit falló: %s", exc)
        _record_verify(
            payload, verdict, action="paco_failed",
            detail=f"'{short_name}' es nuevo, pero Paco no respondió. Error: {str(exc)[:120]}",
        )
    except Exception as exc:  # noqa: BLE001
        response.paco_error = f"{type(exc).__name__}: {exc}"
        log.exception("Paco submit error inesperado")
        _record_verify(
            payload, verdict, action="paco_failed",
            detail=f"'{short_name}' es nuevo, pero Paco falló: {type(exc).__name__}: {exc}"[:120],
        )
    return response


@router.post("/audit")
async def audit_now(
    target: str = "all",
) -> dict[str, str]:
    """Dispara una auditoría on-demand.

    target: "all" (default) | "duplicates" | "prices" | "quality"
    """
    from app.scheduler.jobs import (
        audit_bx_no_image,
        audit_bx_no_image_lock,
        audit_catalog_quality,
        audit_dupes_lock,
        audit_duplicates,
        audit_pa_variants,
        audit_pa_variants_lock,
        audit_prices_lock,
        audit_quality_lock,
        audit_source_prices,
    )
    import asyncio

    wants_prices = target in ("prices", "all")
    wants_dupes = target in ("duplicates", "all")
    wants_quality = target in ("quality", "all")
    wants_pa = target in ("pa_variants", "all")
    wants_bx = target in ("bx_no_image", "all")

    if wants_prices and audit_prices_lock.locked():
        raise HTTPException(409, "Ya hay una auditoría de precios en curso.")
    if wants_dupes and audit_dupes_lock.locked():
        raise HTTPException(409, "Ya hay una auditoría de duplicados en curso.")
    if wants_quality and audit_quality_lock.locked():
        raise HTTPException(409, "Ya hay una auditoría de calidad en curso.")
    if wants_pa and audit_pa_variants_lock.locked():
        raise HTTPException(409, "Ya hay una auditoría de variantes PA en curso.")
    if wants_bx and audit_bx_no_image_lock.locked():
        raise HTTPException(409, "Ya hay una auditoría de BX sin imagen en curso.")

    if wants_dupes:
        asyncio.create_task(audit_duplicates())
    if wants_prices:
        asyncio.create_task(audit_source_prices())
    if wants_quality:
        asyncio.create_task(audit_catalog_quality())
    if wants_pa:
        asyncio.create_task(audit_pa_variants())
    if wants_bx:
        asyncio.create_task(audit_bx_no_image())
    return {"status": "scheduled", "target": target}


@router.get("/products/{product_id}/check")
async def check_product(product_id: str) -> dict[str, Any]:
    """Chequea un producto puntual: trae sus datos y evalúa si su precio está alineado."""
    client = VendureClient()
    prod = await client.get_product(product_id)
    if not prod:
        raise HTTPException(status_code=404, detail="Producto no existe en Vendure")

    payload: dict[str, Any] = {
        "product": {
            "id": prod.id,
            "name": prod.name,
            "source_url": prod.source_url,
            "enabled": prod.enabled,
        },
    }

    if prod.source_url:
        quote = await fetch_source_price(prod.source_url)
        if quote:
            payload["source_price"] = {
                "price_cents": quote.price_cents,
                "currency": quote.currency,
                "usd_equivalent_cents": quote.usd_price_cents,
                "fetched_from": quote.source,
            }
        else:
            payload["source_price"] = {"status": "source_unreachable"}
    else:
        payload["source_price"] = {"status": "skipped", "reason": "sin supplierLink"}

    return payload


def _apply_search(stmt, q: str):
    """Filtra por texto: nombre, código BX o ID de producto (case-insensitive)."""
    like = f"%{q.strip()}%"
    return stmt.where(
        AuditLog.product_name.ilike(like)  # type: ignore[union-attr]
        | AuditLog.product_code.ilike(like)  # type: ignore[union-attr]
        | AuditLog.product_id.ilike(like)  # type: ignore[union-attr]
    )


@router.get("/audit-log")
async def list_audit_log(
    skip: int = 0,
    limit: int = 25,
    section: str | None = None,
    action: str | None = None,
    q: str | None = None,
    include_dismissed: bool = False,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Devuelve eventos paginados.

    Params:
      skip               — offset para paginación
      limit              — page size (max 100)
      section            — filtra por tab
      action             — filtra por action puntual
      q                  — busca por nombre / código BX / ID de producto
      include_dismissed  — por defecto False, ocultar eventos descartados
    """
    limit = max(1, min(100, limit))
    base = select(AuditLog)
    count_q = select(func.count(AuditLog.id))  # type: ignore[arg-type]

    if not include_dismissed:
        # is_not(True) matchea False y NULL (filas previas a la migración)
        base = base.where(AuditLog.dismissed.is_not(True))  # type: ignore[union-attr]
        count_q = count_q.where(AuditLog.dismissed.is_not(True))  # type: ignore[union-attr]
    if section:
        base = _apply_section_filter(base, section)
        count_q = _apply_section_filter(count_q, section)
    if action:
        base = base.where(AuditLog.action == action)
        count_q = count_q.where(AuditLog.action == action)
    if q and q.strip():
        base = _apply_search(base, q)
        count_q = _apply_search(count_q, q)

    total = session.exec(count_q).one() or 0
    # Desempate por id: sin esto, filas con el mismo created_at pueden salir en
    # distinto orden entre requests → el listado se "reordena" y titila en cada
    # auto-refresh. Con (created_at desc, id desc) el orden es determinístico.
    stmt = (
        base.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())  # type: ignore[union-attr]
        .offset(skip)
        .limit(limit)
    )
    items = [_humanize(e) for e in session.exec(stmt)]
    return {
        "items": items,
        "total": total,
        "skip": skip,
        "limit": limit,
        "has_more": skip + limit < total,
    }


# ─── Settings runtime (configurables desde el dashboard) ──────────


class SettingUpdate(BaseModel):
    value: float | int | str


@router.get("/api/settings")
async def list_settings() -> list[dict[str, Any]]:
    """Devuelve todos los settings runtime con su valor actual + metadata."""
    return runtime.get_all_with_meta()


@router.put("/api/settings/{key}")
async def update_setting(key: str, payload: SettingUpdate) -> dict[str, Any]:
    """Actualiza un setting runtime. Persiste en DB y aplica en la próxima lectura."""
    try:
        new_value = runtime.set_value(key, payload.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"key": key, "value": new_value, "ok": True}


@router.delete("/api/settings/{key}")
async def reset_setting(key: str) -> dict[str, Any]:
    """Borra el override y vuelve al default del .env."""
    try:
        new_value = runtime.reset_to_default(key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"key": key, "value": new_value, "ok": True, "reset": True}


@router.post("/api/audit-log/dismiss-section")
async def dismiss_section(
    section: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Archiva (dismiss) en bloque todos los eventos NO descartados de una sección.

    Sirve para que tabs como "Enviados a Paco" o "Llegan de Orders" no se
    acumulen: una vez procesados, se archivan de una. No borra nada de Vendure
    ni de la DB, solo los saca del listado (dismissed=True).
    """
    from sqlalchemy import update as sa_update

    stmt = (
        sa_update(AuditLog)
        .where(AuditLog.dismissed.is_not(True))  # type: ignore[union-attr]
        .values(dismissed=True, dismissed_at=utcnow())
    )
    stmt = _apply_section_filter(stmt, section)
    result = session.execute(stmt)
    session.commit()
    return {"ok": True, "dismissed": int(result.rowcount or 0)}


@router.post("/api/audit-log/{event_id}/dismiss")
async def dismiss_event(
    event_id: int,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Marca un evento como descartado. No vuelve a aparecer en el listado por defecto."""
    entry = session.get(AuditLog, event_id)
    if not entry:
        raise HTTPException(404, "Evento no encontrado")
    entry.dismissed = True
    entry.dismissed_at = utcnow()
    session.add(entry)
    session.commit()
    return {"ok": True, "id": event_id}


@router.post("/api/audit-log/{event_id}/retry-paco")
async def retry_paco(
    event_id: int,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Reintenta enviar un evento a Paco (solo si tiene image_url guardada).

    Crea un nuevo AuditLog con el resultado y descarta el original.
    """
    entry = session.get(AuditLog, event_id)
    if not entry:
        raise HTTPException(404, "Evento no encontrado")
    if not entry.product_image_url:
        raise HTTPException(400, "Este evento no tiene image_url para reintentar")

    # Llamar a Paco
    try:
        result = await paco_integration.submit(entry.product_image_url)
        new_action = "verify_passed_to_paco"
        new_detail = (
            f"Reintento manual exitoso. '{(entry.product_name or '?')[:60]}' "
            f"enviado a Paco (search_id={result.search_id})"
        )
        new_after = json.dumps({"paco_search_id": result.search_id, "retry_of": event_id})
        session.add(AuditLog(
            action=new_action,
            source="manual",
            product_id=entry.product_id,
            detail=new_detail,
            after=new_after,
            product_name=entry.product_name,
            product_code=entry.product_code,
            product_image_url=entry.product_image_url,
            product_source_url=entry.product_source_url,
        ))
        # Marcar el evento original como dismissed (ya se resolvió)
        entry.dismissed = True
        entry.dismissed_at = utcnow()
        session.add(entry)
        session.commit()
        return {"ok": True, "paco_search_id": result.search_id, "paco_status": result.status}
    except paco_integration.PacoError as exc:
        session.add(AuditLog(
            action="paco_failed",
            source="manual",
            product_id=entry.product_id,
            detail=f"Reintento manual falló: {str(exc)[:200]}",
            product_name=entry.product_name,
            product_code=entry.product_code,
            product_image_url=entry.product_image_url,
            product_source_url=entry.product_source_url,
        ))
        session.commit()
        raise HTTPException(502, f"Paco rechazó el reintento: {exc}")


@router.get("/api/duplicates-stats")
async def duplicates_stats(
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Resumen agregado de los duplicados detectados:
    - cuántos hay
    - cuántos disparó cada estrategia (url / image / text)
    - score promedio por estrategia
    - distribución del score (>=0.99, 0.95-0.99, 0.90-0.95, <0.90)
    """
    rows = list(session.exec(
        select(AuditLog).where(
            AuditLog.action.in_(["duplicate_flagged", "duplicate_disabled"])  # type: ignore[attr-defined]
        )
    ))

    total = len(rows)
    by_strategy = {"url": 0, "image": 0, "text": 0}
    score_buckets = {"99-100": 0, "95-99": 0, "90-95": 0, "<90": 0}
    score_sums = {"url": 0.0, "image": 0.0, "text": 0.0}
    score_counts = {"url": 0, "image": 0, "text": 0}

    for r in rows:
        # Bucket por confidence
        c = r.confidence or 0.0
        if c >= 0.99:
            score_buckets["99-100"] += 1
        elif c >= 0.95:
            score_buckets["95-99"] += 1
        elif c >= 0.90:
            score_buckets["90-95"] += 1
        else:
            score_buckets["<90"] += 1

        # Si tiene `after` con scores detallados, los usamos
        if r.after:
            try:
                data = json.loads(r.after)
                for s in (data.get("matched_by") or []):
                    if s in by_strategy:
                        by_strategy[s] += 1
                for k, v in (data.get("per_strategy_scores") or {}).items():
                    if k in score_sums and v is not None:
                        score_sums[k] += float(v)
                        score_counts[k] += 1
                continue
            except (ValueError, TypeError):
                pass
        # Fallback: parsear el detail (eventos viejos)
        detail = (r.detail or "").lower()
        for s in by_strategy:
            if s in detail:
                by_strategy[s] += 1

    avg_score = {
        k: (score_sums[k] / score_counts[k]) if score_counts[k] else None
        for k in score_sums
    }

    return {
        "total_flagged": total,
        "by_strategy": by_strategy,
        "score_buckets": score_buckets,
        "average_score_per_strategy": avg_score,
        "hint": (
            "Si la mayoría tiene score 0.90-0.95, podés subir el threshold a 0.97 "
            "para reducir falsos positivos. Andá a Configuración."
        ),
    }


@router.post("/api/restore-duplicates")
async def restore_duplicates(
    confirm: bool = False,
    safe: bool = True,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Re-habilita productos que Hugo deshabilitó (action='duplicate_disabled').

    Params:
      confirm — sin esto solo hace dry-run, no toca Vendure
      safe    — (default True) verifica antes que el producto esté ACTUALMENTE disabled.
                Si está enabled (porque vos ya lo activaste, o nunca lo deshabilitamos), lo deja.
                Si tiene before.enabled=False registrado (o sea Hugo NUNCA lo deshabilitó porque
                ya estaba off), lo respeta y NO lo toca.
    """
    stmt = select(AuditLog).where(AuditLog.action == "duplicate_disabled")
    rows = list(session.exec(stmt))
    total = len(rows)

    # Construir mapa product_id → before_enabled (si está registrado)
    before_state: dict[str, bool | None] = {}
    for r in rows:
        if not r.product_id or r.product_id == "(nuevo)":
            continue
        before_state.setdefault(r.product_id, None)
        if r.before:
            try:
                data = json.loads(r.before)
                if "enabled" in data:
                    before_state[r.product_id] = bool(data["enabled"])
            except (ValueError, TypeError):
                pass

    unique_ids = sorted(before_state.keys())

    if not confirm:
        # Categorizar para preview
        explicit_was_enabled = [pid for pid, st in before_state.items() if st is True]
        explicit_was_disabled = [pid for pid, st in before_state.items() if st is False]
        unknown_state = [pid for pid, st in before_state.items() if st is None]
        return {
            "would_restore_max": len(unique_ids),
            "explicit_was_enabled": len(explicit_was_enabled),
            "explicit_was_disabled_skip": len(explicit_was_disabled),
            "unknown_state": len(unknown_state),
            "total_disable_events": total,
            "safe_mode": safe,
            "preview_ids": unique_ids[:20],
            "hint": (
                "Pasame ?confirm=true&safe=true para que: "
                "(1) ignore los que YO sé que vos ya tenías disabled, "
                "(2) verifique en Vendure que el producto esté actualmente disabled antes de tocarlo. "
                "Pasame ?confirm=true&safe=false para forzar todos (riesgo: reactivar productos que vos querías off)."
            ),
        }

    client = VendureClient()
    restored: list[str] = []
    skipped_was_disabled: list[str] = []
    skipped_already_enabled: list[str] = []
    failed: list[dict[str, str]] = []

    for pid in unique_ids:
        # 1) Si tenemos registro explícito de que estaba disabled, lo respetamos
        if before_state[pid] is False:
            skipped_was_disabled.append(pid)
            continue

        try:
            # 2) Modo seguro: verificar el estado actual antes de tocar
            if safe:
                current = await client.get_enabled_status(pid)
                if current is True:
                    # Ya está enabled, no hace falta hacer nada
                    skipped_already_enabled.append(pid)
                    continue
                if current is None:
                    failed.append({"product_id": pid, "error": "Producto no existe en Vendure"})
                    continue
            await client.enable_product(pid)
            restored.append(pid)
        except Exception as exc:  # noqa: BLE001
            failed.append({"product_id": pid, "error": f"{type(exc).__name__}: {exc}"[:200]})

    session.add(AuditLog(
        action="duplicates_restored_bulk",
        source="manual",
        product_id="(bulk)",
        detail=(
            f"Restauración masiva (safe={safe}): "
            f"{len(restored)} restaurados, "
            f"{len(skipped_was_disabled)} respetados (estaban disabled antes), "
            f"{len(skipped_already_enabled)} ya estaban enabled, "
            f"{len(failed)} fallaron."
        ),
    ))
    session.commit()

    vendure_catalog.invalidate()  # cambió el enabled de varios productos
    return {
        "restored": len(restored),
        "skipped_was_disabled": len(skipped_was_disabled),
        "skipped_already_enabled": len(skipped_already_enabled),
        "failed": len(failed),
        "failed_details": failed[:10],
    }


@router.post("/api/duplicates/bulk-confirm")
async def bulk_confirm_duplicates(
    min_confidence: float = 0.99,
    confirm: bool = False,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Confirma en bloque los duplicados flagged con confianza >= min_confidence.

    Sin `confirm=true` es un dry-run (solo dice cuántos tocaría). Con confirm=true,
    deshabilita cada uno en Vendure (guardando estado previo para revertir) y los
    archiva. Sirve para limpiar el backlog de duplicados de alta confianza rápido.
    """
    stmt = select(AuditLog).where(
        AuditLog.action == "duplicate_flagged",
        AuditLog.dismissed.is_not(True),  # type: ignore[union-attr]
        AuditLog.confidence >= min_confidence,  # type: ignore[operator]
    )
    rows = [r for r in session.exec(stmt) if r.product_id and r.product_id != "(nuevo)"]

    if not confirm:
        return {
            "would_disable": len(rows),
            "min_confidence": min_confidence,
            "preview_ids": [r.product_id for r in rows[:20]],
            "hint": "Pasá ?confirm=true&min_confidence=0.99 para deshabilitarlos en Vendure.",
        }

    client = VendureClient()
    disabled: list[str] = []
    skipped_already_disabled: list[str] = []
    failed: list[dict[str, str]] = []

    for entry in rows:
        try:
            previous = await client.get_enabled_status(entry.product_id)
        except Exception as exc:  # noqa: BLE001
            failed.append({"product_id": entry.product_id, "error": f"{type(exc).__name__}: {exc}"[:150]})
            continue
        if previous is None:
            failed.append({"product_id": entry.product_id, "error": "No existe en Vendure"})
            continue
        if previous is False:
            entry.dismissed = True
            entry.dismissed_at = utcnow()
            session.add(entry)
            skipped_already_disabled.append(entry.product_id)
            continue
        try:
            await client.disable_product(entry.product_id)
        except Exception as exc:  # noqa: BLE001
            failed.append({"product_id": entry.product_id, "error": f"{type(exc).__name__}: {exc}"[:150]})
            continue
        session.add(AuditLog(
            action="duplicate_disabled",
            source="manual",
            product_id=entry.product_id,
            related_product_id=entry.related_product_id,
            confidence=entry.confidence,
            detail=(
                f"Confirmado en bloque (confianza >= {min_confidence:.0%}) como duplicado de "
                f"#{entry.related_product_id}. Deshabilitado en Vendure."
            ),
            before=json.dumps({"enabled": True}),
            after=json.dumps({"enabled": False}),
            product_name=entry.product_name,
            product_code=entry.product_code,
            product_image_url=entry.product_image_url,
            product_source_url=entry.product_source_url,
            related_product_name=entry.related_product_name,
            related_product_code=entry.related_product_code,
        ))
        entry.dismissed = True
        entry.dismissed_at = utcnow()
        session.add(entry)
        disabled.append(entry.product_id)

    session.commit()
    vendure_catalog.invalidate()
    return {
        "disabled": len(disabled),
        "skipped_already_disabled": len(skipped_already_disabled),
        "failed": len(failed),
        "failed_details": failed[:10],
    }


@router.post("/api/audit-log/{event_id}/confirm-duplicate")
async def confirm_duplicate(
    event_id: int,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Confirma un duplicado flagged: deshabilita el producto en Vendure.

    Antes de tocar, lee el `enabled` actual y lo guarda en `before` para poder
    restaurarlo después si te equivocás.
    """
    entry = session.get(AuditLog, event_id)
    if not entry:
        raise HTTPException(404, "Evento no encontrado")
    if entry.action != "duplicate_flagged":
        raise HTTPException(400, "Solo se puede confirmar eventos de tipo duplicate_flagged")
    if not entry.product_id or entry.product_id == "(nuevo)":
        raise HTTPException(400, "El evento no tiene product_id válido")

    client = VendureClient()
    try:
        previous_enabled = await client.get_enabled_status(entry.product_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"No pude leer estado actual de Vendure: {exc}")

    if previous_enabled is None:
        raise HTTPException(404, "Producto no existe en Vendure")
    if previous_enabled is False:
        # Ya estaba disabled, no hacemos nada (pero descartamos el flag)
        entry.dismissed = True
        entry.dismissed_at = utcnow()
        session.add(entry)
        session.commit()
        return {"ok": True, "action": "no-op", "reason": "Producto ya estaba disabled en Vendure"}

    try:
        await client.disable_product(entry.product_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"No pude deshabilitar en Vendure: {exc}")

    # Crear nuevo evento de tipo duplicate_disabled con tracking del estado previo
    session.add(AuditLog(
        action="duplicate_disabled",
        source="manual",
        product_id=entry.product_id,
        related_product_id=entry.related_product_id,
        confidence=entry.confidence,
        detail=(
            f"Confirmado manualmente como duplicado de #{entry.related_product_id}. "
            f"Deshabilitado en Vendure."
        ),
        before=json.dumps({"enabled": True}),
        after=json.dumps({"enabled": False}),
        product_name=entry.product_name,
        product_code=entry.product_code,
        product_image_url=entry.product_image_url,
        product_source_url=entry.product_source_url,
        related_product_name=entry.related_product_name,
        related_product_code=entry.related_product_code,
    ))
    # El evento original se descarta (ya se actuó sobre él)
    entry.dismissed = True
    entry.dismissed_at = utcnow()
    session.add(entry)
    session.commit()

    vendure_catalog.invalidate()  # el catálogo cacheado ya no refleja este disable
    return {"ok": True, "action": "disabled", "product_id": entry.product_id}


@router.post("/api/audit-log/{event_id}/confirm-disable-bx")
async def confirm_disable_bx(
    event_id: int,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Confirma un bx_no_image_flagged: deshabilita el producto en Vendure.

    Mismo patrón que confirm-duplicate, pero para productos con nombre 'BX…' sin imagen.
    """
    entry = session.get(AuditLog, event_id)
    if not entry:
        raise HTTPException(404, "Evento no encontrado")
    if entry.action != "bx_no_image_flagged":
        raise HTTPException(400, "Solo se puede confirmar eventos de tipo bx_no_image_flagged")
    if not entry.product_id or entry.product_id == "(nuevo)":
        raise HTTPException(400, "El evento no tiene product_id válido")

    client = VendureClient()
    try:
        previous_enabled = await client.get_enabled_status(entry.product_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"No pude leer estado actual de Vendure: {exc}")

    if previous_enabled is None:
        raise HTTPException(404, "Producto no existe en Vendure")
    if previous_enabled is False:
        entry.dismissed = True
        entry.dismissed_at = utcnow()
        session.add(entry)
        session.commit()
        return {"ok": True, "action": "no-op", "reason": "Producto ya estaba disabled en Vendure"}

    try:
        await client.disable_product(entry.product_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"No pude deshabilitar en Vendure: {exc}")

    session.add(AuditLog(
        action="bx_no_image_disabled",
        source="manual",
        product_id=entry.product_id,
        detail=(
            f"Confirmado manualmente: nombre '{entry.product_name}' empezaba por BX y no tenía imagen. "
            "Deshabilitado en Vendure."
        ),
        before=json.dumps({"enabled": True}),
        after=json.dumps({"enabled": False}),
        product_name=entry.product_name,
        product_code=entry.product_code,
        product_image_url=entry.product_image_url,
        product_source_url=entry.product_source_url,
    ))
    entry.dismissed = True
    entry.dismissed_at = utcnow()
    session.add(entry)
    session.commit()

    vendure_catalog.invalidate()
    return {"ok": True, "action": "disabled", "product_id": entry.product_id}


@router.get("/api/products/{product_id}/history")
async def product_history(
    product_id: str,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Timeline completo del producto: todos los eventos de Hugo en orden cronológico."""
    stmt = (
        select(AuditLog)
        .where(AuditLog.product_id == product_id)
        .order_by(AuditLog.created_at.asc())
    )
    events = [_humanize(e) for e in session.exec(stmt)]
    # Estado actual real en Vendure
    try:
        client = VendureClient()
        current_enabled = await client.get_enabled_status(product_id)
        current = {
            "exists_in_vendure": current_enabled is not None,
            "enabled": current_enabled,
        }
    except Exception as exc:  # noqa: BLE001
        current = {"exists_in_vendure": None, "enabled": None, "error": str(exc)[:200]}

    return {
        "product_id": product_id,
        "current_state_in_vendure": current,
        "total_events": len(events),
        "events": events,
    }


@router.get("/api/debug-config")
async def debug_config() -> dict[str, Any]:
    """Diagnóstico: dice qué env vars están seteadas (sin exponer los valores).

    Útil para verificar desde fuera del container que la config está completa
    sin necesidad de entrar a Coolify ni mirar logs.
    """
    from app.config import get_settings
    s = get_settings()

    def _mask(v: str | None) -> dict[str, Any]:
        # No exponemos ningún fragmento del secreto: solo si está seteado y su
        # longitud. Suficiente para diagnosticar config sin filtrar valores.
        if not v:
            return {"set": False, "length": 0}
        return {"set": True, "length": len(v)}

    return {
        "vendure": {
            "api_url": s.vendure_api_url,
            "bearer": _mask(s.vendure_bearer),
            "channel_token": s.vendure_channel_token,
            "user": _mask(s.vendure_user),
            "pass": _mask(s.vendure_pass),
            "source_url_field": s.vendure_source_url_field,
        },
        "rapidapi": {
            "key": _mask(s.rapidapi_key),
            "host": s.otapi_1688_host,
        },
        "hugo_auth": {
            "api_key": _mask(s.hugo_api_key),
        },
        "paco": {
            "url": s.paco_url,
            "api_key": _mask(s.paco_api_key),
            "submit_path": s.paco_submit_path,
            "cf_client_id": _mask(s.paco_cf_client_id),
            "cf_client_secret": _mask(s.paco_cf_client_secret),
        },
        "alerts": {
            "smtp_host": s.alert_smtp_host,
            "smtp_user": _mask(s.alert_smtp_user),
            "smtp_pass": _mask(s.alert_smtp_pass),
            "email_to": s.alert_email_to,
            "webhook_url": s.alert_webhook_url or None,
        },
        "database": {
            "url_starts_with": (s.database_url[:30] + "…") if s.database_url else None,
            "is_postgres": s.database_url.startswith("postgresql"),
        },
    }


@router.get("/api/sections")
async def section_counts(
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Conteos de cada sección/tab del dashboard (excluye descartados).

    Antes: 1 COUNT por sección (13 queries por request, cada 15s). Ahora:
    2 GROUP BY (por action y por source) + 1 LIKE por cada sección con
    `detail_contains`. La mayoría de los conteos salen en memoria.
    """
    not_dismissed = AuditLog.dismissed.is_not(True)  # type: ignore[union-attr]

    # Conteos agregados en una sola pasada cada uno.
    action_counts: dict[str, int] = {
        a: c
        for a, c in session.exec(
            select(AuditLog.action, func.count(AuditLog.id)).where(not_dismissed).group_by(AuditLog.action)  # type: ignore[arg-type]
        )
    }
    source_counts: dict[str, int] = {
        (src or ""): c
        for src, c in session.exec(
            select(AuditLog.source, func.count(AuditLog.id)).where(not_dismissed).group_by(AuditLog.source)  # type: ignore[arg-type]
        )
    }
    total = sum(action_counts.values())

    out: dict[str, Any] = {}
    for key, s in SECTIONS.items():
        if s.get("detail_contains"):
            # Necesita LIKE — una query puntual (solo 2 secciones la usan).
            stmt = select(func.count(AuditLog.id)).where(not_dismissed)  # type: ignore[arg-type]
            stmt = _apply_section_filter(stmt, key)
            count = session.exec(stmt).one() or 0
        elif s["source"]:
            src = s["source"]
            if isinstance(src, (list, tuple, set)):
                count = sum(source_counts.get(x, 0) for x in src)
            else:
                count = source_counts.get(src, 0)
        elif s["actions"]:
            count = sum(action_counts.get(a, 0) for a in s["actions"])
        else:  # "all"
            count = total
        out[key] = {"label": s["label"], "count": count}
    return out


@router.get("/api/health-metrics")
async def health_metrics(
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Salud del sistema: budget OTAPI, tasa de éxito con Paco, últimas auditorías,
    tamaño del cache de imágenes. Para ver el estado de un vistazo."""
    from app.db.models import ImageHashCache, Setting
    from app.pricing.source_check import otapi_budget_status

    def _count(*where) -> int:
        stmt = select(func.count(AuditLog.id))  # type: ignore[arg-type]
        for w in where:
            stmt = stmt.where(w)
        return session.exec(stmt).one() or 0

    passed = _count(AuditLog.action == "verify_passed_to_paco")
    failed = _count(AuditLog.action == "paco_failed")
    total_paco = passed + failed
    paco_rate = (passed / total_paco) if total_paco else None

    last_price = session.exec(
        select(PriceHistory.captured_at).order_by(PriceHistory.captured_at.desc()).limit(1)
    ).first()
    dedup_marker = session.get(Setting, "_meta:last_dedup_updated_at")
    image_hashes_db = session.exec(select(func.count(ImageHashCache.url))).one() or 0  # type: ignore[arg-type]

    from app.dedup.image_hash import _HASH_CACHE  # tamaño del L1 en memoria

    return {
        "otapi_budget": otapi_budget_status(),
        "paco": {
            "passed": passed,
            "failed": failed,
            "success_rate": paco_rate,
        },
        "duplicates": {
            "pending_flagged": _count(
                AuditLog.action == "duplicate_flagged",
                AuditLog.dismissed.is_not(True),  # type: ignore[union-attr]
            ),
            "disabled_total": _count(AuditLog.action == "duplicate_disabled"),
        },
        "quality_pending": _count(
            AuditLog.action == "quality_issue_found",
            AuditLog.dismissed.is_not(True),  # type: ignore[union-attr]
        ),
        "errors_pending": _count(
            AuditLog.action == "error",
            AuditLog.dismissed.is_not(True),  # type: ignore[union-attr]
        ),
        "image_hash_cache": {"in_memory": len(_HASH_CACHE), "persisted": image_hashes_db},
        "last_price_snapshot": (last_price.isoformat() + "Z") if last_price else None,
        "last_dedup_marker": dedup_marker.value if dedup_marker else None,
    }


@router.get("/api/status")
async def dashboard_status(
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Resumen agregado para el dashboard: conteos, último audit y eventos recientes."""
    now = utcnow()
    last_24h = now - timedelta(hours=24)
    last_7d = now - timedelta(days=7)

    # Conteos
    products_tracked = session.exec(
        select(func.count(func.distinct(PriceHistory.product_id)))  # type: ignore[arg-type]
    ).one() or 0
    snapshots_total = session.exec(select(func.count(PriceHistory.id))).one() or 0  # type: ignore[arg-type]
    alerts_24h = session.exec(
        select(func.count(AuditLog.id)).where(  # type: ignore[arg-type]
            AuditLog.action == "price_flagged",
            AuditLog.created_at >= last_24h,
        )
    ).one() or 0
    duplicates_7d = session.exec(
        select(func.count(AuditLog.id)).where(  # type: ignore[arg-type]
            AuditLog.action == "duplicate_disabled",
            AuditLog.created_at >= last_7d,
        )
    ).one() or 0
    last_audit = session.exec(
        select(PriceHistory.captured_at).order_by(PriceHistory.captured_at.desc()).limit(1)
    ).first()

    # Estado actual de los locks para que el dashboard muestre "trabajando"
    from app.scheduler.jobs import audit_dupes_lock, audit_prices_lock
    in_progress = {
        "prices": audit_prices_lock.locked(),
        "duplicates": audit_dupes_lock.locked(),
    }

    # Últimos 15 eventos para mostrar
    recent = session.exec(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(15)
    )
    events = [_humanize(e) for e in recent]

    return {
        "agent": "Hugo",
        "status": "healthy",
        "now": now.isoformat() + "Z",
        "metrics": {
            "products_tracked": products_tracked,
            "snapshots_total": snapshots_total,
            "alerts_last_24h": alerts_24h,
            "duplicates_last_7d": duplicates_7d,
            "audit_in_progress": in_progress,
        },
        "last_audit": (last_audit.isoformat() + "Z") if last_audit else None,
        "recent_events": events,
    }
