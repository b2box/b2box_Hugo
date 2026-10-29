"""La bandeja de revisión sobre un Postgres 16 descartable: la migración por `init_db()` y la pasada completa.

Se salta sola sin HUGO_TEST_PG_URL:

    docker run --rm -d --name hugo-bandeja-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=hugo_test -p 55531:5432 postgres:16-alpine
    HUGO_TEST_PG_URL=postgresql+psycopg://postgres:pw@localhost:55531/hugo_test \\
        VENDURE_API_URL=https://example.invalid/admin-api pytest backend/tests/test_price_review_pg.py

El esquema de partida (`golden/pg_schema_origin_main_74ec5fb.sql`) salió de un `pg_dump -s` de una base creada por el
`init_db()` de origin/main (74ec5fb: con tiendas, oficina y auditoría de textos). Todo corre en un schema propio que se
borra al final.

  * el arranque agrega EXACTAMENTE price_review_item y price_review_event (con sus índices) y 5 columnas opcionales; no
    toca nada más ni cambia el tipo de ninguna columna que ya estaba;
  * es idempotente (3 arranques = mismo esquema), no reescribe la tabla grande y conserva todas las filas;
  * el código anterior sigue insertando sobre el esquema nuevo;
  * el SQL de rollback del README, copiado tal cual, deja el esquema EXACTAMENTE como estaba y volver a subir da lo mismo;
  * la pasada, el índice único de la ficha abierta, el registro de solo agregar, la API y el mail funcionan sobre Postgres;
  * (PR3) todos los estados entran en VARCHAR(16) (el viejo «buscando_proveedor» no entraba), y las decisiones, el
    cambio de estado atómico, Deshacer y la conciliación funcionan sobre Postgres.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from datetime import timedelta
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import auth  # noqa: E402
from app import main as main_mod  # noqa: E402
from app.db import session  # noqa: E402
from app.db.models import EventLogIsAppendOnly, PriceReviewEvent, PriceReviewItem  # noqa: E402
from app.pricing import price_review as pr  # noqa: E402
from app.pricing import price_review_actions as actions  # noqa: E402
from tests import review_decide as k  # noqa: E402
from tests import review_world as w  # noqa: E402
from tests.review_fixtures import FakeReviewVendure, engine_variant, install  # noqa: E402
from tests.test_price_monitor_routes import _use_settings  # noqa: E402
from tests.test_qa_tiendas_pg import PG_URL, _patch_engine, _pg_dsn, _schema_url  # noqa: E402

pytestmark = pytest.mark.skipif(not PG_URL, reason="falta HUGO_TEST_PG_URL (Postgres descartable)")
SCHEMA_SQL = (Path(__file__).parent / "golden" / "pg_schema_origin_main_74ec5fb.sql").read_text()
README = (Path(__file__).resolve().parents[2] / "README.md").read_text()

NEW_TABLES = {"price_review_item", "price_review_event"}
NEW_COLS = {"market_price_snapshot": {"data_at", "review_flag"},
            "price_monitor_run": {"review_at", "review_summary", "n_flipped"}}
# Lo que agregan los PRs que entran antes que la bandeja y no está en el esquema de partida: las tablas del estimado por
# foto, de reservas de la cola y del gasto de la búsqueda con Apify y el caché del radar de ML (las prueban
# test_qa_estimate_migracion.py, test_qa_apify_ml_search_pg.py y compañía) y las columnas de «No medible» y del juez de
# fix/semaforo-sin-dato (#25, las prueban test_qa_postgres_migration.py y compañía). Las crea el mismo arranque; acá no se
# miran.
OTHER_PRS_TABLES = {"estimate_usage", "lookup_estimate", "ml_web_claim", "apify_spend", "radar_ml_supply_cache"}
# Lo mismo con las del recálculo al cambiar en Vendure, que entra DESPUÉS (las prueba test_recalculo_pg.py).
OTHER_PRS_COLS = {"market_price_snapshot": {"unmeasurable", "recalc_at", "recalc_before"},
                  "price_monitor_run": {"llm_failures", "llm_skipped_cap", "llm_skipped_api", "llm_skipped_photos",
                                        "llm_skipped_cut", "llm_status", "n_no_medible"}}
NOW = w.NOW


def _rollback_sql() -> str:
    for block in re.findall(r"```sql\n(.*?)```", README, flags=re.S):
        if "DROP TABLE IF EXISTS price_review_event" in block:
            return block
    raise AssertionError("el README no trae el SQL de rollback de la bandeja")


def _shape(engine) -> dict:
    insp = inspect(engine)
    out = {}
    for table in sorted(set(insp.get_table_names()) - OTHER_PRS_TABLES):
        cols = {c["name"]: (str(c["type"]), c["nullable"], str(c.get("default"))) for c in insp.get_columns(table)
                if c["name"] not in OTHER_PRS_COLS.get(table, ())}
        idx = {i["name"]: (tuple(i["column_names"]), bool(i["unique"])) for i in insp.get_indexes(table)}
        out[table] = (cols, idx)
    return out


@pytest.fixture
def old_schema(monkeypatch):
    schema = "rev_" + uuid.uuid4().hex[:10]
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
                       "n_est_verde, n_est_amarillo, n_est_rojo, n_solo_diferentes, oficina_fresh, n_oficina_ok) SELECT now(), 'ok', 0, "
                       "'cron', 200, 200, 1, 2, 0, 0, 1, 0, 0, 2, 40, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0 FROM generate_series(1, 5)"))
        c.execute(text("INSERT INTO market_price_snapshot (run_id, product_id, captured_at, ml_status, color, ml_listing_count, "
                       "ml_seller_count, candidates_count, ambiguous_count, commission_pct, shipping_cents, ml_currency, product_enabled, "
                       "similar_count, web_searches, web_bytes, other_count, estimated_listing_count, our_price_cents, product_name, "
                       "price_basis, matched_listings, ml_median_cents, match_origin) SELECT 1 + (g - 1) / 20, ((g - 1) % 20 + 1)::text, "
                       "now(), CASE WHEN g % 3 = 0 THEN 'ok' ELSE 'no_data' END, 'verde', 0, 0, 0, 0, 13.0, 0, 'ARS', true, 0, 0, 0, 0, 0, "
                       "10000, 'Producto ' || g, 'ml', '[{\"ml_id\": \"MLA1\"}]', 25000, 'api' FROM generate_series(1, 100) g"))


def _old_dump(engine) -> list:
    with engine.connect() as c:
        snaps = c.execute(text("SELECT id, run_id, product_id, ml_status, color, ml_median_cents, match_origin, matched_listings, "
                               "product_name, price_basis FROM market_price_snapshot ORDER BY id")).all()
        runs = c.execute(text("SELECT id, status, total_products, n_ok, ml_requests_used FROM price_monitor_run ORDER BY id")).all()
    return [list(map(tuple, snaps)), list(map(tuple, runs))]


# ─── migración ───────────────────────────────────────────────────


def test_the_starting_schema_is_the_one_of_origin_main(old_schema):
    shape = _shape(old_schema)
    assert not NEW_TABLES & set(shape)
    for table, cols in NEW_COLS.items():
        assert not cols & set(shape[table][0]), table
    assert {"text_audit_run", "ml_web_result", "store_match"} <= set(shape)


def test_boot_adds_exactly_the_new_objects_and_changes_nothing_else(old_schema):
    before = _shape(old_schema)
    session.init_db()
    after = _shape(old_schema)
    assert set(after) - set(before) == NEW_TABLES and set(before) <= set(after)
    for table in before:
        b_cols, b_idx = before[table]
        a_cols, a_idx = after[table]
        assert {k: v for k, v in a_cols.items() if k in b_cols} == b_cols, f"{table}: cambió una columna que ya existía"
        assert a_idx == b_idx, f"{table}: cambió un índice"
        assert set(a_cols) - set(b_cols) == NEW_COLS.get(table, set()), table
    item_cols, item_idx = after["price_review_item"]
    assert item_idx["ix_pri_open_key"] == (("open_key",), True)
    assert item_idx["ix_pri_state_since"] == (("state", "state_since"), False)
    assert item_idx["ix_pri_product_opened"] == (("product_id", "opened_at"), False)
    assert item_cols["open_key"][1] is True and "timestamp" in item_cols["state_since"][0].lower()
    assert "WITH TIME ZONE" not in item_cols["opened_at"][0].upper(), "las fechas van sin zona (UTC), como el resto"
    ev_cols, ev_idx = after["price_review_event"]
    assert set(ev_idx) >= {"ix_pre_item_at", "ix_pre_notified"} and "WITH TIME ZONE" not in ev_cols["at"][0].upper()
    for table, names in NEW_COLS.items():
        assert all(after[table][0][n][1] for n in names), "las columnas nuevas tienen que aceptar NULL (rollback de código seguro)"


def test_three_boots_leave_the_same_schema_and_never_touch_the_rows(old_schema):
    _seed_old_rows(old_schema)
    rows_before = _old_dump(old_schema)
    session.init_db()
    first = _shape(old_schema)
    session.init_db()
    session.init_db()
    assert _shape(old_schema) == first and _old_dump(old_schema) == rows_before
    with old_schema.connect() as c:
        assert c.execute(text("SELECT count(*) FROM market_price_snapshot WHERE data_at IS NOT NULL OR review_flag IS NOT NULL")).scalar() == 0
        assert c.execute(text("SELECT DISTINCT n_flipped FROM price_monitor_run")).scalars().all() == [0]
        assert c.execute(text("SELECT count(*) FROM price_monitor_run WHERE review_at IS NOT NULL")).scalar() == 0


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
        row = c.execute(text("SELECT data_at, review_flag FROM market_price_snapshot LIMIT 1")).one()
        run = c.execute(text("SELECT review_at, review_summary, n_flipped FROM price_monitor_run LIMIT 1")).one()
    assert tuple(row) == (None, None) and tuple(run)[:2] == (None, None)


def test_readme_rollback_sql_restores_the_original_schema_and_boot_goes_back_up(old_schema):
    before = _shape(old_schema)
    _seed_old_rows(old_schema)
    rows = _old_dump(old_schema)
    session.init_db()
    up = _shape(old_schema)
    with old_schema.begin() as c:                       # algo para borrar: los settings y una fila de cada tabla nueva
        c.execute(text("INSERT INTO settings (key, value, updated_at) VALUES ('pm_review_sla_days', '9', now()), "
                       "('pm_diag_box_z', '4', now()), ('pm_mode', '2', now()), ('_meta:last_run:price_review_digest', 'x', now())"))
        c.execute(text("INSERT INTO price_review_item (product_id, state, state_since, opened_at, mode_at_open, streak_nights, "
                       "reopen_count, version) VALUES ('x', 'propuesto', now(), now(), 1, 2, 0, 1)"))
    with old_schema.begin() as c:
        for stmt in [s.strip() for s in _rollback_sql().split(";") if s.strip()]:
            c.execute(text(stmt))
    assert _shape(old_schema) == before, "el SQL del README no deja el esquema como estaba"
    assert _old_dump(old_schema) == rows, "el rollback no pierde datos viejos"
    with old_schema.connect() as c:
        left = dict(c.execute(text("SELECT key, value FROM settings")).all())
    assert left == {"pm_mode": "1"}, "el rollback borra los settings nuevos y baja el modo 2 a 1"
    session.init_db()
    assert _shape(old_schema) == up


# ─── la pasada completa sobre Postgres ────────────────────────────


@pytest.fixture
def pg(old_schema, monkeypatch):
    session.init_db()
    w.reset_db()
    w.set_setting("pm_mode", 1)
    fake = FakeReviewVendure()
    install(monkeypatch, fake)
    v = engine_variant(0)
    fake.breakdowns[v["variant"]] = v["breakdowns"]
    fake.variants[v["variant"]] = {"id": "101", "priceWithTax": v["priceWithTax"], "currencyCode": "ARS",
                                   "customFields": v["customFields"], "bulkPriceTiers": v["tiers"]}
    _use_settings(monkeypatch)
    return old_schema


async def _two_nights(pids):
    prev = w.make_run(started=NOW - timedelta(days=1))
    today = w.make_run()
    for pid in pids:
        w.add_snap(prev, pid, flag="firme", data_at=NOW - timedelta(days=1), captured_at=NOW - timedelta(days=1))
        w.add_snap(today, pid)
    return today


async def test_the_pass_opens_the_fichas_and_is_idempotent_on_postgres(pg):
    today = await _two_nights(["a", "b", "c"])
    first = await pr.process_run(today, now=NOW)
    assert first["nuevas"] == 3 and first["firmes"] == 3
    assert await pr.process_run(today, now=NOW) is None
    assert sorted(i.product_id for i in w.items()) == ["a", "b", "c"] and len(w.events("propuesto")) == 3
    run = w.run_of(today)
    assert run.review_at is not None and run.n_flipped == 0
    assert w.snap_of(today, "a").review_flag == "firme" and w.snap_of(today, "a").data_at == NOW


async def test_a_ficha_closes_and_a_new_one_opens_for_the_same_product_on_postgres(pg):
    today = await _two_nights(["a"])
    await pr.process_run(today, now=NOW)
    r3 = w.make_run(started=NOW + timedelta(days=1))
    w.add_snap(r3, "a", color="verde", our=500_000)
    await pr.process_run(r3, now=NOW + timedelta(days=1))
    with Session(pg) as s:
        [closed] = s.exec(select(PriceReviewItem)).all()
        assert closed.state == "cerrado_auto" and closed.open_key is None
        s.add(PriceReviewItem(product_id="a", open_key="a", state="propuesto"))     # la nueva ficha
        s.add(PriceReviewItem(product_id="a", open_key=None, state="cerrado_auto"))  # y otra cerrada: los NULL no chocan
        s.commit()
        s.add(PriceReviewItem(product_id="a", open_key="a", state="propuesto"))
        with pytest.raises(IntegrityError):
            s.commit()


async def test_events_are_append_only_on_postgres(pg):
    today = await _two_nights(["a"])
    await pr.process_run(today, now=NOW)
    with Session(pg) as s:
        ev = s.exec(select(PriceReviewEvent)).one()
        ev.motive_text = "otra cosa"
        s.add(ev)
        with pytest.raises(EventLogIsAppendOnly):
            s.flush()
        s.rollback()
        ev = s.exec(select(PriceReviewEvent)).one()
        ev.notified_at = NOW
        s.add(ev)
        s.commit()
        assert s.exec(select(PriceReviewEvent)).one().notified_at == NOW


async def test_the_api_works_on_postgres_including_search_with_special_characters(pg, monkeypatch):
    from app.api import price_review_routes as routes
    from app.config import Settings

    cfg = Settings(vendure_api_url="https://example.invalid/admin-api", price_review_viewers="pao@b2box.pro")
    monkeypatch.setattr(routes, "get_settings", lambda: cfg)             # Pao es responsable de costos: ve las 10 partes
    today = await _two_nights(["a", "b"])
    await pr.process_run(today, now=NOW)
    c = TestClient(main_mod.app, raise_server_exceptions=False)
    c.cookies.set(auth.COOKIE_NAME, auth.issue_session_token("pao@b2box.pro"))
    assert c.get("/api/price-review/summary").json()["pending"] == 2
    assert c.get("/api/price-review/items").json()["total"] == 2
    assert c.get("/api/price-review/items?q=Producto%20a").json()["total"] == 1
    for q in ("%00", "a%00b", "%25", "_", "%27%3B--"):
        assert c.get(f"/api/price-review/items?q={q}").status_code == 200, q
    item = w.items()[0]
    d = c.get(f"/api/price-review/items/{item.id}").json()
    assert d["state"] == "propuesto" and len(d["breakdown"]["components"]) == 10
    snaps = c.get(f"/api/price-monitor/snapshots?run_id={today}&review=firme").json()
    assert snaps["total"] == 2


async def test_the_digest_marks_events_after_sending_on_postgres(pg, monkeypatch):
    from app.config import Settings
    from app.notifier import email as email_chan
    from app.pricing import price_review_digest as digest

    sent = []

    async def fake_send(msg, **kw):
        sent.append(msg)

    cfg = Settings(vendure_api_url="https://example.invalid/admin-api", alert_smtp_user="u", alert_smtp_pass="p",
                   pm_review_emails="pao@b2box.pro")
    monkeypatch.setattr(email_chan.aiosmtplib, "send", fake_send)
    for mod in (email_chan, digest):
        monkeypatch.setattr(mod, "get_settings", lambda: cfg)
    today = await _two_nights(["a", "b"])
    await pr.process_run(today, now=NOW)
    assert (await digest.send_digest(NOW))["sent"] is True
    assert (await digest.send_digest(NOW))["reason"] == "sin novedades" and len(sent) == 1
    assert all(e.notified_at is not None for e in w.events())


# ─── PR3: estados y decisiones sobre Postgres ─────────────────────


def test_every_review_state_fits_in_postgres_and_the_old_name_did_not(pg):
    from sqlalchemy.exc import DataError

    from app.db.models import REVIEW_STATES

    assert "buscando_prov" in REVIEW_STATES and "buscando_proveedor" not in REVIEW_STATES
    with Session(pg) as s:
        for state in REVIEW_STATES:
            item = PriceReviewItem(product_id=f"s-{state}", state=state)
            s.add(item)
            s.flush()
            s.add(PriceReviewEvent(item_id=item.id, product_id=item.product_id, actor="hugo:auto", kind="propuesto",
                                   from_state=state, to_state=state))
        s.commit()
        assert sorted(s.exec(select(PriceReviewItem.state)).all()) == sorted(REVIEW_STATES)
        s.add(PriceReviewItem(product_id="viejo", state="buscando_proveedor"))
        with pytest.raises(DataError):
            s.commit()


async def test_decisions_undo_and_reconcile_work_on_postgres(pg, monkeypatch):
    fake = k.setup(monkeypatch)
    c = k.client()
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    stale, _ = actions._load(item_id)                       # otra pestaña la leyó antes del clic
    assert k.post(c, item_id, "approve", expected_state="propuesto").json()["state"] == "en_revision"
    r = k.decide(c, item_id, "despublicar", "propuesto", "calidad")
    assert r.status_code == 409 and r.json()["detail"]["state"] == "en_revision"
    with Session(pg) as s:                                   # el UPDATE … WHERE version de una lectura vieja no pasa
        assert pr.move_item(s, stale, "despublicado") is False
    assert k.decide(c, item_id, "mantener", "en_revision", "nicho", "Se vende poco pero sirve").json()["state"] == "mantenido"
    assert k.post(c, item_id, "undo", expected_state="mantenido").json()["state"] == "en_revision"
    assert fake.writes() == [False, True, False] and w.item(item_id).hugo_disabled is True
    assert len(k.audit("rv1")) == 3
    # una intención a medias se concilia (la consulta con GROUP BY de la conciliación corre en Postgres)
    with Session(pg) as s:
        it = s.get(PriceReviewItem, item_id)
        ev = pr.add_event(s, it, "decision_corregir", actor=k.PAO, from_state="en_revision", to_state="corrigiendo",
                          payload={"antes": {"state_since": it.state_since.isoformat()}})
        pr.move_item(s, it, "corrigiendo")
        s.flush()
        pr.add_event(s, it, "intent", actor=k.PAO, from_state="en_revision", to_state="corrigiendo", enabled_before=True,
                     enabled_after=False, payload={"accion": "corregir", "evento": ev.id,
                                                   "antes": {"state_since": it.state_since.isoformat()}})
        s.commit()
    from app.clock import utcnow

    assert await actions.reconcile(now=utcnow() + timedelta(minutes=11)) == 1
    assert w.item(item_id).state == "corrigiendo"
    k.clean_audit()
