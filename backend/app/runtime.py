"""Settings runtime — editables desde el dashboard sin redeploy.

Lee/escribe la tabla `settings` (key-value). Si una key no existe en la DB,
devuelve el default del .env. Cache en memoria con TTL corto.

Lista canónica de claves editables: ver SETTINGS_SCHEMA abajo.
"""

from __future__ import annotations

import logging
import math
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
    # Semáforo de precios contra Mercado Libre (app/pricing/price_monitor.py)
    SettingMeta(
        key="pm_mode",
        label="Modo del semáforo (0 sombra / 1 activo)",
        description=(
            "0 = sombra: calcula y guarda, no toca Vendure. 1 = activo: pasar rojos a "
            "inactivo y bandeja de revisión — TODAVÍA NO IMPLEMENTADO (PR 2); con 1 el job "
            "solo lo loguea y sigue en sombra."
        ),
        type="int", parser=int,
        default_attr="pm_mode",
        min=0, max=1, step=1, group="monitor",
    ),
    SettingMeta(
        key="pm_green_min_pct",
        label="Verde desde (% de ganancia)",
        description="Ganancia estimada mínima del revendedor para que el producto quede VERDE.",
        type="float", parser=float,
        default_attr="pm_green_min_pct",
        min=0.0, max=200.0, step=1.0, group="monitor",
    ),
    SettingMeta(
        key="pm_yellow_min_pct",
        label="Amarillo desde (% de ganancia)",
        description="Entre este valor y el verde el producto queda AMARILLO; por debajo, ROJO.",
        type="float", parser=float,
        default_attr="pm_yellow_min_pct",
        min=0.0, max=200.0, step=1.0, group="monitor",
    ),
    SettingMeta(
        key="pm_ml_commission_pct",
        label="Comisión de ML (%)",
        description=(
            "Porcentaje de la mediana de ML que se le resta como comisión de venta. "
            "Lo fija Gabriel; 13 % es la Clásica típica de MLA."
        ),
        type="float", parser=float,
        default_attr="pm_ml_commission_pct",
        min=0.0, max=40.0, step=0.5, group="monitor",
    ),
    SettingMeta(
        key="pm_ml_shipping_cents",
        label="Envío de ML (centavos ARS)",
        description=(
            "Monto fijo que se le resta a la mediana como costo de envío. 0 hasta que se "
            "mida con la sonda de /sites/MLA/listing_prices. 100000 = ARS 1.000."
        ),
        type="int", parser=int,
        default_attr="pm_ml_shipping_cents",
        min=0, max=5000000, step=10000, group="monitor",
    ),
    SettingMeta(
        key="pm_min_seller_sales",
        label="Ventas mínimas del vendedor",
        description=(
            "Publicaciones de vendedores con menos ventas concretadas que esto no cuentan "
            "para la mediana ni el mínimo. Vendedores sin dato en ML sí cuentan."
        ),
        type="int", parser=int,
        default_attr="pm_min_seller_sales",
        min=0, max=5000, step=10, group="monitor",
    ),
    SettingMeta(
        key="pm_image_threshold",
        label="Umbral de imagen (mismo producto)",
        description=(
            "Coseno CLIP mínimo (escala CENTRADA del índice, igual que el match del app) "
            "para que una ficha de ML sea el mismo producto JUNTO con el nombre. "
            "Recalibrar con app.pricing.calibrate_market_match."
        ),
        type="float", parser=float,
        default_attr="pm_image_threshold",
        min=0.2, max=1.0, step=0.01, group="monitor",
    ),
    SettingMeta(
        key="pm_name_threshold",
        label="Umbral de nombre (mismo producto)",
        description="Similitud mínima entre nuestro nombre y el título de la ficha, junto con la imagen.",
        type="float", parser=float,
        default_attr="pm_name_threshold",
        min=0.2, max=1.0, step=0.01, group="monitor",
    ),
    SettingMeta(
        key="pm_image_strong",
        label="Imagen que alcanza sola",
        description="Por encima de esto la foto decide sola (match 'clip') si el nombre no está vetado.",
        type="float", parser=float,
        default_attr="pm_image_strong",
        min=0.3, max=1.0, step=0.01, group="monitor",
    ),
    SettingMeta(
        key="pm_image_veto",
        label="Veto por imagen",
        description="Por debajo de esto la ficha se descarta sin más (ni el juez la mira).",
        type="float", parser=float,
        default_attr="pm_image_veto",
        min=0.0, max=1.0, step=0.01, group="monitor",
    ),
    SettingMeta(
        key="pm_name_veto",
        label="Veto por nombre",
        description="Por debajo de esto el título no tiene nada que ver y la ficha se descarta.",
        type="float", parser=float,
        default_attr="pm_name_veto",
        min=0.0, max=1.0, step=0.01, group="monitor",
    ),
    SettingMeta(
        key="pm_ml_daily_budget",
        label="Budget diario ML (requests)",
        description=(
            "Requests a la API de Mercado Libre por día (UTC). Si se acaba a mitad de corrida, "
            "lo que falta queda `skipped` (sin dato nuevo) y la corrida termina; la próxima "
            "empieza por los productos que hace más que no se miden, así todo rota. "
            "1.500 productos ≈ 13.500 por noche."
        ),
        type="int", parser=int,
        default_attr="pm_ml_daily_budget",
        min=0, max=60000, step=500, group="monitor",
    ),
    SettingMeta(
        key="pm_ml_concurrency",
        label="Productos en paralelo contra ML",
        description="Cuántos productos se consultan a la vez. Más = más rápido y más riesgo de 429.",
        type="int", parser=int,
        default_attr="pm_ml_concurrency",
        min=1, max=8, step=1, group="monitor",
    ),
    SettingMeta(
        key="pm_tier_policy",
        label="Tramo propio a comparar (0 mínimo / 1 más barato)",
        description=(
            "0 = tramo mínimo (el más caro por unidad, lo que paga quien compra lo justo). "
            "1 = tramo más barato (= priceWithTax, lo que Hugo leía hasta ahora)."
        ),
        type="int", parser=int,
        default_attr="pm_tier_policy",
        min=0, max=1, step=1, group="monitor",
    ),
    SettingMeta(
        key="pm_vision_max_calls",
        label="Juez LLM: llamadas por día",
        description=(
            "Tope diario de consultas al juez multimodal para la banda ambigua de "
            "'¿es el mismo producto?'. 0 = apagado (sombra sin IA). Necesita "
            "PM_LLM_BASE_URL y PM_LLM_API_KEY."
        ),
        type="int", parser=int,
        default_attr="pm_vision_max_calls",
        min=0, max=3000, step=10, group="monitor",
    ),
    SettingMeta(
        key="pm_manual_cooldown_min",
        label="Espera entre corridas manuales (min)",
        description=(
            "\"Correr ahora\" se rechaza si la última corrida arrancó hace menos que esto. "
            "Evita gastar el budget de ML apretando el botón varias veces."
        ),
        type="int", parser=int,
        default_attr="pm_manual_cooldown_min",
        min=0, max=1440, step=5, group="monitor",
    ),
    SettingMeta(
        key="pm_include_disabled",
        label="Medir también los productos deshabilitados (1 / 0)",
        description=(
            "1 = el semáforo también mide los productos deshabilitados en Vendure (se marcan "
            "como tales y se pueden filtrar). 0 = solo los habilitados. Nunca escribe en Vendure."
        ),
        type="int", parser=int,
        default_attr="pm_include_disabled",
        min=0, max=1, step=1, group="monitor",
    ),
    SettingMeta(
        key="pm_spec_check",
        label="Chequeo de medidas, cantidad y capacidad (1 / 0)",
        description=(
            "1 = una publicación igual en foto y nombre pero con otra cantidad (pack), "
            "capacidad o medidas pasa de IGUAL a SIMILAR y no cuenta para el precio."
        ),
        type="int", parser=int,
        default_attr="pm_spec_check",
        min=0, max=1, step=1, group="monitor",
    ),
    SettingMeta(
        key="pm_dim_tol_pct",
        label="Tolerancia de medidas (± % por lado)",
        description="Si las medidas de la publicación difieren de las nuestras más que esto, es SIMILAR.",
        type="float", parser=float,
        default_attr="pm_dim_tol_pct",
        min=0.0, max=100.0, step=1.0, group="monitor",
    ),
    SettingMeta(
        key="pm_weight_tol_pct",
        label="Tolerancia de peso (± %)",
        description="Si el peso de la publicación difiere del nuestro más que esto, es SIMILAR.",
        type="float", parser=float,
        default_attr="pm_weight_tol_pct",
        min=0.0, max=100.0, step=1.0, group="monitor",
    ),
    SettingMeta(
        key="pm_ml_web_daily_budget",
        label="ML web: búsquedas por día",
        description=(
            "Búsquedas en la web de Mercado Libre por día (UTC), para los productos sin ficha "
            "IGUAL en la API. 0 = fuente apagada. Necesita BROWSER_PROXY (sin proxy no se intenta). "
            "Cada búsqueda baja cientos de KB por el proxy."
        ),
        type="int", parser=int,
        default_attr="pm_ml_web_daily_budget",
        min=0, max=20000, step=100, group="monitor",
    ),
    SettingMeta(
        key="pm_ml_web_max_results",
        label="ML web: resultados por búsqueda",
        description="Cuántas publicaciones de cada búsqueda se comparan con el producto (las primeras de ML).",
        type="int", parser=int,
        default_attr="pm_ml_web_max_results",
        min=1, max=24, step=1, group="monitor",
    ),
    SettingMeta(
        key="pm_ml_web_concurrency",
        label="ML web: búsquedas en paralelo",
        description="1-2. Más paralelismo gasta el proxy más rápido y llama la atención del anti-bot.",
        type="int", parser=int,
        default_attr="pm_ml_web_concurrency",
        min=1, max=2, step=1, group="monitor",
    ),
    SettingMeta(
        key="pm_ml_web_pause_s",
        label="ML web: pausa entre búsquedas (s)",
        description="Espera (con variación al azar) después de cada búsqueda.",
        type="float", parser=float,
        default_attr="pm_ml_web_pause_s",
        min=0.0, max=120.0, step=1.0, group="monitor",
    ),
    SettingMeta(
        key="pm_ml_web_block_streak",
        label="ML web: fallos seguidos que la cortan",
        description=(
            "Bloqueos, captchas o fallos del proxy seguidos que apagan la fuente web por esa "
            "noche. Los productos sin dato por esto muestran el motivo."
        ),
        type="int", parser=int,
        default_attr="pm_ml_web_block_streak",
        min=1, max=50, step=1, group="monitor",
    ),
    SettingMeta(
        key="pm_ml_keep_listings",
        label="Publicaciones de ML que se guardan por producto",
        description=(
            "Hasta cuántas publicaciones (idénticas, similares y diferentes, las más parecidas "
            "primero) se guardan y se muestran por producto. Solo las idénticas entran al color real."
        ),
        type="int", parser=int,
        default_attr="pm_ml_keep_listings",
        min=1, max=24, step=1, group="monitor",
    ),
    SettingMeta(
        key="pm_ml_web_block_scripts",
        label="ML web: no bajar scripts ni estilos (1 / 0)",
        description=(
            "1 = el navegador baja solo el HTML de la búsqueda (~0,2 MB por el proxy en vez de "
            "~1,7 MB): el estado con los resultados viene en el HTML. Si ML empezara a bloquear "
            "por no ver los scripts, ponerlo en 0."
        ),
        type="int", parser=int,
        default_attr="pm_ml_web_block_scripts",
        min=0, max=1, step=1, group="monitor",
    ),
    SettingMeta(
        key="pm_stores_affect_color",
        label="Tiendas cuentan para el color (1 / 0)",
        description=(
            "0 = Casa Perfecta, Gadnic y las demás tiendas se muestran como referencia y el color "
            "sale solo de Mercado Libre. 1 = el color y la ganancia usan la mediana de los "
            "idénticos de Mercado Libre Y de las tiendas (un precio dudoso nunca cuenta)."
        ),
        type="int", parser=int,
        default_attr="pm_stores_affect_color",
        min=0, max=1, step=1, group="monitor",
    ),
    SettingMeta(
        key="pm_stores_topup_minutes",
        label="Tiendas: minutos para refrescar el índice al empezar",
        description=(
            "Si al arrancar una corrida el índice de una tienda está viejo, Hugo lo refresca hasta "
            "este tiempo (dentro del tope diario de páginas de la tienda) antes de comparar. "
            "0 = no se refresca ahí: solo lo hace el job de la madrugada."
        ),
        type="int", parser=int,
        default_attr="pm_stores_topup_minutes",
        min=0, max=180, step=5, group="monitor",
    ),
    SettingMeta(
        key="pm_embed_cache_days",
        label="Días de cache de fotos de ML",
        description=(
            "Los embeddings de fotos de mlstatic más viejos que esto se podan del cache "
            "(job de las 04:30 UTC). Las fotos del catálogo propio no se tocan."
        ),
        type="int", parser=int,
        default_attr="pm_embed_cache_days",
        min=7, max=365, step=1, group="monitor",
    ),
]

_BY_KEY: dict[str, SettingMeta] = {m.key: m for m in SETTINGS_SCHEMA}

# Pares (bajo, alto) que tienen que quedar ordenados. Un setting cruzado no
# rompe el código (semaforo.color lo tolera) pero deja reglas sin sentido:
# mejor rechazarlo al guardar, con un mensaje que diga qué choca con qué.
_ORDERED_PAIRS: tuple[tuple[str, str], ...] = (
    ("pm_yellow_min_pct", "pm_green_min_pct"),
    ("pm_image_veto", "pm_image_threshold"),
    ("pm_image_threshold", "pm_image_strong"),
    ("pm_name_veto", "pm_name_threshold"),
)


def _check_order(key: str, value: Any) -> None:
    for low, high in _ORDERED_PAIRS:
        if key == low:
            other = get(high)
            if other is not None and value > other:
                raise ValueError(
                    f"«{_BY_KEY[low].label}» ({value}) no puede ser mayor que "
                    f"«{_BY_KEY[high].label}» ({other}). Bajá este o subí aquel primero."
                )
        elif key == high:
            other = get(low)
            if other is not None and value < other:
                raise ValueError(
                    f"«{_BY_KEY[high].label}» ({value}) no puede ser menor que "
                    f"«{_BY_KEY[low].label}» ({other}). Subí este o bajá aquel primero."
                )


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
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Valor inválido para {key}: {exc}") from exc
    # NaN pasa los chequeos de min/max (toda comparación con NaN da False).
    if isinstance(parsed, float) and not math.isfinite(parsed):
        raise ValueError(f"{key} tiene que ser un número finito")
    if meta.min is not None and parsed < meta.min:
        raise ValueError(f"{key} debe ser >= {meta.min}")
    if meta.max is not None and parsed > meta.max:
        raise ValueError(f"{key} debe ser <= {meta.max}")
    _check_order(key, parsed)

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
    meta = _BY_KEY.get(key)
    if meta is None:
        raise ValueError(f"'{key}' no existe")
    _check_order(key, getattr(get_settings(), meta.default_attr))
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
