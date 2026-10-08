"""Semáforo de precios: funciones puras (margen, color, mediana, nuestro precio).

Bordes que importan: exactamente en el corte, el corte cruzado, nuestro precio
por encima de ML (rojo aunque el margen diga otra cosa) y la falta de dato.
"""

from __future__ import annotations

import pytest

from app.pricing import semaforo
from app.pricing.semaforo import (
    AMARILLO,
    ROJO,
    SIN_DATO,
    VERDE,
    PricedVariant,
    PriceTier,
    color,
    estimated_margin_pct,
    median_cents,
    pick_our_price,
)

GREEN, YELLOW = 30.0, 10.0


# ─── mediana ────────────────────────────────────────────────────────────────


def test_median_odd_takes_the_middle():
    assert median_cents([300, 100, 200]) == 200


def test_median_even_averages_the_two_middle_values():
    assert median_cents([100, 200, 300, 401]) == 250  # (200 + 300) / 2


def test_median_of_nothing_is_none():
    assert median_cents([]) is None


# ─── ganancia estimada ─────────────────────────────────────────────────────


def test_margin_formula_matches_the_spec():
    # (mediana − comisión − envío − nuestro) / nuestro × 100
    # mediana 20.000, comisión 13 % = 2.600, envío 1.000, nuestro 10.000 → 64 %
    assert estimated_margin_pct(2_000_000, 1_000_000, 13.0, 100_000) == 64.0


def test_margin_can_be_negative():
    assert estimated_margin_pct(1_000_000, 1_000_000, 13.0, 0) == -13.0


@pytest.mark.parametrize("median,ours", [(None, 100), (100, None), (100, 0), (100, -5)])
def test_margin_without_both_sides_is_none(median, ours):
    assert estimated_margin_pct(median, ours, 13.0, 0) is None


def test_negative_commission_or_shipping_are_ignored():
    assert estimated_margin_pct(2_000, 1_000, -50.0, -999) == 100.0


# ─── color ─────────────────────────────────────────────────────────────────


def _color(margin, ours=1_000, median=2_000):
    return color(margin, ours, median, GREEN, YELLOW)


def test_exactly_the_green_cut_is_green():
    assert _color(30.0) == VERDE


def test_just_below_green_is_yellow():
    assert _color(29.99) == AMARILLO


def test_exactly_the_yellow_cut_is_yellow():
    assert _color(10.0) == AMARILLO


def test_just_below_yellow_is_red():
    assert _color(9.99) == ROJO


def test_negative_margin_is_red():
    assert _color(-5.0) == ROJO


def test_our_price_above_ml_is_red_even_with_a_big_margin():
    # Regla explícita de Gabriel: si cobramos más que ML, rojo, aunque un
    # margen mal calculado (comisión negativa, etc.) diga otra cosa.
    assert color(80.0, 2_001, 2_000, GREEN, YELLOW) == ROJO


def test_our_price_equal_to_ml_is_not_forced_red():
    # Igual no es "por encima": decide el margen.
    assert color(35.0, 2_000, 2_000, GREEN, YELLOW) == VERDE


@pytest.mark.parametrize("margin,ours,median", [
    (None, 1_000, 2_000),   # sin margen
    (40.0, None, 2_000),    # sin precio nuestro
    (40.0, 0, 2_000),       # precio nuestro inválido
    (40.0, 1_000, None),    # sin mediana de ML
])
def test_missing_data_is_sin_dato(margin, ours, median):
    assert color(margin, ours, median, GREEN, YELLOW) == SIN_DATO


def test_crossed_thresholds_never_downgrade_a_green():
    # amarillo ≥ verde por error de carga: un margen que supera el verde es verde.
    assert color(35.0, 1_000, 2_000, green_min_pct=30.0, yellow_min_pct=50.0) == VERDE
    assert color(20.0, 1_000, 2_000, green_min_pct=30.0, yellow_min_pct=50.0) == ROJO


def test_changing_the_cut_recolors():
    # Lo que hace el job cuando se cambia pm_green_min_pct en el dashboard.
    assert color(35.0, 1_000, 2_000, 30.0, 10.0) == VERDE
    assert color(35.0, 1_000, 2_000, 40.0, 10.0) == AMARILLO


# ─── nuestro precio: tramo y variante ──────────────────────────────────────


def _variant(vid="v1", with_tax=12_100, tiers=(), currency="ARS"):
    return PricedVariant(id=vid, name="", sku="", price_with_tax_cents=with_tax,
                         currency=currency, tiers=tuple(tiers))


def _tier(pos, min_qty, sale, enabled=True):
    return PriceTier(position=pos, min_quantity=min_qty, max_quantity=None,
                     sale_price_cents=sale, enabled=enabled)


def test_policy_cheapest_uses_price_with_tax():
    v = _variant(tiers=[_tier(0, 1, 15_000), _tier(1, 50, 10_000)])
    got = pick_our_price([v], semaforo.TIER_POLICY_CHEAPEST)
    assert (got.price_cents, got.variant_id, got.tier_used) == (12_100, "v1", "priceWithTax")


def test_policy_min_tier_applies_the_variant_vat_factor():
    # priceWithTax 12.100 sobre el tramo más barato 10.000 → IVA 1,21.
    # Tramo mínimo (min_qty 1) 15.000 sin IVA → 18.150 con IVA.
    v = _variant(tiers=[_tier(1, 50, 10_000), _tier(0, 1, 15_000)])
    got = pick_our_price([v], semaforo.TIER_POLICY_MIN)
    assert got.price_cents == 18_150
    assert got.tier_used == "tier:min_qty=1"


def test_policy_min_ignores_disabled_and_priceless_tiers():
    v = _variant(tiers=[_tier(0, 1, 15_000, enabled=False), _tier(1, 10, None),
                        _tier(2, 20, 12_000), _tier(3, 50, 10_000)])
    got = pick_our_price([v], semaforo.TIER_POLICY_MIN)
    assert got.price_cents == 14_520  # 12.000 × 1,21
    assert got.tier_used == "tier:min_qty=20"


def test_policy_min_without_tiers_falls_back_to_price_with_tax():
    got = pick_our_price([_variant(tiers=[])], semaforo.TIER_POLICY_MIN)
    assert (got.price_cents, got.tier_used) == (12_100, "priceWithTax")


def test_implausible_vat_factor_falls_back_and_says_so():
    # Tramo de 1.000 contra priceWithTax 12.100 → factor 12,1: no es IVA.
    v = _variant(tiers=[_tier(0, 1, 1_000)])
    got = pick_our_price([v], semaforo.TIER_POLICY_MIN)
    assert (got.price_cents, got.tier_used) == (12_100, "priceWithTax(fallback)")


def test_first_variant_with_price_represents_the_product():
    got = pick_our_price([_variant("v0", with_tax=0), _variant("v1", with_tax=500)],
                         semaforo.TIER_POLICY_CHEAPEST)
    assert got.variant_id == "v1"


@pytest.mark.parametrize("variants", [[], [_variant(with_tax=None)], [_variant(with_tax=0)]])
def test_no_priced_variant_means_no_price(variants):
    assert pick_our_price(variants, semaforo.TIER_POLICY_MIN) is None
