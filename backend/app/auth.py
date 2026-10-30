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
- `/login`                     → pantalla de acceso (SPA de React)
- `/api/login`, `/api/logout`  → endpoints de sesión
- `/health`                    → liveness probe (orquestadores/monitoreo)
- `/static/*`                  → assets del build de React (JS/CSS)
- `/verify`                    → lo consume Luis con su X-API-Key (auth propia)
- `/radar/*`                   → radar de Luis, mismo X-API-Key que /verify
- `/api/oficina/*`             → la Mac de la oficina, con su x-oficina-key (auth propia)
- `/api/vendure/*`             → avisos de Vendure al guardar, con su X-Hugo-Events-Key (auth propia)
- `/favicon.ico`               → ruido del browser
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import get_settings
from app.security import client_ip  # noqa: F401  (misma función que el rate limit de /verify)

log = logging.getLogger(__name__)

COOKIE_NAME = "hugo_session"

# ── Rate-limit / lockout del login (in-memory, por IP) ─────────────
# Tras _MAX_FAILS intentos fallidos en _WINDOW segundos, bloqueamos esa IP por
# _LOCKOUT segundos. Simple y suficiente para 1 instancia; para multi-instancia
# convendría Redis. Frena fuerza bruta contra el único usuario/contraseña.
_MAX_FAILS = 5
_WINDOW = 300.0
_LOCKOUT = 300.0
_fail_log: dict[str, list[float]] = {}
_locked_until: dict[str, float] = {}


def is_locked(ip: str) -> bool:
    until = _locked_until.get(ip, 0.0)
    if until and time.time() < until:
        return True
    if until:
        _locked_until.pop(ip, None)
    return False


def record_failed_login(ip: str) -> None:
    now = time.time()
    hits = [t for t in _fail_log.get(ip, []) if now - t < _WINDOW]
    hits.append(now)
    _fail_log[ip] = hits
    if len(hits) >= _MAX_FAILS:
        _locked_until[ip] = now + _LOCKOUT
        _fail_log.pop(ip, None)
        log.warning("Login bloqueado para IP %s por %ds (demasiados intentos)", ip, int(_LOCKOUT))


def record_successful_login(ip: str) -> None:
    _fail_log.pop(ip, None)
    _locked_until.pop(ip, None)

# Prefijos que NO requieren sesión. El resto del sitio queda protegido.
_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/login",       # GET: pantalla de login (SPA React)
    "/api/login",   # POST: valida credenciales y setea la cookie
    "/api/logout",  # POST: borra la cookie
    "/health",
    "/static/",     # assets del build de React (JS/CSS)
    "/verify",
    "/radar/",      # radar de Luis → /radar/ml-supply (autentica por X-API-Key, no por cookie)
    "/app/",        # b2box app → /app/lookup (autentica por X-API-Key, no por cookie)
    "/api/oficina/",  # buscador de la oficina: autentica por x-oficina-key, no por cookie
    "/api/vendure/",  # avisos de Vendure al guardar: autentica por X-Hugo-Events-Key, no por cookie
    "/favicon.ico",
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


MIN_SECRET_LEN = 32
SESSION_KEY_MISSING_DETAIL = (
    "No hay clave para firmar las sesiones del dashboard: cargá DASHBOARD_SECRET "
    "(32+ caracteres, ver README). Sin ella nadie puede iniciar sesión."
)


class SessionKeyMissing(RuntimeError):
    """No hay clave secreta para firmar la cookie: no se emiten sesiones."""


def session_secret_configured() -> bool:
    """DASHBOARD_SECRET de 32+ caracteres: la única clave que vale en producción
    y la única con la que una sesión puede ser de un administrador."""
    return len((get_settings().dashboard_secret or "").strip()) >= MIN_SECRET_LEN


def _signing_secret() -> bytes | None:
    """Clave para firmar la cookie, o None si no hay ninguna que sea secreta.

    BUG 20-oct-2026: sin DASHBOARD_SECRET se derivaba de DASHBOARD_PASSWORD
    aunque estuviera VACÍA (prod con login por Supabase): la clave era
    sha256("hugo-session::"), pública en un repo público, y cualquiera podía
    falsificar la cookie. Ahora:
      * DASHBOARD_SECRET de 32+ caracteres → esa;
      * en producción, nada más: sin él no hay sesiones;
      * en desarrollo, derivada de una DASHBOARD_PASSWORD NO vacía;
      * si no, None: no se emite ni se acepta ninguna sesión.
    """
    s = get_settings()
    secret = (s.dashboard_secret or "").strip()
    if len(secret) >= MIN_SECRET_LEN:
        return secret.encode("utf-8")
    if _is_production() or not s.dashboard_password:
        return None
    return hashlib.sha256(b"hugo-session::" + s.dashboard_password.encode("utf-8")).digest()


def supabase_enabled() -> bool:
    """True si el login debe validar contra el Supabase Auth de Cloud_B2BOX."""
    s = get_settings()
    return bool(s.supabase_url and s.supabase_anon_key)


def login_enabled() -> bool:
    """True si hay algún método de login activo (Supabase o password local).

    Si no hay ninguno, el dashboard queda abierto (solo modo dev)."""
    s = get_settings()
    return supabase_enabled() or bool(s.dashboard_password)


ALLOWLIST_OPEN = "*"
# Para el LOG del servidor: dice qué variable falta y cómo arreglarlo.
ALLOWLIST_MISSING_DETAIL = (
    "SUPABASE_ALLOWED_EMAILS no configurado: en producción el login por Supabase "
    "queda cerrado. Cargá los emails permitidos (coma-separados) o \"*\" para abrir "
    "a todos los usuarios de Cloud_B2BOX."
)
# Para la RESPUESTA HTTP: a quien está del otro lado no le damos el nombre de
# nuestras variables de entorno; con saber que es configuración alcanza.
LOGIN_DISABLED_DETAIL = (
    "Login deshabilitado por configuración del servidor. Avisale al administrador."
)


def _is_production() -> bool:
    return get_settings().hugo_env.strip().lower() == "production"


def allowlist_state() -> str:
    """'open' ("*"), 'list' (hay emails) o 'unset' (vacía)."""
    raw = get_settings().supabase_allowed_emails.strip()
    if not raw:
        return "unset"
    if raw == ALLOWLIST_OPEN:
        return "open"
    return "list"


def allowlist_misconfigured() -> bool:
    """True si NINGÚN login por Supabase puede pasar: producción + allowlist vacía.

    Antes, allowlist vacía = entraban todos los usuarios de Cloud_B2BOX. Eso es
    fail-open: olvidarse una variable abría el dashboard a todo el pool. Ahora
    hay que decir "*" a propósito. La app no se cae por esto (no es un
    RuntimeError al arranque): se avisa en logs y cada login recibe un 403 con
    el motivo. Los clientes por API key (/verify, /app/*) no se ven afectados.
    """
    return supabase_enabled() and allowlist_state() == "unset" and _is_production()


def _email_allowed(email: str) -> bool:
    """Aplica la allowlist de emails. Fail-closed en producción si está vacía."""
    state = allowlist_state()
    if state == "open":
        return True
    if state == "unset":
        if _is_production():
            log.error("Login de %s rechazado: %s", email, ALLOWLIST_MISSING_DETAIL)
            return False
        log.warning(
            "SUPABASE_ALLOWED_EMAILS vacío — en development entra cualquier usuario "
            "válido de Cloud_B2BOX; en producción se rechaza"
        )
        return True
    raw = get_settings().supabase_allowed_emails
    allow = {e.strip().lower() for e in raw.split(",") if e.strip()}
    return email.strip().lower() in allow


async def supabase_login(email: str, password: str) -> tuple[bool, str | None]:
    """Valida email+password contra el Supabase Auth (GoTrue) de Cloud_B2BOX.

    Devuelve (ok, email_normalizado). Mismo pool de usuarios que Paco.
    """
    s = get_settings()
    if not email or not password:
        return (False, None)
    url = f"{s.supabase_url.rstrip('/')}/auth/v1/token"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                params={"grant_type": "password"},
                headers={"apikey": s.supabase_anon_key, "Content-Type": "application/json"},
                json={"email": email, "password": password},
            )
    except httpx.HTTPError as exc:
        log.error("Supabase login: error de red: %s", exc)
        return (False, None)

    if resp.status_code != 200:
        # 400 = credenciales inválidas / email no confirmado. No logueamos el body.
        log.info("Supabase login rechazado (%s) para %s", resp.status_code, email)
        return (False, None)

    try:
        data = resp.json()
        user_email = (data.get("user") or {}).get("email") or email
    except ValueError:
        return (False, None)

    if not _email_allowed(user_email):
        log.warning("Supabase login OK pero email %s no está en la allowlist", user_email)
        return (False, None)
    return (True, user_email)


def issue_session_token(username: str) -> str:
    """Genera un token de sesión firmado: <payload_b64>.<firma_b64>."""
    s = get_settings()
    exp = int(time.time()) + s.dashboard_session_hours * 3600
    payload = json.dumps({"u": username, "exp": exp}, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64url(payload)
    key = _signing_secret()
    if key is None:
        log.error(SESSION_KEY_MISSING_DETAIL)
        raise SessionKeyMissing(SESSION_KEY_MISSING_DETAIL)
    sig = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64url(sig)}"


def _read_session(token: str | None) -> dict | None:
    """Payload del token de sesión si la firma y la expiración son válidas."""
    if not token or "." not in token:
        return None
    key = _signing_secret()
    if key is None:
        return None
    payload_b64, _, sig_b64 = token.partition(".")
    expected = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    try:
        got = _b64url_decode(sig_b64)
    except Exception:  # noqa: BLE001
        return None
    if not hmac.compare_digest(expected, got):
        return None
    try:
        data = json.loads(_b64url_decode(payload_b64))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    return data if int(data.get("exp", 0)) > int(time.time()) else None


def verify_session_token(token: str | None) -> bool:
    """Valida firma + expiración del token de sesión en tiempo constante."""
    return _read_session(token) is not None


def session_username(token: str | None) -> str | None:
    """Quién es la sesión (el usuario del .env, o el email con Supabase), o None
    si el token no es válido. Para dejar anotado quién hizo una acción."""
    data = _read_session(token)
    user = data.get("u") if data else None
    return str(user)[:120] if user else None


def admin_users() -> set[str]:
    """DASHBOARD_ADMINS normalizado (minúsculas, sin vacíos)."""
    raw = get_settings().dashboard_admins or ""
    return {u.strip().lower() for u in raw.split(",") if u.strip()}


def admin_username(request: Request) -> str | None:
    """El usuario de la sesión si es administrador, o None.

    FAIL-CLOSED: sin DASHBOARD_ADMINS nadie es admin, tampoco con el login
    deshabilitado (modo dev): un dashboard abierto no puede gastar plata."""
    admins = admin_users()
    # Admin solo con una clave de sesión de verdad secreta (DASHBOARD_SECRET):
    # una derivada de la password del login local no alcanza para gastar plata.
    if not admins or not session_secret_configured():
        return None
    user = session_username(request.cookies.get(COOKIE_NAME))
    return user if user and user.strip().lower() in admins else None


def require_admin(request: Request) -> str:
    """403 si la sesión no es de un administrador. Devuelve el usuario."""
    from fastapi import HTTPException

    user = admin_username(request)
    if user is None:
        if not admin_users():
            log.warning("DASHBOARD_ADMINS vacío: %s %s rechazado (fail-closed)",
                        request.method, request.url.path)
        raise HTTPException(
            status_code=403,
            detail="Solo un administrador del dashboard puede hacer esto.",
        )
    return user


def review_deciders() -> set[str]:
    """PRICE_REVIEW_DECIDERS normalizado (solo emails válidos, en minúsculas)."""
    from app.notifier.email import parse_recipients   # lazy: notifier importa los modelos

    return {e.lower() for e in parse_recipients(get_settings().price_review_deciders)}


def review_decider(request: Request) -> str | None:
    """El usuario de la sesión si puede decidir en la bandeja de precios, o None.

    Mismo criterio que `admin_username`: FAIL-CLOSED (sin PRICE_REVIEW_DECIDERS nadie decide, tampoco en
    desarrollo), solo con DASHBOARD_SECRET y con una sesión que diga quién es. La comparación ignora
    mayúsculas y espacios."""
    deciders = review_deciders()
    if not deciders or not session_secret_configured():
        return None
    user = session_username(request.cookies.get(COOKIE_NAME))
    return user.strip() if user and user.strip().lower() in deciders else None


def require_decider(request: Request) -> str:
    """403 si la sesión no puede decidir en la bandeja. Devuelve el email (el actor de los eventos).

    CSRF explícito: un navegador manda `Sec-Fetch-Site`; si no es `same-origin`, el pedido lo armó otra página y no
    pasa (la cookie de sesión iría sola). Sin el header (un cliente que no es un navegador) se deja pasar."""
    from fastapi import HTTPException

    site = request.headers.get("sec-fetch-site")
    if site is not None and site != "same-origin":
        log.warning("decisión de la bandeja desde otro sitio (Sec-Fetch-Site=%s): %s rechazado",
                    site[:20], request.url.path)
        raise HTTPException(status_code=403, detail="Las decisiones solo se toman desde el dashboard de Hugo.")
    user = review_decider(request)
    if user is not None:
        return user
    if not review_deciders():
        log.warning("PRICE_REVIEW_DECIDERS vacío: %s %s rechazado (fail-closed)", request.method, request.url.path)
        detail = "Nadie puede decidir todavía: falta cargar PRICE_REVIEW_DECIDERS."
    elif not session_secret_configured():
        detail = "Para decidir hace falta DASHBOARD_SECRET configurado."
    else:
        detail = "No estás en la lista de quienes deciden (PRICE_REVIEW_DECIDERS)."
    raise HTTPException(status_code=403, detail=detail)


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
    """¿La ruta está en la allowlist pública?

    El prefijo corta en límite de segmento: "/verify" cubre "/verify" y
    "/verify/...", pero no "/verify-admin". Con un startswith pelado, cualquier
    ruta nueva que empezara con el nombre de una pública salteaba el login sin
    que nadie lo hubiera decidido.
    """
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in _PUBLIC_PREFIXES)


def _wants_html(request: Request) -> bool:
    """Heurística: ¿es una navegación de browser (redirigir) o un fetch/API (401)?"""
    accept = request.headers.get("accept", "")
    return "text/html" in accept


async def auth_middleware(request: Request, call_next):
    """Middleware que exige sesión válida salvo en las rutas públicas.

    - Navegación de browser sin sesión → redirect 302 a /login.
    - Llamada fetch/API sin sesión      → 401 JSON (el front redirige).

    La ruta sale de scope["path"], el mismo string contra el que matchea el
    router. request.url.path NO sirve: Starlette arma request.url pegando el
    header Host, y con "Host: x/verify" un GET /api/settings se veía acá como
    /verify/api/settings (pública) mientras el router servía /api/settings.
    """
    path = request.scope["path"]
    if not login_enabled():
        if not _is_public_path(path):
            log.warning(
                "DASHBOARD_PASSWORD vacío — el dashboard queda ABIERTO, no usar en producción"
            )
        return await call_next(request)

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
        # En producción va Secure siempre: no puede depender de que el proxy
        # mande X-Forwarded-Proto. Si un día no lo manda, la cookie de sesión
        # saldría sin Secure y el browser la mandaría también por http.
        secure=cookie_is_secure(request) or _is_production(),
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


# Utilidad expuesta por si se quiere generar un secreto a mano.
def generate_secret() -> str:
    return secrets.token_urlsafe(32)
