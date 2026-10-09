"""Las tablas de producción del semáforo reciben las columnas de las tiendas con sus defaults,
y las tablas nuevas nacen con los índices únicos que hacen idempotente al indexador."""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402

from app.db import models, session  # noqa: E402,F401


def test_old_tables_get_the_store_columns_and_the_new_tables_are_created(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE market_price_snapshot (id INTEGER PRIMARY KEY, run_id INTEGER, "
                          "product_id VARCHAR(64), ml_status VARCHAR(16), color VARCHAR(16))"))
        conn.execute(text("INSERT INTO market_price_snapshot (run_id, product_id, ml_status, color) "
                          "VALUES (1, '7', 'ok', 'verde')"))
        conn.execute(text("CREATE TABLE price_monitor_run (id INTEGER PRIMARY KEY, status VARCHAR, processed INTEGER)"))
        conn.execute(text("INSERT INTO price_monitor_run (status, processed) VALUES ('ok', 10)"))
    monkeypatch.setattr(session, "engine", engine)

    SQLModel.metadata.create_all(engine)
    session._add_missing_columns()
    session._ensure_indexes()

    insp = inspect(engine)
    assert "price_basis" in {c["name"] for c in insp.get_columns("market_price_snapshot")}
    assert "source_stats" in {c["name"] for c in insp.get_columns("price_monitor_run")}
    with engine.connect() as conn:
        # Un snapshot viejo sacó su color solo de ML; una corrida vieja no tiene contadores por fuente.
        assert tuple(conn.execute(text("SELECT product_id, price_basis FROM market_price_snapshot")).one()) == ("7", "ml")
        assert conn.execute(text("SELECT source_stats FROM price_monitor_run")).scalar() is None

    for table in ("market_store", "store_catalog_item", "store_match", "store_match_feedback"):
        assert table in insp.get_table_names(), table
    unique = {t: {i["name"] for i in insp.get_indexes(t) if i["unique"]}
              for t in ("market_store", "store_catalog_item", "store_match", "store_match_feedback")}
    assert "ix_sci_store_url" in unique["store_catalog_item"]            # una URL por tienda
    assert "ix_sm_run_product_item" in unique["store_match"]             # la reanudación no duplica filas
    assert "ix_smf_product_store_item" in unique["store_match_feedback"]
    assert any("name" in n for n in unique["market_store"])              # no hay dos tiendas con el mismo nombre
