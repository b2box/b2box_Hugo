"""Corrección humana del filtro "mismo producto": el botón "No es el mismo".

Una persona mira una publicación de ML y corrige a Hugo en cualquiera de los dos
sentidos. Se guarda una fila en `market_match_feedback` (una por producto e id de
ML, con `label`) y pasan dos cosas:

  * "No es el mismo" (label 0, sobre un IGUAL): ese id queda como DIFERENTE para ese
    producto en las próximas corridas (`load_excluded`);
  * "Es el mismo" (label 1, sobre un SIMILAR o un DIFERENTE): queda como IGUAL para
    ese producto en las próximas corridas (`load_promoted`);
  * el snapshot donde se vio se recalcula (`price_monitor.drop_listing` y
    `promote_listing`), color real incluido.

La fila guarda lo que Hugo había dicho y con qué puntajes: son las etiquetas
negativas con las que se recalibran los umbrales (ver calibrate_market_match).
Es una tabla propia y chica; la poda de precios no la toca.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db.models import MarketMatchFeedback
from app.db.session import engine

log = logging.getLogger(__name__)

# Ítems (MLA123), productos de usuario (MLAU123) y fichas de catálogo (MLA123).
LABEL_NOT_SAME = 0
LABEL_SAME = 1

ML_ID = re.compile(r"^MLAU?\d{3,20}$", re.ASCII)


def valid_ml_id(value: object) -> bool:
    return isinstance(value, str) and bool(ML_ID.fullmatch(value))


def _load(label: int) -> dict[str, frozenset[str]]:
    cond = MarketMatchFeedback.label == label
    if label == LABEL_NOT_SAME:
        cond = or_(cond, MarketMatchFeedback.label.is_(None))  # type: ignore[union-attr]
    try:
        with Session(engine) as s:
            rows = s.exec(
                select(MarketMatchFeedback.product_id, MarketMatchFeedback.ml_id).where(cond)
            ).all()
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo leer market_match_feedback: %s", exc)
        return {}
    out: dict[str, set[str]] = {}
    for product_id, ml_id in rows:
        out.setdefault(product_id, set()).add(ml_id)
    return {pid: frozenset(ids) for pid, ids in out.items()}


def load_excluded() -> dict[str, frozenset[str]]:
    """{product_id: {ml_id marcados "No es el mismo"}}. Si la tabla no se puede
    leer la corrida sigue sin exclusiones (y lo avisa): no se cae por esto."""
    return _load(LABEL_NOT_SAME)


def load_promoted() -> dict[str, frozenset[str]]:
    """{product_id: {ml_id marcados "Es el mismo"}}: la próxima corrida los toma
    como IGUAL para ese producto."""
    return _load(LABEL_SAME)


def add_feedback(*, product_id: str, ml_id: str, entry: dict[str, Any] | None,
                 snapshot_id: int | None, product_name: str | None,
                 actor: str | None = None, label: int = LABEL_NOT_SAME,
                 session: Session | None = None) -> bool:
    """Guarda la corrección ("No es el mismo", label 0; "Es el mismo", label 1).
    `entry` es la publicación tal como estaba en el snapshot. False si ya estaba
    con ese mismo label (idempotente: apretar dos veces no duplica); si estaba con
    el otro label se da vuelta (una persona cambió de opinión) y devuelve True.

    Con `session` el alta va en la sesión del llamador y NO se commitea: así la
    corrección y el snapshot recalculado se guardan juntos o no se guarda nada.
    Sin `session` abre la suya y commitea."""
    entry = entry or {}
    row = MarketMatchFeedback(
        product_id=product_id[:64], ml_id=ml_id, label=int(label),
        category=(entry.get("category") or None),
        origin=(entry.get("origin") or None),
        source=(str(entry.get("source"))[:16] if entry.get("source") else None),
        image_score=entry.get("image_score"), name_score=entry.get("name_score"),
        confidence=entry.get("confidence"),
        title=(str(entry.get("title"))[:200] if entry.get("title") else None),
        permalink=(str(entry.get("permalink"))[:300] if entry.get("permalink") else None),
        snapshot_id=snapshot_id, product_name=(product_name or "")[:200] or None,
        actor=(actor or "")[:120] or None,
    )

    def _upsert(s: Session) -> bool:
        existing = s.exec(select(MarketMatchFeedback).where(
            MarketMatchFeedback.product_id == row.product_id,
            MarketMatchFeedback.ml_id == ml_id)).first()
        if existing is None:
            s.add(row)
            return True
        if int(existing.label or 0) == int(label):
            return False
        existing.label, existing.actor, existing.snapshot_id = int(label), row.actor, snapshot_id
        s.add(existing)
        return True

    if session is not None:
        return _upsert(session)
    with Session(engine) as s:
        added = _upsert(s)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            return False
    return added


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
    return {(p, m) for p, ids in load_excluded().items() for m in ids}


def positive_pairs() -> set[tuple[str, str]]:
    """{(product_id, ml_id)} marcados "Es el mismo": etiquetas positivas."""
    return {(p, m) for p, ids in load_promoted().items() for m in ids}
