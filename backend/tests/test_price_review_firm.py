"""Rojo firme (PR2 de la bandeja): cada condición por separado, incluido el caso del drone con -99 % que NO entra.

`evaluate_firm` es una función pura sobre un snapshot; la racha (R8) y la ficha abierta (R2) las prueba
`test_price_review_run.py`.
"""

from __future__ import annotations

import os
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app.db.models import MarketPriceSnapshot  # noqa: E402
from app.pricing import price_review as pr  # noqa: E402
from tests import review_world as w  # noqa: E402

P = pr.ReviewParams()                      # los valores por defecto del diseño
NOW = w.NOW


def verdict(**kw) -> pr.FirmVerdict:
    """Arma el snapshot en memoria (sin tocar la base) y lo evalúa."""
    run_id = kw.pop("run_id", 1)
    now = kw.pop("now", NOW)
    params = kw.pop("params", P)
    sid = _build(run_id, **kw)
    return pr.evaluate_firm(sid, params, now)


def _build(run_id: int, **kw) -> MarketPriceSnapshot:
    # add_snap persiste; para evaluar alcanza con leerlo de vuelta
    w.reset_db()
    rid = w.make_run()
    w.add_snap(rid, "x", **kw)
    return w.snap_of(rid, "x")


def test_the_default_snapshot_is_a_firm_red():
    v = verdict()
    assert v.firm and v.flag == "firme" and v.reason is None
    assert v.firm_listings == 3 and v.firm_sellers == 3 and v.firm_median == 900_000
    assert v.price_ratio == pytest.approx(1_000_000 / 900_000) and v.age_h == 0.0


# ─── R1: rojo real, con dato, habilitado ──────────────────────────


@pytest.mark.parametrize("color", ["verde", "amarillo", "sin_dato"])
def test_r1_only_red_counts(color):
    v = verdict(color=color)
    assert v.status == "no_rojo" and v.flag is None


def test_r1_the_estimated_color_never_counts():
    """Un producto sin idénticos con color ESTIMADO rojo no es un rojo (el estimado vive en otros campos)."""
    v = verdict(color="sin_dato", ml_status="no_data", prices=(), estimated_color="rojo", estimated_margin_pct=-30.0)
    assert v.status == "no_rojo"


def test_r1_a_red_without_ml_data_is_only_the_stores():
    v = verdict(ml_status="no_data", prices=(), basis="tiendas")
    assert v.status == "dudoso" and v.reason == "solo_tiendas" and v.flag == "dudoso:solo_tiendas"


def test_r1_ml_failed_or_skipped_are_not_firm():
    for status in ("failed", "skipped"):
        assert not verdict(ml_status=status, prices=()).firm


def test_r1_a_disabled_product_is_not_proposed():
    v = verdict(enabled=False)
    assert v.reason == "deshabilitado" and not v.firm


# ─── R3: solo idénticos confirmados por foto + nombre o por una persona ────


@pytest.mark.parametrize("source", ["clip", "clip+nombre", "manual"])
def test_r3_the_three_firm_sources_count(source):
    assert verdict(sources=(source,) * 3).firm


@pytest.mark.parametrize("source", ["llm", "specs", "ambiguo", None])
def test_r3_a_listing_confirmed_only_by_the_judge_does_not_count(source):
    v = verdict(sources=(source,) * 3)
    assert v.status == "dudoso" and v.reason == "fuentes" and v.firm_listings == 0


def test_r3_two_firm_and_one_judge_listing_leaves_two_firm_ones():
    v = verdict(sources=("clip", "clip", "llm"))
    assert v.reason == "pocas_publicaciones" and v.firm_listings == 2


def test_r3_if_without_the_judge_listing_it_is_no_longer_red_it_is_not_firm():
    """Los firmes (clip) cobran caro: con ellos solos el producto NO es rojo; el rojo lo producía el juez."""
    entries = [w.listing(1, 1_400_000, source="clip"), w.listing(2, 1_410_000, source="clip"),
               w.listing(3, 700_000, source="llm"), w.listing(4, 705_000, source="llm"), w.listing(5, 710_000, source="llm")]
    v = verdict(matched=entries, our=1_000_000)
    assert v.reason == "fuentes" and not v.firm


# ─── R4: el rojo se sostiene solo con ML ──────────────────────────


def test_r4_if_the_stores_made_it_red_it_is_not_firm():
    """Hugo guardó rojo porque mezcló precios de tiendas; con los de ML solos (por encima de lo nuestro) no es rojo."""
    v = verdict(prices=(1_400_000, 1_410_000, 1_390_000), basis="ml+tiendas")
    assert v.status == "dudoso" and v.reason == "tiendas"


def test_r4_an_inconsistent_red_without_stores_says_so():
    v = verdict(prices=(1_400_000, 1_410_000, 1_390_000), basis="ml")
    assert v.reason == "recalculo"


# ─── R5: dato de menos de 48 h ────────────────────────────────────


@pytest.mark.parametrize("age_h,firm", [(0, True), (47.9, True), (48.0, False), (120.0, False)])
def test_r5_the_data_must_be_younger_than_48_hours(age_h, firm):
    v = verdict(origin="oficina", data_at=NOW - timedelta(hours=age_h))
    assert v.firm is firm
    if not firm:
        assert v.reason == "dato_viejo"


def test_r5_an_office_listing_of_5_days_is_not_firm():
    v = verdict(origin="oficina", data_at=NOW - timedelta(days=5))
    assert v.flag == "dudoso:dato_viejo"


def test_r5_office_data_without_a_date_cannot_be_called_fresh_but_server_data_can():
    assert verdict(origin="oficina", data_at=None).reason == "dato_viejo"
    assert verdict(origin="api", data_at=None, captured_at=NOW - timedelta(hours=1)).firm


def test_r5_the_age_limit_is_a_setting():
    strict = pr.ReviewParams(data_max_age_h=6)
    assert verdict(params=strict, data_at=NOW - timedelta(hours=7)).reason == "dato_viejo"


# ─── R6: 3 publicaciones firmes y 2 vendedores ────────────────────


@pytest.mark.parametrize("n,firm", [(1, False), (2, False), (3, True), (5, True)])
def test_r6_needs_three_firm_listings(n, firm):
    prices = (900_000, 905_000, 895_000, 910_000, 890_000)[:n]
    sellers = tuple(f"s{i}" for i in range(n))
    v = verdict(prices=prices, sellers=sellers)
    assert v.firm is firm
    if not firm:
        assert v.reason == "pocas_publicaciones"


@pytest.mark.parametrize("sellers,firm", [(("a", "a", "a"), False), (("a", "a", "b"), True), (("a", "b", "c"), True)])
def test_r6_needs_two_different_sellers(sellers, firm):
    v = verdict(sellers=sellers)
    assert v.firm is firm
    if not firm:
        assert v.reason == "pocos_vendedores" and v.firm_sellers == 1


def test_r6_the_minimums_are_settings():
    assert verdict(params=pr.ReviewParams(min_listings=4)).reason == "pocas_publicaciones"
    assert verdict(params=pr.ReviewParams(min_sellers=4)).reason == "pocos_vendedores"


# ─── R7: nuestro precio no puede ser más de 3 veces el de ML ──────


def test_r7_the_drone_against_the_toy_plane_at_minus_99_percent_does_not_enter():
    """El caso real: nuestro drone contra un avión de juguete de ML dio -99 %. Cociente ~100: casi seguro el match está mal."""
    v = verdict(prices=(90_000, 91_000, 89_000), our=9_000_000)
    assert v.status == "dudoso" and v.reason == "precio_extremo" and v.flag == "dudoso:precio_extremo"
    assert v.price_ratio == pytest.approx(100.0)
    margin = (90_000 * 0.87 - 9_000_000) / 9_000_000 * 100
    assert margin < -99


@pytest.mark.parametrize("our,firm", [(1_000_000, True), (2_700_000, True), (2_700_001, False), (9_000_000, False)])
def test_r7_the_ratio_limit_is_three_inclusive(our, firm):
    v = verdict(prices=(900_000,) * 3, our=our, sellers=("a", "b", "c"))
    assert v.firm is firm
    assert (v.reason is None) is firm and (firm or v.reason == "precio_extremo")


def test_r7_exactly_three_times_is_still_firm():
    assert verdict(prices=(300_000,) * 3, our=900_000).firm
    assert verdict(prices=(300_000,) * 3, our=900_001).reason == "precio_extremo"


def test_r7_the_limit_is_a_setting():
    assert verdict(params=pr.ReviewParams(max_price_ratio=1.05)).reason == "precio_extremo"


# ─── el orden de los motivos es el del diseño ─────────────────────


def test_the_first_failing_rule_gives_the_reason():
    v = verdict(enabled=False, origin="oficina", data_at=NOW - timedelta(days=9), prices=(90_000,) * 3, our=9_000_000)
    assert v.reason == "deshabilitado"
    v = verdict(sources=("llm",) * 3, origin="oficina", data_at=NOW - timedelta(days=9), our=9_000_000, prices=(90_000,) * 3)
    assert v.reason == "fuentes"             # R3 antes que R5 y R7
    v = verdict(origin="oficina", data_at=NOW - timedelta(days=9), our=9_000_000, prices=(90_000,) * 3)
    assert v.reason == "dato_viejo"


def test_the_reason_texts_carry_the_configured_numbers():
    p = pr.ReviewParams(data_max_age_h=24, min_listings=5, max_price_ratio=2)
    assert "24" in p.reason_text("dato_viejo") and "5" in p.reason_text("pocas_publicaciones")
    assert "2" in p.reason_text("precio_extremo")
    assert all(p.reason_text(r) for r in pr.REASONS)


@pytest.mark.parametrize("sellers,distinct", [
    (("TiendaX", "tiendax", "TIENDAX "), 1),
    ((" Tienda  X", "tienda x", "TIENDA\tX"), 1),
    (("Straße", "STRASSE", "strasse"), 1),                      # casefold, no lower
    (("TiendaX", "tiendax", "OtraTienda"), 2),
    (("a", "b", "c"), 3),
])
def test_r6_sellers_are_compared_without_case_or_extra_spaces(sellers, distinct):
    v = verdict(sellers=sellers)
    assert v.firm_sellers == distinct
    assert (v.firm is True) is (distinct >= 2)
    if distinct < 2:
        assert v.reason == "pocos_vendedores"


def test_r6_an_empty_seller_name_is_not_a_seller():
    entries = [w.listing(i, 900_000 + i) for i in (1, 2, 3)]
    for e, name in zip(entries, ("", "   ", "a")):
        e["sellers"] = [name]
    v = verdict(matched=entries)
    assert v.firm_sellers == 1 and v.reason == "pocos_vendedores"
