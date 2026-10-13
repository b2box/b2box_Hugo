"""Diagnóstico a-e de un producto en rojo. Funciones PURAS: sin red, sin DB, sin settings.

Para un rojo firme la bandeja responde "¿por qué sale caro?" con cinco reglas, evaluadas todas y
en este orden (sección 3.8 del diseño). La CAUSA PROBABLE es la primera que se cumple; y cada regla
dice además si corregirla sacaría al producto del rojo: «si», «no» o «aprox» (no se puede asegurar).

    (a) caja fuera de rango para su categoría   → corregir la caja en Vendure
    (b) tramos de cantidad mal armados          → corregir los tramos / volver a calcular
    (c) recargo de certificado alto             → revisar si lo necesita y su costo
    (d) valor bajo por m³ (no compite por volumen)  → estructural
    (e) Mercado Libre por debajo de nuestro costo de fábrica → estructural

Todo se expresa contra UNA cuenta: el precio máximo para no ser rojo,

    P_max = (M × (1 − c/100) − s) / (1 + y/100)

con P nuestro precio con IVA, M la mediana de ML, c la comisión de ML, s el envío (centavos) e y el corte
del amarillo. Estar por encima de ML es el mismo caso (si P > M entonces P > P_max). Un "qué pasaría si"
sale de `P_si ≤ P_max`.

Hugo NO recalcula la fórmula de Vendure: las cifras salen del desglose que Vendure devuelve
(`price_breakdown.TierBreakdown`). Los "qué pasaría si" son aproximados y van rotulados "estimado";
(c) es casi exacto. Si un dato no se pudo leer, la regla dice `sin_dato`: no se inventa.

Con `sourcingMode = local_stock` no hay flete ni cambio: (a), (d) y (e) no aplican.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from statistics import median
from typing import Any, Sequence

from app.pricing.price_breakdown import TierBreakdown
from app.pricing.semaforo import PricedVariant, PriceTier

# ── Códigos y estados ─────────────────────────────────────────────

CAUSES = ("a", "b", "c", "d", "e")
NO_CAUSE = "ninguna"        # se evaluaron las cinco reglas y ninguna explica el rojo
UNKNOWN_CAUSE = "sin_dato"  # ninguna dispara, pero a alguna le faltó un dato: no se puede descartar

ST_FIRES = "dispara"
ST_NO = "no"
ST_NO_DATA = "sin_dato"
ST_NA = "no_aplica"

FIX_YES = "si"
FIX_NO = "no"
FIX_APPROX = "aprox"

LOCAL_STOCK = "local_stock"

# La escalera de cantidades de Vendure (engine/minimum-purchase.ts) y la compra mínima por defecto del canal.
QUANTITY_LADDER = (1, 5, 10, 25, 50, 100, 250, 500)
DEFAULT_MIN_PURCHASE_CNY = 500


@dataclass(slots=True, frozen=True)
class DiagThresholds:
    """Umbrales del diagnóstico (settings `pm_diag_*` en el dashboard)."""
    cert_share_pct: float = 10.0       # (c): el certificado pesa tanto o más que esto del neto
    logistics_ratio: float = 1.0       # (d): logística / fábrica
    box_z: float = 3.0                 # (a): |z| robusto mínimo
    min_neighbors: int = 5             # (a): vecinos con caja válida para aceptar la referencia
    ratio_out: float = 2.0             # (a): además de |z|, el cociente contra la referencia (2x / 0,5x)
    sale_price_tol_pct: float = 1.0    # (b5): precio guardado vs el que da Vendure
    list_price_tol_pct: float = 2.0    # (b5): precio de lista vs factura del cálculo
    ladder_steps: int = 1              # (b4): diferencia tolerada contra la escalera


# ─── Plata ────────────────────────────────────────────────────────


def price_max_cents(median_cents: float, commission_pct: float, shipping_cents: float, yellow_min_pct: float) -> float:
    """Precio máximo (con IVA, centavos) para NO ser rojo: el que deja justo `yellow_min_pct` de ganancia."""
    return (median_cents * (1.0 - commission_pct / 100.0) - shipping_cents) / (1.0 + yellow_min_pct / 100.0)


@dataclass(slots=True, frozen=True)
class Economics:
    """Lo del semáforo que se necesita para las cuentas: sale del snapshot y de los settings."""
    our_price_cents: int
    ml_median_cents: int
    commission_pct: float
    shipping_cents: int
    yellow_min_pct: float

    @property
    def p_max_cents(self) -> float:
        return price_max_cents(self.ml_median_cents, self.commission_pct, self.shipping_cents, self.yellow_min_pct)

    @property
    def gap(self) -> float | None:
        """Cuánto hay que bajar el precio (0-1) para salir del rojo; None si ni a precio cero alcanza."""
        if self.our_price_cents <= 0 or self.p_max_cents <= 0:
            return None
        return max(0.0, 1.0 - self.p_max_cents / self.our_price_cents)

    def gets_out(self, new_price_cents: float) -> bool:
        return new_price_cents <= self.p_max_cents


def ars(cents: float | None) -> str:
    """Centavos → "$ 18.982" (pesos enteros, punto de miles)."""
    if cents is None:
        return "—"
    return "$ " + f"{round(cents / 100):,}".replace(",", ".")


def _pct(v: float, digits: int = 1) -> str:
    return f"{v:.{digits}f}".replace(".", ",") + " %"


def _num(v: float, digits: int = 1) -> str:
    text = f"{v:,.{digits}f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


# ─── Cajas ────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class Box:
    """Medidas en cm y kg. None / 0 = sin dato."""
    length: float | None = None
    width: float | None = None
    height: float | None = None
    weight: float | None = None

    @property
    def has_dims(self) -> bool:
        return all(v and v > 0 for v in (self.length, self.width, self.height))

    @property
    def volume_cm3(self) -> float | None:
        return self.length * self.width * self.height if self.has_dims else None  # type: ignore[operator]

    @property
    def density(self) -> float | None:
        """kg por m³."""
        v = self.volume_cm3
        return self.weight / (v / 1_000_000.0) if v and self.weight and self.weight > 0 else None

    @property
    def is_complete(self) -> bool:
        return self.has_dims and bool(self.weight and self.weight > 0)

    def dims_text(self) -> str:
        return "×".join(_num(x, 0 if float(x).is_integer() else 1) for x in (self.length, self.width, self.height))  # type: ignore[arg-type]


def box_from_specs(specs: dict[str, float] | None, prefix: str = "box_") -> Box:
    """Caja (`box_*`) o producto (`""`) a partir de las claves de market_specs.OurSpecs.as_dict()."""
    s = specs or {}
    return Box(s.get(f"{prefix}length"), s.get(f"{prefix}width"), s.get(f"{prefix}height"), s.get(f"{prefix}weight"))


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


@dataclass(slots=True, frozen=True)
class BoxReference:
    """Caja típica de los productos parecidos, con la dispersión para medir qué tan lejos queda la nuestra."""
    source: str                     # "vecinos_clip" | "typicalBox"
    n: int
    volume_cm3: float               # mediana
    weight_kg: float
    density: float
    # mediana y MAD de ln(valor) de los vecinos, por métrica; el z robusto sale de ahí
    stats: dict[str, tuple[float, float]] = field(default_factory=dict)
    dims_text: str = ""

    def z(self, metric: str, value: float) -> float:
        med, mad = self.stats[metric]
        diff = math.log(value) - med
        scale = 1.4826 * mad
        if scale <= 0:
            return 0.0 if abs(diff) < 1e-12 else math.copysign(math.inf, diff)
        return diff / scale

    def ratio(self, metric: str, value: float) -> float:
        return value / {"volume": self.volume_cm3, "weight": self.weight_kg, "density": self.density}[metric]


def box_reference(boxes: Sequence[Box], min_neighbors: int = 5, source: str = "vecinos_clip") -> BoxReference | None:
    """Referencia de caja a partir de las cajas de los productos parecidos. Sirve una caja con sus 4
    medidas > 0; si sobran, se descartan las que quedan fuera de P5-P95 del volumen (sin bajar del
    mínimo). Con menos de `min_neighbors` válidas no hay referencia (None)."""
    valid = [b for b in boxes if b.is_complete]
    if len(valid) < max(1, min_neighbors):
        return None
    vols = sorted(b.volume_cm3 for b in valid)  # type: ignore[type-var]
    lo, hi = _percentile(vols, 0.05), _percentile(vols, 0.95)
    trimmed = [b for b in valid if lo <= b.volume_cm3 <= hi]  # type: ignore[operator]
    if len(trimmed) >= min_neighbors:
        valid = trimmed
    series = {
        "volume": [math.log(b.volume_cm3) for b in valid],            # type: ignore[arg-type]
        "weight": [math.log(b.weight) for b in valid],                # type: ignore[arg-type]
        "density": [math.log(b.density) for b in valid],              # type: ignore[arg-type]
    }
    stats = {}
    for name, vals in series.items():
        med = median(vals)
        stats[name] = (med, median(abs(v - med) for v in vals))
    dims = [median(getattr(b, side) for b in valid) for side in ("length", "width", "height")]
    return BoxReference(
        source=source, n=len(valid), volume_cm3=math.exp(stats["volume"][0]), weight_kg=math.exp(stats["weight"][0]),
        density=math.exp(stats["density"][0]), stats=stats,
        dims_text="×".join(_num(d, 0 if float(d).is_integer() else 1) for d in dims),
    )


# ─── Certificado y tramos del catálogo ────────────────────────────


@dataclass(slots=True, frozen=True)
class CertFacts:
    status: str | None = None
    cost_usd_cents: int | None = None
    expires_at: str | None = None
    holder: str | None = None
    max_share_pct: float | None = None   # tope del canal (certificateMaxSharePct)


def tiers_from_variant(variant: PricedVariant | None) -> list[PriceTier] | None:
    """Los tramos HABILITADOS de la variante, en el orden de `position`. None si no hay variante."""
    if variant is None:
        return None
    return sorted((t for t in variant.tiers if t.enabled), key=lambda t: t.position)


@dataclass(slots=True)
class DiagFacts:
    """Todo lo que el diagnóstico necesita saber. Lo que no se pudo leer queda en None (→ sin dato)."""
    economics: Economics
    sourcing_mode: str | None = None                  # import | local_stock | None (no se sabe: se trata como import)
    tier_used: str | None = None                      # "tier:min_qty=12" | "priceWithTax" | "priceWithTax(fallback)"
    breakdowns: list[TierBreakdown] | None = None     # None = no se pudo leer; [] = Vendure no devolvió tramos
    tiers: list[PriceTier] | None = None
    min_quantity_override: int | None = None
    min_purchase_cny: int | None = None
    box: Box = field(default_factory=Box)
    product: Box = field(default_factory=Box)         # medidas del producto (sin caja), para (a3)
    neighbor_boxes: list[Box] | None = None           # cajas de los vecinos CLIP; None = no se consultó
    certification: CertFacts | None = None
    certification_read: bool = False                  # ¿se pudo leer? (None + True = no necesita certificado)

    @property
    def is_local_stock(self) -> bool:
        return self.sourcing_mode == LOCAL_STOCK


# ─── Resultado ────────────────────────────────────────────────────


@dataclass(slots=True)
class RuleResult:
    code: str
    title: str
    status: str
    summary: str
    subcodes: list[str] = field(default_factory=list)
    fixes: str | None = None                   # si | no | aprox (solo si dispara)
    p_si_cents: int | None = None
    estimated: bool = False                    # el P_si es una estimación (no una cuenta de Vendure)
    action: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    # La misma frase SIN cifras del costo de fábrica, para quien no es responsable de costos (PRICE_REVIEW_VIEWERS).
    # None = la frase normal no trae ninguna.
    public_summary: str | None = None

    @property
    def fires(self) -> bool:
        return self.status == ST_FIRES

    def to_dict(self) -> dict[str, Any]:
        out = {
            "code": self.code, "title": self.title, "status": self.status, "summary": self.summary,
            "subcodes": self.subcodes, "fixes": self.fixes, "p_si_cents": self.p_si_cents,
            "estimated": self.estimated, "action": self.action, "evidence": self.evidence,
        }
        if self.public_summary is not None:
            out["summary_public"] = self.public_summary
        return out


@dataclass(slots=True)
class Diagnosis:
    rules: list[RuleResult]
    probable_cause: str
    fixable: str                       # si | no | aprox: ¿alguna de a-c, corregida, saca del rojo?
    fix_not_enough: bool               # "corregir lo nuestro no alcanza"
    p_max_cents: float
    p_cents: int
    gap: float | None
    missing: list[str]                 # qué no se pudo leer
    suggestion: str                    # pista de decisión (no obligatoria)

    def rule(self, code: str) -> RuleResult:
        return next(r for r in self.rules if r.code == code)

    @property
    def complete(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "rules": [r.to_dict() for r in self.rules], "probable_cause": self.probable_cause,
            "fixable": self.fixable, "fix_not_enough": self.fix_not_enough,
            "p_max_cents": round(self.p_max_cents), "p_cents": self.p_cents,
            "gap": None if self.gap is None else round(self.gap, 4),
            "missing": self.missing, "suggestion": self.suggestion,
        }


# ─── Tramo usado ──────────────────────────────────────────────────

_TIER_LABEL = re.compile(r"^tier:min_qty=(\d+)$")


def parse_tier_used(label: str | None) -> int | None:
    """"tier:min_qty=12" → 12. Cualquier otra etiqueta (priceWithTax…) → None."""
    m = _TIER_LABEL.match(label or "")
    return int(m.group(1)) if m else None


def select_tier(breakdowns: Sequence[TierBreakdown] | None, tier_used: str | None) -> TierBreakdown | None:
    """La fila del desglose del tramo con el que se calculó nuestro precio. `tier:min_qty=N` es ese tramo;
    `priceWithTax` (política «tramo más barato», o el respaldo) es el de menor factura por unidad."""
    rows = [b for b in (breakdowns or []) if b.usd_available]
    if not rows:
        return None
    want = parse_tier_used(tier_used)
    if want is not None:
        for b in rows:
            if b.min_quantity == want:
                return b
        # el label usa la posición cuando el tramo no tiene cantidad mínima
        return None
    return min(rows, key=lambda b: b.factura_per_unit_minor)


# ─── (a) caja fuera de rango ─────────────────────────────────────

TITLES = {
    "a": "Caja fuera de rango para su categoría",
    "b": "Tramos de cantidad mal armados",
    "c": "Recargo de certificado alto",
    "d": "Valor bajo por m³",
    "e": "Mercado Libre por debajo de nuestro costo de fábrica",
}
ACTIONS = {
    "a": "Corregir la caja de la variante en Vendure y volver a medir.",
    "b": "Corregir los tramos de la variante en Vendure (precio, cantidades) y volver a medir.",
    "c": "Revisar si el producto necesita certificado y si el costo cargado es correcto.",
    "d": "Mantener solo si es un producto de nicho; si no, despublicar.",
    "e": "Buscar otro proveedor más barato o despublicar.",
}

_METRIC_LABEL = {"volume": "volumen", "weight": "peso", "density": "densidad"}


def _fix_state(econ: Economics, p_si: float | None, *, exact: bool, floor: float | None = None) -> str:
    """si | no | aprox según el precio corregido contra P_max.

    `exact`: la cuenta es la de Vendure (o casi): el resultado manda. Si no es exacta (cota inferior del
    ahorro) y no alcanza, solo se descarta («no») cuando ni quitando TODA la logística se llega."""
    if p_si is None:
        return FIX_APPROX
    if econ.gets_out(p_si):
        return FIX_YES
    if exact:
        return FIX_NO
    if floor is not None and not econ.gets_out(floor):
        return FIX_NO
    return FIX_APPROX


def rule_a(f: DiagFacts, th: DiagThresholds, row: TierBreakdown | None) -> RuleResult:
    code = "a"
    base = dict(code=code, title=TITLES[code], action=ACTIONS[code])
    if f.is_local_stock:
        return RuleResult(**base, status=ST_NA, summary="Abastecimiento local: no hay flete que dependa de la caja.")
    box = f.box
    if not box.has_dims and not (box.weight and box.weight > 0):
        return RuleResult(**base, status=ST_NO_DATA, summary="La variante no tiene la caja cargada (medidas ni peso).")

    reasons: list[str] = []
    subcodes: list[str] = []
    evidence: dict[str, Any] = {"box": {"dims_cm": [box.length, box.width, box.height], "weight_kg": box.weight}}

    # (a3) imposibles internos: no necesitan vecinos
    prod = f.product
    v, vp = box.volume_cm3, prod.volume_cm3
    if v and vp and v < vp:
        subcodes.append("a3")
        reasons.append(f"la caja ({box.dims_text()} cm) es más chica que el producto ({prod.dims_text()} cm)")
    elif v and vp and v / vp > 10:
        subcodes.append("a3")
        reasons.append(f"la caja ({box.dims_text()} cm) tiene {_num(v / vp, 0)} veces el volumen del producto ({prod.dims_text()} cm)")
    if box.weight and prod.weight and box.weight < prod.weight:
        if "a3" not in subcodes:
            subcodes.append("a3")
        reasons.append(f"la caja pesa {_num(box.weight)} kg y el producto {_num(prod.weight)} kg")

    # (a1/a2) contra la referencia de los productos parecidos
    ref = box_reference(f.neighbor_boxes, th.min_neighbors) if f.neighbor_boxes is not None else None
    ref_note = ""
    if f.neighbor_boxes is None:
        ref_note = "no se consultaron productos parecidos"
    elif ref is None:
        ref_note = f"hay menos de {th.min_neighbors} productos parecidos con caja válida"
    if ref is not None:
        evidence["reference"] = {"source": ref.source, "n": ref.n, "dims_cm_median": ref.dims_text,
                                 "volume_cm3": round(ref.volume_cm3), "weight_kg": round(ref.weight_kg, 2),
                                 "density": round(ref.density, 1)}
        values = {"volume": v, "weight": box.weight if box.weight and box.weight > 0 else None, "density": box.density}
        for metric, value in values.items():
            if not value:
                continue
            z, ratio = ref.z(metric, value), ref.ratio(metric, value)
            evidence.setdefault("z", {})[metric] = None if math.isinf(z) else round(z, 2)
            evidence.setdefault("ratio", {})[metric] = round(ratio, 2)
            if abs(z) >= th.box_z and (ratio >= th.ratio_out or ratio <= 1.0 / th.ratio_out):
                sub = "a1" if metric == "volume" else "a2"
                if sub not in subcodes:
                    subcodes.append(sub)
                word = "mayor" if ratio > 1 else "menor"
                reasons.append(f"su {_METRIC_LABEL[metric]} es {_num(ratio if ratio >= 1 else 1 / ratio)} veces {word} que el de los parecidos")
    if not subcodes:
        if ref is None:
            # Sin productos parecidos con qué compararla solo se pudieron mirar los imposibles internos (a3):
            # no se puede afirmar que la caja esté bien.
            return RuleResult(**base, status=ST_NO_DATA, evidence=evidence, summary=(
                f"No se pudo comparar la caja con la de productos parecidos ({ref_note}); "
                "tampoco es imposible por sí sola (más chica que el producto o enorme)."))
        return RuleResult(**base, status=ST_NO, evidence=evidence, summary=(
            f"La caja {box.dims_text()} cm, {_num(box.weight or 0)} kg no se aleja de lo normal "
            f"(parecidos: ~{ref.dims_text} cm)." if box.is_complete else "No se detecta una caja fuera de rango."))

    # Qué pasaría con la caja de referencia (cota inferior del ahorro: ignora aranceles sobre el flete)
    p_si: float | None = None
    fixes = FIX_APPROX
    summary_tail = " No se puede estimar el ahorro (falta la referencia de caja o el desglose): se corrige y se vuelve a medir."
    if ref is not None and row is not None and row.chargeable_units > 0 and row.nbg_usd > 0:
        chargeable_ref = max(ref.volume_cm3 * row.quantity / 1_000_000.0, ref.weight_kg * row.quantity / 1000.0)
        share = float(row.logistics_usd / row.nbg_usd)
        if chargeable_ref < row.chargeable_units:
            p = f.economics.our_price_cents
            p_si = p * (1.0 - share * (1.0 - chargeable_ref / row.chargeable_units))
            floor = p * (1.0 - share)
            fixes = _fix_state(f.economics, p_si, exact=False, floor=floor)
            evidence["p_si_floor_cents"] = round(floor)
            outcome = {FIX_YES: "sale del rojo", FIX_NO: "no alcanza para salir del rojo",
                       FIX_APPROX: "quizá no alcance para salir del rojo"}[fixes]
            summary_tail = (f" Con la caja de referencia el precio bajaría a ~{ars(p_si)} (estimado, como mínimo): {outcome}.")
        else:
            summary_tail = " Con la caja de referencia el flete no baja."
    ref_txt = f"; los parecidos miden ~{ref.dims_text} cm (mediana de {ref.n})" if ref is not None else ""
    summary = (f"La caja {box.dims_text()} cm, {_num(box.weight or 0)} kg parece mal cargada: "
               + "; ".join(reasons) + ref_txt + "." + summary_tail)
    return RuleResult(**base, status=ST_FIRES, summary=summary, subcodes=subcodes, fixes=fixes,
                      p_si_cents=None if p_si is None else round(p_si), estimated=True, evidence=evidence)


# ─── (b) tramos mal armados ──────────────────────────────────────


def ladder_index(quantity: float) -> int:
    """Posición de una cantidad en la escalera de Vendure (primer escalón ≥ q); más allá de 500, uno más cada 500."""
    for i, rung in enumerate(QUANTITY_LADDER):
        if quantity <= rung:
            return i
    return len(QUANTITY_LADDER) - 1 + math.ceil(quantity / 500.0)


def round_up_to_ladder(quantity: float) -> int:
    for rung in QUANTITY_LADDER:
        if quantity <= rung:
            return rung
    return math.ceil(quantity)


def minimum_quantity_for_cost(unit_cost_cny: float | None, threshold_cny: float) -> int | None:
    """Unidades para llegar a la compra mínima al costo dado (engine/minimum-purchase.ts: minimumQuantityForCost)."""
    if not unit_cost_cny or unit_cost_cny <= 0 or threshold_cny <= 0:
        return None
    if unit_cost_cny >= threshold_cny:
        return 1
    return round_up_to_ladder(math.ceil(threshold_cny / unit_cost_cny))


def next_bracket_start(derived: int, previous_start: int) -> int:
    if derived > previous_start:
        return derived
    return round_up_to_ladder(previous_start + 1)


def build_quantity_brackets(
    costs_cny: Sequence[float | None], threshold_cny: float, override_first_start: int | None = None,
) -> list[tuple[int, int | None] | None]:
    """(minQuantity, maxQuantity|None) de cada tramo, o None si el tramo no tiene un costo usable.
    Port de `buildQuantityBrackets` (engine/minimum-purchase.ts) con test de paridad contra la salida del
    código de Vendure; los costos van en yuanes por unidad."""
    starts: list[int | None] = []
    previous = 0
    for index, cost in enumerate(costs_cny):
        derived = (override_first_start if index == 0 and override_first_start and override_first_start > 0
                   else minimum_quantity_for_cost(cost, threshold_cny))
        if derived is None:
            starts.append(None)
            continue
        start = next_bracket_start(derived, previous)
        starts.append(start)
        previous = start
    brackets: list[tuple[int, int | None] | None] = [None] * len(starts)
    next_start: int | None = None
    for index in range(len(starts) - 1, -1, -1):
        mn = starts[index]
        if mn is None:
            continue
        brackets[index] = (mn, None if next_start is None else next_start - 1)
        next_start = mn
    return brackets


def _tier_valid_price(t: PriceTier, import_mode: bool) -> bool:
    sale_ok = t.sale_price_cents is not None and t.sale_price_cents > 0
    cost_ok = (t.cost_cny_minor is not None and t.cost_cny_minor > 0) if import_mode else True
    return sale_ok and cost_ok


def rule_b(f: DiagFacts, th: DiagThresholds, row: TierBreakdown | None) -> RuleResult:
    code = "b"
    base = dict(code=code, title=TITLES[code], action=ACTIONS[code])
    tiers = f.tiers
    rows = f.breakdowns
    if tiers is None and rows is None:
        return RuleResult(**base, status=ST_NO_DATA, summary="No se pudieron leer los tramos de la variante.")
    import_mode = not f.is_local_stock
    subcodes: list[str] = []
    reasons: list[str] = []
    evidence: dict[str, Any] = {}

    if tiers is not None:
        evidence["tiers"] = [{"min": t.min_quantity, "max": t.max_quantity, "cny_cents": t.cost_cny_minor,
                              "sale_cents": t.sale_price_cents} for t in tiers]
        if not tiers:
            subcodes.append("b1")
            reasons.append("la variante no tiene ningún tramo habilitado")
        # b1: falta precio
        broken = [t for t in tiers if not _tier_valid_price(t, import_mode)]
        if broken and tiers:
            subcodes.append("b1")
            what = " ni ".join(x for x in (
                "precio de venta" if any(not (t.sale_price_cents and t.sale_price_cents > 0) for t in broken) else "",
                "costo de fábrica" if import_mode and any(not (t.cost_cny_minor and t.cost_cny_minor > 0) for t in broken) else "",
            ) if x)
            reasons.append(f"{len(broken)} tramo(s) habilitado(s) sin {what}")
        # b2: precios que SUBEN al subir la cantidad
        ordered = sorted((t for t in tiers if t.min_quantity is not None), key=lambda t: t.min_quantity)  # type: ignore[arg-type,return-value]
        rises: list[str] = []
        for lo, hi in zip(ordered, ordered[1:]):
            if import_mode and lo.cost_cny_minor and hi.cost_cny_minor and hi.cost_cny_minor > lo.cost_cny_minor:
                rises.append("el costo en yuanes")
            if lo.sale_price_cents and hi.sale_price_cents and hi.sale_price_cents > lo.sale_price_cents:
                rises.append("el precio de venta")
        if rises:
            subcodes.append("b2")
            reasons.append(f"{' y '.join(sorted(set(rises)))} sube al comprar más")
        # b3: cantidades que no cierran
        gaps = _quantity_gaps(tiers)
        if gaps:
            subcodes.append("b3")
            reasons.append("las cantidades no cierran (" + "; ".join(gaps) + ")")
        # b4: el primer tramo contra la regla de compra mínima
        if import_mode and tiers and tiers[0].min_quantity is not None:
            threshold = f.min_purchase_cny or DEFAULT_MIN_PURCHASE_CNY
            costs = [(t.cost_cny_minor / 100.0) if t.cost_cny_minor else None for t in tiers]
            expected = build_quantity_brackets(costs, threshold, f.min_quantity_override)[0]
            evidence["min_purchase_cny"] = threshold
            if expected is not None:
                evidence["first_tier"] = {"actual": tiers[0].min_quantity, "expected": expected[0]}
                if abs(ladder_index(tiers[0].min_quantity) - ladder_index(expected[0])) > th.ladder_steps:
                    subcodes.append("b4")
                    reasons.append(f"el primer tramo arranca en {tiers[0].min_quantity} unidades y la regla de compra "
                                   f"mínima (¥{threshold}) daría {expected[0]}")

    # b1 también se ve en el desglose: un tramo sin precio vuelve en ceros
    if rows is not None and any(not b.usd_available for b in rows) and "b1" not in subcodes and import_mode:
        subcodes.append("b1")
        reasons.append("Vendure no pudo calcular el precio de algún tramo")

    # b5: precio guardado viejo (contra lo que da Vendure hoy)
    p_si: float | None = None
    if tiers is not None and rows:
        stale = _stale_prices(tiers, rows, th.sale_price_tol_pct)
        if stale:
            evidence["stale_tiers"] = stale
            subcodes.append("b5")
            worst = max(stale, key=lambda s: abs(s["diff_pct"]))
            reasons.append(f"el precio guardado del tramo de {worst['min']} unidades difiere {_pct(abs(worst['diff_pct']))} "
                           "del que da el cálculo de Vendure")
    if row is not None and row.factura_per_unit_minor > 0:
        diff = (f.economics.our_price_cents - row.factura_per_unit_minor) / row.factura_per_unit_minor * 100.0
        evidence["list_vs_invoice_pct"] = round(diff, 2)
        if abs(diff) > th.list_price_tol_pct and "b5" not in subcodes:
            subcodes.append("b5")
            reasons.append(f"el precio de lista ({ars(f.economics.our_price_cents)}) no coincide con el cálculo "
                           f"({ars(row.factura_per_unit_minor)})")
        if "b5" in subcodes:
            p_si = float(row.factura_per_unit_minor)

    # Evidencia extra (no dispara): margen con el tramo más barato
    cheap = _cheapest_evidence(f, rows)
    if cheap:
        evidence["cheapest_tier"] = cheap

    if not subcodes:
        return RuleResult(**base, status=ST_NO, evidence=evidence, summary="Los tramos están bien armados.")

    structural = [s for s in subcodes if s != "b5"]
    if structural:
        fixes, p_out = FIX_APPROX, None
        tail = " No se estima el efecto: se corrige y se vuelve a medir."
    elif p_si is not None:
        fixes, p_out = _fix_state(f.economics, p_si, exact=True), round(p_si)
        tail = (f" Con el precio que da Vendure ({ars(p_si)}) " +
                ("sale del rojo." if fixes == FIX_YES else "sigue en rojo."))
    else:
        fixes, p_out, tail = FIX_APPROX, None, ""
    if cheap and cheap.get("only_min_purchase"):
        tail += " Con el tramo más barato no sería rojo: el rojo es por la compra mínima."
    return RuleResult(**base, status=ST_FIRES, subcodes=subcodes, fixes=fixes, p_si_cents=p_out,
                      estimated=False, evidence=evidence, summary="; ".join(reasons).capitalize() + "." + tail)


def _quantity_gaps(tiers: Sequence[PriceTier]) -> list[str]:
    out: list[str] = []
    if tiers and (tiers[0].min_quantity is None or tiers[0].min_quantity < 1):
        out.append("el primer tramo no arranca en 1 o más")
    for lo, hi in zip(tiers, tiers[1:]):
        if lo.max_quantity is None or hi.min_quantity is None:
            out.append("falta un límite entre tramos")
        elif lo.max_quantity + 1 != hi.min_quantity:
            out.append(f"el tramo termina en {lo.max_quantity} y el siguiente arranca en {hi.min_quantity}")
    if tiers and tiers[-1].max_quantity is not None:
        out.append("el último tramo tiene tope de cantidad")
    return out


def _stale_prices(tiers: Sequence[PriceTier], rows: Sequence[TierBreakdown], tol_pct: float) -> list[dict[str, Any]]:
    by_qty = {b.min_quantity: b for b in rows if b.usd_available}
    out = []
    for t in tiers:
        b = by_qty.get(t.min_quantity)
        if b is None or not t.sale_price_cents or b.neto_per_unit_minor <= 0:
            continue
        diff = (t.sale_price_cents - b.neto_per_unit_minor) / b.neto_per_unit_minor * 100.0
        if abs(diff) > tol_pct:
            out.append({"min": t.min_quantity, "saved_cents": t.sale_price_cents,
                        "calc_cents": b.neto_per_unit_minor, "diff_pct": round(diff, 2)})
    return out


def _cheapest_evidence(f: DiagFacts, rows: Sequence[TierBreakdown] | None) -> dict[str, Any] | None:
    usable = [b for b in (rows or []) if b.usd_available and b.factura_per_unit_minor > 0]
    if len(usable) < 2:
        return None
    cheapest = min(usable, key=lambda b: b.factura_per_unit_minor)
    e = f.economics
    would_be_red = cheapest.factura_per_unit_minor > e.p_max_cents
    return {"min_quantity": cheapest.min_quantity, "price_cents": cheapest.factura_per_unit_minor,
            "red": would_be_red, "only_min_purchase": (e.our_price_cents > e.p_max_cents) and not would_be_red}


# ─── (c) certificado ─────────────────────────────────────────────


def rule_c(f: DiagFacts, th: DiagThresholds, row: TierBreakdown | None) -> RuleResult:
    code = "c"
    base = dict(code=code, title=TITLES[code], action=ACTIONS[code])
    if row is None or row.nbg_usd <= 0:
        return RuleResult(**base, status=ST_NO_DATA, summary="No hay desglose de precio para medir el certificado.")
    share = float(row.certification_usd / row.nbg_usd)
    evidence: dict[str, Any] = {"cert_usd": float(row.certification_usd), "share_pct": round(share * 100, 2)}
    cert = f.certification
    if cert is not None:
        evidence["certification"] = {"status": cert.status, "cost_usd": None if cert.cost_usd_cents is None else cert.cost_usd_cents / 100,
                                     "expires_at": cert.expires_at, "holder": cert.holder}
        if cert.max_share_pct is not None:
            evidence["channel_cap_pct"] = cert.max_share_pct
    if row.certification_usd <= 0:
        return RuleResult(**base, status=ST_NO, evidence=evidence, summary="El precio no lleva recargo de certificado.")
    p = f.economics.our_price_cents
    p_si = p * (1.0 - share)
    gets_out = f.economics.gets_out(p_si)
    if share * 100 < th.cert_share_pct and not gets_out:
        return RuleResult(**base, status=ST_NO, evidence=evidence, p_si_cents=round(p_si), summary=(
            f"El certificado pesa {_pct(share * 100)} del precio neto (USD {_num(float(row.certification_usd), 2)}): "
            "no es lo que lo deja en rojo."))
    fixes = FIX_YES if gets_out else FIX_NO
    cap = f" (el tope del canal es {_num(cert.max_share_pct, 0)} %)" if cert and cert.max_share_pct else ""
    state = f" Estado del certificado: {cert.status}." if cert and cert.status else ""
    summary = (f"El certificado suma USD {_num(float(row.certification_usd), 2)}: {_pct(share * 100)} del precio neto{cap}. "
               f"Sin ese recargo el precio sería ~{ars(p_si)}: " + ("sale del rojo." if gets_out else "sigue en rojo.")
               + state + " El recargo se cobra aunque el certificado no esté pagado.")
    return RuleResult(**base, status=ST_FIRES, subcodes=["c1" if share * 100 >= th.cert_share_pct else "c2"],
                      fixes=fixes, p_si_cents=round(p_si), estimated=False, evidence=evidence, summary=summary)


# ─── (d) valor bajo por m³ ───────────────────────────────────────


def rule_d(f: DiagFacts, th: DiagThresholds, row: TierBreakdown | None) -> RuleResult:
    code = "d"
    base = dict(code=code, title=TITLES[code], action=ACTIONS[code])
    if f.is_local_stock:
        return RuleResult(**base, status=ST_NA, summary="Abastecimiento local: no hay logística de importación.")
    if row is None or row.fob_usd <= 0 or row.chargeable_units <= 0:
        return RuleResult(**base, status=ST_NO_DATA, summary="No hay desglose con fábrica y volumen para medir el valor por m³.")
    ratio = float(row.logistics_usd / row.fob_usd)
    log_m3 = float(row.logistics_usd) / row.chargeable_units
    fob_m3 = float(row.fob_usd) / row.chargeable_units
    evidence = {"ratio": round(ratio, 3), "logistics_usd": float(row.logistics_usd), "fob_usd": float(row.fob_usd),
                "logistics_per_m3": round(log_m3, 1), "fob_per_m3": round(fob_m3, 1), "chargeable": row.chargeable_units}
    if ratio < th.logistics_ratio:
        return RuleResult(**base, status=ST_NO, evidence=evidence, summary=(
            f"La logística (USD {_num(float(row.logistics_usd), 0)}) cuesta {_num(ratio, 2)} veces la fábrica "
            f"(USD {_num(float(row.fob_usd), 0)}): compite por volumen."),
            public_summary="La logística cuesta menos que la fábrica: compite por volumen.")
    return RuleResult(**base, status=ST_FIRES, fixes=FIX_NO, evidence=evidence, subcodes=["d1"], summary=(
        f"La logística cuesta {_num(ratio, 2)} veces la fábrica (USD {_num(float(row.logistics_usd), 0)} contra "
        f"USD {_num(float(row.fob_usd), 0)}): sale USD {_num(log_m3, 0)} por m³ llevarlo y lo que se compra "
        f"vale USD {_num(fob_m3, 0)} por m³. No compite por volumen; no se arregla corrigiendo datos."),
        public_summary="La logística cuesta más que la fábrica: no compite por volumen; no se arregla corrigiendo datos.")


# ─── (e) ML por debajo del costo de fábrica ──────────────────────


def rule_e(f: DiagFacts, th: DiagThresholds, row: TierBreakdown | None) -> RuleResult:
    code = "e"
    base = dict(code=code, title=TITLES[code], action=ACTIONS[code])
    if f.is_local_stock:
        return RuleResult(**base, status=ST_NA, summary="Abastecimiento local: no hay costo de fábrica en yuanes.")
    if row is None or row.fob_usd <= 0:
        return RuleResult(**base, status=ST_NO_DATA, summary="No hay desglose con el costo de fábrica.")
    econ = f.economics
    fob_ars_cents = float(row.fob_usd / row.quantity * row.usd_to_channel) * 100.0
    resale_net = econ.ml_median_cents * (1 - econ.commission_pct / 100.0) - econ.shipping_cents
    evidence = {"fob_unit_ars_cents": round(fob_ars_cents), "ml_median_cents": econ.ml_median_cents,
                "resale_net_cents": round(resale_net)}
    if econ.ml_median_cents > fob_ars_cents:
        return RuleResult(**base, status=ST_NO, evidence=evidence, summary=(
            f"Mercado Libre ({ars(econ.ml_median_cents)}) está por encima de lo que nos cuesta la fábrica "
            f"({ars(fob_ars_cents)} por unidad)."),
            public_summary="Mercado Libre está por encima de lo que nos cuesta la fábrica.")
    # costo de fábrica objetivo (orientativo)
    p, p_max = econ.our_price_cents, econ.p_max_cents
    fixed = p * float((row.logistics_usd + row.certification_usd) / row.nbg_usd) if row.nbg_usd > 0 else None
    target_cny: float | None = None
    free_not_enough = False
    if fixed is not None and p - fixed > 0 and row.purchase_cny > 0:
        scale = (p_max - fixed) / (p - fixed)
        if scale > 0:
            target_cny = float(row.purchase_cny) * scale
        else:
            free_not_enough = True
    evidence["target_cny"] = None if target_cny is None else round(target_cny, 2)
    evidence["cny_today"] = float(row.purchase_cny)
    evidence["free_factory_not_enough"] = free_not_enough
    if free_not_enough:
        tail = " No alcanza ni con fábrica gratis."
    elif target_cny is not None:
        tail = f" Para salir del rojo la fábrica tendría que costar ~¥{_num(target_cny, 2)} por unidad (hoy ¥{_num(float(row.purchase_cny), 2)}), orientativo."
    else:
        tail = ""
    return RuleResult(**base, status=ST_FIRES, fixes=FIX_NO, subcodes=["e1"], evidence=evidence, summary=(
        f"Mercado Libre ({ars(econ.ml_median_cents)}) está por debajo de lo que nos cuesta la fábrica "
        f"({ars(fob_ars_cents)} por unidad) al cambio de hoy.{tail}"),
        public_summary="Mercado Libre está por debajo de lo que nos cuesta la fábrica: hay que buscar otro proveedor o despublicar.")


# ─── El diagnóstico completo ─────────────────────────────────────

_SUGGESTION = {
    "a": "Corregir", "b": "Corregir", "c": "Corregir",
    "d": "Mantener (si es nicho) o Despublicar", "e": "Buscar otro proveedor",
    NO_CAUSE: "Revisar las publicaciones: probablemente el match de Mercado Libre está mal",
    UNKNOWN_CAUSE: "Falta información para diagnosticar: revisar las publicaciones y volver a mirar mañana",
}


def diagnose(f: DiagFacts, th: DiagThresholds | None = None) -> Diagnosis:
    """Evalúa las cinco reglas, las guarda todas y elige la causa probable (la primera, en orden a-e)."""
    th = th or DiagThresholds()
    row = select_tier(f.breakdowns, f.tier_used)
    rules = [rule_a(f, th, row), rule_b(f, th, row), rule_c(f, th, row), rule_d(f, th, row), rule_e(f, th, row)]
    cause = next((r.code for r in rules if r.fires), None)
    if cause is None:
        # Sin causa clara solo si TODAS se pudieron evaluar; si a alguna le faltó un dato, no se descarta nada.
        cause = UNKNOWN_CAUSE if any(r.status == ST_NO_DATA for r in rules) else NO_CAUSE
    ours = [r for r in rules if r.code in ("a", "b", "c") and r.fires]
    if any(r.fixes == FIX_YES for r in ours):
        fixable = FIX_YES
    elif any(r.fixes == FIX_APPROX for r in ours):
        fixable = FIX_APPROX
    else:
        fixable = FIX_NO
    missing: list[str] = []
    if f.breakdowns is None:
        missing.append("desglose de precio")
    elif f.breakdowns and row is None and not f.is_local_stock:
        missing.append("tramo usado en el desglose")
    if f.tiers is None:
        missing.append("tramos de la variante")
    if not f.is_local_stock:
        if not f.box.has_dims:
            missing.append("caja de la variante")
        if f.neighbor_boxes is None:
            missing.append("productos parecidos")
        elif box_reference(f.neighbor_boxes, th.min_neighbors) is None:
            missing.append("referencia de caja (pocos parecidos con caja)")
    if not f.certification_read:
        missing.append("certificado")
    e = f.economics
    return Diagnosis(
        rules=rules, probable_cause=cause, fixable=fixable, fix_not_enough=fixable != FIX_YES,
        p_max_cents=e.p_max_cents, p_cents=e.our_price_cents, gap=e.gap, missing=missing,
        suggestion=_SUGGESTION[cause],
    )


def economics_from_snapshot(snap: Any, yellow_min_pct: float, ml_median_cents: int | None = None) -> Economics | None:
    """Economics a partir de un MarketPriceSnapshot (o None si no tiene precio nuestro o mediana de ML). `ml_median_cents`
    pisa la mediana del snapshot: la ficha usa la de los idénticos firmes, la que muestra."""
    median = ml_median_cents or snap.ml_median_cents
    if not snap.our_price_cents or not median:
        return None
    return Economics(
        our_price_cents=int(snap.our_price_cents), ml_median_cents=int(median),
        commission_pct=float(snap.commission_pct or 0.0), shipping_cents=int(snap.shipping_cents or 0),
        yellow_min_pct=float(yellow_min_pct),
    )


def decimal_or_none(v: Decimal | None) -> float | None:
    return None if v is None else float(v)
