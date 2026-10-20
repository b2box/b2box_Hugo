"""QA: la migración del estimado (`estimate_usage`, `lookup_estimate`) en SQLite y Postgres 16.

La parte de SQLite corre siempre; la de Postgres se salta sin HUGO_TEST_PG_URL:

    docker run --rm -d --name qa-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=hugo_test -p 55497:5432 postgres:16-alpine
    HUGO_TEST_PG_URL=postgresql+psycopg://postgres:pw@localhost:55497/hugo_test pytest backend/tests/test_qa_estimate_migracion.py

Esquema de partida: TODAS las tablas del modelo menos las dos nuevas (la parte 3
no cambia ninguna otra). Se verifica:

  * el arranque (`init_db`) agrega EXACTAMENTE las dos tablas, con sus índices, y no toca nada más;
  * es idempotente;
  * el SQL de rollback del README, copiado tal cual, deja el esquema como estaba;
  * el cupo (`limits.reserve`) es atómico con 20 hilos a la vez (nunca pasa del tope);
  * el registro (`store.save`) y las podas funcionan.
"""

from __future__ import annotations

import os
import re
import threading
import uuid
from datetime import timedelta
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import psycopg  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

from app.clock import utcnow  # noqa: E402
from app.db import models, session  # noqa: E402
from app.db.models import EstimateUsage, LookupEstimate  # noqa: E402
from app.estimate import limits, store  # noqa: E402
from tests.test_qa_tiendas_pg import PG_URL, _patch_engine, _pg_dsn, _schema_url  # noqa: E402

README = (Path(__file__).resolve().parents[2] / "README.md").read_text()
NEW_TABLES = {"estimate_usage", "lookup_estimate"}


def _rollback_sql() -> str:
    for block in re.findall(r"```sql\n(.*?)```", README, flags=re.S):
        if "DROP TABLE IF EXISTS lookup_estimate" in block:
            return block
    raise AssertionError("el README no trae el SQL de rollback del estimado")


def _shape(engine) -> dict:
    insp = inspect(engine)
    out = {}
    for table in sorted(insp.get_table_names()):
        cols = {c["name"]: (str(c["type"]), c["nullable"]) for c in insp.get_columns(table)}
        idx = {i["name"]: (tuple(i["column_names"]), bool(i["unique"])) for i in insp.get_indexes(table)}
        out[table] = (cols, idx)
    return out


def _old_tables():
    models  # noqa: B018  (las clases tienen que estar registradas)
    return [t for t in SQLModel.metadata.sorted_tables if t.name not in NEW_TABLES]


@pytest.fixture
def sqlite_old(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.sqlite3'}")
    SQLModel.metadata.create_all(engine, tables=_old_tables())
    _patch_engine(monkeypatch, engine)
    limits._reset_for_tests()
    yield engine
    engine.dispose()


@pytest.fixture
def pg_old(monkeypatch):
    if not PG_URL:
        pytest.skip("falta HUGO_TEST_PG_URL (Postgres descartable)")
    schema = "est_" + uuid.uuid4().hex[:10]
    with psycopg.connect(_pg_dsn(PG_URL), autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(_schema_url(schema), pool_pre_ping=True, pool_size=25)
    SQLModel.metadata.create_all(engine, tables=_old_tables())
    _patch_engine(monkeypatch, engine)
    limits._reset_for_tests()
    yield engine
    engine.dispose()
    with psycopg.connect(_pg_dsn(PG_URL), autocommit=True) as c:
        c.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture(params=["sqlite", "postgres"])
def old_db(request):
    return request.getfixturevalue("sqlite_old" if request.param == "sqlite" else "pg_old")


def test_el_arranque_agrega_solo_las_dos_tablas(old_db):
    before = _shape(old_db)
    assert not NEW_TABLES & set(before)
    session.init_db()
    after = _shape(old_db)
    assert set(after) - set(before) == NEW_TABLES
    for table, shape in before.items():
        assert after[table] == shape, f"init_db cambió {table}"
    cols, idx = after["lookup_estimate"]
    assert idx["ix_lookup_estimate_status_time"] == (("estimate_status", "created_at"), False)
    assert {"ip_hash", "image_sha256", "timings", "candidates", "cost_usd", "tiers"} <= set(cols)
    assert "ip" not in cols and "email" not in cols
    ucols, _ = after["estimate_usage"]
    assert set(ucols) == {"scope", "day", "count", "updated_at"}


def test_el_arranque_es_idempotente(old_db):
    session.init_db()
    first = _shape(old_db)
    session.init_db()
    session.init_db()
    assert _shape(old_db) == first


def test_el_rollback_del_readme_deja_el_esquema_como_estaba(old_db):
    before = _shape(old_db)
    session.init_db()
    with Session(old_db) as s:
        s.add(models.Setting(key="_meta:estimate_otapi_calls_today", value="3"))
        s.add(models.Setting(key="estimate_enabled", value="1"))
        s.commit()
    with old_db.begin() as c:
        for stmt in [x.strip() for x in _rollback_sql().split(";") if x.strip()]:
            c.execute(text(stmt))
    assert _shape(old_db) == before
    with Session(old_db) as s:
        keys = [r.key for r in s.exec(select(models.Setting)).all()]
    assert not [k for k in keys if "estimate" in k]


def test_el_cupo_es_atomico_con_veinte_hilos(old_db):
    session.init_db()
    granted: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()
        ok = limits.reserve("ip:concurrente", 5)
        with lock:
            granted.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert granted.count(True) == 5, granted
    assert limits.used_today("ip:concurrente") == 5


def test_el_cupo_cambia_de_dia_y_se_poda(old_db):
    session.init_db()
    with Session(old_db) as s:
        s.add(EstimateUsage(scope="ip:viejo", day="2026-01-01", count=5))
        s.add(EstimateUsage(scope="ip:ayer", day="2026-10-10", count=5))
        s.commit()
    assert limits.reserve("ip:ayer", 5)          # día nuevo: arranca de 1
    assert limits.used_today("ip:ayer") == 1
    assert limits.prune() >= 1
    with Session(old_db) as s:
        assert s.get(EstimateUsage, "ip:viejo") is None


def test_registro_y_poda(old_db):
    session.init_db()
    store.save({"estimate_status": "ok", "estimate_id": "e_x", "source": "storefront-home",
                "ip_hash": "a" * 32, "timings": {"total": 1234}, "candidates": [{"offer_id": "1"}],
                "apify_launched": True, "apify_usd": 0.02, "cost_usd": 0.025,
                "tiers": [{"quantity": 50, "low_cents": 1, "high_cents": 2}]},
               {"detail": "Estimado para 'x'", "product_name": "x"})
    with Session(old_db) as s:
        row = s.exec(select(LookupEstimate)).one()
        assert row.apify_launched is True and row.cost_usd == 0.025
        row.created_at = utcnow() - timedelta(days=91)
        s.add(row)
        s.commit()
    assert store.prune(90) == 1
