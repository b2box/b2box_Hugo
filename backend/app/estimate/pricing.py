"""Cuentas del estimado, sin red: precio en yuanes, escenarios y rango final.

Reglas del diseño (2.2, 2.5) con las decisiones del 20-oct:
  * precio por candidato de Apify × k (primer tramo del proveedor, medido);
  * anti-carnada: fuera los < 0,5× o > 2× la mediana;
  * escenarios low/mid/high = P25/mediana/P75 (con 2 o 3 precios: mín, mediana,
    máx) y la caja P25/P50/P75 de los vecinos;
  * rango final por cantidad = mín/máx de `invoiceChannelPerUnit` entre los
    escenarios (con fee por tramos, low/high de Vendure no garantizan orden),
    abierto ± m, redondeado: piso para abajo y techo para arriba, a $100 debajo
    de $10.000 y a $500 arriba.

Montos de Vendure: en unidades menores de la moneda del canal (centavos), como
todo precio de Vendure.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

# Dispersión máxima (máx/mín) de los precios buenos para mostrar rango.
MAX_PRICE_DISPERSION = 2.5
# Anti-carnada: alrededor de la mediana.
_BAIT_LOW, _BAIT_HIGH = 0.5, 2.0
# Si techo/piso queda por debajo de esto, se abre a ± _MIN_HALF_WIDTH del medio.
_MIN_RATIO = 1.15
_MIN_HALF_WIDTH = 0.08
# Redondeo, en centavos: $10 debajo de $1.000 (con $100 un spinner de $800
# mostraba el mismo $600–$1.000 en todos los tramos), $100 hasta $10.000 y $500 arriba.
_STEP_TINY = 10_00
_TINY_BELOW = 1_000_00
_STEP_SMALL = 100_00
_STEP_BIG = 500_00
_BIG_FROM = 10_000_00

LOW, MID, HIGH = "low", "mid", "high"
BOX_QUANTILES = {LOW: 0.25, MID: 0.5, HIGH: 0.75}


@dataclass(slots=True, frozen=True)
class Scenarios:
    low: float
    mid: float
    high: float

    def as_dict(self) -> dict[str, float]:
        return {LOW: self.low, MID: self.mid, HIGH: self.high}


@dataclass(slots=True, frozen=True)
class Tier:
    quantity: int
    low_cents: int
    high_cents: int


def remove_bait(prices: Sequence[float]) -> list[float]:
    """Saca los precios < 0,5× o > 2× la mediana (carnada o packs)."""
    clean = [p for p in prices if p > 0 and math.isfinite(p)]
    if not clean:
        return []
    median = statistics.median(clean)
    return [p for p in clean if _BAIT_LOW * median <= p <= _BAIT_HIGH * median]


def dispersion(prices: Sequence[float]) -> float:
    return max(prices) / min(prices) if prices and min(prices) > 0 else math.inf


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """Cuantil con interpolación lineal (el mismo de numpy por defecto)."""
    pos = (len(sorted_values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def scenarios_from_prices(prices_cny: Sequence[float], k: float) -> Scenarios:
    """low/mid/high en yuanes por unidad, ya multiplicados por k."""
    values = sorted(prices_cny)
    if not values:
        raise ValueError("sin precios")
    if len(values) <= 3:
        low, mid, high = values[0], statistics.median(values), values[-1]
    else:
        low, mid, high = (_quantile(values, 0.25), statistics.median(values),
                          _quantile(values, 0.75))
    return Scenarios(round(low * k, 4), round(mid * k, 4), round(high * k, 4))


def ladder_price(ladder: Sequence[tuple[int, float]], quantity: int) -> float:
    """Precio de la escalera de 1688 para `quantity` (el tramo con mayor mínimo
    que no lo supera; debajo del MOQ, el primer tramo)."""
    ordered = sorted(ladder)
    price = ordered[0][1]
    for min_qty, tier_price in ordered:
        if quantity >= min_qty:
            price = tier_price
    return price


def scenarios_from_ladder(ladder: Sequence[tuple[int, float]],
                          quantities: Sequence[int]) -> Scenarios:
    """Escenarios para un link de 1688 leído por id: el precio real del tramo,
    sin k. low = precio a la cantidad más grande (más barato), high = a la más
    chica, mid = a la del medio."""
    qs = sorted(quantities)
    prices = [ladder_price(ladder, q) for q in qs]
    return Scenarios(round(min(prices), 4), round(prices[len(prices) // 2], 4),
                     round(max(prices), 4))


def _step(cents: float) -> int:
    if cents >= _BIG_FROM:
        return _STEP_BIG
    return _STEP_SMALL if cents >= _TINY_BELOW else _STEP_TINY


def round_down(cents: float) -> int:
    step = _step(cents)
    return max(step, int(math.floor(cents / step)) * step)


def round_up(cents: float) -> int:
    step = _step(cents)
    return int(math.ceil(cents / step)) * step


def tier_range(prices_by_scenario: dict[str, int], margin_pct: float,
               max_ratio: float) -> tuple[int, int] | None:
    """(piso, techo) en centavos para UNA cantidad, o None si el rango sale
    demasiado ancho para mostrarse. `prices_by_scenario`: invoiceChannelPerUnit
    de cada escenario a esa cantidad."""
    values = [v for v in prices_by_scenario.values() if v and v > 0]
    if not values:
        return None
    m = max(0.0, margin_pct) / 100.0
    low, high = min(values) * (1 - m), max(values) * (1 + m)
    if low <= 0:
        return None
    if high / low < _MIN_RATIO:
        mid = prices_by_scenario.get(MID) or (min(values) + max(values)) / 2
        low, high = min(low, mid * (1 - _MIN_HALF_WIDTH)), max(high, mid * (1 + _MIN_HALF_WIDTH))
    if high / low > max_ratio:
        return None
    return round_down(low), round_up(high)


def format_ars(cents: int) -> str:
    """$ 4.200 (sin decimales, miles con punto)."""
    return "$ " + f"{int(round(cents / 100)):,}".replace(",", ".")


def estimate_message(tiers: Sequence[Tier]) -> str:
    """Texto para el cliente. Siempre dice "estimado", "con IVA" y la cantidad:
    no hay forma de mostrar un rango sin esas palabras (ER-05)."""
    parts = [
        f"{t.quantity} unidades: entre {format_ars(t.low_cents)} y {format_ars(t.high_cents)}"
        for t in tiers
    ]
    return (
        "Precio estimado por unidad, final con IVA, puesto en Argentina. "
        + "; ".join(parts)
        + ". Es una referencia: el precio real lo confirmamos al cotizar."
    )
