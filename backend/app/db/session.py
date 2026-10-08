"""Engine + session factory + migración liviana de columnas para SQLite."""

from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

log = logging.getLogger(__name__)

_settings = get_settings()
_is_sqlite = _settings.database_url.startswith("sqlite")
_connect_args: dict = {"check_same_thread": False} if _is_sqlite else {}

# Para Postgres: pool sano sin desconexiones colgadas en hosts efímeros (Coolify).
_engine_kwargs: dict = {"echo": False, "connect_args": _connect_args}
if not _is_sqlite:
    _engine_kwargs.update(
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=300,
    )

engine = create_engine(_settings.database_url, **_engine_kwargs)


# ─── Migración liviana de columnas ──────────────────────────────


def _add_missing_columns() -> None:
    """Para cada tabla del modelo, detecta columnas faltantes y las agrega.

    Funciona tanto en SQLite como en Postgres. Si el modelo declara un default
    Python (ej. False, 0, ""), también seteamos ese valor en las filas existentes
    para que no queden con NULL (que rompe los WHERE x = False posteriores).
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    dialect = engine.dialect

    with engine.begin() as conn:
        for table_name, table in SQLModel.metadata.tables.items():
            if table_name not in existing_tables:
                continue
            existing_cols = {c["name"] for c in inspector.get_columns(table_name)}
            for col in table.columns:
                if col.name in existing_cols:
                    continue
                col_type = col.type.compile(dialect=dialect)
                ddl = f'ALTER TABLE "{table_name}" ADD COLUMN "{col.name}" {col_type}'
                log.warning("Migración: agregando columna %s.%s (%s)",
                            table_name, col.name, col_type)
                conn.execute(text(ddl))
                # Si la columna tiene default escalar, llenar filas existentes
                default_val = getattr(col.default, "arg", None)
                if default_val is not None and not callable(default_val):
                    placeholder_val = default_val
                    conn.execute(
                        text(f'UPDATE "{table_name}" SET "{col.name}" = :v WHERE "{col.name}" IS NULL'),
                        {"v": placeholder_val},
                    )
                    log.warning("Migración: backfill %s.%s = %r en filas existentes",
                                table_name, col.name, placeholder_val)


def _has_duplicates(table_name: str, cols: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(text(
            f'SELECT 1 FROM "{table_name}" GROUP BY {cols} HAVING COUNT(*) > 1 LIMIT 1'
        )).first()
    return row is not None


def _ensure_one_index(table, index, current: dict | None) -> None:
    cols = ", ".join(f'"{c.name}"' for c in index.columns)
    if index.unique and _has_duplicates(table.name, cols):
        # No se puede imponer unicidad sobre datos que ya la violan. Se deja lo
        # que haya (o un índice común, para que las consultas no hagan full
        # scan) y se avisa fuerte: hay que limpiar a mano.
        log.error("Migración: no se puede crear el índice ÚNICO %s: hay filas duplicadas en %s(%s)",
                  index.name, table.name, cols)
        if current is None:
            with engine.begin() as conn:
                conn.execute(text(f'CREATE INDEX IF NOT EXISTS "{index.name}" ON "{table.name}" ({cols})'))
        return
    kind = "UNIQUE INDEX" if index.unique else "INDEX"
    with engine.begin() as conn:
        if current is not None:
            log.warning("Migración: recreando %s como único", index.name)
            conn.execute(text(f'DROP INDEX IF EXISTS "{index.name}"'))
        else:
            log.warning("Migración: creando índice %s", index.name)
        conn.execute(text(f'CREATE {kind} IF NOT EXISTS "{index.name}" ON "{table.name}" ({cols})'))


def _ensure_indexes() -> None:
    """Crea índices declarados que create_all() no agrega a tablas ya existentes.

    create_all() solo crea índices al crear la tabla; si la tabla ya existe (prod),
    un índice nuevo no se aplica. Lo forzamos con CREATE [UNIQUE] INDEX IF NOT
    EXISTS (soportado por Postgres y SQLite), respetando `unique`: un índice
    declarado único que existe como común (lo dejaba así la versión anterior de
    esta función) se recrea único. Un índice que falla no frena el arranque.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table in SQLModel.metadata.tables.values():
        if table.name not in existing_tables:
            continue
        existing = {ix["name"]: ix for ix in inspector.get_indexes(table.name)}
        for index in table.indexes:
            current = existing.get(index.name)
            if current is not None and (not index.unique or current.get("unique")):
                continue
            try:
                _ensure_one_index(table, index, current)
            except Exception as exc:  # noqa: BLE001
                log.error("Migración: no se pudo crear el índice %s: %s", index.name, exc)


def init_db() -> None:
    """Crea tablas si no existen, y agrega columnas/índices faltantes."""
    # Asegurar que los modelos están registrados
    from app.db import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    _add_missing_columns()
    _ensure_indexes()


def get_session() -> Iterator[Session]:
    """Dependency-injection style para FastAPI."""
    with Session(engine) as session:
        yield session
