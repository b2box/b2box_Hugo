"""init_db respeta los índices únicos aunque la tabla ya exista (QA bug 9).

El cursor de reanudación del semáforo se apoya en el único
(run_id, product_id) de market_price_snapshot: si quedara común, una
corrida retomada podría duplicar snapshots.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import inspect, text
from sqlmodel import Session, select

from app.db.models import MarketPriceSnapshot
from app.db.session import _ensure_indexes, engine, init_db

IDX = "ix_mps_run_product"


def _unique() -> bool | None:
    for ix in inspect(engine).get_indexes("market_price_snapshot"):
        if ix["name"] == IDX:
            return bool(ix["unique"])
    return None


def _replace_with_plain_index() -> None:
    with engine.begin() as conn:
        conn.execute(text(f'DROP INDEX IF EXISTS "{IDX}"'))
        conn.execute(text(f'CREATE INDEX "{IDX}" ON market_price_snapshot (run_id, product_id)'))


@pytest.fixture(autouse=True)
def _restore():
    init_db()
    yield
    with Session(engine) as s:
        for row in s.exec(select(MarketPriceSnapshot)).all():
            s.delete(row)
        s.commit()
    with engine.begin() as conn:
        conn.execute(text(f'DROP INDEX IF EXISTS "{IDX}"'))
    _ensure_indexes()
    assert _unique() is True


def test_a_missing_unique_index_is_created_unique():
    with engine.begin() as conn:
        conn.execute(text(f'DROP INDEX IF EXISTS "{IDX}"'))
    assert _unique() is None
    _ensure_indexes()
    assert _unique() is True


def test_a_plain_index_left_by_the_old_migration_is_recreated_unique():
    _replace_with_plain_index()
    assert _unique() is False
    init_db()
    assert _unique() is True


def test_duplicates_keep_the_plain_index_and_do_not_break_startup(caplog):
    _replace_with_plain_index()
    with Session(engine) as s:
        s.add(MarketPriceSnapshot(run_id=999, product_id="P1"))
        s.add(MarketPriceSnapshot(run_id=999, product_id="P1"))
        s.commit()
    with caplog.at_level(logging.ERROR, logger="app.db.session"):
        init_db()  # no revienta
    assert _unique() is False
    assert "filas duplicadas" in caplog.text
