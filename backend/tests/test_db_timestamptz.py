"""init_db pasa a naive las columnas de fecha que quedaron timestamptz.

El deploy del semáforo (08-oct-2026) corrió con sqlmodel 0.0.48, que crea los
datetime como `timestamp with time zone`. Con sqlmodel <0.0.45 Postgres las
devuelve aware y Hugo las compara contra utcnow() naive: el disparo manual
daba 500 y el dashboard "Invalid Date". La detección es pura y se testea con
un get_columns falso (la suite corre en SQLite).
"""

from __future__ import annotations

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlmodel import SQLModel

from app.db import models  # noqa: F401  (registra las tablas)
from app.db.session import _fix_timestamptz_columns, _timestamptz_to_fix


def _table(name):
    return SQLModel.metadata.tables[name]


def _fake_columns(tz_cols: dict[str, set[str]]):
    def get_columns(table_name):
        out = []
        for col in _table(table_name).columns:
            if isinstance(col.type, DateTime):
                tz = col.name in tz_cols.get(table_name, set())
                out.append({"name": col.name, "type": TIMESTAMP(timezone=tz)})
            else:
                out.append({"name": col.name, "type": String() if col.name != "id" else Integer()})
        return out
    return get_columns


def test_los_modelos_declaran_fechas_naive():
    # Si esto falla, sqlmodel volvió a crear timestamptz (ver el tope en
    # pyproject) y la migración dejaría de encontrar las columnas del modelo.
    col = _table("price_monitor_run").columns["started_at"]
    assert isinstance(col.type, DateTime)
    assert not col.type.timezone


def test_detecta_solo_las_columnas_timestamptz():
    tables = [_table("price_monitor_run"), _table("market_price_snapshot")]
    get_columns = _fake_columns({"price_monitor_run": {"started_at", "finished_at"}})
    fix = _timestamptz_to_fix(tables, get_columns)
    assert ("price_monitor_run", "started_at") in fix
    assert ("price_monitor_run", "finished_at") in fix
    assert all(t == "price_monitor_run" for t, _ in fix)


def test_sin_timestamptz_no_hay_nada_que_tocar():
    tables = [_table("price_monitor_run"), _table("market_price_snapshot")]
    assert _timestamptz_to_fix(tables, _fake_columns({})) == []


def test_columna_que_la_db_no_tiene_se_ignora():
    tables = [_table("price_monitor_run")]

    def get_columns(_):
        return [{"name": "otra", "type": TIMESTAMP(timezone=True)}]

    assert _timestamptz_to_fix(tables, get_columns) == []


def test_en_sqlite_no_ejecuta_nada():
    _fix_timestamptz_columns()  # la suite corre en SQLite: no debe tirar ni tocar nada
