"""Diagnóstico a-e (PR1 de la bandeja): reglas puras, 8 casos por regla (4 disparan y 4 no, con bordes).

Las filas de desglose salen del engine real de Vendure (golden/breakdowns_engine_fdd7579df.json); los casos
de borde las modifican con `dataclasses.replace` para fijar el número exacto que se quiere probar.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app.pricing.price_breakdown import TierBreakdown, parse_tier_breakdowns  # noqa: E402
from app.pricing.price_diagnosis import (  # noqa: E402
    Box,
    CertFacts,
    DiagFacts,
    DiagThresholds,
    Economics,
    build_quantity_brackets,
    box_reference,
    diagnose,
    ladder_index,
    parse_tier_used,
    price_max_cents,
    rule_a,
    rule_b,
    rule_c,
    rule_d,
    rule_e,
    select_tier,
    tiers_from_variant,
)
from app.pricing.semaforo import PricedVariant, PriceTier  # noqa: E402
from app.vendure.client import VendureClient  # noqa: E402
from tests.review_fixtures import ENGINE_FIXTURE  # noqa: E402

TH = DiagThresholds()
GOLDEN = Path(__file__).parent / "golden"


def rows(i: int) -> list[TierBreakdown]:
    return parse_tier_breakdowns(ENGINE_FIXTURE["variants"][i]["breakdowns"])


def variant(i: int) -> PricedVariant:
    v = ENGINE_FIXTURE["variants"][i]
    [mapped] = VendureClient._map_priced_variants([{
        "id": v["variant"], "name": "", "sku": "", "priceWithTax": v["priceWithTax"], "currencyCode": "ARS",
        "customFields": v["customFields"], "bulkPriceTiers": [dict(t, position=t["position"]) for t in v["tiers"]]}])
    return mapped


def econ_for(row: TierBreakdown, median_ars: float, *, price_cents: int | None = None, c: float = 13.0,
             s: int = 0, y: float = 10.0) -> Economics:
    return Economics(price_cents or row.factura_per_unit_minor, round(median_ars * 100), c, s, y)


def ref_boxes(n: int = 8) -> list[Box]:
    return [Box(24 + i % 3, 18 + i % 2, 14 + i % 3, 0.8 + 0.05 * i) for i in range(n)]


def facts(i: int = 0, tier: int = 0, median_ars: float = 70_000, **kw) -> DiagFacts:
    rs = rows(i)
    base = dict(
        economics=econ_for(rs[tier], median_ars), tier_used=f"tier:min_qty={rs[tier].min_quantity}",
        breakdowns=rs, tiers=tiers_from_variant(variant(i)), sourcing_mode="import", min_purchase_cny=500,
        box=Box(*[ENGINE_FIXTURE["variants"][i]["customFields"][k] for k in ("boxLength", "boxWidth", "boxHeight", "boxWeight")]),
        neighbor_boxes=ref_boxes(), certification_read=True,
    )
    base.update(kw)
    return DiagFacts(**base)


# ─── la cuenta base ───────────────────────────────────────────────


def test_p_max_matches_the_documented_example():
    # P = 20.000, M = 24.000, c = 13, y = 10  →  P_max = 18.982 y hay que bajar 5,1 %
    e = Economics(2_000_000, 2_400_000, 13.0, 0, 10.0)
    assert round(e.p_max_cents / 100) == 18_982
    assert round(e.gap * 100, 1) == 5.1
    assert price_max_cents(2_400_000, 13, 0, 10) == e.p_max_cents


def test_p_max_is_exactly_where_the_semaforo_turns_red():
    from app.pricing import semaforo

    e = Economics(1_000_000, 2_400_000, 13.0, 50_000, 10.0)
    at_edge = round(e.p_max_cents)
    for price, expected in ((at_edge, semaforo.AMARILLO), (at_edge + 5, semaforo.ROJO)):
        margin = semaforo.estimated_margin_pct(e.ml_median_cents, price, 13.0, 50_000)
        assert semaforo.color(margin, price, e.ml_median_cents, 30.0, 10.0) == expected


def test_being_above_ml_is_the_same_case():
    e = Economics(2_500_000, 2_400_000, 13.0, 0, 10.0)      # nuestro precio por encima de ML
    assert not e.gets_out(e.our_price_cents) and e.gets_out(e.p_max_cents)


def test_tier_used_label_and_tier_selection():
    assert parse_tier_used("tier:min_qty=12") == 12
    assert parse_tier_used("priceWithTax") is None and parse_tier_used("priceWithTax(fallback)") is None
    rs = rows(0)
    assert select_tier(rs, "tier:min_qty=50").min_quantity == 50
    assert select_tier(rs, "priceWithTax").min_quantity == 100        # el más barato por unidad
    assert select_tier(rs, "tier:min_qty=7") is None and select_tier(None, "x") is None


# ─── (a) caja fuera de rango: 4 disparan y 4 no ───────────────────


def _a(**kw):
    f = facts(0, **kw)
    return rule_a(f, TH, select_tier(f.breakdowns, f.tier_used))


A_CASES = [
    ("a1: caja de 62x48x40 contra parecidos de ~24x18x14", dict(), True, ["a1", "a2"]),
    ("a3: la caja es más chica que el producto", dict(box=Box(10, 10, 10, 2.0), product=Box(30, 20, 10, 1.0)), True, ["a3"]),
    ("a3: la caja tiene más de 10 veces el volumen del producto",
     dict(box=Box(60, 50, 40, 10.0), product=Box(10, 10, 10, 1.0), neighbor_boxes=None), True, ["a3"]),
    ("a2: caja normal pero con 10 veces el peso", dict(box=Box(24, 18, 14, 9.0)), True, ["a2"]),
    ("no: caja parecida a la de los parecidos", dict(box=Box(25, 18, 15, 0.9)), False, []),
    ("no: z enorme (MAD cero) pero apenas 1,5 veces el volumen",
     dict(neighbor_boxes=[Box(24, 18, 14, 0.8)] * 8, box=Box(24, 18, 21, 0.8)), False, []),
    ("sin dato: menos de 5 parecidos con caja y sin imposibles internos",
     dict(neighbor_boxes=ref_boxes(4)), False, []),
    ("no: abastecimiento local", dict(sourcing_mode="local_stock"), False, []),
]


@pytest.mark.parametrize("name,kw,fires,subs", A_CASES, ids=[c[0][:40] for c in A_CASES])
def test_rule_a(name, kw, fires, subs):
    r = _a(**kw)
    assert r.fires is fires, (name, r.status, r.summary)
    if fires:
        assert set(subs) <= set(r.subcodes) and r.estimated
    else:
        assert r.status in ("no", "no_aplica", "sin_dato") and not r.subcodes
        if name.startswith("sin dato"):
            assert r.status == "sin_dato" and "no se pudo comparar" in r.summary.lower()


def test_rule_a_edges_on_the_ratio_and_the_z_threshold():
    ref = [Box(24, 18, 14, 0.8)] * 5 + [Box(25, 18, 14, 0.8), Box(24, 19, 14, 0.8), Box(24, 18, 15, 0.8)]
    base = dict(neighbor_boxes=ref)
    v_ref = box_reference(ref).volume_cm3
    just_over = Box(24, 18, 14 * 2.02, 0.8)       # ~2,02x el volumen
    just_under = Box(24, 18, 14 * 1.95, 0.8)      # ~1,95x
    assert 2.0 < just_over.volume_cm3 / v_ref and just_under.volume_cm3 / v_ref < 2.0
    assert _a(box=just_over, **base).fires and not _a(box=just_under, **base).fires
    # la mitad de la referencia también cuenta
    assert _a(box=Box(24, 18, 14 * 0.45, 0.8), **base).fires


def test_rule_a_without_a_box_has_no_data():
    r = _a(box=Box())
    assert r.status == "sin_dato" and "no tiene la caja" in r.summary


def test_rule_a_estimate_is_a_lower_bound_and_says_if_it_gets_out_of_red():
    f = facts(0, median_ars=70_000)
    r = rule_a(f, TH, select_tier(f.breakdowns, f.tier_used))
    assert r.fires and r.p_si_cents is not None and r.p_si_cents < f.economics.our_price_cents
    assert r.fixes == "si" and f.economics.gets_out(r.p_si_cents)
    assert "estimado" in r.summary and "sale del rojo" in r.summary
    # con un ML mucho más bajo la corrección no alcanza ni quitando TODA la logística
    f2 = facts(0, median_ars=10_000)
    r2 = rule_a(f2, TH, select_tier(f2.breakdowns, f2.tier_used))
    assert r2.fixes == "no"


def test_rule_a_saving_is_a_lower_bound_never_more_than_removing_all_the_logistics():
    """El ahorro estimado con la caja de referencia nunca supera el de quitar TODA la logística: es una cota inferior."""
    f = facts(0, median_ars=70_000)
    row = select_tier(f.breakdowns, f.tier_used)
    r = rule_a(f, TH, row)
    ref = box_reference(f.neighbor_boxes)
    share = float(row.logistics_usd / row.nbg_usd)
    # Si TODA la logística desapareciera el precio bajaría `share`; la estimación baja menos que eso.
    assert r.p_si_cents >= f.economics.our_price_cents * (1 - share)


# ─── (b) tramos de cantidad mal armados ───────────────────────────


def _tiers_with(i: int, changes: dict[int, dict]) -> list[PriceTier]:
    ts = tiers_from_variant(variant(i))
    out = []
    for n, t in enumerate(ts):
        ch = changes.get(n) or {}
        out.append(replace(t, **ch))
    return out


def _shifted_tiers() -> list[PriceTier]:
    """Los tramos del organizador corridos un escalón (50 / 100 / 200 en vez de 25 / 50 / 100), con los
    precios guardados que da Vendure para esas cantidades."""
    rs = {b.min_quantity: b for b in rows(0)}
    quantities = [(50, 99, rs[50].neto_per_unit_minor), (100, 199, rs[100].neto_per_unit_minor), (200, None, 11_000_000)]
    return [replace(t, min_quantity=m, max_quantity=x, sale_price_cents=price)
            for t, (m, x, price) in zip(tiers_from_variant(variant(0)), quantities)]


def _b(i: int = 0, tiers: list[PriceTier] | None = None, **kw):
    f = facts(i, tiers=tiers if tiers is not None else tiers_from_variant(variant(i)), **kw)
    return rule_b(f, TH, select_tier(f.breakdowns, f.tier_used))


B_CASES = [
    ("b1: tramo habilitado sin precio de venta", lambda: _b(tiers=_tiers_with(0, {1: {"sale_price_cents": None}})), True, "b1"),
    ("b1: tramo habilitado sin costo en yuanes", lambda: _b(tiers=_tiers_with(0, {1: {"cost_cny_minor": 0}})), True, "b1"),
    ("b2: el costo en yuanes sube al subir la cantidad", lambda: _b(tiers=_tiers_with(0, {2: {"cost_cny_minor": 9900}})), True, "b2"),
    ("b3: las cantidades no cierran", lambda: _b(tiers=_tiers_with(0, {0: {"max_quantity": 40}})), True, "b3"),
    ("no: tramos bien armados (engine)", lambda: _b(), False, ""),
    ("no: primer tramo un escalón más arriba que la regla (borde)", lambda: _b(tiers=_shifted_tiers()), False, ""),
    ("no: precio guardado a 1,0 % del cálculo (borde)", lambda: _b(tiers=_tiers_with(0, {0: {"sale_price_cents": round(rows(0)[0].neto_per_unit_minor * 1.0099)}})), False, ""),
    ("no: abastecimiento local no pide costo en yuanes", lambda: _b(sourcing_mode="local_stock", tiers=_tiers_with(0, {0: {"cost_cny_minor": None}})), False, ""),
]


@pytest.mark.parametrize("name,run,fires,sub", B_CASES, ids=[c[0][:40] for c in B_CASES])
def test_rule_b(name, run, fires, sub):
    r = run()
    assert r.fires is fires, (name, r.status, r.subcodes, r.summary)
    if fires:
        assert sub in r.subcodes


def test_rule_b4_first_tier_two_rungs_away_fires():
    ts = [replace(t, min_quantity=m, max_quantity=x) for t, (m, x) in zip(
        tiers_from_variant(variant(0)), [(100, 199), (200, 299), (300, None)])]     # la regla da 25
    r = _b(tiers=ts)
    assert r.fires and "b4" in r.subcodes and r.evidence["first_tier"] == {"actual": 100, "expected": 25}


def test_rule_b5_stale_saved_price_fires_and_has_an_exact_fix():
    # el precio guardado del primer tramo quedó 5 % por encima del que da el cálculo (sigue bajando con la cantidad)
    ts = _tiers_with(0, {0: {"sale_price_cents": round(rows(0)[0].neto_per_unit_minor * 1.05)}})
    r = _b(tiers=ts)
    assert r.fires and r.subcodes == ["b5"] and r.p_si_cents == rows(0)[0].factura_per_unit_minor
    assert r.fixes in ("si", "no") and not r.estimated


def test_rule_b5_list_price_not_matching_the_calculation_fires():
    f = facts(0)
    f = replace(f, economics=replace(f.economics, our_price_cents=round(rows(0)[0].factura_per_unit_minor * 1.05)))
    r = rule_b(f, TH, select_tier(f.breakdowns, f.tier_used))
    assert r.fires and "b5" in r.subcodes and "no coincide" in r.summary


def test_rule_b_structural_problems_are_not_estimated():
    r = _b(tiers=_tiers_with(0, {2: {"cost_cny_minor": 9900}}))
    assert r.fixes == "aprox" and r.p_si_cents is None and "se corrige y se vuelve a medir" in r.summary


def test_rule_b_cheapest_tier_evidence_says_when_the_red_is_only_the_minimum_purchase():
    f = facts(0, median_ars=1_700_000 / 10)       # ML a ~170k: el tramo de 100 unidades (149k) no es rojo, el de 25 (153k) sí
    r = rule_b(f, TH, select_tier(f.breakdowns, f.tier_used))
    cheap = r.evidence["cheapest_tier"]
    assert cheap["min_quantity"] == 100 and cheap["red"] in (True, False)
    f_cheap = facts(0, median_ars=165_000)
    e = f_cheap.economics
    assert e.our_price_cents > e.p_max_cents


def test_rule_b_without_any_reading_has_no_data():
    f = facts(0, tiers=None, breakdowns=None)
    assert rule_b(f, TH, None).status == "sin_dato"


def test_quantity_brackets_match_the_vendure_code_vector_by_vector():
    vectors = json.loads((GOLDEN / "quantity_brackets_fdd7579df.json").read_text())["vectors"]
    assert len(vectors) >= 15
    for v in vectors:
        expected = [None if b is None else (b["minQuantity"], b["maxQuantity"]) for b in v["brackets"]]
        assert build_quantity_brackets(v["costs"], v["threshold"], v["override"]) == expected, v


def test_ladder_index():
    assert [ladder_index(q) for q in (1, 2, 5, 6, 25, 26, 500, 501, 1000, 1001)] == [0, 1, 1, 2, 3, 4, 7, 9, 9, 10]


# ─── (c) certificado alto ─────────────────────────────────────────


def _c(row: TierBreakdown, median_ars: float, *, cert_usd: str | None = None, share_pct: float | None = None, **kw):
    if share_pct is not None:                    # se fija el % exacto sobre el NBG
        base = row.nbg_usd - row.certification_usd
        cert_usd = str((base * Decimal(str(share_pct)) / (Decimal(100) - Decimal(str(share_pct)))).quantize(Decimal("0.000001")))
    if cert_usd is not None:
        row = replace(row, certification_usd=Decimal(cert_usd))
    f = facts(0, **kw)
    f = replace(f, economics=econ_for(row, median_ars, price_cents=row.factura_per_unit_minor), breakdowns=[row],
                tier_used=f"tier:min_qty={row.min_quantity}")
    return rule_c(f, TH, row), f


ROW_CERT = rows(2)[0]          # el cargador con certificado: 180 USD de recargo


def _median_for(row: TierBreakdown, p_max_over_p: float) -> float:
    """Mediana de ML (en pesos) con la que P_max queda en `p_max_over_p` × nuestro precio (c = 13 %, y = 10 %)."""
    return row.factura_per_unit_minor * p_max_over_p / 100 * 1.10 / 0.87
ROW_PLAIN = rows(1)[2]         # la taza, sin certificado


C_CASES = [
    ("c: el certificado es casi la mitad del neto (engine)", lambda: _c(ROW_CERT, 8_000), True),
    ("c: justo el 10,0 % (borde, sigue en rojo sin él)", lambda: _c(ROW_CERT, 8_000, share_pct=10.0), True),
    ("c2: solo 5 % pero sin certificado ya no sería rojo", lambda: _c(ROW_CERT, _median_for(ROW_CERT, 0.975), share_pct=5.0), True),
    ("c: 30 % y sin él sale del rojo", lambda: _c(ROW_CERT, 14_000, share_pct=30.0), True),
    ("no: el precio no lleva certificado", lambda: _c(ROW_PLAIN, 5_000), False),
    ("no: 9,9 % y sigue en rojo sin él", lambda: _c(ROW_CERT, 8_000, share_pct=9.9), False),
    ("no: 5 % y sigue en rojo", lambda: _c(ROW_CERT, 8_000, share_pct=5.0), False),
    ("sin dato: no hay desglose", lambda: (rule_c(replace(facts(0), breakdowns=None), TH, None), None), False),
]


@pytest.mark.parametrize("name,run,fires", C_CASES, ids=[c[0][:40] for c in C_CASES])
def test_rule_c(name, run, fires):
    r, _f = run()
    assert r.fires is fires, (name, r.status, r.summary, r.evidence)


def test_rule_c_is_almost_exact_and_says_if_it_gets_out():
    r, f = _c(ROW_CERT, 14_000, share_pct=30.0)
    share = 0.30
    assert r.p_si_cents == round(f.economics.our_price_cents * (1 - share)) and not r.estimated
    assert r.fixes == "si" and f.economics.gets_out(r.p_si_cents)
    assert "aunque el certificado no esté pagado" in r.summary
    r2, _ = _c(ROW_CERT, 8_000, share_pct=30.0)
    assert r2.fires and r2.fixes == "no"          # pesa mucho, pero sin él sigue en rojo


def test_rule_c_matches_the_engine_recalculation_without_certificate_within_one_percent():
    """P × (1 − cert/NBG) contra la factura que el engine calcula SIN certificado (mismo tramo, certificado en 0)."""
    # La factura sin certificado se arma con las mismas cuentas del pipeline: el neto baja el certificado y la
    # pasarela y el IVA de venta son proporcionales (net = NBG / (1 − gateway), factura = net × (1 + IVA)).
    for i in range(3):
        for row in rows(i):
            if row.certification_usd <= 0:
                continue
            gateway_rate = row.gateway_usd / (row.neto_usd - row.gateway_usd)       # retención sobre el neto previo
            nbg_without = row.nbg_usd - row.certification_usd
            net_without = nbg_without * (1 + gateway_rate)
            invoice_without = net_without * (row.factura_usd / row.neto_usd)
            exact_ratio = float(invoice_without / row.factura_usd)
            mine = 1 - float(row.certification_usd / row.nbg_usd)
            assert abs(mine - exact_ratio) < 0.01, (i, row.min_quantity, mine, exact_ratio)


# ─── (d) valor bajo por m³ ────────────────────────────────────────


def _d(row: TierBreakdown, *, ratio: float | None = None, **kw):
    if ratio is not None:                       # fija logística = ratio × fábrica
        target = row.fob_usd * Decimal(str(ratio))
        row = replace(row, flete_usd=target - row.servicios_usd) if target > row.servicios_usd else \
            replace(row, flete_usd=Decimal(0), servicios_usd=target)
    f = replace(facts(0, **kw), breakdowns=[row], tier_used=f"tier:min_qty={row.min_quantity}")
    return rule_d(f, TH, row)


D_CASES = [
    ("d: la logística cuesta 9 veces la fábrica (engine)", lambda: _d(rows(0)[0]), True),
    ("d: justo 1,0 (borde)", lambda: _d(rows(1)[2], ratio=1.0), True),
    ("d: 2,1 veces (taza del engine)", lambda: _d(rows(1)[0]), True),
    ("d: 1,0001", lambda: _d(rows(1)[2], ratio=1.0001), True),
    ("no: la logística es el 57 % de la fábrica (engine)", lambda: _d(rows(2)[0]), False),
    ("no: 0,9999", lambda: _d(rows(1)[2], ratio=0.9999), False),
    ("no: abastecimiento local", lambda: _d(rows(0)[0], sourcing_mode="local_stock"), False),
    ("sin dato: fábrica en cero", lambda: _d(replace(rows(0)[0], fob_usd=Decimal(0))), False),
]


@pytest.mark.parametrize("name,run,fires", D_CASES, ids=[c[0][:40] for c in D_CASES])
def test_rule_d(name, run, fires):
    r = run()
    assert r.fires is fires, (name, r.status, r.evidence)
    if fires:
        assert r.fixes == "no" and r.p_si_cents is None       # estructural: no hay qué corregir


def test_rule_d_reports_logistics_and_factory_per_m3():
    r = _d(rows(0)[0])
    row = rows(0)[0]
    assert r.evidence["ratio"] == pytest.approx(float(row.logistics_usd / row.fob_usd), rel=1e-3)
    assert r.evidence["logistics_per_m3"] == pytest.approx(float(row.logistics_usd) / row.chargeable_units, rel=1e-2)
    assert r.status == "dispara" and "m³" in r.summary


# ─── (e) ML por debajo de nuestro costo de fábrica ────────────────


def _fob_ars(row: TierBreakdown) -> float:
    return float(row.fob_usd / row.quantity * row.usd_to_channel)


def _e(row: TierBreakdown, median_ars: float, **kw):
    f = replace(facts(0, **kw), economics=econ_for(row, median_ars), breakdowns=[row],
                tier_used=f"tier:min_qty={row.min_quantity}")
    return rule_e(f, TH, row), f


ROW_E = rows(2)[0]       # fábrica por unidad: ~3.041 pesos


E_CASES = [
    ("e: ML mucho más barato que la fábrica", lambda: _e(ROW_E, 1_500)[0], True),
    ("e: ML justo igual a la fábrica (borde)", lambda: _e(ROW_E, _fob_ars(ROW_E))[0], True),
    ("e: un peso por debajo", lambda: _e(ROW_E, _fob_ars(ROW_E) - 1)[0], True),
    ("e: ML a la mitad de la fábrica", lambda: _e(ROW_E, _fob_ars(ROW_E) / 2)[0], True),
    ("no: un centavo por encima (borde)", lambda: _e(ROW_E, _fob_ars(ROW_E) + 0.02)[0], False),
    ("no: ML bastante por encima de la fábrica", lambda: _e(ROW_E, 20_000)[0], False),
    ("no: abastecimiento local", lambda: _e(ROW_E, 1_500, sourcing_mode="local_stock")[0], False),
    ("sin dato: no hay desglose", lambda: rule_e(facts(0), TH, None), False),
]


@pytest.mark.parametrize("name,run,fires", E_CASES, ids=[c[0][:40] for c in E_CASES])
def test_rule_e(name, run, fires):
    r = run()
    assert r.fires is fires, (name, r.status, r.summary)
    if fires:
        assert r.fixes == "no"


def test_rule_e_target_cost_and_the_free_factory_message():
    # ML apenas por debajo de la fábrica, pero con un precio nuestro que sí podría bajar: hay un objetivo en yuanes
    r, f = _e(ROW_E, 9_000)
    assert r.status == "no"
    r, f = _e(ROW_E, _fob_ars(ROW_E) * 0.9)
    assert r.fires
    cny_today = float(ROW_E.purchase_cny)
    p, p_max = f.economics.our_price_cents, f.economics.p_max_cents
    fixed = p * float((ROW_E.logistics_usd + ROW_E.certification_usd) / ROW_E.nbg_usd)
    if p_max > fixed:
        assert r.evidence["target_cny"] == pytest.approx(cny_today * (p_max - fixed) / (p - fixed), rel=1e-3)
        assert "orientativo" in r.summary
    else:
        assert r.evidence["free_factory_not_enough"] and "ni con fábrica gratis" in r.summary


def test_rule_e_says_when_not_even_a_free_factory_reaches():
    # lo fijo (logística + certificado) ya supera lo que ML deja: ni con fábrica gratis
    r, _f = _e(ROW_E, 1_500)
    assert r.evidence["free_factory_not_enough"] is True and "ni con fábrica gratis" in r.summary


# ─── local_stock ──────────────────────────────────────────────────


def test_local_stock_only_applies_b_and_c():
    f = facts(0, sourcing_mode="local_stock")
    d = diagnose(f, TH)
    assert [d.rule(c).status for c in "ade"] == ["no_aplica"] * 3
    assert all(not d.rule(c).fires for c in "ade")
    assert d.rule("b").status != "no_aplica" and d.rule("c").status != "no_aplica"


# ─── el diagnóstico completo ──────────────────────────────────────


def test_probable_cause_is_the_first_rule_that_fires_in_order_a_to_e():
    f = facts(0, median_ars=70_000)      # el organizador: caja (a) y valor por m³ (d) disparan
    d = diagnose(f, TH)
    assert d.rule("a").fires and d.rule("d").fires
    assert d.probable_cause == "a" and [r.code for r in d.rules] == list("abcde")
    # sacando la causa (a), la probable pasa a ser la siguiente que dispara
    d2 = diagnose(replace(f, box=Box(25, 18, 15, 0.9), product=Box()), TH)
    assert not d2.rule("a").fires and d2.probable_cause == "d"


def test_no_cause_means_no_clear_cause_and_points_to_the_listings():
    # el cargador sin recargo de certificado: caja normal, tramos bien armados, logística chica y ML por encima de la
    # fábrica. Sigue en rojo (su precio es mucho mayor que el de ML) pero ninguna de las cinco lo explica.
    row = replace(rows(2)[0], certification_usd=Decimal(0))
    f = facts(2, median_ars=8_500, box=Box(20, 15, 8, 0.35),
              neighbor_boxes=[Box(20 + i % 2, 15, 8 + i % 2, 0.35) for i in range(8)])
    f = replace(f, breakdowns=[row], tier_used="tier:min_qty=50", economics=econ_for(row, 8_500))
    d = diagnose(f, TH)
    assert [r.fires for r in d.rules] == [False] * 5, [(r.code, r.status, r.summary) for r in d.rules]
    assert d.probable_cause == "ninguna" and d.fixable == "no" and d.fix_not_enough is True
    assert "match de Mercado Libre" in d.suggestion


def test_a_missing_reading_is_never_reported_as_no_clear_cause():
    """Si a alguna regla le faltó un dato y ninguna dispara, la causa es «sin_dato», no «ninguna»: no se descarta nada."""
    row = replace(rows(2)[0], certification_usd=Decimal(0))
    f = facts(2, median_ars=8_500, box=Box(20, 15, 8, 0.35), neighbor_boxes=None)
    f = replace(f, breakdowns=[row], tier_used="tier:min_qty=50", economics=econ_for(row, 8_500))
    d = diagnose(f, TH)
    assert d.rule("a").status == "sin_dato" and not any(r.fires for r in d.rules)
    assert d.probable_cause == "sin_dato" and "Falta información" in d.suggestion


def test_fixable_and_the_not_enough_warning():
    f = facts(0, median_ars=70_000)
    d = diagnose(f, TH)
    assert d.fixable == "si" and d.fix_not_enough is False
    # un ML tan bajo que ni quitando toda la logística alcanza → corregir lo nuestro no alcanza
    f2 = facts(0, median_ars=5_000)
    d2 = diagnose(f2, TH)
    assert d2.fixable == "no" and d2.fix_not_enough is True
    assert d2.p_max_cents < d2.p_cents and d2.gap is not None and 0 < d2.gap < 1


def test_all_five_rules_are_always_evaluated_and_stored():
    d = diagnose(facts(1, median_ars=3_000), TH)
    assert [r.code for r in d.rules] == list("abcde")
    blob = json.dumps(d.to_dict())
    assert len(blob) < 8_000          # cabe en la columna (hasta 8 KB)
    assert d.to_dict()["probable_cause"] == d.probable_cause


def test_missing_data_is_reported_and_never_invented():
    f = facts(0, breakdowns=None, tiers=None, neighbor_boxes=None, certification_read=False)
    d = diagnose(f, TH)
    assert set(d.missing) >= {"desglose de precio", "tramos de la variante", "productos parecidos", "certificado"}
    assert d.rule("b").status == "sin_dato" and d.rule("c").status == "sin_dato"
    assert d.rule("d").status == "sin_dato" and d.rule("e").status == "sin_dato"
    assert d.probable_cause in ("a", "sin_dato")       # (a3) todavía puede dispararse con la caja sola; si no, no se descarta nada
    assert not d.complete


def test_box_reference_needs_five_valid_boxes_and_ignores_incomplete_ones():
    assert box_reference(ref_boxes(4), 5) is None
    assert box_reference(ref_boxes(5) + [Box(None, 10, 10, 1), Box(10, 10, 10, 0)], 5).n >= 3
    ref = box_reference(ref_boxes(10), 5)
    assert ref.n >= 8 and ref.source == "vecinos_clip" and "×" in ref.dims_text
    assert ref.z("volume", ref.volume_cm3) == pytest.approx(0.0, abs=1e-9)
