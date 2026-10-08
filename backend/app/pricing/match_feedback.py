"""Corrección humana del filtro "mismo producto": el botón "No es el mismo".

Una persona mira una publicación de ML que Hugo dio por IGUAL (o SIMILAR) y dice
que no lo es. Se guarda una fila en `market_match_feedback` y pasan dos cosas:

  * ese id de ML queda excluido PARA ESE PRODUCTO en las próximas corridas
    (`load_excluded` lo lee una vez al arrancar la corrida);
  * el snapshot donde se vio se recalcula sin esa publicación
    (`price_monitor.drop_listing`).

La fila guarda lo que Hugo había dicho y con qué puntajes: son las etiquetas
negativas con las que se recalibran los umbrales (ver calibrate_market_match).
Es una tabla propia y chica; la poda de precios no la toca.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db.models import MarketMatchFeedback
from app.db.session import engine

log = logging.getLogger(__name__)

# Ítems (MLA123), productos de usuario (MLAU123) y fichas de catálogo (MLA123).
ML_ID = re.compile(r"^MLAU?\d{3,20}$")


def valid_ml_id(value: object) -> bool:
    return isinstance(value, str) and bool(ML_ID.fullmatch(value))


def load_excluded() -> dict[str, frozenset[str]]:
    """{product_id: {ml_id marcados "No es el mismo"}}. Si la tabla no se puede
    leer la corrida sigue sin exclusiones (y lo avisa): no se cae por esto."""
    try:
        with Session(engine) as s:
            rows = s.exec(select(MarketMatchFeedback.product_id, MarketMatchFeedback.ml_id)).all()
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo leer market_match_feedback: %s", exc)
        return {}
    out: dict[str, set[str]] = {}
    for product_id, ml_id in rows:
        out.setdefault(product_id, set()).add(ml_id)
    return {pid: frozenset(ids) for pid, ids in out.items()}


def add_feedback(*, product_id: str, ml_id: str, entry: dict[str, Any] | None,
                 snapshot_id: int | None, product_name: str | None) -> bool:
    """Guarda la corrección. False si ya estaba (idempotente: apretar dos veces
    no duplica). `entry` es la publicación tal como estaba en el snapshot."""
    entry = entry or {}
    row = MarketMatchFeedback(
        product_id=product_id[:64], ml_id=ml_id,
        category=(entry.get("category") or None),
        origin=(entry.get("origin") or None),
        source=(str(entry.get("source"))[:16] if entry.get("source") else None),
        image_score=entry.get("image_score"), name_score=entry.get("name_score"),
        confidence=entry.get("confidence"),
        title=(str(entry.get("title"))[:200] if entry.get("title") else None),
        permalink=(str(entry.get("permalink"))[:300] if entry.get("permalink") else None),
        snapshot_id=snapshot_id, product_name=(product_name or "")[:200] or None,
    )
    with Session(engine) as s:
        s.add(row)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            return False
    return True


def remove_feedback(product_id: str, ml_id: str) -> bool:
    """Quita la exclusión (la próxima corrida vuelve a considerar esa publicación)."""
    with Session(engine) as s:
        row = s.exec(select(MarketMatchFeedback).where(
            MarketMatchFeedback.product_id == product_id,
            MarketMatchFeedback.ml_id == ml_id)).first()
        if row is None:
            return False
        s.delete(row)
        s.commit()
    return True


def feedback_pairs() -> set[tuple[str, str]]:
    """{(product_id, ml_id)} marcados como NO iguales: etiquetas negativas para
    la calibración."""
    try:
        with Session(engine) as s:
            return {(p, m) for p, m in s.exec(
                select(MarketMatchFeedback.product_id, MarketMatchFeedback.ml_id)).all()}
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo leer market_match_feedback: %s", exc)
        return set()
