"""Límites del estimado: por visitante (IP real, hash con sal) y global.

Adaptado de `cotizador/limits.py` (rama feat/cotizador-quote):

  * ráfaga por visitante, en memoria: N por minuto;
  * por visitante y por día, en base de datos (`estimate_usage`): 5 por día;
  * global por día (`estimate_daily_budget`, runtime).

El día es el de Argentina (UTC-3, sin horario de verano). Pasarse NO es un
error HTTP: el lookup sigue como siempre y el estimado vuelve `rate_limited` u
`over_budget`, sin gastar nada.

Fail-closed: si la base no contesta, el cupo no se concede.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.db.models import EstimateUsage
from app.db.session import engine

log = logging.getLogger(__name__)

AR_TZ = timezone(timedelta(hours=-3), "America/Argentina/Buenos_Aires")
GLOBAL_SCOPE = "global"
_WINDOW_S = 60.0
_BURST_MAX_KEYS = 20_000
_burst: dict[str, deque[float]] = {}

# Qué frenó la consulta (va al log y a lookup_estimate, nunca a la respuesta).
LIMIT_BURST = "burst"
LIMIT_VISITOR = "visitor"
LIMIT_GLOBAL = "global"


@dataclass(slots=True, frozen=True)
class Reservation:
    """Resultado de pedir cupo. `hit` None = concedido; `taken` = lo reservado."""
    hit: str | None
    taken: tuple[str, ...] = ()

    @property
    def granted(self) -> bool:
        return self.hit is None


def today() -> str:
    return datetime.now(timezone.utc).astimezone(AR_TZ).strftime("%Y-%m-%d")


def seconds_until_midnight(now: datetime | None = None) -> int:
    """Segundos hasta las 00:00 de Argentina (cuando se renuevan los cupos)."""
    now = (now or datetime.now(timezone.utc)).astimezone(AR_TZ)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


def visitor_scope(ip_hash: str) -> str:
    return f"ip:{ip_hash}"


def _reset_for_tests() -> None:
    _burst.clear()


def check_burst(scope: str, per_minute: int) -> bool:
    """True si el visitante puede hacer otra consulta este minuto (y la registra)."""
    if per_minute <= 0:
        return True
    now = time.time()
    cutoff = now - _WINDOW_S
    if len(_burst) > _BURST_MAX_KEYS:
        for key in [k for k, d in _burst.items() if not d or d[-1] < cutoff]:
            del _burst[key]
    window = _burst.setdefault(scope, deque())
    while window and window[0] < cutoff:
        window.popleft()
    if len(window) >= per_minute:
        return False
    window.append(now)
    return True


def reserve(scope: str, limit: int) -> bool:
    """Suma 1 al contador de `scope` hoy si no llegó a `limit`. True si hubo cupo.

    UPDATE condicional (`count < limit`): atómico en SQLite y en Postgres.
    `limit <= 0` = sin cupo (para el estimado, 0 es apagado, no "sin tope").
    Fail-closed ante un error de base.
    """
    if limit <= 0:
        return False
    day = today()
    try:
        for _ in range(4):
            with Session(engine) as s:
                bumped = s.execute(
                    update(EstimateUsage)
                    .where(EstimateUsage.scope == scope, EstimateUsage.day == day,
                           EstimateUsage.count < limit)
                    .values(count=EstimateUsage.count + 1)
                )
                if bumped.rowcount == 1:
                    s.commit()
                    return True
                row = s.get(EstimateUsage, scope)
                if row is None:
                    s.add(EstimateUsage(scope=scope, day=day, count=1))
                    try:
                        s.commit()
                        return True
                    except IntegrityError:
                        s.rollback()      # otro worker la creó primero
                        continue
                if row.day == day:
                    return False          # hoy y ya en el tope
                rolled = s.execute(
                    update(EstimateUsage)
                    .where(EstimateUsage.scope == scope, EstimateUsage.day == row.day)
                    .values(day=day, count=1)
                )
                if rolled.rowcount == 1:
                    s.commit()
                    return True
                s.rollback()
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("estimado: no se pudo reservar cupo en %s (%s) — se deniega",
                    scope.split(":")[0], type(exc).__name__)
        return False


def used_today(scope: str) -> int:
    """Lo usado hoy. Ante un error de base devuelve un número enorme: fail-closed."""
    try:
        with Session(engine) as s:
            row = s.get(EstimateUsage, scope)
            return int(row.count) if row is not None and row.day == today() else 0
    except Exception as exc:  # noqa: BLE001
        log.warning("estimado: no se pudo leer el contador (%s)", type(exc).__name__)
        return 1 << 30


def refund(scope: str) -> None:
    """Devuelve 1 al contador de hoy (no baja de 0)."""
    try:
        with Session(engine) as s:
            s.execute(
                update(EstimateUsage)
                .where(EstimateUsage.scope == scope, EstimateUsage.day == today(),
                       EstimateUsage.count > 0)
                .values(count=EstimateUsage.count - 1)
            )
            s.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("estimado: no se pudo devolver cupo (%s)", type(exc).__name__)


def prune(keep_days: int = 2) -> int:
    cutoff = (datetime.now(timezone.utc).astimezone(AR_TZ) - timedelta(days=keep_days)).strftime(
        "%Y-%m-%d")
    with Session(engine) as s:
        res = s.execute(delete(EstimateUsage).where(EstimateUsage.day < cutoff))
        s.commit()
        return int(res.rowcount or 0)


def reserve_estimate(ip_hash: str | None, *, global_limit: int, visitor_limit: int,
                     per_minute: int) -> Reservation:
    """Cupo para UN estimado. Orden: ráfaga (memoria), global (solo se mira),
    visitante (día) y al final el global. Cortar en el primero que falla evita
    que un bloqueado le gaste el cupo global a los demás.

    `ip_hash` None = consulta sin visitante (endpoint de prueba del dashboard):
    cuenta solo para el global."""
    if global_limit <= 0 or used_today(GLOBAL_SCOPE) >= global_limit:
        return Reservation(LIMIT_GLOBAL)
    taken: list[str] = []
    if ip_hash is not None:
        scope = visitor_scope(ip_hash)
        if not check_burst(scope, per_minute):
            return Reservation(LIMIT_BURST)
        if not reserve(scope, visitor_limit):
            return Reservation(LIMIT_VISITOR)
        taken.append(scope)
    if not reserve(GLOBAL_SCOPE, global_limit):
        for scope in taken:
            refund(scope)
        return Reservation(LIMIT_GLOBAL)
    taken.append(GLOBAL_SCOPE)
    return Reservation(None, tuple(taken))


async def reserve_estimate_async(ip_hash: str | None, **kw) -> Reservation:
    return await asyncio.to_thread(reserve_estimate, ip_hash, **kw)


async def refund_all_async(scopes: tuple[str, ...]) -> None:
    for scope in scopes:
        await asyncio.to_thread(refund, scope)
