"""Armado del diagnóstico de UN producto (PR1): lecturas de Vendure con tope duro, sin escrituras y sin inventar.

Vendure es el doble HTTP de `review_fixtures`; las cajas de los parecidos salen de los snapshots.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app.db.models import MarketPriceSnapshot  # noqa: E402
from app.pricing import diagnosis_source as ds  # noqa: E402
from app.pricing.price_diagnosis import Box  # noqa: E402
from app.vendure.client import VendureClient  # noqa: E402
from tests.review_fixtures import FakeReviewVendure, engine_variant, install  # noqa: E402


@pytest.fixture
def vendure(monkeypatch):
    fake = FakeReviewVendure()
    install(monkeypatch, fake)
    for i in range(3):
        v = engine_variant(i)
        fake.breakdowns[v["variant"]] = v["breakdowns"]
        fake.variants[v["variant"]] = {
            "id": v["variant"], "name": "", "sku": "", "priceWithTax": v["priceWithTax"], "currencyCode": "ARS",
            "customFields": v["customFields"], "bulkPriceTiers": v["tiers"]}
    fake.certifications["900"] = {"status": "required_uncertified", "holder": "b2box", "costUsd": 18000,
                                  "issuedAt": None, "expiresAt": None}
    return fake


def snap(i: int = 0, *, median_ars: float = 70_000, tier: int = 0, product_id: str = "900", **kw) -> MarketPriceSnapshot:
    v = engine_variant(i)
    b = v["breakdowns"][tier]
    specs = {"box_length": v["customFields"]["boxLength"], "box_width": v["customFields"]["boxWidth"],
             "box_height": v["customFields"]["boxHeight"], "box_weight": v["customFields"]["boxWeight"]}
    base = dict(run_id=1, product_id=product_id, variant_id=v["variant"], ml_status="ok", color="rojo",
                our_price_cents=b["facturaChannelPerUnit"]["minor"], ml_median_cents=round(median_ars * 100),
                commission_pct=13.0, shipping_cents=0, tier_used=f"tier:min_qty={b['minQuantity']}",
                our_specs=json.dumps(specs))
    base.update(kw)
    return MarketPriceSnapshot(**base)


async def test_a_full_diagnosis_makes_two_reads_when_the_nightly_variant_is_at_hand(vendure):
    client = VendureClient()
    ch = await client.get_channel_pricing()
    assert ch.channel_id == "2"
    variant = await client.get_priced_variant("101")
    vendure.requests.clear()
    src = ds.BudgetedSource(client)
    res = await ds.diagnose_product(snap(), src, yellow_min_pct=10.0, channel=ch, variant=variant,
                                    neighbor_boxes_=[Box(24 + i % 3, 18 + i % 2, 14 + i % 3, 0.8 + 0.05 * i) for i in range(8)])
    assert vendure.ops() == ["TierBreakdowns", "ProductCertification"]
    assert src.calls == 2 and res.errors == []
    d = res.diagnosis
    assert d.probable_cause == "a" and d.rule("d").fires
    assert d.missing == []
    assert res.breakdown["quantity"] == 25 and len(res.breakdown["components"]) == 10
    assert len(res.tiers_breakdown) == 3 and res.tiers_breakdown[0]["min_quantity"] == 25
    assert vendure.mutations == []


async def test_without_the_nightly_variant_it_reads_it_once(vendure):
    client = VendureClient()
    src = ds.BudgetedSource(client)
    res = await ds.diagnose_product(snap(1), src, yellow_min_pct=10.0, channel=None)
    assert vendure.ops() == ["PricedVariant", "TierBreakdowns"]
    assert res.diagnosis.rule("b").status != "sin_dato"
    assert "certificado" in res.diagnosis.missing       # sin canal no se pudo consultar el certificado


async def test_the_hard_cap_stops_the_reads_and_the_rules_say_no_data(vendure):
    client = VendureClient()
    src = ds.BudgetedSource(client, max_calls=1)
    res = await ds.diagnose_product(snap(0), src, yellow_min_pct=10.0,
                                    channel=await client.get_channel_pricing())
    assert src.calls == 1 and vendure.count("TierBreakdowns") == 0       # el tope se cuenta ANTES de pedir
    assert res.diagnosis.rule("c").status == "sin_dato" and res.diagnosis.rule("e").status == "sin_dato"
    assert any("ReadBudgetExhausted" in e for e in res.errors)
    assert not res.complete


async def test_the_cap_counts_every_call_and_never_goes_over(vendure):
    client = VendureClient()
    src = ds.BudgetedSource(client, max_calls=150)
    ch = await src.get_channel_pricing()
    assert src.calls == 2
    variant = await client.get_priced_variant("101")
    for _ in range(100):
        await ds.diagnose_product(snap(), src, yellow_min_pct=10.0, channel=ch, variant=variant)
    assert src.calls == 150 and src.exhausted
    with pytest.raises(ds.ReadBudgetExhausted):
        await src.get_tier_breakdowns("101")
    assert src.calls == 150
    assert vendure.count("TierBreakdowns") + vendure.count("ProductCertification") == 148


async def test_breakdowns_that_do_not_add_up_are_reported_and_the_rest_still_runs(vendure):
    bad = [dict(b, fobUsd={"exact": "1.0000"}) for b in engine_variant(0)["breakdowns"]]
    vendure.breakdowns["101"] = bad
    client = VendureClient()
    res = await ds.diagnose_product(snap(0), ds.BudgetedSource(client), yellow_min_pct=10.0,
                                    channel=await client.get_channel_pricing())
    assert any("desglose de precio (no cierra)" in e for e in res.errors)
    assert res.diagnosis.rule("d").status == "sin_dato" and res.breakdown is None
    assert res.diagnosis.rule("b").status != "sin_dato"      # los tramos del catálogo se leyeron bien


async def test_a_forbidden_read_is_a_missing_piece_not_a_crash(vendure):
    vendure.http_fail.add("ProductCertification")
    client = VendureClient()
    res = await ds.diagnose_product(snap(2, product_id="900", median_ars=8_000), ds.BudgetedSource(client),
                                    yellow_min_pct=10.0, channel=await client.get_channel_pricing())
    assert any(e.startswith("certificado") for e in res.errors)
    assert res.diagnosis.rule("c").fires        # el cálculo sale del desglose; falta solo el estado del certificado
    assert "certificado" in res.diagnosis.missing


async def test_no_economics_no_diagnosis(vendure):
    client = VendureClient()
    assert await ds.diagnose_product(snap(ml_median_cents=None), ds.BudgetedSource(client),
                                     yellow_min_pct=10.0, channel=None) is None
    assert vendure.requests == []


async def test_local_stock_variant_skips_a_d_e(vendure):
    raw = vendure.variants["101"]
    vendure.variants["101"] = dict(raw, customFields=dict(raw["customFields"], sourcingMode="local_stock"))
    client = VendureClient()
    res = await ds.diagnose_product(snap(0), ds.BudgetedSource(client), yellow_min_pct=10.0, channel=None)
    assert [res.diagnosis.rule(c).status for c in "ade"] == ["no_aplica"] * 3


def test_neighbor_boxes_come_from_the_snapshot_specs():
    specs = {"1": {"box_length": 24, "box_width": 18, "box_height": 14, "box_weight": 0.8},
             "2": {"box_length": 30, "box_width": 20, "box_height": 10, "box_weight": 1.0}, "3": {}}
    boxes = ds.neighbor_boxes("0", specs, lambda pid, k: [("1", .9), ("2", .8), ("3", .7), ("9", .6)][:k])
    assert [b.is_complete for b in boxes] == [True, True, False]     # el "9" no tiene snapshot: se omite
    assert ds.neighbor_boxes("0", specs, lambda pid, k: []) is None   # sin índice CLIP: sin referencia
    assert ds.parse_specs("not json") == {} and ds.parse_specs(None) == {} and ds.parse_specs('{"a": 1, "b": "x"}') == {"a": 1.0}


async def test_the_source_wrapper_exposes_no_write_method():
    names = {n for n in dir(ds.BudgetedSource(object())) if not n.startswith("_")}
    assert names == {"get_tier_breakdowns", "get_priced_variant", "get_certification", "get_channel_pricing",
                     "calls", "exhausted", "max_calls"}
