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
from tests.test_recalculo_al_cambiar import FIXED, in_vendure, night, recalc  # noqa: E402

pytestmark = pytest.mark.skipif(not PG_URL, reason="falta HUGO_TEST_PG_URL (Postgres descartable)")


async def test_qa_k4_a_mass_change_keeps_the_run_counters_exact_and_the_reactivation_cap_on_postgres(old_schema, monkeypatch):  # noqa: F811
    session.init_db()
    fake = k.setup(monkeypatch)
    price_recalc._pending.clear()
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
    with old_schema.connect() as c:
        colors = dict(c.execute(text("SELECT color, count(*) FROM market_price_snapshot WHERE run_id = :r GROUP BY color"),
                                {"r": run}).all())
    r = w.run_of(run)
    assert (r.n_verde, r.n_rojo) == (colors.get("verde", 0), colors.get("rojo", 0))
    assert fake.writes() == [True] * 5 and len(w.items("corregido")) == 5
    assert len([e for e in w.events("aviso_reactivar")]) == 35
    k.clean_audit()
