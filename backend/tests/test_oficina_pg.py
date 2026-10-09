"""Buscador de la oficina y variantes de búsqueda sobre Postgres 16 descartable.

Se salta sola sin `HUGO_TEST_PG_URL`:

    docker run -d --name hugo-oficina-pgtest -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=hugo_test -p 55499:5432 postgres:16-alpine
    HUGO_TEST_PG_URL=postgresql+psycopg://postgres:pw@localhost:55499/hugo_test pytest backend/tests/test_oficina_pg.py
    docker rm -f hugo-oficina-pgtest

Lo que SQLite no hace cumplir: columnas nuevas sin NOT NULL para que el código anterior siga insertando, el índice único de
`ml_web_result` (idempotencia), fechas sin zona, agregados (`max`, `distinct`) de la cola y la corrida de punta a punta.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import psycopg  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import runtime  # noqa: E402
from app.clock import utcnow  # noqa: E402
from app.db import session  # noqa: E402
from app.db.models import MarketPriceSnapshot, MlWebResult  # noqa: E402
from app.pricing import oficina_ml, price_monitor  # noqa: E402
from tests.test_oficina_api import _card, _res  # noqa: E402
from tests.test_oficina_semaforo import _wire  # noqa: E402
from tests.test_price_monitor import FakeVendure, _product, _runs, _snaps, world  # noqa: E402,F401
from tests.test_qa2_tiendas_pg_migracion import BASE_SQL, _old_rows  # noqa: E402
from tests.test_qa_tiendas_pg import _patch_engine, _pg_dsn, _schema_url  # noqa: E402
from tests.test_semaforo_web import _score, webw  # noqa: E402,F401

PG_URL = os.environ.get("HUGO_TEST_PG_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL, reason="falta HUGO_TEST_PG_URL (Postgres descartable)")
NEW_SNAPSHOT_COLS = {"ml_variant", "web_via"}
NEW_RUN_COLS = {"variant_stats", "oficina_fresh", "n_oficina_ok"}


def _new_schema(monkeypatch, *, base: bool):
    schema = "ofi_" + uuid.uuid4().hex[:10]
    with psycopg.connect(_pg_dsn(PG_URL), autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
        if base:
            c.execute(f'SET search_path TO "{schema}"')
            c.execute(BASE_SQL)                                       # el esquema de origin/main (77645f9)
    engine = create_engine(_schema_url(schema), pool_pre_ping=True)
    _patch_engine(monkeypatch, engine)
    runtime.invalidate()
    return schema, engine


def _drop(schema: str, engine) -> None:
    engine.dispose()
    with psycopg.connect(_pg_dsn(PG_URL), autocommit=True) as c:
        c.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def pg_base(monkeypatch):
    """El esquema de origin/main con una corrida y dos snapshots cargados por el código de entonces, SIN migrar."""
    schema, engine = _new_schema(monkeypatch, base=True)
    with engine.begin() as c:
        c.execute(text("INSERT INTO price_monitor_run (started_at, status, mode, trigger, total_products, processed, n_ok, n_no_data, "
                       "n_failed, n_skipped, n_verde, n_amarillo, n_rojo, n_sin_dato, ml_requests_used, llm_calls, llm_input_tokens, "
                       "llm_output_tokens, llm_cost_usd, resumed_count, web_searches, web_bytes, web_blocked, n_web_ok, n_con_similares, "
                       "n_est_verde, n_est_amarillo, n_est_rojo, n_solo_diferentes) VALUES ('2026-10-08 06:00:00', 'ok', 0, 'cron', 2, 2, "
                       "0, 2, 0, 0, 0, 0, 0, 2, 12, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)"))
        for pid in ("11", "12"):
            c.execute(text("INSERT INTO market_price_snapshot (run_id, product_id, captured_at, ml_status, color, ml_listing_count, "
                           "ml_seller_count, candidates_count, ambiguous_count, commission_pct, shipping_cents, ml_currency, product_enabled, "
                           "similar_count, web_searches, web_bytes, other_count, estimated_listing_count, our_price_cents, product_name) "
                           "VALUES (1, :p, '2026-10-08 06:10:00', 'no_data', 'sin_dato', 0, 0, 0, 0, 13.0, 0, 'ARS', true, 0, 0, 0, 0, 0, "
                           "10000, :n)"), {"p": pid, "n": f"Organizador cocina {pid}"})
    yield engine
    _drop(schema, engine)


@pytest.fixture
def pg(pg_base):
    session.init_db()
    yield pg_base


# ─── la migración ───────────────────────────────────────────────────────────


def test_upgrade_adds_nullable_columns_the_table_and_the_unique_index_without_touching_old_rows(pg_base):
    before = _old_rows(pg_base)
    session.init_db()
    session.init_db()                                                     # idempotente
    insp = inspect(pg_base)
    snap = {c["name"]: c for c in insp.get_columns("market_price_snapshot")}
    run = {c["name"]: c for c in insp.get_columns("price_monitor_run")}
    assert NEW_SNAPSHOT_COLS <= set(snap) and NEW_RUN_COLS <= set(run)
    assert all(snap[c]["nullable"] for c in NEW_SNAPSHOT_COLS) and all(run[c]["nullable"] for c in NEW_RUN_COLS), \
        "agregadas sin NOT NULL: el código anterior puede insertar sin ellas (rollback seguro)"
    assert "ml_web_result" in insp.get_table_names()
    assert {i["name"] for i in insp.get_indexes("ml_web_result") if i["unique"]} == {"ix_mwr_product_fetched"}
    cols = {c["name"]: c for c in insp.get_columns("ml_web_result")}
    assert not getattr(cols["fetched_at"]["type"], "timezone", False) and not getattr(cols["received_at"]["type"], "timezone", False)
    assert cols["candidates"]["nullable"] is False and cols["product_id"]["type"].length == 64
    with pg_base.connect() as c:
        assert c.execute(text("select distinct oficina_fresh from price_monitor_run")).scalars().all() == [0], "backfill a 0"
        assert c.execute(text("select distinct n_oficina_ok from price_monitor_run")).scalars().all() == [0]
        assert c.execute(text("select count(*) from market_price_snapshot where ml_variant is not null")).scalar() == 0
    assert _old_rows(pg_base) == before


def test_the_previous_code_keeps_inserting_on_the_upgraded_schema(pg):
    with pg.begin() as c:
        c.execute(text("INSERT INTO price_monitor_run (started_at, status, mode, trigger, total_products, processed, n_ok, n_no_data, "
                       "n_failed, n_skipped, n_verde, n_amarillo, n_rojo, n_sin_dato, ml_requests_used, llm_calls, llm_input_tokens, "
                       "llm_output_tokens, llm_cost_usd, resumed_count, web_searches, web_bytes, web_blocked, n_web_ok, n_con_similares, "
                       "n_est_verde, n_est_amarillo, n_est_rojo, n_solo_diferentes) VALUES (now(), 'ok', 0, 'cron', 1, 1, 0, 1, 0, 0, 0, "
                       "0, 0, 1, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)"))
    with Session(pg) as s:
        run = s.exec(select(price_monitor.PriceMonitorRun).order_by(price_monitor.PriceMonitorRun.id.desc())).first()
        d = price_monitor.run_to_dict(run)
    assert d["variants"] == {} and d["oficina"] == {"fresh": 0, "n_ok": 0}            # NULL se sirve como 0


# ─── guardar, la cola y lo fresco ───────────────────────────────────────────


def _iso(delta: timedelta = timedelta(0)) -> str:
    return (utcnow() + delta).replace(microsecond=0).isoformat() + "Z"


def test_ingest_is_idempotent_on_postgres_and_the_queue_follows(pg):
    first = oficina_ml.ingest([_res("11", [_card("MLA111"), _card("MLA222")], fetched_at=_iso(-timedelta(minutes=5)))])
    assert (first.stored, first.duplicates) == (1, 0)
    again = oficina_ml.ingest([_res("11", [_card("MLA111")], fetched_at=_iso(-timedelta(minutes=5)))])
    assert (again.stored, again.duplicates) == (0, 1)
    with Session(pg) as s:
        rows = list(s.exec(select(MlWebResult)))
    assert len(rows) == 1 and rows[0].status == "ok" and rows[0].n_candidates == 2
    assert not getattr(type(rows[0]).__table__.c.fetched_at.type, "timezone", False)
    assert [i["product_id"] for i in oficina_ml.build_queue(10)] == ["12"]             # el 11 ya está buscado
    assert oficina_ml.status()["fresh_products"] == 1


def test_a_lost_race_is_a_duplicate_on_postgres_too(pg):
    now = utcnow()
    oficina_ml.ingest([_res("11", fetched_at=_iso(-timedelta(minutes=9)))])
    a = oficina_ml.clean_result(_res("11", fetched_at=_iso(-timedelta(minutes=9))), now=now, max_candidates=8)
    b = oficina_ml.clean_result(_res("11", fetched_at=_iso(-timedelta(minutes=2))), now=now, max_candidates=8)
    report = oficina_ml.IngestReport()
    with Session(pg) as s:
        oficina_ml._insert(s, [a, b], report)
    assert (report.stored, report.duplicates) == (1, 1)


def test_hostile_values_are_stored_clean_on_postgres(pg):
    """Postgres no acepta NUL ni sustitutos sueltos en un texto: el saneo los saca antes de llegar a la base."""
    report = oficina_ml.ingest([_res("11", [_card("MLA5", name="Hola\x00 mundo \ud800 x", seller="s\x00" * 50, brand="\x00"),
                                            _card("MLA6", price_cents=10**30)], query="consulta\x00 rara")])
    assert report.stored == 1 and report.rejected == []
    [row] = oficina_ml.load_fresh().values()
    assert row.query == "consulta rara" and [c.name for c in row.candidates][0] == "Hola mundo x"
    assert row.candidates[1].price_cents is None


def test_load_fresh_takes_the_newest_ok_or_empty_per_product(pg):
    oficina_ml.ingest([_res("11", [_card("MLA1")], fetched_at=_iso(-timedelta(days=3))),
                       _res("11", [_card("MLA2")], fetched_at=_iso(-timedelta(hours=2))),
                       _res("11", [], status="blocked", fetched_at=_iso(-timedelta(minutes=1))),
                       _res("12", [_card("MLA3")], fetched_at=_iso(-timedelta(days=8)))])
    fresh = oficina_ml.load_fresh()
    assert set(fresh) == {"11"} and [c.id for c in fresh["11"].candidates] == ["MLA2"]
    assert [i["product_id"] for i in oficina_ml.build_queue(10)] == ["12"]             # 12 vencido; 11 buscado hace 2 h


# ─── el semáforo de punta a punta ───────────────────────────────────────────


async def test_the_run_uses_the_oficina_result_on_postgres(pg, webw):
    FakeVendure.products = [_product("3", "Producto raro")]
    with Session(pg) as s:                                                             # el producto tiene que existir en el semáforo
        s.add(MarketPriceSnapshot(run_id=1, product_id="3", ml_status="no_data", product_name="Producto raro"))
        s.commit()
    report = oficina_ml.ingest([_res("3", [_wire("MLA901", "Producto Raro", 250.0), _wire("MLA902", "Producto Raro", 300.0, seller="Dos")],
                                     fetched_at=_iso(-timedelta(hours=1)))])
    assert report.stored == 1
    _score(webw, "MLA901", 0.91)
    _score(webw, "MLA902", 0.88)
    await price_monitor.run_price_monitor()
    snap = _snaps()["3"]
    assert snap.ml_status == "ok" and snap.match_origin == "oficina" and snap.web_via == "oficina" and snap.ml_median_cents == 27_500
    run = _runs()[-1]
    assert (run.n_oficina_ok, run.oficina_fresh) == (1, 1) and webw.web.calls == []


async def test_variants_are_recorded_on_postgres(pg, world):
    from tests.test_price_monitor import _candidate, _set
    FakeVendure.products = [_product("7", "Organizador Doble Ajustable 3 Niveles 40x30 Blanco")]
    _set("pm_ml_query_variants", 3)
    world.ml.search.clear()
    world.ml.search["Organizador Doble Ajustable Niveles"] = [_candidate("MLA1", "Organizador doble ajustable")]
    await price_monitor.run_price_monitor()
    snap = _snaps()["7"]
    assert snap.ml_status == "ok" and snap.ml_variant == "corto"
    assert json.loads(_runs()[-1].variant_stats) == {"corto": 1}
