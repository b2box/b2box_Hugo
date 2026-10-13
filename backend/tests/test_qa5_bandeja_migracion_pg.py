"""QA independiente (criterio 6): migración por `init_db()` sobre un Postgres 16 descartable, desde el esquema que deja el
`init_db()` REAL de origin/main (no el golden del developer): se extrae `origin/main:backend/app` con `git archive` y se lo corre en un
subproceso contra un schema propio. Se salta sola sin HUGO_TEST_PG_URL:

    docker run --rm -d --name hugo-qa-bandeja-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=hugo_test -p 127.0.0.1:55539:5432 postgres:16-alpine
    HUGO_TEST_PG_URL=postgresql+psycopg://postgres:pw@127.0.0.1:55539/hugo_test VENDURE_API_URL=https://example.invalid/admin-api \\
        pytest backend/tests/test_qa5_bandeja_migracion_pg.py

Cubre: idempotencia, que no se toque nada de lo que ya estaba, que no se reescriba la tabla grande, que el código viejo siga andando sobre
el esquema nuevo, que el SQL de rollback del README deje el esquema EXACTAMENTE como estaba, y la pasada completa (racha, ficha, API, mail)
sobre Postgres, incluida la prueba de que dos procesos no abren dos fichas abiertas del mismo producto.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tarfile
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import psycopg  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from app import auth, main as _main, runtime as _runtime  # noqa: E402,F401  (se importan ANTES de parchar el engine: si no, capturan el de Postgres)
from app.db import session  # noqa: E402
from app.notifier import email as _email  # noqa: E402,F401
from app.pricing import price_monitor as _pm, price_review as _pr, price_review_digest as _prd  # noqa: E402,F401
from app.scheduler import jobs as _jobs  # noqa: E402,F401
from tests import qa5_bandeja as q  # noqa: E402

PG_URL = os.environ.get("HUGO_TEST_PG_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL, reason="falta HUGO_TEST_PG_URL (Postgres descartable)")
REPO = Path(__file__).resolve().parents[2]
NEW_TABLES = {"price_review_item", "price_review_event"}
NEW_COLS = {"market_price_snapshot": {"data_at", "review_flag"}, "price_monitor_run": {"review_at", "review_summary", "n_flipped"}}


def dsn() -> str:
    u = make_url(PG_URL)
    return f"host={u.host} port={u.port} user={u.username} password={u.password} dbname={u.database}"


def schema_url(schema: str) -> str:
    return make_url(PG_URL).update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)


def shape(engine) -> dict:
    """Todo lo que describe el esquema: columnas (tipo, null, default), PK, únicos, FK, checks e índices de cada tabla."""
    insp = inspect(engine)
    out = {}
    for t in sorted(insp.get_table_names()):
        out[t] = {
            "cols": {c["name"]: (str(c["type"]) + (" WITH TIME ZONE" if getattr(c["type"], "timezone", False) else ""), c["nullable"],
                             str(c.get("default"))) for c in insp.get_columns(t)},
            "pk": insp.get_pk_constraint(t)["constrained_columns"],
            "uq": sorted((u["name"], tuple(u["column_names"])) for u in insp.get_unique_constraints(t)),
            "fk": sorted((tuple(f["constrained_columns"]), f["referred_table"]) for f in insp.get_foreign_keys(t)),
            "ck": sorted(str(c["sqltext"]) for c in insp.get_check_constraints(t)),
            "ix": sorted((i["name"], tuple(i["column_names"]), bool(i["unique"])) for i in insp.get_indexes(t)),
        }
    return out


def table_stats(engine, table: str) -> tuple:
    with engine.connect() as c:
        rel = c.execute(text("SELECT pg_relation_filenode(CAST(:t AS regclass))"), {"t": table}).scalar()
        upd = c.execute(text("SELECT n_tup_upd FROM pg_stat_user_tables WHERE relname = :t AND schemaname = current_schema()"),
                        {"t": table}).scalar()
    return rel, upd


def xmin_digest(engine, table: str) -> str:
    """Si alguna fila se actualizó (aunque sea con el mismo valor) cambia su xmin: más fiable que las estadísticas de pg_stat, que llegan tarde."""
    with engine.connect() as c:
        return c.execute(text(f'SELECT md5(string_agg(xmin::text, \',\' ORDER BY id)) FROM "{table}"')).scalar()


def rows_digest(engine, table: str, cols: list[str]) -> tuple:
    with engine.connect() as c:
        n = c.execute(text(f'SELECT count(*) FROM "{table}"')).scalar()
        h = c.execute(text(f"SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM (SELECT {', '.join(cols)} FROM \"{table}\") t")).scalar()
    return n, h


OLD_SCRIPT = r'''
import json, sys
from datetime import datetime, timedelta
from sqlmodel import Session
from app.db.session import init_db, engine
from app.db import models as m
init_db()
now = datetime(2026, 10, 12, 6, 0, 0)
with Session(engine) as s:
    for d in range(3):
        run = m.PriceMonitorRun(status="ok", mode=0, started_at=now - timedelta(days=d), finished_at=now - timedelta(days=d), total_products=200)
        s.add(run); s.flush()
        for i in range(200):
            s.add(m.MarketPriceSnapshot(run_id=run.id, product_id=str(i), captured_at=now - timedelta(days=d), ml_status="ok" if i % 3 == 0 else "no_data",
                  color="rojo" if i % 5 == 0 else "verde", our_price_cents=1000000 + i, ml_median_cents=900000 if i % 3 == 0 else None,
                  matched_listings=json.dumps([{"ml_id": "MLA1000001", "source": "clip", "prices_cents": [900000], "sellers": ["a"]}]),
                  product_name=f"Producto {i}", product_enabled=True))
    s.add(m.MarketMatchFeedback(product_id="11", ml_id="MLA555", label=0, category="igual", origin="web"))
    s.commit()
print("OLD_OK")
'''

OLD_CLIENT_SCRIPT = r'''
from datetime import datetime
from sqlmodel import Session, select
from app.db.session import engine
from app.db import models as m
with Session(engine) as s:
    run = m.PriceMonitorRun(status="ok", mode=0, started_at=datetime(2026, 10, 13, 6, 0, 0), total_products=1)
    s.add(run); s.flush()
    s.add(m.MarketPriceSnapshot(run_id=run.id, product_id="999", captured_at=datetime(2026, 10, 13, 6, 0, 0), ml_status="ok", color="rojo",
          our_price_cents=1, product_name="viejo", product_enabled=True))
    s.commit()
    n = len(s.exec(select(m.MarketPriceSnapshot)).all()); k = len(s.exec(select(m.PriceMonitorRun)).all())
print("OLD_CLIENT_OK", n, k)
'''


def run_old_code(code_dir: Path, schema: str, script: str) -> str:
    env = {**os.environ, "DATABASE_URL": schema_url(schema), "PYTHONPATH": str(code_dir), "VENDURE_API_URL": "https://example.invalid/admin-api"}
    res = subprocess.run([sys.executable, "-c", script], cwd=code_dir, env=env, capture_output=True, text=True, timeout=240)
    assert res.returncode == 0, res.stderr[-2000:]
    return res.stdout


@pytest.fixture(scope="module")
def main_code() -> Path:
    """El `backend/` de origin/main, extraído con git archive."""
    probe = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", "origin/main"], capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("no hay origin/main en este repo")
    tmp = Path(tempfile.mkdtemp(prefix="qa5-main-"))
    tar = tmp / "main.tar"
    subprocess.run(["git", "-C", str(REPO), "archive", "origin/main", "backend/app", "-o", str(tar)], check=True)
    with tarfile.open(tar) as tf:
        tf.extractall(tmp, filter="data")
    return tmp / "backend"


@pytest.fixture(params=["tal_cual", "timestamptz_legacy"])
def old_db(request, main_code, monkeypatch):
    """Un schema de Postgres con el esquema Y los datos que deja el `init_db()` de origin/main. `tal_cual` es lo que hay hoy en prod (el
    arreglo de timestamptz del 08-oct ya corrió); `timestamptz_legacy` simula una base que todavía lo tiene (sqlmodel 0.0.48): ahí el
    arranque también convierte esas fechas, que es una migración PREEXISTENTE y reescribe la tabla (no es de este PR)."""
    schema = "qa5_" + uuid.uuid4().hex[:10]
    with psycopg.connect(dsn(), autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
    assert "OLD_OK" in run_old_code(main_code, schema, OLD_SCRIPT)
    engine = create_engine(schema_url(schema), pool_pre_ping=True)
    engine.legacy_tz = request.param == "timestamptz_legacy"
    if engine.legacy_tz:
        with engine.begin() as c:
            for table, col in (("price_monitor_run", "started_at"), ("price_monitor_run", "finished_at"),
                               ("market_price_snapshot", "captured_at"), ("market_match_feedback", "created_at")):
                c.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN "{col}" TYPE timestamp with time zone USING "{col}" AT TIME ZONE \'UTC\''))
    import sys as _sys
    original = session.engine
    for name, mod in list(_sys.modules.items()):
        if mod is not None and getattr(mod, "engine", None) is original:
            monkeypatch.setattr(mod, "engine", engine)
    yield schema, engine, main_code
    engine.dispose()
    with psycopg.connect(dsn(), autocommit=True) as c:
        c.execute(f'DROP SCHEMA "{schema}" CASCADE')


def readme_rollback() -> str:
    text_ = (REPO / "README.md").read_text()
    for block in re.findall(r"```sql\n(.*?)```", text_, flags=re.S):
        if "DROP TABLE IF EXISTS price_review_event" in block:
            return block
    raise AssertionError("el README no trae el SQL de rollback")


def test_the_starting_point_is_really_origin_main_with_data(old_db):
    schema, engine, _ = old_db
    s = shape(engine)
    assert not NEW_TABLES & set(s)
    for table, cols in NEW_COLS.items():
        assert not cols & set(s[table]["cols"])
    assert rows_digest(engine, "market_price_snapshot", ["id"])[0] == 600
    assert ("WITH TIME ZONE" in s["market_price_snapshot"]["cols"]["captured_at"][0]) is engine.legacy_tz


def test_boot_adds_only_the_new_objects_keeps_every_row_and_does_not_rewrite_the_big_table(old_db):
    schema, engine, _ = old_db
    before, snap_before = shape(engine), table_stats(engine, "market_price_snapshot")
    xmin_before = xmin_digest(engine, "market_price_snapshot")
    digest_before = rows_digest(engine, "market_price_snapshot", ["id", "run_id", "product_id", "ml_status", "color", "our_price_cents",
                                                                  "matched_listings", "product_name"])
    runs_before = rows_digest(engine, "price_monitor_run", ["id", "status", "total_products"])
    session.init_db()
    after = shape(engine)
    assert set(after) - set(before) == NEW_TABLES and set(before) <= set(after)
    for t in before:
        assert set(after[t]["cols"]) - set(before[t]["cols"]) == NEW_COLS.get(t, set()), t
        for name, defn in before[t]["cols"].items():
            if "timestamp" in defn[0].lower():
                continue                                    # timestamptz -> timestamp es la migración que ya existía (08-oct)
            assert after[t]["cols"][name] == defn, f"{t}.{name} cambió"
        for key in ("pk", "uq", "fk", "ck", "ix"):
            assert after[t][key] == before[t][key], f"{t}: cambió {key}"
    # las columnas de fecha nuevas son naive, como el resto del modelo
    assert "with time zone" not in after["market_price_snapshot"]["cols"]["data_at"][0].lower()
    assert "with time zone" not in after["price_review_item"]["cols"]["opened_at"][0].lower()
    # ninguna fila se perdió ni cambió
    assert rows_digest(engine, "market_price_snapshot", ["id", "run_id", "product_id", "ml_status", "color", "our_price_cents",
                                                         "matched_listings", "product_name"]) == digest_before
    assert rows_digest(engine, "price_monitor_run", ["id", "status", "total_products"]) == runs_before
    # la tabla grande no se reescribió (mismo filenode) ni se le hizo ningún UPDATE de relleno. Con la base `timestamptz_legacy` el arranque
    # además convierte esas fechas (migración del 08-oct, preexistente): eso sí reescribe la tabla y no se mide acá.
    snap_after = table_stats(engine, "market_price_snapshot")
    if not engine.legacy_tz:
        assert snap_after[0] == snap_before[0], "market_price_snapshot se reescribió"
        assert xmin_digest(engine, "market_price_snapshot") == xmin_before, "UPDATE sobre market_price_snapshot"
    # el contador nuevo de las corridas quedó en 0 en las que ya existían
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM price_monitor_run WHERE n_flipped IS NULL")).scalar() == 0


def test_boot_is_idempotent_three_times_and_the_later_boots_write_nothing(old_db):
    schema, engine, _ = old_db
    session.init_db()
    s1 = shape(engine)
    stats = {t: (table_stats(engine, t)[0], xmin_digest(engine, t)) for t in ("market_price_snapshot", "price_monitor_run")}
    session.init_db()
    session.init_db()
    assert shape(engine) == s1
    for t, (rel, xm) in stats.items():
        assert table_stats(engine, t)[0] == rel and xmin_digest(engine, t) == xm, f"{t}: los arranques siguientes escribieron"


def test_the_old_code_keeps_working_on_the_new_schema_and_the_new_code_reads_what_it_wrote(old_db):
    schema, engine, main_code = old_db
    session.init_db()
    out = run_old_code(main_code, schema, OLD_CLIENT_SCRIPT)
    assert "OLD_CLIENT_OK 601 4" in out
    # lo que escribió el código viejo no tiene las columnas nuevas: el nuevo lo lee sin romper
    from sqlmodel import Session, select

    from app.db.models import MarketPriceSnapshot, PriceMonitorRun
    from app.pricing import price_review as pr
    from tests.qa5_bandeja import NOW

    with Session(engine) as s:
        snap = s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.product_id == "999")).one()
        assert snap.data_at is None and snap.review_flag is None
        run = s.exec(select(PriceMonitorRun).order_by(PriceMonitorRun.id.desc())).first()
        assert run.review_at is None
        v = pr.evaluate_firm(snap, pr.ReviewParams(), NOW)           # una fila vieja, sin data_at, no revienta
        assert v.status in (pr.NO_ROJO, pr.DUDOSO, pr.FIRME)


def test_the_readme_rollback_leaves_the_schema_exactly_as_it_was_and_the_upgrade_can_be_repeated(old_db):
    schema, engine, main_code = old_db
    before = shape(engine)
    session.init_db()
    upgraded = shape(engine)
    sql = readme_rollback()
    assert "DROP TABLE IF EXISTS price_review_event" in sql and "DROP TABLE IF EXISTS price_review_item" in sql
    with engine.begin() as c:
        c.execute(text(sql))
    rolled = shape(engine)
    if engine.legacy_tz:      # lo único que el rollback no deshace es el arreglo PREEXISTENTE de timestamptz
        diff = {t for t in rolled if rolled[t] != before.get(t)}
        assert diff <= {"price_monitor_run", "market_price_snapshot", "market_match_feedback"}, diff
        for t in diff:
            assert {k: v for k, v in rolled[t].items() if k != "cols"} == {k: v for k, v in before[t].items() if k != "cols"}
            assert {c: d for c, d in rolled[t]["cols"].items() if "TIME ZONE" not in before[t]["cols"][c][0]} == \
                   {c: d for c, d in before[t]["cols"].items() if "TIME ZONE" not in d[0]}
        before = rolled
    else:
        assert rolled == before, {t: (before.get(t), rolled.get(t)) for t in set(before) | set(rolled) if before.get(t) != rolled.get(t)}
    # el código viejo anda sobre la base ya revertida
    assert "OLD_CLIENT_OK" in run_old_code(main_code, schema, OLD_CLIENT_SCRIPT)
    # y volver a subir da lo mismo que la primera vez
    session.init_db()
    assert shape(engine) == upgraded
    # el rollback es seguro de correr dos veces
    with engine.begin() as c:
        c.execute(text(sql))
        c.execute(text(sql))
    assert shape(engine) == before


def test_the_rollback_sql_only_touches_what_the_pr_added():
    sql = readme_rollback().lower()
    assert "truncate" not in sql and "drop schema" not in sql and "drop database" not in sql
    deletes = re.findall(r"delete from (\w+) where ([^;]+);", sql)
    assert [t for t, _ in deletes] == ["settings"] and all(k in deletes[0][1] for k in ("pm_review_", "pm_diag_", "price_review_digest"))
    assert "pm_mode" not in deletes[0][1]              # no borra el modo: solo lo baja de 2 a 1 (el código viejo no conoce el 2)
    assert re.findall(r"update (\w+) set", sql) == ["settings"]
    dropped_tables = set(re.findall(r"drop table if exists (\w+)", sql))
    assert dropped_tables == NEW_TABLES
    cols = set(re.findall(r"drop column if exists (\w+)", sql))
    assert cols == {c for cs in NEW_COLS.values() for c in cs}


# ─── La pasada completa sobre Postgres ───────────────────────────────────────────────────────────


@pytest.fixture
def pg_run(old_db, monkeypatch):
    schema, engine, _ = old_db
    session.init_db()
    q.setting("pm_mode", 1)
    world = q.VendureWorld()
    world.spy = q.install_spy(monkeypatch, world)
    return schema, engine, world


async def test_two_nights_open_one_ficha_on_postgres_and_a_second_open_one_is_refused_by_the_database(pg_run):
    schema, engine, world = pg_run
    from sqlmodel import Session

    from app.db.models import PriceReviewEvent, PriceReviewItem
    from app.pricing import price_review as pr

    for day in (-1, 0):
        started = q.NOW + timedelta(days=day)
        rid = q.new_run(started)
        for pid in ("a", "b"):
            q.save(q.mk_snap(rid, pid, data_at=started - timedelta(hours=1), captured_at=started))
        await pr.process_run(rid, now=started + timedelta(hours=1))
    items = q.rows(PriceReviewItem)
    assert sorted(i.product_id for i in items) == ["a", "b"] and all(i.state == "propuesto" for i in items)
    assert len(q.rows(PriceReviewEvent)) == 2
    # la base misma impide una segunda ficha abierta del mismo producto
    with Session(engine) as s:
        s.add(PriceReviewItem(product_id="a", open_key="a", state="propuesto"))
        with pytest.raises(IntegrityError):
            s.commit()
    # y una cerrada (open_key NULL) sí puede convivir con una abierta
    with Session(engine) as s:
        s.add(PriceReviewItem(product_id="a", open_key=None, state="cerrado_auto"))
        s.commit()
    assert world.writes() == []


async def test_the_api_and_the_filters_work_on_postgres(pg_run, monkeypatch):
    schema, engine, world = pg_run
    from fastapi.testclient import TestClient

    from app import auth
    from app import main as main_mod
    from app.config import Settings
    from app.pricing import price_review as pr

    cfg = Settings(vendure_api_url="https://example.invalid/admin-api", hugo_env="development", dashboard_password="x", dashboard_secret="s" * 32)
    for mod in (auth, main_mod):
        monkeypatch.setattr(mod, "get_settings", lambda: cfg)
    for day in (-1, 0):
        started = q.NOW + timedelta(days=day)
        rid = q.new_run(started)
        for i in range(5):
            q.save(q.mk_snap(rid, f"p{i}", data_at=started - timedelta(hours=1), captured_at=started))
        await pr.process_run(rid, now=started + timedelta(hours=1))
    c = TestClient(main_mod.app)
    c.cookies.set(auth.COOKIE_NAME, auth.issue_session_token("pao@b2box.pro"))
    assert c.get("/api/price-review/summary").json()["pending"] == 5
    assert c.get("/api/price-review/items", params={"q": "Producto p3"}).json()["total"] == 1
    assert c.get("/api/price-review/items", params={"q": "%"}).json()["total"] == 0
    assert c.get("/api/price-review/items", params={"q": "producto P1"}).json()["total"] == 1             # ILIKE
    assert c.get("/api/price-review/items", params={"state": "todas", "page_size": 2, "page": 2}).json()["items"][0]["product"]["id"] == "p0"
    item = c.get("/api/price-review/items").json()["items"][0]
    assert c.get(f"/api/price-review/items/{item['id']}").status_code == 200
    # datos viejos de la corrida (timestamptz en prod) tampoco rompen el filtro del Semáforo
    assert c.get("/api/price-monitor/snapshots", params={"review": "firme"}).status_code == 200


async def test_the_digest_marks_events_on_postgres(pg_run, monkeypatch):
    schema, engine, world = pg_run
    from app.config import Settings
    from app.db.models import PriceReviewEvent
    from app.notifier import email as email_chan
    from app.pricing import price_review as pr
    from app.pricing import price_review_digest as digest

    sent = []

    async def fake(msg, **kw):
        sent.append(msg)

    cfg = Settings(vendure_api_url="https://example.invalid/admin-api", alert_smtp_user="a@b2box.pro", alert_smtp_pass="x",
                   pm_review_emails="pao@b2box.pro,gabriel@b2box.pro")
    monkeypatch.setattr(email_chan.aiosmtplib, "send", fake)
    monkeypatch.setattr(email_chan, "get_settings", lambda: cfg)
    monkeypatch.setattr(digest, "get_settings", lambda: cfg)
    for day in (-1, 0):
        started = q.NOW + timedelta(days=day)
        rid = q.new_run(started)
        q.save(q.mk_snap(rid, "z", data_at=started - timedelta(hours=1), captured_at=started))
        await pr.process_run(rid, now=started + timedelta(hours=1))
    assert (await digest.send_digest(q.NOW + timedelta(hours=3)))["sent"] is True
    assert (await digest.send_digest(q.NOW + timedelta(hours=4)))["sent"] is False and len(sent) == 1
    assert all(e.notified_at is not None for e in q.rows(PriceReviewEvent))
