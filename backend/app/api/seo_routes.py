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
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from app import auth
from app.config import get_settings
from app.db.models import AuditLog, TextAuditRun
from app.db.session import engine, get_session
from app.seo import lists as seo_lists
from app.seo import text_audit
from app.seo import text_rules as rules

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/seo/text-audit", tags=["seo"])

# Las rutas con base son `def` (no `async def`): FastAPI las corre en su threadpool y una consulta
# lenta no frena al event loop (dashboard, scheduler). Solo «run» es async: tiene que crear la task.

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
        rule=rule, enabled=enabled, lang=lang or None, channel=channel,
        q=text_audit.clean_query(q) or None, only_issues=only_issues,
    )


def _resolve_run(session: Session, run_id: int | None):
    run = session.get(TextAuditRun, run_id) if run_id else text_audit.latest_run(session)
    if run_id and run is None:
        raise HTTPException(404, "Esa corrida no existe (se conservan las últimas 8).")
    return run


@router.get("/summary")
def summary(session: Session = Depends(get_session)) -> dict[str, Any]:
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
def list_items(
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
def export_csv(
    f: text_audit.Filters = Depends(_filters),
    run_id: int | None = Query(None, ge=1, le=DB_INT_MAX),
    sep: str = Query(text_audit.DEFAULT_CSV_SEP, max_length=3, description="; (Excel es-AR) | , | tab"),
    session: Session = Depends(get_session),
) -> Response:
    """CSV con los mismos filtros que /items. Separador `;` por defecto (Excel en es-AR). Si hay más de
    EXPORT_MAX_ROWS filas se corta y lo dice en `X-Export-Truncated` / `X-Export-Total`."""
    if sep not in text_audit.CSV_SEPARATORS:
        raise HTTPException(422, f"sep tiene que ser uno de {list(text_audit.CSV_SEPARATORS)}")
    run = _resolve_run(session, run_id)
    if run is None:
        raise HTTPException(404, "Todavía no hay ninguna corrida de la auditoría.")
    export = text_audit.export_csv(session, int(run.id), f, sep)  # type: ignore[arg-type]
    stamp = run.started_at.strftime("%Y%m%d-%H%M")
    return Response(
        content=export.text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="auditoria-textos-{stamp}.csv"',
            "X-Export-Total": str(export.total),
            "X-Export-Truncated": "1" if export.truncated else "0",
            "X-Export-Max-Rows": str(text_audit.EXPORT_MAX_ROWS),
        },
    )


def _actor(request: Request) -> str | None:
    return auth.session_username(request.cookies.get(auth.COOKIE_NAME))


def _audit(session: Session, action: str, actor: str | None, detail: str, after: dict | None = None) -> None:
    """Deja anotado quién hizo qué (AuditLog, como el resto de las acciones del dashboard)."""
    session.add(AuditLog(
        action=action, source="seo", product_id="-", detail=detail[:500],
        after=json.dumps(after, ensure_ascii=False) if after else None,
        note=f"por {actor}" if actor else None,
    ))
    session.commit()
    log.info("seo: %s — %s (%s)", action, detail[:200], actor or "?")


def _start_blocker(actor: str | None) -> tuple[int, str, int | None] | None:
    """Por qué no se puede disparar ahora (status, mensaje, Retry-After), o None y queda anotado quién."""
    with Session(engine) as session:
        if text_audit.is_running(session):
            return 409, "Ya hay una auditoría de textos en curso.", None
        since = text_audit.seconds_since_last_start(session)
        if since is not None and since < text_audit.MANUAL_COOLDOWN_S:
            wait = int(text_audit.MANUAL_COOLDOWN_S - since) + 1
            return 429, f"La última corrida arrancó hace segundos. Probá de nuevo en {wait} s.", wait
        _audit(session, "seo_audit_requested", actor, f"{actor or 'alguien'} disparó la auditoría de textos a mano")
    return None


@router.post("/run", status_code=202)
async def run_now(request: Request) -> dict[str, Any]:
    """Dispara una corrida en background. Solo lee Vendure. 409 si ya hay una en
    curso; 429 si la última arrancó hace menos de un minuto."""
    from app.scheduler import jobs  # import tardío: jobs arrastra todo el scheduler

    blocker = await run_in_threadpool(_start_blocker, _actor(request))
    if blocker is not None:
        status, detail, retry_after = blocker
        raise HTTPException(status, detail, headers={"Retry-After": str(retry_after)} if retry_after else None)
    task = asyncio.create_task(jobs.seo_text_audit(trigger="manual"))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"status": "scheduled", "read_only": True}


# ─── Listas editables ──────────────────────────────────────────────

class ListBody(BaseModel):
    # Topes anchos solo para cortar cuerpos enormes antes de validar; el límite real
    # (y su mensaje) lo pone seo_lists.sanitize_items.
    items: list[Annotated[str, StringConstraints(max_length=400)]] = Field(max_length=seo_lists.MAX_ITEMS + 50)
    # Una lista vacía apaga la regla (sin marcas no hay MAR): hay que pedirlo a propósito.
    allow_empty: bool = False


def _check_list_name(name: str) -> None:
    if name not in seo_lists.LIST_NAMES:
        raise HTTPException(404, f"No existe la lista «{name[:30]}».")


@router.get("/lists")
def get_lists(session: Session = Depends(get_session)) -> dict[str, Any]:
    return {"lists": seo_lists.current(session), "max_items": seo_lists.MAX_ITEMS,
            "max_item_len": seo_lists.MAX_ITEM_LEN}


def _list_change_detail(actor: str | None, name: str, before: list[str], after: list[str]) -> tuple[str, dict]:
    old, new = {x.casefold() for x in before}, {x.casefold() for x in after}
    added = [x for x in after if x.casefold() not in old]
    removed = [x for x in before if x.casefold() not in new]
    detail = (f"{actor or 'alguien'} cambió la lista «{name}» de la auditoría de textos: "
              f"{len(before)} → {len(after)} elementos (+{len(added)}, −{len(removed)})")
    return detail, {"lista": name, "total": len(after), "agregados": added[:25], "quitados": removed[:25]}


@router.put("/lists/{name}")
def put_list(
    body: ListBody,
    request: Request,
    name: str = Path(..., max_length=20),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    _check_list_name(name)
    try:
        items = seo_lists.sanitize_items(body.items)
        if not items and not body.allow_empty:
            raise ValueError(
                f"La lista «{name}» quedaría vacía y la regla dejaría de marcar. Si es lo que querés, "
                "mandá allow_empty=true (en el dashboard, el botón pide confirmación)."
            )
        before = seo_lists.current(session)[name]["items"]
        items = seo_lists.save(session, name, items)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    detail, after = _list_change_detail(_actor(request), name, before, items)  # type: ignore[arg-type]
    _audit(session, "seo_list_changed", _actor(request), detail, after)
    return {"name": name, "items": items, "modified": True}


@router.delete("/lists/{name}")
def reset_list(
    request: Request,
    name: str = Path(..., max_length=20),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    _check_list_name(name)
    before = seo_lists.current(session)[name]["items"]
    items = seo_lists.reset(session, name)
    detail, after = _list_change_detail(_actor(request), name, before, items)  # type: ignore[arg-type]
    _audit(session, "seo_list_reset", _actor(request), detail.replace("cambió", "restableció"), after)
    return {"name": name, "items": items, "modified": False}
