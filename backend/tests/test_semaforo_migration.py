"""La migración liviana agrega las columnas nuevas del semáforo a las tablas que
ya existen en producción, sin tocar lo que había y con los defaults correctos
(un snapshot viejo era de un producto habilitado, sin similares, sin web)."""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402

from app.db import models, session  # noqa: E402,F401


def test_old_semaforo_tables_get_the_new_columns_with_defaults(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        # Las tablas como estaban antes de esta etapa (sin ninguna columna nueva).
        conn.execute(text(
            "CREATE TABLE market_price_snapshot (id INTEGER PRIMARY KEY, run_id INTEGER, "
            "product_id VARCHAR(64), captured_at DATETIME, ml_status VARCHAR(16), color VARCHAR(16), "
            "ml_listing_count INTEGER, ml_seller_count INTEGER, candidates_count INTEGER, "
            "ambiguous_count INTEGER)"))
        conn.execute(text(
            "INSERT INTO market_price_snapshot (run_id, product_id, ml_status, color, ml_listing_count, "
            "ml_seller_count, candidates_count, ambiguous_count) VALUES (1, '7', 'ok', 'verde', 2, 2, 3, 0)"))
        conn.execute(text(
            "CREATE TABLE price_monitor_run (id INTEGER PRIMARY KEY, status VARCHAR, processed INTEGER, "
            "llm_calls INTEGER)"))
        conn.execute(text("INSERT INTO price_monitor_run (status, processed, llm_calls) VALUES ('ok', 10, 0)"))
    monkeypatch.setattr(session, "engine", engine)

    SQLModel.metadata.create_all(engine)          # crea market_match_feedback (tabla nueva)
    session._add_missing_columns()
    session._ensure_indexes()

    cols = {c["name"] for c in inspect(engine).get_columns("market_price_snapshot")}
    assert {"product_enabled", "match_origin", "similar_count", "similar_listings", "web_searches",
            "web_bytes", "web_state", "our_specs"} <= cols
    run_cols = {c["name"] for c in inspect(engine).get_columns("price_monitor_run")}
    assert {"web_status", "web_searches", "web_bytes", "web_blocked", "n_web_ok", "n_con_similares"} <= run_cols
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT product_id, ml_status, product_enabled, similar_count, web_searches, web_bytes, "
            "match_origin, web_state FROM market_price_snapshot")).one()
        assert tuple(row) == ("7", "ok", 1, 0, 0, 0, None, None)     # habilitado, sin similares ni web
        run = conn.execute(text(
            "SELECT status, web_searches, web_bytes, web_blocked, n_web_ok FROM price_monitor_run")).one()
        assert tuple(run) == ("ok", 0, 0, 0, 0)

    idx = {i["name"]: i for i in inspect(engine).get_indexes("market_match_feedback")}
    assert idx["ix_mmf_product_ml"]["unique"] in (1, True)
