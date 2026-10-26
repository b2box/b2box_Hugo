"""La pasada de la noche (PR2 de la bandeja): puerta de entrada, racha, idempotencia y cero escrituras.

Vendure es el doble HTTP de `review_fixtures` (cualquier mutation queda registrada y se contesta con un 500);
la base es la SQLite de la suite.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.db.models import EventLogIsAppendOnly, PriceReviewEvent, PriceReviewItem  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.integrations import paco  # noqa: E402
from app.pricing import price_review as pr  # noqa: E402
from app.vendure.client import VendureClient  # noqa: E402
from tests import review_world as w  # noqa: E402
from tests.review_fixtures import FakeReviewVendure, engine_variant, install  # noqa: E402

NOW = w.NOW


@pytest.fixture(autouse=True)
def world(monkeypatch):
    w.reset_db()
    w.set_setting("pm_mode", 1)

    async def forbidden(*_a, **_k):
        raise AssertionError("la bandeja no puede escribir en Vendure ni mandar nada a Paco")

    for name in ("_set_enabled", "disable_product", "enable_product"):
        monkeypatch.setattr(VendureClient, name, forbidden)
    for name in ("submit", "submit_pro"):
        monkeypatch.setattr(paco, name, forbidden)
    fake = FakeReviewVendure()
    install(monkeypatch, fake)
    for i in range(3):
        v = engine_variant(i)
        fake.breakdowns[v["variant"]] = v["breakdowns"]
        fake.variants[v["variant"]] = {
            "id": v["variant"], "name": "", "sku": "", "priceWithTax": v["priceWithTax"], "currencyCode": "ARS",
            "customFields": v["customFields"], "bulkPriceTiers": v["tiers"]}
    return fake


def two_nights(pid_to_kwargs: dict[str, dict], *, prev_flag: str | None = "firme") -> tuple[int, int]:
    """Una corrida anterior (con `review_flag`) y la de hoy con los mismos productos. Devuelve (anterior, hoy)."""
    prev = w.make_run(started=NOW - timedelta(days=1))
    today = w.make_run()
    for pid, kw in pid_to_kwargs.items():
        if prev_flag is not None:
            w.add_snap(prev, pid, flag=prev_flag, data_at=NOW - timedelta(days=1), captured_at=NOW - timedelta(days=1))
        w.add_snap(today, pid, **kw)
    return prev, today


# ─── 2-1: la puerta de entrada, 14 casos ──────────────────────────


def _gate_cases() -> dict[str, tuple[dict, str | None]]:
    """nombre → (argumentos del snapshot de hoy, flag esperado). Solo `todo_ok` abre ficha."""
    return {
        "todo_ok": ({}, "firme"),
        "r1_sin_dato_de_ml": (dict(ml_status="no_data", color="sin_dato", prices=()), None),
        "r1_deshabilitado": (dict(enabled=False), "dudoso:deshabilitado"),
        "r3_solo_el_juez": (dict(sources=("llm", "llm", "llm")), "dudoso:fuentes"),
        "r4_el_rojo_lo_ponen_las_tiendas": (dict(prices=(1_400_000, 1_410_000, 1_390_000), basis="ml+tiendas"), "dudoso:tiendas"),
        "r5_dato_de_la_oficina_de_5_dias": (dict(origin="oficina", data_at=NOW - timedelta(days=5)), "dudoso:dato_viejo"),
        "r6_dos_publicaciones": (dict(prices=(900_000, 905_000), sellers=("a", "b")), "dudoso:pocas_publicaciones"),
        "r6_un_solo_vendedor": (dict(sellers=("a", "a", "a")), "dudoso:pocos_vendedores"),
        "r7_el_drone_contra_el_avion": (dict(prices=(90_000, 91_000, 89_000), our=9_000_000), "dudoso:precio_extremo"),
        "rojo_solo_por_tiendas": (dict(ml_status="no_data", prices=(), basis="tiendas"), "dudoso:solo_tiendas"),
        "r8_primera_noche": ({}, "firme"),        # el snapshot es firme; le falta la racha (sin noche anterior)
        "verde": (dict(color="verde", our=500_000), None),
        "r2_ya_tiene_ficha": ({}, "firme"),
        "ml_fallo": (dict(ml_status="failed", color="sin_dato", prices=()), None),
    }


async def test_gate_of_14_cases_only_the_one_that_meets_everything_opens_a_ficha(world):
    cases = _gate_cases()
    assert len(cases) == 14
    prev = w.make_run(started=NOW - timedelta(days=1))
    today = w.make_run()
    for pid, (kw, _flag) in cases.items():
        if pid != "r8_primera_noche":        # R8: ese producto no tiene noche anterior
            w.add_snap(prev, pid, flag="firme", data_at=NOW - timedelta(days=1), captured_at=NOW - timedelta(days=1))
        w.add_snap(today, pid, **kw)
    # R2: ya tiene una ficha abierta (de otra pasada)
    with Session(engine) as s:
        s.add(PriceReviewItem(product_id="r2_ya_tiene_ficha", open_key="r2_ya_tiene_ficha", state="propuesto",
                              last_run_id=prev, product_name="viejo"))
        s.commit()

    summary = await pr.process_run(today, now=NOW)

    opened = {i.product_id for i in w.items() if i.opened_run_id == today}
    assert opened == {"todo_ok"}, opened
    for pid, (_kw, flag) in cases.items():
        assert w.snap_of(today, pid).review_flag == flag, pid
    assert summary["firmes"] == 3 and summary["rojos"] == 11     # firmes: todo_ok, r8_primera_noche y r2
    assert summary["dudosos"] == {
        "deshabilitado": 1, "fuentes": 1, "tiendas": 1, "dato_viejo": 1, "pocas_publicaciones": 1,
        "pocos_vendedores": 1, "precio_extremo": 1, "solo_tiendas": 1}
    assert summary["esperando_racha"] == 1 and summary["nuevas"] == 1
    # el del drone queda marcado como dudoso por precio extremo, sin ficha ni evento
    assert w.snap_of(today, "r7_el_drone_contra_el_avion").review_flag == "dudoso:precio_extremo"
    assert [e.product_id for e in w.events("propuesto")] == ["todo_ok"]
    # la ficha que ya existía sigue siendo UNA
    assert len([i for i in w.items() if i.product_id == "r2_ya_tiene_ficha"]) == 1


# ─── 2-2: la racha ───────────────────────────────────────────────


async def test_streak_red_red_opens_on_the_second_night():
    r1 = w.make_run(started=NOW - timedelta(days=1))
    w.add_snap(r1, "a")
    await pr.process_run(r1, now=NOW - timedelta(days=1))
    assert w.items() == [] and w.snap_of(r1, "a").review_flag == "firme"
    r2 = w.make_run()
    w.add_snap(r2, "a")
    summary = await pr.process_run(r2, now=NOW)
    [item] = w.items()
    assert item.product_id == "a" and item.streak_nights == 2 and item.opened_run_id == r2 and summary["nuevas"] == 1


async def test_streak_a_failed_night_neither_adds_nor_subtracts():
    runs = [w.make_run(started=NOW - timedelta(days=d)) for d in (2, 1, 0)]
    w.add_snap(runs[0], "b")
    w.add_snap(runs[1], "b", ml_status="failed", color="sin_dato", prices=())
    w.add_snap(runs[2], "b")
    await pr.process_run(runs[0], now=NOW - timedelta(days=2))
    await pr.process_run(runs[1], now=NOW - timedelta(days=1))
    assert w.items() == []                       # la noche failed no abre nada
    await pr.process_run(runs[2], now=NOW)
    [item] = w.items()
    assert item.product_id == "b" and item.streak_nights == 2     # la 3.ª noche (rojo, failed, rojo)


async def test_streak_a_skipped_night_is_the_same():
    runs = [w.make_run(started=NOW - timedelta(days=d)) for d in (2, 1, 0)]
    w.add_snap(runs[0], "s")
    w.add_snap(runs[1], "s", ml_status="skipped", color="sin_dato", prices=())
    w.add_snap(runs[2], "s")
    for r, d in zip(runs, (2, 1, 0)):
        await pr.process_run(r, now=NOW - timedelta(days=d))
    assert [i.product_id for i in w.items()] == ["s"]


@pytest.mark.parametrize("middle", [dict(color="verde", our=500_000), dict(color="amarillo", our=720_000),
                                    dict(ml_status="no_data", color="sin_dato", prices=()),
                                    dict(prices=(90_000,) * 3, our=9_000_000)])
async def test_streak_a_night_that_is_not_firm_red_restarts_the_count(middle):
    runs = [w.make_run(started=NOW - timedelta(days=d)) for d in (2, 1, 0)]
    w.add_snap(runs[0], "c")
    w.add_snap(runs[1], "c", **middle)
    w.add_snap(runs[2], "c")
    for r, d in zip(runs, (2, 1, 0)):
        await pr.process_run(r, now=NOW - timedelta(days=d))
    assert w.items() == []                       # rojo, verde (o equivalente), rojo → sin ficha


async def test_streak_three_nights_of_waiting_do_not_open_two_items():
    runs = [w.make_run(started=NOW - timedelta(days=d)) for d in (2, 1, 0)]
    for r, d in zip(runs, (2, 1, 0)):
        w.add_snap(r, "d")
        await pr.process_run(r, now=NOW - timedelta(days=d))
    [item] = w.items()
    assert item.streak_nights == 3 and len(w.events("propuesto")) == 1      # la 3.ª noche actualiza, no duplica


async def test_the_nights_setting_changes_the_streak_needed():
    w.set_setting("pm_review_confirm_nights", 3)
    runs = [w.make_run(started=NOW - timedelta(days=d)) for d in (2, 1, 0)]
    for r, d in zip(runs, (2, 1, 0)):
        w.add_snap(r, "e")
        await pr.process_run(r, now=NOW - timedelta(days=d))
        assert (len(w.items()) == 1) is (d == 0)
    w.reset_db()
    w.set_setting("pm_mode", 1)
    w.set_setting("pm_review_confirm_nights", 1)
    r = w.make_run()
    w.add_snap(r, "f")
    await pr.process_run(r, now=NOW)
    assert [i.product_id for i in w.items()] == ["f"]        # 1 noche alcanza


# ─── 2-3: idempotencia ───────────────────────────────────────────


class _Crash(BaseException):
    """Un corte del proceso: no es una Exception, no lo atrapa ningún try/except del código."""


def _three_firm_products() -> int:
    prev, today = two_nights({"p1": {}, "p2": {}, "p3": {}})
    return today


async def test_running_the_pass_twice_changes_nothing_and_review_at_is_written_once():
    today = _three_firm_products()
    first = await pr.process_run(today, now=NOW)
    stamp = w.run_of(today).review_at
    assert first["nuevas"] == 3 and stamp is not None
    items_before, events_before = w.items(), w.events()
    assert await pr.process_run(today, now=NOW + timedelta(hours=1)) is None
    assert w.run_of(today).review_at == stamp
    assert [i.id for i in w.items()] == [i.id for i in items_before] and len(w.events()) == len(events_before) == 3


async def test_a_restart_in_the_middle_leaves_the_same_items_and_events(monkeypatch):
    today = _three_firm_products()
    real = pr._create_item
    calls = {"n": 0}

    def flaky(plan, cand, pdiag):
        calls["n"] += 1
        if calls["n"] == 2:
            raise _Crash()
        return real(plan, cand, pdiag)

    monkeypatch.setattr(pr, "_create_item", flaky)
    with pytest.raises(_Crash):
        await pr.process_run(today, now=NOW)
    assert len(w.items()) == 1 and w.run_of(today).review_at is None       # quedó a mitad y NO se marcó como hecha
    monkeypatch.setattr(pr, "_create_item", real)
    summary = await pr.process_run(today, now=NOW)                          # el reinicio retoma
    assert summary["actualizadas"] == 1 and summary["nuevas"] == 2
    assert sorted(i.product_id for i in w.items()) == ["p1", "p2", "p3"]
    assert len(w.events("propuesto")) == 3 and len(w.events()) == 3        # mismo número de fichas y de eventos
    stamp = w.run_of(today).review_at
    assert stamp is not None
    assert await pr.process_run(today, now=NOW) is None and w.run_of(today).review_at == stamp


async def test_an_item_that_already_exists_is_never_created_twice():
    today = _three_firm_products()
    cands = pr._plan(today, NOW)
    assert len(cands.to_create) == 3
    assert pr._create_item(cands, cands.to_create[0], pr._Diag(None)) is True
    assert pr._create_item(cands, cands.to_create[0], pr._Diag(None)) is False       # open_key único
    assert len(w.items()) == 1 and len(w.events()) == 1


# ─── 2-4: cero escrituras ────────────────────────────────────────


@pytest.mark.parametrize("mode", [0, 1, 2])
async def test_zero_writes_to_vendure_at_the_http_level_in_every_mode(world, mode):
    w.set_setting("pm_mode", mode)
    prev, today = two_nights({"p1": {}, "p2": dict(variant="102", tier="tier:min_qty=100"),
                              "p3": dict(variant="103", tier="tier:min_qty=50")})
    await pr.process_run(today, now=NOW)
    assert world.mutations == [], world.mutations
    assert all(r["kind"] == "query" and r["query"].lstrip().startswith("query") for r in world.requests)
    assert not any("updateProduct" in r["query"] or "enabled:" in r["query"] for r in world.requests)
    assert (len(w.items()) == 3) is (mode >= 1)


async def test_the_whole_pass_uses_only_the_four_read_operations(world):
    prev, today = two_nights({"p1": {}})
    await pr.process_run(today, now=NOW)
    assert set(world.ops()) <= {"ActiveChannelPricing", "FiscalProfile", "PricedVariant", "TierBreakdowns", "ProductCertification"}
    assert world.count("TierBreakdowns") == 1


# ─── 2-8: modo 0 ─────────────────────────────────────────────────


async def test_mode_0_creates_no_ficha_no_event_and_asks_vendure_nothing(world):
    w.set_setting("pm_mode", 0)
    prev, today = two_nights({"p1": {}, "p2": dict(sources=("llm",) * 3)})
    summary = await pr.process_run(today, now=NOW)
    assert w.items() == [] and w.events() == []
    assert world.requests == []
    assert w.snap_of(today, "p1").review_flag == "firme" and w.snap_of(today, "p2").review_flag == "dudoso:fuentes"
    assert w.run_of(today).review_at is not None and summary["mode"] == 0 and summary["firmes"] == 1


async def test_mode_0_keeps_marking_flags_so_the_streak_is_ready_when_it_is_switched_on():
    w.set_setting("pm_mode", 0)
    r1 = w.make_run(started=NOW - timedelta(days=1))
    w.add_snap(r1, "a")
    await pr.process_run(r1, now=NOW - timedelta(days=1))
    w.set_setting("pm_mode", 1)
    r2 = w.make_run()
    w.add_snap(r2, "a")
    await pr.process_run(r2, now=NOW)
    assert [i.product_id for i in w.items()] == ["a"]


async def test_mode_2_behaves_as_mode_1_and_says_so(caplog):
    w.set_setting("pm_mode", 2)
    prev, today = two_nights({"p1": {}})
    with caplog.at_level(logging.WARNING, logger="app.pricing.price_review"):
        summary = await pr.process_run(today, now=NOW)
    assert "pm_mode=2" in caplog.text and "todavía no está implementado" in caplog.text
    assert summary["mode"] == 2 and summary["mode_effective"] == 1 and "no implementado" in summary["note"]
    [item] = w.items()
    assert item.state == "propuesto" and item.mode_at_open == 1
    assert json.loads(w.events("propuesto")[0].payload)["modo"] == 2


# ─── lo que guarda la ficha ──────────────────────────────────────


async def test_the_ficha_keeps_the_firm_figures_the_diagnosis_and_the_breakdown(world):
    prev, today = two_nights({"p1": dict(specs={"box_length": 62, "box_width": 48, "box_height": 40, "box_weight": 18})})
    await pr.process_run(today, now=NOW)
    [item] = w.items()
    assert (item.state, item.mode_at_open, item.streak_nights) == ("propuesto", 1, 2)
    assert item.open_key == "p1" and item.product_name == "Producto p1" and item.product_code == "BXp1"
    assert (item.our_price_cents, item.ml_median_cents, item.firm_listing_count, item.firm_seller_count) == (
        1_000_000, 900_000, 3, 3)
    assert item.price_ratio == pytest.approx(1.111, abs=1e-3) and item.reff_ratio == pytest.approx(1.277, abs=1e-3)
    assert item.diag_status == "completo" and item.probable_cause in list("abcde") + ["ninguna"]
    diag, bd = json.loads(item.diag), json.loads(item.breakdown)
    assert [r["code"] for r in diag["rules"]] == list("abcde") and len(item.diag) <= 8_000 and len(item.breakdown) <= 8_000
    assert len(bd["tier"]["components"]) == 10 and bd["tier"]["quantity"] == 25
    assert abs(sum(c["usd"] for c in bd["tier"]["components"]) - bd["tier"]["factura_usd"]) <= 0.01
    [ev] = w.events()
    assert (ev.kind, ev.actor, ev.to_state, ev.run_id, ev.item_id) == ("propuesto", "hugo:auto", "propuesto", today, item.id)
    assert ev.snapshot_id == item.opened_snapshot_id and json.loads(ev.payload)["racha"] == 2


async def test_a_ficha_from_the_office_data_is_flagged(world):
    prev, today = two_nights({"p1": dict(origin="oficina", data_at=NOW - timedelta(hours=30))})
    await pr.process_run(today, now=NOW)
    [item] = w.items()
    assert "oficina" in json.loads(item.flags)


# ─── fichas abiertas: se actualizan y se cierran solas ────────────


async def _open_one():
    prev, today = two_nights({"p1": {}})
    await pr.process_run(today, now=NOW)
    [item] = w.items()
    return item, today


async def test_an_open_ficha_that_is_still_red_is_updated_not_duplicated():
    item, r2 = await _open_one()
    r3 = w.make_run(started=NOW + timedelta(days=1))
    w.add_snap(r3, "p1", data_at=NOW + timedelta(days=1), captured_at=NOW + timedelta(days=1))
    summary = await pr.process_run(r3, now=NOW + timedelta(days=1))
    [again] = w.items()
    assert again.id == item.id and again.state == "propuesto" and again.last_run_id == r3 and again.streak_nights == 3
    assert summary["actualizadas"] == 1 and summary["nuevas"] == 0 and len(w.events()) == 1


async def test_an_open_ficha_closes_itself_when_the_product_stops_being_red():
    item, _ = await _open_one()
    r3 = w.make_run(started=NOW + timedelta(days=1))
    w.add_snap(r3, "p1", color="verde", our=500_000)
    summary = await pr.process_run(r3, now=NOW + timedelta(days=1))
    [closed] = w.items()
    assert closed.state == "cerrado_auto" and closed.open_key is None and closed.closed_at is not None
    assert closed.version == item.version + 1 and summary["cerradas_solas"] == 1
    ev = w.events("cerrado_auto")[0]
    assert (ev.actor, ev.from_state, ev.to_state, ev.motive_code) == ("hugo:auto", "propuesto", "cerrado_auto", "ya_no_es_rojo")
    assert "verde" in ev.motive_text


async def test_a_disabled_product_closes_the_ficha_as_already_inactive():
    await _open_one()
    r3 = w.make_run(started=NOW + timedelta(days=1))
    w.add_snap(r3, "p1", enabled=False)
    await pr.process_run(r3, now=NOW + timedelta(days=1))
    [closed] = w.items()
    assert closed.state == "cerrado_auto" and w.events("cerrado_auto")[0].motive_code == "ya_inactivo"


@pytest.mark.parametrize("night", [dict(ml_status="failed", color="sin_dato", prices=()),
                                   dict(ml_status="skipped", color="sin_dato", prices=()),
                                   dict(ml_status="no_data", color="sin_dato", prices=())])
async def test_a_night_without_data_does_not_close_the_ficha(night):
    item, _ = await _open_one()
    r3 = w.make_run(started=NOW + timedelta(days=1))
    w.add_snap(r3, "p1", **night)
    await pr.process_run(r3, now=NOW + timedelta(days=1))
    [still] = w.items()
    assert still.state == "propuesto" and still.open_key == "p1" and "sin_dato_hoy" in json.loads(still.flags)


async def test_a_ficha_whose_red_turned_doubtful_stays_open_and_says_why():
    await _open_one()
    r3 = w.make_run(started=NOW + timedelta(days=1))
    w.add_snap(r3, "p1", sources=("llm",) * 3)
    await pr.process_run(r3, now=NOW + timedelta(days=1))
    [still] = w.items()
    assert still.state == "propuesto" and "dudoso:fuentes" in json.loads(still.flags)


async def test_after_closing_a_new_ficha_can_be_opened_later():
    await _open_one()
    r3 = w.make_run(started=NOW + timedelta(days=1))
    w.add_snap(r3, "p1", color="verde", our=500_000)
    await pr.process_run(r3, now=NOW + timedelta(days=1))
    r4, r5 = w.make_run(started=NOW + timedelta(days=2)), w.make_run(started=NOW + timedelta(days=3))
    for r, d in ((r4, 2), (r5, 3)):
        w.add_snap(r, "p1", data_at=NOW + timedelta(days=d), captured_at=NOW + timedelta(days=d))
        await pr.process_run(r, now=NOW + timedelta(days=d))
    states = [(i.state, i.open_key) for i in w.items()]
    assert states == [("cerrado_auto", None), ("propuesto", "p1")]       # una cerrada de historial y una abierta nueva


# ─── Vendure: tope de consultas y diagnóstico pendiente ───────────


async def test_the_hard_cap_of_150_reads_bounds_the_night_and_the_rest_wait_for_tomorrow(world):
    pids = {f"p{i}": {} for i in range(90)}
    prev, today = two_nights(pids)
    summary = await pr.process_run(today, now=NOW)
    assert len(w.items()) == 90 and summary["nuevas"] == 90          # todos entran a la bandeja…
    assert len(world.requests) <= 150 and summary["consultas_vendure"] <= 150
    done = [i for i in w.items() if i.diag_status == "completo"]
    waiting = [i for i in w.items() if i.diag_status == "pendiente"]
    assert done and waiting and len(done) + len(waiting) == 90 and summary["tope_alcanzado"] is True
    assert all("diag_pendiente" in json.loads(i.flags) for i in waiting)
    # lo que no entró por el tope NO cuenta como intento: mañana tiene todos los suyos
    assert all(json.loads(i.diag)["attempts"] in (0, 1) for i in waiting)
    assert sum(1 for i in waiting if json.loads(i.diag)["attempts"] == 0) >= len(waiting) - 1
    # …y a la noche siguiente las pendientes se completan primero
    r3 = w.make_run(started=NOW + timedelta(days=1))
    for pid in pids:
        w.add_snap(r3, pid, data_at=NOW + timedelta(days=1), captured_at=NOW + timedelta(days=1))
    world.requests.clear()
    s3 = await pr.process_run(r3, now=NOW + timedelta(days=1))
    assert s3["diag_reintentados"] == len(waiting) and len(world.requests) <= 150
    assert sum(1 for i in w.items() if i.diag_status == "completo") > len(done)
    assert len(w.events("propuesto")) == 90


async def test_if_the_breakdown_cannot_be_read_the_ficha_still_opens_and_is_retried(world):
    world.http_fail.add("TierBreakdowns")
    prev, today = two_nights({"p1": {}})
    await pr.process_run(today, now=NOW)
    [item] = w.items()
    assert item.diag_status == "pendiente" and item.breakdown is None
    diag = json.loads(item.diag)
    assert diag["attempts"] == 1 and any("desglose" in e for e in diag["errors"])
    assert all(r["status"] in ("sin_dato", "no", "dispara", "no_aplica") for r in diag["rules"])
    world.http_fail.clear()
    r3 = w.make_run(started=NOW + timedelta(days=1))
    w.add_snap(r3, "p1", data_at=NOW + timedelta(days=1), captured_at=NOW + timedelta(days=1))
    await pr.process_run(r3, now=NOW + timedelta(days=1))
    [item] = w.items()
    assert item.diag_status == "completo" and item.breakdown and json.loads(item.diag)["attempts"] == 2
    assert len(w.events()) == 1


async def test_a_diagnosis_that_never_works_stops_being_retried_after_five_attempts(world):
    world.http_fail.add("TierBreakdowns")
    prev, today = two_nights({"p1": {}})
    await pr.process_run(today, now=NOW)
    last = None
    for d in range(1, 6):
        r = w.make_run(started=NOW + timedelta(days=d))
        w.add_snap(r, "p1", data_at=NOW + timedelta(days=d), captured_at=NOW + timedelta(days=d))
        await pr.process_run(r, now=NOW + timedelta(days=d))
        last = r
    [item] = w.items()
    assert item.diag_status == "parcial" and json.loads(item.diag)["attempts"] == 5 and "diag_parcial" in json.loads(item.flags)
    world.requests.clear()
    r = w.make_run(started=NOW + timedelta(days=9))
    w.add_snap(r, "p1", data_at=NOW + timedelta(days=9), captured_at=NOW + timedelta(days=9))
    await pr.process_run(r, now=NOW + timedelta(days=9))
    assert world.count("TierBreakdowns") == 0 and last is not None


async def test_the_nightly_catalog_variant_saves_a_read_per_product(world):
    from app.vendure.client import VendureProduct

    prev, today = two_nights({"p1": {}})
    v = engine_variant(0)
    [pv] = VendureClient._map_priced_variants([{
        "id": "101", "priceWithTax": v["priceWithTax"], "currencyCode": "ARS", "customFields": v["customFields"],
        "bulkPriceTiers": v["tiers"]}])
    product = VendureProduct(id="p1", name="x", slug="x", description="", enabled=True, source_url=None, image_urls=[],
                             product_code="BX", featured_image_url=None, first_variant_price_cents=1, variant_count=1,
                             priced_variants=[pv])
    await pr.process_run(today, now=NOW, catalog={"p1": product})
    assert world.count("PricedVariant") == 0 and world.count("TierBreakdowns") == 1


async def test_an_unexpected_failure_in_the_diagnosis_never_stops_the_pass(monkeypatch):
    from app.pricing import diagnosis_source as ds

    async def boom(*_a, **_k):
        raise RuntimeError("bug")

    monkeypatch.setattr(ds, "diagnose_product", boom)
    prev, today = two_nights({"p1": {}, "p2": {}})
    summary = await pr.process_run(today, now=NOW)
    assert summary["nuevas"] == 2 and all(i.diag_status == "pendiente" for i in w.items())


# ─── n_flipped ───────────────────────────────────────────────────


async def test_flips_count_any_color_change_including_to_or_from_no_data():
    run = w.make_run()
    w.add_snap(run, "a", color="rojo", prev_color="verde")
    w.add_snap(run, "b", color="sin_dato", ml_status="no_data", prices=(), prev_color="rojo")
    w.add_snap(run, "c", color="verde", our=500_000, prev_color="verde")
    w.add_snap(run, "d", color="amarillo", our=720_000, prev_color=None)
    w.set_setting("pm_mode", 0)
    await pr.process_run(run, now=NOW)
    assert w.run_of(run).n_flipped == 2


# ─── el registro de eventos solo se agrega ────────────────────────


async def test_events_are_append_only_and_the_actor_is_always_there():
    prev, today = two_nights({"p1": {}})
    await pr.process_run(today, now=NOW)
    with Session(engine) as s:
        ev = s.exec(select(PriceReviewEvent)).one()
        assert ev.actor
        ev.kind = "otra_cosa"
        s.add(ev)
        with pytest.raises(EventLogIsAppendOnly):
            s.flush()
        s.rollback()
        ev = s.exec(select(PriceReviewEvent)).one()
        s.delete(ev)
        with pytest.raises(EventLogIsAppendOnly):
            s.flush()
        s.rollback()
        ev = s.exec(select(PriceReviewEvent)).one()
        ev.notified_at = NOW                                   # lo único que puede cambiar
        s.add(ev)
        s.commit()
    assert w.events()[0].notified_at == NOW and w.events()[0].kind == "propuesto"


def test_only_one_open_ficha_per_product_but_history_is_kept():
    with Session(engine) as s:
        s.add(PriceReviewItem(product_id="x", open_key="x", state="propuesto"))
        s.add(PriceReviewItem(product_id="x", open_key=None, state="cerrado_auto"))
        s.add(PriceReviewItem(product_id="x", open_key=None, state="cerrado_auto"))
        s.commit()
        s.add(PriceReviewItem(product_id="x", open_key="x", state="propuesto"))
        with pytest.raises(IntegrityError):
            s.commit()


# ─── de cuándo es el dato (R5) ────────────────────────────────────


def test_the_data_time_of_an_office_result_is_its_fetched_at_and_the_rest_is_the_run_time():
    from types import SimpleNamespace

    from app.db.models import MarketPriceSnapshot
    from app.pricing import oficina_ml, price_monitor

    fetched = NOW - timedelta(days=5)
    ctx = SimpleNamespace(oficina={"7": oficina_ml.FreshResult("7", "ok", "q", fetched, [])})
    office = MarketPriceSnapshot(run_id=1, product_id="7", match_origin="oficina", captured_at=NOW)
    api = MarketPriceSnapshot(run_id=1, product_id="7", match_origin="api", captured_at=NOW)
    web = MarketPriceSnapshot(run_id=1, product_id="7", match_origin="web", captured_at=NOW)
    assert price_monitor._data_time(ctx, office, "7") == fetched
    assert price_monitor._data_time(ctx, api, "7") == NOW and price_monitor._data_time(ctx, web, "7") == NOW
    assert price_monitor._data_time(SimpleNamespace(oficina={}), office, "7") == NOW      # sin resultado: la hora de la corrida


# ─── 2-9: costo ──────────────────────────────────────────────────


class _QuickSource:
    """Vendure instantáneo: el criterio mide la pasada SIN contar las consultas a Vendure."""

    async def get_tier_breakdowns(self, variant_id):
        return []

    async def get_priced_variant(self, variant_id):
        return None

    async def get_certification(self, product_id, channel_id):
        return None

    async def get_channel_pricing(self):
        return None


async def test_the_pass_over_1500_snapshots_takes_under_a_minute():
    import time

    prev = w.make_run(started=NOW - timedelta(days=1))
    today = w.make_run()
    for i in range(1500):
        pid = f"q{i}"
        if i % 5 == 0:                              # 300 rojos, 150 de ellos firmes con racha
            firm = i % 10 == 0
            w.add_snap(prev, pid, flag="firme" if firm else None, data_at=NOW - timedelta(days=1))
            w.add_snap(today, pid, sources=None if firm else ("llm",) * 3)
        else:
            w.add_snap(today, pid, color="verde", our=500_000, prev_color="verde")
    t0 = time.monotonic()
    summary = await pr.process_run(today, now=NOW, source=_QuickSource())
    elapsed = time.monotonic() - t0
    assert elapsed < 60, f"{elapsed:.1f} s"
    assert summary["firmes"] == 150 and summary["nuevas"] == 150 and summary["rojos"] == 300


# ─── noches, no corridas ─────────────────────────────────────────


async def test_two_runs_on_the_same_day_are_one_night_so_a_manual_run_cannot_skip_the_streak():
    morning = w.make_run(started=NOW.replace(hour=6))
    w.add_snap(morning, "a")
    await pr.process_run(morning, now=NOW.replace(hour=6))
    afternoon = w.make_run(started=NOW.replace(hour=15))                      # «Correr ahora» el mismo día
    w.add_snap(afternoon, "a")
    await pr.process_run(afternoon, now=NOW.replace(hour=15))
    assert w.items() == [], "dos corridas el mismo día no son dos noches"
    tomorrow = w.make_run(started=NOW + timedelta(days=1))
    w.add_snap(tomorrow, "a", data_at=NOW + timedelta(days=1))
    await pr.process_run(tomorrow, now=NOW + timedelta(days=1))
    [item] = w.items()
    assert item.streak_nights == 2                                           # ayer (dos corridas) + hoy


async def test_of_each_day_the_latest_run_with_data_decides():
    d1a = w.make_run(started=NOW.replace(hour=6) - timedelta(days=1))
    d1b = w.make_run(started=NOW.replace(hour=15) - timedelta(days=1))
    d2 = w.make_run(started=NOW.replace(hour=6))
    w.add_snap(d1a, "a")
    w.add_snap(d1b, "a", color="verde", our=500_000)                         # la última de ayer ya no era rojo
    w.add_snap(d2, "a")
    for r, when in ((d1a, NOW.replace(hour=6) - timedelta(days=1)), (d1b, NOW.replace(hour=15) - timedelta(days=1)), (d2, NOW.replace(hour=6))):
        await pr.process_run(r, now=when)
    assert w.items() == []


async def test_a_failed_run_of_the_same_day_does_not_hide_the_good_one():
    ok = w.make_run(started=NOW.replace(hour=6) - timedelta(days=1))
    failed = w.make_run(started=NOW.replace(hour=15) - timedelta(days=1))
    today = w.make_run(started=NOW.replace(hour=6))
    w.add_snap(ok, "a")
    w.add_snap(failed, "a", ml_status="failed", color="sin_dato", prices=())
    w.add_snap(today, "a")
    await pr.process_run(ok, now=NOW.replace(hour=6) - timedelta(days=1))
    await pr.process_run(failed, now=NOW.replace(hour=15) - timedelta(days=1))
    await pr.process_run(today, now=NOW.replace(hour=6))
    assert [i.product_id for i in w.items()] == ["a"]


async def test_lowering_the_mode_during_the_pass_stops_opening_fichas(monkeypatch):
    prev, today = two_nights({"p1": {}, "p2": {}, "p3": {}})
    real = pr._create_item

    def create_then_switch_off(plan, cand, pdiag):
        out = real(plan, cand, pdiag)
        w.set_setting("pm_mode", 0)                          # alguien apaga la bandeja a mitad de la pasada
        return out

    monkeypatch.setattr(pr, "_create_item", create_then_switch_off)
    summary = await pr.process_run(today, now=NOW)
    assert len(w.items()) == 1 and summary["nuevas"] == 1 and summary["cortada_por_modo"] is True


# ─── una sola mediana: la de los idénticos firmes ─────────────────


def _with_judge_listing(our: int = 1_000_000):
    """3 idénticos firmes a ~900.000 y 2 solo-juez baratos: la mediana de TODOS (880.000) no es la de los firmes (900.000)."""
    return [w.listing(1, 900_000), w.listing(2, 905_000), w.listing(3, 895_000),
            w.listing(4, 700_000, source="llm"), w.listing(5, 710_000, source="llm")]


async def test_the_card_and_the_max_price_use_the_same_median_the_firm_one(world):
    from app.pricing.price_diagnosis import price_max_cents

    prev, today = two_nights({"p1": dict(matched=_with_judge_listing())})
    await pr.process_run(today, now=NOW)
    [item] = w.items()
    snap = w.snap_of(today, "p1")
    assert snap.ml_median_cents != item.ml_median_cents == 900_000                 # la del snapshot es de todos; la de la ficha, de los firmes
    diag = json.loads(item.diag)
    assert diag["p_max_cents"] == round(price_max_cents(item.ml_median_cents, 13.0, 0, 10.0))


async def test_when_the_diagnosis_is_completed_later_the_card_is_refreshed_with_the_same_figures(world):
    from app.pricing.price_diagnosis import price_max_cents

    world.http_fail.add("TierBreakdowns")
    prev, today = two_nights({"p1": dict(matched=_with_judge_listing())})
    await pr.process_run(today, now=NOW)
    assert w.items()[0].diag_status == "pendiente"
    world.http_fail.clear()
    # al día siguiente los idénticos firmes cuestan otra cosa y nuestro precio también
    r3 = w.make_run(started=NOW + timedelta(days=1))
    later = [w.listing(1, 950_000), w.listing(2, 955_000), w.listing(3, 945_000), w.listing(4, 700_000, source="llm")]
    w.add_snap(r3, "p1", matched=later, our=1_100_000, data_at=NOW + timedelta(days=1), captured_at=NOW + timedelta(days=1))
    await pr.process_run(r3, now=NOW + timedelta(days=1))
    [item] = w.items()
    assert item.diag_status == "completo" and item.ml_median_cents == 950_000 and item.our_price_cents == 1_100_000
    assert json.loads(item.diag)["p_max_cents"] == round(price_max_cents(950_000, 13.0, 0, 10.0))
    assert json.loads(item.diag)["p_cents"] == item.our_price_cents


# ─── noche = día ART, y a menos de 12 h es la misma ───────────────


def _ART(day: int, hour: int, minute: int = 0) -> datetime:
    """Un instante dado en HORA ARGENTINA, devuelto en UTC naive (como se guarda)."""
    return datetime(2026, 10, day, hour, minute) + timedelta(hours=3)


async def _run_at(pid: str, when: datetime, **snap) -> None:
    rid = w.make_run(started=when)
    w.add_snap(rid, pid, data_at=when, captured_at=when, **snap)
    await pr.process_run(rid, now=when + timedelta(minutes=5))


async def test_a_night_is_an_argentine_calendar_day_not_a_utc_one():
    """22:30 y 23:30 ART del 13 son el 13 y el 14 en UTC (01:30 y 02:30): para Argentina es UNA noche."""
    await _run_at("a", _ART(13, 22, 30))
    await _run_at("a", _ART(13, 23, 50))
    assert w.items() == []
    await _run_at("a", _ART(14, 3, 0))                       # 3 h después: aunque sea otro día ART, es la misma noche
    assert w.items() == []
    await _run_at("a", _ART(14, 22, 0))                      # al otro día (hora argentina) y 19 h después: la segunda noche
    [item] = w.items()
    assert item.streak_nights == 2


async def test_two_runs_less_than_twelve_hours_apart_are_one_night_even_across_art_midnight():
    await _run_at("a", _ART(13, 21, 0))
    await _run_at("a", _ART(14, 1, 0))                       # otro día ART, pero 4 h después
    assert w.items() == []
    await _run_at("a", _ART(14, 22, 0))                      # 21 h después de la última: ahora sí otra noche
    [item] = w.items()
    assert item.streak_nights == 2


async def test_the_normal_nightly_cron_still_counts_one_night_per_day():
    for day in (11, 12, 13):
        await _run_at("a", _ART(day, 3, 0))                  # 06:00 UTC todos los días
    [item] = w.items()
    assert item.streak_nights == 3 and len(w.events("propuesto")) == 1


async def test_one_manual_run_the_same_art_day_as_the_cron_does_not_make_a_night():
    await _run_at("a", _ART(12, 3, 0))
    await _run_at("a", _ART(13, 3, 0))                       # ya hay ficha esta noche; abajo se prueba que la manual no la altera
    [item] = w.items()
    n = item.streak_nights
    await _run_at("a", _ART(13, 18, 0))                      # «Correr ahora» a la tarde: mismo día ART
    assert w.items()[0].streak_nights == n
