"""Entry point de Hugo.

Levanta FastAPI, inicializa la DB y arranca el scheduler.

Uso:
    uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import auth, security
from app.api.app_routes import router as app_router
from app.api.oficina_routes import router as oficina_router
from app.api.price_monitor_routes import router as price_monitor_router
from app.api.routes import router
from app.api.seo_routes import router as seo_router
from app.api.store_routes import router as store_router
from app.config import EnvSettings, get_settings
from app.db.session import init_db
from app.scheduler.jobs import register_jobs, scheduler

_STATIC_DIR = Path(__file__).parent / "static"


def _configure_logging() -> None:
    s = get_settings()
    logging.basicConfig(
        level=s.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Aunque LOG_LEVEL sea DEBUG: gql, httpx y httpcore loguean respuestas con datos del proveedor y
    # headers con el token de Vendure (ver quiet_http_loggers).
    from app.vendure.client import quiet_http_loggers

    quiet_http_loggers()


def _enforce_prod_secrets() -> None:
    """En producción, faltar credenciales NO puede ser solo un warning.

    Si HUGO_ENV=production y falta el login del dashboard o ninguna API key
    PARSEA, abortamos el arranque: mejor caer que quedar expuestos a internet
    sin auth. "Parsea" es la palabra clave: antes alcanzaba con que la variable
    no estuviera vacía, y un HUGO_API_KEYS pegado sin "nombre:" (o "luis:", o
    HUGO_API_KEY="   ") pasaba el chequeo mientras security.parse_api_keys lo
    descartaba → /verify y /app/* abiertos con solo un warning.
    """
    s = get_settings()
    log = logging.getLogger(__name__)
    if s.hugo_env.strip().lower() != "production":
        # development no puede arrancar callado: es el modo en que el panel quedó
        # abierto a internet. Si alguien lo prende en un server, que el log de
        # arranque diga qué quedó sin traba.
        log.error(
            "HUGO_ENV=development: arranque SIN los chequeos de producción. Se relaja: "
            "login del dashboard no obligatorio (sin credenciales queda abierto); "
            "SUPABASE_ALLOWED_EMAILS vacía deja entrar a cualquier usuario de Cloud_B2BOX; "
            "API keys débiles o placeholder aceptadas (y sin keys, /verify y /app/* abiertos); "
            "/docs, /redoc y /openapi.json publicados. No exponer a internet."
        )
        return
    missing = []
    # Login del dashboard: vale Supabase (preferido) o password local.
    has_supabase = bool(s.supabase_url and s.supabase_anon_key)
    if not has_supabase and not s.dashboard_password:
        missing.append("SUPABASE_URL+SUPABASE_ANON_KEY (o DASHBOARD_PASSWORD)")
    if not security.api_keys_configured():
        missing.append("HUGO_API_KEYS (o HUGO_API_KEY) con al menos una key válida nombre:key")
    if missing:
        raise RuntimeError(
            "HUGO_ENV=production pero faltan credenciales obligatorias: "
            f"{', '.join(missing)}. Seteálas o poné HUGO_ENV=development."
        )
    # La allowlist vacía NO tira el arranque (sería un restart loop por una
    # variable): los logins por Supabase reciben 403 con el motivo, y los
    # clientes por API key siguen funcionando. Pero se avisa fuerte.
    if auth.allowlist_misconfigured():
        log.error("Dashboard sin acceso: %s", auth.ALLOWLIST_MISSING_DETAIL)
    # Sin DASHBOARD_SECRET no hay sesiones (la clave derivada era pública). No se
    # cae el arranque: /verify y /app/* (Paco, el app) siguen andando.
    if not auth.session_secret_configured():
        log.error("Dashboard sin login posible: %s", auth.SESSION_KEY_MISSING_DETAIL)
    # Tampoco tira el arranque: es una ruta de diagnóstico. Pero sin esto el 403
    # de /api/debug-config no tiene explicación a la vista.
    for problem in security.admin_config_warnings():
        log.warning("/api/debug-config: %s", problem)


def _seed_stores() -> None:
    """Siembra Casa Perfecta y Gadnic (una sola vez) y carga los hosts de foto de las
    tiendas activas. Un error acá no puede impedir que Hugo arranque."""
    from app.pricing import store_catalog

    try:
        store_catalog.seed_default_stores()
        store_catalog.refresh_allowed_image_hosts()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("No se pudieron sembrar las tiendas: %s", exc)


async def _drain_apify_aborts() -> None:
    """Al apagar, espera los aborts de Apify que quedaron en segundo plano (cada
    uno tiene su tope de segundos). Sin esto un redeploy los mata a mitad de
    camino y la corrida perdedora sigue gastando hasta su timeout."""
    from app.ingest import apify_1688

    try:
        await apify_1688.wait_pending_aborts()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("Aborts de Apify sin confirmar al apagar: %s",
                                            type(exc).__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    import asyncio

    _configure_logging()
    _enforce_prod_secrets()
    init_db()
    _seed_stores()

    # Precalentar el catálogo de Vendure en background: así el primer /verify tras
    # el deploy no espera un cold-fetch de todo el catálogo. No bloquea el arranque.
    # Reintenta con backoff hasta lograrlo: un solo intento fallido dejaba a Hugo
    # sin catálogo (y a /app/lookup muerto) hasta el primer request con suerte.
    async def _warm_catalog() -> None:
        from app.vendure import catalog as vendure_catalog

        delay = 15.0
        for attempt in range(1, 21):
            try:
                await vendure_catalog.get_catalog()
                return
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning(
                    "warm catalog falló (intento %d): %s — reintento en %ds",
                    attempt, exc, int(delay),
                )
                await asyncio.sleep(delay)
                delay = min(delay * 1.7, 300.0)
        logging.getLogger(__name__).error(
            "warm catalog agotó los reintentos; /app/lookup va a fallar hasta que Vendure responda"
        )

    asyncio.create_task(_warm_catalog())

    # Índice vectorial (CLIP) para /app/lookup. El primer build baja y embebe las
    # imágenes del catálogo: minutos en 1 CPU. Va en background para no demorar
    # el arranque; mientras tanto /app/lookup responde status="indexing".
    async def _warm_image_index() -> None:
        from app.dedup import catalog_index, image_embed

        if not image_embed.available():
            return
        try:
            await catalog_index.build()
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning("warm image index falló: %s", exc)

    asyncio.create_task(_warm_image_index())

    # Import de camoufox al arranque. Es perezoso porque el paquete es opcional
    # (Firefox + fingerprints), pero eso hacía que el PRIMER lookup después de
    # cada deploy pagara ~9 s de import antes de siquiera abrir la página. El
    # que espera es un cliente; el arranque no espera a nadie.
    async def _warm_browser() -> None:
        from app.ingest import browser_fetch

        if not browser_fetch.available():
            return
        try:
            await asyncio.to_thread(browser_fetch._camoufox)
            # Si el build no pudo bajar Firefox (GitHub limita por IP), se
            # baja acá, en background. Fail-soft: si falla, render() dirá
            # BrowserUnavailable y el fetch plano sigue como siempre.
            await asyncio.to_thread(browser_fetch.ensure_browser_installed)
        except Exception as exc:  # noqa: BLE001
            # available() vuelve a intentar el import y devuelve False si no
            # está: que esto falle no puede tumbar el arranque.
            logging.getLogger(__name__).warning("warm browser falló: %s", exc)

    asyncio.create_task(_warm_browser())
    # Solo el líder corre el scheduler (evita jobs y gasto OTAPI duplicados si
    # hay más de una réplica).
    from app.leader import try_become_leader

    is_leader = try_become_leader()
    if is_leader:
        register_jobs()
        scheduler.start()
    try:
        yield
    finally:
        if is_leader:
            scheduler.shutdown(wait=False)
        await _drain_apify_aborts()


# En producción no publicamos Swagger: el mapa completo de rutas es
# reconocimiento gratis para un atacante y acá no le sirve a nadie. En
# development sigue disponible en /docs.
#
# EnvSettings en vez de get_settings(): esto corre al importar el módulo, y
# Settings() exige campos obligatorios (VENDURE_API_URL) que no tienen por qué
# estar presentes solo para importar app.main. EnvSettings lee HUGO_ENV de las
# mismas fuentes y con el mismo validador que Settings, así que /docs se publica
# si y solo si los chequeos de seguridad están en development.
_DEV = EnvSettings().hugo_env == "development"

app = FastAPI(
    title="Hugo — B2Box Catalog QC",
    version="0.1.0",
    description="Anti-duplicados + vigilancia de precios de proveedor para Vendure (no modifica precios)",
    lifespan=lifespan,
    docs_url="/docs" if _DEV else None,
    redoc_url="/redoc" if _DEV else None,
    openapi_url="/openapi.json" if _DEV else None,
)

# ─── Login del dashboard (cookie de sesión) ────────────────────────
# El middleware protege todo salvo /login, /api/login, /api/logout, /health,
# /static y /verify.
app.middleware("http")(auth.auth_middleware)


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


@app.get("/login", include_in_schema=False)
async def login_page() -> FileResponse:
    """Pantalla de login.

    Servimos la misma SPA de React (index.html); el router del front detecta la
    ruta /login y muestra el formulario de acceso.
    """
    return FileResponse(_STATIC_DIR / "index.html")


@app.post("/api/login", include_in_schema=False)
async def login(payload: LoginRequest, request: Request) -> JSONResponse:
    """Valida credenciales y, si son correctas, setea la cookie de sesión."""
    if not auth.login_enabled():
        # Login deshabilitado (sin password en .env): dejamos pasar.
        return JSONResponse({"ok": True, "disabled": True})
    ip = auth.client_ip(request)
    if auth.is_locked(ip):
        return JSONResponse(
            {"ok": False, "detail": "Demasiados intentos. Esperá unos minutos."},
            status_code=429,
        )

    # Método preferido: Supabase Auth de Cloud_B2BOX (mismo login que Paco).
    # Fallback: user/pass local (solo si Supabase no está configurado).
    if auth.supabase_enabled():
        if auth.allowlist_misconfigured():
            # Nadie puede entrar: no se manda la contraseña a Supabase para
            # nada. El motivo exacto va al log; al cliente, un texto genérico.
            logging.getLogger(__name__).error(
                "Login rechazado desde %s: %s", ip, auth.ALLOWLIST_MISSING_DETAIL
            )
            return JSONResponse(
                {"ok": False, "detail": auth.LOGIN_DISABLED_DETAIL}, status_code=403
            )
        ok, identity = await auth.supabase_login(payload.username, payload.password)
    else:
        ok = auth.check_credentials(payload.username, payload.password)
        identity = payload.username

    if not ok:
        auth.record_failed_login(ip)
        return JSONResponse(
            {"ok": False, "detail": "Email o contraseña incorrectos"}, status_code=401
        )
    auth.record_successful_login(ip)
    resp = JSONResponse({"ok": True})
    try:
        auth.set_session_cookie(resp, request, identity or payload.username)
    except auth.SessionKeyMissing:
        return JSONResponse(
            {"ok": False, "detail": auth.LOGIN_DISABLED_DETAIL}, status_code=503
        )
    return resp


@app.post("/api/logout", include_in_schema=False)
async def logout() -> JSONResponse:
    """Cierra la sesión borrando la cookie."""
    resp = JSONResponse({"ok": True})
    auth.clear_session_cookie(resp)
    return resp


app.include_router(router)
app.include_router(app_router)
app.include_router(price_monitor_router)
app.include_router(oficina_router)
app.include_router(store_router)
app.include_router(seo_router)


# ─── Dashboard estático ────────────────────────────────────────────


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    """Sirve el dashboard (SPA de React) en la raíz.

    El build de React (Vite) se copia a app/static/ en el Dockerfile. Los assets
    (JS/CSS) se referencian bajo /static/ y se sirven vía el mount de más abajo.
    """
    return FileResponse(_STATIC_DIR / "index.html")


if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
