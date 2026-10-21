"""QA de la búsqueda con Apify sobre Postgres 16 descartable: la tabla `ml_web_claim` y sus consultas, el cupo por lote y
una corrida entera. Se salta sola sin `HUGO_TEST_PG_URL`:

    docker run -d --name hugo-apify-pgtest -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=hugo_test -p 55499:5432 postgres:16-alpine
    HUGO_TEST_PG_URL=postgresql+psycopg://postgres:pw@localhost:55499/hugo_test pytest backend/tests/test_qa_apify_ml_search_pg.py

Lo que SQLite no hace cumplir: el tipo y largo de las columnas, la clave primaria bajo carrera real (varias conexiones a la
vez, READ COMMITTED), el UPDATE condicionado de la reserva y el compare-and-swap del cupo entre "procesos".
"""

from __future__ import annotations

import contextlib
import os
import threading
import uuid
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import psycopg
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, select

from app import runtime
from app.clock import utcnow
from app.db import session
from app.db.models import MarketPriceSnapshot, MlWebClaim, MlWebResult
from app.pricing import apify_ml_search as ams
from app.pricing import daily_budget, oficina_ml
from tests.test_apify_ml_search import ACTOR, TOKEN, FakeApify
from tests.test_qa_tiendas_pg import PG_URL, _patch_engine, _pg_dsn, _schema_url

pytestmark = pytest.mark.skipif(not PG_URL, reason="falta HUGO_TEST_PG_URL (Postgres descartable)")


@pytest.fixture
def pg(monkeypatch):
    schema = "apy_" + uuid.uuid4().hex[:10]
    with psycopg.connect(_pg_dsn(PG_URL), autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(_schema_url(schema), pool_pre_ping=True, pool_size=20, max_overflow=10)
    _patch_engine(monkeypatch, engine)
    runtime.invalidate()
    session.init_db()
    yield engine
    runtime.invalidate()
    engine.dispose()
    with psycopg.connect(_pg_dsn(PG_URL), autocommit=True) as c:
        c.execute(f'DROP SCHEMA "{schema}" CASCADE')


def _snaps(engine, n: int) -> list[str]:
    pids = [str(700 + i) for i in range(n)]
    with Session(engine) as s:
        for pid in pids:
            s.add(MarketPriceSnapshot(run_id=1, product_id=pid, ml_status="no_data", product_name=f"Producto PG {pid}",
                                      product_enabled=True))
        s.commit()
    return pids


def test_la_tabla_de_reservas_en_postgres(pg):
    insp = inspect(pg)
    cols = {c["name"]: c for c in insp.get_columns("ml_web_claim")}
    assert set(cols) == {"product_id", "claimed_by", "claimed_at", "expires_at"}
    assert insp.get_pk_constraint("ml_web_claim")["constrained_columns"] == ["product_id"]
    assert cols["product_id"]["type"].length == 64 and cols["claimed_by"]["type"].length == 24
    assert all(not getattr(cols[c]["type"], "timezone", False) for c in ("claimed_at", "expires_at")), \
        "fechas sin zona, como el resto (utcnow naive)"
    assert all(cols[c]["nullable"] is False for c in cols)
    assert any(i["column_names"] == ["expires_at"] for i in insp.get_indexes("ml_web_claim"))
    origin = {c["name"]: c for c in insp.get_columns("ml_web_result")}["origin"]
    assert origin["type"].length >= len("apify")


def test_la_migracion_solo_agrega_la_tabla_y_es_idempotente(pg):
    before = {t: [c["name"] for c in inspect(pg).get_columns(t)] for t in inspect(pg).get_table_names()}
    with pg.begin() as c:
        c.execute(text("DROP TABLE ml_web_claim"))                  # el rollback del README
    session.init_db()
    session.init_db()
    after = {t: [c["name"] for c in inspect(pg).get_columns(t)] for t in inspect(pg).get_table_names()}
    assert after == before


def test_reservar_soltar_y_vencer_en_postgres(pg):
    pids = _snaps(pg, 3)
    assert oficina_ml.claim(pids, "apify-aaaa", timedelta(minutes=30)) == pids
    assert oficina_ml.claim(pids, "oficina", timedelta(hours=3)) == []
    assert oficina_ml.claim(pids[:1], "apify-aaaa", timedelta(minutes=30)) == pids[:1]      # renovar el suyo
    assert oficina_ml.build_queue(10, claimer="oficina") == []
    assert [i["product_id"] for i in oficina_ml.build_queue(10, claimer="apify-aaaa")] == pids
    oficina_ml.release("apify-aaaa", pids[:1])
    assert [i["product_id"] for i in oficina_ml.queue_for_oficina(10)] == pids[:1]
    later = utcnow() + timedelta(hours=1)
    assert oficina_ml.claim(pids[1:], "oficina", timedelta(hours=3), now=later) == pids[1:]
    with Session(pg) as s:
        assert {c.claimed_by for c in s.exec(select(MlWebClaim))} == {"oficina"}


def test_reserva_bajo_carrera_real_uno_solo_se_lleva_cada_producto(pg):
    """16 hilos (16 conexiones) piden los mismos 10 productos a la vez: cada uno queda con UN dueño."""
    pids = _snaps(pg, 10)
    got: dict[str, list[str]] = {}
    barrier = threading.Barrier(16)

    def worker(i: int) -> None:
        barrier.wait()
        got[f"apify-{i:04d}"] = oficina_ml.claim(pids, f"apify-{i:04d}", timedelta(minutes=30))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    winners = [p for g in got.values() for p in g]
    assert sorted(winners) == sorted(pids), got
    with Session(pg) as s:
        rows = s.exec(select(MlWebClaim)).all()
    assert {r.product_id: r.claimed_by for r in rows} == {p: c for c, g in got.items() for p in g}


def test_reserva_de_cupo_entre_procesos_no_se_pasa_del_tope(pg, monkeypatch):
    """Sin el candado del proceso (como dos workers distintos), el compare-and-swap en la base es lo único que frena."""
    monkeypatch.setattr(daily_budget, "_lock", contextlib.nullcontext())
    key = "_meta:qa_reserve_up_to_pg"
    granted: list[int] = []
    barrier = threading.Barrier(12)

    def worker() -> None:
        barrier.wait()
        for _ in range(5):
            granted.append(daily_budget.reserve_up_to(key, 37, 4))

    threads = [threading.Thread(target=worker) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert sum(granted) == daily_budget.used_today(key) <= 37
    assert daily_budget.used_today(key) == 37 or 0 in granted        # se llena, o alguno se rindió (fail-closed)


async def test_una_corrida_entera_en_postgres(pg, monkeypatch):
    pids = _snaps(pg, 6)
    settings = ams.get_settings()
    monkeypatch.setattr(settings, "apify_token", TOKEN, raising=False)
    monkeypatch.setattr(settings, "apify_meli_search_actor", ACTOR, raising=False)
    monkeypatch.setattr(settings, "apify_meli_search_build", "1.0.103", raising=False)
    monkeypatch.setattr(settings, "apify_meli_search_timeout_s", 30, raising=False)
    monkeypatch.setattr(settings, "apify_meli_search_concurrency", 4, raising=False)
    monkeypatch.setattr(ams, "_POLL_INTERVAL_S", 0.0)
    runtime.set_value("apify_meli_search_enabled", 1)
    runtime.set_value("apify_meli_search_daily_max", 4)
    oficina_ml.queue_for_oficina(1)                                  # la Mac se llevó el primero
    fake = FakeApify().install(monkeypatch)

    report = await ams.run_search()

    assert report.status == "over_budget" and fake.starts == 4 and report.searches == 4
    with Session(pg) as s:
        rows = s.exec(select(MlWebResult)).all()
        claims = s.exec(select(MlWebClaim)).all()
    assert {r.product_id for r in rows} == set(pids[1:5]) and {r.origin for r in rows} == {"apify"}
    assert [(c.product_id, c.claimed_by) for c in claims] == [(pids[0], "oficina")]
    fresh = oficina_ml.load_fresh()
    assert set(fresh) == set(pids[1:5]) and all(len(f.candidates) == 8 for f in fresh.values())
    assert daily_budget.used_today(ams.COUNTER_KEY) == 4
    # Lo que no entró en el cupo quedó libre (para mañana o para la Mac).
    assert [i["product_id"] for i in oficina_ml.build_queue(10, claimer="apify-x")] == [pids[5]]
