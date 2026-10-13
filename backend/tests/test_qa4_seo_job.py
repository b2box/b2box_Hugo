"""QA independiente de HG1: el job semanal (lunes 07:30 UTC), la retención de 8 corridas y el redeploy.

El marcador de «última corrida» vive en la tabla `settings` (no en memoria), así que un redeploy no lo pierde:
estas pruebas simulan el arranque de un proceso nuevo (`register_jobs()` sobre un scheduler propio) en cada situación
que importa, y también corren el job por el camino real de APScheduler.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.clock import utcnow  # noqa: E402
from app.db.models import Setting, TextAuditItem, TextAuditRun  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.scheduler import jobs  # noqa: E402
from app.seo import text_audit  # noqa: E402
from tests.seo_fixtures import FakeVendure, install, raw_product  # noqa: E402
from tests.test_seo_text_audit import env, run  # noqa: E402,F401

_REAL_SLEEP = asyncio.sleep          # `install` neutraliza asyncio.sleep para los reintentos de Vendure
JOB = jobs.SEO_TEXT_AUDIT_JOB_ID
MARKER = f"{jobs._LAST_RUN_PREFIX}{JOB}"


def _settings(cron="30 7 * * mon"):
    return type("S", (), {"audit_interval_hours": 336, "verify_catalog_ttl_seconds": 300, "seo_text_audit_cron_utc": cron})()


@pytest.fixture
def boot(monkeypatch):
    """Simula el arranque de un proceso: scheduler nuevo + register_jobs(). Devuelve el job registrado."""

    def _boot(cron="30 7 * * mon"):
        sch = AsyncIOScheduler()
        monkeypatch.setattr(jobs, "scheduler", sch)
        monkeypatch.setattr(jobs, "get_settings", lambda: _settings(cron))
        jobs.register_jobs()
        return sch.get_job(JOB)

    return _boot


def _trigger(expr="30 7 * * mon") -> CronTrigger:
    return CronTrigger.from_crontab(expr, timezone="UTC")


def _prev_fire(now: datetime) -> datetime:
    """El último lunes 07:30 UTC <= now."""
    t = _trigger()
    cur = t.get_next_fire_time(None, now - timedelta(days=8))
    last = cur
    while cur is not None and cur <= now:
        last = cur
        cur = t.get_next_fire_time(cur, cur + timedelta(seconds=1))
    return last


# ─── El cron ───────────────────────────────────────────────────────

def test_las_proximas_diez_corridas_son_siempre_lunes_a_las_0730_utc():
    t = _trigger()
    cur = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)            # viernes
    seen = []
    for _ in range(10):
        cur = t.get_next_fire_time(None, cur)
        seen.append(cur)
        cur += timedelta(seconds=1)
    assert all(d.weekday() == 0 and (d.hour, d.minute, d.second) == (7, 30, 0) for d in seen)
    assert all(str(d.tzinfo) in ("UTC", "tzutc()") or d.utcoffset() == timedelta(0) for d in seen)
    assert [(b - a).days for a, b in zip(seen, seen[1:])] == [7] * 9
    assert seen[0] == datetime(2026, 10, 12, 7, 30, tzinfo=timezone.utc)


def test_el_numero_1_en_el_dia_de_la_semana_NO_es_lunes_en_apscheduler():
    """La trampa que el .env y el README advierten: `1` = martes. Por eso el default usa `mon`."""
    nxt = _trigger("30 7 * * 1").get_next_fire_time(None, datetime(2026, 10, 9, tzinfo=timezone.utc))
    assert nxt.weekday() == 1
    assert jobs._SEO_TEXT_AUDIT_DEFAULT_CRON.endswith("mon")
    from app.config import Settings
    assert Settings.model_fields["seo_text_audit_cron_utc"].default == "30 7 * * mon"


def test_el_job_queda_registrado_con_las_garantias_de_no_solaparse(boot):
    job = boot()
    assert job.id == JOB and job.max_instances == 1 and job.coalesce is True
    assert job.func is jobs.seo_text_audit
    assert str(job.trigger.timezone) == "UTC"


# ─── Redeploy ──────────────────────────────────────────────────────

def test_redeploy_despues_de_una_corrida_normal_no_la_repite(boot):
    now = datetime.now(timezone.utc)
    prev = _prev_fire(now)
    marker = min(prev + timedelta(minutes=2), now - timedelta(seconds=1))
    jobs._set_meta(MARKER, marker.isoformat())
    assert getattr(boot(), "next_run_time", None) is None, "redeploy tras una corrida completa: no hay que correr de nuevo"


def test_redeploy_que_pisa_el_lunes_0730_la_recupera_una_sola_vez(boot, monkeypatch):
    now = datetime.now(timezone.utc)
    prev = _prev_fire(now)
    jobs._set_meta(MARKER, (prev - timedelta(days=7) + timedelta(minutes=2)).isoformat())   # corrió la semana pasada
    job = boot()
    delay = (job.next_run_time - now).total_seconds()
    assert 0 < delay <= jobs._STARTUP_GRACE.total_seconds() + 5, "se recupera, pero no en el mismo segundo del arranque"
    # La recupera y termina: el marcador se mueve y el SIGUIENTE redeploy ya no la repite.
    async def finished(trigger="cron"):
        return {"status": "ok"}
    monkeypatch.setattr(text_audit, "run_text_audit", finished)
    asyncio.run(jobs.seo_text_audit())
    assert getattr(boot(), "next_run_time", None) is None


def test_si_la_corrida_de_recuperacion_falla_se_vuelve_a_intentar_en_el_proximo_arranque(boot, monkeypatch):
    now = datetime.now(timezone.utc)
    jobs._set_meta(MARKER, (_prev_fire(now) - timedelta(days=7)).isoformat())

    async def failed(trigger="cron"):
        return {"status": "failed", "error": "Vendure caído"}

    monkeypatch.setattr(text_audit, "run_text_audit", failed)
    asyncio.run(jobs.seo_text_audit())
    assert boot().next_run_time is not None, "una corrida fallida no puede contar como hecha"


@pytest.mark.parametrize("marker", ["", "basura", "2999-01-01T00:00:00+00:00", "0000-00-00", None])
def test_un_marcador_ausente_roto_o_en_el_futuro_no_dispara_una_corrida_a_destiempo(boot, marker):
    with Session(engine) as s:
        for row in s.exec(select(Setting).where(Setting.key == MARKER)).all():
            s.delete(row)
        s.commit()
    if marker is not None:
        jobs._set_meta(MARKER, marker)
    assert getattr(boot(), "next_run_time", None) is None


def test_el_marcador_vive_en_la_base_y_no_en_memoria(monkeypatch):
    install(monkeypatch, FakeVendure({"ar": [raw_product(1)], None: [raw_product(1)]}))
    with Session(engine) as s:
        for row in s.exec(select(Setting).where(Setting.key == MARKER)).all():
            s.delete(row)
        s.commit()
    assert jobs._last_job_run(JOB) is None
    asyncio.run(jobs.seo_text_audit())
    with Session(engine) as s:
        row = s.get(Setting, MARKER)
    assert row is not None and datetime.fromisoformat(row.value).tzinfo is not None
    assert abs((jobs._last_job_run(JOB) - datetime.now(timezone.utc)).total_seconds()) < 60


def test_el_cron_vacio_apaga_el_job_pero_el_boton_manual_sigue_funcionando(boot, monkeypatch):
    assert boot("") is None
    assert boot("   ") is None
    result = run(monkeypatch, FakeVendure({"ar": [raw_product(1)], None: [raw_product(1)]}))
    assert result["status"] == "ok"


def test_una_corrida_a_medias_por_redeploy_se_recupera_y_no_bloquea_para_siempre(boot, monkeypatch):
    """El proceso murió con una corrida `running` (2 min de antigüedad). Tras el arranque, la corrida programada
    termina bien y, pasada la media hora, la huérfana se marca como interrumpida."""
    with Session(engine) as s:
        s.add(TextAuditRun(status="running", started_at=utcnow() - timedelta(minutes=2)))
        s.commit()
    now = datetime.now(timezone.utc)
    jobs._set_meta(MARKER, (_prev_fire(now) - timedelta(days=7)).isoformat())
    assert boot().next_run_time is not None
    result = run(monkeypatch, FakeVendure({"ar": [raw_product(1)], None: [raw_product(1)]}))
    assert result["status"] == "ok", "la huérfana reciente no puede impedir la corrida de recuperación"
    with Session(engine) as s:
        orphan = s.exec(select(TextAuditRun).where(TextAuditRun.id < result["id"])).one()
        orphan_id = orphan.id
        orphan.started_at = utcnow() - timedelta(minutes=45)
        s.add(orphan)
        s.commit()
    run(monkeypatch, FakeVendure({"ar": [raw_product(1)], None: [raw_product(1)]}))
    with Session(engine) as s:
        assert s.get(TextAuditRun, orphan_id).status == "failed"


def test_el_job_corre_por_el_camino_real_de_apscheduler(monkeypatch):
    """APScheduler invoca `func()` sin argumentos dentro de su loop: el default tiene que ser el `cron`."""
    install(monkeypatch, FakeVendure({"ar": [raw_product(1), raw_product(2)], None: [raw_product(1), raw_product(2)]}))

    async def main():
        sch = AsyncIOScheduler()
        sch.start()
        try:
            sch.add_job(jobs.seo_text_audit, "date", run_date=datetime.now(timezone.utc) + timedelta(milliseconds=200),
                        id="qa4-seo-once", max_instances=1, coalesce=True)
            for _ in range(100):
                await _REAL_SLEEP(0.05)
                with Session(engine) as s:
                    done = s.exec(select(TextAuditRun).where(TextAuditRun.status == "ok")).all()
                if done:
                    return done
            return []
        finally:
            sch.shutdown(wait=False)

    done = asyncio.run(main())
    assert len(done) == 1 and done[0].trigger == "cron" and done[0].products_total == 2


# ─── Retención ─────────────────────────────────────────────────────

def _runs_and_items():
    with Session(engine) as s:
        runs = s.exec(select(TextAuditRun).order_by(TextAuditRun.id)).all()
        item_runs = {i.run_id for i in s.exec(select(TextAuditItem))}
    return runs, item_runs


def test_retiene_exactamente_las_ultimas_8_y_sus_filas(monkeypatch):
    fake = FakeVendure({"ar": [raw_product(1), raw_product(2)], None: [raw_product(1), raw_product(2)]})
    ids = [run(monkeypatch, fake)["id"] for _ in range(20)]
    runs, item_runs = _runs_and_items()
    assert [r.id for r in runs] == ids[-8:]
    assert item_runs == set(ids[-8:])
    with Session(engine) as s:
        assert text_audit.latest_run(s).id == ids[-1]


def test_hasta_8_corridas_no_se_borra_nada(monkeypatch):
    fake = FakeVendure({"ar": [raw_product(1)], None: [raw_product(1)]})
    ids = [run(monkeypatch, fake)["id"] for _ in range(8)]
    runs, item_runs = _runs_and_items()
    assert [r.id for r in runs] == ids and item_runs == set(ids)


def test_las_corridas_fallidas_no_hacen_perder_la_ultima_buena_y_la_retencion_sigue_siendo_8(monkeypatch):
    good = FakeVendure({"ar": [raw_product(1)], None: [raw_product(1)]})
    bad = FakeVendure({"ar": [], None: []})
    bad.fail_tokens = {"ar", None}
    ok_ids = [run(monkeypatch, good)["id"] for _ in range(3)]
    for _ in range(12):
        assert run(monkeypatch, bad)["status"] == "failed"
    last_ok = run(monkeypatch, good)
    runs, item_runs = _runs_and_items()
    assert len(runs) == 8 and runs[-1].id == last_ok["id"]
    assert last_ok["id"] in item_runs
    with Session(engine) as s:
        assert text_audit.latest_run(s).id == last_ok["id"]
    assert len(ok_ids) == 3


def test_la_poda_no_toca_una_corrida_en_curso_mas_nueva(monkeypatch):
    fake = FakeVendure({"ar": [raw_product(1)], None: [raw_product(1)]})
    for _ in range(9):
        run(monkeypatch, fake)
    with Session(engine) as s:
        running = TextAuditRun(status="running")
        s.add(running)
        s.commit()
        rid = running.id
    run(monkeypatch, fake)                      # esta termina y poda
    runs, _ = _runs_and_items()
    assert rid in {r.id for r in runs} and len(runs) == 8
