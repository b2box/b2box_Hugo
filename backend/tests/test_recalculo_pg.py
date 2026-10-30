"""K-4: el recálculo al cambiar en Vendure sobre un Postgres 16 descartable. Se salta sola sin HUGO_TEST_PG_URL (ver
test_price_review_pg.py para levantarlo).

  * el arranque agrega SOLO `recalc_at` y `recalc_before` a market_price_snapshot: nullables, la fecha sin zona (UTC, como el
    resto), sin reescribir la tabla ni tocar las filas; tres arranques dan lo mismo;
  * el SQL de vuelta atrás del README, copiado tal cual, las saca sin perder datos y volver a subir da lo mismo;
  * el recálculo completo (snapshot en el lugar, contadores, ficha, reactivación por el protocolo, AuditLog y mail) corre
    sobre Postgres, y el vigía de las fichas vivas (cada 30 min) también.
"""

from __future__ import annotations

import json
import os
import re
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.db import session  # noqa: E402
from app.db.models import MarketPriceSnapshot  # noqa: E402
from app.pricing import price_recalc, price_review_digest  # noqa: E402
from tests import review_decide as k  # noqa: E402
from tests import review_world as w  # noqa: E402
from tests.test_price_review_pg import README, _old_dump, _seed_old_rows, old_schema  # noqa: E402,F401
from tests.test_qa_tiendas_pg import PG_URL  # noqa: E402
from tests.test_recalculo_al_cambiar import CHEAPER, FIXED, LATER, confirm, in_vendure, night, recalc  # noqa: E402

pytestmark = pytest.mark.skipif(not PG_URL, reason="falta HUGO_TEST_PG_URL (Postgres descartable)")
COLS = {"recalc_at", "recalc_before", "recalc_flag"}


def _rollback_sql() -> str:
    for block in re.findall(r"```sql\n(.*?)```", README, flags=re.S):
        if "recalc_before" in block:
            return block
    raise AssertionError("el README no trae el SQL de vuelta atrás del recálculo")


def _snapshot_cols(engine) -> dict:
    return {c["name"]: (str(c["type"]), c["nullable"]) for c in inspect(engine).get_columns("market_price_snapshot")}


def test_boot_adds_only_the_two_nullable_columns_and_never_touches_the_rows(old_schema):  # noqa: F811  (fixture importada)
    before = _snapshot_cols(old_schema)
    assert not COLS & set(before)
    session.init_db()
    _seed_old_rows(old_schema)                                    # el código anterior sigue insertando sin las columnas
    rows = _old_dump(old_schema)
    after = _snapshot_cols(old_schema)
    assert COLS <= set(after) and all(after[c][1] for c in COLS)
    assert "WITH TIME ZONE" not in after["recalc_at"][0].upper() and "TIMESTAMP" in after["recalc_at"][0].upper()
    with old_schema.connect() as c:
        node = c.execute(text("SELECT pg_relation_filenode('market_price_snapshot')")).scalar()
    session.init_db()
    session.init_db()
    assert _snapshot_cols(old_schema) == after and _old_dump(old_schema) == rows
    with old_schema.connect() as c:
        assert c.execute(text("SELECT pg_relation_filenode('market_price_snapshot')")).scalar() == node
        assert c.execute(text("SELECT count(*) FROM market_price_snapshot WHERE recalc_at IS NOT NULL "
                              "OR recalc_before IS NOT NULL OR recalc_flag IS NOT NULL")).scalar() == 0


def test_readme_rollback_sql_removes_the_columns_and_the_settings_and_boot_goes_back_up(old_schema):  # noqa: F811  (fixture importada)
    session.init_db()
    up = _snapshot_cols(old_schema)
    _seed_old_rows(old_schema)
    rows = _old_dump(old_schema)
    with old_schema.begin() as c:
        c.execute(text("UPDATE market_price_snapshot SET recalc_at = now(), recalc_before = '{}' WHERE id = 1"))
        c.execute(text("INSERT INTO settings (key, value, updated_at) VALUES ('pm_recalc_daily_max', '50', now()), "
                       "('pm_recalc_reads', '2026-10-29:3', now()), ('pm_recalc_watch_daily_max', '60', now()), "
                       "('pm_recalc_watch_reads', '2026-10-30:7', now()), "
                       "('vendure_review_requests_recalc', '2026-10-30:2', now()), ('pm_mode', '1', now())"))
    with old_schema.begin() as c:
        for stmt in [s.strip() for s in _rollback_sql().split(";") if s.strip() and not s.strip().startswith("--")]:
            c.execute(text(stmt))
    assert not COLS & set(_snapshot_cols(old_schema)) and _old_dump(old_schema) == rows
    with old_schema.connect() as c:
        assert dict(c.execute(text("SELECT key, value FROM settings")).all()) == {"pm_mode": "1"}
    session.init_db()
    assert _snapshot_cols(old_schema) == up


async def test_the_recalculation_and_the_automatic_reactivation_work_on_postgres(old_schema, monkeypatch):  # noqa: F811  (fixture importada)
    session.init_db()
    fake = k.setup(monkeypatch)
    price_recalc._pending.clear()
    monkeypatch.setattr(price_recalc, "_watched_at", {})
    run, sid = night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    _, other = night("rv2", run=run)
    proposed = w.make_item("rv2")
    in_vendure(fake, "rv2", FIXED)
    assert (await recalc("rv1", "rv2"))["recalculado"] == 2
    assert w.item(item_id).state == "corrigiendo"                                 # una sola lectura no alcanza
    await confirm()
    with Session(old_schema) as s:
        snap = s.get(MarketPriceSnapshot, sid)
        assert snap.color == "verde" and snap.recalc_at == LATER and json.loads(snap.recalc_before)["color"] == "rojo"
    assert (w.run_of(run).n_rojo, w.run_of(run).n_verde) == (0, 2)
    assert w.item(item_id).state == "corregido" and w.item(item_id).open_key is None and fake.writes() == [True]
    assert w.item(proposed).state == "cerrado_auto"
    assert [a.action for a in k.audit("rv1")] == ["price_review_enabled"]
    with Session(old_schema, expire_on_commit=False) as s:
        data = price_review_digest.collect(s, LATER + timedelta(hours=1))
        assert [i.id for _, i in data.reactivated] == [item_id]
        price_review_digest.mark_sent(s, data)
        s.commit()
    k.clean_audit()


async def test_the_watch_of_live_items_works_on_postgres(old_schema, monkeypatch):  # noqa: F811  (fixture importada)
    session.init_db()
    fake = k.setup(monkeypatch)
    monkeypatch.setattr(price_recalc, "_watched_at", {})
    run, sid = night("rv1")
    kept = w.make_item("rv1", "mantenido", kept_ratio=1.277)
    night("rv2", run=run)
    w.make_item("rv2", "despublicado", closed_at=w.NOW)
    in_vendure(fake, "rv1", CHEAPER)
    in_vendure(fake, "rv2", CHEAPER)
    assert await price_recalc.watch_live_items(now=LATER) == {"recalculado": 1}
    assert [r["variables"]["id"] for r in fake.requests if r["op"] == "PricedProduct"] == ["rv1"]
    with Session(old_schema) as s:
        assert s.get(MarketPriceSnapshot, sid).our_price_cents == CHEAPER
    assert w.item(kept).state == "mantenido" and w.events("recalculado")
    k.clean_audit()
