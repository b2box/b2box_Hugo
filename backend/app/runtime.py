"""Settings runtime — editables desde el dashboard sin redeploy.

Lee/escribe la tabla `settings` (key-value). Si una key no existe en la DB,
devuelve el default del .env. Cache en memoria con TTL corto.

Lista canónica de claves editables: ver SETTINGS_SCHEMA abajo.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import Setting
from app.db.session import engine

log = logging.getLogger(__name__)

_TTL_SECONDS = 30.0
_cache: dict[str, Any] = {}
_cache_loaded_at: float = 0.0
_lock = threading.Lock()


@dataclass(slots=True, frozen=True)
class SettingMeta:
    key: str
    label: str
    description: str
    type: str           # "float" | "int"
    parser: Callable[[str], Any]
    default_attr: str   # nombre del campo en Settings (.env) para el default
    min: float | None = None
    max: float | None = None
    step: float | None = None
    group: str = "general"


# Esquema canónico — lista única de qué settings son runtime-editables.
SETTINGS_SCHEMA: list[SettingMeta] = [
    # Dedup
    SettingMeta(
        key="dedup_url_threshold",
        label="Threshold URL match",
        description="Score mínimo para considerar duplicado por source URL. 1.0 = match exacto requerido.",
        type="float", parser=float,
        default_attr="dedup_url_threshold",
        min=0.0, max=1.0, step=0.01, group="dedup",
    ),
    SettingMeta(
        key="dedup_image_threshold",
        label="Threshold Image hash",
        description="Score mínimo para considerar duplicado por similitud visual. Más alto = más estricto.",
        type="float", parser=float,
        default_attr="dedup_image_threshold",
        min=0.5, max=1.0, step=0.01, group="dedup",
    ),
    SettingMeta(
        key="dedup_text_threshold",
        label="Threshold Texto",
        description="Score mínimo para considerar duplicado por similitud de título+descripción.",
        type="float", parser=float,
        default_attr="dedup_text_threshold",
        min=0.5, max=1.0, step=0.01, group="dedup",
    ),
    # Pricing
    SettingMeta(
        key="price_drift_threshold",
        label="% mínimo para alertar",
        description="Variación mínima del precio fuente que dispara una alerta. Ej: 0.05 = 5%.",
        type="float", parser=float,
        default_attr="price_drift_threshold",
        min=0.0, max=1.0, step=0.01, group="pricing",
    ),
    SettingMeta(
        key="price_drift_max_auto",
        label="% crítico (revisión manual)",
        description="Variación brusca que se marca como crítica para revisión humana.",
        type="float", parser=float,
        default_attr="price_drift_max_auto",
        min=0.0, max=2.0, step=0.05, group="pricing",
    ),
    # Scheduler
    SettingMeta(
        key="audit_interval_hours",
        label="Cada cuántas horas correr la auditoría",
        description="Intervalo automático de las auditorías de duplicados y precios.",
        type="int", parser=int,
        default_attr="audit_interval_hours",
        min=1, max=168, step=1, group="scheduler",
    ),
]

_BY_KEY: dict[str, SettingMeta] = {m.key: m for m in SETTINGS_SCHEMA}


def _refresh_cache() -> None:
    global _cache, _cache_loaded_at
    settings = get_settings()
    new: dict[str, Any] = {}
    try:
        with Session(engine) as session:
            db_rows = {r.key: r.value for r in session.exec(select(Setting))}
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo leer tabla settings, uso defaults del .env: %s", exc)
        db_rows = {}

    for meta in SETTINGS_SCHEMA:
        if meta.key in db_rows:
            try:
                new[meta.key] = meta.parser(db_rows[meta.key])
                continue
            except (TypeError, ValueError) as exc:
                log.warning("Setting %s en DB inválido (%s), uso default", meta.key, exc)
        new[meta.key] = getattr(settings, meta.default_attr)

    _cache = new
    _cache_loaded_at = time.time()


def _ensure_fresh() -> None:
    if time.time() - _cache_loaded_at > _TTL_SECONDS:
        with _lock:
            if time.time() - _cache_loaded_at > _TTL_SECONDS:
                _refresh_cache()


def get(key: str) -> Any:
    """Devuelve el valor actual del setting (DB o default .env)."""
    _ensure_fresh()
    return _cache.get(key)


def get_all_with_meta() -> list[dict[str, Any]]:
    """Para el endpoint GET /api/settings — devuelve valor + metadata por setting."""
    _ensure_fresh()
    settings = get_settings()
    out = []
    for meta in SETTINGS_SCHEMA:
        out.append({
            "key": meta.key,
            "label": meta.label,
            "description": meta.description,
            "type": meta.type,
            "value": _cache.get(meta.key),
            "default": getattr(settings, meta.default_attr),
            "min": meta.min,
            "max": meta.max,
            "step": meta.step,
            "group": meta.group,
            "modified": _cache.get(meta.key) != getattr(settings, meta.default_attr),
        })
    return out


def set_value(key: str, value: Any) -> Any:
    """Persiste un setting nuevo en la DB. Devuelve el valor parseado.

    Lanza ValueError si la key no es runtime-editable o el valor es inválido.
    """
    meta = _BY_KEY.get(key)
    if meta is None:
        raise ValueError(f"'{key}' no es un setting runtime-editable")
    try:
        parsed = meta.parser(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Valor inválido para {key}: {exc}") from exc
    if meta.min is not None and parsed < meta.min:
        raise ValueError(f"{key} debe ser >= {meta.min}")
    if meta.max is not None and parsed > meta.max:
        raise ValueError(f"{key} debe ser <= {meta.max}")

    with Session(engine) as session:
        existing = session.get(Setting, key)
        if existing:
            existing.value = str(parsed)
            existing.updated_at = __import__("datetime").datetime.utcnow()
            session.add(existing)
        else:
            session.add(Setting(key=key, value=str(parsed)))
        session.commit()

    invalidate()
    return parsed


def reset_to_default(key: str) -> Any:
    """Borra el override de la DB; el setting vuelve al default del .env."""
    if key not in _BY_KEY:
        raise ValueError(f"'{key}' no existe")
    with Session(engine) as session:
        existing = session.get(Setting, key)
        if existing:
            session.delete(existing)
            session.commit()
    invalidate()
    return get(key)


def invalidate() -> None:
    """Fuerza el próximo `get()` a releer de la DB."""
    global _cache_loaded_at
    _cache_loaded_at = 0.0
