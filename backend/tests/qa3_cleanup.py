"""Limpieza de los tests de QA3: dejan la base como la encontraron.

Varios tests de la suite viven de que las tablas del semáforo empiecen vacías y de que no queden settings editados; los de QA3
escriben mucho (simulaciones de noches, corridas enteras, cupos) y sin esto contaminaban lo que corre después.
Se importa como fixture autouse: `from tests.qa3_cleanup import qa3_clean  # noqa: F401`."""

from __future__ import annotations

import pytest
from sqlmodel import Session, select


@pytest.fixture(autouse=True)
def qa3_clean():
    yield
    from app import runtime
    from app.db.models import (
        MarketMatchFeedback,
        MarketPriceSnapshot,
        MlSellerCache,
        MlWebResult,
        PriceMonitorRun,
        Setting,
    )
    from app.db.session import engine

    with Session(engine) as s:
        for model in (MlWebResult, MarketPriceSnapshot, PriceMonitorRun, MarketMatchFeedback, MlSellerCache):
            for row in s.exec(select(model)).all():
                s.delete(row)
        for row in s.exec(select(Setting)).all():
            s.delete(row)
        s.commit()
    runtime.invalidate()
