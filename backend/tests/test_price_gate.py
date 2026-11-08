"""Portón de precio (pm_mode = 2, pricing/price_gate.py): lo que sube Paco se activa con verde firme, queda inactivo con rojo
firme, va a la bandeja de Pao con amarillo o sin dato, y un rojo firme prendido a mano vuelve a inactivo."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import delete
from sqlmodel import Session, select

from app.db.models import AuditLog, LuisIntake
from app.db.session import engine
from app.pricing import price_gate
from app.pricing import price_review as pr
from app.pricing import price_review_actions as actions
from tests import review_decide as kit
from tests import review_world as w

GREEN_PRICES = (1_900_000, 1_950_000, 2_000_000)   # mediana 1.950.000 contra nuestro 1.000.000 → verde
YELLOW_PRICES = (1_350_000, 1_380_000, 1_400_000)   # ~20 % → amarillo


@pytest.fixture
def fake(monkeypatch):
    f = kit.setup(monkeypatch, mode=2)
    with Session(engine) as s:
        s.execute(delete(LuisIntake))
        s.commit()
    yield f
    with Session(engine) as s:
        s.execute(delete(LuisIntake))
        s.commit()


def paco_upload(pid: str, *, status: str | None = None, uploaded_days_ago: float = 1) -> int:
    with Session(engine) as s:
        row = LuisIntake(offer_id=f"o{pid}", source_url=f"https://detail.1688.com/offer/{pid}.html", name="x",
                         image_url="https://cbu01.alicdn.com/x.jpg", vendure_product_id=pid,
                         uploaded_at=w.NOW - timedelta(days=uploaded_days_ago), gate_status=status)
        s.add(row)
        s.commit()
        return int(row.id)  # type: ignore[arg-type]


def intake(pid: str) -> LuisIntake:
    with Session(engine) as s:
        return s.exec(select(LuisIntake).where(LuisIntake.vendure_product_id == pid)).one()


# ─── classify (pura) ──────────────────────────────────────────────


def test_classify_green_firm_activates_red_firm_stays_off_and_the_rest_go_to_the_tray(fake):
    p = pr.ReviewParams()
    run = w.make_run()
    w.add_snap(run, "v", color="verde", prices=GREEN_PRICES, enabled=False)
    w.add_snap(run, "r", color="rojo", enabled=False)
    w.add_snap(run, "a", color="amarillo", prices=YELLOW_PRICES, enabled=False)
    w.add_snap(run, "s", color="sin_dato", ml_status="no_data", enabled=False)
    w.add_snap(run, "d", color="verde", prices=GREEN_PRICES, enabled=False, sources=("llm", "llm", "llm"))
    w.add_snap(run, "x", color="verde", prices=(2_500_000, 2_500_000, 2_500_000), our=1_000_000, enabled=False)
    got = {pid: price_gate.classify(w.snap_of(run, pid), p, w.NOW) for pid in "vrasdx"}
    assert got["v"].action == price_gate.ACTIVAR and got["v"].reason == "verde_firme"
    assert got["r"].action == price_gate.ROJO          # un rojo APAGADO también es firme para el portón
    assert got["a"].action == price_gate.BANDEJA and got["a"].reason == "amarillo"
    assert got["s"].action == price_gate.BANDEJA and got["s"].reason == "sin_dato"
    assert got["d"].action == price_gate.BANDEJA and got["d"].reason == "verde_dudoso:fuentes"
    assert got["x"].action == price_gate.ACTIVAR       # 0,4 del precio de ML: por encima del piso (1/3)
    assert price_gate.classify(None, p).action == price_gate.BANDEJA


def test_classify_old_data_or_too_cheap_never_activates(fake):
    p = pr.ReviewParams()
    run = w.make_run()
    w.add_snap(run, "old", color="verde", prices=GREEN_PRICES, enabled=False, data_at=w.NOW - timedelta(hours=60))
    w.add_snap(run, "cheap", color="verde", prices=(4_000_000, 4_000_000, 4_000_000), our=1_000_000 // 2,
               enabled=False)
    assert price_gate.classify(w.snap_of(run, "old"), p, w.NOW).reason == "verde_dudoso:dato_viejo"
    assert price_gate.classify(w.snap_of(run, "cheap"), p, w.NOW).reason == "precio_bajo"


# ─── la noche ─────────────────────────────────────────────────────


async def test_night_activates_green_firm_with_the_protocol_and_it_can_be_undone(fake):
    run = w.make_run()
    w.add_snap(run, "g1", color="verde", prices=GREEN_PRICES, enabled=False)
    fake.enabled["g1"] = False
    paco_upload("g1")
    counts = await price_gate.process_run(run, now=w.NOW)
    assert counts == {"activado": 1}
    assert fake.enabled["g1"] is True and fake.writes() == [True]
    [item] = w.items()
    assert item.state == pr.ACTIVADO and item.decision == actions.ACTIVAR_AUTO and item.open_key is None
    assert "decision_activar_auto" in kit.kinds(int(item.id)) and "reactivado" in kit.kinds(int(item.id))
    assert intake("g1").gate_status == "activado"
    assert [a.action for a in kit.audit("g1")] == ["price_review_enabled"]
    # Pao puede deshacerlo: vuelve a «por activar» y se apaga.
    r = kit.post(kit.client(), int(item.id), "undo", expected_state=pr.ACTIVADO)
    assert r.status_code == 200, r.text
    assert fake.enabled["g1"] is False and w.item(int(item.id)).state == pr.POR_ACTIVAR


async def test_night_leaves_red_firm_off_and_sends_yellow_and_no_data_to_the_tray(fake):
    run = w.make_run()
    w.add_snap(run, "r1", color="rojo", enabled=False)
    w.add_snap(run, "a1", color="amarillo", prices=YELLOW_PRICES, enabled=False)
    w.add_snap(run, "s1", color="sin_dato", ml_status="no_data", enabled=False)
    for pid in ("r1", "a1", "s1"):
        fake.enabled[pid] = False
        paco_upload(pid)
    counts = await price_gate.process_run(run, now=w.NOW)
    assert counts == {"rojo_inactivo": 1, "bandeja": 2}
    assert fake.writes() == []                                   # nada se escribió en Vendure
    assert {i.product_id: i.state for i in w.items()} == {"a1": pr.POR_ACTIVAR, "s1": pr.POR_ACTIVAR}
    assert intake("r1").gate_status == "rojo_inactivo" and intake("a1").gate_status == "bandeja"
    # Pao lo activa a mano desde la bandeja.
    item = next(i for i in w.items() if i.product_id == "a1")
    r = kit.decide(kit.client(), int(item.id), "activar", pr.POR_ACTIVAR)
    assert r.status_code == 200, r.text
    assert fake.enabled["a1"] is True and w.item(int(item.id)).state == pr.ACTIVADO


async def test_tray_item_that_turns_green_firm_is_activated_by_hugo(fake):
    run1 = w.make_run(started=w.NOW - timedelta(days=1))
    w.add_snap(run1, "a2", color="amarillo", prices=YELLOW_PRICES, enabled=False, captured_at=w.NOW - timedelta(days=1))
    fake.enabled["a2"] = False
    paco_upload("a2")
    await price_gate.process_run(run1, now=w.NOW - timedelta(days=1))
    run2 = w.make_run()
    w.add_snap(run2, "a2", color="verde", prices=GREEN_PRICES, enabled=False)
    assert await price_gate.process_run(run2, now=w.NOW) == {"activado": 1}
    assert [i.state for i in w.items()] == [pr.ACTIVADO] and fake.enabled["a2"] is True


async def test_daily_cap_keeps_the_rest_in_the_tray(fake):
    w.set_setting("pm_gate_activations_daily_max", 1)
    run = w.make_run()
    for pid in ("c1", "c2"):
        w.add_snap(run, pid, color="verde", prices=GREEN_PRICES, enabled=False)
        fake.enabled[pid] = False
        paco_upload(pid)
    counts = await price_gate.process_run(run, now=w.NOW)
    assert counts == {"activado": 1, "tope": 1} and fake.writes() == [True]
    assert sorted(i.state for i in w.items()) == [pr.ACTIVADO, pr.POR_ACTIVAR]


async def test_mode_1_or_0_never_runs_the_gate(fake):
    run = w.make_run()
    w.add_snap(run, "m1", color="verde", prices=GREEN_PRICES, enabled=False)
    fake.enabled["m1"] = False
    paco_upload("m1")
    for mode in (0, 1):
        w.set_setting("pm_mode", mode)
        assert await price_gate.process_run(run, now=w.NOW) == {}
        assert await price_gate.on_vendure_events(["m1"], now=w.NOW) == {}
    assert fake.writes() == [] and w.items() == []


async def test_approval_pending_blocks_the_activation(fake):
    run = w.make_run()
    w.add_snap(run, "ap", color="verde", prices=GREEN_PRICES, enabled=False)
    fake.enabled["ap"], fake.approval["ap"] = False, "pending"
    paco_upload("ap")
    assert await price_gate.process_run(run, now=w.NOW) == {"activar_frenado": 1}
    assert fake.writes() == [] and [i.state for i in w.items()] == [pr.POR_ACTIVAR]
    assert intake("ap").gate_status == "bandeja"


async def test_old_uploads_and_products_paco_did_not_upload_are_left_alone(fake):
    run = w.make_run()
    w.add_snap(run, "old", color="verde", prices=GREEN_PRICES, enabled=False)
    w.add_snap(run, "nopaco", color="verde", prices=GREEN_PRICES, enabled=False)
    fake.enabled.update(old=False, nopaco=False)
    paco_upload("old", uploaded_days_ago=60)
    assert await price_gate.process_run(run, now=w.NOW) == {}
    assert fake.writes() == []


# ─── rojo firme prendido a mano ──────────────────────────────────


async def test_red_firm_turned_on_by_hand_goes_back_off_with_the_motive(fake):
    run = w.make_run()
    w.add_snap(run, "h1", color="rojo", enabled=False)        # la noche lo vio apagado y rojo firme
    fake.enabled["h1"] = True                                 # alguien lo prendió a mano
    counts = await price_gate.on_vendure_events(["h1"], now=w.NOW)
    assert counts == {"reapagado": 1} and fake.enabled["h1"] is False
    [item] = w.items()
    assert item.state == pr.EN_REVISION and item.hugo_disabled is True
    ks = kit.kinds(int(item.id))
    assert "porton" in ks and "decision_inactivar_auto" in ks and "inactivado" in ks
    with Session(engine) as s:
        assert s.exec(select(AuditLog).where(AuditLog.product_id == "h1",
                                             AuditLog.action == "price_review_disabled")).first() is not None
    # Deshacer (una persona) lo prende de nuevo, y Hugo NO vuelve a pelear.
    r = kit.post(kit.client(), int(item.id), "undo", expected_state=pr.EN_REVISION)
    assert r.status_code == 200, r.text
    assert fake.enabled["h1"] is True
    assert await price_gate.on_vendure_events(["h1"], now=w.NOW) == {"sin_escribir": 1}
    assert fake.enabled["h1"] is True


async def test_events_ignore_green_still_off_and_kept_products(fake):
    run = w.make_run()
    w.add_snap(run, "e1", color="verde", prices=GREEN_PRICES, enabled=False)
    w.add_snap(run, "e2", color="rojo", enabled=False)
    w.add_snap(run, "e3", color="rojo", enabled=True)         # ya estaba prendido: eso es la bandeja de siempre
    fake.enabled.update(e1=True, e2=False, e3=True)
    assert await price_gate.on_vendure_events(["e1", "e2", "e3"], now=w.NOW) == {"sigue_apagado": 1}
    assert fake.writes() == []


async def test_inactivation_cap_and_reads_cap(fake):
    w.set_setting("pm_gate_inactivations_daily_max", 0)
    run = w.make_run()
    w.add_snap(run, "k1", color="rojo", enabled=False)
    fake.enabled["k1"] = True
    assert await price_gate.on_vendure_events(["k1"], now=w.NOW) == {"sin_escribir": 1}
    w.set_setting("pm_gate_reads_daily_max", 0)
    assert await price_gate.on_vendure_events(["k1"], now=w.NOW) == {"tope_lecturas": 1}
    assert fake.writes() == []


def test_humans_cannot_call_the_automatic_actions(fake):
    item_id = w.make_item("z1", pr.POR_ACTIVAR)
    with pytest.raises(actions.ActionError) as exc:
        import asyncio
        asyncio.run(actions.act(item_id, actions.ACTIVAR_AUTO, "pao@b2box.pro", expected_state=pr.POR_ACTIVAR))
    assert exc.value.status == 400
    r = kit.decide(kit.client(), item_id, "activar_auto", pr.POR_ACTIVAR)
    assert r.status_code == 422
