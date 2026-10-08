"""Configuración común de la suite: la DB es SIEMPRE una SQLite temporal.

Varios tests vacían tablas enteras (settings, audit_log, snapshots…) para
arrancar limpios. Si el entorno trajera un DATABASE_URL real —un `.env` de
prod, una variable exportada en la terminal— la suite lo borraría. Por eso se
fuerza una SQLite nueva en el directorio temporal ANTES de que cualquier test
importe la app (este conftest se carga primero), y la sesión se aborta si el
engine no quedó apuntando ahí.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP_DIR = Path(tempfile.gettempdir()).resolve()
_fd, _DB_PATH = tempfile.mkstemp(prefix="hugo-tests-", suffix=".sqlite3")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")


def _engine_is_a_temp_sqlite(url) -> bool:
    if url.get_backend_name() != "sqlite" or not url.database:
        return False
    return _TMP_DIR in Path(url.database).resolve().parents


def pytest_sessionstart(session):  # noqa: ARG001
    from app.db.session import engine

    if not _engine_is_a_temp_sqlite(engine.url):
        pytest.exit(
            "La suite solo corre contra una SQLite temporal y el engine apunta a "
            f"{engine.url.render_as_string(hide_password=True)}. Abortado para no borrar datos reales.",
            returncode=3,
        )


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            os.remove(_DB_PATH + suffix)
        except OSError:
            pass
