"""Endpoints del buscador de la oficina (la Mac de la oficina busca en ML y Hugo hace el matching).

GET  /api/oficina/ml-queue?limit=N   → productos a buscar y con qué consultas
POST /api/oficina/ml-results         → lote de resultados por producto (Hugo los re-sanea)

Autenticación PROPIA: el header `x-oficina-key` contra OFICINA_SEARCH_KEY, comparado en tiempo
constante. No usan la cookie del dashboard (están en las rutas públicas del middleware, auth.py).
Sin la variable (o con una key floja) los dos endpoints contestan 404, como si no existieran.

Defensas: rate limit por IP, bloqueo temporal de la IP tras varios intentos con key mala, tope de body
(512 KB, también sin Content-Length), tope de productos por lote y de la cola. El saneo de cada campo
vive en pricing/oficina_ml.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app import runtime
from app.pricing import oficina_ml
from app.security import client_ip

log = logging.getLogger(__name__)

HEADER = "x-oficina-key"
# Pedidos por IP y por ventana (cola + resultados). La Mac hace un GET y unos pocos POST por noche.
RATE_MAX = 30
RATE_WINDOW_S = 60.0
# Intentos con key mala por IP antes del bloqueo temporal.
FAIL_MAX = 10
FAIL_WINDOW_S = 300.0
LOCK_S = 300.0

_hits: dict[str, deque[float]] = defaultdict(deque)
_fails: dict[str, deque[float]] = defaultdict(deque)
_locked_until: dict[str, float] = {}


def _now() -> float:
    return time.monotonic()


def reset_limits() -> None:
    """Para los tests."""
    _hits.clear()
    _fails.clear()
    _locked_until.clear()


_MAX_TRACKED_IPS = 2000


def _purge(store: dict[str, Any], expired: Callable[[Any], bool]) -> None:
    """Saca de una tabla por IP lo vencido, solo cuando creció de más (nadie tiene 2.000 IPs distintas de la oficina).
    Helper aparte, con nombres propios: la limpieza vive dentro de `guard`, donde `key` es la key configurada."""
    if len(store) > _MAX_TRACKED_IPS:
        for stale_ip in [ip for ip, value in store.items() if expired(value)]:
            del store[stale_ip]


def _window(store: dict[str, deque[float]], ip: str, span: float, now: float) -> deque[float]:
    _purge(store, lambda hits: not hits or hits[-1] <= now - span)
    hits = store[ip]
    while hits and hits[0] <= now - span:
        hits.popleft()
    return hits


def _key_matches(presented: str | None, key: str) -> bool:
    """Compara en tiempo constante (se comparan los hashes: no filtra ni el largo)."""
    given = hashlib.sha256((presented or "").encode("utf-8", "replace")).digest()
    return hmac.compare_digest(given, hashlib.sha256(key.encode("utf-8")).digest()) and bool(presented)


def guard(request: Request) -> None:
    """Dependency del router: 404 sin key configurada; con la key CORRECTA entra (salvo el rate limit); con una key
    mala o ausente, 401 y, tras varios intentos, 429 para esa IP.

    El bloqueo por IP frena FALLOS, nunca a quien trae la key buena: detrás de un CDN la IP puede ser la de un borde
    compartido, y si un tercero bloqueara esa IP dejaría sin cola al runner de la oficina."""
    configured = oficina_ml.configured_key()
    if configured is None:
        raise HTTPException(status_code=404, detail="Not Found")
    ip = client_ip(request)
    now = _now()
    if not _key_matches(request.headers.get(HEADER), configured):
        _purge(_locked_until, lambda until: until <= now)
        if _locked_until.get(ip, 0.0) > now:
            raise HTTPException(status_code=429, detail="Demasiados intentos con una key inválida",
                                headers={"Retry-After": str(int(_locked_until[ip] - now) + 1)})
        fails = _window(_fails, ip, FAIL_WINDOW_S, now)
        fails.append(now)
        if len(fails) >= FAIL_MAX:
            _locked_until[ip] = now + LOCK_S
            fails.clear()
            log.warning("oficina: IP %s bloqueada %d s por %d intentos con key inválida", ip, int(LOCK_S), FAIL_MAX)
        raise HTTPException(status_code=401, detail="Key inválida o ausente")
    hits = _window(_hits, ip, RATE_WINDOW_S, now)
    if len(hits) >= RATE_MAX:
        raise HTTPException(status_code=429, detail="Demasiados pedidos",
                            headers={"Retry-After": str(int(hits[0] + RATE_WINDOW_S - now) + 1)})
    hits.append(now)


# La auth vale para TODA ruta del router: una ruta nueva no puede olvidarse de protegerse.
router = APIRouter(prefix="/api/oficina", tags=["oficina"], dependencies=[Depends(guard)])


async def _read_body(request: Request, limit: int) -> bytes:
    """El body con tope, también cuando no declara Content-Length (chunked): se corta al pasarse."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError:
            raise HTTPException(status_code=400, detail="Content-Length inválido") from None
        if size > limit:
            raise HTTPException(status_code=413, detail=f"El body pasa el tope de {limit // 1024} KB")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail=f"El body pasa el tope de {limit // 1024} KB")
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/ml-queue")
async def ml_queue(limit: int = Query(50, ge=1, le=oficina_ml.MAX_QUEUE)) -> dict[str, Any]:
    items = await asyncio.to_thread(oficina_ml.build_queue, limit)
    return {
        "items": items,
        "max_results": int(runtime.get("pm_ml_web_max_results") or 8),
        "ttl_days": int(oficina_ml.ttl().days),
    }


@router.post("/ml-results")
async def ml_results(request: Request) -> dict[str, Any]:
    body = await _read_body(request, oficina_ml.MAX_BODY_BYTES)
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise HTTPException(status_code=400, detail="El body no es un JSON válido") from None
    items = data.get("results") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail='Se espera {"results": [...]}')
    if len(items) > oficina_ml.MAX_PRODUCTS_PER_BATCH:
        raise HTTPException(status_code=413,
                            detail=f"Máximo {oficina_ml.MAX_PRODUCTS_PER_BATCH} productos por lote")
    report = await asyncio.to_thread(oficina_ml.ingest, items)
    log.info("oficina: lote de %d desde %s: %d guardados, %d repetidos, %d rechazados",
             report.received, client_ip(request), report.stored, report.duplicates, len(report.rejected))
    return report.as_dict()
