"""Semáforo de precios: funciones PURAS. Sin red, sin DB, sin settings.

Qué le decimos al cliente con cada color (sección 3 del pedido de Gabriel):

    verde     → revendiendo en ML gana ≥ pm_green_min_pct (30 %)
    amarillo  → gana entre pm_yellow_min_pct (10 %) y el verde
    rojo      → gana menos de 10 %, o nuestro precio ya está por encima de ML
    sin_dato  → no encontramos el producto en ML. No es malo: puede ser algo
                que todavía no llegó al mercado.

Ganancia estimada del revendedor, en % sobre lo que nos paga:

    (mediana_ML − comisión_ML − envío_ML − nuestro_precio_con_IVA)
    ─────────────────────────────────────────────────────────────── × 100
                        nuestro_precio_con_IVA

La comisión es un % de la mediana (`pm_ml_commission_pct`); el envío es un
monto fijo en centavos (`pm_ml_shipping_cents`). Los dos los fija Gabriel.

Los umbrales entran como parámetros: el job los lee de runtime (dashboard) en
cada corrida, así cambiar el 30 % recolorea la próxima pasada sin redeploy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

VERDE = "verde"
AMARILLO = "amarillo"
ROJO = "rojo"
SIN_DATO = "sin_dato"
COLORS = (VERDE, AMARILLO, ROJO, SIN_DATO)

# Factor IVA máximo creíble al derivar "con IVA" de un tramo: si priceWithTax /
# salePrice se sale de este rango, el tramo no es comparable con el precio con
# impuestos y es más honesto usar priceWithTax directo que inventar un número.
_VAT_FACTOR_MIN = 1.0
_VAT_FACTOR_MAX = 1.6


def median_cents(values: Sequence[int]) -> int | None:
    """Mediana entera de una lista de centavos. None si está vacía.

    Con cantidad par promedia los dos del medio y redondea: la mediana es el
    precio "típico" de la ficha, no el del vendedor más barato (ese es `min`).
    """
    clean = sorted(int(v) for v in values if v is not None)
    if not clean:
        return None
    n = len(clean)
    mid = n // 2
    if n % 2:
        return clean[mid]
    return int(round((clean[mid - 1] + clean[mid]) / 2))


def estimated_margin_pct(
    ml_median_cents: int | None,
    our_price_cents: int | None,
    commission_pct: float,
    shipping_cents: int,
    digits: int | None = None,
) -> float | None:
    """Ganancia estimada del revendedor en %, o None si falta un lado.

    None también cuando nuestro precio es 0 o negativo: dividir por eso no
    significa nada y arriba se trata como `skipped`.

    Por default SIN redondear: es lo que hay que pasarle a `color()`
    (redondeado antes, un 29,996 % daba 30,0 y salía verde). `digits` es para
    guardar o mostrar.
    """
    if ml_median_cents is None or our_price_cents is None or our_price_cents <= 0:
        return None
    commission = ml_median_cents * (max(0.0, float(commission_pct)) / 100.0)
    net = ml_median_cents - commission - max(0, int(shipping_cents)) - our_price_cents
    margin = net / our_price_cents * 100.0
    return margin if digits is None else round(margin, digits)


def color(
    margin_pct: float | None,
    our_price_cents: int | None,
    ml_median_cents: int | None,
    green_min_pct: float,
    yellow_min_pct: float,
) -> str:
    """Asigna el color. Reglas en orden:

    1. sin mediana de ML, o sin precio propio válido → sin_dato
    2. nuestro precio por encima de la mediana de ML → rojo (aunque el margen
       diga otra cosa: es la regla explícita de Gabriel)
    3. margen ≥ verde → verde; ≥ amarillo → amarillo; si no → rojo

    Si alguien configura amarillo ≥ verde, gana el verde: un margen que supera
    el corte alto no puede quedar en amarillo por un setting cruzado.
    """
    if ml_median_cents is None or our_price_cents is None or our_price_cents <= 0:
        return SIN_DATO
    if margin_pct is None:
        return SIN_DATO
    if our_price_cents > ml_median_cents:
        return ROJO
    if margin_pct >= green_min_pct:
        return VERDE
    if margin_pct >= yellow_min_pct:
        return AMARILLO
    return ROJO


# ─── Nuestro precio: qué tramo y qué variante ──────────────────────


@dataclass(slots=True, frozen=True)
class PriceTier:
    position: int
    min_quantity: int | None
    max_quantity: int | None
    sale_price_cents: int | None   # neto, SIN IVA (Vendure BulkPriceTier.salePrice)
    enabled: bool = True


@dataclass(slots=True, frozen=True)
class PricedVariant:
    id: str
    name: str
    sku: str
    price_with_tax_cents: int | None  # Vendure priceWithTax = IVA del tramo más barato
    currency: str | None
    tiers: tuple[PriceTier, ...] = ()


@dataclass(slots=True, frozen=True)
class OurPrice:
    price_cents: int
    variant_id: str
    tier_used: str
    currency: str | None = None


TIER_POLICY_MIN = 0       # tramo mínimo: el más caro por unidad (compra chica)
TIER_POLICY_CHEAPEST = 1  # tramo más barato: lo que ya leía Hugo (priceWithTax)


def _usable_tiers(variant: PricedVariant) -> list[PriceTier]:
    return [
        t for t in variant.tiers
        if t.enabled and t.sale_price_cents is not None and t.sale_price_cents > 0
    ]


def _representative_variant(variants: Sequence[PricedVariant]) -> PricedVariant | None:
    """La primera variante con precio > 0. DECISIÓN PENDIENTE DE GABRIEL: con
    varias variantes (colores, tamaños) esta es la más simple de explicar;
    las alternativas son la más barata o la más vendida."""
    for v in variants:
        if v.price_with_tax_cents and v.price_with_tax_cents > 0:
            return v
    return None


def pick_our_price(variants: Sequence[PricedVariant], tier_policy: int) -> OurPrice | None:
    """Nuestro precio por unidad CON IVA según `pm_tier_policy`.

    * policy 1 (tramo más barato): `priceWithTax` tal cual — Vendure ya lo
      calcula sobre el tramo más barato.
    * policy 0 (tramo mínimo): el tramo de menor `minQuantity` trae `salePrice`
      SIN IVA. El factor de IVA se deriva de la propia variante:
      priceWithTax / salePrice_del_tramo_más_barato. Si no hay tramos o el
      factor no es creíble, cae a priceWithTax y lo dice en `tier_used`.

    None si el producto no tiene ninguna variante con precio: arriba es `skipped`.
    """
    variant = _representative_variant(variants)
    if variant is None or not variant.price_with_tax_cents:
        return None
    with_tax = int(variant.price_with_tax_cents)

    if int(tier_policy) == TIER_POLICY_CHEAPEST:
        return OurPrice(with_tax, variant.id, "priceWithTax", variant.currency)

    tiers = _usable_tiers(variant)
    if not tiers:
        return OurPrice(with_tax, variant.id, "priceWithTax", variant.currency)

    cheapest = min(t.sale_price_cents for t in tiers)  # type: ignore[type-var]
    vat_factor = with_tax / cheapest if cheapest else 0.0
    if not (_VAT_FACTOR_MIN <= vat_factor <= _VAT_FACTOR_MAX):
        return OurPrice(with_tax, variant.id, "priceWithTax(fallback)", variant.currency)

    first = min(tiers, key=lambda t: (t.min_quantity if t.min_quantity is not None else 0, t.position))
    price = int(round(first.sale_price_cents * vat_factor))  # type: ignore[operator]
    label = f"tier:min_qty={first.min_quantity if first.min_quantity is not None else first.position}"
    return OurPrice(price, variant.id, label, variant.currency)
