"""Reloj UTC único para Hugo.

`datetime.utcnow()` quedó deprecado en Python 3.12+. Usamos `utcnow()` de acá,
que devuelve un datetime UTC *naive* (sin tzinfo) para mantener la misma
semántica que ya tienen las columnas de la DB (evita mezclar naive/aware en los
WHERE created_at >= x).
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """UTC actual, naive (tzinfo=None), sin usar el deprecado datetime.utcnow()."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
