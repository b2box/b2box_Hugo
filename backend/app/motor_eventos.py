"""Avisos de cambio de estado al tablero del motor: un hilo por producto.

Cada producto que recorre la cadena (detectado → buscado → subido → medido → listo → publicado) lleva un `hilo`: un texto de
hasta 128 caracteres que nace en el radar y viaja con el producto. Hugo lo recibe en `POST /verify` (`hilo`), lo guarda
junto a lo que mandó el radar (`luis_intake.hilo`), lo reenvía al buscar proveedor y, cada vez que algo cambia de estado de
su lado, avisa:

    POST {MOTOR_EVENTOS_URL}        cabecera  x-radar-key: {MOTOR_EVENTOS_KEY}
    {"accion": "eventos", "eventos": [
        {"hilo": "…", "sistema": "hugo", "paso": "medido", "estado": "ok", "en": "2026-11-11T15:04:05Z",
         "costo_usd": 0.02, "ms": 61234, "ref": "<id de producto>", "motivo": "<texto corto>"}]}

Reglas:

  * todo es opcional: sin `hilo`, sin URL o sin clave no se manda nada y todo funciona como siempre;
  * `paso` es una lista cerrada (PASOS) y `estado` es ok | frenado | error; un evento que no cumple se descarta;
  * el aviso NUNCA frena ni rompe lo que lo dispara: va en segundo plano, con tope de 5 s, y si falla se anota en el log
    (solo el tipo de error) y se sigue;
  * `motivo` es un texto corto y sin datos personales ni de proveedores: códigos y frases fijas, nunca un mail ni un link.

Lo que avisa Hugo: `duplicado` y `prohibido` (frenado) al contestar `/verify`; `medido` y `listo` desde la vía rápida
(pricing/fast_ready.py); `publicado` cuando una activación queda confirmada en Vendure; `error` si no pudo mandar a buscar.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import httpx

from app.clock import utcnow
from app.config import get_settings

log = logging.getLogger(__name__)

SISTEMA = "hugo"
PASOS = frozenset({"detectado", "prohibido", "aprobado", "descartado", "duplicado", "prechequeo", "buscado", "ficha",
                   "calidad", "subido", "medido", "listo", "publicado", "error"})
ESTADOS = ("ok", "frenado", "error")
HILO_MAX = 128
MOTIVO_MAX = 200
REF_MAX = 80
TIMEOUT_S = 5.0
MAX_POR_LLAMADA = 200

# Referencias a las tareas de fondo: sin esto el loop podría recolectarlas a mitad de camino.
_TASKS: set[asyncio.Task] = set()
_avisado: set[str] = set()


def limpiar_hilo(value: Any) -> str | None:
    """El hilo tal como se guarda y se reenvía, o None si no sirve (vacío, no es texto, más de 128 caracteres, o con
    caracteres de control). Un hilo que no sirve no rompe nada: se sigue sin hilo."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > HILO_MAX or any(ord(c) < 32 or ord(c) == 127 for c in text):
        return None
    return text


def configurado() -> tuple[str, str] | None:
    """(URL, clave) si las dos están y la URL es https; si no, None (no se manda nada)."""
    s = get_settings()
    url, key = (s.motor_eventos_url or "").strip(), (s.motor_eventos_key or "").strip()
    if not url or not key:
        return None
    if not url.lower().startswith("https://"):
        if "http" not in _avisado:
            _avisado.add("http")
            log.error("eventos del motor: MOTOR_EVENTOS_URL no es https: no se avisa nada")
        return None
    return url, key


def evento(hilo: str | None, paso: str, estado: str = "ok", *, costo_usd: float | None = None, ms: int | None = None,
           ref: str | int | None = None, motivo: str | None = None, en: datetime | None = None) -> dict[str, Any] | None:
    """El evento listo para mandar, o None si no se puede atribuir (sin hilo) o no cumple la lista cerrada."""
    hilo = limpiar_hilo(hilo)
    if hilo is None or paso not in PASOS or estado not in ESTADOS:
        return None
    out: dict[str, Any] = {"hilo": hilo, "sistema": SISTEMA, "paso": paso, "estado": estado,
                           "en": (en or utcnow()).replace(microsecond=0).isoformat() + "Z"}
    if isinstance(costo_usd, (int, float)) and not isinstance(costo_usd, bool) and costo_usd >= 0:
        out["costo_usd"] = round(float(costo_usd), 6)
    if isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms >= 0:
        out["ms"] = int(ms)
    if ref is not None and str(ref).strip():
        out["ref"] = str(ref).strip()[:REF_MAX]
    if isinstance(motivo, str) and motivo.strip():
        out["motivo"] = " ".join(motivo.split())[:MOTIVO_MAX]
    return out


async def enviar(eventos: list[dict[str, Any]], http: Any = None) -> bool:
    """Manda los eventos (de a 200). True si todos los lotes se aceptaron. Nunca lanza."""
    cfg = configurado()
    eventos = [e for e in eventos if e]
    if cfg is None or not eventos:
        return False
    url, key = cfg
    propio = http is None
    ok = True
    try:
        http = http or httpx.AsyncClient(timeout=TIMEOUT_S)
        for at in range(0, len(eventos), MAX_POR_LLAMADA):
            lote = eventos[at:at + MAX_POR_LLAMADA]
            r = await asyncio.wait_for(
                http.post(url, json={"accion": "eventos", "eventos": lote}, headers={"x-radar-key": key}),
                timeout=TIMEOUT_S)
            if r.status_code >= 300:
                ok = False
                log.warning("eventos del motor: el aviso de %d eventos devolvió HTTP %s", len(lote), r.status_code)
    except Exception as exc:  # noqa: BLE001  (el aviso nunca rompe la cadena)
        # Solo el tipo: el mensaje de httpx puede arrastrar la URL.
        log.warning("eventos del motor: no se pudo avisar (%s)", type(exc).__name__)
        ok = False
    finally:
        if propio and http is not None:
            try:
                await http.aclose()
            except Exception as exc:  # noqa: BLE001
                log.debug("eventos del motor: no se pudo cerrar el cliente (%s)", type(exc).__name__)
    return ok


def avisar(*eventos: dict[str, Any] | None) -> None:
    """Manda los eventos en segundo plano y vuelve enseguida. Sin configuración, sin eventos válidos o sin un event loop
    corriendo no hace nada. Nunca lanza."""
    try:
        listos = [e for e in eventos if e]
        if not listos or configurado() is None:
            return
        task = asyncio.get_running_loop().create_task(enviar(listos))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
    except Exception as exc:  # noqa: BLE001  (sin loop, o cualquier sorpresa)
        log.debug("eventos del motor: no se pudo encolar el aviso (%s)", type(exc).__name__)


def hilo_de_producto(product_id: str) -> str | None:
    """El hilo del producto de Vendure, si llegó por el radar con uno. Nunca lanza."""
    try:
        from sqlmodel import Session, select

        from app.db.models import LuisIntake
        from app.db.session import engine

        with Session(engine) as s:
            return limpiar_hilo(s.exec(select(LuisIntake.hilo).where(
                LuisIntake.vendure_product_id == str(product_id), LuisIntake.hilo.is_not(None))  # type: ignore[union-attr]
                .order_by(LuisIntake.id.desc()).limit(1)).first())  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001
        log.debug("eventos del motor: no se pudo leer el hilo de %s (%s)", product_id, type(exc).__name__)
        return None
