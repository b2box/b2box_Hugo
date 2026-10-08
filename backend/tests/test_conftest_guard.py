"""La suite nunca corre contra una DB real (ver tests/conftest.py)."""

from __future__ import annotations

import os
import tempfile

from sqlalchemy.engine import make_url

from app.db.session import engine
from tests import conftest


def test_the_suite_engine_is_the_temp_sqlite():
    assert engine.url.get_backend_name() == "sqlite"
    assert os.path.samefile(engine.url.database, conftest._DB_PATH)


def test_guard_rejects_anything_that_is_not_a_temp_sqlite():
    tmp = os.path.join(tempfile.gettempdir(), "x.sqlite3")
    assert conftest._engine_is_a_temp_sqlite(make_url(f"sqlite:///{tmp}")) is True
    assert conftest._engine_is_a_temp_sqlite(make_url("sqlite:///./hugo.db")) is False
    assert conftest._engine_is_a_temp_sqlite(make_url("sqlite://")) is False
    assert conftest._engine_is_a_temp_sqlite(
        make_url("postgresql+psycopg://u:p@db.supabase.co:5432/postgres")) is False
