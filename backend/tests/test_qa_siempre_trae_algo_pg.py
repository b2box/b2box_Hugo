"""«Siempre trae algo» sobre un Postgres REAL (criterio 6 y el camino de la API con SQL de Postgres).

Se salta sola sin `HUGO_TEST_PG_URL`:

    docker run --rm -d --name hugo-pg -e POSTGRES_PASSWORD=qa -p 55432:5432 postgres:16-alpine
    HUGO_TEST_PG_URL=postgresql+psycopg://postgres:qa@localhost:55432/postgres \\
        VENDURE_API_URL=https://example.invalid/admin-api pytest backend/tests/test_qa_siempre_trae_algo_pg.py

A diferencia de test_qa_postgres_migration.py (que arma la base como estaba ANTES de toda la rama), acá la base
está como la dejó el deploy de 9c6dc45: con todo lo anterior y SIN las columnas de este PR (otros listados,
estado, color estimado, contadores n_est_* y `label`), con las fechas en timestamptz, con filas y marcas ya
cargadas. Después corre la migración y la API (filtros, "Es el mismo", "No es el mismo", deshacer) contra ella.
Todo en un schema propio que se borra al final.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, Table, create_engine, inspect, text
from sqlmodel import Session, SQLModel, select

from app import auth, runtime
from app import main as main_mod
from app.db import models, session  # noqa: F401
from app.db.models import MarketMatchFeedback, MarketPriceSnapshot, PriceMonitorRun
from app.pricing import daily_budget, market_ml, match_feedback, price_monitor
from tests.test_price_monitor_routes import _env  # noqa: F401

PG_URL = os.environ.get("HUGO_TEST_PG_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL, reason="falta HUGO_TEST_PG_URL (Postgres descartable)")

# Lo que agregan los 4 commits de este PR (5986080..3e9ef5a) sobre 9c6dc45.
PR_SNAPSHOT_COLS = {"other_listings", "other_count", "unpriced_listings", "match_state", "estimated_color",
                    "estimated_margin_pct", "estimated_median_cents", "estimated_listing_count", "estimated_from"}
PR_RUN_COLS = {"n_est_verde", "n_est_amarillo", "n_est_rojo", "n_solo_diferentes"}
PR_FEEDBACK_COLS = {"label"}
DT_COLS = (("price_monitor_run", ("started_at", "finished_at")), ("market_price_snapshot", ("captured_at",)),
           ("ml_seller_cache", ("fetched_at",)), ("market_match_feedback", ("created_at",)))


@pytest.fixture
def pg(monkeypatch):
    schema = "qa_" + uuid.uuid4().hex[:10]
    admin = create_engine(PG_URL)
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(PG_URL, connect_args={"options": f"-csearch_path={schema}"})
    # la app y sus módulos importaron `engine` por nombre: se apunta en todos
    for mod in (session, price_monitor, match_feedback, runtime, daily_budget, market_ml):
        if hasattr(mod, "engine"):
            monkeypatch.setattr(mod, "engine", engine)
    yield engine
    engine.dispose()
    with admin.begin() as c:
        c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def _tables_as_deployed_at_9c6dc45(engine) -> None:
    old = MetaData()
    for name, new_cols in (("price_monitor_run", PR_RUN_COLS), ("market_price_snapshot", PR_SNAPSHOT_COLS),
                           ("ml_seller_cache", set()), ("settings", set()),
                           ("market_match_feedback", PR_FEEDBACK_COLS)):
        src = SQLModel.metadata.tables[name]
        Table(name, old, *[c._copy() for c in src.columns if c.name not in new_cols])
    old.create_all(engine)
    with engine.begin() as c:
        for table, cols in DT_COLS:
            for col in cols:
                c.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN "{col}" TYPE timestamp with time zone '
                               f'USING "{col}" AT TIME ZONE \'UTC\''))
        c.execute(text("INSERT INTO price_monitor_run (started_at, status, mode, trigger, total_products, processed, "
                       "n_ok, n_no_data, n_failed, n_skipped, n_verde, n_amarillo, n_rojo, n_sin_dato, "
                       "ml_requests_used, llm_calls, llm_input_tokens, llm_output_tokens, llm_cost_usd, resumed_count, "
                       "web_searches, web_bytes, web_blocked, n_web_ok, n_con_similares) "
                       "VALUES (now(), 'ok', 0, 'cron', 3, 3, 1, 2, 0, 0, 1, 0, 0, 2, 10, 0, 0, 0, 0, 0, 4, 900, 0, 0, 1)"))
        for pid, st, col, sim in (("11", "ok", "verde", 0), ("12", "no_data", "sin_dato", 2), ("13", "no_data", "sin_dato", 0)):
            c.execute(text("INSERT INTO market_price_snapshot (run_id, product_id, captured_at, ml_status, color, "
                           "ml_listing_count, ml_seller_count, candidates_count, ambiguous_count, commission_pct, "
                           "shipping_cents, ml_currency, product_enabled, similar_count, web_searches, web_bytes, "
                           "our_price_cents, product_name) "
                           "VALUES (1, :p, now(), :s, :c, 1, 1, 1, 0, 13.0, 0, 'ARS', true, :sim, 0, 0, 10000, :n)"),
                      {"p": pid, "s": st, "c": col, "sim": sim, "n": f"Producto {pid}"})
        c.execute(text("INSERT INTO market_match_feedback (product_id, ml_id, created_at, category, origin, actor) "
                       "VALUES ('11', 'MLA555', now(), 'igual', 'web', 'viejo@b2box.pro')"))


def test_migration_from_the_9c6dc45_database(pg):
    _tables_as_deployed_at_9c6dc45(pg)
    session.init_db()
    session.init_db()                                              # idempotente

    insp = inspect(pg)
    cols = {t: {c["name"]: c for c in insp.get_columns(t)}
            for t in ("market_price_snapshot", "price_monitor_run", "market_match_feedback", "ml_seller_cache")}
    assert PR_SNAPSHOT_COLS <= cols["market_price_snapshot"].keys()
    assert PR_RUN_COLS <= cols["price_monitor_run"].keys()
    assert PR_FEEDBACK_COLS <= cols["market_match_feedback"].keys()
    assert {i["name"]: bool(i["unique"]) for i in insp.get_indexes("market_match_feedback")} == {"ix_mmf_product_ml": True}
    # las fechas siguen naive (el arreglo de timestamptz no se confunde con las columnas nuevas)
    for table, cs in DT_COLS:
        for col in cs:
            assert getattr(cols[table][col]["type"], "timezone", False) is False, (table, col)
    with pg.connect() as c:
        # lo que ya había no se movió
        assert [tuple(r) for r in c.execute(text(
            "SELECT product_id, ml_status, color, similar_count, our_price_cents FROM market_price_snapshot ORDER BY id"))
        ] == [("11", "ok", "verde", 0, 10000), ("12", "no_data", "sin_dato", 2, 10000), ("13", "no_data", "sin_dato", 0, 10000)]
        # lo nuevo arranca en su default, sin NULL en los contadores
        assert [tuple(r) for r in c.execute(text(
            "SELECT other_count, estimated_listing_count, other_listings, unpriced_listings, match_state, estimated_color "
            "FROM market_price_snapshot ORDER BY id"))] == [(0, 0, None, None, None, None)] * 3
        assert tuple(c.execute(text("SELECT n_est_verde, n_est_amarillo, n_est_rojo, n_solo_diferentes, n_con_similares, "
                                    "web_searches, web_bytes FROM price_monitor_run")).one()) == (0, 0, 0, 0, 1, 4, 900)
        # la marca vieja es "No es el mismo" (label 0) y conserva su actor
        assert tuple(c.execute(text("SELECT label, actor FROM market_match_feedback")).one()) == (0, "viejo@b2box.pro")
    assert match_feedback.load_excluded() == {"11": frozenset({"MLA555"})}
    assert match_feedback.load_promoted() == {}


# ─── la API sobre Postgres ────────────────────────────────────────────────


def _pub(ml_id, category, price=None, **kw):
    e = {"ml_id": ml_id, "title": f"Publicación {ml_id}", "permalink": f"https://www.mercadolibre.com.ar/p/{ml_id}",
         "origin": "web", "category": category, "source": "specs" if category == "similar" else "clip",
         "image_score": 0.9, "name_score": 0.8, "confidence": None, "reason": "x",
         "differences": [], "notes": [], "brand": None, "image_url": None, "seller": "T", "sold_quantity": 500,
         "price_cents": price, "est_ok": True}
    e.update(kw)
    return e


def _igual(ml_id, price):
    return _pub(ml_id, "igual", price, listings=1, min_cents=price, median_cents=price, prices_cents=[price],
                sellers=["T"], source="clip")


def _seed_on(engine) -> int:
    with Session(engine) as s:
        run = PriceMonitorRun(status="ok", total_products=7)
        s.add(run)
        s.commit()
        s.refresh(run)
        base = dict(run_id=run.id, our_price_cents=10_000, commission_pct=13.0, shipping_cents=0)

        def product(pid, *, igual=(), unpriced=(), similar=(), other=(), ok=False, status=None):
            snap = MarketPriceSnapshot(product_id=pid, product_name=f"P{pid}", **base,
                                       ml_status=status or ("ok" if ok else "no_data"),
                                       color="verde" if ok else "sin_dato")
            if ok:
                snap.ml_median_cents, snap.ml_min_cents, snap.ml_listing_count, snap.ml_seller_count = 25_000, 25_000, 1, 1
                snap.est_margin_pct, snap.match_origin = 117.5, "web"
            price_monitor._store_listings(snap, list(igual), list(unpriced), list(similar), list(other),
                                          keep=8, green_min=30.0, yellow_min=10.0)
            s.add(snap)

        product("1", igual=[_igual("MLA101", 25_000)], ok=True)
        product("2", similar=[_pub("MLA221", "similar", 25_000), _pub("MLA222", "similar", 30_000)])
        product("3", similar=[_pub("MLA331", "similar", 9_000)])
        product("4", other=[_pub("MLA441", "diferente", 99_000), _pub("MLA442", "diferente", 12_000)])
        product("5")
        product("6", unpriced=[_pub("MLA661", "igual", None, listings=0)])
        product("7", similar=[_pub("MLA771", "similar", 20_000)], status="failed")
        s.commit()
        return run.id


@pytest.fixture
def api(pg, monkeypatch):
    session.init_db()
    from tests.test_price_monitor_routes import _use_settings

    _use_settings(monkeypatch)
    c = TestClient(main_mod.app)
    c.cookies.set(auth.COOKIE_NAME, auth.issue_session_token("pao@b2box.pro"))
    return c


def _ids(r):
    return sorted(i["product"]["id"] for i in r.json()["items"])


def test_filters_counts_and_ordering_on_postgres(api, pg):
    _seed_on(pg)
    get = lambda **p: api.get("/api/price-monitor/snapshots", params=p)
    body = get().json()
    assert body["colors"] == {"verde": 1, "amarillo": 0, "rojo": 0, "sin_dato": 6}
    assert body["estimated_colors"] == {"verde": 1, "amarillo": 0, "rojo": 1}      # 2 verde (27.500) y 3 rojo (9.000 < 10.000)
    # igual 1+6 | solo_similar 2,3 + el failed 7 | solo_diferente 4 | ninguno 5
    assert body["states"] == {"igual": 2, "solo_similar": 3, "solo_diferente": 1, "ninguno": 1}
    assert _ids(get(match="igual")) == ["1", "6"]
    assert _ids(get(match="similar")) == ["2", "3", "7"]
    assert _ids(get(match="solo_similar")) == ["2", "3", "7"]
    assert _ids(get(match="diferente")) == ["4"]
    assert _ids(get(match="solo_diferente")) == ["4"]
    assert _ids(get(match="ninguno")) == ["5"]
    assert _ids(get(estimated="any")) == ["2", "3"]
    assert _ids(get(estimated="rojo")) == ["3"]
    assert _ids(get(estimated="verde", match="solo_similar")) == ["2"]
    assert _ids(get(color="verde")) == ["1"]
    # primero lo que tiene color real (peor margen primero), después lo estimado, después el resto
    order = [i["product"]["id"] for i in get(page_size=50).json()["items"]]
    assert order[0] == "1" and order.index("3") < order.index("2")                    # el estimado rojo antes que el verde


def test_it_is_the_same_not_the_same_and_undo_on_postgres(api, pg):
    run_id = _seed_on(pg)
    sid = lambda pid: next(i["id"] for i in api.get("/api/price-monitor/snapshots", params={"q": f"P{pid}"}).json()["items"]
                           if i["product"]["id"] == pid)
    post = lambda pid, ml, path: api.post(f"/api/price-monitor/snapshots/{sid(pid)}/{path}", json={"ml_id": ml})

    r = post("2", "MLA221", "same")
    assert r.status_code == 200 and r.json()["already"] is False
    snap = r.json()["snapshot"]
    assert (snap["ml_status"], snap["match_state"], snap["ml_median_cents"], snap["estimated_color"]) == ("ok", "igual", 25_000, None)
    with Session(pg) as s:
        run = s.get(PriceMonitorRun, run_id)
        assert (run.n_verde, run.n_est_verde, run.n_est_rojo, run.n_ok) == (2, 0, 1, 2)
        assert [(f.product_id, f.ml_id, f.label, f.actor) for f in s.exec(select(MarketMatchFeedback))] == [
            ("2", "MLA221", 1, "pao@b2box.pro")]
    assert post("2", "MLA221", "same").json()["already"] is True                       # idempotente
    # cambia de opinión: una sola fila que da vuelta (UPDATE de label sobre el índice único)
    flipped = post("2", "MLA221", "not-same").json()["snapshot"]
    assert (flipped["ml_status"], flipped["match_state"], [o["ml_id"] for o in flipped["other_listings"]]) == (
        "no_data", "similar", ["MLA221"])
    with Session(pg) as s:
        [fb] = s.exec(select(MarketMatchFeedback)).all()
        assert fb.label == 0
        run = s.get(PriceMonitorRun, run_id)
        assert (run.n_verde, run.n_est_verde, run.n_est_rojo, run.n_ok) == (1, 1, 1, 1)   # contadores de vuelta
    assert api.delete("/api/price-monitor/products/2/feedback/MLA221").json() == {"removed": True}
    assert api.delete("/api/price-monitor/products/2/feedback/MLA221").status_code == 404
    assert match_feedback.load_excluded() == {} and match_feedback.load_promoted() == {}
    # "Es el mismo" sobre un diferente y "No es el mismo" sobre el último idéntico
    assert post("4", "MLA442", "same").json()["snapshot"]["ml_median_cents"] == 12_000
    last = post("1", "MLA101", "not-same").json()["snapshot"]
    assert (last["ml_status"], last["color"], last["match_state"]) == ("no_data", "sin_dato", "diferente")
    with Session(pg) as s:
        run = s.get(PriceMonitorRun, run_id)
        assert (run.n_ok, run.n_no_data, run.n_failed) == (1, 5, 1) and run.n_verde + run.n_amarillo + run.n_rojo == 1
        assert all(f.created_at.tzinfo is None for f in s.exec(select(MarketMatchFeedback)))
