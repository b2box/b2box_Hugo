"""Registro del estimado: fila en `lookup_estimate` + AuditLog `app_lookup_estimated`.

Una fila por consulta que pidió estimado, salga como salga (ER-10). Sin IP ni
mail en claro: solo `ip_hash` (sha256 con sal). El AuditLog va solo cuando se
mostró un rango, con el texto que vio el cliente (sin yuanes ni proveedores).
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import delete
from sqlmodel import Session

from app.clock import utcnow
from app.db.models import AuditLog, LookupEstimate
from app.db.session import engine
from app.upload_urls import redact, redact_text

log = logging.getLogger(__name__)


def _json(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:20_000]
    except (TypeError, ValueError):
        return None


def save(row: dict[str, Any], audit: dict[str, Any] | None = None) -> None:
    """Guarda la fila (y el AuditLog si viene). Nunca lanza: un fallo de base no
    le cambia la respuesta al cliente."""
    try:
        with Session(engine) as s:
            s.add(LookupEstimate(
                estimate_id=row.get("estimate_id"),
                source=(row.get("source") or "")[:40],
                input_kind=(row.get("input_kind") or "link")[:16],
                marketplace=(row.get("marketplace") or "other")[:24],
                image_sha256=row.get("image_sha256"),
                ip_hash=row.get("ip_hash"),
                estimate_status=row["estimate_status"],
                reason=(row.get("reason") or None) and str(row["reason"])[:200],
                deadline_hit=row.get("deadline_hit"),
                total_ms=int(row.get("total_ms") or 0),
                timings=_json(row.get("timings")),
                apify_launched=bool(row.get("apify_launched")),
                apify_status=row.get("apify_status"),
                apify_run_ids=_json(row.get("apify_run_ids")),
                apify_results=int(row.get("apify_results") or 0),
                apify_usd=float(row.get("apify_usd") or 0.0),
                ended_found=bool(row.get("ended_found")),
                candidates=_json(row.get("candidates")),
                qwen_used=bool(row.get("qwen_used")),
                price_factor_k=row.get("price_factor_k"),
                margin_pct=row.get("margin_pct"),
                box_source=row.get("box_source"),
                box_sample_size=row.get("box_sample_size"),
                tax_classification_code=row.get("tax_classification_code"),
                tax_classification_share=row.get("tax_classification_share"),
                profile_version=row.get("profile_version"),
                vendure_scenarios=_json(row.get("vendure_scenarios")),
                tiers=_json(row.get("tiers")),
                currency=row.get("currency"),
                cost_usd=float(row.get("cost_usd") or 0.0),
            ))
            if audit is not None:
                s.add(AuditLog(
                    action="app_lookup_estimated",
                    source=(row.get("source") or "b2box-app")[:40],
                    product_id="(estimado)",
                    detail=(redact_text(str(audit.get("detail") or "")) or "")[:500],
                    product_name=(audit.get("product_name") or "")[:200] or None,
                    product_image_url=redact(audit.get("image_url")),
                    product_source_url=redact(audit.get("source_url")),
                ))
            s.commit()
    except Exception:  # noqa: BLE001
        log.exception("No se pudo guardar el registro del estimado")


def prune(retention_days: int) -> int:
    """Borra filas más viejas que `retention_days` (0 = no borra)."""
    if retention_days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=retention_days)
    with Session(engine) as s:
        res = s.execute(delete(LookupEstimate).where(LookupEstimate.created_at < cutoff))
        s.commit()
        return int(res.rowcount or 0)
