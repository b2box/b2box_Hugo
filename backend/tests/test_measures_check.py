"""Chequeo de medidas con IA barata (pricing/measures_check.py) y el portón sobre el catálogo apagado (price_gate, paso 3):
un verde firme apagado se activa solo si sus medidas (producto / caja / master) tienen sentido; los rojos firmes publicados
se apagan ya con «Aplicar ahora»."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete
from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import AuditLog, LuisIntake, MeasuresCheck, SemaforoCorreccion
from app.db.session import engine
from app.pricing import market_judge, market_specs, measures_check, price_gate
from app.pricing import price_review as pr
from app.vendure import client as vclient
from tests import review_decide as kit
from tests import review_world as w

GREEN_PRICES = (1_900_000, 1_950_000, 2_000_000)
GOOD_SPECS = {"length": 20, "width": 10, "height": 5, "weight": 0.4,
              "box_length": 22, "box_width": 12, "box_height": 7, "box_weight": 0.5}


class FakeAI:
    """Cliente OpenAI-compatible falso: devuelve `reply` y cuenta las llamadas."""

    def __init__(self, reply: dict[str, Any] | str) -> None:
        self.reply = reply if isinstance(reply, str) else json.dumps(reply)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **request: Any) -> Any:
        self.calls.append(request)
        msg = SimpleNamespace(content=self.reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                               usage=SimpleNamespace(prompt_tokens=800, completion_tokens=60))

    async def close(self) -> None:
        pass


AI_OK = {"producto": "ok", "caja": "ok", "master": "sin_dato", "motivo": "Medidas razonables para una lámpara"}
AI_BAD = {"producto": "ok", "caja": "mal", "master": "sin_dato", "motivo": "La caja tiene un cero de más"}


@pytest.fixture
def fake(monkeypatch):
    f = kit.setup(monkeypatch, mode=2)
    w.set_setting("pm_gate_catalog_greens", 1)
    w.set_setting("pm_gate_measures_check", 1)
    with Session(engine) as s:
        s.execute(delete(LuisIntake))
        s.execute(delete(AuditLog).where(AuditLog.product_id.like("cg%")))  # type: ignore[attr-defined]
        s.commit()
    yield f
    with Session(engine) as s:
        s.execute(delete(LuisIntake))
        s.execute(delete(MeasuresCheck))
        s.commit()


@pytest.fixture
def ai(monkeypatch):
    """La IA prendida con modo url (sin bajar fotos) y un cliente falso que responde OK."""
    box: dict[str, FakeAI] = {"client": FakeAI(AI_OK)}
    monkeypatch.setattr(market_judge, "enabled", lambda: True)
    monkeypatch.setattr(market_judge, "image_mode", lambda: market_judge.IMAGE_MODE_URL)
    monkeypatch.setattr(market_judge, "make_client", lambda: box["client"])
    return box


def specs(**over: float) -> dict[str, float]:
    return {**GOOD_SPECS, **over}


def levels(**over: float) -> measures_check.Levels:
    return measures_check.Levels.from_specs({k: float(v) for k, v in specs(**over).items()})


# ─── Reglas puras ─────────────────────────────────────────────────


def test_rules_accept_sane_measures():
    assert measures_check.rule_problems(levels()) == []


def test_rules_catch_impossible_boxes():
    small_box = measures_check.rule_problems(levels(box_length=8, box_width=6, box_height=3))
    assert [lvl for lvl, _ in small_box] == ["caja"] and "no entra" in small_box[0][1]
    huge_box = measures_check.rule_problems(levels(box_length=220, box_width=120, box_height=70))
    assert any("veces el volumen" in p for _, p in huge_box)
    light_box = measures_check.rule_problems(levels(box_weight=0.04))
    assert any("pesa" in p for _, p in light_box)
    out_of_range = measures_check.rule_problems(levels(length=2000))
    assert any("fuera de rango" in p for _, p in out_of_range)


def test_rules_check_the_master_box():
    ok = levels(master_length=50, master_width=40, master_height=30, master_weight=12, master_units=20)
    assert measures_check.rule_problems(ok) == []
    too_many = levels(master_length=50, master_width=40, master_height=30, master_weight=12, master_units=500)
    assert any("no entran 500 cajas" in p for _, p in measures_check.rule_problems(too_many))
    too_light = levels(master_length=50, master_width=40, master_height=30, master_weight=1, master_units=20)
    assert any("la master pesa" in p for _, p in measures_check.rule_problems(too_light))
    no_units = levels(master_length=50, master_width=40, master_height=30, master_weight=12)
    assert any("piezas" in p for _, p in measures_check.rule_problems(no_units))


def test_parse_ai_only_accepts_the_closed_json():
    v = measures_check.parse_ai("```json\n" + json.dumps(AI_BAD) + "\n```")
    assert v is not None and v.overall == measures_check.MAL and v.box == "mal"
    assert measures_check.parse_ai(json.dumps({**AI_OK, "caja": "quizás"})) is None
    assert measures_check.parse_ai("no sé") is None
    assert measures_check.parse_ai(json.dumps({**AI_OK, "producto": "dudoso"})).overall == measures_check.DUDOSO


# ─── Un producto ──────────────────────────────────────────────────


async def test_check_without_measures_or_with_broken_rules_does_not_call_the_ai(fake, ai):
    run = w.make_run()
    w.add_snap(run, "cg0", color="verde", enabled=False)
    w.add_snap(run, "cg1", color="verde", enabled=False, specs=specs(box_length=8, box_width=6, box_height=3))
    none = await measures_check.check_snapshot(w.snap_of(run, "cg0"))
    bad = await measures_check.check_snapshot(w.snap_of(run, "cg1"))
    assert none.verdict == measures_check.SIN_MEDIDAS and bad.verdict == measures_check.MAL
    assert bad.source == "reglas" and bad.box_verdict == "mal"
    assert ai["client"].calls == []


async def test_check_asks_the_ai_once_and_reuses_it_until_the_measures_change(fake, ai):
    run = w.make_run()
    w.add_snap(run, "cg2", color="verde", enabled=False, specs=specs())
    row = await measures_check.check_snapshot(w.snap_of(run, "cg2"))
    assert row.verdict == measures_check.OK and row.source == "ia" and row.cost_usd > 0
    assert len(ai["client"].calls) == 1
    text = json.dumps(ai["client"].calls[0]["messages"], ensure_ascii=False)
    assert "CAJA INDIVIDUAL: 22 × 12 × 7 cm" in text and "CAJA MASTER: no cargada" in text
    await measures_check.check_snapshot(w.snap_of(run, "cg2"))
    assert len(ai["client"].calls) == 1                     # cache: mismas medidas y mismo nombre
    run2 = w.make_run()
    w.add_snap(run2, "cg2", color="verde", enabled=False, specs=specs(box_weight=0.6))
    await measures_check.check_snapshot(w.snap_of(run2, "cg2"))
    assert len(ai["client"].calls) == 2                     # cambió la caja: se vuelve a mirar


async def test_check_respects_the_daily_cap_and_an_unreadable_answer_is_an_error(fake, ai):
    run = w.make_run()
    w.add_snap(run, "cg3", color="verde", enabled=False, specs=specs())
    w.set_setting("pm_measures_check_daily_max", 0)
    row = await measures_check.check_snapshot(w.snap_of(run, "cg3"))
    assert row.verdict == measures_check.ERROR and row.motive == "tope_del_dia" and ai["client"].calls == []
    w.set_setting("pm_measures_check_daily_max", 10)
    ai["client"] = FakeAI("no es json")
    row = await measures_check.check_snapshot(w.snap_of(run, "cg3"))
    assert row.verdict == measures_check.ERROR and row.motive == "ia_ilegible"


async def test_run_disabled_checks_only_the_disabled_products(fake, ai):
    run = w.make_run()
    w.add_snap(run, "cg4", color="verde", enabled=False, specs=specs())
    w.add_snap(run, "cg5", color="rojo", enabled=True, specs=specs())
    counts = await measures_check.run_disabled()
    assert counts == {"ok": 1}
    with Session(engine) as s:
        assert [r.product_id for r in s.exec(select(MeasuresCheck)).all()] == ["cg4"]
    assert await measures_check.run_disabled() == {}        # ya revisado: no se vuelve a pagar


# ─── El portón sobre el catálogo apagado ──────────────────────────


async def test_catalog_green_firm_with_sane_measures_is_activated(fake, ai):
    run = w.make_run()
    w.add_snap(run, "cg6", color="verde", prices=GREEN_PRICES, enabled=False, specs=specs())
    fake.enabled["cg6"] = False
    counts = await price_gate.process_run(run, now=w.NOW)
    assert counts == {"catalogo_activado": 1}
    assert fake.enabled["cg6"] is True and fake.writes() == [True]
    [item] = w.items()
    assert item.state == pr.ACTIVADO


async def test_catalog_green_with_bad_measures_goes_to_the_tray(fake, ai):
    ai["client"] = FakeAI(AI_BAD)
    run = w.make_run()
    w.add_snap(run, "cg7", color="verde", prices=GREEN_PRICES, enabled=False, specs=specs())
    w.add_snap(run, "cg8", color="verde", prices=GREEN_PRICES, enabled=False)          # sin medidas
    for pid in ("cg7", "cg8"):
        fake.enabled[pid] = False
    counts = await price_gate.process_run(run, now=w.NOW)
    assert counts == {"catalogo_medidas_a_bandeja": 2} and fake.writes() == []
    got = {i.product_id: i for i in w.items()}
    assert got["cg7"].state == pr.POR_ACTIVAR and "medidas_mal" in (got["cg7"].flags or "")
    motive = next(e.motive_text for e in w.events("porton") if e.product_id == "cg7")
    assert "un cero de más" in motive


async def test_catalog_never_turns_on_duplicates_or_what_a_person_turned_off(fake, ai):
    run = w.make_run()
    for pid in ("cg9", "cg10", "cg11"):
        w.add_snap(run, pid, color="verde", prices=GREEN_PRICES, enabled=False, specs=specs())
        fake.enabled[pid] = False
    with Session(engine) as s:
        s.add(AuditLog(action="duplicate_disabled", product_id="cg9", source="audit", detail="{}"))
        s.add(SemaforoCorreccion(product_id="cg10", at=w.NOW - timedelta(days=200), hugo_color="verde",
                                 human_decision="inactivar", our_price_cents=1_000_000,
                                 hold_until=w.NOW - timedelta(days=100)))
        s.commit()
    w.set_setting("pm_gate_catalog_greens", 0)
    assert await price_gate.process_run(run, now=w.NOW) == {}
    w.set_setting("pm_gate_catalog_greens", 1)
    counts = await price_gate.process_run(run, now=w.NOW)
    assert counts == {"catalogo_duplicado_o_bx": 1, "catalogo_corregido": 1, "catalogo_activado": 1}
    assert fake.enabled == {"cg9": False, "cg10": False, "cg11": True}


async def test_luis_green_without_measures_waits_in_the_tray_when_the_guard_is_on(fake, ai):
    run = w.make_run()
    w.add_snap(run, "cg12", color="verde", prices=GREEN_PRICES, enabled=False)
    fake.enabled["cg12"] = False
    with Session(engine) as s:
        s.add(LuisIntake(offer_id="ocg12", source_url="https://detail.1688.com/offer/1.html", name="x",
                         image_url="https://cbu01.alicdn.com/x.jpg", vendure_product_id="cg12",
                         uploaded_at=w.NOW - timedelta(days=1)))
        s.commit()
    assert await price_gate.process_run(run, now=w.NOW) == {"bandeja": 1}
    assert fake.writes() == []
    w.set_setting("pm_gate_measures_check", 0)
    run2 = w.make_run()
    w.add_snap(run2, "cg12", color="verde", prices=GREEN_PRICES, enabled=False)
    assert await price_gate.process_run(run2, now=w.NOW) == {"activado": 1}


async def test_apply_now_turns_off_published_red_firm_without_the_streak(fake, ai):
    run = w.make_run()
    w.add_snap(run, "cg13", color="rojo", enabled=True, specs=specs())
    fake.enabled["cg13"] = True
    assert await price_gate.process_run(run, now=w.NOW) == {}              # la noche espera la racha
    counts = await price_gate.process_run(run, now=w.NOW, immediate_reds=True)
    assert counts == {"apagado": 1} and fake.enabled["cg13"] is False
    [item] = w.items()
    assert item.state == pr.EN_REVISION


def test_apply_now_endpoint_is_admin_only_and_needs_mode_2(fake, monkeypatch):
    w.make_run()
    c = kit.client()
    assert c.post("/api/price-monitor/gate/apply-now").status_code == 403
    kit._use_settings(monkeypatch, dashboard_admins=kit.PAO, price_review_deciders=kit.DECIDERS)
    w.set_setting("pm_mode", 1)
    assert c.post("/api/price-monitor/gate/apply-now").status_code == 409
    w.set_setting("pm_mode", 2)
    r = c.post("/api/price-monitor/gate/apply-now")
    assert r.status_code == 202, r.text


def test_measures_endpoints_list_worst_first(fake):
    with Session(engine) as s:
        for pid, verdict in (("cg14", "ok"), ("cg15", "mal"), ("cg16", "dudoso")):
            s.add(MeasuresCheck(product_id=pid, product_name=f"P {pid}", product_enabled=False, verdict=verdict))
        s.commit()
    c = kit.client()
    r = c.get("/api/price-monitor/measures")
    assert r.status_code == 200, r.text
    assert [i["verdict"] for i in r.json()["items"]] == ["mal", "dudoso", "ok"]
    assert c.get("/api/price-monitor/measures?verdict=mal").json()["total"] == 1
    assert c.get("/api/price-monitor/measures/summary").json()["by_verdict"]["mal"] == 1


# ─── Caja master desde Vendure ────────────────────────────────────


def test_master_box_fields_are_read_only_when_configured(monkeypatch):
    custom = {"length": 20, "boxLength": 22, "mLen": 50, "mW": 40, "mH": 30, "mKg": 12, "mUnits": 20}
    monkeypatch.setattr(get_settings(), "vendure_master_box_fields", "")
    assert "master_length" not in market_specs.our_specs_from_custom_fields(custom).as_dict()
    monkeypatch.setattr(get_settings(), "vendure_master_box_fields", "mLen,mW,mH,mKg,mUnits")
    got = market_specs.our_specs_from_custom_fields(custom).as_dict()
    assert got["master_length"] == 50 and got["master_units"] == 20
    monkeypatch.setattr(vclient.VendureClient, "_master_supported", True)
    assert "mLen mW mH mKg mUnits" in vclient._variant_custom_fields()
    assert vclient.VendureClient._drop_unsupported_field(Exception('Cannot query field "mKg" on type "X"'))
    assert vclient.VendureClient._master_supported is False and vclient.VendureClient._dims_supported is True
    assert "mLen" not in vclient._variant_custom_fields()


def test_master_cbm_must_match_its_sides_and_can_stand_in_for_them():
    ok = levels(master_length=50, master_width=40, master_height=30, master_weight=12, master_units=20,
                master_cbm=0.06)
    assert measures_check.rule_problems(ok) == []
    wrong = levels(master_length=50, master_width=40, master_height=30, master_weight=12, master_units=20,
                   master_cbm=0.6)
    assert any("CBM" in p and "no coincide" in p for _, p in measures_check.rule_problems(wrong))
    only_cbm = levels(master_weight=12, master_units=500, master_cbm=0.06)
    assert any("no entran 500 cajas" in p for _, p in measures_check.rule_problems(only_cbm))
    assert "0.06 m³, 20 piezas" in ok.master_text()


def test_default_master_fields_are_the_vendure_ctn_ones(monkeypatch):
    monkeypatch.setattr(get_settings(), "vendure_master_box_fields",
                        type(get_settings()).model_fields["vendure_master_box_fields"].default)
    custom = {"ctnLength": 50, "ctnWidth": 40, "ctnHeight": 30, "ctnGrossWeight": 12.5, "pcsPerCtn": 24,
              "cbmPerCtn": 0.06}
    got = market_specs.our_specs_from_custom_fields(custom).as_dict()
    assert got == {"master_length": 50, "master_width": 40, "master_height": 30, "master_weight": 12.5,
                   "master_units": 24, "master_cbm": 0.06}


# ─── Verdes dudosos, tope propio y piloto automático ──────────────


async def test_catalog_green_that_is_not_firm_waits_in_the_tray(fake, ai):
    run = w.make_run()
    w.add_snap(run, "cg20", color="verde", prices=GREEN_PRICES, enabled=False, specs=specs(),
               sources=("llm", "llm", "llm"))                      # verde, pero sin idénticos confirmados por foto
    fake.enabled["cg20"] = False
    assert await price_gate.process_run(run, now=w.NOW) == {"catalogo_verde_dudoso_a_bandeja": 1}
    assert fake.writes() == [] and [i.state for i in w.items()] == [pr.POR_ACTIVAR]


async def test_catalog_has_its_own_daily_cap(fake, ai):
    w.set_setting("pm_gate_activations_daily_max", 0)                 # el de Paco no frena al catálogo
    w.set_setting("pm_gate_catalog_activations_daily_max", 1)
    run = w.make_run()
    for pid in ("cg21", "cg22"):
        w.add_snap(run, pid, color="verde", prices=GREEN_PRICES, enabled=False, specs=specs())
        fake.enabled[pid] = False
    counts = await price_gate.process_run(run, now=w.NOW)
    assert counts == {"catalogo_activado": 1, "catalogo_tope": 1} and fake.writes() == [True]


async def test_autopilot_turns_on_mode_2_once_and_runs_the_gate_once_per_fresh_run(fake, ai):
    from app import runtime
    from app.scheduler import jobs

    w.set_setting("pm_autopilot", 1)
    w.set_setting("pm_mode", 1)
    run = w.make_run()
    w.add_snap(run, "cg23", color="verde", prices=GREEN_PRICES, enabled=False, specs=specs())
    w.add_snap(run, "cg24", color="rojo", enabled=True, specs=specs())
    fake.enabled.update({"cg23": False, "cg24": True})
    out = await jobs.autopilot_kickoff(now=w.NOW + timedelta(hours=2))
    assert out["pm_mode"] == {"antes": 1, "ahora": 2} and int(runtime.get("pm_mode")) == 2
    assert out["porton"] == {"apagado": 1, "catalogo_activado": 1}
    assert fake.enabled == {"cg23": True, "cg24": False}
    again = await jobs.autopilot_kickoff(now=w.NOW + timedelta(hours=3))
    assert "ya la pasó" in again["porton"] and "pm_mode" not in again
    # Si una persona baja el modo, el piloto no lo vuelve a subir.
    w.set_setting("pm_mode", 1)
    third = await jobs.autopilot_kickoff(now=w.NOW + timedelta(hours=4))
    assert int(runtime.get("pm_mode")) == 1 and third["porton"] == "pm_mode distinto de 2"


async def test_autopilot_waits_for_a_fresh_run_and_can_be_turned_off(fake, ai):
    from app.scheduler import jobs

    w.set_setting("pm_autopilot", 1)
    run = w.make_run()
    w.add_snap(run, "cg25", color="verde", prices=GREEN_PRICES, enabled=False, specs=specs())
    fake.enabled["cg25"] = False
    stale = await jobs.autopilot_kickoff(now=w.NOW + timedelta(hours=50))
    assert "vieja" in stale["porton"] and fake.writes() == []
    w.set_setting("pm_autopilot", 0)
    assert await jobs.autopilot_kickoff(now=w.NOW) == {"apagado": True}
    assert fake.writes() == []


# ─── «No está en ML» cuenta como verde (pm_gate_sin_dato) ─────────


def _missing(run: int, pid: str, **kw: Any) -> None:
    """Un apagado que ML buscó y no tiene con precio (los textos de price_monitor._mark)."""
    base: dict[str, Any] = {"color": "sin_dato", "ml_status": "no_data", "prices": (), "enabled": False,
                            "specs": specs(), "ml_error": "ML no devolvió fichas para el título"}
    w.add_snap(run, pid, **{**base, **kw})


def _run_ml_ok(n_ok: int = 50) -> int:
    """Una corrida en la que ML sí contestó (encontró idénticos con precio para `n_ok` productos)."""
    from app.db.models import PriceMonitorRun

    run = w.make_run()
    with Session(engine) as s:
        r = s.get(PriceMonitorRun, run)
        r.n_ok = n_ok
        s.add(r)
        s.commit()
    return run


def _rule_of(pid: str) -> str | None:
    from app.db.models import PriceReviewEvent

    with Session(engine) as s:
        ev = s.exec(select(PriceReviewEvent).where(PriceReviewEvent.product_id == pid,
                                                   PriceReviewEvent.kind == "decision_activar_auto")).first()
    return json.loads(ev.payload or "{}").get("regla") if ev else None


def test_not_on_ml_only_when_ml_really_searched(fake):
    p = pr.ReviewParams()
    run = w.make_run()
    _missing(run, "nm1")                                                     # la API buscó y no hay
    _missing(run, "nm2", ml_error="API de ML apagada")                       # no se buscó
    _missing(run, "nm3", ml_error="API de ML apagada", web_state="empty")    # la web buscó y no hay
    _missing(run, "nm4", ml_error="API de ML apagada", web_state="blocked")  # ML bloqueó
    _missing(run, "nm5", ml_status="failed")                                 # la búsqueda falló
    _missing(run, "nm6", ml_status="skipped", unmeasurable="sin_precio")     # no se puede medir
    _missing(run, "nm7", estimated_color="rojo")                             # hay similares más baratos
    _missing(run, "nm8", our=0)                                              # sin precio nuestro
    _missing(run, "nm9", ml_error="las fichas no tienen vendedores que cuenten")  # ML sí lo tiene
    _missing(run, "nm10", data_at=w.NOW - timedelta(hours=60))               # dato viejo
    got = {pid: price_gate.classify(w.snap_of(run, pid), p, w.NOW, sin_dato_ok=True)
           for pid in ("nm1", "nm2", "nm3", "nm4", "nm5", "nm6", "nm7", "nm8", "nm9", "nm10")}
    assert {pid for pid, d in got.items() if d.action == price_gate.ACTIVAR} == {"nm1", "nm3"}
    assert got["nm1"].reason == "sin_dato_ml"
    # Sin la opción, «sin dato» sigue yendo a la bandeja.
    assert price_gate.classify(w.snap_of(run, "nm1"), p, w.NOW).reason == "sin_dato"


async def test_catalog_not_on_ml_with_sane_measures_is_activated_only_with_the_setting(fake, ai):
    run = _run_ml_ok()
    _missing(run, "cg40")
    fake.enabled["cg40"] = False
    w.set_setting("pm_gate_sin_dato", 0)
    assert await price_gate.process_run(run, now=w.NOW) == {}
    w.set_setting("pm_gate_sin_dato", 1)
    try:
        counts = await price_gate.process_run(run, now=w.NOW)
    finally:
        w.set_setting("pm_gate_sin_dato", 0)
    assert counts == {"catalogo_activado": 1}
    assert fake.enabled["cg40"] is True and fake.writes() == [True]
    assert _rule_of("cg40") == price_gate.RULE_CATALOG_SIN_DATO


async def test_catalog_not_on_ml_with_bad_or_missing_measures_goes_to_the_tray(fake, ai):
    ai["client"] = FakeAI(AI_BAD)
    run = _run_ml_ok()
    _missing(run, "cg41")
    _missing(run, "cg42", specs=None)
    for pid in ("cg41", "cg42"):
        fake.enabled[pid] = False
    w.set_setting("pm_gate_sin_dato", 1)
    try:
        counts = await price_gate.process_run(run, now=w.NOW)
    finally:
        w.set_setting("pm_gate_sin_dato", 0)
    assert counts == {"catalogo_medidas_a_bandeja": 2} and fake.writes() == []


async def test_not_on_ml_never_activates_without_the_measures_guard(fake, ai):
    run = _run_ml_ok()
    _missing(run, "cg43")
    fake.enabled["cg43"] = False
    w.set_setting("pm_gate_sin_dato", 1)
    w.set_setting("pm_gate_measures_check", 0)
    try:
        counts = await price_gate.process_run(run, now=w.NOW)
    finally:
        w.set_setting("pm_gate_sin_dato", 0)
        w.set_setting("pm_gate_measures_check", 1)
    assert counts == {} and fake.writes() == []


async def test_paco_upload_not_on_ml_with_sane_measures_is_activated(fake, ai):
    run = _run_ml_ok()
    _missing(run, "cg44")
    fake.enabled["cg44"] = False
    with Session(engine) as s:
        s.add(LuisIntake(offer_id="ocg44", source_url="https://detail.1688.com/offer/44.html", name="x",
                         image_url="https://cbu01.alicdn.com/x.jpg", vendure_product_id="cg44",
                         uploaded_at=w.NOW - timedelta(days=1)))
        s.commit()
    w.set_setting("pm_gate_sin_dato", 1)
    try:
        counts = await price_gate.process_run(run, now=w.NOW)
    finally:
        w.set_setting("pm_gate_sin_dato", 0)
    assert counts == {"activado": 1} and fake.enabled["cg44"] is True
    assert _rule_of("cg44") == price_gate.RULE_PACO_SIN_DATO


async def test_not_on_ml_waits_when_ml_barely_answered_in_the_run(fake, ai):
    run = _run_ml_ok(n_ok=3)                    # ML casi no devolvió nada: sus «no hay» no valen
    _missing(run, "cg45")
    fake.enabled["cg45"] = False
    w.set_setting("pm_gate_sin_dato", 1)
    try:
        counts = await price_gate.process_run(run, now=w.NOW)
    finally:
        w.set_setting("pm_gate_sin_dato", 0)
    assert counts == {} and fake.writes() == []


async def test_not_on_ml_has_its_own_daily_cap(fake, ai):
    run = _run_ml_ok()
    for pid in ("cg46", "cg47"):
        _missing(run, pid)
        fake.enabled[pid] = False
    w.set_setting("pm_gate_sin_dato", 1)
    w.set_setting("pm_gate_sin_dato_daily_max", 1)
    try:
        counts = await price_gate.process_run(run, now=w.NOW)
    finally:
        w.set_setting("pm_gate_sin_dato", 0)
        w.set_setting("pm_gate_sin_dato_daily_max", 20)
    assert counts == {"catalogo_activado": 1, "catalogo_tope": 1} and fake.writes() == [True]
