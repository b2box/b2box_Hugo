"""Leader election para que SOLO una instancia corra el scheduler.

Si Coolify escala Hugo a >1 replica, cada proceso arrancaría su propio
APScheduler → auditorías duplicadas y, peor, doble gasto de OTAPI/RapidAPI ($).

Solución: un advisory lock de Postgres (`pg_try_advisory_lock`). La primera
instancia lo toma y es el "líder" que corre el scheduler; las demás no. El lock
se mantiene mientras viva la conexión dedicada (si el líder muere, Postgres lo
libera y otra instancia lo toma en su próximo arranque/reintento).

Con SQLite (dev, 1 sola instancia) siempre somos líder.
"""

from __future__ import annotations

import logging

from app.db.session import engine

log = logging.getLogger(__name__)

# Clave arbitraria pero estable para el advisory lock de Hugo.
_LOCK_KEY = 0x48_55_47_4F  # "HUGO" en hex

# Conexión dedicada que mantiene vivo el lock mientras el proceso corra.
_lock_conn = None


def try_become_leader() -> bool:
    """Devuelve True si esta instancia debe correr el scheduler."""
    global _lock_conn
    if engine.dialect.name != "postgresql":
        # SQLite u otro: asumimos single-instance.
        return True
    try:
        conn = engine.raw_connection()
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_KEY,))
        got = bool(cur.fetchone()[0])
        cur.close()
        if got:
            _lock_conn = conn  # mantener viva la conexión = mantener el lock
            log.info("Soy el líder del scheduler (advisory lock tomado)")
        else:
            conn.close()
            log.warning(
                "Otra instancia ya es líder del scheduler — esta NO correrá jobs "
                "(evita auditorías y gasto OTAPI duplicados)"
            )
        return got
    except Exception as exc:  # noqa: BLE001
        # Fail-open a nivel funcional pero logueado: si el lock falla, preferimos
        # que el scheduler corra a que no corra nunca. En prod multi-instancia esto
        # es raro; si pasa, se ve en logs.
        log.error("No se pudo tomar el advisory lock (%s); corro el scheduler igual", exc)
        return True
