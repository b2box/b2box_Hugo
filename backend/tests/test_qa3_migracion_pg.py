"""QA3, criterio 6: la migración del buscador de la oficina y de las variantes, sobre Postgres 16 descartable, partiendo del
esquema REAL de origin/main (e8209e1: ya con tiendas, price_basis y source_stats), no del de 77645f9 que usa la suite de la rama.

El esquema de partida (`golden/pg_schema_origin_main_e8209e1.sql`) salió de un `pg_dump -s` de una base creada por el `init_db()` de
un worktree de e8209e1. Se salta sola sin HUGO_TEST_PG_URL.

  * el arranque agrega EXACTAMENTE: la tabla ml_web_result (+ su índice único), 2 columnas a market_price_snapshot y 3 a
    price_monitor_run; no toca nada más ni cambia el tipo de ninguna columna;
  * idempotente (3 arranques = mismo esquema), no reescribe la tabla grande (mismo filenode) y conserva todas las filas;
  * el código anterior sigue insertando sobre el esquema nuevo (columnas nuevas sin NOT NULL);
  * el SQL de rollback que trae el README, copiado tal cual del README, deja el esquema EXACTAMENTE como estaba, y volver a subir
    da el mismo resultado.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import psycopg  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402

from app.db import session  # noqa: E402
from tests.test_qa_tiendas_pg import PG_URL, _patch_engine, _pg_dsn, _schema_url  # noqa: E402

pytestmark = pytest.mark.skipif(not PG_URL, reason="falta HUGO_TEST_PG_URL (Postgres descartable)")
SCHEMA_SQL = (Path(__file__).parent / "golden" / "pg_schema_origin_main_e8209e1.sql").read_text()
README = (Path(__file__).resolve().parents[2] / "README.md").read_text()

NEW_SNAPSHOT_COLS = {"ml_variant", "web_via"}
NEW_RUN_COLS = {"variant_stats", "oficina_fresh", "n_oficina_ok"}


def _rollback_sql() -> str:
    for block in re.findall(r"```sql\n(.*?)```", README, flags=re.S):
        if "DROP TABLE IF EXISTS ml_web_result" in block:
            return block
    raise AssertionError("el README no trae el SQL de rollback")


def _shape(engine) -> dict:
    insp = inspect(engine)
    out = {}
    for table in sorted(insp.get_table_names()):
        cols = {c["name"]: (str(c["type"]), c["nullable"], str(c.get("default"))) for c in insp.get_columns(table)}
        idx = {i["name"]: (tuple(i["column_names"]), bool(i["unique"])) for i in insp.get_indexes(table)}
        out[table] = (cols, idx)
    return out


@pytest.fixture
def old_schema(monkeypatch):
    schema = "mig_" + uuid.uuid4().hex[:10]
    with psycopg.connect(_pg_dsn(PG_URL), autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
        c.execute(f'SET search_path TO "{schema}"')
        c.execute(SCHEMA_SQL)
    engine = create_engine(_schema_url(schema), pool_pre_ping=True)
    _patch_engine(monkeypatch, engine)
    yield engine
    engine.dispose()
    with psycopg.connect(_pg_dsn(PG_URL), autocommit=True) as c:
        c.execute(f'DROP SCHEMA "{schema}" CASCADE')


def _seed_old_rows(engine) -> None:
    with engine.begin() as c:
        c.execute(text("INSERT INTO price_monitor_run (started_at, status, mode, trigger, total_products, processed, n_ok, n_no_data, "
                       "n_failed, n_skipped, n_verde, n_amarillo, n_rojo, n_sin_dato, ml_requests_used, llm_calls, llm_input_tokens, "
                       "llm_output_tokens, llm_cost_usd, resumed_count, web_searches, web_bytes, web_blocked, n_web_ok, n_con_similares, "
                       "n_est_verde, n_est_amarillo, n_est_rojo, n_solo_diferentes, source_stats) SELECT now(), 'ok', 0, 'cron', 200, 200, "
                       "1, 2, 0, 0, 1, 0, 0, 2, 40, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, '{\"x\": 1}' FROM generate_series(1, 5)"))
        c.execute(text("INSERT INTO market_price_snapshot (run_id, product_id, captured_at, ml_status, color, ml_listing_count, ml_seller_count, "
                       "candidates_count, ambiguous_count, commission_pct, shipping_cents, ml_currency, product_enabled, similar_count, "
                       "web_searches, web_bytes, other_count, estimated_listing_count, our_price_cents, product_name, price_basis, "
                       "matched_listings, ml_median_cents, match_origin) SELECT 1 + (g - 1) / 20, ((g - 1) % 20 + 1)::text, now(), "
                       "CASE WHEN g % 3 = 0 THEN 'ok' ELSE 'no_data' END, 'verde', 0, 0, 0, 0, 13.0, 0, 'ARS', true, 0, 0, 0, 0, 0, 10000, "
                       "'Producto ' || g, 'ml', '[{\"ml_id\": \"MLA1\"}]', 25000, 'api' FROM generate_series(1, 100) g"))


def _old_columns_dump(engine) -> list:
    with engine.connect() as c:
        snaps = c.execute(text("SELECT id, run_id, product_id, ml_status, color, ml_median_cents, match_origin, matched_listings, "
                               "product_name, price_basis FROM market_price_snapshot ORDER BY id")).all()
        runs = c.execute(text("SELECT id, status, total_products, n_ok, ml_requests_used, source_stats FROM price_monitor_run ORDER BY id")).all()
    return [list(map(tuple, snaps)), list(map(tuple, runs))]


def test_the_starting_schema_is_the_one_of_origin_main(old_schema):
    shape = _shape(old_schema)
    assert "ml_web_result" not in shape
    assert not NEW_SNAPSHOT_COLS & set(shape["market_price_snapshot"][0]) and not NEW_RUN_COLS & set(shape["price_monitor_run"][0])
    assert {"price_basis"} <= set(shape["market_price_snapshot"][0]) and "source_stats" in shape["price_monitor_run"][0]
    assert {"market_store", "store_catalog_item", "store_match"} <= set(shape)


def test_boot_adds_exactly_the_new_objects_and_changes_nothing_else(old_schema):
    before = _shape(old_schema)
    session.init_db()
    after = _shape(old_schema)
    assert set(after) - set(before) == {"ml_web_result"} and set(before) <= set(after)
    for table in before:
        b_cols, b_idx = before[table]
        a_cols, a_idx = after[table]
        added_cols = set(a_cols) - set(b_cols)
        assert {k: v for k, v in a_cols.items() if k in b_cols} == b_cols, f"{table}: cambió una columna que ya existía"
        assert a_idx == b_idx, f"{table}: cambió un índice"
        assert added_cols == {"market_price_snapshot": NEW_SNAPSHOT_COLS, "price_monitor_run": NEW_RUN_COLS}.get(table, set()), table
    cols, idx = after["ml_web_result"]
    assert idx["ix_mwr_product_fetched"] == (("product_id", "fetched_at"), True)
    assert "timestamp" in cols["fetched_at"][0].lower() and "WITH TIME ZONE" not in cols["fetched_at"][0].upper()
    for table, names in (("market_price_snapshot", NEW_SNAPSHOT_COLS), ("price_monitor_run", NEW_RUN_COLS)):
        assert all(after[table][0][n][1] for n in names), "las columnas nuevas tienen que aceptar NULL (rollback de código seguro)"


def test_three_boots_leave_the_same_schema_and_never_touch_the_rows(old_schema):
    _seed_old_rows(old_schema)
    rows_before = _old_columns_dump(old_schema)
    session.init_db()
    first = _shape(old_schema)
    session.init_db()
    session.init_db()
    assert _shape(old_schema) == first
    assert _old_columns_dump(old_schema) == rows_before
    with old_schema.connect() as c:
        assert c.execute(text("SELECT count(*) FROM market_price_snapshot WHERE ml_variant IS NOT NULL OR web_via IS NOT NULL")).scalar() == 0
        assert c.execute(text("SELECT DISTINCT oficina_fresh || '/' || n_oficina_ok FROM price_monitor_run")).scalars().all() == ["0/0"]
        assert c.execute(text("SELECT count(*) FROM price_monitor_run WHERE variant_stats IS NOT NULL")).scalar() == 0


def test_the_big_table_is_not_rewritten_and_the_boot_is_fast(old_schema):
    with old_schema.begin() as c:
        c.execute(text("INSERT INTO market_price_snapshot (run_id, product_id, captured_at, ml_status, color, ml_listing_count, ml_seller_count, "
                       "candidates_count, ambiguous_count, commission_pct, shipping_cents, ml_currency, product_enabled, similar_count, "
                       "web_searches, web_bytes, other_count, estimated_listing_count, our_price_cents, product_name, price_basis) "
                       "SELECT 1 + (g - 1) / 200, ((g - 1) % 200 + 1)::text, now(), 'no_data', 'sin_dato', 0, 0, 0, 0, 13.0, 0, 'ARS', true, "
                       "0, 0, 0, 0, 0, 10000, 'Producto', 'ml' FROM generate_series(1, 200000) g"))
    with old_schema.connect() as c:
        node = c.execute(text("SELECT pg_relation_filenode('market_price_snapshot')")).scalar()
    t0 = time.perf_counter()
    session.init_db()
    elapsed = time.perf_counter() - t0
    with old_schema.connect() as c:
        assert c.execute(text("SELECT pg_relation_filenode('market_price_snapshot')")).scalar() == node, "ALTER reescribió la tabla"
        assert c.execute(text("SELECT count(*) FROM market_price_snapshot")).scalar() == 200_000
    assert elapsed < 5.0, f"el arranque tardó {elapsed:.1f} s con 200.000 snapshots"


def test_the_previous_code_keeps_inserting_and_reading_on_the_upgraded_schema(old_schema):
    session.init_db()
    _seed_old_rows(old_schema)                       # lo mismo que haría el código anterior: sin las columnas nuevas
    with old_schema.connect() as c:
        row = c.execute(text("SELECT ml_variant, web_via FROM market_price_snapshot LIMIT 1")).one()
        run = c.execute(text("SELECT variant_stats, oficina_fresh, n_oficina_ok FROM price_monitor_run LIMIT 1")).one()
    assert tuple(row) == (None, None) and tuple(run) == (None, None, None)


def test_readme_rollback_sql_restores_the_original_schema_and_boot_goes_back_up(old_schema):
    before = _shape(old_schema)
    _seed_old_rows(old_schema)
    rows = _old_columns_dump(old_schema)
    session.init_db()
    up = _shape(old_schema)
    sql = _rollback_sql()
    with old_schema.begin() as c:
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            c.execute(text(stmt))
    assert _shape(old_schema) == before, "el SQL del README no deja el esquema como estaba"
    assert _old_columns_dump(old_schema) == rows, "el rollback no pierde datos viejos"
    session.init_db()
    assert _shape(old_schema) == up
