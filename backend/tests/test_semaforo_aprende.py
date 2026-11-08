"""Semáforo automático con pm_mode = 2, la persona manda y el semáforo aprende.

1. Automático completo: un rojo firme PUBLICADO (ficha `propuesto` de la noche) se apaga solo, del peor margen al mejor y
   con el tope diario.
2. La persona manda: si alguien prende un rojo o apaga un verde (en Vendure o desde la bandeja), Hugo no lo revierte,
   anota la corrección y no actúa solo sobre el producto por `pm_correction_hold_days` días salvo que cambie su precio.
   «Los de ML no eran el mismo producto» alimenta `market_match_feedback`.
3. Aprender: el job semanal propone (o aplica) cortes por segmento, acotados, con historial y Deshacer; los aplicados
   solo frenan acciones automáticas (van a la bandeja).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import delete
from sqlmodel import Session, select

from app.db.models import (
    AuditLog,
    LuisIntake,
    MarketMatchFeedback,
    PriceFactorSample,
    SemaforoAjuste,
    SemaforoCorreccion,
)
from app.db.session import engine
from app.pricing import price_gate
from app.pricing import price_review as pr
from app.pricing import semaforo_aprende as learn
from app.pricing import semaforo_correcciones as corr
from app.scheduler import jobs
from tests import review_decide as kit
from tests import review_world as w

GREEN_PRICES = (1_900_000, 1_950_000, 2_000_000)   # mediana 1.950.000 contra nuestro 1.000.000 → verde firme
BASE = "/api/price-review"


@pytest.fixture
def fake(monkeypatch):
    f = kit.setup(monkeypatch, mode=2)
    with Session(engine) as s:
        s.execute(delete(LuisIntake))
        s.execute(delete(PriceFactorSample))
        s.commit()
    yield f
    with Session(engine) as s:
        s.execute(delete(LuisIntake))
        s.execute(delete(PriceFactorSample))
        s.commit()


def corrections() -> list[SemaforoCorreccion]:
    with Session(engine) as s:
        return list(s.exec(select(SemaforoCorreccion).order_by(SemaforoCorreccion.id)).all())  # type: ignore[arg-type]


def adjustments() -> list[SemaforoAjuste]:
    with Session(engine) as s:
        return list(s.exec(select(SemaforoAjuste).order_by(SemaforoAjuste.id)).all())  # type: ignore[arg-type]


def published_red(run: int, pid: str, *, prices: tuple[int, ...] = w.OK_PRICES, our: int = 1_000_000) -> int:
    """Un rojo firme publicado como lo deja la pasada de la noche: snapshot `firme` y ficha `propuesto` de esa corrida."""
    snap = w.add_snap(run, pid, color="rojo", prices=prices, our=our, enabled=True, flag="firme")
    return w.make_item(pid, pr.PROPUESTO, last_run_id=run, last_snapshot_id=snap, opened_snapshot_id=snap)


def paco_upload(pid: str, *, status: str | None = None, gate_at=None) -> int:
    with Session(engine) as s:
        row = LuisIntake(offer_id=f"o{pid}", source_url=f"https://detail.1688.com/offer/{pid}.html", name="x",
                         image_url="https://cbu01.alicdn.com/x.jpg", vendure_product_id=pid,
                         uploaded_at=w.NOW - timedelta(days=1), gate_status=status, gate_at=gate_at)
        s.add(row)
        s.commit()
        return int(row.id)  # type: ignore[arg-type]


def intake(pid: str) -> LuisIntake:
    with Session(engine) as s:
        return s.exec(select(LuisIntake).where(LuisIntake.vendure_product_id == pid)).one()


def corr_row(pid: str, *, color: str = "rojo", decision: str = "activar", margin: float | None = 8.0,
             listings: int | None = 6, our: int = 3_000_000, ratio: float | None = 1.1, category: str | None = None,
             motive: str | None = None, days_ago: float = 1) -> SemaforoCorreccion:
    with Session(engine, expire_on_commit=False) as s:
        row = SemaforoCorreccion(product_id=pid, at=w.NOW - timedelta(days=days_ago), hugo_color=color,
                                 human_decision=decision, margin_pct=margin, firm_listings=listings,
                                 our_price_cents=our, price_ratio=ratio, category=category, motive=motive,
                                 hold_until=w.NOW - timedelta(days=1))
        s.add(row)
        s.commit()
        return row


# ─── 1. automático completo: rojos publicados ─────────────────────


async def test_published_red_firm_is_turned_off_alone_worst_margin_first_with_the_cap(fake):
    w.set_setting("pm_gate_inactivations_daily_max", 1)
    run = w.make_run()
    a = published_red(run, "pa", our=1_000_000)                 # margen ≈ −22 %
    b = published_red(run, "pb", our=1_300_000)                 # peor margen: va primero
    fake.enabled.update(pa=True, pb=True)
    counts = await price_gate.process_run(run, now=w.NOW)
    assert counts == {"apagado": 1, "rojo_sin_escribir": 1}
    assert fake.enabled == {"pa": True, "pb": False} and fake.writes() == [False]
    assert w.item(b).state == pr.EN_REVISION and w.item(b).hugo_disabled is True
    assert w.item(a).state == pr.PROPUESTO                      # el tope: queda para Pao
    assert "decision_inactivar_auto" in kit.kinds(b)


async def test_mode_1_never_turns_off_published_reds(fake):
    w.set_setting("pm_mode", 1)
    run = w.make_run()
    published_red(run, "m1")
    fake.enabled["m1"] = True
    assert await price_gate.process_run(run, now=w.NOW) == {}
    assert fake.writes() == []


async def test_pao_can_undo_the_automatic_inactivation_and_it_is_a_correction(fake):
    run = w.make_run()
    item_id = published_red(run, "u1")
    fake.enabled["u1"] = True
    assert await price_gate.process_run(run, now=w.NOW) == {"apagado": 1}
    r = kit.post(kit.client(), item_id, "undo", expected_state=pr.EN_REVISION)
    assert r.status_code == 200, r.text
    assert fake.enabled["u1"] is True
    [c] = corrections()
    assert (c.hugo_color, c.human_decision, c.origin, c.actor) == ("rojo", "activar", "bandeja", kit.PAO)


# ─── 2. la persona manda ──────────────────────────────────────────


async def test_a_correction_holds_hugo_until_the_price_changes(fake):
    # Alguien prende a mano un rojo firme que Hugo había visto apagado: corrección.
    run0 = w.make_run(started=w.NOW - timedelta(days=1))
    w.add_snap(run0, "p1", color="rojo", enabled=False, captured_at=w.NOW - timedelta(days=1))
    fake.enabled["p1"] = True
    assert await price_gate.on_vendure_events(["p1"], now=w.NOW - timedelta(hours=20)) == {"corregido_a_mano": 1}
    # La noche siguiente sigue rojo firme publicado, con el MISMO precio: Hugo no lo apaga.
    run1 = w.make_run()
    published_red(run1, "p1")
    assert await price_gate.process_run(run1, now=w.NOW) == {"rojo_sin_escribir": 1}
    assert fake.writes() == []
    # Cambió el precio (sigue rojo firme): la corrección ya no frena.
    run2 = w.make_run(started=w.NOW + timedelta(days=1))
    with Session(engine) as s:
        old = s.exec(select(pr.PriceReviewItem).where(pr.PriceReviewItem.open_key == "p1")).one()
        s.delete(old)
        s.commit()
    snap = w.add_snap(run2, "p1", color="rojo", our=1_100_000, enabled=True, flag="firme",
                      captured_at=w.NOW + timedelta(days=1), data_at=w.NOW + timedelta(days=1))
    w.make_item("p1", pr.PROPUESTO, last_run_id=run2, last_snapshot_id=snap)
    assert await price_gate.process_run(run2, now=w.NOW + timedelta(days=1)) == {"apagado": 1}
    assert fake.enabled["p1"] is False


async def test_hold_days_flag(fake):
    w.set_setting("pm_correction_hold_days", 7)
    run = w.make_run()
    w.add_snap(run, "hd", color="rojo", enabled=False)
    fake.enabled["hd"] = True
    await price_gate.on_vendure_events(["hd"], now=w.NOW)
    [c] = corrections()
    assert c.hold_until == w.NOW + timedelta(days=7)
    with Session(engine) as s:
        assert corr.held(s, "hd", 1_000_000, w.NOW + timedelta(days=6))
        assert not corr.held(s, "hd", 1_000_000, w.NOW + timedelta(days=8))
        assert not corr.held(s, "hd", 1_200_000, w.NOW + timedelta(days=1))      # otro precio


async def test_paco_red_turned_on_by_hand_is_recorded_not_reverted(fake):
    run = w.make_run()
    w.add_snap(run, "r1", color="rojo", enabled=True)
    fake.enabled["r1"] = True
    paco_upload("r1", status=price_gate.G_ROJO)
    assert await price_gate.process_run(run, now=w.NOW) == {"corregido_a_mano": 1}
    assert fake.writes() == [] and intake("r1").gate_status == price_gate.G_YA_ACTIVO
    [c] = corrections()
    assert (c.hugo_color, c.human_decision) == ("rojo", "activar")


async def test_green_activated_by_hugo_then_turned_off_by_a_person(fake):
    run0 = w.make_run(started=w.NOW - timedelta(days=1))
    w.add_snap(run0, "g1", color="verde", prices=GREEN_PRICES, enabled=False, captured_at=w.NOW - timedelta(days=1))
    fake.enabled["g1"] = False
    paco_upload("g1")
    assert await price_gate.process_run(run0, now=w.NOW - timedelta(days=1)) == {"activado": 1}
    # Alguien lo apaga en Vendure; la noche lo mide apagado y verde: corrección, Hugo no lo vuelve a prender.
    fake.enabled["g1"] = False
    run1 = w.make_run()
    w.add_snap(run1, "g1", color="verde", prices=GREEN_PRICES, enabled=False)
    assert await price_gate.process_run(run1, now=w.NOW) == {"apagado_a_mano": 1}
    assert intake("g1").gate_status == price_gate.G_APAGADO_A_MANO
    [c] = corrections()
    assert (c.hugo_color, c.human_decision, c.origin) == ("verde", "inactivar", "vendure")
    run2 = w.make_run(started=w.NOW + timedelta(days=1))
    w.add_snap(run2, "g1", color="verde", prices=GREEN_PRICES, enabled=False, captured_at=w.NOW + timedelta(days=1),
               data_at=w.NOW + timedelta(days=1))
    assert await price_gate.process_run(run2, now=w.NOW + timedelta(days=1)) == {}
    assert fake.enabled["g1"] is False


async def test_vendure_event_green_turned_off_is_a_correction_unless_hugo_did_it(fake):
    run = w.make_run()
    w.add_snap(run, "v1", color="verde", prices=GREEN_PRICES, enabled=True)
    w.add_snap(run, "v2", color="verde", prices=GREEN_PRICES, enabled=True)
    w.add_snap(run, "v3", color="verde", prices=GREEN_PRICES, enabled=True)
    fake.enabled.update(v1=False, v2=False, v3=True)
    with Session(engine) as s:
        s.add(AuditLog(action="duplicate_disabled", source="audit", product_id="v2", created_at=w.NOW))
        s.commit()
    counts = await price_gate.on_vendure_events(["v1", "v2", "v3"], now=w.NOW)
    assert counts == {"corregido_a_mano": 1, "apagado_por_hugo": 1, "sigue_prendido": 1}
    assert [(c.product_id, c.human_decision) for c in corrections()] == [("v1", "inactivar")]
    with Session(engine) as s:
        s.execute(delete(AuditLog).where(AuditLog.product_id == "v2"))  # type: ignore[arg-type]
        s.commit()


async def test_mantener_with_match_mal_is_a_correction_and_the_motive_feeds_match_feedback(fake):
    run = w.make_run()
    item_id = published_red(run, "mm")
    w.set_setting("pm_gate_inactivations_daily_max", 0)
    fake.enabled["mm"] = True
    await price_gate.process_run(run, now=w.NOW)
    r = kit.decide(kit.client(), item_id, "mantener", pr.PROPUESTO, code="match_mal",
                   text="Las publicaciones de ML son de otro producto")
    assert r.status_code == 200, r.text
    [c] = corrections()
    assert (c.human_decision, c.motive, c.origin) == ("mantener", "match_malo", "bandeja")
    # En el panel marca cuál publicación no era el mismo producto: va a market_match_feedback.
    c2 = kit.client()
    bad = c2.post(f"{BASE}/corrections/{c.id}/motive", json={"motive": "match_malo", "ml_ids": ["MLA9999"]})
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "publicacion_ajena"
    ok = c2.post(f"{BASE}/corrections/{c.id}/motive", json={"motive": "match_malo", "ml_ids": ["MLA1001"]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["bad_ml_ids"] == ["MLA1001"]
    with Session(engine) as s:
        fb = s.exec(select(MarketMatchFeedback).where(MarketMatchFeedback.product_id == "mm")).one()
    assert (fb.ml_id, fb.label, fb.motive) == ("MLA1001", 0, "otro_producto")


def test_motive_validation_and_permissions(fake):
    run = w.make_run()
    snap = w.add_snap(run, "mv", color="rojo", enabled=False)
    c = corr.record(w.snap_of(run, "mv"), corr.ACTIVAR, actor="vendure", origin="vendure", now=w.NOW)
    assert c is not None and c.snapshot_id == snap
    url = f"{BASE}/corrections/{c.id}/motive"
    assert kit.client("otra@b2box.pro").post(url, json={"motive": "precio_ok"}).status_code == 403
    assert kit.client().post(url, json={"motive": "otro"}).json()["detail"]["code"] == "falta_texto"
    assert kit.client().post(url, json={"motive": "match_malo"}).json()["detail"]["code"] == "faltan_publicaciones"
    assert kit.client().post(url, json={"motive": "precio_ok", "ml_ids": ["MLA1001"]}).status_code == 400
    assert kit.client().post(url, json={"motive": "nada"}).status_code == 422
    r = kit.client().post(url, json={"motive": "precio_ok", "note": "  alcanza  "})
    assert r.status_code == 200 and r.json()["motive"] == "precio_ok" and r.json()["note"] == "alcanza"
    listed = kit.client().get(f"{BASE}/corrections").json()
    assert listed["total"] == 1 and listed["hold_days"] == 60
    assert listed["items"][0]["listings"][0]["ml_id"] == "MLA1001"


def test_record_only_when_the_person_goes_against_the_color(fake):
    run = w.make_run()
    w.add_snap(run, "a1", color="amarillo", prices=(1_350_000, 1_380_000, 1_400_000), enabled=True)
    w.add_snap(run, "r1", color="rojo", enabled=True)
    w.add_snap(run, "g1", color="verde", prices=GREEN_PRICES, enabled=True)
    snap = w.snap_of
    assert corr.record(snap(run, "a1"), corr.ACTIVAR, actor="x", origin="vendure", now=w.NOW) is None
    assert corr.record(snap(run, "r1"), corr.INACTIVAR, actor="x", origin="vendure", now=w.NOW) is None
    assert corr.record(snap(run, "g1"), corr.ACTIVAR, actor="x", origin="vendure", now=w.NOW) is None
    with Session(engine) as s:
        s.add(PriceFactorSample(product_id="g1", category="Hogar", captured_at=w.NOW))
        s.commit()
    c = corr.record(snap(run, "g1"), corr.INACTIVAR, actor="x", origin="vendure", now=w.NOW)
    assert c is not None and c.category == "Hogar" and c.hugo_firm is True and c.margin_pct is not None


# ─── 3. aprender ──────────────────────────────────────────────────


def test_propose_border_margin_lowers_the_yellow_cut_within_bounds():
    p = pr.ReviewParams()
    cs = [corr_row(f"b{i}", margin=m) for i, m in enumerate((6.0, 7.0, 8.0, 9.0, 9.5))]
    [prop] = [x for x in learn.propose(cs, p, {}, min_corrections=5, max_shift=5.0) if x.segment == "margen_borde"]
    assert (prop.direction, prop.param, prop.old_value, prop.new_value) == ("rojo", "yellow_min", 10.0, 7.5)
    assert prop.n_corrections == 5 and "5 de 5" in prop.reason
    # Con un tope de 1 punto, 7,5 % queda fuera: no se propone (el motivo de esas correcciones es otro).
    assert [x for x in learn.propose(cs, p, {}, min_corrections=5, max_shift=1.0) if x.segment == "margen_borde"] == []


def test_propose_needs_concentration_and_enough_corrections():
    p = pr.ReviewParams()
    few = [corr_row(f"f{i}", margin=8.0) for i in range(4)]
    assert learn.propose(few, p, {}, min_corrections=5, max_shift=5.0) == []
    spread = [corr_row(f"s{i}", margin=8.0, our=3_000_000) for i in range(5)] + \
        [corr_row(f"t{i}", margin=-30.0, our=30_000_000) for i in range(6)]
    segs = {x.segment for x in learn.propose(spread, p, {}, min_corrections=5, max_shift=5.0)}
    assert "margen_borde" not in segs                           # 5 de 11: no se concentra
    assert "precio:muy_alto" not in segs                        # 6 de 11, pero una ganancia de −30 % no se ajusta


def test_propose_few_identicals_green_side_and_doubtful_match():
    p = pr.ReviewParams()
    few = [corr_row(f"p{i}", listings=3 + i % 2, margin=-15.0) for i in range(5)]
    [prop] = [x for x in learn.propose(few, p, {}, min_corrections=5, max_shift=5.0) if x.segment == "pocos_identicos"]
    assert (prop.param, prop.old_value, prop.new_value) == ("min_listings", 3.0, 5.0)
    greens = [corr_row(f"g{i}", color="verde", decision="inactivar", margin=m, listings=8)
              for i, m in enumerate((31.0, 32.0, 33.0, 34.0, 35.0))]
    [g] = [x for x in learn.propose(greens, p, {}, min_corrections=5, max_shift=5.0) if x.segment == "margen_borde"]
    assert (g.direction, g.param, g.new_value) == ("verde", "green_min", 33.5)
    bad = [corr_row(f"m{i}", ratio=2.4 + i / 10, margin=-60.0, listings=8, motive="match_malo") for i in range(5)]
    [m] = [x for x in learn.propose(bad, p, {}, min_corrections=5, max_shift=5.0) if x.segment == "match_dudoso"]
    assert (m.param, m.new_value) == ("max_price_ratio", 2.35)
    # Ya aplicado ese mismo corte: no se vuelve a proponer.
    assert [x for x in learn.propose(bad, p, {("match_dudoso", "rojo", "max_price_ratio"): 2.35}, min_corrections=5,
                                     max_shift=5.0) if x.segment == "match_dudoso"] == []


def test_blocking_rule_is_pure_and_per_segment():
    p = pr.ReviewParams()
    rule = learn.Rule(1, "margen_borde", "rojo", "yellow_min", 7.0)
    assert learn.blocking_rule([rule], "rojo", {"margin_pct": 8.0, "firm_listings": 6}, p) == rule
    assert learn.blocking_rule([rule], "rojo", {"margin_pct": 6.0, "firm_listings": 6}, p) is None
    assert learn.blocking_rule([rule], "verde", {"margin_pct": 8.0}, p) is None
    assert learn.blocking_rule([rule], "rojo", {"margin_pct": -20.0}, p) is None          # no está en el segmento
    cat = learn.Rule(2, "categoria:Hogar", "verde", "green_min", 35.0)
    assert learn.blocking_rule([cat], "verde", {"margin_pct": 33.0, "category": "Hogar"}, p) == cat
    assert learn.blocking_rule([cat], "verde", {"margin_pct": 33.0, "category": "Otra"}, p) is None


def test_learning_modes_history_replace_and_undo(fake):
    for i, m in enumerate((6.0, 7.0, 8.0, 9.0, 9.5)):
        corr_row(f"l{i}", margin=m)
    w.set_setting("pm_learn_mode", 0)
    assert learn.run_learning(w.NOW) == {"modo": 0} and adjustments() == []
    w.set_setting("pm_learn_mode", 1)
    out = learn.run_learning(w.NOW)
    assert out["propuestos"] >= 1 and out["aplicados"] == 0
    first = next(a for a in adjustments() if a.segment == "margen_borde")
    assert first.status == learn.PROPUESTO and first.evidence and learn.active_rules() == []
    assert learn.run_learning(w.NOW)["propuestos"] == 0          # no repite lo pendiente
    learn.apply(int(first.id), kit.PAO, now=w.NOW)
    assert [r.segment for r in learn.active_rules()] == ["margen_borde"]
    with pytest.raises(learn.LearnError):
        learn.apply(int(first.id), kit.PAO)
    # Más correcciones, un corte más bajo, aplicado solo con modo 2: reemplaza al anterior.
    for i, m in enumerate((5.0, 5.5, 6.0, 6.0, 6.5)):
        corr_row(f"n{i}", margin=m)
    w.set_setting("pm_learn_mode", 2)
    assert learn.run_learning(w.NOW)["aplicados"] >= 1
    second = next(a for a in adjustments() if a.segment == "margen_borde" and a.id != first.id)
    assert second.status == learn.APLICADO and second.decided_by == pr.ACTOR_AUTO and second.replaces_id == first.id
    assert second.new_value < first.new_value
    assert {a.id: a.status for a in adjustments()}[first.id] == learn.REEMPLAZADO
    # Deshacer el nuevo vuelve a poner el anterior.
    learn.undo(int(second.id), kit.PAO, now=w.NOW)
    st = {a.id: a.status for a in adjustments()}
    assert st[second.id] == learn.DESHECHO and st[first.id] == learn.APLICADO


def test_reject_and_window(fake):
    for i in range(5):
        corr_row(f"o{i}", margin=8.0, days_ago=200)                # fuera de la ventana de 90 días
    w.set_setting("pm_learn_mode", 1)
    assert learn.run_learning(w.NOW)["propuestos"] == 0
    for i in range(5):
        corr_row(f"r{i}", margin=8.0)
    learn.run_learning(w.NOW)
    adj = next(a for a in adjustments() if a.segment == "margen_borde")
    learn.reject(int(adj.id), kit.PAO, now=w.NOW)
    assert learn.active_rules() == []
    with pytest.raises(learn.LearnError):
        learn.undo(int(adj.id), kit.PAO)


async def test_an_applied_adjustment_sends_the_case_to_the_tray_instead_of_acting(fake):
    with Session(engine) as s:
        s.add(SemaforoAjuste(segment="pocos_identicos", direction="rojo", param="min_listings", old_value=3,
                             new_value=5, status=learn.APLICADO, reason="test"))
        s.add(SemaforoAjuste(segment="pocos_identicos", direction="verde", param="min_listings", old_value=3,
                             new_value=5, status=learn.APLICADO, reason="test"))
        s.commit()
    rules = learn.active_rules()
    run = w.make_run()
    published_red(run, "ar")                                     # 3 idénticos firmes: cae en el segmento
    w.add_snap(run, "ag", color="verde", prices=GREEN_PRICES, enabled=False)
    fake.enabled.update(ar=True, ag=False)
    paco_upload("ag")
    decision = price_gate.classify(w.snap_of(run, "ag"), pr.ReviewParams(), w.NOW, rules)
    assert decision.action == price_gate.BANDEJA and decision.reason.startswith("ajuste:")
    counts = await price_gate.process_run(run, now=w.NOW)
    assert counts == {"rojo_publicado_a_bandeja": 1, "bandeja": 1}
    assert fake.writes() == []
    assert "segmento ajustado" in intake("ag").gate_reason


def test_adjustment_api(fake):
    for i, m in enumerate((6.0, 7.0, 8.0, 9.0, 9.5)):
        corr_row(f"x{i}", margin=m)
    w.set_setting("pm_learn_mode", 1)
    c = kit.client()
    assert kit.client("otra@b2box.pro").post(f"{BASE}/adjustments/run").status_code == 403
    r = c.post(f"{BASE}/adjustments/run")
    assert r.status_code == 200 and r.json()["propuestos"] >= 1
    listed = c.get(f"{BASE}/adjustments").json()
    assert listed["mode"] == 1 and listed["can_decide"] is True
    adj = next(a for a in listed["items"] if a["segment"] == "margen_borde")
    assert adj["status"] == "propuesto" and adj["reason"] and adj["evidence"]
    assert c.post(f"{BASE}/adjustments/{adj['id']}/undo").status_code == 409
    assert c.post(f"{BASE}/adjustments/{adj['id']}/approve").json()["status"] == "aplicado"
    assert c.post(f"{BASE}/adjustments/{adj['id']}/undo").json()["status"] == "deshecho"
    assert c.post(f"{BASE}/adjustments/999999/approve").status_code == 404


async def test_weekly_job_runs_the_learning(fake):
    w.set_setting("pm_learn_mode", 0)
    assert await jobs.semaforo_learn() == {"modo": 0}
