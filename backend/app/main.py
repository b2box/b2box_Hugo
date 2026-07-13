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

from app import auth
from app.api.routes import router
from app.config import get_settings
from app.db.session import init_db
from app.scheduler.jobs import register_jobs, scheduler

_STATIC_DIR = Path(__file__).parent / "static"


def _configure_logging() -> None:
    s = get_settings()
    logging.basicConfig(
        level=s.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _enforce_prod_secrets() -> None:
    """En producción, faltar credenciales NO puede ser solo un warning.

    Si HUGO_ENV=production y falta DASHBOARD_PASSWORD o HUGO_API_KEY, abortamos
    el arranque: mejor caer que quedar expuestos a internet sin auth.
    """
    s = get_settings()
    if s.hugo_env.strip().lower() != "production":
        return
    missing = []
    # Login del dashboard: vale Supabase (preferido) o password local.
    has_supabase = bool(s.supabase_url and s.supabase_anon_key)
    if not has_supabase and not s.dashboard_password:
        missing.append("SUPABASE_URL+SUPABASE_ANON_KEY (o DASHBOARD_PASSWORD)")
    if not s.hugo_api_key:
        missing.append("HUGO_API_KEY")
    if missing:
        raise RuntimeError(
            "HUGO_ENV=production pero faltan credenciales obligatorias: "
            f"{', '.join(missing)}. Seteálas o poné HUGO_ENV=development."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    _configure_logging()
    _enforce_prod_secrets()
    init_db()
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


app = FastAPI(
    title="Hugo — B2Box Catalog QC",
    version="0.1.0",
    description="Anti-duplicados + sincronización de precios para Vendure",
    lifespan=lifespan,
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
    auth.set_session_cookie(resp, request, identity or payload.username)
    return resp


@app.post("/api/logout", include_in_schema=False)
async def logout() -> JSONResponse:
    """Cierra la sesión borrando la cookie."""
    resp = JSONResponse({"ok": True})
    auth.clear_session_cookie(resp)
    return resp


app.include_router(router)


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
