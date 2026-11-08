"""El semáforo aprende de las correcciones humanas, de forma explicable y acotada.

Una vez por semana (`run_learning`, flag `pm_learn_mode`: 0 apagado, 1 propone, 2 aplica solo) Hugo mira las correcciones
de los últimos `pm_learn_window_days` días (pricing/semaforo_correcciones.py) en dos sentidos:

    rojo   una persona prendió o mantuvo un producto que Hugo veía rojo  → Hugo fue demasiado estricto apagando
    verde  una persona apagó un producto que Hugo veía verde             → Hugo fue demasiado flojo activando

y busca si se CONCENTRAN en un tipo de caso (un segmento con al menos `pm_learn_min_corrections` correcciones y la mitad o
más de las de ese sentido):

    pocos_identicos   pocos idénticos firmes (menos de `pm_review_min_listings` + 2)
    margen_borde      la ganancia quedó a 5 puntos o menos de un corte (amarillo o verde)
    match_dudoso      nuestro precio es 1,5 veces o más la mediana de ML, o la persona dijo que el match estaba mal
    precio:<tramo>    rango de nuestro precio (bajo, medio, alto, muy_alto)
    categoria:<x>     la categoría de Vendure, si Hugo la conoce

Para cada segmento así propone UN ajuste de un corte, solo para ese segmento y solo para lo que Hugo hace SOLO:

    pocos_identicos          → más idénticos firmes para actuar solo (`min_listings`, hasta +3)
    match_dudoso (rojo)      → un tope más bajo de nuestro precio / mediana para apagar solo (`max_price_ratio`)
    los demás, rojo          → corte amarillo más bajo para apagar solo (`yellow_min`, hasta −`pm_learn_max_shift_pts`)
    los demás, verde         → corte verde más alto para activar solo (`green_min`, hasta +`pm_learn_max_shift_pts`)

Un producto que cae en el segmento y no pasa el corte ajustado NO se apaga ni se activa solo: va a la bandeja de Pao. El
semáforo que ve el cliente no cambia. Cada ajuste es una fila de `semaforo_ajustes` (historial: qué cambió, por qué, con
cuántas correcciones, quién lo aprobó) y se puede deshacer; si reemplazó a otro del mismo segmento, Deshacer lo vuelve a
poner.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from app import runtime
from app.clock import utcnow
from app.db.models import SemaforoAjuste, SemaforoCorreccion
from app.db.session import engine
from app.pricing import price_review as pr
from app.pricing import semaforo_correcciones as corr

log = logging.getLogger(__name__)

ROJO, VERDE = "rojo", "verde"
PROPUESTO, APLICADO, RECHAZADO, DESHECHO, REEMPLAZADO = "propuesto", "aplicado", "rechazado", "deshecho", "reemplazado"
STATUS_LABEL = {PROPUESTO: "Propuesto", APLICADO: "Aplicado", RECHAZADO: "Rechazado", DESHECHO: "Deshecho",
                REEMPLAZADO: "Reemplazado"}
P_YELLOW, P_GREEN, P_LISTINGS, P_RATIO = "yellow_min", "green_min", "min_listings", "max_price_ratio"
PARAM_LABEL = {P_YELLOW: "corte amarillo (%)", P_GREEN: "corte verde (%)", P_LISTINGS: "idénticos firmes mínimos",
               P_RATIO: "nuestro precio / mediana de ML (máximo)"}
DIRECTION_LABEL = {ROJO: "rojos que una persona prendió o mantuvo", VERDE: "verdes que una persona apagó"}

BORDER_PTS = 5.0
DOUBTFUL_RATIO = 1.5
MIN_SHARE = 0.5
MAX_EXTRA_LISTINGS = 3
MIN_RATIO = 1.0
# Rangos de nuestro precio con IVA (centavos de ARS).
PRICE_BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("bajo", 1_000_000), ("medio", 5_000_000), ("alto", 20_000_000), ("muy_alto", None))


def mode() -> int:
    return int(runtime.get("pm_learn_mode") or 0)


@dataclass(slots=True, frozen=True)
class Rule:
    id: int
    segment: str
    direction: str
    param: str
    value: float


# ─── Segmentos (puras) ────────────────────────────────────────────


def price_bucket(cents: int | None) -> str | None:
    if cents is None or cents <= 0:
        return None
    for name, top in PRICE_BUCKETS:
        if top is None or cents < top:
            return name
    return None


def segments_of(f: dict[str, Any], params: pr.ReviewParams) -> list[str]:
    """Los segmentos de un caso. `f`: margin_pct, firm_listings, our_price_cents, price_ratio, category y motive."""
    out: list[str] = []
    n = f.get("firm_listings")
    if n is not None and n < params.min_listings + 2:
        out.append("pocos_identicos")
    m = f.get("margin_pct")
    if m is not None and (abs(m - params.yellow_min) <= BORDER_PTS or abs(m - params.green_min) <= BORDER_PTS):
        out.append("margen_borde")
    ratio = f.get("price_ratio")
    if f.get("motive") == corr.MATCH_MALO or (ratio is not None and ratio >= DOUBTFUL_RATIO):
        out.append("match_dudoso")
    bucket = price_bucket(f.get("our_price_cents"))
    if bucket:
        out.append(f"precio:{bucket}")
    if f.get("category"):
        out.append(f"categoria:{str(f['category'])[:60]}")
    return out


def segment_label(segment: str) -> str:
    fixed = {"pocos_identicos": "pocos idénticos firmes", "margen_borde": "ganancia cerca de un corte",
             "match_dudoso": "match de ML dudoso"}
    if segment in fixed:
        return fixed[segment]
    kind, _, value = segment.partition(":")
    if kind == "precio":
        return f"precio {value.replace('_', ' ')}"
    if kind == "categoria":
        return f"categoría «{value}»"
    return segment


def blocking_rule(rules: Iterable[Rule], direction: str, f: dict[str, Any], params: pr.ReviewParams) -> Rule | None:
    """El ajuste aplicado que frena la acción automática de este caso (va a la bandeja), o None. PURA."""
    segs = set(segments_of(f, params))
    m, n, ratio = f.get("margin_pct"), f.get("firm_listings"), f.get("price_ratio")
    for r in rules:
        if r.direction != direction or r.segment not in segs:
            continue
        if r.param == P_YELLOW and m is not None and m >= r.value:
            return r
        if r.param == P_GREEN and m is not None and m < r.value:
            return r
        if r.param == P_LISTINGS and n is not None and n < r.value:
            return r
        if r.param == P_RATIO and ratio is not None and ratio > r.value:
            return r
    return None


def direction_of(c: SemaforoCorreccion) -> str | None:
    if c.hugo_color == ROJO and c.human_decision in (corr.ACTIVAR, corr.MANTENER):
        return ROJO
    if c.hugo_color == VERDE and c.human_decision == corr.INACTIVAR:
        return VERDE
    return None


def features(c: SemaforoCorreccion) -> dict[str, Any]:
    return {"margin_pct": c.margin_pct, "firm_listings": c.firm_listings, "our_price_cents": c.our_price_cents,
            "price_ratio": c.price_ratio, "category": c.category, "motive": c.motive}


@dataclass(slots=True)
class Proposal:
    segment: str
    direction: str
    param: str
    old_value: float
    new_value: float
    n_corrections: int
    n_direction: int
    reason: str
    evidence: list[int]


def _fmt(v: float) -> str:
    return f"{v:g}".replace(".", ",")


def propose(corrections: Sequence[SemaforoCorreccion], params: pr.ReviewParams, current: dict[tuple[str, str, str], float],
            *, min_corrections: int, max_shift: float) -> list[Proposal]:
    """Los ajustes que salen de las correcciones. PURA. `current` = lo aplicado hoy por (segmento, sentido, parámetro)."""
    out: list[Proposal] = []
    for direction in (ROJO, VERDE):
        cs = [c for c in corrections if direction_of(c) == direction]
        if len(cs) < min_corrections:
            continue
        by_seg: dict[str, list[SemaforoCorreccion]] = {}
        for c in cs:
            for seg in segments_of(features(c), params):
                by_seg.setdefault(seg, []).append(c)
        for seg, members in sorted(by_seg.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            if len(members) < min_corrections or len(members) / len(cs) < MIN_SHARE:
                continue
            p = _one(seg, direction, members, len(cs), params, current, max_shift)
            if p is not None:
                out.append(p)
    return out


def _one(seg: str, direction: str, members: list[SemaforoCorreccion], n_dir: int, params: pr.ReviewParams,
         current: dict[tuple[str, str, str], float], max_shift: float) -> Proposal | None:
    head = (f"{len(members)} de {n_dir} correcciones de {DIRECTION_LABEL[direction]} caen en «{segment_label(seg)}»")
    ids = [int(c.id) for c in members if c.id is not None]
    if seg == "pocos_identicos":
        param = P_LISTINGS
        base = current.get((seg, direction, param), float(params.min_listings))
        seen = [c.firm_listings for c in members if c.firm_listings is not None]
        if not seen:
            return None
        new = float(min(params.min_listings + MAX_EXTRA_LISTINGS, max(seen) + 1))
        if new <= base:
            return None
        why = (f"{head}: tenían entre {min(seen)} y {max(seen)} idénticos firmes. Para actuar solo en este segmento Hugo "
               f"pide {int(new)}; con menos, va a la bandeja.")
    elif seg == "match_dudoso":
        if direction != ROJO:
            return None
        param = P_RATIO
        base = current.get((seg, direction, param), float(params.max_price_ratio))
        ratios = [c.price_ratio for c in members if c.price_ratio is not None]
        if not ratios:
            return None
        new = round(max(MIN_RATIO, min(ratios) - 0.05), 2)
        if new >= base:
            return None
        why = (f"{head}: nuestro precio era {_fmt(round(min(ratios), 2))} veces la mediana de ML o más (match "
               f"probablemente equivocado). Hugo deja de apagar solo por encima de {_fmt(new)} veces.")
    else:
        margins = [c.margin_pct for c in members if c.margin_pct is not None]
        if not margins:
            return None
        med = statistics.median(margins)
        if direction == ROJO:
            param = P_YELLOW
            base = current.get((seg, direction, param), params.yellow_min)
            if med - 0.5 < params.yellow_min - max_shift:
                return None              # con un corte acotado no se explican: el motivo es otro (match, estrategia)
            new = round(med - 0.5, 1)
            if new >= base:
                return None
            why = (f"{head}; la mitad tenía una ganancia de {_fmt(round(med, 1))} % o más. Para apagar solo en este "
                   f"segmento el corte amarillo baja de {_fmt(base)} % a {_fmt(new)} %.")
        else:
            param = P_GREEN
            base = current.get((seg, direction, param), params.green_min)
            if med + 0.5 > params.green_min + max_shift:
                return None
            new = round(med + 0.5, 1)
            if new <= base:
                return None
            why = (f"{head}; la mitad tenía una ganancia de {_fmt(round(med, 1))} % o menos. Para activar solo en este "
                   f"segmento el corte verde sube de {_fmt(base)} % a {_fmt(new)} %.")
    return Proposal(seg, direction, param, float(base), float(new), len(members), n_dir, why[:500], ids)


# ─── Base ─────────────────────────────────────────────────────────


def active_rules(session: Session | None = None) -> list[Rule]:
    """Los ajustes aplicados. Si no se pueden leer, ninguno (el portón sigue con los cortes generales)."""
    try:
        if session is not None:
            rows = session.exec(select(SemaforoAjuste).where(SemaforoAjuste.status == APLICADO)).all()
        else:
            with Session(engine) as s:
                rows = s.exec(select(SemaforoAjuste).where(SemaforoAjuste.status == APLICADO)).all()
    except Exception:
        log.exception("semáforo: no se pudieron leer los ajustes aprendidos")
        return []
    return [Rule(int(r.id), r.segment, r.direction, r.param, float(r.new_value)) for r in rows]  # type: ignore[arg-type]


def _current(s: Session) -> dict[tuple[str, str, str], float]:
    return {(r.segment, r.direction, r.param): float(r.new_value)
            for r in s.exec(select(SemaforoAjuste).where(SemaforoAjuste.status == APLICADO)).all()}


def run_learning(now: datetime | None = None) -> dict[str, Any]:
    """El aprendizaje semanal. Con `pm_learn_mode = 0` no hace nada; con 1 deja los ajustes «propuestos»; con 2 los aplica
    (actor `hugo:auto`). No repite una propuesta que ya está pendiente. Devuelve un resumen."""
    m = mode()
    if m == 0:
        return {"modo": 0}
    now = now or utcnow()
    params = pr.ReviewParams.from_runtime()
    window = timedelta(days=max(1, int(runtime.get("pm_learn_window_days") or 90)))
    created: list[int] = []
    with Session(engine, expire_on_commit=False) as s:
        rows = list(s.exec(select(SemaforoCorreccion).where(SemaforoCorreccion.at >= now - window)).all())
        current = _current(s)
        pending = {(r.segment, r.direction, r.param, round(r.new_value, 3))
                   for r in s.exec(select(SemaforoAjuste).where(SemaforoAjuste.status == PROPUESTO)).all()}
        proposals = propose(rows, params, current, min_corrections=int(runtime.get("pm_learn_min_corrections") or 5),
                            max_shift=float(runtime.get("pm_learn_max_shift_pts") or 5.0))
        for p in proposals:
            if (p.segment, p.direction, p.param, round(p.new_value, 3)) in pending:
                continue
            row = SemaforoAjuste(created_at=now, segment=p.segment[:80], direction=p.direction, param=p.param,
                                 old_value=p.old_value, new_value=p.new_value, n_corrections=p.n_corrections,
                                 n_direction=p.n_direction, reason=p.reason, evidence=json.dumps(p.evidence[:200]),
                                 status=PROPUESTO)
            s.add(row)
            s.flush()
            created.append(int(row.id))  # type: ignore[arg-type]
        s.commit()
    applied = 0
    if m == 2:
        for adj_id in created:
            try:
                apply(adj_id, pr.ACTOR_AUTO, now=now)
                applied += 1
            except LearnError as exc:
                log.info("semáforo: el ajuste %s no se aplicó solo (%s)", adj_id, exc.code)
    out = {"modo": m, "correcciones": len(rows), "propuestos": len(created), "aplicados": applied}
    log.info("semáforo aprende: %s", out)
    return out


class LearnError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


def _get(s: Session, adj_id: int) -> SemaforoAjuste:
    row = s.get(SemaforoAjuste, adj_id)
    if row is None:
        raise LearnError(404, "no_existe", "Ajuste no encontrado.")
    return row


def apply(adj_id: int, actor: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Propuesto → aplicado. Si ya había uno aplicado para el mismo segmento, sentido y corte, queda «reemplazado» (y
    Deshacer lo vuelve a poner)."""
    now = now or utcnow()
    with Session(engine, expire_on_commit=False) as s:
        row = _get(s, adj_id)
        if row.status != PROPUESTO:
            raise LearnError(409, "estado_cambio", f"El ajuste ya está «{STATUS_LABEL.get(row.status, row.status)}».")
        prev = s.exec(select(SemaforoAjuste).where(
            SemaforoAjuste.status == APLICADO, SemaforoAjuste.segment == row.segment,
            SemaforoAjuste.direction == row.direction, SemaforoAjuste.param == row.param)).first()
        if prev is not None:
            prev.status = REEMPLAZADO
            s.add(prev)
            row.replaces_id = prev.id
        row.status, row.decided_by, row.decided_at = APLICADO, actor[:120], now
        s.add(row)
        s.commit()
        log.info("semáforo: ajuste %s aplicado (%s %s %s: %s → %s), por %s", adj_id, row.segment, row.direction,
                 row.param, row.old_value, row.new_value, actor[:120])
        return to_dict(row)


def reject(adj_id: int, actor: str, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    with Session(engine, expire_on_commit=False) as s:
        row = _get(s, adj_id)
        if row.status != PROPUESTO:
            raise LearnError(409, "estado_cambio", f"El ajuste ya está «{STATUS_LABEL.get(row.status, row.status)}».")
        row.status, row.decided_by, row.decided_at = RECHAZADO, actor[:120], now
        s.add(row)
        s.commit()
        return to_dict(row)


def undo(adj_id: int, actor: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Aplicado → deshecho. Si había reemplazado a otro, ese vuelve a quedar aplicado."""
    now = now or utcnow()
    with Session(engine, expire_on_commit=False) as s:
        row = _get(s, adj_id)
        if row.status != APLICADO:
            raise LearnError(409, "estado_cambio", "Solo se deshace un ajuste aplicado.")
        row.status, row.undone_by, row.undone_at = DESHECHO, actor[:120], now
        s.add(row)
        if row.replaces_id:
            prev = s.get(SemaforoAjuste, row.replaces_id)
            if prev is not None and prev.status == REEMPLAZADO:
                prev.status = APLICADO
                s.add(prev)
        s.commit()
        log.info("semáforo: ajuste %s deshecho, por %s", adj_id, actor[:120])
        return to_dict(row)


def to_dict(row: SemaforoAjuste) -> dict[str, Any]:
    def iso(dt: datetime | None) -> str | None:
        return None if dt is None else dt.isoformat() + "Z"

    try:
        evidence = json.loads(row.evidence) if row.evidence else []
    except ValueError:
        evidence = []
    return {
        "id": row.id, "created_at": iso(row.created_at), "segment": row.segment,
        "segment_label": segment_label(row.segment), "direction": row.direction,
        "direction_label": DIRECTION_LABEL.get(row.direction, row.direction), "param": row.param,
        "param_label": PARAM_LABEL.get(row.param, row.param), "old_value": row.old_value, "new_value": row.new_value,
        "n_corrections": row.n_corrections, "n_direction": row.n_direction, "reason": row.reason,
        "evidence": evidence if isinstance(evidence, list) else [], "status": row.status,
        "status_label": STATUS_LABEL.get(row.status, row.status), "replaces_id": row.replaces_id,
        "decided_by": row.decided_by, "decided_at": iso(row.decided_at), "undone_by": row.undone_by,
        "undone_at": iso(row.undone_at),
    }


def list_adjustments(limit: int = 200) -> list[dict[str, Any]]:
    with Session(engine) as s:
        rows = s.exec(select(SemaforoAjuste).order_by(SemaforoAjuste.created_at.desc(), SemaforoAjuste.id.desc())  # type: ignore[union-attr]
                      .limit(limit)).all()
        return [to_dict(r) for r in rows]
