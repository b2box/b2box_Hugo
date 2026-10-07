"""Auth simple por API key.

Endpoints sensibles (los que Luis/Paco u otros agentes externos consumen)
requieren el header `X-API-Key`. Hay una key POR CLIENTE:

    HUGO_API_KEYS="luis:xxx,cloud:yyy,b2box-app:zzz"

El nombre del cliente se loguea en cada request autenticado y queda en
`request.state.api_client`. HUGO_API_KEY (una sola key compartida) sigue
valiendo como cliente "legacy" para no romper a quien ya la tenga.

Si no hay ninguna key configurada, el endpoint queda abierto y loguea un
warning. Esto facilita testing local pero NO debe usarse en producción (con
HUGO_ENV=production el arranque falla, ver main._enforce_prod_secrets).
"""

from __future__ import annotations

import hmac
import logging
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


LEGACY_CLIENT = "legacy"


def parse_api_keys(raw: str) -> dict[str, str]:
    """"luis:xxx,cloud:yyy" → {"luis": "xxx", "cloud": "yyy"}.

    Tolerante: entradas vacías, sin ':' o sin key se ignoran con warning (una
    key mal escrita no debe tirar abajo a los demás clientes). Si un nombre se
    repite, gana la última.
    """
    keys: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, sep, key = chunk.partition(":")
        name, key = name.strip(), key.strip()
        if not sep or not name or not key:
            log.warning("HUGO_API_KEYS: entrada ignorada (formato esperado nombre:key)")
            continue
        if name in keys:
            log.warning("HUGO_API_KEYS: cliente %r repetido, gana la última key", name)
        keys[name] = key
    return keys


def configured_api_keys() -> dict[str, str]:
    """Keys vigentes por nombre de cliente, incluida la legacy si está seteada."""
    s = get_settings()
    keys = parse_api_keys(s.hugo_api_keys)
    legacy = (s.hugo_api_key or "").strip()
    if legacy:
        keys.setdefault(LEGACY_CLIENT, legacy)
    return keys


def api_keys_configured() -> bool:
    return bool(configured_api_keys())


def match_api_key(presented: str | None) -> str | None:
    """Nombre del cliente cuya key coincide con la presentada, o None.

    Compara contra TODAS las keys con hmac.compare_digest y sin cortar en la
    primera coincidencia, para que el tiempo de respuesta no dependa de cuál
    (ni de si alguna) matcheó.
    """
    if not presented:
        return None
    given = presented.encode("utf-8")
    matched: str | None = None
    for name, key in configured_api_keys().items():
        if hmac.compare_digest(given, key.encode("utf-8")) and matched is None:
            matched = name
    return matched


def verify_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> str | None:
    """FastAPI dependency: valida X-API-Key contra las keys configuradas.

    - Sin keys configuradas, deja pasar (modo dev) y loguea un warning.
    - Con keys, exige el header y compara en tiempo constante. Devuelve el
      nombre del cliente, lo loguea y lo deja en request.state.api_client.
    """
    if not api_keys_configured():
        log.warning("Sin HUGO_API_KEYS ni HUGO_API_KEY — %s queda abierto, no usar en producción",
                    request.url.path)
        return None
    client = match_api_key(x_api_key)
    if client is None:
        log.info("X-API-Key rechazada en %s %s desde %s",
                 request.method, request.url.path, client_ip(request))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-API-Key inválida o ausente",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    request.state.api_client = client
    log.info("API key OK: cliente=%s %s %s", client, request.method, request.url.path)
    return client
