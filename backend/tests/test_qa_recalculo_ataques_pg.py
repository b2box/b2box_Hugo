"""QA del recálculo sobre Postgres 16: dos productos de la misma corrida a la vez (el lock de la fila de la corrida) y
reactivaciones en paralelo con el tope. Se salta sola sin HUGO_TEST_PG_URL."""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import session  # noqa: E402
from app.pricing import price_recalc  # noqa: E402
from tests import review_decide as k  # noqa: E402
from tests import review_world as w  # noqa: E402
from tests.test_price_review_pg import old_schema  # noqa: E402,F401
from tests.test_qa_tiendas_pg import PG_URL  # noqa: E402
from tests.test_recalculo_al_cambiar import FIXED, confirm, in_vendure, night, recalc  # noqa: E402

pytestmark = pytest.mark.skipif(not PG_URL, reason="falta HUGO_TEST_PG_URL (Postgres descartable)")


async def test_qa_k4_a_mass_change_keeps_the_run_counters_exact_and_the_reactivation_cap_on_postgres(old_schema, monkeypatch):  # noqa: F811
    session.init_db()
    fake = k.setup(monkeypatch)
    price_recalc._pending.clear()
    monkeypatch.setattr(price_recalc, "_watched_at", {})
    w.set_setting("pm_recalc_reactivations_daily_max", 5)
    run = w.make_run()
    pids = [f"rv{i:03d}" for i in range(120)]
    for i, pid in enumerate(pids):
        night(pid, run=run)
        if i % 3 == 0:                                       # 40 apagadas por Hugo
            k.disabled_by_hugo(pid, "corrigiendo")
            in_vendure(fake, pid, FIXED, enabled=False)
        else:
            in_vendure(fake, pid, FIXED if i % 2 else 1_000_000)
    out = await recalc(*pids)
    assert out.get("error", 0) == 0, out
    out = await confirm()                                   # la segunda lectura (ajustes 30-oct): recién ahí reactiva
    assert out.get("error", 0) == 0, out
    with old_schema.connect() as c:
        colors = dict(c.execute(text("SELECT color, count(*) FROM market_price_snapshot WHERE run_id = :r GROUP BY color"),
                                {"r": run}).all())
    r = w.run_of(run)
    assert (r.n_verde, r.n_rojo) == (colors.get("verde", 0), colors.get("rojo", 0))
    assert fake.writes() == [True] * 5 and len(w.items("corregido")) == 5
    assert len([e for e in w.events("aviso_reactivar")]) == 35
    k.clean_audit()


async def test_qa_k4_the_tier_policy_audit_and_the_price_floor_hold_work_on_postgres(old_schema, monkeypatch):  # noqa: F811
    """El LIKE sobre audit_log.before (varchar en prod) y la consulta de «precio_bajo» pegado, en Postgres 16."""
    from datetime import timedelta

    from sqlmodel import Session

    from app.db.models import AuditLog
    from tests.test_recalculo_al_cambiar import LATER, _tiered, confirm, product

    session.init_db()
    fake = k.setup(monkeypatch)
    price_recalc._pending.clear()
    monkeypatch.setattr(price_recalc, "_watched_at", {})
    monkeypatch.setattr(price_recalc, "_read_marks", {})
    w.add_snap(w.make_run(), "rv1", our=1_125_000, tier="tier:min_qty=10", flag="firme")
    k.disabled_by_hugo("rv1", "corrigiendo")
    fake.products.append(_tiered("rv1", enabled=False))
    fake.enabled["rv1"] = False
    w.set_setting("pm_tier_policy", 1)
    with Session(old_schema) as s:
        s.add(AuditLog(action="setting_changed", source="manual", product_id="(settings)", created_at=w.NOW + timedelta(hours=1),
                       detail="cambió pm_tier_policy", before='{"pm_tier_policy": 0}', after='{"pm_tier_policy": 1}'))
        s.commit()
    assert await recalc("rv1") == {"sin_cambio": 1}
    night("rv2", our=1_000_000)
    held = k.disabled_by_hugo("rv2", "corrigiendo")
    in_vendure(fake, "rv2", 10_000, enabled=False)
    await recalc("rv2")
    await confirm()
    fake.products[-1] = product("rv2", FIXED, enabled=False)
    await recalc("rv2", now=LATER + timedelta(minutes=40))
    await confirm(80)
    assert w.item(held).state == "corrigiendo" and fake.writes() == []
    k.clean_audit()
