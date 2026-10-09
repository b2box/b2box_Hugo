"""QA de cierre de feat/semaforo-tiendas, criterio 6 (y 8/9 de la primera QA): Postgres 16 descartable.

Se salta sola sin `HUGO_TEST_PG_URL`:

    docker run -d --name qa2-pg16 -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=hugo_test -p 55499:5432 postgres:16-alpine
    HUGO_TEST_PG_URL=postgresql+psycopg://postgres:pw@localhost:55499/hugo_test pytest backend/tests/test_qa2_tiendas_pg_migracion.py
    docker rm -f qa2-pg16

La base de partida es el esquema de ORIGIN/MAIN (77645f9) que dejó SU `init_db()` hoy (`golden/pg_schema_origin_main_77645f9.sql`,
distinto del dump de b052e4f solo en las 5 fechas que prod tiene como timestamptz; esa variante también se prueba), con
filas cargadas por el código viejo. Cada test corre en un schema propio que se borra al final.

Qué se prueba: la migración hacia adelante conserva cada fila (columna a columna) y no cambia el instante de las fechas;
es idempotente (la firma del esquema no cambia en la 2ª y 3ª corrida); el código viejo sigue andando sobre el esquema nuevo
(rollback del deploy); el SQL de bajada deja el esquema idéntico al de origin/main y las filas viejas intactas; volver a
subir funciona; y las columnas que SQLite no hace cumplir (INTEGER, VARCHAR(n), NUL, sustitutos) no tumban una pasada.
"""

from __future__ import annotations

import json
import os
import random
import re
import uuid
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import psycopg  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import runtime  # noqa: E402
from app.db import session  # noqa: E402
from app.db.models import MarketPriceSnapshot, MarketStore, StoreCatalogItem, StoreMatch  # noqa: E402
from app.pricing import price_monitor, store_catalog, store_match  # noqa: E402
from tests import store_fixtures as fx  # noqa: E402
from tests.store_fixtures import Clock, FakeSite  # noqa: E402
from tests.test_price_monitor import _runs, world  # noqa: E402,F401
from tests.test_price_monitor_routes import _env, client  # noqa: E402,F401
from tests.test_qa_tiendas_color import CP, GD, add_item, sw  # noqa: E402,F401
from tests.store_fixtures import store_db  # noqa: E402,F401  (fixture)
from tests.test_qa_tiendas_pg import _patch_engine, _pg_dsn, _schema_url  # noqa: E402
from tests.test_semaforo_web import webw  # noqa: E402,F401
from tests.test_store_catalog import ROBOTS_TN, add_store, tn_product  # noqa: E402,F401

PG_URL = os.environ.get("HUGO_TEST_PG_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL, reason="falta HUGO_TEST_PG_URL (Postgres descartable)")
BASE_SQL = (Path(__file__).parent / "golden" / "pg_schema_origin_main_77645f9.sql").read_text()
OLD_TABLES = ("audit_log", "image_embed_cache", "image_hash_cache", "market_match_feedback", "market_price_snapshot",
              "ml_seller_cache", "price_history", "price_monitor_run", "settings")
# ml_web_result: el buscador de la oficina (feat/semaforo-variantes-y-oficina), que se apila sobre las tiendas.
NEW_TABLES = ("market_store", "store_catalog_item", "store_match", "store_match_feedback", "ml_web_result")
ADDED_COLUMNS = {("market_price_snapshot", "price_basis"), ("price_monitor_run", "source_stats"),
                 # variantes de búsqueda y buscador de la oficina
                 ("market_price_snapshot", "ml_variant"), ("market_price_snapshot", "web_via"),
                 ("price_monitor_run", "variant_stats"), ("price_monitor_run", "oficina_fresh"),
                 ("price_monitor_run", "n_oficina_ok")}
PROD_TIMESTAMPTZ = [("price_monitor_run", "started_at"), ("price_monitor_run", "finished_at"), ("market_price_snapshot", "captured_at"),
                    ("ml_seller_cache", "fetched_at"), ("market_match_feedback", "created_at")]

DOWN_SQL = """
DROP TABLE IF EXISTS store_match_feedback, store_match, store_catalog_item, market_store, ml_web_result;
ALTER TABLE market_price_snapshot DROP COLUMN IF EXISTS price_basis, DROP COLUMN IF EXISTS ml_variant, DROP COLUMN IF EXISTS web_via;
ALTER TABLE price_monitor_run DROP COLUMN IF EXISTS source_stats, DROP COLUMN IF EXISTS variant_stats,
    DROP COLUMN IF EXISTS oficina_fresh, DROP COLUMN IF EXISTS n_oficina_ok;
DELETE FROM settings WHERE key LIKE '_meta:store%' OR key LIKE 'pm_stores%' OR key = '_meta:pm_llm_calls_stores_today';
"""


def _signature(engine) -> dict:
    with engine.connect() as c:
        cols = [tuple(map(str, r)) for r in c.execute(text(
            "select table_name, column_name, data_type, character_maximum_length, is_nullable, column_default from "
            "information_schema.columns where table_schema = current_schema() order by 1, 2"))]
        idx = [tuple(r) for r in c.execute(text(
            "select tablename, indexname, indexdef from pg_indexes where schemaname = current_schema() order by 1, 2"))]
        con = [tuple(map(str, r)) for r in c.execute(text(
            "select conrelid::regclass::text, conname, pg_get_constraintdef(oid) from pg_constraint "
            "where connamespace = current_schema()::regnamespace order by 1, 2"))]
    return {"columns": cols, "indexes": idx, "constraints": con}


def _old_rows(engine) -> dict:
    """Las filas de las tablas viejas, SIN las columnas que agrega la rama, con las fechas pasadas a UTC naive."""
    out = {}
    with engine.connect() as c:
        for t in OLD_TABLES:
            cols = [r[0] for r in c.execute(text(
                "select column_name from information_schema.columns where table_schema = current_schema() and table_name = :t "
                "order by ordinal_position"), {"t": t}) if (t, r[0]) not in ADDED_COLUMNS]
            sel = ", ".join(f'"{x}"' for x in cols)
            order = ", ".join(str(i + 1) for i in range(len(cols)))          # por todas las columnas: sirve para las tablas sin `id`
            rows = c.execute(text(f'select {sel} from "{t}" order by {order}')).all()
            out[t] = [[(v.replace(tzinfo=None) if hasattr(v, "tzinfo") and v.tzinfo else v) for v in r] for r in rows]
    return json.loads(json.dumps(out, default=str))


def _load_base(schema: str, *, prod_timestamptz: bool) -> None:
    with psycopg.connect(_pg_dsn(PG_URL), autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
        c.execute(f'SET search_path TO "{schema}"')
        c.execute(BASE_SQL)
        if prod_timestamptz:
            for table, col in PROD_TIMESTAMPTZ:
                c.execute(f'ALTER TABLE "{table}" ALTER COLUMN "{col}" TYPE timestamp with time zone USING "{col}" AT TIME ZONE \'UTC\'')


def _seed_old(engine) -> None:
    """Filas como las deja el semáforo de origin/main (cuatro snapshots, listas JSON, una marca de «No es el mismo», ajustes)."""
    matched = json.dumps([{"ml_id": "MLA1", "title": "Organizador", "prices_cents": [12000, 14000], "median_cents": 13000,
                           "min_cents": 12000, "category": "igual", "source": "clip", "origin": "api", "listings": 2}])
    with engine.begin() as c:
        c.execute(text("INSERT INTO price_monitor_run (started_at, finished_at, status, mode, trigger, total_products, processed, n_ok, "
                       "n_no_data, n_failed, n_skipped, n_verde, n_amarillo, n_rojo, n_sin_dato, ml_requests_used, llm_calls, "
                       "llm_input_tokens, llm_output_tokens, llm_cost_usd, resumed_count, web_searches, web_bytes, web_blocked, n_web_ok, "
                       "n_con_similares, n_est_verde, n_est_amarillo, n_est_rojo, n_solo_diferentes) VALUES "
                       "('2026-10-08 06:00:00', '2026-10-08 06:40:00', 'ok', 0, 'cron', 4, 4, 2, 1, 1, 0, 1, 1, 0, 2, 12, 0, 0, 0, 0, 0, "
                       "0, 0, 0, 0, 0, 0, 0, 0, 0)"))
        for pid, st, col, med, ml_json in (("11", "ok", "verde", 13000, matched), ("12", "ok", "amarillo", 11500, None),
                                           ("13", "no_data", "sin_dato", None, None), ("14", "failed", "sin_dato", None, None)):
            c.execute(text("INSERT INTO market_price_snapshot (run_id, product_id, captured_at, ml_status, color, ml_median_cents, "
                           "ml_listing_count, ml_seller_count, candidates_count, ambiguous_count, commission_pct, shipping_cents, "
                           "ml_currency, product_enabled, similar_count, web_searches, web_bytes, other_count, estimated_listing_count, "
                           "our_price_cents, product_name, matched_listings, match_state) VALUES (1, :p, '2026-10-08 06:10:00', :s, :c, "
                           ":m, 1, 1, 1, 0, 13.0, 0, 'ARS', true, 0, 0, 0, 0, 0, 10000, :n, :ml, 'igual')"),
                      {"p": pid, "s": st, "c": col, "m": med, "n": f"Producto {pid}", "ml": ml_json})
        c.execute(text("INSERT INTO market_match_feedback (product_id, ml_id, created_at, label, category, origin) "
                       "VALUES ('11', 'MLA555', '2026-10-08 07:00:00', 0, 'igual', 'web')"))
        c.execute(text("INSERT INTO settings (key, value, updated_at) VALUES ('pm_green_min_pct', '35', '2026-10-08 05:00:00'), ('_meta:pm_llm_calls_today', '7', '2026-10-08 05:00:00')"))


@pytest.fixture(params=["fechas_sin_zona", "fechas_con_zona_como_prod"])
def base(request, monkeypatch):
    schema = "qa2_" + uuid.uuid4().hex[:10]
    _load_base(schema, prod_timestamptz=request.param == "fechas_con_zona_como_prod")
    engine = create_engine(_schema_url(schema), pool_pre_ping=True)
    _patch_engine(monkeypatch, engine)
    _seed_old(engine)
    runtime.invalidate()
    yield engine
    engine.dispose()
    with psycopg.connect(_pg_dsn(PG_URL), autocommit=True) as c:
        c.execute(f'DROP SCHEMA "{schema}" CASCADE')


# ─── hacia adelante ──────────────────────────────────────────────────────────


def test_the_base_is_really_the_schema_origin_main_creates(base):
    insp = inspect(base)
    assert set(insp.get_table_names()) == set(OLD_TABLES)
    assert not ({"price_basis"} & {c["name"] for c in insp.get_columns("market_price_snapshot")})
    assert not ({"source_stats"} & {c["name"] for c in insp.get_columns("price_monitor_run")})


def test_the_upgrade_keeps_every_old_row_untouched_and_is_idempotent(base):
    before = _old_rows(base)
    session.init_db()
    first = _signature(base)
    after = _old_rows(base)
    session.init_db()
    session.init_db()
    assert _signature(base) == first, "la 2ª y 3ª corrida cambiaron el esquema"
    assert _old_rows(base) == after
    assert after == before, "una fila vieja cambió con la migración (las fechas se comparan como instante UTC)"
    insp = inspect(base)
    assert set(NEW_TABLES) <= set(insp.get_table_names())
    snap_cols = {c["name"]: c for c in insp.get_columns("market_price_snapshot")}
    assert snap_cols["price_basis"]["nullable"] is True, "agregada sin NOT NULL: el código viejo puede insertar sin ella"
    with base.connect() as c:
        assert c.execute(text("select distinct price_basis from market_price_snapshot")).scalars().all() == ["ml"], "backfill a «ml»"
        assert c.execute(text("select count(*) from price_monitor_run where source_stats is not null")).scalar() == 0
    tz = [(t, c["name"]) for t in insp.get_table_names() for c in insp.get_columns(t) if getattr(c["type"], "timezone", False)]
    assert tz == [], "ninguna fecha queda con zona horaria"


def test_the_unique_indexes_of_the_new_tables_exist_and_hold(base):
    session.init_db()
    with base.begin() as c:
        c.execute(text("INSERT INTO market_store (name, base_url, platform, enabled, refresh_days, max_pages_per_day, created_at) "
                       "VALUES ('A', 'https://a.com.ar', 'tiendanube', true, 7, 1000, now())"))
        sid = c.execute(text("select id from market_store")).scalar()
        c.execute(text("INSERT INTO store_catalog_item (store_id, url, price_doubtful, first_seen_at, dead, fails, in_sitemap) "
                       "VALUES (:s, 'https://a.com.ar/productos/x/', false, now(), false, 0, true)"), {"s": sid})
    for sql in ("INSERT INTO market_store (name, base_url, platform, enabled, refresh_days, max_pages_per_day, created_at) "
                "VALUES ('A', 'https://b.com.ar', 'tiendanube', true, 7, 1000, now())",
                f"INSERT INTO store_catalog_item (store_id, url, price_doubtful, first_seen_at, dead, fails, in_sitemap) "
                f"VALUES ({sid}, 'https://a.com.ar/productos/x/', false, now(), false, 0, true)"):
        with pytest.raises(Exception, match="duplicate key|unique"):
            with base.begin() as c:
                c.execute(text(sql))


# ─── rollback ────────────────────────────────────────────────────────────────


def test_the_old_code_keeps_working_on_the_upgraded_schema_and_the_new_code_reads_what_it_wrote(base):
    session.init_db()
    with base.begin() as c:                  # lo que INSERTA el código de origin/main: sin price_basis ni source_stats
        c.execute(text("INSERT INTO price_monitor_run (started_at, status, mode, trigger, total_products, processed, n_ok, n_no_data, "
                       "n_failed, n_skipped, n_verde, n_amarillo, n_rojo, n_sin_dato, ml_requests_used, llm_calls, llm_input_tokens, "
                       "llm_output_tokens, llm_cost_usd, resumed_count, web_searches, web_bytes, web_blocked, n_web_ok, n_con_similares, "
                       "n_est_verde, n_est_amarillo, n_est_rojo, n_solo_diferentes) VALUES (now(), 'ok', 0, 'cron', 1, 1, 1, 0, 0, 0, 1, "
                       "0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)"))
        rid = c.execute(text("select max(id) from price_monitor_run")).scalar()
        c.execute(text("INSERT INTO market_price_snapshot (run_id, product_id, captured_at, ml_status, color, ml_median_cents, "
                       "ml_listing_count, ml_seller_count, candidates_count, ambiguous_count, commission_pct, shipping_cents, ml_currency, "
                       "product_enabled, similar_count, web_searches, web_bytes, other_count, estimated_listing_count, our_price_cents, "
                       "product_name, matched_listings) VALUES (:r, '99', now(), 'ok', 'amarillo', 13000, 2, 2, 2, 0, 13.0, 0, 'ARS', true, 0, 0, 0, 0, "
                       "0, 10000, 'Escrito por el código viejo', :ml)"),
                  {"r": rid, "ml": json.dumps([{"ml_id": "MLA1", "prices_cents": [12000, 14000], "median_cents": 13000, "listings": 2}])})
    with Session(base) as s:
        row = s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.product_id == "99")).one()
        assert row.price_basis is None
        assert price_monitor.snapshot_to_dict(row)["price_basis"] == "ml"
        run = s.get(_run_model(), rid)
        assert price_monitor.run_to_dict(run)["sources"] == {}
        # una corrección sobre esa fila (el botón) no la mueve: el color de ML (13 % = amarillo) es el mismo
        before = (row.color, row.est_margin_pct, row.ml_median_cents)
        store_match.reapply_color(s, row)
        assert row.price_basis == "ml" or row.price_basis is None
        assert (row.color, row.est_margin_pct or before[1], row.ml_median_cents) == (before[0], before[1] or row.est_margin_pct, before[2])
        assert row.color == "amarillo"
    session.init_db()                                      # volver a desplegar la rama: no pisa nada raro
    with base.connect() as c:
        assert c.execute(text("select color from market_price_snapshot where product_id = '99'")).scalar() == "amarillo"


def _run_model():
    from app.db.models import PriceMonitorRun
    return PriceMonitorRun


def test_the_down_sql_leaves_the_origin_main_schema_and_every_old_row_intact_and_going_up_again_works(base):
    base_sig = _signature(base)
    before = _old_rows(base)
    session.init_db()
    main_mod_seed = __import__("app.main", fromlist=["_seed_stores"])._seed_stores
    main_mod_seed()                                        # tiendas sembradas + sus contadores en settings
    with base.begin() as c:
        c.execute(text("INSERT INTO settings (key, value, updated_at) VALUES ('_meta:store_pages_today:1', '5', now()), "
                       "('_meta:pm_llm_calls_stores_today', '3', now()), ('pm_stores_affect_color', '1', now())"))
        for stmt in [s for s in DOWN_SQL.split(";") if s.strip()]:
            c.execute(text(stmt))
    after_down = _signature(base)
    naive = lambda sig: {**sig, "columns": [  # noqa: E731
        tuple("timestamp without time zone" if (r[0], r[1]) in PROD_TIMESTAMPTZ else r[2] if i == 2 else x for i, x in enumerate(r))
        for r in sig["columns"]]}
    assert naive(after_down) == naive(base_sig), "el esquema de bajada no es el de origin/main (salvo las 5 fechas ya corregidas)"
    rows = _old_rows(base)
    rows["settings"] = [r for r in rows["settings"] if not r[0].startswith(("_meta:store", "pm_stores", "_meta:stores_seeded"))]
    before["settings"] = [r for r in before["settings"]]
    assert rows == before or {k: v for k, v in rows.items() if k != "settings"} == {k: v for k, v in before.items() if k != "settings"}
    with base.connect() as c:
        left = c.execute(text("select key from settings where key like '%store%' and key not like '_meta:stores_seeded'")).scalars().all()
    assert left == []
    session.init_db()                                      # y se puede volver a subir
    assert set(NEW_TABLES) <= set(inspect(base).get_table_names())
    assert _old_rows(base) == _old_rows(base)


# ─── 8 y 9 de la primera QA: lo que SQLite no hace cumplir ───────────────────


@pytest.fixture
def pg(base):
    session.init_db()
    yield base


async def _index_pages(pg, pages: dict[str, str]):
    sid = add_store()
    site = FakeSite()
    site.add(f"{fx.CP}/robots.txt", ROBOTS_TN)
    urls = [f"{fx.CP}/productos/{slug}/" for slug in pages]
    site.add(f"{fx.CP}/sitemap.xml", fx.urlset(*urls, f"{fx.CP}/productos/normal/"))
    for slug, body in pages.items():
        site.add(f"{fx.CP}/productos/{slug}/", body)
    n_url, n_body = tn_product("normal")
    site.add(n_url, n_body)
    clock = Clock()
    clock.attach(site)
    r = await store_catalog.index_store(sid, get=site.get, sleep=site.sleep, rng=random.Random(1), monotonic=clock, delay=(0, 0))
    with Session(session.engine) as s:
        got = {x.url: x for x in s.exec(select(StoreCatalogItem)).all()}
    return r, got, n_url


def _tn(slug: str, **kw) -> str:
    url = f"{fx.CP}/productos/{slug}/"
    variants = kw.pop("variants", [fx.variant(5000)])
    return fx.tiendanube_page(url, kw.pop("name", f"Producto {slug}"), variants, **kw)


@pytest.mark.parametrize("stock", [-1, 0, 2**31 - 1, 2**31, 2**40, 10**20, 1e30, "9" * 30, "infinito"])
@pytest.mark.parametrize("n_variants", [1, 4])
async def test_any_stock_value_of_a_page_fits_the_integer_column_and_the_rest_of_the_store_is_read(pg, stock, n_variants):
    page = _tn("rara", variants=[fx.variant(5000, stock=stock, option=str(i)) for i in range(n_variants)])
    r, got, n_url = await _index_pages(pg, {"rara": page})
    assert r.status == "ok" and r.ok == 2, r.summary()
    rara = got[f"{fx.CP}/productos/rara/"]
    assert rara.title and (rara.stock is None or 0 <= rara.stock <= 10_000_000), rara.stock
    assert got[n_url].title


@pytest.mark.parametrize("extra", [0, 1, 2, 80, 600])
async def test_a_photo_url_of_any_length_never_breaks_the_pass_and_a_stored_one_fits_varchar_500(pg, extra):
    import re as _re

    head = "https://acdn-us.mitiendanube.com/stores/001/133/924/products/"
    long_og = head + "a" * (500 - len(head) - len(".webp") + extra) + ".webp"
    page = _tn("rara", og_image=long_og)
    page = _re.sub(r'"image": "[^"]*",\n', "", page)
    page = _re.sub(r'"image_url": "[^"]*"', '"image_url": ""', page)
    r, got, n_url = await _index_pages(pg, {"rara": page})
    assert r.status == "ok" and r.ok == 2, r.summary()
    img = got[f"{fx.CP}/productos/rara/"].image_url
    assert img is None or len(img) <= 500
    if extra <= 0:
        assert img == long_og, "justo 500 o menos se guarda entera"
    else:
        assert img != long_og, "más de 500 no cabe: se descarta esa foto (queda la siguiente candidata o ninguna), no la ficha"


@pytest.mark.parametrize("field,value", [
    ("name", "N\x00ombre con NUL"), ("name", "emoji 🧰🔧 " * 20), ("name", "\ud800 sustituto suelto"),
    ("sku", "SKU\x00X"), ("sku", "S" * 400), ("brand", "Marca\x00" + "M" * 200), ("brand", "🛠" * 100),
])
async def test_texts_with_nul_surrogates_emoji_or_huge_sizes_never_tumble_the_pass(pg, field, value):
    import json as _json

    url = f"{fx.CP}/productos/rara/"
    page = _tn("rara", sku="OK")
    ld = {"@context": "https://schema.org/", "@type": "Product", "@id": url, "name": "Rara " + (value if field == "name" else "x"),
          "sku": value if field == "sku" else "OK", "image": f"{fx.TN_IMG}/x-480-0.webp",
          "offers": {"@type": "Offer", "url": url, "priceCurrency": "ARS", "price": "5000"}}
    if field == "brand":
        ld["brand"] = {"@type": "Brand", "name": value}
    block = '<script type="application/ld+json">' + _json.dumps(ld, ensure_ascii=True) + "</script>"
    page = page.replace("</head>", block + "</head>", 1)
    r, got, n_url = await _index_pages(pg, {"rara": page})
    assert r.status == "ok", r.summary()
    assert got[n_url].title, "la ficha normal quedó leída pase lo que pase con la rara"
    rara = got[url]
    assert rara.last_checked_at is not None, "y la rara quedó marcada como revisada (no vuelve a ser la primera cada noche)"
    for col, limit in (("title", 300), ("sku", 80), ("brand", 80)):
        v = getattr(rara, col)
        assert v is None or ("\x00" not in v and len(v) <= limit), (col, v)


@pytest.mark.parametrize("host_len", [60, 100, 101, 150, 247])
def test_a_store_with_a_long_hostname_is_saved_or_refused_with_422_never_500(pg, client, host_len):
    label = "b" * 55
    host = (".".join([label] * 5))[: host_len - 4] + ".com"
    host = host.strip(".")
    r = client.post("/api/stores", json={"name": f"Larga {host_len}", "base_url": f"https://{host}", "platform": "tiendanube"})
    assert r.status_code in (201, 422), (host_len, r.status_code, r.text[:200])
    if r.status_code == 201:
        assert len(r.json()["base_url"]) <= 200


@pytest.mark.parametrize("payload", [
    {"name": "n" * 61}, {"name": "n" * 120}, {"house_brand": "h" * 120}, {"notes": "x" * 800},
    {"sitemap_url": "https://www.casaperfecta.com.ar/" + "s" * 300}, {"image_hosts": ",".join(["a.casaperfecta.com.ar"] * 20)},
    {"refresh_days": 2**40}, {"max_pages_per_day": 2**63}, {"refresh_days": -5}, {"max_pages_per_day": 0},
])
def test_creating_a_store_with_extreme_fields_is_a_clean_201_or_422_on_postgres(pg, client, payload):
    body = {"name": "Extrema", "base_url": "https://www.extrema.com.ar", "platform": "tiendanube", **payload}
    r = client.post("/api/stores", json=body)
    assert r.status_code in (201, 422), (r.status_code, r.text[:300])


async def test_a_run_a_label_and_an_undo_on_postgres_after_migrating_from_origin_main(pg, sw, client):
    runtime.set_value("pm_stores_affect_color", 1)
    add_item(GD, "gd-org", "Organizador de cocina", 20_000, 0.95)
    add_item(CP, "cp-org", "Organizador de cocina plegable", 22_000, 0.95, stock=0)
    await price_monitor.run_price_monitor()
    stats = json.loads(_runs()[-1].source_stats)
    assert stats["ml"]["igual"] == 1
    items = {i["product"]["id"]: i for i in client.get("/api/price-monitor/snapshots").json()["items"]}
    assert items["1"]["price_basis"] == "ml+tiendas"
    assert items["1"]["cheapest_outside"]["out_of_stock"] is False
    with Session(pg) as s:
        m = s.exec(select(StoreMatch).where(StoreMatch.title == "Organizador de cocina")).one()
    assert client.post(f"/api/price-monitor/store-matches/{m.id}/label", json={"label": "no_es"}).status_code == 200
    assert client.delete(f"/api/price-monitor/store-matches/{m.id}/label").status_code == 200
    assert {x["name"] for x in client.get("/api/price-monitor/summary").json()["stores"]} == {"Casa Perfecta", "Gadnic"}


@pytest.mark.parametrize("field", ["name", "house_brand", "notes"])
def test_a_nul_byte_in_a_text_field_of_the_store_form_is_refused_or_cleaned_not_a_500(pg, client, field):
    body = {"name": "Con NUL", "base_url": "https://www.connul.com.ar", "platform": "tiendanube"}
    body[field] = "ab\x00cd"
    r = client.post("/api/stores", json=body)
    assert r.status_code in (201, 422), r.status_code
    if r.status_code == 201:
        assert "\x00" not in json.dumps(r.json())
