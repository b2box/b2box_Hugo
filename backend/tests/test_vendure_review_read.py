"""Lecturas de Vendure para el diagnóstico de la bandeja (PR1): solo queries, sin N+1.

Vendure es un doble a nivel HTTP (`tests/review_fixtures.py`): el cliente real arma sus requests y
cualquier `mutation` queda registrada y contestada con un 500.
"""

from __future__ import annotations

import os
import re

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app.pricing.price_breakdown import BreakdownError  # noqa: E402
from app.vendure.client import VendureClient  # noqa: E402
from tests.review_fixtures import FakeReviewVendure, engine_variant, install  # noqa: E402


@pytest.fixture
def vendure(monkeypatch):
    fake = FakeReviewVendure()
    install(monkeypatch, fake)
    return fake


def _product(pid: str, *, n_variants: int = 1, enabled: bool = True) -> dict:
    return {
        "id": pid, "name": f"Producto {pid}", "slug": f"p{pid}", "description": "", "enabled": enabled,
        "updatedAt": "2026-10-01T12:00:00.000Z",
        "customFields": {"b2boxProductCode": f"BX{pid}"},
        "featuredAsset": {"preview": f"https://cdn/{pid}-p.jpg", "source": f"https://cdn/{pid}.jpg"},
        "variantList": {"totalItems": n_variants, "items": [{
            "id": f"v{pid}{i}", "name": "", "sku": f"S{pid}", "priceWithTax": 12100, "currencyCode": "ARS",
            "customFields": {"boxLength": 60, "boxWidth": 40, "boxHeight": 40, "boxWeight": 12,
                             "sourcingMode": "import", "minQuantityOverride": 10},
            "bulkPriceTiers": [
                {"position": 1, "enabled": True, "minQuantity": 50, "maxQuantity": None, "salePrice": 10000, "price": 800},
                {"position": 0, "enabled": True, "minQuantity": 1, "maxQuantity": 49, "salePrice": 15000, "price": 1000},
            ],
        } for i in range(n_variants)]},
    }


# ─── la consulta nocturna: mismas páginas, una columna más ────────────────


async def test_the_nightly_listing_reads_tier_cost_and_variant_mode_in_the_same_pages(vendure):
    vendure.products = [_product(str(i)) for i in range(250)]
    products = await VendureClient().fetch_all_products_priced()
    assert len(products) == 250
    # 250 productos / 100 por página = 3 requests, exactamente como antes de la bandeja
    assert vendure.ops() == ["Products"] * 3
    query = vendure.requests[0]["query"]
    assert re.search(r"\bprice\b", query), "falta el costo por tramo (price)"
    assert "sourcingMode" in query and "minQuantityOverride" in query
    assert vendure.mutations == []
    [v] = products[0].priced_variants
    assert v.sourcing_mode == "import" and v.min_quantity_override == 10
    assert [(t.min_quantity, t.cost_cny_minor, t.sale_price_cents) for t in v.tiers] == [(1, 1000, 15000), (50, 800, 10000)]


async def test_a_schema_without_the_new_fields_keeps_giving_prices(vendure):
    vendure.products = [_product("1")]
    vendure.unknown_fields = {"sourcingMode"}
    products = await VendureClient().fetch_all_products_priced()
    assert products and products[0].priced_variants[0].tiers
    assert VendureClient._review_supported is False and VendureClient._dims_supported is True
    assert "sourcingMode" not in vendure.requests[-1]["query"]
    assert "boxLength" in vendure.requests[-1]["query"]            # las medidas siguen


async def test_a_schema_without_the_tier_price_also_drops_only_that_part(vendure):
    vendure.products = [_product("1")]
    vendure.unknown_fields = {"price"}
    await VendureClient().fetch_all_products_priced()
    assert VendureClient._review_supported is False and VendureClient._dims_supported is True


async def test_an_unrelated_error_is_not_swallowed(vendure):
    vendure.products = [_product("1")]
    vendure.errors["Products"] = "Variable $skip of required type was not provided"
    with pytest.raises(Exception):  # noqa: B017
        await VendureClient().fetch_all_products_priced()
    assert VendureClient._review_supported is True


# ─── las lecturas puntuales ───────────────────────────────────────────────


async def test_tier_breakdowns_are_one_query_per_variant_and_get_validated(vendure):
    v = engine_variant(0)
    vendure.breakdowns[v["variant"]] = v["breakdowns"]
    rows = await VendureClient().get_tier_breakdowns(v["variant"])
    assert [r.min_quantity for r in rows] == [25, 50, 100]
    assert vendure.ops() == ["TierBreakdowns"]
    q = vendure.requests[0]["query"]
    assert "bulkPriceTierBreakdowns(productVariantId: $id)" in q and vendure.requests[0]["variables"] == {"id": "101"}
    assert vendure.mutations == []


async def test_breakdowns_that_do_not_add_up_fail_loudly(vendure):
    v = engine_variant(1)
    bad = [dict(v["breakdowns"][0], fobUsd={"exact": "999.0000"})]
    vendure.breakdowns[v["variant"]] = bad
    with pytest.raises(BreakdownError):
        await VendureClient().get_tier_breakdowns(v["variant"])


async def test_a_variant_without_enabled_tiers_has_no_breakdowns(vendure):
    assert await VendureClient().get_tier_breakdowns("999") == []


async def test_get_priced_variant_maps_like_the_nightly_listing(vendure):
    raw = _product("7")["variantList"]["items"][0]
    vendure.variants["v70"] = raw
    var = await VendureClient().get_priced_variant("v70")
    assert var.id == "v70" and var.sourcing_mode == "import" and var.tiers[0].cost_cny_minor == 1000
    assert var.specs["box_weight"] == 12.0
    assert await VendureClient().get_priced_variant("nope") is None


async def test_certification_and_no_certification(vendure):
    vendure.certifications["10"] = {"status": "required_uncertified", "holder": "b2box", "costUsd": 18000,
                                    "issuedAt": None, "expiresAt": "2027-01-01T00:00:00.000Z"}
    cert = await VendureClient().get_certification("10", "2")
    assert (cert.status, cert.holder, cert.cost_usd_cents, cert.expires_at) == (
        "required_uncertified", "b2box", 18000, "2027-01-01T00:00:00.000Z")
    assert await VendureClient().get_certification("11", "2") is None        # sin fila: no necesita certificado
    assert vendure.requests[0]["variables"] == {"productId": "10", "channelId": "2"}


async def test_channel_pricing_reads_min_purchase_and_certificate_cap(vendure):
    info = await VendureClient().get_channel_pricing()
    assert (info.channel_id, info.minimum_purchase_cny, info.certificate_max_share_pct, info.container_cbm) == (
        "2", 500, 20.0, 30.0)
    assert vendure.ops() == ["ActiveChannelPricing", "FiscalProfile"]


async def test_channel_pricing_survives_a_missing_fiscal_profile(vendure):
    vendure.channel = {"id": "2", "customFields": {"minimumPurchaseCny": None}}
    vendure.http_fail.add("FiscalProfile")
    info = await VendureClient().get_channel_pricing()
    assert info.minimum_purchase_cny is None and info.certificate_max_share_pct is None


async def test_the_read_helpers_refuse_anything_that_is_not_a_query():
    with pytest.raises(RuntimeError):
        VendureClient._read_query("mutation X { updateProduct(input: {id: 1, enabled: false}) { id } }")


async def test_every_diagnosis_read_is_a_query_never_a_mutation(vendure):
    v = engine_variant(2)
    vendure.breakdowns[v["variant"]] = v["breakdowns"]
    vendure.variants[v["variant"]] = {"id": v["variant"], "priceWithTax": v["priceWithTax"], "currencyCode": "ARS",
                                      "customFields": v["customFields"], "bulkPriceTiers": v["tiers"]}
    c = VendureClient()
    await c.get_channel_pricing()
    await c.get_priced_variant(v["variant"])
    await c.get_tier_breakdowns(v["variant"])
    await c.get_certification("5", "2")
    assert vendure.mutations == []
    assert all(r["kind"] == "query" for r in vendure.requests) and len(vendure.requests) == 5
