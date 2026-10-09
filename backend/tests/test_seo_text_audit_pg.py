"""HG1 sobre un Postgres 16 descartable: la migración por `init_db()` y la corrida completa.

Se salta sola sin HUGO_TEST_PG_URL:

    docker run --rm -d --name hugo-seo-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=hugo_test -p 55497:5432 postgres:16-alpine
    HUGO_TEST_PG_URL=postgresql+psycopg://postgres:pw@localhost:55497/hugo_test \\
        VENDURE_API_URL=https://example.invalid/admin-api pytest backend/tests/test_seo_text_audit_pg.py

El esquema de partida es el de origin/main (3c177c5): TODAS las tablas del modelo menos las dos nuevas (HG1 no
cambia ninguna otra tabla). Todo corre en un schema propio que se borra al final.

  * el arranque agrega EXACTAMENTE text_audit_run y text_audit_item (con su índice único) y no toca nada más;
  * es idempotente (3 arranques = mismo esquema);
  * el SQL de rollback que trae el README, copiado tal cual, deja el esquema como estaba;
  * la corrida completa, los filtros (LIKE con comodines escapados), el CSV, el borrado de corridas viejas
    y el índice único funcionan sobre Postgres.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import psycopg  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

from app.db import models, session  # noqa: E402
from app.db.models import TextAuditItem, TextAuditRun  # noqa: E402
from app.seo import text_audit  # noqa: E402
from tests.seo_fixtures import FakeVendure, raw_product  # noqa: E402
from tests.test_qa_tiendas_pg import PG_URL, _patch_engine, _pg_dsn, _schema_url  # noqa: E402
from tests.test_seo_text_audit import (  # noqa: E402,F401  (fixtures y helpers)
    client,
    env,
    items,
    make_catalog,
    rows_of,
    rules_in,
    run,
)

pytestmark = pytest.mark.skipif(not PG_URL, reason="falta HUGO_TEST_PG_URL (Postgres descartable)")
README = (Path(__file__).resolve().parents[2] / "README.md").read_text()
NEW_TABLES = {"text_audit_run", "text_audit_item"}


def _rollback_sql() -> str:
    for block in re.findall(r"```sql\n(.*?)```", README, flags=re.S):
        if "DROP TABLE IF EXISTS text_audit_item" in block:
            return block
    raise AssertionError("el README no trae el SQL de rollback de la auditoría de textos")


def _shape(engine) -> dict:
    insp = inspect(engine)
    out = {}
    for table in sorted(insp.get_table_names()):
        cols = {c["name"]: (str(c["type"]), c["nullable"], str(c.get("default"))) for c in insp.get_columns(table)}
        idx = {i["name"]: (tuple(i["column_names"]), bool(i["unique"])) for i in insp.get_indexes(table)}
        out[table] = (cols, idx)
    return out


@pytest.fixture
def old_schema(monkeypatch, env):  # noqa: F811
    """Postgres con el esquema de origin/main (sin las tablas nuevas), con el engine de la app apuntando ahí."""
    schema = "seo_" + uuid.uuid4().hex[:10]
    with psycopg.connect(_pg_dsn(PG_URL), autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(_schema_url(schema), pool_pre_ping=True)
    models  # noqa: B018  (las clases tienen que estar registradas en SQLModel.metadata)
    SQLModel.metadata.create_all(
        engine, tables=[t for t in SQLModel.metadata.sorted_tables if t.name not in NEW_TABLES],
    )
    _patch_engine(monkeypatch, engine)
    yield engine
    engine.dispose()
    with psycopg.connect(_pg_dsn(PG_URL), autocommit=True) as c:
        c.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_el_esquema_de_partida_no_tiene_las_tablas_nuevas(old_schema):
    shape = _shape(old_schema)
    assert not NEW_TABLES & set(shape)
    assert {"price_monitor_run", "market_price_snapshot", "settings", "audit_log"} <= set(shape)


def test_el_arranque_agrega_solo_las_dos_tablas_y_no_toca_nada_mas(old_schema):
    before = _shape(old_schema)
    session.init_db()
    after = _shape(old_schema)
    assert set(after) - set(before) == NEW_TABLES
    for table, shape in before.items():
        assert after[table] == shape, f"init_db cambió {table}"
    cols_item, idx_item = after["text_audit_item"]
    assert idx_item["ix_text_audit_item_run_prod_lang"] == (("run_id", "product_id", "language_code"), True)
    assert idx_item["ix_text_audit_item_run_issues"] == (("run_id", "n_issues"), False)
    assert {"run_id", "product_id", "language_code", "issues", "details", "in_ar", "in_default"} <= set(cols_item)
    cols_run, _ = after["text_audit_run"]
    assert cols_run["started_at"][0] == "TIMESTAMP"          # sin zona (no timestamptz): como el resto de Hugo


def test_el_arranque_es_idempotente(old_schema):
    session.init_db()
    first = _shape(old_schema)
    session.init_db()
    session.init_db()
    assert _shape(old_schema) == first


def test_el_rollback_del_readme_deja_el_esquema_como_estaba(old_schema):
    before = _shape(old_schema)
    session.init_db()
    with old_schema.begin() as c:
        c.execute(text("INSERT INTO settings (key, value, updated_at) VALUES ('seo:lista:marcas', '[\"X\"]', now())"))
        c.execute(text("INSERT INTO settings (key, value, updated_at) VALUES ('_meta:last_run:seo_text_audit', 'x', now())"))
        c.execute(text("INSERT INTO settings (key, value, updated_at) VALUES ('otra_cosa', '1', now())"))
    sql = _rollback_sql()
    with old_schema.begin() as c:
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            c.execute(text(stmt))
    assert _shape(old_schema) == before
    with old_schema.connect() as c:
        keys = {r[0] for r in c.execute(text("SELECT key FROM settings"))}
    assert keys == {"otra_cosa"}
    session.init_db()                                   # y volver a subir da lo mismo
    assert set(_shape(old_schema)) - set(before) == NEW_TABLES


def test_la_corrida_completa_sobre_postgres(monkeypatch, old_schema):
    session.init_db()
    result = run(monkeypatch, FakeVendure(make_catalog()))
    assert result["status"] == "ok" and result["products_total"] == 9 and result["rows_total"] == 10
    rows = rows_of(result["id"])
    assert "SIN_ES_AR" in rules_in(rows[("3", "es")])
    assert {"MAR", "ESPACIOS"} <= rules_in(rows[("9", "es_AR")])
    assert rows[("7", "es_AR")].in_ar is False and rows[("1", "es_AR")].in_ar is True


def test_filtros_csv_y_borrado_sobre_postgres(monkeypatch, old_schema, client):  # noqa: F811
    session.init_db()
    cat = {"ar": [], None: [
        raw_product(1, translations=[("es_AR", "Taza 100% algodón", "taza-100", "")]),
        raw_product(2, translations=[("es_AR", "Taza 1000 algodón", "taza-1000", "")]),
        raw_product(3, translations=[("es_AR", "Funda iPhone_ Rosa", "funda-iphone-rosa", "")]),
    ]}
    fake = FakeVendure(cat)
    result = run(monkeypatch, fake)
    rid = result["id"]
    assert [i["product_id"] for i in items(client, run_id=rid, q="100%25", only_issues="false")["items"]] == ["1"]
    assert [i["product_id"] for i in items(client, run_id=rid, q="iphone_")["items"]] == ["3"]
    assert [i["product_id"] for i in items(client, run_id=rid, rule="MAR")["items"]] == ["3"]
    assert items(client, run_id=rid, rule="SIN_DESCRIPCION")["total"] == 3
    csv_text = client.get(f"/api/seo/text-audit/export.csv?run_id={rid}&rule=MAR").content.decode("utf-8-sig")
    assert "Funda iPhone_ Rosa" in csv_text and "Taza" not in csv_text

    for _ in range(text_audit.KEEP_RUNS + 2):
        run(monkeypatch, fake)
    with Session(old_schema) as s:
        runs = s.exec(select(TextAuditRun.id)).all()
        item_runs = set(s.exec(select(TextAuditItem.run_id)).all())
    assert len(runs) == text_audit.KEEP_RUNS and item_runs <= set(runs)


def test_el_indice_unico_impide_la_misma_fila_dos_veces(old_schema):
    session.init_db()
    with Session(old_schema) as s:
        s.add(TextAuditItem(run_id=1, product_id="1", language_code="es_AR"))
        s.commit()
        s.add(TextAuditItem(run_id=1, product_id="1", language_code="es_AR"))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        s.add(TextAuditItem(run_id=1, product_id="1", language_code="es"))      # otro idioma: sí
        s.commit()
