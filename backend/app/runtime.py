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

from app.clock import utcnow
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
    SettingMeta(
        key="dedup_image_text_gate",
        label="Gate de imagen (ahorro de costo)",
        description=(
            "Solo se descargan/hashean imágenes para comparar un par cuando su "
            "similitud de texto supera este valor. Más alto = menos descargas = "
            "menos costo, pero puede perder duplicados con misma foto y título muy "
            "distinto. 0 = sin gate (compara imagen siempre)."
        ),
        type="float", parser=float,
        default_attr="dedup_image_text_gate",
        min=0.0, max=1.0, step=0.05, group="dedup",
    ),
    # Match por imagen del b2box app (/app/lookup)
    SettingMeta(
        key="embed_match_threshold",
        label="Threshold CLIP (match del app)",
        description=(
            "Coseno mínimo para decirle al app 'lo tenemos'. El índice está CENTRADO: "
            "la escala no es la del coseno crudo. Medido contra el catálogo real: "
            "0.72 → 1% de falsos positivos y 18.5% de recall; 0.65 → 5% y 27%; "
            "0.62 → 10% y 33%. Valores >0.80 son de la escala vieja (sin centrar) y "
            "dejan al app sin encontrar nada."
        ),
        type="float", parser=float,
        default_attr="embed_match_threshold",
        min=0.2, max=1.0, step=0.01, group="app",
    ),
    SettingMeta(
        key="embed_suggest_threshold",
        label="Threshold de sugerencia",
        description=(
            "Por debajo del threshold de match pero por encima de este valor, el producto "
            "no se muestra como encontrado pero viaja como 'mejor candidato' en el "
            "formulario que se abre en Cloud."
        ),
        type="float", parser=float,
        default_attr="embed_suggest_threshold",
        min=0.1, max=1.0, step=0.01, group="app",
    ),
    SettingMeta(
        key="embed_name_confirm_threshold",
        label="Threshold de nombre (confirma)",
        description=(
            "Similitud mínima entre el título de origen y el nombre del catálogo para "
            "que el nombre CONFIRME el candidato: gana aunque su foto no sea la de mayor "
            "score y rescata un match de imagen flojo. Sube para exigir más coincidencia "
            "de nombre; baja si productos que sí tenemos no se reconocen."
        ),
        type="float", parser=float,
        default_attr="embed_name_confirm_threshold",
        min=0.3, max=1.0, step=0.01, group="app",
    ),
    SettingMeta(
        key="embed_name_reject_threshold",
        label="Threshold de nombre (veta)",
        description=(
            "Si el nombre del mejor candidato por imagen queda por debajo de esto, se "
            "considera que no tiene nada que ver y se VETA el match: evita mostrar un "
            "producto totalmente distinto. Sube para vetar más agresivo; 0 = sin veto."
        ),
        type="float", parser=float,
        default_attr="embed_name_reject_threshold",
        min=0.0, max=1.0, step=0.01, group="app",
    ),
    SettingMeta(
        key="embed_name_rescue_image_floor",
        label="Piso de imagen para rescate por nombre",
        description=(
            "Coseno mínimo que igual se le exige a la foto de un candidato que el NOMBRE "
            "confirma. Nuestras fichas suelen tener láminas de marketing (varias unidades, "
            "fondo de color) y contra la foto blanca del marketplace puntúan bajo aunque "
            "sean el mismo producto. Escala centrada: el impostor mediano da ~0.38. Bajalo "
            "si productos que sí tenemos siguen sin aparecer."
        ),
        type="float", parser=float,
        default_attr="embed_name_rescue_image_floor",
        min=0.0, max=1.0, step=0.01, group="app",
    ),
    SettingMeta(
        key="vision_affirm_confidence",
        label="Confianza del rerank para afirmar",
        description=(
            "Cuando el modelo con visión elige un candidato, esta es la confianza "
            "mínima para decirle al app 'lo tenemos'. Por debajo el producto viaja "
            "como sugerencia y el cliente decide. Subilo si aparecen falsos positivos."
        ),
        type="float", parser=float,
        default_attr="vision_affirm_confidence",
        min=0.0, max=1.0, step=0.05, group="app",
    ),
    SettingMeta(
        key="vision_affirm_confidence_approximate",
        label="Confianza para afirmar con foto prestada",
        description=(
            "Cuando ML bloquea la publicación, Hugo resuelve el producto buscando su "
            "nombre en el catálogo del marketplace: las fotos que ve el modelo son de "
            "un homónimo, no las que mandó el cliente. Esta es la confianza mínima para "
            "afirmar 'lo tenemos' en ese caso. Se le pide más que al caso normal, pero "
            "un veredicto casi seguro del modelo que sí miró las fotos alcanza. Ojo: "
            "solo aplica al rerank de visión — el match por CLIP a secas nunca afirma "
            "con foto prestada."
        ),
        type="float", parser=float,
        default_attr="vision_affirm_confidence_approximate",
        min=0.0, max=1.0, step=0.05, group="app",
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
        description="Intervalo automático de las auditorías (duplicados, precios, calidad, PA, BX). 336h = 14 días.",
        type="int", parser=int,
        default_attr="audit_interval_hours",
        min=1, max=720, step=1, group="scheduler",
    ),
    SettingMeta(
        key="catalog_full_refresh_seconds",
        label="Full refresh del catálogo Vendure (segundos)",
        description=(
            "Cada cuánto Hugo baja TODO el catálogo de Vendure (caro para su base). "
            "Entre medio solo trae productos modificados. 43200 = 12 h."
        ),
        type="int", parser=int,
        default_attr="catalog_full_refresh_seconds",
        min=600, max=86400, step=600, group="scheduler",
    ),
    SettingMeta(
        key="otapi_daily_budget",
        label="Budget diario OTAPI (calls)",
        description="Máximo de llamadas a RapidAPI/OTAPI por día (UTC). Al llegar, los siguientes fetch se saltean.",
        type="int", parser=int,
        default_attr="otapi_daily_budget",
        min=0, max=5000, step=10, group="scheduler",
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
            existing.updated_at = utcnow()
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
