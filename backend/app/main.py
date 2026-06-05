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


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    _configure_logging()
    init_db()
    register_jobs()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="Hugo — B2Box Catalog QC",
    version="0.1.0",
    description="Anti-duplicados + sincronización de precios para Vendure",
    lifespan=lifespan,
)

# ─── Login del dashboard (cookie de sesión) ────────────────────────
# El middleware protege todo salvo /login, /logout, /health, /static y /verify.
app.middleware("http")(auth.auth_middleware)


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


@app.get("/login", include_in_schema=False)
async def login_page() -> FileResponse:
    """Pantalla de login."""
    return FileResponse(_STATIC_DIR / "login.html")


@app.post("/login", include_in_schema=False)
async def login(payload: LoginRequest, request: Request) -> JSONResponse:
    """Valida credenciales y, si son correctas, setea la cookie de sesión."""
    if not auth.login_enabled():
        # Login deshabilitado (sin password en .env): dejamos pasar.
        return JSONResponse({"ok": True, "disabled": True})
    if not auth.check_credentials(payload.username, payload.password):
        return JSONResponse({"ok": False, "detail": "Usuario o contraseña incorrectos"}, status_code=401)
    resp = JSONResponse({"ok": True})
    auth.set_session_cookie(resp, request, payload.username)
    return resp


@app.post("/logout", include_in_schema=False)
async def logout() -> JSONResponse:
    """Cierra la sesión borrando la cookie."""
    resp = JSONResponse({"ok": True})
    auth.clear_session_cookie(resp)
    return resp


app.include_router(router)


# ─── Dashboard estático ────────────────────────────────────────────


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    """Sirve el dashboard amigable de Hugo en la raíz."""
    return FileResponse(_STATIC_DIR / "index.html")


if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
