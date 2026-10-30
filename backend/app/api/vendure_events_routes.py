"""Avisos de Vendure al guardar (contrato `vendure-hugo-eventos-contrato.md`, lado Hugo).

POST /api/vendure/events → `{"events": [{"type": "product|variant|tier|price", "productId": "123", "variantId": "456",
                            "at": "2026-10-30T12:00:00Z"}]}` (hasta 500) → 202 `{"queued": N}`

Vendure avisa cuando se guarda un producto, una variante, un tramo o termina de recalcular un precio de venta. Hugo anota
cada producto (sin repetir) en la cola del recálculo y lo lee a los ~20 s; un aviso de tramo o de variante que dio «sin
cambio» se relee una vez a los ~2 min (pricing/price_recalc.py: mismos topes, locks y reglas). El sondeo cada 4,5 min y el
vigía de 30 min quedan de respaldo.

Autenticación PROPIA: el header `X-Hugo-Events-Key` contra VENDURE_EVENTS_KEY, comparado en tiempo constante. No usa la
cookie del dashboard (está en las rutas públicas del middleware, auth.py). Sin la variable, o con una key floja, contesta
503: falla cerrado. Defensas como las de /api/oficina: rate limit por IP, bloqueo temporal tras varios intentos con key
mala, tope de body y validación estricta (Pydantic, `extra="forbid"`, ids solo dígitos).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.api.oficina_routes import _IpTable, _ip_key, _key_matches, _read_body, _window
from app.config import get_settings
from app.pricing import price_recalc
from app.security import client_ip, weak_key_reason

log = logging.getLogger(__name__)

HEADER = "x-hugo-events-key"
MAX_EVENTS = 500
MAX_BODY_BYTES = 256 * 1024          # 500 avisos son ~60 KB
MIN_KEY_LEN = 32
# Vendure junta los avisos ~5 s por producto y manda un POST por lote: 120 por minuto sobra.
RATE_MAX = 120
RATE_WINDOW_S = 60.0
FAIL_MAX = 10
FAIL_WINDOW_S = 300.0
LOCK_S = 300.0

_hits = _IpTable(deque)
_fails = _IpTable(deque)
_locked_until = _IpTable()
_weak_logged: set[str] = set()


def reset_limits() -> None:
    """Para los tests."""
    for table in (_hits, _fails, _locked_until):
        table.clear()
        table.last_purge = 0.0


def configured_key() -> str | None:
    """VENDURE_EVENTS_KEY si es una key de verdad (>= 32 caracteres, >= 12 distintos, no un placeholder); si no, None y se
    avisa una vez."""
    key = (get_settings().vendure_events_key or "").strip()
    if not key:
        return None
    reason = weak_key_reason(key, min_distinct=12)
    if reason is None and len(key) < MIN_KEY_LEN:
        reason = f"tiene menos de {MIN_KEY_LEN} caracteres"
    if reason:
        if reason not in _weak_logged:
            _weak_logged.add(reason)
            log.error("VENDURE_EVENTS_KEY %s: los avisos de Vendure quedan apagados", reason)
        return None
    return key


def guard(request: Request) -> None:
    """Sin key configurada (o floja): 503. Key mala, vacía o ausente: 401 y, tras varios intentos, 429 para esa IP. Con la
    key correcta, rate limit por IP. El bloqueo frena solo FALLOS: nunca a quien trae la key buena."""
    configured = configured_key()
    if configured is None:
        raise HTTPException(status_code=503, detail="Avisos de Vendure apagados (falta VENDURE_EVENTS_KEY)")
    ip = _ip_key(client_ip(request))
    now = time.monotonic()
    if not _key_matches(request.headers.get(HEADER), configured):
        if _locked_until.get(ip, 0.0) > now:
            raise HTTPException(status_code=429, detail="Demasiados intentos con una key inválida",
                                headers={"Retry-After": str(int(_locked_until[ip] - now) + 1)})
        fails = _window(_fails, ip, FAIL_WINDOW_S, now)
        fails.append(now)
        if len(fails) >= FAIL_MAX:
            _locked_until[ip] = now + LOCK_S
            _locked_until.touch(ip)
            fails.clear()
            log.warning("vendure events: IP %s bloqueada %d s por %d intentos con key inválida", ip, int(LOCK_S), FAIL_MAX)
        raise HTTPException(status_code=401, detail="Key inválida o ausente")
    hits = _window(_hits, ip, RATE_WINDOW_S, now)
    if len(hits) >= RATE_MAX:
        raise HTTPException(status_code=429, detail="Demasiados pedidos",
                            headers={"Retry-After": str(int(hits[0] + RATE_WINDOW_S - now) + 1)})
    hits.append(now)


# La auth vale para TODA ruta del router.
router = APIRouter(prefix="/api/vendure", tags=["vendure-events"], dependencies=[Depends(guard)])

_ID = r"^[0-9]{1,20}$"


class VendureEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["product", "variant", "tier", "price"]
    productId: str = Field(pattern=_ID)  # noqa: N815  (el nombre es el del contrato)
    variantId: str | None = Field(default=None, pattern=_ID)  # noqa: N815
    at: datetime | None = None


class EventsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[VendureEvent] = Field(max_length=MAX_EVENTS)


@router.post("/events", status_code=202)
async def vendure_events(request: Request) -> dict[str, Any]:
    """El body se lee DESPUÉS de la auth (dependencia del router) y con tope; inválido = 422."""
    raw = await _read_body(request, MAX_BODY_BYTES)
    try:
        body = EventsIn.model_validate_json(raw)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_input=False)) from None
    changes: dict[str, bool] = {}
    for ev in body.events:
        changes[ev.productId] = changes.get(ev.productId, False) or ev.type in ("tier", "variant")
    queued = price_recalc.notify(changes)
    log.info("vendure events: %d avisos, %d productos anotados (%s)", len(body.events), queued, client_ip(request))
    return {"queued": queued}
