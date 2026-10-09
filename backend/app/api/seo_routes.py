"""Endpoints de la auditoría de textos del catálogo (SEO, HG1).

GET    /api/seo/text-audit/summary        → última corrida, catálogo de reglas
GET    /api/seo/text-audit/items          → tabla paginada con filtros y conteos por regla
GET    /api/seo/text-audit/export.csv     → los mismos filtros, en CSV
POST   /api/seo/text-audit/run            → dispara una corrida (lee Vendure, no escribe)
GET    /api/seo/text-audit/lists          → listas editables (marcas, relleno, datos técnicos)
PUT    /api/seo/text-audit/lists/{name}   → guarda una lista
DELETE /api/seo/text-audit/lists/{name}   → vuelve a la lista de fábrica

Viven bajo /api/ → el middleware de auth exige sesión del dashboard.

Nada de acá escribe en Vendure ni devuelve los campos de proveedor del producto
(supplierBusiness / supplierSizeModel / supplierLink): la regla FAB solo informa
que hubo coincidencia.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.config import get_settings
from app.db.models import TextAuditRun
from app.db.session import get_session
from app.seo import lists as seo_lists
from app.seo import text_audit
from app.seo import text_rules as rules

router = APIRouter(prefix="/api/seo/text-audit", tags=["seo"])

PAGE_SIZE_DEFAULT = 25
PAGE_SIZE_MAX = 200
PAGE_MAX = 10_000
DB_INT_MAX = 2**31 - 1

# El event loop solo guarda referencias débiles a las tasks: sin esto una corrida
# disparada a mano podría ser recolectada a mitad de camino.
_background: set[asyncio.Task] = set()


def _filters(
    rule: str | None = Query(None, description="Identificador de regla"),
    enabled: str = Query("all"),
    lang: str | None = Query(None, max_length=16, pattern=r"^[A-Za-z_\-]+$"),
    channel: str = Query("all"),
    q: str | None = Query(None, max_length=100),
    only_issues: bool = Query(True),
) -> text_audit.Filters:
    if rule is not None and rule not in rules.RULE_IDS:
        raise HTTPException(422, f"Regla desconocida: {rule[:40]!r}")
    if enabled not in text_audit.ENABLED_FILTERS:
        raise HTTPException(422, f"enabled tiene que ser uno de {text_audit.ENABLED_FILTERS}")
    if channel not in text_audit.CHANNEL_FILTERS:
        raise HTTPException(422, f"channel tiene que ser uno de {text_audit.CHANNEL_FILTERS}")
    return text_audit.Filters(
        rule=rule, enabled=enabled, lang=lang or None, channel=channel, q=q, only_issues=only_issues,
    )


def _resolve_run(session: Session, run_id: int | None):
    run = session.get(TextAuditRun, run_id) if run_id else text_audit.latest_run(session)
    if run_id and run is None:
        raise HTTPException(404, "Esa corrida no existe (se conservan las últimas 8).")
    return run


@router.get("/summary")
async def summary(session: Session = Depends(get_session)) -> dict[str, Any]:
    run = text_audit.latest_run(session)
    cron = (get_settings().seo_text_audit_cron_utc or "").strip()
    return {
        "run": text_audit.run_to_dict(run) if run else None,
        "running": text_audit.is_running(session),
        "rules": [
            {"id": r.id, "label": r.label, "group": r.group, "help": r.help} for r in rules.RULES
        ],
        "limits": {"title_max": rules.TITLE_MAX, "meta_max": rules.META_MAX},
        "cron_utc": cron or None,
    }


@router.get("/items")
async def list_items(
    f: text_audit.Filters = Depends(_filters),
    run_id: int | None = Query(None, ge=1, le=DB_INT_MAX),
    page: int = Query(0, ge=0, le=PAGE_MAX),
    page_size: int = Query(PAGE_SIZE_DEFAULT, ge=1, le=PAGE_SIZE_MAX),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    run = _resolve_run(session, run_id)
    if run is None:
        return {"run_id": None, "total": 0, "items": [], "counts": {}, "facet_products": 0,
                "languages": [], "page": page, "page_size": page_size}
    data = text_audit.query_items(session, int(run.id), f, page, page_size)  # type: ignore[arg-type]
    return {"run_id": run.id, "page": page, "page_size": page_size, **data}


@router.get("/export.csv")
async def export_csv(
    f: text_audit.Filters = Depends(_filters),
    run_id: int | None = Query(None, ge=1, le=DB_INT_MAX),
    session: Session = Depends(get_session),
) -> Response:
    run = _resolve_run(session, run_id)
    if run is None:
        raise HTTPException(404, "Todavía no hay ninguna corrida de la auditoría.")
    body = text_audit.export_csv(session, int(run.id), f)  # type: ignore[arg-type]
    stamp = run.started_at.strftime("%Y%m%d-%H%M")
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="auditoria-textos-{stamp}.csv"'},
    )


@router.post("/run", status_code=202)
async def run_now(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Dispara una corrida en background. Solo lee Vendure. 409 si ya hay una en
    curso; 429 si la última arrancó hace menos de un minuto."""
    from app.scheduler import jobs  # import tardío: jobs arrastra todo el scheduler

    if text_audit.is_running(session):
        raise HTTPException(409, "Ya hay una auditoría de textos en curso.")
    since = text_audit.seconds_since_last_start(session)
    if since is not None and since < text_audit.MANUAL_COOLDOWN_S:
        wait = int(text_audit.MANUAL_COOLDOWN_S - since) + 1
        raise HTTPException(429, f"La última corrida arrancó hace segundos. Probá de nuevo en {wait} s.",
                            headers={"Retry-After": str(wait)})
    task = asyncio.create_task(jobs.seo_text_audit(trigger="manual"))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"status": "scheduled", "read_only": True}


# ─── Listas editables ──────────────────────────────────────────────

class ListBody(BaseModel):
    items: list[str] = Field(max_length=seo_lists.MAX_ITEMS + 50)


def _check_list_name(name: str) -> None:
    if name not in seo_lists.LIST_NAMES:
        raise HTTPException(404, f"No existe la lista «{name[:30]}».")


@router.get("/lists")
async def get_lists(session: Session = Depends(get_session)) -> dict[str, Any]:
    return {"lists": seo_lists.current(session), "max_items": seo_lists.MAX_ITEMS,
            "max_item_len": seo_lists.MAX_ITEM_LEN}


@router.put("/lists/{name}")
async def put_list(
    body: ListBody,
    name: str = Path(..., max_length=20),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    _check_list_name(name)
    try:
        items = seo_lists.save(session, name, body.items)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"name": name, "items": items, "modified": True}


@router.delete("/lists/{name}")
async def reset_list(
    name: str = Path(..., max_length=20),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    _check_list_name(name)
    return {"name": name, "items": seo_lists.reset(session, name), "modified": False}
