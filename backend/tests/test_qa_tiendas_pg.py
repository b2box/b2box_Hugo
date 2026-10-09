"""QA independiente de feat/semaforo-tiendas sobre un Postgres REAL (criterio 7 y todo lo que toca SQL).

Se salta sola sin `HUGO_TEST_PG_URL`:

    docker run --rm -d --name qa-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=hugo_test -p 55499:5432 postgres:16-alpine
    HUGO_TEST_PG_URL=postgresql+psycopg://postgres:pw@localhost:55499/hugo_test \\
        VENDURE_API_URL=https://example.invalid/admin-api pytest backend/tests/test_qa_tiendas_pg.py

A diferencia de las migraciones de otras ramas (que reconstruyen las tablas «viejas» restando columnas al modelo
actual), la base de partida es `golden/pg_schema_b052e4f.sql`: el pg_dump del esquema que dejó el `init_db()` REAL
de feat/semaforo-ml-web (b052e4f), con las fechas del semáforo en `timestamptz` como quedaron en prod el 08-oct y
filas ya cargadas. Todo corre en un schema propio que se borra al final.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import psycopg  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import main as main_mod  # noqa: E402
from app import runtime  # noqa: E402
from app.db import session  # noqa: E402
from app.db.models import (  # noqa: E402
    MarketPriceSnapshot,
    MarketStore,
    PriceMonitorRun,
    StoreCatalogItem,
    StoreMatch,
)
from app.pricing import daily_budget, price_monitor, store_catalog, store_match  # noqa: E402
from tests import store_fixtures as fx  # noqa: E402
from tests.store_fixtures import Clock, FakeSite  # noqa: E402
from tests.store_fixtures import store_db  # noqa: E402,F401  (fixture)
from tests.test_price_monitor import _runs, _snaps, world  # noqa: E402,F401
from tests.test_price_monitor_routes import _env, client  # noqa: E402,F401
from tests.test_qa_tiendas_color import CP, GD, add_item, sw, webw  # noqa: E402,F401
from tests.test_store_catalog import ROBOTS_TN, add_store, tn_product, tn_site  # noqa: E402,F401

PG_URL = os.environ.get("HUGO_TEST_PG_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL, reason="falta HUGO_TEST_PG_URL (Postgres descartable)")
BASE_SCHEMA_SQL = (Path(__file__).parent / "golden" / "pg_schema_b052e4f.sql").read_text()
BACKEND_DIR = Path(__file__).resolve().parents[1]
STORE_TABLES = ("market_store", "store_catalog_item", "store_match", "store_match_feedback")
BASE_TIMESTAMPTZ = {("price_monitor_run", "started_at"), ("price_monitor_run", "finished_at"),
                    ("market_price_snapshot", "captured_at"), ("ml_seller_cache", "fetched_at"),
                    ("market_match_feedback", "created_at")}


def _pg_dsn(url: str) -> str:
    u = make_url(url)
    return f"host={u.host} port={u.port} user={u.username} password={u.password} dbname={u.database}"


def _schema_url(schema: str) -> str:
    return make_url(PG_URL).update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)


def _patch_engine(monkeypatch, engine) -> None:
    """Apunta TODO módulo que haya importado el `engine` de la app (los de app.* y los de los tests) al de Postgres."""
    original = session.engine
    for name, mod in list(sys.modules.items()):
        if mod is not None and getattr(mod, "engine", None) is original:
            monkeypatch.setattr(mod, "engine", engine)


def _load_base_schema(schema: str) -> None:
    with psycopg.connect(_pg_dsn(PG_URL), autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
        c.execute(f'SET search_path TO "{schema}"')
        c.execute(BASE_SCHEMA_SQL)


def _seed_base_rows(engine) -> None:
    """Lo que ya había en prod: una corrida, tres snapshots (verde, sin dato, fallido) y una marca de «No es el mismo»."""
    with engine.begin() as c:
        c.execute(text("INSERT INTO price_monitor_run (started_at, finished_at, status, mode, trigger, total_products, processed, "
                       "n_ok, n_no_data, n_failed, n_skipped, n_verde, n_amarillo, n_rojo, n_sin_dato, ml_requests_used, llm_calls, "
                       "llm_input_tokens, llm_output_tokens, llm_cost_usd, resumed_count, web_searches, web_bytes, web_blocked, "
                       "n_web_ok, n_con_similares, n_est_verde, n_est_amarillo, n_est_rojo, n_solo_diferentes) "
                       "VALUES (now(), now(), 'ok', 0, 'cron', 3, 3, 1, 1, 1, 0, 1, 0, 0, 2, 10, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)"))
        for pid, st, col in (("11", "ok", "verde"), ("12", "no_data", "sin_dato"), ("13", "failed", "sin_dato")):
            c.execute(text("INSERT INTO market_price_snapshot (run_id, product_id, captured_at, ml_status, color, ml_listing_count, "
                           "ml_seller_count, candidates_count, ambiguous_count, commission_pct, shipping_cents, ml_currency, "
                           "product_enabled, similar_count, web_searches, web_bytes, other_count, estimated_listing_count, "
                           "our_price_cents, product_name) VALUES (1, :p, now(), :s, :c, 1, 1, 1, 0, 13.0, 0, 'ARS', true, 0, 0, 0, 0, 0, "
                           "10000, :n)"), {"p": pid, "s": st, "c": col, "n": f"Producto {pid}"})
        c.execute(text("INSERT INTO market_match_feedback (product_id, ml_id, created_at, label, category, origin) "
                       "VALUES ('11', 'MLA555', now(), 0, 'igual', 'web')"))
    with engine.begin() as c:
        for table, cols in (("price_monitor_run", ("started_at", "finished_at")), ("market_price_snapshot", ("captured_at",)),
                            ("market_match_feedback", ("created_at",))):
            for col in cols:                                             # como quedó en prod (sqlmodel 0.0.48)
                c.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN "{col}" TYPE timestamp with time zone '
                               f'USING "{col}" AT TIME ZONE \'UTC\''))


@pytest.fixture
def pg_base(monkeypatch):
    """Postgres con el esquema de b052e4f y sus filas, SIN migrar todavía."""
    schema = "qa_" + uuid.uuid4().hex[:10]
    _load_base_schema(schema)
    engine = create_engine(_schema_url(schema), pool_pre_ping=True)
    _patch_engine(monkeypatch, engine)
    _seed_base_rows(engine)
    runtime.invalidate()
    yield engine
    engine.dispose()
    with psycopg.connect(_pg_dsn(PG_URL), autocommit=True) as c:
        c.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def pg(pg_base):
    """El mismo, ya migrado por el `init_db()` de esta rama."""
    session.init_db()
    yield pg_base


# ─── criterio 7: la migración sobre las tablas que deja feat/semaforo-ml-web ─


def test_the_base_schema_really_is_the_one_of_b052e4f(pg_base):
    insp = inspect(pg_base)
    assert set(insp.get_table_names()) == {"audit_log", "image_embed_cache", "image_hash_cache", "market_match_feedback",
                                           "market_price_snapshot", "ml_seller_cache", "price_history", "price_monitor_run", "settings"}
    assert "price_basis" not in {c["name"] for c in insp.get_columns("market_price_snapshot")}
    tz = {(t, c["name"]) for t in ("price_monitor_run", "market_price_snapshot", "ml_seller_cache", "market_match_feedback")
          for c in insp.get_columns(t) if getattr(c["type"], "timezone", False)}
    assert tz >= {("price_monitor_run", "started_at"), ("market_price_snapshot", "captured_at")}


def test_upgrade_adds_the_columns_the_tables_the_indexes_and_keeps_every_row(pg_base):
    session.init_db()
    session.init_db()                                                   # idempotente
    insp = inspect(pg_base)
    snap = {c["name"]: c for c in insp.get_columns("market_price_snapshot")}
    run = {c["name"]: c for c in insp.get_columns("price_monitor_run")}
    assert "price_basis" in snap and "source_stats" in run
    assert set(STORE_TABLES) <= set(insp.get_table_names())
    uniq = {t: {i["name"] for i in insp.get_indexes(t) if i["unique"]} for t in STORE_TABLES}
    assert "ix_sci_store_url" in uniq["store_catalog_item"] and "ix_sm_run_product_item" in uniq["store_match"]
    assert "ix_smf_product_store_item" in uniq["store_match_feedback"] and uniq["market_store"]
    with pg_base.connect() as c:
        assert [tuple(r) for r in c.execute(text(
            "SELECT product_id, ml_status, color, price_basis FROM market_price_snapshot ORDER BY id"))] == [
            ("11", "ok", "verde", "ml"), ("12", "no_data", "sin_dato", "ml"), ("13", "failed", "sin_dato", "ml")]
        assert c.execute(text("SELECT source_stats FROM price_monitor_run")).scalar() is None
        assert tuple(c.execute(text("SELECT product_id, ml_id, label FROM market_match_feedback")).one()) == ("11", "MLA555", 0)
    # ninguna fecha quedó con zona horaria, ni las viejas ni las de las tablas nuevas
    tz = [(t, c["name"]) for t in insp.get_table_names() for c in insp.get_columns(t) if getattr(c["type"], "timezone", False)]
    assert tz == []


def test_the_upgraded_schema_also_works_for_the_previous_code_so_a_rollback_is_safe(pg):
    """Si hay que volver a b052e4f: sus INSERT (sin `price_basis`) y sus lecturas siguen andando."""
    with pg.begin() as c:
        c.execute(text("INSERT INTO market_price_snapshot (run_id, product_id, captured_at, ml_status, color, ml_listing_count, "
                       "ml_seller_count, candidates_count, ambiguous_count, shipping_cents, product_enabled, similar_count, "
                       "web_searches, web_bytes, other_count, estimated_listing_count) "
                       "VALUES (1, '99', now(), 'ok', 'verde', 1, 1, 1, 0, 0, true, 0, 0, 0, 0, 0)"))
        assert c.execute(text("SELECT price_basis FROM market_price_snapshot WHERE product_id='99'")).scalar() is None
    # y esta rama lee esa fila sin romperse (price_basis NULL se trata como «ml»)
    with Session(pg) as s:
        row = s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.product_id == "99")).one()
        assert price_monitor.snapshot_to_dict(row)["price_basis"] == "ml"
        store_match.reapply_color(s, row)                               # corregir una fila así no explota
        assert row.color == "verde" or row.color == "sin_dato"


def test_startup_seeds_the_two_stores_once_on_the_upgraded_database(pg):
    main_mod._seed_stores()
    main_mod._seed_stores()
    with Session(pg) as s:
        assert sorted(r.name for r in s.exec(select(MarketStore)).all()) == ["Casa Perfecta", "Gadnic"]
        s.delete(s.exec(select(MarketStore).where(MarketStore.name == "Gadnic")).one())
        s.commit()
    main_mod._seed_stores()                                             # borrada a propósito: no vuelve
    with Session(pg) as s:
        assert [r.name for r in s.exec(select(MarketStore)).all()] == ["Casa Perfecta"]


# ─── el indexador sobre Postgres ────────────────────────────────────────────


async def test_the_indexer_runs_on_postgres_new_first_cap_dead_and_gone(pg):
    import random

    sid = add_store(max_pages_per_day=5)
    site = tn_site([f"p{i}" for i in range(8)])
    clock = Clock()
    clock.attach(site)
    r = await store_catalog.index_store(sid, get=site.get, sleep=site.sleep, rng=random.Random(1), monotonic=clock, delay=(0, 0))
    assert (r.status, r.fetched, r.new_urls) == ("ok", 5, 8)
    with Session(pg) as s:
        rows = s.exec(select(StoreCatalogItem).where(StoreCatalogItem.store_id == sid).order_by(StoreCatalogItem.id)).all()
    assert [bool(x.last_seen_at) for x in rows] == [True] * 5 + [False] * 3         # las primeras 5 del sitemap
    # un segundo pasaje el mismo día no puede pasar del tope diario (contador en Postgres)
    r2 = await store_catalog.index_store(sid, get=site.get, sleep=site.sleep, rng=random.Random(1), monotonic=clock, delay=(0, 0))
    assert r2.fetched == 0 and "tope diario" in r2.message
    assert store_catalog.pages_used_today(store_catalog.get_store(sid)) == 5


def test_what_is_due_comes_in_priority_order_on_postgres_never_read_first_then_the_oldest(pg):
    """El ORDER BY (`last_checked_at IS NULL` primero, después el más viejo) con las reglas de Postgres para NULL."""
    from datetime import timedelta

    from app.clock import utcnow

    sid = add_store(refresh_days=7)
    now = utcnow()

    def row(name, checked, *, dead=False, dead_since=None):
        return StoreCatalogItem(store_id=sid, url=f"{fx.CP}/productos/{name}/", title=None if checked is None else name,
                                last_seen_at=None if checked is None else checked, last_checked_at=checked, dead=dead,
                                dead_since=dead_since, in_sitemap=True)

    with Session(pg) as s:
        s.add_all([
            row("vieja-20", now - timedelta(days=20)), row("nueva-1", None), row("reciente-3", now - timedelta(days=3)),
            row("vencida-8", now - timedelta(days=8)), row("nueva-2", None), row("muerta-vencida", now - timedelta(days=45),
                                                                                 dead=True, dead_since=now - timedelta(days=31)),
            row("muerta-fresca", now - timedelta(days=10), dead=True, dead_since=now - timedelta(days=10)),
            row("vieja-30", now - timedelta(days=30))])
        s.commit()
    due = store_catalog._due_items(store_catalog.get_store(sid), 10, now)
    names = [d.url.rsplit("/", 2)[-2] for d in due]
    assert names == ["nueva-1", "nueva-2", "muerta-vencida", "vieja-30", "vieja-20", "vencida-8"]
    assert store_catalog.count_due(store_catalog.get_store(sid), now) == 6


def test_the_daily_cap_is_atomic_across_real_processes(pg):
    """4 procesos de verdad pelean por un tope de 100 con 60 intentos cada uno: exactamente 100 ganan."""
    schema = pg.url.query["options"].replace("-csearch_path=", "")
    dsn = _schema_url(schema)
    code = (
        "import os,sys\n"
        "os.environ['VENDURE_API_URL']='https://example.invalid/admin-api'\n"
        "os.environ['DATABASE_URL']=sys.argv[1]\n"
        "from app.pricing import daily_budget\n"
        "print(sum(1 for _ in range(60) if daily_budget.reserve('_meta:store_pages_today:777', 100) is not None))\n"
    )
    procs = [subprocess.Popen([sys.executable, "-c", code, dsn], cwd=BACKEND_DIR, stdout=subprocess.PIPE, text=True)
             for _ in range(4)]
    won = [int(p.communicate(timeout=120)[0].strip()) for p in procs]
    assert all(p.returncode == 0 for p in procs)
    assert sum(won) == 100, won
    assert daily_budget.used_today("_meta:store_pages_today:777") == 100


# ─── una corrida completa con tiendas y la API, sobre Postgres ──────────────


async def test_a_full_run_with_stores_and_the_dashboard_api_on_postgres(pg, sw, client):
    runtime.set_value("pm_stores_affect_color", 1)
    add_item(GD, "gd-org", "Organizador de cocina", 20_000, 0.95)
    add_item(CP, "cp-org", "Organizador de cocina plegable", 22_000, 0.95)
    add_item(CP, "cp-cortina", "Cortina de baño", 5_000, 0.20)
    await price_monitor.run_price_monitor()
    run = _runs()[-1]
    stats = json.loads(run.source_stats)
    assert stats["ml"]["igual"] == 1 and sum(v["igual"] for k, v in stats.items() if k.startswith("store:")) == 2
    r = client.get("/api/price-monitor/snapshots").json()
    [item] = [i for i in r["items"] if i["product"]["id"] == "1"]
    assert item["price_basis"] == "ml+tiendas" and item["color"] == "verde"
    assert {c["key"] for c in item["cells"].values()} >= {"ml"} and item["cheapest_outside"]["label"] == "Mercado Libre"
    gd = _store_key(GD)
    assert [i["product"]["id"] for i in client.get(f"/api/price-monitor/snapshots?igual_in={gd}").json()["items"]] == ["1"]
    assert client.get("/api/price-monitor/snapshots?source=ml&igual_in=" + _store_key(CP)).json()["total"] == 1
    # etiquetar y deshacer contra Postgres
    with Session(pg) as s:
        m = s.exec(select(StoreMatch).where(StoreMatch.title == "Organizador de cocina")).one()
    assert client.post(f"/api/price-monitor/store-matches/{m.id}/label", json={"label": "no_es"}).status_code == 200
    assert client.post(f"/api/price-monitor/store-matches/{m.id}/label", json={"label": "no_es"}).status_code == 200  # idempotente
    assert client.delete(f"/api/price-monitor/store-matches/{m.id}/label").json()["match"]["category"] == "igual"
    s = client.get("/api/price-monitor/summary").json()
    assert {x["name"] for x in s["stores"]} == {"Casa Perfecta", "Gadnic"}


def _store_key(name: str) -> str:
    with Session(session.engine) as s:
        return f"store:{s.exec(select(MarketStore.id).where(MarketStore.name == name)).one()}"


# ─── límites de columnas que SQLite no hace cumplir ─────────────────────────


async def _index_two_pages(pg, rare_page: str):
    """Una ficha rara y una normal (la rara es la primera del sitemap): ¿se leen las dos?"""
    import random

    sid = add_store()
    url = f"{fx.CP}/productos/rara/"
    site = FakeSite()
    site.add(f"{fx.CP}/robots.txt", ROBOTS_TN)
    site.add(f"{fx.CP}/sitemap.xml", fx.urlset(url, f"{fx.CP}/productos/normal/"))
    site.add(url, rare_page)
    n_url, n_body = tn_product("normal")
    site.add(n_url, n_body)
    clock = Clock()
    clock.attach(site)
    r = await store_catalog.index_store(sid, get=site.get, sleep=site.sleep, rng=random.Random(1), monotonic=clock, delay=(0, 0))
    with Session(pg) as s:
        got = {x.url: x for x in s.exec(select(StoreCatalogItem)).all()}
    return r, got, url, n_url


async def test_a_huge_stock_in_one_page_does_not_stop_the_rest_of_the_store_on_postgres(pg):
    url = f"{fx.CP}/productos/rara/"
    page = fx.tiendanube_page(url, "Producto raro", [fx.variant(5000, stock=999_999_999, option=str(i)) for i in range(3)])
    r, got, url, n_url = await _index_two_pages(pg, page)
    assert r.status == "ok", f"la ficha rara tumbó la pasada de la tienda: {r.summary()}"
    assert got[url].title and got[n_url].title, "las dos fichas tenían que quedar leídas"


async def test_a_very_long_photo_url_in_one_page_does_not_stop_the_rest_of_the_store_on_postgres(pg):
    import re

    url = f"{fx.CP}/productos/rara/"
    long_og = "https://acdn-us.mitiendanube.com/stores/001/133/924/products/" + "a" * 600 + ".webp"
    page = fx.tiendanube_page(url, "Producto raro", [fx.variant(5000)], og_image=long_og)
    page = re.sub(r'"image": "[^"]*",\n', "", page)                       # sin foto en el JSON-LD: queda la del og:image
    page = re.sub(r'"image_url": "[^"]*"', '"image_url": ""', page)
    r, got, url, n_url = await _index_two_pages(pg, page)
    assert r.status == "ok", f"la ficha rara tumbó la pasada de la tienda: {r.summary()}"
    assert got[url].title and got[n_url].title


def test_creating_a_store_with_a_very_long_hostname_is_a_422_not_a_500(pg, client):
    host = ".".join(["b" * 60] * 4) + ".com"                                # 247 caracteres: https:// + host = 255 > 200
    assert len(f"https://{host}") > 200
    r = client.post("/api/stores", json={"name": "Larga", "base_url": f"https://{host}", "platform": "tiendanube"})
    assert r.status_code == 422, r.text


# ─── volumen: cuánto pesa una noche de tiendas ──────────────────────────────


def test_a_night_of_store_matches_weighs_what_the_readme_estimate_needs(pg):
    """1.800 productos × 2 tiendas × 6 candidatos = 21.600 filas por noche. Mide bytes por fila con datos realistas."""
    n_products, per_store = 1800, 6
    with pg.begin() as c:
        c.execute(text("INSERT INTO price_monitor_run (started_at, status, mode, trigger, total_products, processed, n_ok, n_no_data, "
                       "n_failed, n_skipped, n_verde, n_amarillo, n_rojo, n_sin_dato, ml_requests_used, llm_calls, llm_input_tokens, "
                       "llm_output_tokens, llm_cost_usd, resumed_count, web_searches, web_bytes, web_blocked, n_web_ok, n_con_similares, "
                       "n_est_verde, n_est_amarillo, n_est_rojo, n_solo_diferentes) VALUES (now(), 'ok', 0, 'cron', 1800, 1800, "
                       "0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0)"))
        run_id = c.execute(text("SELECT max(id) FROM price_monitor_run")).scalar()
        c.execute(text(
            "INSERT INTO store_match (run_id, product_id, store_id, item_id, captured_at, rank, category, auto_category, source, "
            "title, url, image_url, brand, price_cents, price_doubtful, price_note, stock, image_score, name_score, confidence, "
            "differences, reason, notes, human_label) "
            "SELECT :run, p::text, s, p * 100 + k, now(), k, CASE WHEN k = 1 THEN 'igual' WHEN k < 4 THEN 'similar' ELSE 'diferente' END, "
            "'igual', 'clip+nombre', 'Organizador de cocina plegable de tela con tapa y asas reforzadas modelo ' || k, "
            "'https://www.gadnic.com.ar/organizadores/organizador-de-cocina-plegable-de-tela-con-tapa-' || p || '-' || k, "
            "'https://static.bidcom.com.ar/publicacionesML/productos/ORG' || p || '/1000x1000-ORG' || p || '-A.jpg', 'Gadnic', "
            "1500000 + p, false, NULL, 5, 0.91, 0.83, 0.9, '[\"medida\"]', 'parecido en foto y nombre, sin confirmar', NULL, NULL "
            "FROM generate_series(1, :n) p, generate_series(1, 2) s, generate_series(1, :k) k"),
            {"run": run_id, "n": n_products, "k": per_store})
        c.execute(text("ANALYZE store_match"))
    with pg.connect() as c:
        rows = c.execute(text("SELECT count(*) FROM store_match")).scalar()
        heap = c.execute(text("SELECT pg_relation_size('store_match')")).scalar()
        total = c.execute(text("SELECT pg_total_relation_size('store_match')")).scalar()
    per_row = total / rows
    nights = 180                                                        # PRICE_MONITOR_RETENTION_DAYS por defecto
    print(f"store_match: {rows} filas/noche, {heap/1e6:.1f} MB tabla, {total/1e6:.1f} MB con índices, {per_row:.0f} B/fila; "
          f"{nights} noches = {total*nights/1e9:.2f} GB")
    assert rows == n_products * 2 * per_store
    assert per_row < 1200


def test_the_dashboard_queries_stay_fast_with_a_night_of_data_on_postgres(pg, client):
    n_products = 1800
    with pg.begin() as c:
        c.execute(text("INSERT INTO price_monitor_run (started_at, status, mode, trigger, total_products, processed, n_ok, n_no_data, "
                       "n_failed, n_skipped, n_verde, n_amarillo, n_rojo, n_sin_dato, ml_requests_used, llm_calls, llm_input_tokens, "
                       "llm_output_tokens, llm_cost_usd, resumed_count, web_searches, web_bytes, web_blocked, n_web_ok, n_con_similares, "
                       "n_est_verde, n_est_amarillo, n_est_rojo, n_solo_diferentes) VALUES (now(), 'ok', 0, 'cron', 1800, 1800, "
                       "0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0)"))
        run_id = c.execute(text("SELECT max(id) FROM price_monitor_run")).scalar()
        c.execute(text("INSERT INTO market_store (name, base_url, platform, enabled, refresh_days, max_pages_per_day, created_at) "
                       "VALUES ('Gadnic', 'https://www.gadnic.com.ar', 'jsonld_sitemap', true, 11, 2000, now()), "
                       "('Casa Perfecta', 'https://www.casaperfecta.com.ar', 'tiendanube', true, 7, 1000, now())"))
        c.execute(text(
            "INSERT INTO market_price_snapshot (run_id, product_id, captured_at, ml_status, color, ml_listing_count, ml_seller_count, "
            "candidates_count, ambiguous_count, commission_pct, shipping_cents, ml_currency, product_enabled, similar_count, web_searches, "
            "web_bytes, other_count, estimated_listing_count, our_price_cents, product_name, price_basis) "
            "SELECT :run, 'P' || p, now(), 'no_data', 'sin_dato', 0, 0, 0, 0, 13.0, 0, 'ARS', true, 0, 0, 0, 0, 0, 10000, 'Producto ' || p, 'ml' "
            "FROM generate_series(1, :n) p"), {"run": run_id, "n": n_products})
        c.execute(text(
            "INSERT INTO store_match (run_id, product_id, store_id, item_id, captured_at, rank, category, auto_category, title, url, "
            "price_cents, price_doubtful) SELECT :run, 'P' || p, s.id, p * 100 + k, now(), k, CASE WHEN k = 1 AND p % 3 = 0 THEN 'igual' "
            "WHEN k < 4 THEN 'similar' ELSE 'diferente' END, 'igual', 'Algo ' || k, 'https://www.gadnic.com.ar/x-' || p, 1500000, false "
            "FROM generate_series(1, :n) p, market_store s, generate_series(1, 6) k"), {"run": run_id, "n": n_products})
        c.execute(text("ANALYZE"))
    ids = {n: _store_key(n) for n in (GD, CP)}
    timings = {}
    for label, url in (("pagina", "/api/price-monitor/snapshots"), ("source", f"/api/price-monitor/snapshots?source={ids[GD]}"),
                       ("igual_in", f"/api/price-monitor/snapshots?igual_in={ids[GD]},{ids[CP]}"),
                       ("igual_in+color", f"/api/price-monitor/snapshots?igual_in={ids[GD]}&color=sin_dato&page=3")):
        t = time.perf_counter()
        body = client.get(url).json()
        timings[label] = round(time.perf_counter() - t, 3)
        assert "items" in body
    t = time.perf_counter()
    with Session(pg) as s:
        stats = store_match.source_stats(s, run_id)
    timings["source_stats"] = round(time.perf_counter() - t, 3)
    print("tiempos (s):", timings)
    assert stats[ids[GD]]["igual"] == n_products // 3
    assert max(timings.values()) < 2.0, timings
