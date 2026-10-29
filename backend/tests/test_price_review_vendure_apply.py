"""Escribir `enabled` desde la bandeja (PR3): el protocolo 3.7 (V-*). El cliente (`set_enabled`, `get_review_guard`) se
prueba en test_vendure_set_enabled.py.

Vendure es el doble HTTP de `review_fixtures`: el cliente real arma sus requests y los reintentos son de verdad (sin
esperas). Para simular que el proceso muere a mitad se levanta `asyncio.CancelledError`, que no es una `Exception`: nada
del código la atrapa, como pasaría con un SIGKILL.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.clock import utcnow  # noqa: E402
from app.db.models import AuditLog  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import daily_budget  # noqa: E402
from app.pricing import price_review as pr  # noqa: E402
from app.pricing import price_review_actions as actions  # noqa: E402
from app.pricing.diagnosis_source import RequestBudget  # noqa: E402
from app.vendure import catalog as vendure_catalog  # noqa: E402
from app.vendure.client import VendureClient  # noqa: E402
from tests import review_decide as k  # noqa: E402
from tests import review_world as w  # noqa: E402

KEEP = dict(code="nicho", text="Se vende poco pero nos interesa tenerlo")
FORBIDDEN = {"errors": [{"message": "You are not currently authorized", "extensions": {"code": "FORBIDDEN"}}]}
BOOM = {"errors": [{"message": "Internal error"}]}


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    f = k.setup(monkeypatch)
    yield f
    k.clean_audit()


@pytest.fixture
def c():
    return k.client()


@pytest.fixture
def relogins(monkeypatch):
    calls: list[int] = []

    async def login(self) -> bool:
        calls.append(1)
        return True

    monkeypatch.setattr(VendureClient, "_login", login)
    return calls


def later(minutes: float = 3):
    return utcnow() + timedelta(minutes=minutes)


# ─── V-1: el orden ───────────────────────────────────────────────


async def test_v1_turning_off_follows_the_protocol_in_order(c, fake, monkeypatch):
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    seen: list[tuple[str, list[str], bool | None]] = []
    handler = fake.handler

    def spy(request):
        op = json.loads(request.content).get("query", "").split("(")[0].split()[-1]
        seen.append((op, k.kinds(item_id), w.item(item_id).hugo_disabled))
        return handler(request)

    fake.handler = spy
    invalidated: list[int] = []
    monkeypatch.setattr(vendure_catalog, "invalidate", lambda: invalidated.append(len(k.kinds(item_id))))
    r = k.post(c, item_id, "approve", expected_state="propuesto")
    assert r.status_code == 200
    # 1) estado + intent ANTES de leer; 2) leer; 3) escribir; 5) confirmar después; 6) invalidar al final
    assert [s[0] for s in seen] == ["GetEnabled", "SetEnabled"]
    for _op, evs, hugo in seen:
        assert evs == ["propuesto", "aprobado", "intent"] and hugo is None
    intent = w.events("intent")[0]
    assert (intent.enabled_before, intent.enabled_after, intent.actor) == (True, False, k.PAO)
    item = w.item(item_id)
    assert item.state == "en_revision" and item.hugo_disabled is True and item.enabled_before is True
    assert item.disabled_at is not None
    ev = w.events("inactivado")[0]
    assert (ev.actor, ev.enabled_before, ev.enabled_after) == (k.PAO, True, False)
    [row] = k.audit("rv1")
    assert (row.action, row.source, row.product_id) == ("price_review_disabled", "price_review", "rv1")
    assert json.loads(row.before) == {"enabled": True} and json.loads(row.after) == {"enabled": False}
    assert f"ficha #{item_id}" in row.detail and "Inactivar y revisar" in row.detail and k.PAO in row.detail
    assert invalidated == [len(k.kinds(item_id))]


# ─── V-2 y V-3: lo que devuelve Vendure ──────────────────────────


@pytest.mark.parametrize("reply", [{"id": "rv1", "enabled": True}, {"id": "rv1"}])
async def test_v2_a_value_that_does_not_match_rolls_back_without_a_second_try(c, fake, reply):
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    fake.write_reply["rv1"] = reply
    v0 = w.item(item_id).version
    r = k.post(c, item_id, "approve", expected_state="propuesto")
    assert r.status_code == 502 and r.json()["detail"]["code"] == "vendure_error"
    item = w.item(item_id)
    assert item.state == "propuesto" and item.vendure_error == "valor_no_coincide" and item.version > v0
    assert len(fake.mutations) == 1 and k.kinds(item_id)[-1] == "vendure_error"
    assert w.events("vendure_error")[0].motive_code == "valor_no_coincide" and k.audit("rv1") == []


async def test_v3_an_error_while_reactivating_is_one_call_502_and_the_state_does_not_change(c, fake):
    item_id = k.disabled_by_hugo("rv1")
    fake.enabled["rv1"] = False
    fake.script["SetEnabled"] = [(500, BOOM)] * 3
    r = k.decide(c, item_id, "mantener", "en_revision", **KEEP)
    assert r.status_code == 502
    assert len(fake.mutations) == 1
    item = w.item(item_id)
    assert item.state == "en_revision" and item.hugo_disabled is True and item.decision is None
    ev = w.events("vendure_error")[0]
    assert ev.motive_code == "escritura" and json.loads(ev.payload)["error"] == "TransportQueryError"


async def test_v3_reactivating_with_an_expired_bearer_is_one_call_plus_the_relogin(c, fake, relogins):
    item_id = k.disabled_by_hugo("rv1")
    fake.enabled["rv1"] = False
    fake.script["SetEnabled"] = [(200, FORBIDDEN)]
    r = k.decide(c, item_id, "mantener", "en_revision", **KEEP)
    assert r.status_code == 200 and len(fake.mutations) == 2 and relogins == [1]


async def test_v3_turning_off_keeps_the_retries(c, fake):
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    fake.script["SetEnabled"] = [(500, BOOM), (500, BOOM)]
    r = k.post(c, item_id, "approve", expected_state="propuesto")
    assert r.status_code == 200 and len(fake.mutations) == 3 and w.item(item_id).state == "en_revision"


async def test_v3_a_read_error_rolls_back_too(c, fake):
    item_id = w.make_item("rv1")
    fake.http_fail.add("GetEnabled")
    r = k.post(c, item_id, "approve", expected_state="propuesto")
    assert r.status_code == 502 and fake.mutations == [] and w.item(item_id).state == "propuesto"
    assert w.events("vendure_error")[0].motive_code == "lectura"


# ─── V-4 y V-5: cuándo se reactiva ───────────────────────────────


def _later_audit(action: str, pid: str = "rv1", delta: timedelta = timedelta(hours=1)) -> None:
    with Session(engine) as s:
        s.add(AuditLog(action=action, source="manual", product_id=pid, created_at=w.NOW + delta))
        s.commit()


@pytest.mark.parametrize("case,code", [
    ("not_hugo", "no_lo_apago_hugo"), ("pending", "aprobacion_pending"), ("rejected", "aprobacion_rejected"),
    ("duplicate", "duplicado"), ("bx", "bx_sin_imagen"), ("unreadable", "aprobacion_ilegible"),
])
async def test_v4_reactivation_needs_every_guard_or_it_is_409_reactivate_by_hand(c, fake, case, code):
    item_id = (w.make_item("rv1", "en_revision", hugo_disabled=False) if case == "not_hugo"
               else k.disabled_by_hugo("rv1"))
    fake.enabled["rv1"] = False
    if case in ("pending", "rejected"):
        fake.approval["rv1"] = case
    if case == "duplicate":
        _later_audit("duplicate_disabled")
    if case == "bx":
        _later_audit("bx_no_image_disabled")
    if case == "unreadable":
        fake.unknown_fields.add("approvalStatus")
    before = w.item(item_id)
    r = k.decide(c, item_id, "mantener", "en_revision", **KEEP)
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "reactivar_a_mano" and d["reason"] == code and "a mano" in d["message"]
    after = w.item(item_id)
    assert fake.mutations == [] and after.state == "en_revision" and after.hugo_disabled == before.hugo_disabled
    ev = w.events("reactivar_a_mano")[0]
    assert ev.motive_code == code and ev.actor == k.PAO


async def test_v4_a_duplicate_disabled_from_before_hugo_turned_it_off_does_not_block(c, fake):
    item_id = k.disabled_by_hugo("rv1")
    fake.enabled["rv1"] = False
    fake.approval["rv1"] = "approved"
    _later_audit("duplicate_disabled", delta=-timedelta(days=3))
    assert k.decide(c, item_id, "mantener", "en_revision", **KEEP).status_code == 200
    assert fake.writes() == [True]


async def test_v5_someone_already_turned_it_on_so_keep_writes_nothing_and_does_not_fail(c, fake):
    item_id = k.disabled_by_hugo("rv1")
    fake.enabled["rv1"] = True
    r = k.decide(c, item_id, "mantener", "en_revision", **KEEP)
    assert r.status_code == 200 and r.json()["state"] == "mantenido" and r.json()["note"] == "ya_activo"
    assert fake.mutations == [] and w.item(item_id).hugo_disabled is False
    assert "ya estaba activo" in w.events("ya_activo")[0].motive_text.lower()


# ─── V-6: el proceso muere a mitad ───────────────────────────────


def _die(monkeypatch, name: str, *, applied: bool = False):
    """El proceso muere al llegar a `VendureClient.<name>` (con `applied`, después de que Vendure aplicó la escritura).
    Devuelve cómo «reiniciar»: vuelve el método real."""
    real = getattr(VendureClient, name)

    async def dying(self, product_id, *a, **kw):
        if applied:
            await real(self, product_id, *a, **kw)
        raise asyncio.CancelledError

    monkeypatch.setattr(VendureClient, name, dying)
    return lambda: monkeypatch.setattr(VendureClient, name, real)


async def _crash(coro) -> None:
    with pytest.raises(asyncio.CancelledError):
        await coro


@pytest.mark.parametrize("scenario,final,hugo,kind", [
    ("a_antes_de_escribir", "propuesto", None, "intent_descartado"),
    ("b_escribio_y_no_confirmo", "en_revision", True, "inactivado"),
    ("c_vendure_la_rechazo", "propuesto", None, "intent_descartado"),
])
async def test_v6_a_half_done_write_is_reconciled_against_vendure(fake, monkeypatch, scenario, final, hugo, kind):
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    restart = (_die(monkeypatch, "get_enabled_status") if scenario.startswith("a")
               else _die(monkeypatch, "set_enabled", applied=scenario.startswith("b")))
    await _crash(actions.act(item_id, "inactivar", k.PAO, expected_state="propuesto"))
    restart()
    assert w.item(item_id).state == "en_revision" and k.kinds(item_id)[-1] == "intent"
    assert await actions.reconcile() == 0                          # todavía no pasaron 2 minutos
    assert await actions.reconcile(now=later()) == 1
    item = w.item(item_id)
    assert (item.state, item.hugo_disabled) == (final, hugo) and k.kinds(item_id)[-1] == kind
    assert item.state == "en_revision" or item.open_key == "rv1"
    assert fake.enabled["rv1"] is (final != "en_revision")
    assert fake.writes() == ([False] if scenario.startswith("b") else [])     # la conciliación nunca escribe
    events = k.kinds(item_id)
    assert await actions.reconcile(now=later(10)) == 0 and k.kinds(item_id) == events      # sin duplicados
    assert len(k.audit("rv1")) == (1 if kind == "inactivado" else 0)
    if kind == "inactivado":
        assert json.loads(w.events("inactivado")[0].payload)["conciliado"] is True


@pytest.mark.parametrize("applied,final,hugo", [(True, "propuesto", False), (False, "en_revision", True)])
async def test_v6_d_a_half_done_undo_is_reconciled_too(c, fake, monkeypatch, applied, final, hugo):
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    assert k.post(c, item_id, "approve", expected_state="propuesto").status_code == 200
    restart = _die(monkeypatch, "set_enabled", applied=applied)
    await _crash(actions.act(item_id, "deshacer", k.PAO, expected_state="en_revision"))
    restart()
    assert await actions.reconcile(now=later()) == 1
    item = w.item(item_id)
    assert (item.state, item.hugo_disabled) == (final, hugo) and fake.enabled["rv1"] is applied
    assert await actions.reconcile(now=later(10)) == 0


async def test_v6_the_nightly_pass_reconciles_in_any_mode_and_survives_a_failure(fake, monkeypatch):
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    restart = _die(monkeypatch, "set_enabled", applied=True)
    await _crash(actions.act(item_id, "inactivar", k.PAO, expected_state="propuesto"))
    restart()
    w.set_setting("pm_mode", 0)
    monkeypatch.setattr(actions, "utcnow", later)
    summary = await pr.process_run(w.make_run(), now=w.NOW)
    assert summary is not None and "errores" not in summary
    assert w.item(item_id).state == "en_revision" and w.item(item_id).hugo_disabled is True

    async def boom(**_k):
        raise RuntimeError("se cayó la conciliación")

    monkeypatch.setattr(actions, "reconcile", boom)
    summary = await pr.process_run(w.make_run(started=w.NOW + timedelta(days=1)), now=w.NOW + timedelta(days=1))
    assert summary["errores"] == ["conciliación: RuntimeError"]


async def test_v6_acting_on_a_ficha_with_a_write_in_flight(c, fake, monkeypatch):
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    restart = _die(monkeypatch, "get_enabled_status")
    await _crash(actions.act(item_id, "inactivar", k.PAO, expected_state="propuesto"))
    restart()
    r = k.decide(c, item_id, "corregir", "en_revision", "caja")        # recién empezó: puede estar en curso
    assert r.status_code == 409 and r.json()["detail"]["code"] == "en_curso"
    monkeypatch.setattr(actions, "utcnow", later)                     # quedó a medias: se concilia y sigue
    r = k.decide(c, item_id, "despublicar", "propuesto", "calidad")
    assert r.status_code == 200 and r.json()["state"] == "despublicado"
    assert k.kinds(item_id)[-4:] == ["intent_descartado", "decision_despublicar", "intent", "inactivado"]


# ─── V-7 y V-8 ───────────────────────────────────────────────────


async def test_v7_writes_neither_spend_nor_are_stopped_by_the_nightly_read_cap(c, fake):
    from app.db.models import Setting

    day = daily_budget.today_art()
    with Session(engine) as s:
        s.merge(Setting(key=RequestBudget.KEY, value=f"{day}:150"))
        s.commit()
    assert RequestBudget(150, day).used() == 150                     # la noche ya gastó todo
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    assert k.post(c, item_id, "approve", expected_state="propuesto").status_code == 200
    assert fake.writes() == [False] and RequestBudget(150, day).used() == 150


async def test_v8_shadow_mode_stops_decisions_but_not_undo(c, fake):
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    assert k.decide(c, item_id, "mantener", "propuesto", **KEEP).status_code == 200
    other = w.make_item("rv2")
    w.set_setting("pm_mode", 0)
    for r in (k.post(c, other, "approve", expected_state="propuesto"),
              k.decide(c, other, "despublicar", "propuesto", "calidad")):
        assert r.status_code == 409 and "modo sombra" in r.json()["detail"]["message"]
    assert fake.requests == []
    r = k.post(c, item_id, "undo", expected_state="mantenido")
    assert r.status_code == 200 and r.json()["state"] == "propuesto"


def test_v6_at_startup_a_one_shot_job_reconciles_three_minutes_later(monkeypatch):
    """«Cuando arranca de nuevo»: un job de una sola vez, a los 3 minutos (las intenciones del proceso anterior ya tienen
    más de 2) y solo en el líder (lo registra `register_jobs`)."""
    from datetime import datetime, timezone

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.date import DateTrigger

    from app.scheduler import jobs

    monkeypatch.setattr(jobs, "scheduler", AsyncIOScheduler())
    t0 = datetime.now(timezone.utc)
    jobs.register_jobs()
    job = jobs.scheduler.get_job("price_review_reconcile")
    assert job is not None and isinstance(job.trigger, DateTrigger)
    assert timedelta(minutes=2) < job.trigger.run_date - t0 < timedelta(minutes=4)


async def test_v6_the_startup_job_never_raises(monkeypatch):
    from app.scheduler import jobs

    async def boom(**_k):
        raise RuntimeError("Vendure caído")

    monkeypatch.setattr(actions, "reconcile", boom)
    await jobs.price_review_reconcile()                      # se loguea; no tira
