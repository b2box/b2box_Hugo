"""Contador diario con reserva atómica (app.pricing.daily_budget), genérico.

Lo comparten OTAPI, el budget de ML del semáforo y el tope del juez LLM. La
mecánica de CAS y carreras está cubierta en test_otapi_budget.py; acá, lo que
agrega el refactor: claves independientes por consumidor y fail-closed.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".sqlite3")
os.close(_DB_FD)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB_PATH}")

import pytest  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

from app.db.models import Setting  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import daily_budget  # noqa: E402


@pytest.fixture
def clean_settings():
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        for row in s.exec(select(Setting)).all():
            s.delete(row)
        s.commit()
    yield


def test_each_consumer_has_its_own_counter(clean_settings):
    assert daily_budget.reserve("_meta:a", 2) == 1
    assert daily_budget.reserve("_meta:a", 2) == 2
    assert daily_budget.reserve("_meta:a", 2) is None
    assert daily_budget.reserve("_meta:b", 2) == 1  # otro consumidor, otro cupo
    assert daily_budget.used_today("_meta:a") == 2


def test_yesterday_counter_starts_over(clean_settings):
    with Session(engine) as s:
        s.add(Setting(key="_meta:a", value="2000-01-01:999"))
        s.commit()
    assert daily_budget.reserve("_meta:a", 5) == 1


def test_zero_budget_never_reserves(clean_settings):
    assert daily_budget.reserve("_meta:a", 0) is None


def test_db_down_is_fail_closed(clean_settings, monkeypatch):
    class Broken:
        def __init__(self, *a, **kw):
            raise RuntimeError("la base no responde")

    monkeypatch.setattr(daily_budget, "Session", Broken)
    assert daily_budget.reserve("_meta:a", 100) is None
    assert daily_budget.used_today("_meta:a") == 0
