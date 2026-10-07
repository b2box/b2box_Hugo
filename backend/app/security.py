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
from functools import lru_cache

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
    más corta que N, algo no es como se configuró (un proxy que no agregó nada,
    o tráfico que llegó salteándose un hop): la única IP que no pudo inventar
    nadie es la del socket, así que se usa esa y no la primera del header. Sin
    X-Forwarded-For se mira X-Real-IP (lo setea Traefik) y, si no, el socket.
    Con hops=0 se ignoran los headers: solo vale el socket.

    hops=2 solo tiene sentido si Traefik acepta tráfico ÚNICAMENTE desde los
    rangos de Cloudflare: si alguien le pega directo a Traefik con un
    X-Forwarded-For armado, el "segundo hop" lo escribió el atacante.
    """
    if hops is None:
        hops = int(get_settings().trusted_proxy_hops)
    peer = request.client.host if request.client else "unknown"
    if hops <= 0:
        return peer
    fwd = request.headers.get("x-forwarded-for", "")
    chain = [part.strip() for part in fwd.split(",") if part.strip()]
    if chain:
        return chain[-hops] if len(chain) >= hops else peer
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


def _is_production() -> bool:
    env = getattr(get_settings(), "hugo_env", "") or ""
    return env.strip().lower() == "production"


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


# Una key de producción sale de secrets.token_urlsafe(32) (43 chars). Menos de
# esto es algo tipeado a mano o un placeholder que quedó del .env.example.
MIN_KEY_LEN = 24
_PLACEHOLDER_KEYS = frozenset({"replace-me", "replaceme", "change-me", "changeme", "example"})


def weak_key_reason(key: str) -> str | None:
    """Por qué una key NO sirve en producción, o None si sirve."""
    if key.strip().lower() in _PLACEHOLDER_KEYS:
        return "es un placeholder del .env.example"
    if len(key) < MIN_KEY_LEN:
        return f"tiene menos de {MIN_KEY_LEN} caracteres"
    return None


def _drop_weak_keys(keys: dict[str, str]) -> dict[str, str]:
    """En producción una key débil cuenta como NO configurada (con warning).

    Copiar el .env.example tal cual no puede dejar un cliente autenticable con
    "replace-me". Se avisa con el nombre del cliente, nunca con la key.
    """
    strong: dict[str, str] = {}
    for name, key in keys.items():
        reason = weak_key_reason(key)
        if reason:
            log.warning("HUGO_API_KEYS: la key del cliente %r %s; en producción se ignora", name, reason)
            continue
        strong[name] = key
    return strong


@lru_cache(maxsize=32)
def _resolve_keys(raw: str, legacy: str, production: bool) -> tuple[tuple[str, str], ...]:
    """Parsea y valida las keys UNA vez por combinación de valores.

    Esto corre en cada request autenticado; sin cache se re-parseaba el string
    y se repetía el warning de cada entrada rota en cada /verify. Cacheado por
    el valor crudo: si la env cambia (otro proceso), se vuelve a parsear. Las
    advertencias de configuración (entrada rota, cliente "legacy", dos clientes
    con la misma key) salen una sola vez por valor, con nombres y nunca keys.
    """
    keys = parse_api_keys(raw)
    legacy = legacy.strip()
    if legacy:
        if LEGACY_CLIENT in keys:
            log.warning(
                "HUGO_API_KEYS define un cliente %r: pisa a HUGO_API_KEY, que queda sin efecto. "
                "Renombrá el cliente o vaciá HUGO_API_KEY.", LEGACY_CLIENT,
            )
        else:
            keys[LEGACY_CLIENT] = legacy
    if production:
        keys = _drop_weak_keys(keys)
    owners: dict[str, list[str]] = defaultdict(list)
    for name, key in keys.items():
        owners[key].append(name)
    for names in owners.values():
        if len(names) > 1:
            log.warning(
                "HUGO_API_KEYS: los clientes %s comparten la misma key; los requests se van a "
                "atribuir todos a %r y no se puede rotar uno sin el otro", names, names[0],
            )
    return tuple(keys.items())


def configured_api_keys() -> dict[str, str]:
    """Keys vigentes por nombre de cliente, incluida la legacy si está seteada.

    En production las keys débiles (placeholder / cortas) se descartan."""
    s = get_settings()
    return dict(_resolve_keys(s.hugo_api_keys or "", s.hugo_api_key or "", _is_production()))


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

    - Sin keys configuradas: en development deja pasar con un warning; en
      producción responde 503 (fail-closed). El arranque ya rechaza ese estado
      (main._enforce_prod_secrets), esto es la segunda traba por si algo lo
      saltea: un endpoint cerrado es mejor que /verify abierto a internet.
    - Con keys, exige el header y compara en tiempo constante. Devuelve el
      nombre del cliente, lo loguea y lo deja en request.state.api_client.
    """
    if not api_keys_configured():
        if _is_production():
            log.error(
                "Producción sin API keys válidas (HUGO_API_KEYS/HUGO_API_KEY): %s cerrado con 503",
                request.url.path,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Autenticación por API key no configurada en el servidor",
            )
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
