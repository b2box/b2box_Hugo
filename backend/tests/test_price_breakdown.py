"""Desglose del precio por tramo (PR1 de la bandeja): el parser valida y las 10 partes suman la factura.

Los fixtures (`golden/breakdowns_engine_fdd7579df.json`) salieron de correr el engine REAL de
Vendure (describeImportPricing, B2BOX-Cloud/b2box fdd7579df) con costos, cajas y tarifas de
ejemplo: no son una captura de producción (ver su campo `_meta`).
"""

from __future__ import annotations

import copy
import json
import os
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app.pricing.price_breakdown import (  # noqa: E402
    SUM_TOLERANCE_USD,
    BreakdownError,
    parse_tier_breakdown,
    parse_tier_breakdowns,
)

FIXTURE = json.loads((Path(__file__).parent / "golden" / "breakdowns_engine_fdd7579df.json").read_text())
ALL_TIERS = [(v["name"], b) for v in FIXTURE["variants"] for b in v["breakdowns"]]


def test_the_fixture_has_three_products_with_three_tiers_each():
    assert len(FIXTURE["variants"]) == 3
    assert [len(v["breakdowns"]) for v in FIXTURE["variants"]] == [3, 3, 3]
    assert "engine REAL" in FIXTURE["_meta"]["fuente"]


@pytest.mark.parametrize("name,raw", ALL_TIERS, ids=[f"{n[:12]}-{b['minQuantity']}" for n, b in ALL_TIERS])
def test_the_ten_parts_add_up_to_the_invoice_in_9_of_9_tiers(name, raw):
    bd = parse_tier_breakdown(raw)
    assert bd.usd_available
    assert abs(bd.parts_sum_usd() - bd.factura_usd) <= SUM_TOLERANCE_USD
    comps = bd.components()
    assert [c.key for c in comps] == [
        "fabrica", "flete", "aranceles", "servicios", "logistica_china", "fee", "costo_financiero",
        "certificado", "pasarela", "iva_venta"]
    assert abs(sum(c.usd for c in comps) - bd.factura_usd) <= SUM_TOLERANCE_USD
    assert abs(sum(c.pct_of_invoice for c in comps) - 100.0) < 0.1


def test_parts_follow_the_documented_formulas():
    raw = FIXTURE["variants"][2]["breakdowns"][0]            # el cargador con certificado
    bd = parse_tier_breakdown(raw)
    parts = {k: v for k, _l, v in bd.parts()}
    ex = lambda key: Decimal(raw[key]["exact"])               # noqa: E731
    assert parts["fabrica"] == ex("fobUsd")
    assert parts["flete"] == ex("fleteUsd") + ex("seguroUsd")
    assert parts["aranceles"] == ex("arancelesUsd")
    assert parts["servicios"] == ex("serviciosUsd") - ex("chinaLogisticsUsd")
    assert parts["logistica_china"] == ex("chinaLogisticsUsd")
    assert parts["certificado"] == Decimal("180.0000")
    assert parts["iva_venta"] == ex("facturaUsd") - ex("netoUsd")
    assert bd.nbg_usd == ex("subtopUsd") + ex("b2boxFeeUsd") + ex("finCostUsd") + ex("certificationUsd")
    assert bd.logistics_usd == ex("fleteUsd") + ex("serviciosUsd")


def test_the_services_detail_travels_with_the_services_part():
    bd = parse_tier_breakdown(FIXTURE["variants"][0]["breakdowns"][0])
    services = next(c for c in bd.components() if c.key == "servicios")
    codes = [code for code, _ in services.detail]
    assert "deposito_fiscal" in codes and "terminal" in codes
    assert sum(v for _c, v in services.detail) + bd.china_logistics_usd == bd.servicios_usd


def test_money_comes_from_the_exact_string_and_units_from_the_channel():
    raw = FIXTURE["variants"][0]["breakdowns"][2]
    bd = parse_tier_breakdown(raw)
    assert bd.factura_per_unit_minor == raw["facturaChannelPerUnit"]["minor"]
    assert bd.channel_currency == "ARS"
    assert bd.quantity == 100 and bd.min_quantity == 100 and bd.max_quantity is None
    ars = next(c for c in bd.components() if c.key == "fabrica").ars_per_unit
    assert ars == pytest.approx(float(bd.fob_usd) / 100 * float(bd.usd_to_channel))


def test_if_the_parts_do_not_add_up_the_parser_fails_loudly_and_invents_nothing():
    raw = copy.deepcopy(FIXTURE["variants"][1]["breakdowns"][0])
    raw["b2boxFeeUsd"]["exact"] = str(Decimal(raw["b2boxFeeUsd"]["exact"]) + Decimal("0.03"))
    with pytest.raises(BreakdownError) as exc:
        parse_tier_breakdown(raw)
    assert "suman" in str(exc.value) and "diferencia" in str(exc.value)


def test_a_one_cent_rounding_difference_is_tolerated():
    raw = copy.deepcopy(FIXTURE["variants"][1]["breakdowns"][0])
    raw["paymentGatewayFeeUsd"]["exact"] = str(Decimal(raw["paymentGatewayFeeUsd"]["exact"]) + Decimal("0.01"))
    assert parse_tier_breakdown(raw).usd_available


@pytest.mark.parametrize("path", [("fobUsd",), ("inputs", "quantity"), ("netoChannelPerUnit",),
                                  ("facturaUsd", "exact"), ("inputs", "usdToChannelRate")])
def test_a_missing_field_is_an_explicit_error(path):
    raw = copy.deepcopy(FIXTURE["variants"][0]["breakdowns"][0])
    node = raw
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    with pytest.raises(BreakdownError):
        parse_tier_breakdown(raw)


@pytest.mark.parametrize("bad", ["abc", "NaN", "Infinity", None])
def test_a_non_numeric_amount_is_an_explicit_error(bad):
    raw = copy.deepcopy(FIXTURE["variants"][0]["breakdowns"][0])
    raw["fleteUsd"]["exact"] = bad
    with pytest.raises(BreakdownError):
        parse_tier_breakdown(raw)


def test_a_tier_without_price_comes_back_as_zeros_and_has_nothing_to_show():
    raw = copy.deepcopy(FIXTURE["variants"][0]["breakdowns"][0])
    for key, node in list(raw.items()):
        if isinstance(node, dict) and "exact" in node and key not in ("netoChannelPerUnit", "facturaChannelPerUnit"):
            node["exact"] = "0.0000"
    raw["servicios"] = []
    raw["netoChannelPerUnit"] = {"exact": "0.00", "minor": 0, "currencyCode": "ARS"}
    raw["facturaChannelPerUnit"] = {"exact": "0.00", "minor": 0, "currencyCode": "ARS"}
    bd = parse_tier_breakdown(raw)
    assert bd.usd_available is False and bd.components() == [] and bd.factura_per_unit_minor == 0


def test_the_list_parser_handles_nothing_and_rejects_non_lists():
    assert parse_tier_breakdowns(None) == [] and parse_tier_breakdowns([]) == []
    with pytest.raises(BreakdownError):
        parse_tier_breakdowns({"a": 1})
    assert len(parse_tier_breakdowns(FIXTURE["variants"][0]["breakdowns"])) == 3


def test_to_dict_is_json_ready_and_small():
    d = parse_tier_breakdown(FIXTURE["variants"][2]["breakdowns"][1]).to_dict()
    text = json.dumps(d)
    assert len(text) < 4_000 and d["components"][7]["key"] == "certificado"
    assert d["components"][7]["usd"] == 180.0
