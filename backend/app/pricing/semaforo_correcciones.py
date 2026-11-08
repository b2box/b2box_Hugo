"""La persona manda: correcciones humanas al semáforo.

Con `pm_mode = 2` Hugo activa los verdes firmes y apaga los rojos firmes solo. Si una persona hace lo contrario (prende a
mano un producto que Hugo ve rojo, o apaga uno que ve verde; en Vendure o desde la bandeja con Mantener, Activar,
Despublicar o Deshacer), Hugo NO lo revierte:

  * lo anota en `semaforo_correcciones` con lo que decía Hugo, lo que hizo la persona, quién y cuándo, y los datos del
    cálculo de ese momento (mediana de ML, idénticos firmes, ganancia, tramo, categoría si la hay);
  * deja el producto fuera de las acciones automáticas `pm_correction_hold_days` días (default 60), salvo que cambie
    su precio (`held`);
  * una persona puede contar por qué (`set_motive`). Con «los de Mercado Libre no eran el mismo producto» (`match_malo`) las
    publicaciones que marque van a `market_match_feedback` como «No es el mismo»: la próxima corrida no las usa y el juez
    las recibe como ejemplo.

El aprendizaje semanal (pricing/semaforo_aprende.py) usa esta tabla para proponer cortes por segmento.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from app import runtime
from app.clock import utcnow
from app.db.models import MarketPriceSnapshot, PriceFactorSample, SemaforoCorreccion
from app.db.session import engine
from app.pricing import match_feedback, price_monitor, semaforo
from app.pricing import price_review as pr

log = logging.getLogger(__name__)

ACTIVAR = "activar"
INACTIVAR = "inactivar"
MANTENER = "mantener"
DECISION_LABEL = {ACTIVAR: "La activó", INACTIVAR: "La apagó", MANTENER: "La mantuvo activa"}
ORIGIN_VENDURE = "vendure"
ORIGIN_BANDEJA = "bandeja"

MOTIVES = {
    "precio_ok": "El precio está bien: la ganancia alcanza",
    "match_malo": "Los de Mercado Libre no eran el mismo producto",
    "estrategia": "Lo queremos así igual (estrategia, stock, cliente)",
    "otro": "Otro",
}
MATCH_MALO = "match_malo"
NOTE_MAX = 500
MAX_BAD_IDS = 30


class CorrectionError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


def hold_days() -> int:
    return max(1, int(runtime.get("pm_correction_hold_days") or 60))


def opposes(hugo_color: str | None, human_decision: str) -> bool:
    """¿La persona hizo lo contrario del semáforo? Prender o mantener un rojo; apagar un verde."""
    if hugo_color == semaforo.ROJO:
        return human_decision in (ACTIVAR, MANTENER)
    if hugo_color == semaforo.VERDE:
        return human_decision == INACTIVAR
    return False


# ─── Los datos del cálculo ────────────────────────────────────────


def figures(snap: MarketPriceSnapshot, params: pr.ReviewParams | None = None, now: datetime | None = None
            ) -> dict[str, Any]:
    """Los números del semáforo para un snapshot, como los usa el portón: idénticos firmes, su mediana, la ganancia con esa
    mediana y nuestro precio / mediana. `firm` = si el color era firme. PURA."""
    params = params or pr.ReviewParams()
    v = pr.evaluate_firm(snap, params, now or utcnow(), colors=(snap.color,), ignore_enabled=True) \
        if snap.color in (semaforo.ROJO, semaforo.VERDE) else None
    median = (v.firm_median if v is not None and v.firm_median else None) or snap.ml_median_cents
    margin = semaforo.estimated_margin_pct(median, snap.our_price_cents, snap.commission_pct or 0.0,
                                           snap.shipping_cents or 0, digits=2) if median else snap.est_margin_pct
    ratio = v.price_ratio if v is not None and v.price_ratio is not None else (
        snap.our_price_cents / median if median and snap.our_price_cents else None)
    return {
        "firm": bool(v is not None and v.firm),
        "ml_median_cents": median,
        "firm_listings": v.firm_listings if v is not None else None,
        "firm_sellers": v.firm_sellers if v is not None else None,
        "margin_pct": margin,
        "price_ratio": None if ratio is None else round(ratio, 3),
        "our_price_cents": snap.our_price_cents,
    }


def category_of(s: Session, product_id: str) -> str | None:
    """La categoría de Vendure del producto, si Hugo la vio (las muestras del factor de precio la guardan)."""
    return s.exec(select(PriceFactorSample.category).where(
        PriceFactorSample.product_id == product_id, PriceFactorSample.category.is_not(None))  # type: ignore[union-attr]
        .order_by(PriceFactorSample.captured_at.desc()).limit(1)).first()  # type: ignore[union-attr]


def latest_snapshot(s: Session, product_id: str) -> MarketPriceSnapshot | None:
    return s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.product_id == product_id)
                  .order_by(MarketPriceSnapshot.captured_at.desc(), MarketPriceSnapshot.id.desc())  # type: ignore[union-attr]
                  .limit(1)).first()


# ─── Anotar ───────────────────────────────────────────────────────


def record(snap: MarketPriceSnapshot, human_decision: str, *, actor: str, origin: str, now: datetime | None = None,
           motive: str | None = None, note: str | None = None, session: Session | None = None
           ) -> SemaforoCorreccion | None:
    """Anota una corrección si la persona hizo lo contrario del color del snapshot. Idempotente: si ya hay una vigente
    para el producto con la misma decisión y el mismo precio, devuelve esa (no duplica). None si no es una corrección
    (amarillo, sin dato, o la persona hizo lo mismo que decía Hugo)."""
    if not opposes(snap.color, human_decision):
        return None
    now = now or utcnow()

    def _do(s: Session) -> SemaforoCorreccion:
        current = s.exec(select(SemaforoCorreccion).where(
            SemaforoCorreccion.product_id == snap.product_id, SemaforoCorreccion.human_decision == human_decision,
            SemaforoCorreccion.hold_until > now)  # type: ignore[operator]
            .order_by(SemaforoCorreccion.id.desc()).limit(1)).first()  # type: ignore[union-attr]
        if current is not None and current.our_price_cents == snap.our_price_cents:
            return current
        f = figures(snap, pr.ReviewParams.from_runtime(), now)
        row = SemaforoCorreccion(
            product_id=snap.product_id[:64], product_name=(snap.product_name or "")[:200] or None, at=now,
            actor=(actor or ORIGIN_VENDURE)[:120], origin=origin[:12], hugo_color=snap.color, hugo_firm=f["firm"],
            human_decision=human_decision, snapshot_id=snap.id, run_id=snap.run_id,
            our_price_cents=snap.our_price_cents, ml_median_cents=f["ml_median_cents"],
            firm_listings=f["firm_listings"], firm_sellers=f["firm_sellers"], margin_pct=f["margin_pct"],
            price_ratio=f["price_ratio"], tier_used=(snap.tier_used or "")[:40] or None,
            category=(category_of(s, snap.product_id) or "")[:80] or None,
            motive=motive if motive in MOTIVES else None, note=(note or "")[:NOTE_MAX] or None,
            hold_until=now + timedelta(days=hold_days()),
        )
        s.add(row)
        s.flush()
        log.info("semáforo: corrección humana en %s (Hugo %s%s, persona %s, %s)", snap.product_id, snap.color,
                 " firme" if f["firm"] else "", human_decision, origin)
        return row

    if session is not None:
        return _do(session)
    with Session(engine, expire_on_commit=False) as s:
        row = _do(s)
        s.commit()
        return row


def record_for_product(product_id: str, human_decision: str, *, actor: str, origin: str, now: datetime | None = None,
                       motive: str | None = None, note: str | None = None) -> SemaforoCorreccion | None:
    """`record` con el último snapshot del producto. Nunca lanza (una corrección que no se pudo anotar no frena nada)."""
    try:
        with Session(engine, expire_on_commit=False) as s:
            snap = latest_snapshot(s, product_id)
            if snap is None:
                return None
            row = record(snap, human_decision, actor=actor, origin=origin, now=now, motive=motive, note=note, session=s)
            s.commit()
            return row
    except Exception:
        log.exception("semáforo: no se pudo anotar la corrección de %s", product_id)
        return None


# ─── ¿Hugo puede actuar solo? ─────────────────────────────────────


def held(s: Session, product_id: str, our_price_cents: int | None, now: datetime | None = None) -> bool:
    """¿Hay una corrección vigente para el producto con el MISMO precio? Entonces Hugo no lo activa ni lo apaga solo. Si
    el precio cambió, la corrección ya no frena (se volvió a calcular sobre otro precio)."""
    now = now or utcnow()
    rows = s.exec(select(SemaforoCorreccion.our_price_cents).where(
        SemaforoCorreccion.product_id == product_id, SemaforoCorreccion.hold_until > now)).all()  # type: ignore[operator]
    return any(p is None or our_price_cents is None or int(p) == int(our_price_cents) for p in rows)


# ─── Por qué (lo completa una persona) ────────────────────────────


def _listings(snap: MarketPriceSnapshot | None) -> list[dict[str, Any]]:
    if snap is None:
        return []
    return price_monitor._json_list(snap.matched_listings)


def set_motive(correction_id: int, motive: str, note: str | None, ml_ids: list[str] | None, actor: str) -> dict[str, Any]:
    """Guarda por qué la persona corrigió al semáforo. Con `match_malo`, `ml_ids` (al menos uno, de los idénticos del
    snapshot de la corrección) van a `market_match_feedback` como «No es el mismo» (motivo «Es otro producto»)."""
    if motive not in MOTIVES:
        raise CorrectionError(400, "motivo_invalido", "Elegí un motivo de la lista.")
    note = " ".join((note or "").split())[:NOTE_MAX] or None
    if motive == "otro" and not note:
        raise CorrectionError(400, "falta_texto", "Contá el motivo.")
    ids = list(dict.fromkeys(ml_ids or []))
    if motive == MATCH_MALO and not ids:
        raise CorrectionError(400, "faltan_publicaciones", "Marcá qué publicaciones de Mercado Libre no eran el mismo producto.")
    if motive != MATCH_MALO and ids:
        raise CorrectionError(400, "publicaciones_de_mas", "Las publicaciones solo van con «no eran el mismo producto».")
    if len(ids) > MAX_BAD_IDS or not all(match_feedback.valid_ml_id(i) for i in ids):
        raise CorrectionError(400, "publicacion_invalida", "Hay una publicación que no es de Mercado Libre.")
    with Session(engine, expire_on_commit=False) as s:
        row = s.get(SemaforoCorreccion, correction_id)
        if row is None:
            raise CorrectionError(404, "no_existe", "Corrección no encontrada.")
        snap = s.get(MarketPriceSnapshot, row.snapshot_id) if row.snapshot_id else None
        by_id = {str(m.get("ml_id")): m for m in _listings(snap)}
        unknown = [i for i in ids if i not in by_id]
        if unknown:
            raise CorrectionError(400, "publicacion_ajena", "Esa publicación no estaba entre los idénticos de la medición.")
        for ml_id in ids:
            match_feedback.add_feedback(product_id=row.product_id, ml_id=ml_id, entry=by_id[ml_id],
                                        snapshot_id=row.snapshot_id, product_name=row.product_name, actor=actor,
                                        label=match_feedback.LABEL_NOT_SAME, motive="otro_producto",
                                        note="Corrección del semáforo", session=s)
        row.motive, row.note = motive, note
        if ids:
            row.bad_ml_ids = json.dumps(sorted(set(ids) | set(_loads_list(row.bad_ml_ids))))
        s.add(row)
        s.commit()
        log.info("semáforo: motivo de la corrección %s = %s (%d publicaciones a match_feedback), por %s",
                 correction_id, motive, len(ids), actor[:120])
        return to_dict(row, snap)


def _loads_list(raw: str | None) -> list[str]:
    try:
        out = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [str(x) for x in out] if isinstance(out, list) else []


def to_dict(row: SemaforoCorreccion, snap: MarketPriceSnapshot | None = None, now: datetime | None = None
            ) -> dict[str, Any]:
    now = now or utcnow()
    return {
        "id": row.id, "product_id": row.product_id, "product_name": row.product_name,
        "at": row.at.isoformat() + "Z", "actor": row.actor, "origin": row.origin,
        "hugo_color": row.hugo_color, "hugo_firm": row.hugo_firm, "human_decision": row.human_decision,
        "human_decision_label": DECISION_LABEL.get(row.human_decision, row.human_decision),
        "snapshot_id": row.snapshot_id, "our_price_cents": row.our_price_cents, "ml_median_cents": row.ml_median_cents,
        "firm_listings": row.firm_listings, "firm_sellers": row.firm_sellers, "margin_pct": row.margin_pct,
        "price_ratio": row.price_ratio, "tier_used": row.tier_used, "category": row.category,
        "motive": row.motive, "motive_label": MOTIVES.get(row.motive or "", None), "note": row.note,
        "bad_ml_ids": _loads_list(row.bad_ml_ids),
        "hold_until": row.hold_until.isoformat() + "Z" if row.hold_until else None,
        "holding": bool(row.hold_until and row.hold_until > now),
        "listings": [{"ml_id": m.get("ml_id"), "title": (m.get("title") or "")[:160], "permalink": m.get("permalink"),
                      "median_cents": m.get("median_cents"), "source": m.get("source")}
                     for m in _listings(snap)][:20],
    }


def list_corrections(page: int = 0, page_size: int = 25) -> dict[str, Any]:
    from sqlmodel import func

    with Session(engine) as s:
        total = int(s.exec(select(func.count(SemaforoCorreccion.id))).one() or 0)  # type: ignore[arg-type]
        rows = s.exec(select(SemaforoCorreccion).order_by(SemaforoCorreccion.at.desc(), SemaforoCorreccion.id.desc())  # type: ignore[union-attr]
                      .offset(page * page_size).limit(page_size)).all()
        snaps = {sn.id: sn for sn in s.exec(select(MarketPriceSnapshot).where(
            MarketPriceSnapshot.id.in_([r.snapshot_id for r in rows if r.snapshot_id]))).all()}  # type: ignore[union-attr]
        now = utcnow()
        return {"items": [to_dict(r, snaps.get(r.snapshot_id), now) for r in rows], "total": total, "page": page,
                "page_size": page_size, "has_more": (page + 1) * page_size < total,
                "motives": [{"code": c, "label": lbl} for c, lbl in MOTIVES.items()]}
