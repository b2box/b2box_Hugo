"""El reloj de las auditorías sobrevive al redeploy.

IntervalTrigger(hours=336) cuenta desde que se registra el job, o sea desde
cada arranque. Con un redeploy cada pocos días las auditorías no corrían nunca.
Ahora cada job guarda cuándo terminó (settings `_meta:last_run:<job>`) y al
arrancar la próxima corrida se calcula desde ahí.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.db.models import Setting  # noqa: E402
from app.db.session import engine, init_db  # noqa: E402
from app.scheduler import jobs  # noqa: E402

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
TWO_WEEKS = timedelta(hours=336)


@pytest.fixture(autouse=True)
def _db():
    init_db()
    with Session(engine) as s:
        for row in s.exec(select(Setting)).all():
            if row.key.startswith(jobs._LAST_RUN_PREFIX):
                s.delete(row)
        s.commit()
    yield
    for job in jobs.scheduler.get_jobs():
        jobs.scheduler.remove_job(job.id)


# ─── la cuenta pura ───────────────────────────────────────────────────────────


def test_first_ever_run_waits_a_full_interval():
    assert jobs._interval_next_run(None, TWO_WEEKS, NOW) == NOW + TWO_WEEKS


def test_recent_run_keeps_the_original_clock():
    last = NOW - timedelta(days=3)
    assert jobs._interval_next_run(last, TWO_WEEKS, NOW) == last + TWO_WEEKS


def test_overdue_run_fires_after_the_startup_grace_not_immediately():
    last = NOW - timedelta(days=20)
    assert jobs._interval_next_run(last, TWO_WEEKS, NOW) == NOW + jobs._STARTUP_GRACE


def test_run_due_in_less_than_the_grace_is_pushed_to_the_grace():
    last = NOW - TWO_WEEKS + timedelta(minutes=1)
    assert jobs._interval_next_run(last, TWO_WEEKS, NOW) == NOW + jobs._STARTUP_GRACE


# ─── persistencia ─────────────────────────────────────────────────────────────


def test_mark_and_read_round_trip():
    assert jobs._last_job_run("audit_duplicates") is None
    jobs._mark_job_run("audit_duplicates")
    got = jobs._last_job_run("audit_duplicates")
    assert got is not None and got.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - got).total_seconds()) < 5


def test_marker_lives_in_settings_with_the_meta_prefix():
    jobs._mark_job_run("audit_prices_x")
    with Session(engine) as s:
        row = s.get(Setting, "_meta:last_run:audit_prices_x")
    assert row is not None


def test_corrupt_marker_is_treated_as_never(monkeypatch):
    jobs._set_meta("_meta:last_run:audit_duplicates", "ayer a la tarde")
    assert jobs._last_job_run("audit_duplicates") is None


def test_db_failure_on_read_does_not_break_startup(monkeypatch):
    def boom(key):  # noqa: ARG001
        raise RuntimeError("db caída")

    monkeypatch.setattr(jobs, "_get_meta", boom)
    assert jobs._last_job_run("audit_duplicates") is None


@pytest.mark.asyncio
async def test_a_finished_audit_leaves_its_marker(monkeypatch):
    async def fake_flatten(client):  # noqa: ARG001
        return []

    monkeypatch.setattr(jobs, "VendureClient", lambda: object())
    monkeypatch.setattr(jobs, "_flatten_products", fake_flatten)
    await jobs.audit_catalog_quality()
    assert jobs._last_job_run("audit_catalog_quality") is not None


@pytest.mark.asyncio
async def test_a_crashed_audit_does_not_advance_the_clock(monkeypatch):
    async def boom(client):  # noqa: ARG001
        raise RuntimeError("Vendure no responde")

    monkeypatch.setattr(jobs, "VendureClient", lambda: object())
    monkeypatch.setattr(jobs, "_flatten_products", boom)
    with pytest.raises(RuntimeError):
        await jobs.audit_catalog_quality()
    assert jobs._last_job_run("audit_catalog_quality") is None


# ─── registro: el scheduler arranca con la fecha calculada ────────────────────


def test_register_jobs_resumes_from_the_persisted_clock(monkeypatch):
    three_days_ago = datetime.now(timezone.utc) - timedelta(days=3)
    jobs._set_meta("_meta:last_run:audit_duplicates", three_days_ago.isoformat())
    monkeypatch.setattr(jobs, "get_settings", lambda: type("S", (), {
        "audit_interval_hours": 336, "verify_catalog_ttl_seconds": 300,
    })())

    jobs.register_jobs()

    dupes = jobs.scheduler.get_job("audit_duplicates")
    expected = three_days_ago + TWO_WEEKS
    assert abs((dupes.next_run_time - expected).total_seconds()) < 2, (
        "la próxima corrida sigue el reloj guardado, no el arranque"
    )

    # Sin marcador: espera un intervalo entero desde ahora (como antes).
    prices = jobs.scheduler.get_job("audit_source_prices")
    assert abs((prices.next_run_time - (datetime.now(timezone.utc) + TWO_WEEKS)).total_seconds()) < 5


def test_register_jobs_schedules_an_overdue_audit_soon(monkeypatch):
    jobs._set_meta("_meta:last_run:audit_pa_variants",
                   (datetime.now(timezone.utc) - timedelta(days=40)).isoformat())
    monkeypatch.setattr(jobs, "get_settings", lambda: type("S", (), {
        "audit_interval_hours": 336, "verify_catalog_ttl_seconds": 300,
    })())
    jobs.register_jobs()
    job = jobs.scheduler.get_job("audit_pa_variants")
    delay = (job.next_run_time - datetime.now(timezone.utc)).total_seconds()
    assert 0 < delay <= jobs._STARTUP_GRACE.total_seconds() + 5
