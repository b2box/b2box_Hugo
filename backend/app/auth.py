"""Login del dashboard por sesión (cookie firmada con HMAC, solo stdlib).

El dashboard humano (`/`) y todos sus endpoints de datos (`/api/*`, `/audit`,
`/audit-log`) quedan detrás de un login usuario+contraseña. La sesión se guarda
en una cookie `hugo_session` firmada con HMAC-SHA256 (no se puede falsificar sin
el secreto del servidor) y con expiración embebida.

Diseño deliberadamente simple y sin dependencias nuevas:
- Un único usuario/contraseña (DASHBOARD_USER / DASHBOARD_PASSWORD del .env).
- Si DASHBOARD_PASSWORD está vacío → login DESHABILITADO (modo dev), igual que
  el criterio de HUGO_API_KEY. Loguea un warning al primer chequeo.

Lo que NO requiere login (allowlist):
- `/login`, `/logout`     → la propia pantalla de acceso
- `/health`               → liveness probe (orquestadores/monitoreo)
- `/static/*`             → assets de la UI (incluye la propia login.html)
- `/verify`               → lo consume Luis con su X-API-Key (auth propia)
- `/favicon.ico`          → ruido del browser
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import get_settings

log = logging.getLogger(__name__)

COOKIE_NAME = "hugo_session"

# Prefijos que NO requieren sesión. El resto del sitio queda protegido.
_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/login",
    "/logout",
    "/health",
    "/static/",
    "/verify",
    "/favicon.ico",
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _signing_secret() -> bytes:
    """Secreto para firmar la cookie.

    Usa DASHBOARD_SECRET si está seteado; si no, lo deriva de la password (así es
    estable entre reinicios y workers sin tener que configurar nada extra).
    """
    s = get_settings()
    if s.dashboard_secret:
        return s.dashboard_secret.encode("utf-8")
    return hashlib.sha256(b"hugo-session::" + s.dashboard_password.encode("utf-8")).digest()


def login_enabled() -> bool:
    """True si hay password configurada. Si no, el dashboard queda abierto (dev)."""
    return bool(get_settings().dashboard_password)


def issue_session_token(username: str) -> str:
    """Genera un token de sesión firmado: <payload_b64>.<firma_b64>."""
    s = get_settings()
    exp = int(time.time()) + s.dashboard_session_hours * 3600
    payload = json.dumps({"u": username, "exp": exp}, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64url(payload)
    sig = hmac.new(_signing_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64url(sig)}"


def verify_session_token(token: str | None) -> bool:
    """Valida firma + expiración del token de sesión en tiempo constante."""
    if not token or "." not in token:
        return False
    payload_b64, _, sig_b64 = token.partition(".")
    expected = hmac.new(_signing_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    try:
        got = _b64url_decode(sig_b64)
    except Exception:  # noqa: BLE001
        return False
    if not hmac.compare_digest(expected, got):
        return False
    try:
        data = json.loads(_b64url_decode(payload_b64))
    except Exception:  # noqa: BLE001
        return False
    return int(data.get("exp", 0)) > int(time.time())


def check_credentials(username: str, password: str) -> bool:
    """Compara usuario+contraseña contra el .env en tiempo constante."""
    s = get_settings()
    user_ok = hmac.compare_digest(username.encode("utf-8"), s.dashboard_user.encode("utf-8"))
    pass_ok = hmac.compare_digest(password.encode("utf-8"), s.dashboard_password.encode("utf-8"))
    # Evaluamos ambos siempre para no filtrar cuál falló por timing.
    return user_ok and pass_ok


def cookie_is_secure(request: Request) -> bool:
    """True si la conexión es HTTPS (directa o vía proxy) → cookie Secure."""
    proto = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or proto.split(",")[0].strip() == "https"


def _is_public_path(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in _PUBLIC_PREFIXES)


def _wants_html(request: Request) -> bool:
    """Heurística: ¿es una navegación de browser (redirigir) o un fetch/API (401)?"""
    accept = request.headers.get("accept", "")
    return "text/html" in accept


async def auth_middleware(request: Request, call_next):
    """Middleware que exige sesión válida salvo en las rutas públicas.

    - Navegación de browser sin sesión → redirect 302 a /login.
    - Llamada fetch/API sin sesión      → 401 JSON (el front redirige).
    """
    if not login_enabled():
        if not _is_public_path(request.url.path):
            log.warning(
                "DASHBOARD_PASSWORD vacío — el dashboard queda ABIERTO, no usar en producción"
            )
        return await call_next(request)

    path = request.url.path
    if _is_public_path(path):
        return await call_next(request)

    token = request.cookies.get(COOKIE_NAME)
    if verify_session_token(token):
        return await call_next(request)

    # No autenticado
    if _wants_html(request):
        return RedirectResponse(url="/login", status_code=302)
    return JSONResponse({"detail": "No autenticado"}, status_code=401)


def set_session_cookie(response, request: Request, username: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=issue_session_token(username),
        max_age=get_settings().dashboard_session_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=cookie_is_secure(request),
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


# Utilidad expuesta por si se quiere generar un secreto a mano.
def generate_secret() -> str:
    return secrets.token_urlsafe(32)
