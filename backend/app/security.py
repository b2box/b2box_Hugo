"""Auth simple por API key.

Endpoints sensibles (los que Luis/Paco u otros agentes externos consumen)
deben requerir el header `X-API-Key` con el valor de HUGO_API_KEY.

Si HUGO_API_KEY no está seteado en .env, el endpoint queda abierto y
loguea un warning prominente al startup. Esto facilita testing local pero
NO debe usarse en producción.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections import defaultdict, deque

from fastapi import Header, HTTPException, Request, status

from app.config import get_settings

log = logging.getLogger(__name__)


# ─── Rate limit de /verify (sliding window por IP, in-memory) ──────
# /verify baja el catálogo de Vendure y puede pegarle a Paco → es caro. Sin
# límite, un cliente mal configurado (o abuso) puede disparar costo. Ventana
# deslizante simple, suficiente para 1 instancia (multi-instancia → Redis).
_VERIFY_MAX_PER_WINDOW = 120
_VERIFY_WINDOW_SECONDS = 60.0
_verify_hits: dict[str, deque[float]] = defaultdict(deque)


def client_ip(request: Request, hops: int | None = None) -> str:
    """IP del cliente, confiando solo en los proxies configurados.

    Hugo corre detrás del Traefik de Coolify. Cada proxy AGREGA al final de
    X-Forwarded-For la IP de quien le habló, así que las últimas
    `trusted_proxy_hops` entradas las escribieron proxies nuestros y todo lo
    anterior lo pudo inventar el cliente. Tomar el PRIMER valor (lo que hacía
    antes) dejaba falsificar la IP con un header y saltear el rate limit y el
    lockout del login.

    Con N hops, la IP real es la N-ésima desde la derecha. Si la cadena viene
    más corta que N (un proxy no agregó nada), se usa la primera que haya. Sin
    X-Forwarded-For se mira X-Real-IP (lo setea Traefik) y, si no, el socket.
    Con hops=0 se ignoran los headers: solo vale el socket.
    """
    if hops is None:
        hops = int(get_settings().trusted_proxy_hops)
    peer = request.client.host if request.client else "unknown"
    if hops <= 0:
        return peer
    fwd = request.headers.get("x-forwarded-for", "")
    chain = [part.strip() for part in fwd.split(",") if part.strip()]
    if chain:
        return chain[-hops] if len(chain) >= hops else chain[0]
    real = (request.headers.get("x-real-ip") or "").strip()
    return real or peer


def verify_rate_limit(request: Request) -> None:
    """FastAPI dependency: limita /verify a N requests por IP por ventana."""
    ip = client_ip(request)
    now = time.time()
    hits = _verify_hits[ip]
    cutoff = now - _VERIFY_WINDOW_SECONDS
    while hits and hits[0] < cutoff:
        hits.popleft()
    if len(hits) >= _VERIFY_MAX_PER_WINDOW:
        retry = max(1, int(hits[0] + _VERIFY_WINDOW_SECONDS - now))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Demasiados /verify desde {ip}. Reintentá en {retry}s.",
            headers={"Retry-After": str(retry)},
        )
    hits.append(now)


def verify_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """FastAPI dependency: valida X-API-Key contra HUGO_API_KEY.

    - Si HUGO_API_KEY no está configurado, deja pasar (modo dev).
    - Si está configurado, exige el header y compara en tiempo constante.
    """
    expected = get_settings().hugo_api_key
    if not expected:
        log.warning("HUGO_API_KEY vacío — /verify queda abierto, no usar en producción")
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-API-Key inválida o ausente",
            headers={"WWW-Authenticate": "ApiKey"},
        )
