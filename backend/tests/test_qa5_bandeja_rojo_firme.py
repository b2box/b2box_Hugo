"""QA independiente (criterio 2): «rojo firme» con bordes exactos y combinaciones, la racha por noche, el drone, los
deshabilitados y los datos viejos de la oficina. Snapshots armados con `qa5_bandeja.mk_snap` (no los del developer)."""

from __future__ import annotations

import os
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app.db.models import MarketPriceSnapshot, PriceReviewEvent, PriceReviewItem  # noqa: E402
from app.pricing import price_review as pr  # noqa: E402
from tests import qa5_bandeja as q  # noqa: E402
from tests.qa5_bandeja import NOW, entry, mk_snap  # noqa: E402

P = pr.ReviewParams()           # 30 / 10 / 48 h / 3 publicaciones / 2 vendedores / x3 / 2 noches


def verdict(snap, params=P, now=NOW):
    return pr.evaluate_firm(snap, params, now)


def reds(*listings, **kw):
    return mk_snap(None, "p", listings=listings, **kw)


# ─── La base: lo que cumple todo ───────────────────────────────────────────────────────────────


def test_the_base_case_is_a_firm_red_by_the_semaforo_rules_written_apart():
    s = mk_snap(None, "p")
    assert s.color == "rojo" and s.est_margin_pct == pytest.approx(-21.7, abs=0.01)    # (900.000 x 0,87 - 1.000.000) / 1.000.000
    v = verdict(s)
    assert v.firm and v.firm_median == 900_000 and v.firm_sellers == 3 and v.firm_listings == 3
    assert v.price_ratio == pytest.approx(1.1111, abs=1e-4)


# ─── R1: rojo real, con dato, habilitado. Nunca el estimado ───────────────────────────────────────


@pytest.mark.parametrize("our,color", [(600_000, "verde"), (700_000, "amarillo")])
def test_r1_only_a_real_red_counts(our, color):
    s = mk_snap(None, "p", our=our)
    assert s.color == color                            # margen 30,5 % / 11,9 %
    assert verdict(s).status == pr.NO_ROJO


def test_r1_the_yellow_border_is_exact():
    """P_max = 900.000 x 0,87 / 1,10 = 711.818,18: 711.818 es amarillo (margen >= 10 %) y 711.819 es rojo."""
    assert mk_snap(None, "p", our=711_818).color == "amarillo"
    s = mk_snap(None, "p", our=711_819)
    assert s.color == "rojo" and verdict(s).status in (pr.FIRME, pr.DUDOSO)


def test_r1_the_estimated_color_alone_never_makes_a_firm_red():
    s = mk_snap(None, "p", ml_status="no_data", listings=(), color="sin_dato", estimated_color="rojo")
    assert verdict(s).status == pr.NO_ROJO
    s2 = mk_snap(None, "p", listings=((900_000, "clip", "a"),) * 3, color="amarillo", estimated_color="rojo", our=750_000)
    assert verdict(s2).status == pr.NO_ROJO            # el color real manda aunque el estimado diga rojo


@pytest.mark.parametrize("status", ["no_data", "failed", "skipped"])
def test_r1_a_red_without_ml_data_is_never_firm(status):
    s = mk_snap(None, "p", ml_status=status, listings=(), color="rojo")        # rojo que solo pueden haber puesto las tiendas
    v = verdict(s)
    assert v.status == pr.DUDOSO and v.reason == "solo_tiendas"


def test_r1_a_disabled_product_does_not_enter():
    v = verdict(mk_snap(None, "p", enabled=False))
    assert v.status == pr.DUDOSO and v.reason == "deshabilitado"


# ─── R3: las fuentes. Solo foto+nombre o una persona ──────────────────────────────────────────────


@pytest.mark.parametrize("source", ["clip", "clip+nombre", "manual"])
def test_r3_the_three_firm_sources(source):
    assert verdict(reds((900_000, source, "a"), (905_000, source, "b"), (895_000, source, "c"))).firm


@pytest.mark.parametrize("source", ["llm", "ambiguo", "specs", "", None, "otra"])
def test_r3_anything_else_is_not_firm(source):
    v = verdict(reds((900_000, source, "a"), (905_000, source, "b"), (895_000, source, "c")))
    assert v.status == pr.DUDOSO and v.reason == "fuentes"


def test_r3_a_judge_only_listing_cannot_be_the_third_one():
    v = verdict(reds((900_000, "clip", "a"), (905_000, "clip+nombre", "b"), (895_000, "llm", "c")))
    assert v.status == pr.DUDOSO and v.reason == "pocas_publicaciones"          # quedan 2 firmes


def test_r3_a_person_confirmed_listing_counts_as_the_third_one():
    assert verdict(reds((900_000, "clip", "a"), (905_000, "clip", "b"), (895_000, "manual", "c"))).firm


def test_r3_the_red_must_hold_with_the_firm_listings_alone():
    """3 del juez baratas (rojo) + 3 de foto+nombre caras: con TODAS es rojo, con las firmes solas no -> dudoso: fuentes."""
    s = mk_snap(None, "p", our=1_000_000, listings=(
        (800_000, "llm", "a"), (810_000, "llm", "b"), (790_000, "llm", "c"),
        (1_300_000, "clip", "d"), (1_310_000, "clip", "e"), (1_290_000, "clip", "f")))
    assert s.color == "rojo"                                   # mediana 900.000 -> rojo con todas
    v = verdict(s)
    assert v.status == pr.DUDOSO and v.reason == "fuentes"


# ─── R4: el rojo se sostiene solo con ML ──────────────────────────────────────────────────────────


def test_r4_a_red_that_only_exists_because_of_the_stores_is_not_firm():
    s = mk_snap(None, "p", our=1_000_000, listings=((1_400_000, "clip", "a"), (1_410_000, "clip", "b"), (1_390_000, "clip", "c")),
                color="rojo", basis="ml+tiendas")
    v = verdict(s)
    assert v.status == pr.DUDOSO and v.reason == "tiendas"


def test_r4_basis_stores_with_ml_data_ok_but_no_prices_is_doubtful():
    s = mk_snap(None, "p", ml_status="ok", entries=[], color="rojo", basis="tiendas")
    assert verdict(s).reason in ("tiendas", "solo_tiendas")


def test_r4_a_red_that_ml_also_sustains_is_firm_even_if_the_stores_took_part():
    assert verdict(mk_snap(None, "p", basis="ml+tiendas")).firm


# ─── R5: frescura. Menos de 48 h, borde exacto ─────────────────────────────────────────────────


@pytest.mark.parametrize("age,firm", [
    (timedelta(hours=1), True), (timedelta(hours=47, minutes=59, seconds=59), True),
    (timedelta(hours=48), False), (timedelta(hours=48, seconds=1), False), (timedelta(days=5), False),
    (timedelta(days=7), False), (timedelta(0), True)])
def test_r5_less_than_48_hours_exactly(age, firm):
    v = verdict(mk_snap(None, "p", data_at=NOW - age))
    assert v.firm is firm and (firm or v.reason == "dato_viejo")


@pytest.mark.parametrize("hours,age_h,firm", [(24, 23.99, True), (24, 24.0, False), (168, 167.9, True), (168, 168.0, False),
                                              (6, 5.9, True), (6, 6.0, False)])
def test_r5_the_limit_is_the_setting(hours, age_h, firm):
    p = pr.ReviewParams(data_max_age_h=hours)
    assert verdict(mk_snap(None, "p", data_at=NOW - timedelta(hours=age_h)), p).firm is firm


def test_r5_office_data_of_5_days_does_not_count_even_if_the_run_is_from_today():
    s = mk_snap(None, "p", origin="oficina", web_via="oficina", data_at=NOW - timedelta(days=5), captured_at=NOW)
    v = verdict(s)
    assert v.status == pr.DUDOSO and v.reason == "dato_viejo"
    # hasta 7 días (el TTL de la oficina) tampoco
    s7 = mk_snap(None, "p", origin="oficina", web_via="oficina", data_at=NOW - timedelta(days=7), captured_at=NOW)
    assert verdict(s7).reason == "dato_viejo"


def test_r5_office_data_of_47_hours_is_fresh_enough():
    s = mk_snap(None, "p", origin="oficina", web_via="oficina", data_at=NOW - timedelta(hours=47))
    assert verdict(s).firm


def test_r5_office_data_without_a_date_is_never_fresh_but_server_data_without_a_date_takes_the_capture_time():
    off = mk_snap(None, "p", origin="oficina", web_via="oficina", data_at=None, captured_at=NOW)
    assert verdict(off).reason == "dato_viejo"
    off2 = mk_snap(None, "p", origin="api", web_via="oficina", data_at=None, captured_at=NOW)
    assert verdict(off2).reason == "dato_viejo"                  # web_via oficina alcanza
    api = mk_snap(None, "p", origin="api", data_at=None, captured_at=NOW - timedelta(hours=2))
    assert verdict(api).firm
    api_old = mk_snap(None, "p", origin="api", data_at=None, captured_at=NOW - timedelta(hours=60))
    assert verdict(api_old).reason == "dato_viejo"


# ─── R6: evidencia mínima ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("n,firm", [(1, False), (2, False), (3, True), (4, True), (10, True)])
def test_r6_three_listings(n, firm):
    ls = tuple((900_000 + 1000 * i, "clip", f"s{i}") for i in range(n))
    v = verdict(reds(*ls))
    assert v.firm is firm and (firm or v.reason == "pocas_publicaciones")


@pytest.mark.parametrize("sellers,firm", [(("a", "a", "a"), False), (("a", "a", "b"), True), (("a", "b", "c"), True),
                                          (("a", "", "a"), False)])
def test_r6_two_distinct_sellers(sellers, firm):
    ls = tuple((900_000 + 1000 * i, "clip", s) for i, s in enumerate(sellers))
    v = verdict(reds(*ls))
    assert v.firm is firm and (firm or v.reason == "pocos_vendedores")


def test_r6_one_catalog_entry_with_three_offers_counts_as_three_listings_and_its_sellers():
    es = [entry(1, 900_000, "clip", None, prices=[890_000, 900_000, 910_000], sellers=["x", "y", "z"])]
    assert verdict(mk_snap(None, "p", entries=es)).firm


def test_r6_the_minimums_are_the_settings():
    p = pr.ReviewParams(min_listings=4, min_sellers=3)
    assert verdict(reds(*OK3()), p).reason == "pocas_publicaciones"
    p = pr.ReviewParams(min_listings=1, min_sellers=1)
    assert verdict(reds((900_000, "clip", "a")), p).firm


def OK3():
    return ((900_000, "clip", "a"), (905_000, "clip", "b"), (895_000, "clip", "c"))


def test_r6_the_same_seller_written_in_two_cases_is_one_seller():
    v = verdict(reds((900_000, "clip", "TiendaX"), (905_000, "clip", "tiendax"), (895_000, "clip", "TIENDAX ")))
    assert v.status == pr.DUDOSO and v.reason == "pocos_vendedores"


# ─── R7: precio creíble, hasta 3 veces la mediana ───────────────────────────────────────────────


@pytest.mark.parametrize("our,firm", [(2_700_000, True), (2_700_001, True), (2_999_999, True), (3_000_000, True),
                                      (3_000_001, False), (3_100_000, False), (9_000_000, False)])
def test_r7_exactly_three_times_the_firm_median(our, firm):
    s = mk_snap(None, "p", our=our, listings=((1_000_000, "clip", "a"), (1_000_000, "clip", "b"), (1_000_000, "clip", "c")))
    v = verdict(s)
    assert v.firm is firm and (firm or v.reason == "precio_extremo"), v.price_ratio


def test_r7_the_drone_against_the_toy_plane_at_minus_99_percent_does_not_enter():
    s = mk_snap(None, "p", our=9_000_000, listings=((90_000, "clip", "a"), (91_000, "clip", "b"), (89_000, "clip", "c")))
    assert s.color == "rojo" and s.est_margin_pct < -99
    v = verdict(s)
    assert v.status == pr.DUDOSO and v.reason == "precio_extremo" and v.flag == "dudoso:precio_extremo"


@pytest.mark.parametrize("ratio,firm", [(1.49, True), (1.5, True), (1.51, False)])
def test_r7_the_limit_is_the_setting(ratio, firm):
    p = pr.ReviewParams(max_price_ratio=1.5)
    s = mk_snap(None, "p", our=int(1_000_000 * ratio), listings=((1_000_000, "clip", "a"),) * 3 + ())
    s2 = mk_snap(None, "p", our=int(1_000_000 * ratio), listings=((1_000_000, "clip", "a"), (1_000_000, "clip", "b"), (1_000_000, "clip", "c")))
    assert verdict(s2, p).firm is firm


def test_r7_uses_the_median_of_the_firm_listings_not_the_one_of_the_whole_snapshot():
    """Con 3 baratas del juez (no firmes) la mediana de todo es chica y el cociente se dispara; con las firmes (caras) es creíble."""
    s = mk_snap(None, "p", our=1_000_000, listings=((100_000, "llm", "a"), (100_000, "llm", "b"), (100_000, "llm", "c"),
                                                    (900_000, "clip", "d"), (905_000, "clip", "e"), (895_000, "clip", "f")))
    v = verdict(s)
    assert v.firm and v.firm_median == 900_000 and v.price_ratio == pytest.approx(1.111, abs=1e-3)


# ─── Combinaciones: manda la primera regla que falla, en el orden del diseño ─────────────────────


def test_combinations_report_the_first_failing_rule_and_never_let_two_failures_cancel_out():
    old = NOW - timedelta(days=5)
    cases = [
        (dict(enabled=False, data_at=old), "deshabilitado"),
        (dict(listings=((9e5, "llm", "a"),) * 3 if False else ((900_000, "llm", "a"),) * 3, data_at=old), "fuentes"),
        (dict(data_at=old, listings=((900_000, "clip", "a"), (905_000, "clip", "a"))), "dato_viejo"),
        (dict(listings=((900_000, "clip", "a"), (905_000, "clip", "a")), our=9_000_000), "pocas_publicaciones"),
        (dict(listings=((90_000, "clip", "a"), (91_000, "clip", "a"), (89_000, "clip", "a")), our=9_000_000), "pocos_vendedores"),
    ]
    for kw, reason in cases:
        v = verdict(mk_snap(None, "p", **kw))
        assert v.status == pr.DUDOSO and v.reason == reason, (kw, v.reason)


# ─── Nocturno: racha por noche, deshabilitados, drone y oficina, contra la base ───────────────────


@pytest.fixture(autouse=True)
def db(monkeypatch):
    q.reset_db()
    q.setting("pm_mode", 1)
    world = q.VendureWorld()
    world.spy = q.install_spy(monkeypatch, world)
    yield world
    assert world.writes() == [] and world.spy.violations == []


async def night(pid_kwargs: dict[str, dict], day: int, *, status="ok", hour: int = 6, run=None, flag="auto"):
    """Una corrida en el día `day` (0 = hoy 2026-10-14) con los productos dados, y su pasada. Devuelve el run_id."""
    started = (NOW + timedelta(days=day)).replace(hour=hour, minute=0)
    rid = run or q.new_run(started, status="ok")
    for pid, kw in pid_kwargs.items():
        kw = dict(kw)
        kw.setdefault("data_at", started - timedelta(hours=1))
        kw.setdefault("captured_at", started)
        q.save(mk_snap(rid, pid, **kw))
    await pr.process_run(rid, now=started + timedelta(hours=1))
    return rid


def fichas():
    return sorted(i.product_id for i in q.rows(PriceReviewItem))


async def test_streak_red_red_gives_a_ficha_on_the_second_night_only():
    await night({"a": {}}, -1)
    assert fichas() == []
    await night({"a": {}}, 0)
    assert fichas() == ["a"]


async def test_streak_red_failed_red_gives_a_ficha_on_the_third_run():
    await night({"a": {}}, -2)
    await night({"a": dict(ml_status="failed", listings=(), color="sin_dato")}, -1)
    assert fichas() == []
    await night({"a": {}}, 0)
    assert fichas() == ["a"]


async def test_streak_a_skipped_night_neither_adds_nor_subtracts():
    await night({"a": {}}, -2)
    await night({"a": dict(ml_status="skipped", listings=(), color="sin_dato")}, -1)
    await night({"a": {}}, 0)
    assert fichas() == ["a"]


@pytest.mark.parametrize("middle", [
    dict(our=600_000), dict(our=700_000),                                    # verde, amarillo
    dict(ml_status="no_data", listings=(), color="sin_dato"),                # ML no encontró nada: corta
    dict(listings=((900_000, "llm", "a"),) * 3),                             # rojo dudoso por fuentes
    dict(data_at=NOW - timedelta(days=9)),                                   # rojo dudoso por dato viejo
    dict(enabled=False)])
async def test_streak_red_something_else_red_gives_no_ficha(middle):
    await night({"a": {}}, -2)
    await night({"a": middle}, -1)
    await night({"a": {}}, 0)
    assert fichas() == []


async def test_streak_with_three_nights_required():
    q.setting("pm_review_confirm_nights", 3)
    await night({"a": {}}, -2)
    await night({"a": {}}, -1)
    assert fichas() == []
    await night({"a": {}}, 0)
    assert fichas() == ["a"]


async def test_streak_failed_nights_in_between_do_not_count_toward_three():
    q.setting("pm_review_confirm_nights", 3)
    await night({"a": {}}, -3)
    await night({"a": dict(ml_status="failed", listings=(), color="sin_dato")}, -2)
    await night({"a": {}}, -1)
    assert fichas() == []                       # 2 noches con dato
    await night({"a": {}}, 0)
    assert fichas() == ["a"]


async def test_two_runs_on_the_same_day_are_one_night():
    """El cron de las 06:00 y un «Correr ahora» de las 15:00: una sola noche. Dos corridas de hoy no alcanzan."""
    await night({"a": {}}, 0, hour=6)
    await night({"a": {}}, 0, hour=15)
    assert fichas() == []


async def test_a_first_night_run_plus_two_runs_the_next_day_make_exactly_one_ficha_and_one_event():
    await night({"a": {}}, -1)
    await night({"a": {}}, 0, hour=6)
    await night({"a": {}}, 0, hour=15)
    assert fichas() == ["a"] and len([e for e in q.rows(PriceReviewEvent) if e.kind == "propuesto"]) == 1


async def test_two_runs_twenty_minutes_apart_across_midnight_utc_are_not_two_nights():
    rid1 = q.new_run(NOW.replace(day=13, hour=23, minute=50))
    q.save(mk_snap(rid1, "a", data_at=NOW.replace(day=13, hour=23), captured_at=NOW.replace(day=13, hour=23, minute=50)))
    await pr.process_run(rid1, now=NOW.replace(day=13, hour=23, minute=55))
    rid2 = q.new_run(NOW.replace(day=14, hour=0, minute=10))
    q.save(mk_snap(rid2, "a", data_at=NOW.replace(day=14, hour=0), captured_at=NOW.replace(day=14, hour=0, minute=10)))
    await pr.process_run(rid2, now=NOW.replace(day=14, hour=0, minute=15))
    assert fichas() == []


async def test_a_product_that_was_disabled_never_gets_a_ficha_even_with_two_red_nights():
    await night({"a": dict(enabled=False)}, -1)
    await night({"a": dict(enabled=False)}, 0)
    assert fichas() == []
    snaps = q.rows(MarketPriceSnapshot, MarketPriceSnapshot.product_id == "a")
    assert {s.review_flag for s in snaps} == {"dudoso:deshabilitado"}


async def test_a_product_disabled_after_its_ficha_opened_closes_the_ficha():
    await night({"a": {}}, -1)
    await night({"a": {}}, 0)
    assert fichas() == ["a"]
    await night({"a": dict(enabled=False)}, 1)
    [item] = q.rows(PriceReviewItem)
    assert item.state == "cerrado_auto" and item.open_key is None


async def test_the_drone_never_gets_a_ficha_in_any_mode_and_leaves_the_reason_in_the_snapshot():
    drone = dict(our=9_000_000, listings=((90_000, "clip", "a"), (91_000, "clip", "b"), (89_000, "clip", "c")))
    for mode in (1, 2):
        q.reset_db()
        q.setting("pm_mode", mode)
        await night({"drone": drone}, -1)
        rid = await night({"drone": drone}, 0)
        assert fichas() == [] and q.rows(PriceReviewEvent) == []
        [s] = q.rows(MarketPriceSnapshot, MarketPriceSnapshot.run_id == rid)
        assert s.review_flag == "dudoso:precio_extremo" and s.est_margin_pct < -99


async def test_stale_office_data_never_gets_a_ficha_even_after_many_nights():
    old = dict(origin="oficina", web_via="oficina")
    for day in (-3, -2, -1, 0):
        started = NOW + timedelta(days=day)
        await night({"a": dict(old, data_at=started - timedelta(days=5))}, day)
    assert fichas() == []


async def test_office_data_of_47_hours_two_nights_in_a_row_does_get_a_ficha():
    for day in (-1, 0):
        started = NOW + timedelta(days=day)
        await night({"a": dict(origin="oficina", web_via="oficina", data_at=started - timedelta(hours=47))}, day)
    [item] = q.rows(PriceReviewItem)
    assert "oficina" in item.flags


async def test_mode_0_marks_the_snapshots_but_creates_nothing():
    q.setting("pm_mode", 0)
    await night({"a": {}}, -1)
    rid = await night({"a": {}}, 0)
    assert fichas() == [] and q.rows(PriceReviewEvent) == []
    [s] = q.rows(MarketPriceSnapshot, MarketPriceSnapshot.run_id == rid)
    assert s.review_flag == "firme"


async def test_mode_2_is_still_proposal_only():
    q.setting("pm_mode", 2)
    await night({"a": {}}, -1)
    await night({"a": {}}, 0)
    [item] = q.rows(PriceReviewItem)
    assert item.state == "propuesto" and item.hugo_disabled is None and item.mode_at_open == 1


async def test_a_ficha_already_open_blocks_a_second_one_and_the_unique_key_holds():
    await night({"a": {}}, -2)
    await night({"a": {}}, -1)
    await night({"a": {}}, 0)
    assert len(q.rows(PriceReviewItem)) == 1


async def test_a_crash_in_the_middle_of_the_pass_then_a_retry_leaves_one_ficha_per_product_and_one_review_at(monkeypatch):
    names = {"a", "b", "c"}
    await night({n: {} for n in names}, -1)
    rid = q.new_run(NOW)
    for n in sorted(names):
        q.save(mk_snap(rid, n))
    real, calls = pr._create_item, []

    def boom(plan, cand, pdiag):
        calls.append(cand.snap.product_id)
        if len(calls) == 2:
            raise RuntimeError("se cayó el proceso")
        return real(plan, cand, pdiag)

    monkeypatch.setattr(pr, "_create_item", boom)
    with pytest.raises(RuntimeError):
        await pr.process_run(rid, now=NOW + timedelta(hours=1))
    assert q.rows(__import__("app.db.models", fromlist=["PriceMonitorRun"]).PriceMonitorRun,
                  __import__("app.db.models", fromlist=["PriceMonitorRun"]).PriceMonitorRun.id == rid)[0].review_at is None
    monkeypatch.setattr(pr, "_create_item", real)
    await pr.process_run(rid, now=NOW + timedelta(hours=1))
    assert fichas() == ["a", "b", "c"]
    assert sorted(e.product_id for e in q.rows(PriceReviewEvent)) == ["a", "b", "c"]
    again = await pr.process_run(rid, now=NOW + timedelta(hours=1))
    assert again is None and len(q.rows(PriceReviewEvent)) == 3
