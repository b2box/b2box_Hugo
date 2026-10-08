"""La migración liviana sobre un Postgres REAL, parecido al de producción.

Se salta sola si no hay `HUGO_TEST_PG_URL`. Para correrla:

    docker run --rm -d --name hugo-pg -e POSTGRES_PASSWORD=qa -p 55432:5432 postgres:16-alpine
    HUGO_TEST_PG_URL=postgresql+psycopg://postgres:qa@localhost:55432/postgres \\
        VENDURE_API_URL=https://example.invalid/admin-api pytest backend/tests/test_qa_postgres_migration.py

Arma las tablas del semáforo COMO ESTABAN antes de la rama (sin ninguna columna
nueva) con las fechas en `timestamptz` (lo que dejó sqlmodel 0.0.48 el 08-oct),
mete datos, corre `init_db()` y verifica columnas, defaults, la tabla nueva, que
el arreglo de timestamptz las deje naive y que se pueda escribir con el ORM.
Todo en un schema propio que se borra al final.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlalchemy import MetaData, Table, create_engine, inspect, text  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

from app.db import models, session  # noqa: E402,F401

PG_URL = os.environ.get("HUGO_TEST_PG_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL, reason="falta HUGO_TEST_PG_URL (Postgres descartable)")

NEW_SNAPSHOT_COLS = {"product_enabled", "match_origin", "similar_count", "similar_listings", "web_searches",
                     "web_bytes", "web_state", "our_specs"}
NEW_RUN_COLS = {"web_status", "web_searches", "web_bytes", "web_blocked", "n_web_ok", "n_con_similares"}


@pytest.fixture
def pg(monkeypatch):
    schema = "qa_" + uuid.uuid4().hex[:10]
    admin = create_engine(PG_URL)
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(PG_URL, connect_args={"options": f"-csearch_path={schema}"})
    monkeypatch.setattr(session, "engine", engine)
    yield engine
    engine.dispose()
    with admin.begin() as c:
        c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def _old_tables(engine) -> None:
    old = MetaData()
    for name, new_cols in (("price_monitor_run", NEW_RUN_COLS), ("market_price_snapshot", NEW_SNAPSHOT_COLS),
                           ("ml_seller_cache", set()), ("settings", set())):
        src = SQLModel.metadata.tables[name]
        Table(name, old, *[c._copy() for c in src.columns if c.name not in new_cols])
    old.create_all(engine)
    with engine.begin() as c:
        for table, cols in (("price_monitor_run", ("started_at", "finished_at")),
                            ("market_price_snapshot", ("captured_at",)), ("ml_seller_cache", ("fetched_at",))):
            for col in cols:
                c.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN "{col}" TYPE timestamp with time zone '
                               f'USING "{col}" AT TIME ZONE \'UTC\''))
        c.execute(text("INSERT INTO price_monitor_run (started_at, status, mode, trigger, total_products, processed, "
                       "n_ok, n_no_data, n_failed, n_skipped, n_verde, n_amarillo, n_rojo, n_sin_dato, "
                       "ml_requests_used, llm_calls, llm_input_tokens, llm_output_tokens, llm_cost_usd, resumed_count) "
                       "VALUES (now(), 'ok', 0, 'cron', 2, 2, 1, 1, 0, 0, 1, 0, 0, 1, 10, 0, 0, 0, 0, 0)"))
        for pid, st, col in (("11", "ok", "verde"), ("12", "no_data", "sin_dato")):
            c.execute(text("INSERT INTO market_price_snapshot (run_id, product_id, captured_at, ml_status, color, "
                           "ml_listing_count, ml_seller_count, candidates_count, ambiguous_count, commission_pct, "
                           "shipping_cents, ml_currency) VALUES (1, :p, now(), :s, :c, 1, 1, 1, 0, 13.0, 0, 'ARS')"),
                      {"p": pid, "s": st, "c": col})


def _dt_columns(engine):
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    return {(t, c["name"]): getattr(c["type"], "timezone", False)
            for t in ("price_monitor_run", "market_price_snapshot", "market_match_feedback", "ml_seller_cache")
            if t in existing
            for c in insp.get_columns(t) if "TIMESTAMP" in str(c["type"]).upper()}


def test_an_old_postgres_gets_the_new_columns_the_new_table_and_naive_dates(pg):
    _old_tables(pg)
    assert set(_dt_columns(pg).values()) == {True}                  # como lo dejó el deploy del 08-oct

    session.init_db()
    session.init_db()                                                # idempotente

    insp = inspect(pg)
    snap_cols = {c["name"] for c in insp.get_columns("market_price_snapshot")}
    run_cols = {c["name"] for c in insp.get_columns("price_monitor_run")}
    assert NEW_SNAPSHOT_COLS <= snap_cols and NEW_RUN_COLS <= run_cols
    assert "market_match_feedback" in insp.get_table_names()
    assert {i["name"]: bool(i["unique"]) for i in insp.get_indexes("market_match_feedback")} == {
        "ix_mmf_product_ml": True}
    # el arreglo de timestamptz las deja naive, también las de la tabla nueva
    after = _dt_columns(pg)
    assert ("market_match_feedback", "created_at") in after
    assert set(after.values()) == {False}
    with pg.connect() as c:
        rows = [tuple(r) for r in c.execute(text(
            "SELECT product_id, product_enabled, similar_count, web_searches, web_bytes, match_origin, web_state, "
            "ml_status, color FROM market_price_snapshot ORDER BY id"))]
        assert rows == [("11", True, 0, 0, 0, None, None, "ok", "verde"),
                        ("12", True, 0, 0, 0, None, None, "no_data", "sin_dato")]
        assert tuple(c.execute(text("SELECT status, web_searches, web_bytes, web_blocked, n_web_ok, n_con_similares, "
                                    "web_status FROM price_monitor_run")).one()) == ("ok", 0, 0, 0, 0, 0, None)


def test_the_orm_and_the_not_the_same_flow_work_on_postgres(pg):
    _old_tables(pg)
    session.init_db()
    import json

    from app.db.models import MarketMatchFeedback, MarketPriceSnapshot, PriceMonitorRun
    from app.pricing import match_feedback, price_monitor

    # lo de la rama usa su propio `engine` importado: se apunta también ahí
    for mod in (match_feedback, price_monitor):
        mod.engine = pg
    entry = {"ml_id": "MLA901", "title": "x", "origin": "web", "category": "igual", "source": "clip",
             "prices_cents": [25_000, 30_000], "median_cents": 27_500, "listings": 2, "sellers": ["a", "b"]}
    with Session(pg) as s:
        snap = s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.product_id == "11")).one()
        snap.matched_listings = json.dumps([entry])
        snap.our_price_cents, snap.ml_median_cents, snap.ml_min_cents, snap.match_origin = 10_000, 27_500, 25_000, "web"
        s.add(snap)
        s.commit()
        sid = snap.id
    assert match_feedback.add_feedback(product_id="11", ml_id="MLA901", entry=entry, snapshot_id=sid,
                                       product_name="p") is True
    assert match_feedback.add_feedback(product_id="11", ml_id="MLA901", entry=entry, snapshot_id=sid,
                                       product_name="p") is False                     # único (producto, ml_id)
    assert match_feedback.load_excluded() == {"11": frozenset({"MLA901"})}
    with Session(pg) as s:
        snap = s.get(MarketPriceSnapshot, sid)
        assert price_monitor.drop_listing(snap, "MLA901")["ml_id"] == "MLA901"
        s.add(snap)
        s.commit()
        s.refresh(snap)
        assert (snap.ml_status, snap.color, snap.ml_median_cents) == ("no_data", "sin_dato", None)
        assert s.exec(select(MarketMatchFeedback)).one().created_at.tzinfo is None
        run = s.exec(select(PriceMonitorRun)).one()
        assert run.started_at.tzinfo is None                                         # utcnow() naive compara bien


@pytest.mark.xfail(strict=True, reason="BUG-L7: web_bytes es INTEGER de 32 bits y 3,4 GB (sin bloquear scripts) "
                                       "lo desbordan con NumericValueOutOfRange")
def test_the_run_byte_counter_takes_a_night_without_blocked_scripts(pg):
    _old_tables(pg)
    session.init_db()
    from app.pricing import price_monitor

    price_monitor.engine = pg
    for _ in range(4):
        price_monitor._add_usage(1, web_bytes=900_000_000)                           # 3,6 GB en total
