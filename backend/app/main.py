"""Entry point de Hugo.

Levanta FastAPI, inicializa la DB y arranca el scheduler.

Uso:
    uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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

app.include_router(router)


# ─── Dashboard estático ────────────────────────────────────────────


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    """Sirve el dashboard amigable de Hugo en la raíz."""
    return FileResponse(_STATIC_DIR / "index.html")


if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
