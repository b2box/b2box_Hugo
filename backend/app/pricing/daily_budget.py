"""Contador diario de requests con reserva atómica, fail-closed.

Nació para OTAPI (ver source_check._reserve_otapi_call y su historia) y ahora lo
comparten el budget de Mercado Libre y el tope del juez LLM del semáforo. Cada
consumidor tiene su propia fila en `settings`, con formato "YYYY-MM-DD:N": se
resetea sola al cambiar el día UTC y no deja basura.

Por qué reservar y chequear van JUNTOS: con N fetchers en paralelo, "leer el
contador" y "después incrementar" en dos pasos deja que todos vean el mismo
"todavía hay lugar" y se pasen juntos del tope. Acá es compare-and-swap sobre
la fila: se lee y se escribe condicionado a que siga valiendo lo leído; si otro
proceso la movió, se reintenta. El lock de threading ordena a los fetchers de
ESTE proceso; el CAS cubre multi-worker.

FAIL-CLOSED: si la DB no contesta, `reserve` devuelve None y el request no se
manda. Presupuesto desconocido no puede querer decir "gastá tranquilo".
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.db.models import Setting
from app.db.session import engine

log = logging.getLogger(__name__)

_lock = threading.Lock()
_CAS_ATTEMPTS = 5


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def parse_day_counter(raw: str | None, today: str) -> int:
    """"YYYY-MM-DD:N" → N si la fila es de hoy; 0 si es de otro día o está rota.

    Fail-safe hacia 0: un valor corrupto hace que se vuelva a contar desde cero,
    nunca que el budget quede bloqueado para siempre.
    """
    if not raw:
        return 0
    day, _, count = str(raw).partition(":")
    if day != today:
        return 0
    try:
        return max(0, int(count))
    except (TypeError, ValueError):
        return 0


def used_today(key: str, floor: Callable[[], int] | None = None) -> int:
    """Requests reservados hoy (UTC) bajo `key`. Si todavía no se contó nada,
    vale `floor()` (piso histórico opcional, ver source_check._snapshots_today)."""
    today = today_utc()
    try:
        with Session(engine) as s:
            row = s.get(Setting, key)
            counted = parse_day_counter(row.value if row else None, today)
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo leer el contador %s: %s", key, exc)
        return floor() if floor else 0
    if counted:
        return counted
    return floor() if floor else 0


def reserve(key: str, budget: int, floor: Callable[[], int] | None = None) -> int | None:
    """Reserva cupo para UN request contra `budget`.

    Devuelve el total del día (ya incluyendo este request) si había lugar, o
    None si el budget está agotado o no se pudo reservar (fail-closed).

    `floor` se consulta una sola vez por día, cuando el contador arranca en 0:
    sirve para sembrarlo con un piso que ya se gastó por otra vía.
    """
    today = today_utc()
    try:
        with _lock, Session(engine) as s:
            for _ in range(_CAS_ATTEMPTS):
                row = s.get(Setting, key)
                if row is not None:
                    s.refresh(row)
                previous = row.value if row is not None else None
                base = parse_day_counter(previous, today)
                if base == 0 and floor is not None:
                    base = floor()
                if base >= budget:
                    return None  # sin cupo: no se reserva ni se incrementa
                total = base + 1
                value = f"{today}:{total}"
                if row is None:
                    s.add(Setting(key=key, value=value))
                    try:
                        s.commit()
                    except IntegrityError:
                        # Otro worker creó la fila primero: se reintenta el CAS.
                        s.rollback()
                        continue
                    return total
                done = s.execute(
                    update(Setting)
                    .where(Setting.key == key, Setting.value == previous)
                    .values(value=value, updated_at=datetime.now(timezone.utc))
                )
                s.commit()
                if done.rowcount == 1:
                    return total
                # rowcount 0 = alguien más escribió entre la lectura y el UPDATE.
                s.expire_all()
            log.warning("No se pudo reservar cupo en %s tras %d intentos — no mando el request",
                        key, _CAS_ATTEMPTS)
            return None
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo reservar cupo en %s (%s) — no mando el request", key, exc)
        return None
