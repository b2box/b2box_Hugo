"""QA independiente (criterio 3): diagnóstico a-e con 20 productos de desglose realista, cuentas hechas a mano.

El modelo de precios y el Vendure falso son de `qa5_bandeja` (no usan los helpers del developer). Cada escenario
pasa por el cliente real de Vendure -> parser del desglose -> reglas, y se contrasta contra:

  * las fórmulas del documento (sección 3.8) recalculadas acá con Decimal, aparte del código a probar;
  * la causa probable = la primera que dispara en el orden a-e;
  * «arreglarla saca del rojo»: se recalcula el precio con el dato corregido (modelo propio) y se mira el color real.
"""

from __future__ import annotations

import os
from decimal import Decimal as D

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app.pricing import price_diagnosis as pd_  # noqa: E402
from app.pricing.price_breakdown import BreakdownError, parse_tier_breakdown  # noqa: E402
from tests import qa5_bandeja as q  # noqa: E402
from tests.qa5_bandeja import Scenario, Tier, VendureWorld, healthy, ladder_tiers, neighbors  # noqa: E402

P0 = healthy("0").our_price                      # el precio de un producto sano de la familia: 3.411.335 centavos
M12 = q.median_for(P0, 12)                         # con esta mediana el sano deja 12 % (amarillo)


@pytest.fixture
def world(monkeypatch):
    w = VendureWorld()
    w.spy = q.install_spy(monkeypatch, w)
    return w


def diag(world, sc, neigh=None, **kw):
    world.add(sc)
    out = q.call_diag(world, sc, neigh, **kw)
    assert world.writes() == [] and world.spy.violations == []
    return out.diagnosis, out


def fires(d) -> dict[str, list[str]]:
    return {r.code: r.subcodes for r in d.rules if r.fires}


# ─── 0. el modelo de QA es coherente (si no, nada de lo que sigue vale) ────────────────────────────


def test_the_qa_model_builds_breakdowns_whose_ten_parts_add_up_to_the_invoice():
    for sc in (healthy("0"), healthy("1", box=q.BIG_BOX, cbm="0.024", kg="6.0"), healthy("2", cert="30")):
        for raw in sc.breakdowns():
            bd = parse_tier_breakdown(raw)
            assert abs(bd.parts_sum_usd() - bd.factura_usd) <= D("0.01"), raw["tierId"]


# ─── 1. Las fórmulas del documento, a mano ─────────────────────────────────────────────────────────


def test_p_max_and_gap_by_hand(world):
    """P_max = (M x (1 - c/100) - s) / (1 + y/100). M = 4.391.604, c = 13, s = 0, y = 10:
    4.391.604 x 0,87 = 3.820.695,48 ; / 1,10 = 3.473.359,5 ; gap = 1 - 3.473.359,5 / 5.108.948 = 0,3201."""
    sc = healthy("1", box=q.BIG_BOX, cbm="0.024", kg="6.0", median=M12)
    d, _ = diag(world, sc, neighbors())
    assert M12 == 4_391_604
    assert d.p_max_cents == pytest.approx(3_473_359.5, abs=0.6)
    assert sc.our_price == 5_108_948
    assert d.gap == pytest.approx(0.3201, abs=5e-4)
    assert d.p_cents == 5_108_948


def test_rule_a_estimate_by_hand_is_the_documented_formula_and_never_overstates_the_saving(world):
    """Tramo usado: 10 unidades a ¥60. Caja cargada 0,024 m³ (40x30x20); la de los parecidos, mediana 0,009 m³ (30x20x15) y 2,5 kg.
       chargeable = max(0,024 x 10 ; 6 kg x 10 / 1000) = 0,24 m³ ; chargeable_ref = max(0,009 x 10 ; 2,5 x 10 / 1000) = 0,09 m³
       fob = 60 x 10 x 0,1427 = 85,62 ; flete = 0,24 x 103,17 = 24,7608 ; seguro = 0,5 % x (85,62 + 24,7608) = 0,5519
       cif = 110,9327 ; aranceles = 23 % x cif = 25,5144 ; servicios = 0,24 x (358,80 + 14,27) = 89,5368
       subtop = 225,9839 ; fee = 20 % = 45,1968 ; fin = 0,2 % = 0,4520 ; NBG = 271,6327
       L = flete + servicios = 24,7608 + 89,5368 = 114,2976 ; L/NBG = 0,42076
       P_si = P x (1 - 0,42076 x (1 - 0,09/0,24)) = 5.108.948 x 0,73702 = 3.765.3xx"""
    sc = healthy("1", box=q.BIG_BOX, cbm="0.024", kg="6.0", median=M12)
    d, _ = diag(world, sc, neighbors())
    a = d.rule("a")
    assert a.fires and a.subcodes == ["a1", "a2"] and a.estimated is True
    nbg = D("225.9839") + D("45.1968") + D("0.4520")
    share = D("114.2976") / nbg
    assert float(share) == pytest.approx(0.42076, abs=2e-4)
    hand = float(D(5_108_948) * (1 - share * (1 - D("0.09") / D("0.24"))))
    assert a.p_si_cents == pytest.approx(hand, rel=1e-3)
    # lo que daría de verdad la caja bien cargada (modelo propio): el estimado es una COTA, no puede quedar por debajo
    assert q.healthy("0").our_price == 3_411_335 <= a.p_si_cents
    assert q.color_of(M12, 3_411_335) == "amarillo" and q.color_of(M12, sc.our_price) == "rojo"


def test_rule_c_by_hand_is_almost_exact(world):
    """Tramo de 10 unidades, caja sana de 0,009 m³ (chargeable 0,09 m³): fob 85,62 ; flete 0,09 x 103,17 = 9,2853 ;
    seguro 0,4745 ; cif 95,3798 ; aranceles 23 % = 21,9374 ; servicios 0,09 x 372,07 = 33,5763 ; subtop 150,8935 ;
    fee 30,1787 ; fin 0,3018 ; NBG sin certificado = 181,3740 ; con USD 40 de certificado = 221,3740 -> pesa 18,07 %.
    P_si = P x (1 - 40/221,374) debe coincidir con el precio SIN certificado (el del producto sano)."""
    sc = healthy("8", cert="40")
    sc.median = q.median_for(sc.our_price, -2)
    d, _ = diag(world, sc, neighbors())
    c = d.rule("c")
    share = D("40") / (D("150.8935") + D("30.1787") + D("0.3018") + D("40"))
    assert float(share) == pytest.approx(0.18069, abs=1e-4)
    assert c.fires and c.subcodes == ["c1"] and c.fixes == "si"
    assert c.p_si_cents == pytest.approx(float(D(sc.our_price) * (1 - share)), abs=2)
    real_without_cert = healthy("8b").our_price                   # el mismo producto sin certificado
    assert abs(c.p_si_cents - real_without_cert) / real_without_cert <= 0.01     # criterio 1-4: 1 % o menos
    assert d.probable_cause == "c"


def test_rule_d_ratio_by_hand(world):
    """Item de ¥30 en caja de 0,024 m³ (4 kg), tramo de 10 unidades: chargeable 0,24 m³.
    fob = 30 x 10 x 0,1427 = 42,81 ; L = 0,24 x (103,17 + 358,80 + 14,27) = 114,2976 ; R = L / fob = 2,67 >= 1 -> dispara."""
    sc = healthy("10", costs=["30", "28", "25"], box=(40, 30, 20, 4.0), product=(35, 25, 15, 3.0), cbm="0.024", kg="4.0")
    sc.median = q.median_for(sc.our_price, -4)
    d, _ = diag(world, sc, neighbors((40, 30, 20), 4.0))
    r = d.rule("d")
    assert r.fires and r.fixes == "no"
    assert r.evidence["ratio"] == pytest.approx(114.2976 / 42.81, abs=2e-3)
    assert r.evidence["logistics_per_m3"] == pytest.approx(476.24, abs=0.1)
    assert d.probable_cause == "d"


def test_rule_e_target_cost_by_hand(world):
    """Item de ¥5 en 100 unidades (¥500 de compra mínima), caja 0,000576 m³.
    fob = 5 x 100 x 0,1427 = 71,35 USD -> por unidad 0,7135 x 1.500 = $ 1.070,25 ; ML mediana $ 1.040,25 <= $ 1.070,25 -> dispara.
    chargeable = 0,0576 ; L = 0,0576 x 476,24 = 27,4314 ; subtop = 117,036 ; NBG = 140,677 ; L/NBG = 0,19501
    P_fijo = P x 0,19501 = 51.597 ; P_max = 82.274 ; escala = (82.274 - 51.597)/(264.586 - 51.597) = 0,14403 ; objetivo = ¥5 x 0,14403 = ¥0,72."""
    sc = healthy("11", costs=["5", "4.5", "4"], box=(12, 8, 6, 0.3), product=(10, 6, 4, 0.2), cbm="0.000576", kg="0.3")
    sc.median = int(5 * D("0.1427") * 1500 * 100) - 3000
    d, _ = diag(world, sc, neighbors((12, 8, 6), 0.3))
    e = d.rule("e")
    assert sc.tiers[0].min_q == 100 and sc.our_price == 264_586
    assert e.fires and e.evidence["fob_unit_ars_cents"] == 107_025 and sc.median == 104_025
    assert d.p_max_cents == pytest.approx(82_274, abs=1)
    assert e.evidence["target_cny"] == pytest.approx(0.72, abs=0.01)
    assert e.evidence["free_factory_not_enough"] is False
    assert d.probable_cause == "e" and d.fixable == "no"


# ─── 2. Causa probable = la primera del orden a-e, y «arreglarla saca del rojo» ────────────────────


def build_catalog():
    """(nombre, escenario, vecinos, causa esperada, reglas que disparan, `fixes` de la causa)"""
    N = neighbors()
    out = []

    def add(name, sc, neigh, cause, fire, **extra):
        out.append((name, sc, neigh, cause, fire, extra))

    # a: caja inflada, ML tal que la caja bien cargada deja 12 %
    add("S01 caja inflada (a1+a2)", healthy("101", box=q.BIG_BOX, cbm="0.024", kg="6.0", median=M12), N, "a",
        {"a": ["a1", "a2"], "d": ["d1"]}, fixes="aprox", fix_exits=True)
    # a: ML tan bajo que ni arreglada sale (queda en rojo): nunca debe decir «sí»
    add("S02 caja inflada, aun arreglada sigue en rojo", healthy("102", box=q.BIG_BOX, cbm="0.024", kg="6.0",
                                                                 median=q.median_for(P0, -3)), N, "a",
        {"a": ["a1", "a2"], "d": ["d1"]}, fixes="aprox", fix_exits=False)
    add("S02b caja inflada, ni sin logística sale", healthy("103", box=q.BIG_BOX, cbm="0.024", kg="6.0",
                                                           median=q.median_for(P0, -12)), N, "a",
        {"a": ["a1", "a2"], "d": ["d1"]}, fixes="no", fix_exits=False)
    # b
    add("S03 b2: el costo sube al comprar más", healthy("104", costs=["60", "55", "58"], median=q.median_for(P0, 0)), N,
        "b", {"b": ["b2"]})
    tiers = ladder_tiers(q.OK_COSTS, "0.009", "2.5")
    tiers[2] = Tier(tiers[2].min_q, None, "0", tiers[2].q, "0.009", "2.5", empty=True)
    add("S04 b1: un tramo sin precio (PA00060)", Scenario("105", "v105", tiers, 10, q.median_for(P0, 0), box=q.OK_BOX,
                                                          product=q.OK_PRODUCT), N, "b", {"b": ["b1"]})
    tiers = ladder_tiers(q.OK_COSTS, "0.009", "2.5")
    tiers[0].max_q = 20
    add("S05 b3: las cantidades no cierran", Scenario("106", "v106", tiers, 10, q.median_for(P0, 0), box=q.OK_BOX,
                                                      product=q.OK_PRODUCT), N, "b", {"b": ["b3"]})
    t = [Tier(1, 4, "60", 1, "0.009", "2.5"), Tier(5, None, "55", 5, "0.009", "2.5")]
    add("S06 b4: el primer tramo arranca en 1 y la regla daría 10", Scenario("107", "v107", t, 1, q.median_for(P0, 0),
                                                                          box=q.OK_BOX, product=q.OK_PRODUCT), N, "b",
        {"b": ["b4"]})
    calc = healthy("0").used()["netoChannelPerUnit"]["minor"]
    add("S07 b5: precio guardado 8 % arriba del cálculo, con el cálculo sale del rojo",
        healthy("108", median=q.median_for(P0, 5), sale_prices={10: int(calc * 1.08)}), N, "b", {"b": ["b5"]})
    # c
    sc8 = healthy("109", cert="40")
    sc8.median = q.median_for(sc8.our_price, -2)
    add("S08 c1: certificado de 18,1 % del neto", sc8, N, "c", {"c": ["c1"]}, fixes="si", fix_exits=True)
    sc9 = healthy("110", cert="9")
    sc9.median = int((sc9.our_price + P0) / 2 * 1.1 / 0.87)
    add("S09 c2: certificado de 4,7 %, pero sin él sale del rojo", sc9, N, "c", {"c": ["c2"]}, fixes="si", fix_exits=True)
    # d y e
    sc = healthy("111", costs=["30", "28", "25"], box=(40, 30, 20, 4.0), product=(35, 25, 15, 3.0), cbm="0.024", kg="4.0")
    sc.median = q.median_for(sc.our_price, -4)
    add("S10 d: valor bajo por m³", sc, neighbors((40, 30, 20), 4.0), "d", {"d": ["d1"]}, fixes="no")
    sc = healthy("112", costs=["5", "4.5", "4"], box=(12, 8, 6, 0.3), product=(10, 6, 4, 0.2), cbm="0.000576", kg="0.3")
    sc.median = int(5 * D("0.1427") * 1500 * 100) - 3000
    add("S11 e: ML por debajo de la fábrica", sc, neighbors((12, 8, 6), 0.3), "e", {"e": ["e1"]}, fixes="no")
    # combinaciones: manda la primera del orden
    sc = healthy("113", costs=["5", "4.5", "4"], box=(40, 30, 20, 6.0), product=(10, 6, 4, 0.2), cbm="0.024", kg="6.0")
    sc.median = int(5 * D("0.1427") * 1500 * 100) - 3000
    add("S12 a + d + e -> a", sc, neighbors((12, 8, 6), 0.3), "a", {"a": ["a3", "a1", "a2"], "d": ["d1"], "e": ["e1"]})
    sc = healthy("114", costs=["30", "28", "25"], box=(40, 30, 20, 4.0), product=(35, 25, 15, 3.0), cbm="0.024",
                 kg="4.0", cert="60")
    sc.median = q.median_for(sc.our_price, -4)
    add("S13 c + d -> c", sc, neighbors((40, 30, 20), 4.0), "c", {"c": ["c1"], "d": ["d1"]})
    t = [Tier(1, 4, "5", 1, "0.000576", "0.3"), Tier(5, None, "4.5", 5, "0.000576", "0.3")]
    sc = Scenario("115", "v115", t, 1, int(5 * D("0.1427") * 1500 * 100) - 3000, box=(12, 8, 6, 0.3), product=(10, 6, 4, 0.2))
    add("S14 b + e -> b", sc, neighbors((12, 8, 6), 0.3), "b", {"b": ["b4"], "e": ["e1"]})
    add("S20 a2: peso 10 veces el de los parecidos", healthy("116", box=(30, 20, 15, 25.0), kg="25.0",
                                                            median=q.median_for(P0, 0)), N, "a", {"a": ["a2"], "d": ["d1"]})
    add("S18 a3: la caja es más chica que el producto (sin vecinos)", healthy("117", box=(10, 10, 10, 1.0), cbm="0.001",
                                                                          kg="1.0", median=q.median_for(P0, 0)), None,
        "a", {"a": ["a3"]})
    # sin causa
    sc = healthy("118")
    sc.median = q.median_for(sc.our_price, 4)
    add("S17 nada dispara: el match de ML está mal", sc, N, "ninguna", {})
    return out


CATALOG = build_catalog()


@pytest.mark.parametrize("name,sc,neigh,cause,fire,extra", CATALOG, ids=[c[0] for c in CATALOG])
def test_probable_cause_is_the_first_of_a_to_e_and_every_rule_is_stored(world, name, sc, neigh, cause, fire, extra):
    d, out = diag(world, sc, neigh)
    assert fires(d) == fire, d.to_dict()
    assert d.probable_cause == cause
    assert [r.code for r in d.rules] == ["a", "b", "c", "d", "e"]          # se guardan las cinco, siempre en orden
    # la causa probable es exactamente la primera regla que dispara
    assert d.probable_cause == (next(r.code for r in d.rules if r.fires) if fire else "ninguna")
    if "fixes" in extra:
        assert d.rule(cause).fixes == extra["fixes"], d.rule(cause).summary
    assert out.errors == []
    assert d.missing == ([] if neigh is not None else ["productos parecidos"])


def test_fix_it_gets_it_out_of_the_red_is_never_a_false_yes(world):
    """Para cada producto cuya causa es corregible (a-c), se recalcula con el modelo propio el precio que daría CORREGIDO y
    se mira el color real. Un «sí» nunca puede ser falso; un «no» exacto tampoco."""
    checked = 0
    for name, sc, neigh, cause, fire, extra in CATALOG:
        if "fix_exits" not in extra:
            continue
        d, _ = diag(world, sc, neigh)
        rule = d.rule(cause)
        real_after = healthy("0").our_price if cause == "a" else healthy("0").our_price      # el sano (caja/certificado bien)
        real_color = q.color_of(sc.median, real_after)
        exits = real_color != "rojo"
        assert exits is extra["fix_exits"], (name, real_color)
        if rule.fixes == "si":
            assert exits, f"{name}: dice «arreglarla saca del rojo» y no es cierto"
        if rule.fixes == "no":
            assert not exits, f"{name}: dice «no alcanza» y en realidad sale"
        if rule.p_si_cents is not None and cause == "a":
            assert rule.p_si_cents >= real_after - 1, f"{name}: el estimado de (a) promete más ahorro que el real"
        checked += 1
    assert checked == 5


def test_combinations_agree_on_fixable_even_when_the_first_cause_is_structural(world):
    """S13: el certificado explica el rojo y arreglarlo sale (si); (d) también dispara pero no se arregla. fixable mira a-c."""
    name, sc, neigh, cause, fire, _ = next(c for c in CATALOG if c[0].startswith("S13"))
    d, _ = diag(world, sc, neigh)
    assert d.fixable == d.rule("c").fixes
    # el certificado de ese producto sin recargo no lo saca del rojo (d manda): «corregir lo nuestro no alcanza»
    assert d.fix_not_enough == (d.fixable != "si")


# ─── 3. Datos faltantes: nada se inventa ───────────────────────────────────────────────────────────


def test_missing_breakdown_and_neighbors_leave_the_rules_without_data_and_the_cause_unknown(world):
    sc = healthy("15", median=q.median_for(P0, -2))
    world.breakdown_error.add(sc.variant)
    d, out = diag(world, sc, None)
    assert d.probable_cause == "sin_dato"
    assert {r.code: r.status for r in d.rules} == {"a": "sin_dato", "b": "no", "c": "sin_dato", "d": "sin_dato", "e": "sin_dato"}
    assert "desglose de precio" in d.missing and "productos parecidos" in d.missing
    assert out.errors and out.breakdown is None
    assert all(r.p_si_cents is None for r in d.rules)


def test_without_neighbors_a_box_that_is_not_impossible_is_not_called_ok(world):
    d, _ = diag(world, healthy("19", median=q.median_for(P0, 0)), None)
    assert d.rule("a").status == "sin_dato" and d.probable_cause == "sin_dato"


def test_local_stock_only_applies_b_and_c(world):
    sc = healthy("16", median=q.median_for(P0, -2), sourcing="local_stock")
    d, _ = diag(world, sc, neighbors())
    assert {r.code: r.status for r in d.rules}["a"] == "no_aplica"
    assert [r.status for r in d.rules if r.code in "ade"] == ["no_aplica"] * 3
    assert d.rule("b").status == "no" and d.rule("c").status == "no"
    assert d.probable_cause == "ninguna"


def test_local_stock_with_the_zeroed_breakdown_vendure_returns_does_not_look_like_missing_data(world):
    """Vendure contesta ceros para un abastecimiento local (sin dólares que explicar). Con eso (c) no tiene fila que mirar: no
    puede convertir un producto local sano en «diagnóstico incompleto» eterno."""
    tiers = [Tier(1, None, "0", 1, "0", "0", empty=True)]
    sc = Scenario("216", "v216", tiers, 1, q.median_for(3_025_000, -2), box=None, product=None, sourcing="local_stock",
                  sale_prices={1: 2_500_000})
    d, out = diag(world, sc, neighbors())
    statuses = {r.code: r.status for r in d.rules}
    assert statuses["a"] == statuses["d"] == statuses["e"] == "no_aplica"
    assert d.probable_cause in ("ninguna", "b"), (d.probable_cause, statuses)


# ─── 4. El parser del desglose falla explícito ──────────────────────────────────────────────────────


def _bump(raw, key, delta):
    raw = __import__("copy").deepcopy(raw)
    raw[key]["exact"] = str(D(raw[key]["exact"]) + D(delta))
    return raw


GOOD = healthy("0").breakdowns()[0]


def test_parser_accepts_parts_that_differ_by_up_to_one_cent_and_rejects_more():
    """La diferencia se mide contra la factura. Se parte de la diferencia (con signo) que ya trae el desglose y se mueve el neto
    hasta que quede en +/- 0,0100 (se acepta) o +/- 0,0101 (se rechaza). OJO: «IVA de la venta» es factura - neto, así que
    mover la factura NO cambia la suma; lo que se compara de verdad es la suma de los costos contra el neto."""
    g0 = parse_tier_breakdown(GOOD).parts_sum_usd() - D(GOOD["facturaUsd"]["exact"])
    for target in (D("0.0100"), D("-0.0100")):
        assert parse_tier_breakdown(_bump(GOOD, "netoUsd", str(g0 - target))).tier_id == GOOD["tierId"]
    for target in (D("0.0101"), D("-0.0101"), D("0.02"), D("5"), D("-100")):
        with pytest.raises(BreakdownError) as exc:
            parse_tier_breakdown(_bump(GOOD, "netoUsd", str(g0 - target)))
        assert "suman" in str(exc.value) and "tolerancia" in str(exc.value)


@pytest.mark.parametrize("key", ["fobUsd", "fleteUsd", "seguroUsd", "arancelesUsd", "serviciosUsd", "b2boxFeeUsd",
                                 "finCostUsd", "certificationUsd", "paymentGatewayFeeUsd", "netoUsd"])
def test_parser_rejects_any_single_part_that_is_off_by_two_cents(key):
    with pytest.raises(BreakdownError):
        parse_tier_breakdown(_bump(GOOD, key, "0.03"))
    with pytest.raises(BreakdownError):
        parse_tier_breakdown(_bump(GOOD, key, "-0.03"))


def test_the_china_logistics_line_is_inside_the_services_total_so_it_cannot_unbalance_the_sum_alone():
    """Se cuenta en «servicios» y en «logística China», pero la parte 4 es servicios - china: no mueve la suma. Esto documenta
    por qué ese campo no está en la tabla de arriba."""
    assert parse_tier_breakdown(_bump(GOOD, "chinaLogisticsUsd", "0.5")).china_logistics_usd > 0


@pytest.mark.xfail(strict=True, reason="ENDURECIMIENTO (no es un criterio): la parte 10 es factura - neto, así que una factura "
                                       "inconsistente con el neto o con los pesos por unidad pasa el control de «las 10 partes suman»")
def test_parser_notices_a_per_unit_peso_price_that_contradicts_the_dollar_totals():
    raw = __import__("copy").deepcopy(GOOD)
    raw["facturaChannelPerUnit"]["minor"] = int(raw["facturaChannelPerUnit"]["minor"] * 1.5)
    with pytest.raises(BreakdownError):
        parse_tier_breakdown(raw)


@pytest.mark.parametrize("mutate", [
    lambda r: r.pop("fobUsd"), lambda r: r.pop("inputs"), lambda r: r["fobUsd"].update(exact="no-es-numero"),
    lambda r: r["fobUsd"].update(exact="NaN"), lambda r: r["fobUsd"].update(exact="Infinity"),
    lambda r: r["inputs"].update(quantity=0), lambda r: r["inputs"].update(cnyToUsdRate="-1"),
    lambda r: r["netoChannelPerUnit"].update(minor=None), lambda r: r.update(facturaUsd=None),
])
def test_parser_fails_loudly_on_broken_shapes(mutate):
    raw = __import__("copy").deepcopy(GOOD)
    mutate(raw)
    with pytest.raises(BreakdownError):
        parse_tier_breakdown(raw)


def test_all_nine_golden_tiers_of_the_real_engine_add_up():
    import json
    from pathlib import Path

    gold = json.loads((Path(__file__).parent / "golden" / "breakdowns_engine_fdd7579df.json").read_text())
    n = 0
    for v in gold["variants"]:
        for raw in v["breakdowns"]:
            x = lambda k: D(raw[k]["exact"])      # noqa: E731
            by_hand = (x("fobUsd") + x("fleteUsd") + x("seguroUsd") + x("arancelesUsd") + (x("serviciosUsd") - x("chinaLogisticsUsd"))
                       + x("chinaLogisticsUsd") + x("b2boxFeeUsd") + x("finCostUsd") + x("certificationUsd")
                       + x("paymentGatewayFeeUsd") + (x("facturaUsd") - x("netoUsd")))
            assert abs(by_hand - x("facturaUsd")) <= D("0.01")
            bd = parse_tier_breakdown(raw)
            assert abs(bd.parts_sum_usd() - x("facturaUsd")) <= D("0.01")
            n += 1
    assert n == 9


@pytest.mark.xfail(strict=True, reason="BUG QA: un tramo con fábrica, factura y neto en 0 pero con otros montos distintos de cero "
                                       "se da por «sin dólares» y NO falla aunque las partes no sumen (price_breakdown.py:323)")
def test_parser_does_not_hide_a_broken_breakdown_behind_the_no_dollars_shortcut():
    raw = __import__("copy").deepcopy(GOOD)
    for k in ("fobUsd", "facturaUsd", "netoUsd"):
        raw[k]["exact"] = "0"
    with pytest.raises(BreakdownError):
        parse_tier_breakdown(raw)          # quedan flete, aranceles, servicios, fee... > 0: no suman la factura (0)


async def test_through_the_client_a_breakdown_that_does_not_add_up_raises_and_the_diagnosis_says_so(world, monkeypatch):
    from app.pricing import diagnosis_source as ds
    from app.vendure.client import VendureClient

    sc = healthy("1", median=q.median_for(P0, -2))
    world.add(sc)
    orig = Scenario.breakdowns
    monkeypatch.setattr(Scenario, "breakdowns", lambda self: [_bump(b, "netoUsd", "1.5") for b in orig(self)])
    with pytest.raises(BreakdownError):
        await VendureClient().get_tier_breakdowns(sc.variant)
    client = VendureClient()
    out = await ds.diagnose_product(q.snap_of(sc), client, yellow_min_pct=10.0, channel=await client.get_channel_pricing(),
                                    neighbor_boxes_=neighbors())
    assert out.breakdown is None and any("no cierra" in e for e in out.errors)
    assert out.diagnosis.rule("c").status == "sin_dato" and out.diagnosis.probable_cause in ("sin_dato", "a", "b")


# ─── 5. Texto ───────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.xfail(strict=True, reason="BUG QA (cosmético): la frase de (b) pasa por str.capitalize() y deja «vendure» en minúscula "
                                       "(y cualquier sigla): «...del que da el cálculo de vendure» (price_diagnosis.py:639)")
def test_the_text_of_b_keeps_the_names_it_mentions_capitalized(world):
    calc = healthy("0").used()["netoChannelPerUnit"]["minor"]
    d, _ = diag(world, healthy("108", median=q.median_for(P0, 5), sale_prices={10: int(calc * 1.08)}), neighbors())
    assert "de vendure" not in d.rule("b").summary, d.rule("b").summary


# ─── Solo Argentina: todas las lecturas nuevas van al canal configurado ──────────────────────────────


async def test_every_new_read_carries_the_channel_token_so_the_numbers_are_the_argentina_ones(world, monkeypatch):
    """Decisión de Nico: b2box.app vende solo en Argentina. Las 4 lecturas nuevas (y la del listado) tienen que ir con el header
    `vendure-token` del canal configurado: sin él Vendure contestaría con el canal por DEFECTO (otras tarifas, otro fee)."""
    from app.config import Settings
    from app.pricing import diagnosis_source as ds
    from app.vendure import client as vendure_client
    from app.vendure.client import VendureClient

    cfg = Settings(vendure_api_url="https://example.invalid/admin-api", vendure_channel_token="ar-token")
    monkeypatch.setattr(vendure_client, "get_settings", lambda: cfg)
    sc = healthy("1", median=q.median_for(P0, -2))
    world.add(sc)
    client = VendureClient()
    channel = await client.get_channel_pricing()
    await ds.diagnose_product(q.snap_of(sc), client, yellow_min_pct=10.0, channel=channel, neighbor_boxes_=neighbors())
    await client.fetch_all_products_priced()
    reads = [r for r in world.spy.requests if r["host"] == VendureWorld.HOST]
    ops = {__import__("re").match(r"\s*query\s+(\w+)", __import__("json").loads(r["body"])["query"]).group(1) for r in reads}
    assert {"ActiveChannelPricing", "FiscalProfile", "TierBreakdowns", "PricedVariant", "ProductCertification", "Products"} <= ops
    assert {r["channel"] for r in reads} == {"ar-token"}
