"""Decisiones de Pao sobre las fichas (PR3): permisos y saneo (S-*), decisiones (D-*), concurrencia (C-*) y Deshacer (U-*).

Vendure es el doble HTTP de `review_fixtures` con `writable = True`: `SetEnabled` cambia `enabled` y queda anotado en
`mutations`. La base es la SQLite de la suite. Los criterios son los de `bandeja-pao-pr3-pr5-criterios.md`, sección 4.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import timedelta
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.clock import utcnow  # noqa: E402
from app.db.models import AuditLog, PriceReviewItem  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import price_review as pr  # noqa: E402
from app.pricing import price_review_actions as actions  # noqa: E402
from tests import review_decide as k  # noqa: E402
from tests import review_world as w  # noqa: E402

KEEP = dict(code="nicho", text="Se vende poco pero nos interesa tenerlo")


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    f = k.setup(monkeypatch)
    yield f
    k.clean_audit()


@pytest.fixture
def c():
    return k.client()


def snapshot(item_id: int) -> dict:
    it = w.item(item_id)
    return {a: getattr(it, a) for a in ("state", "version", "hugo_disabled", "decision", "open_key", "kept_ratio")}


# ─── S: permisos, validación y saneo ──────────────────────────────


async def test_s1_someone_who_is_not_a_decider_gets_403_and_nothing_changes(fake):
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    before, events = snapshot(item_id), k.kinds(item_id)
    other = k.client("vendedor@b2box.pro")
    for r in (k.post(other, item_id, "approve", expected_state="propuesto"),
              k.decide(other, item_id, "mantener", "propuesto", **KEEP),
              k.post(other, item_id, "undo", expected_state="propuesto")):
        assert r.status_code == 403 and "No estás en la lista de quienes deciden" in r.json()["detail"]
    assert fake.mutations == [] and snapshot(item_id) == before and k.kinds(item_id) == events


@pytest.mark.parametrize("env", ["development", "production"])
async def test_s2_with_an_empty_list_nobody_decides_and_it_is_logged_as_fail_closed(monkeypatch, fake, caplog, env):
    k.setup(monkeypatch, price_review_deciders="", hugo_env=env)
    item_id = w.make_item("rv1")
    with caplog.at_level(logging.WARNING, logger="app.auth"):
        r = k.post(k.client(), item_id, "approve", expected_state="propuesto")
    assert r.status_code == 403 and "PRICE_REVIEW_DECIDERS" in r.json()["detail"]
    assert "fail-closed" in caplog.text and fake.mutations == []


async def test_s3_without_dashboard_secret_or_without_a_user_in_the_session_it_is_403(monkeypatch, fake):
    k.setup(monkeypatch, dashboard_secret="")
    item_id = w.make_item("rv1")
    assert k.post(k.client(), item_id, "approve", expected_state="propuesto").status_code == 403
    k.setup(monkeypatch)
    item_id = w.make_item("rv2")
    anonymous = k.client("")                                    # cookie válida, pero sin `u`
    assert k.post(anonymous, item_id, "approve", expected_state="propuesto").status_code == 403
    assert fake.mutations == []


@pytest.mark.parametrize("listed,session", [(" Pao@B2box.pro ", "pao@b2box.pro"), ("pao@b2box.pro", " Pao@B2box.pro ")])
async def test_s4_case_and_spaces_do_not_matter(monkeypatch, fake, listed, session):
    k.setup(monkeypatch, price_review_deciders=f"{listed}, gabriel@b2box.pro")
    item_id = w.make_item("rv1")
    r = k.decide(k.client(session), item_id, "mantener", "propuesto", **KEEP)
    assert r.status_code == 200 and r.json()["state"] == "mantenido"


@pytest.mark.parametrize("code,text", [
    (None, "Se vende poco pero nos interesa"),            # falta el motivo
    ("caja", "Se vende poco pero nos interesa"),          # no es un motivo de Mantener
    ("nicho", None),                                      # el texto es obligatorio
    ("nicho", "corto"),
    ("nicho", " \x00 a \n\t b \x7f  c  "),                # menos de 10 después de limpiar
    ("nicho", "x" * 501),
])
async def test_s5_keep_needs_a_listed_motive_and_10_to_500_characters(c, fake, code, text):
    item_id = w.make_item("rv1")
    before = snapshot(item_id)
    r = k.decide(c, item_id, "mantener", "propuesto", code, text)
    assert r.status_code == 400, r.text
    assert snapshot(item_id) == before and k.kinds(item_id) == ["propuesto"] and fake.requests == []


@pytest.mark.parametrize("decision,code,text,ok", [
    ("corregir", "caja", None, True),
    ("corregir", "tramos", "Los tramos están al revés", True),
    ("corregir", "precio", "Algo que no es de la lista", False),
    ("corregir", "otro", "corto", False),
    ("corregir", "otro", "La foto principal es de otro producto", True),
    ("despublicar", "sin_margen", None, True),
    ("despublicar", "sin_proveedor", None, True),
    ("despublicar", "calidad", None, True),
    ("despublicar", "duplicado", None, True),
    ("despublicar", "caro", None, False),
    ("despublicar", "otro", "nah", False),
    ("despublicar", "otro", "El proveedor dejó de fabricarlo", True),
])
async def test_s6_fix_and_unpublish_take_their_own_motives_and_otro_needs_text(c, fake, decision, code, text, ok):
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    r = k.decide(c, item_id, decision, "propuesto", code, text)
    assert (r.status_code == 200) is ok, r.text
    if not ok:
        assert r.status_code == 400 and fake.mutations == [] and w.item(item_id).state == "propuesto"


async def test_s7_control_characters_are_cleaned_html_is_kept_as_text_and_10000_characters_are_rejected(c, fake):
    item_id = w.make_item("rv1")
    r = k.decide(c, item_id, "mantener", "propuesto", "otro", "x" * 10_000)
    assert r.status_code == 400 and w.item(item_id).state == "propuesto"
    evil = "<script>alert(1)</script>\x00\r\nlínea 2  y\tmás"
    r = k.decide(c, item_id, "mantener", "propuesto", "otro", evil)
    assert r.status_code == 200
    saved = w.item(item_id).decision_motive_text
    assert saved == "<script>alert(1)</script> línea 2 y más" and len(saved) <= 500
    assert not any(ord(ch) < 32 or ord(ch) == 127 for ch in saved)
    ev = w.events("decision_mantener")[0]
    assert ev.motive_text == saved
    view = (Path(__file__).resolve().parents[2] / "frontend/src/components/BandejaView.tsx").read_text()
    assert "dangerouslySetInnerHTML" not in view and "innerHTML" not in view


async def test_s8_vendure_errors_are_stored_and_answered_only_as_a_code(c, fake):
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    secret = "costo de fábrica 12,34 USD proveedor@1688.com"
    fake.script["SetEnabled"] = [(500, {"errors": [{"message": secret}]})] * 3
    r = k.post(c, item_id, "approve", expected_state="propuesto")
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["code"] == "vendure_error" and detail["error"] == "TransportQueryError"
    item = w.item(item_id)
    assert item.state == "propuesto" and item.vendure_error == "TransportQueryError"
    ev = w.events("vendure_error")[0]
    assert json.loads(ev.payload) == {"accion": "inactivar", "error": "TransportQueryError"}
    everything = r.text + json.dumps([e.model_dump(mode="json") for e in w.events()]) + str(item.model_dump())
    assert "fábrica" not in everything and "1688" not in everything and "USD" not in everything


async def test_s12_only_json_with_a_session(fake):
    item_id = w.make_item("rv1")
    c = k.client()
    form = c.post(f"{k.BASE}/items/{item_id}/approve", data={"expected_state": "propuesto"})
    assert form.status_code == 422
    extra = k.post(c, item_id, "approve", expected_state="propuesto", sorpresa=1)
    assert extra.status_code == 422
    assert k.post(k.client(None), item_id, "approve", expected_state="propuesto").status_code == 401
    assert fake.mutations == [] and w.item(item_id).state == "propuesto"


# ─── D: decisiones ───────────────────────────────────────────────


async def test_d1_inactivate_and_review_writes_false_once_and_leaves_two_events_with_the_actor(c, fake):
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    t0 = utcnow()
    r = k.post(c, item_id, "approve", expected_state="propuesto")
    assert r.status_code == 200 and r.json()["state"] == "en_revision" and r.json()["wrote"] is True
    assert fake.writes() == [False] and fake.enabled["rv1"] is False
    evs = [e for e in w.events() if e.item_id == item_id and e.kind in ("aprobado", "inactivado")]
    assert [e.kind for e in evs] == ["aprobado", "inactivado"]
    assert all(e.actor == k.PAO and e.at.tzinfo is None and abs((e.at - t0).total_seconds()) < 60 for e in evs)


async def test_d2_fix_from_proposed_turns_it_off_and_from_in_review_writes_nothing(c, fake):
    a = w.make_item("rv1")
    fake.enabled["rv1"] = True
    r = k.decide(c, a, "corregir", "propuesto", "caja", "La caja está cargada el doble")
    assert r.status_code == 200 and r.json()["state"] == "corrigiendo" and fake.writes() == [False]
    b = k.disabled_by_hugo("rv2")
    fake.enabled["rv2"] = False
    r = k.decide(c, b, "corregir", "en_revision", "caja", None)
    assert r.status_code == 200 and r.json()["state"] == "corrigiendo" and r.json()["wrote"] is False
    assert fake.writes() == [False] and fake.count("GetEnabled") == 1           # la segunda no lee ni escribe


async def test_d3_keep_from_proposed_writes_nothing_and_keeps_the_baseline(c, fake):
    item_id = w.make_item("rv1", reff_ratio=0.958, ml_median_cents=2_400_000, our_price_cents=2_000_000)
    r = k.decide(c, item_id, "mantener", "propuesto", **KEEP)
    assert r.status_code == 200 and r.json()["state"] == "mantenido" and fake.requests == []
    item = w.item(item_id)
    assert (item.kept_ratio, item.kept_median_cents, item.kept_our_price_cents) == (0.958, 2_400_000, 2_000_000)
    assert item.open_key == "rv1"                                     # sigue viva: la vigila la regla del 10 %
    assert "sin_linea_base" not in json.loads(w.events("decision_mantener")[0].payload)


async def test_d3_keep_without_a_baseline_says_it_will_not_reopen_by_itself(c, fake):
    item_id = w.make_item("rv1", reff_ratio=None)
    assert k.decide(c, item_id, "mantener", "propuesto", **KEEP).status_code == 200
    assert w.item(item_id).kept_ratio is None
    assert json.loads(w.events("decision_mantener")[0].payload)["sin_linea_base"] == "sin línea base: no se reabre sola"


async def test_d4_keep_from_in_review_reactivates_through_the_guards(c, fake):
    item_id = k.disabled_by_hugo("rv1")
    fake.enabled["rv1"] = False
    r = k.decide(c, item_id, "mantener", "en_revision", **KEEP)
    assert r.status_code == 200 and r.json()["state"] == "mantenido" and fake.writes() == [True]
    item = w.item(item_id)
    assert item.reactivated_at is not None and item.hugo_disabled is False
    assert "reactivado" in k.kinds(item_id) and fake.count("ReviewGuard") == 1
    assert [a.action for a in k.audit("rv1")] == ["price_review_enabled"]


async def test_d5_unpublish_from_proposed_turns_it_off_and_closes_the_ficha(c, fake):
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    r = k.decide(c, item_id, "despublicar", "propuesto", "sin_margen")
    assert r.status_code == 200 and r.json()["state"] == "despublicado" and fake.writes() == [False]
    item = w.item(item_id)
    assert item.open_key is None and item.closed_at is not None and item.hugo_disabled is True


@pytest.mark.parametrize("state,path,body", [
    ("propuesto", "decision", dict(decision="reactivar")),
    ("en_revision", "approve", {}),
    ("corrigiendo", "decision", dict(decision="corregir", motive_code="caja")),
    ("buscando_prov", "decision", dict(decision="reactivar")),
    ("mantenido", "decision", dict(decision="despublicar", motive_code="calidad")),
    ("despublicado", "decision", dict(decision="mantener", motive_code="nicho", motive_text="x" * 12)),
    ("corregido", "approve", {}),
    ("cerrado_auto", "decision", dict(decision="corregir", motive_code="caja")),
])
async def test_d6_an_action_that_the_state_does_not_allow_is_409_with_the_allowed_ones(c, fake, state, path, body):
    item_id = w.make_item("rv1", state)
    r = k.post(c, item_id, path, expected_state=state, **body)
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "no_permitida" and d["state"] == state
    assert d["allowed"] == [a for a in ("inactivar", "corregir", "mantener", "despublicar", "reactivar")
                            if (state, a) in actions.TRANSITIONS]
    assert fake.requests == [] and k.kinds(item_id) == ["propuesto"]


@pytest.mark.parametrize("path,body", [("approve", {}), ("decision", dict(decision="corregir", motive_code="caja")),
                                       ("decision", dict(decision="despublicar", motive_code="calidad"))])
async def test_d7_turning_off_something_already_off_writes_nothing_and_closes_the_ficha(c, fake, path, body):
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = False                                       # lo apagó otra persona u otro proceso
    r = k.post(c, item_id, path, expected_state="propuesto", **body)
    assert r.status_code == 200 and r.json()["state"] == "cerrado_auto" and r.json()["note"] == "ya_inactivo"
    item = w.item(item_id)
    assert fake.mutations == [] and item.hugo_disabled is False and item.open_key is None
    ev = w.events("cerrado_auto")[-1]
    assert "ya estaba inactivo por otra razón" in ev.motive_text and ev.actor == k.PAO


@pytest.mark.parametrize("decision,state,code,write", [
    ("corregir", "propuesto", "caja", True), ("mantener", "propuesto", "nicho", None),
    ("despublicar", "propuesto", "duplicado", True), ("reactivar", "corrigiendo", None, True),
])
async def test_d8_every_decision_keeps_who_when_and_why_and_one_event(c, fake, decision, state, code, write):
    item_id = k.disabled_by_hugo("rv1", state) if state == "corrigiendo" else w.make_item("rv1")
    fake.enabled["rv1"] = state != "corrigiendo"
    text = "Lo revisé con el proveedor hoy"
    r = k.decide(c, item_id, decision, state, code, text)
    assert r.status_code == 200, r.text
    item = w.item(item_id)
    assert (item.decision, item.decision_by, item.decision_motive_code, item.decision_motive_text) == (decision, k.PAO, code, text)
    assert item.decision_at is not None
    evs = [e for e in w.events() if e.item_id == item_id and e.kind.startswith("decision_")]
    assert [e.kind for e in evs] == [f"decision_{decision}"] and len(evs[0].kind) <= 24


async def test_d9_reactivate_from_fixing_ends_fixed_and_a_red_two_nights_later_opens_a_new_ficha(c, fake):
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    fake.enabled["rv1"] = False
    r = k.decide(c, item_id, "reactivar", "corrigiendo", text="Cargué la caja bien")
    assert r.status_code == 200 and r.json()["state"] == "corregido" and fake.writes() == [True]
    item = w.item(item_id)
    assert item.open_key is None and item.closed_at is not None and item.hugo_disabled is False
    r1, r2 = w.make_run(started=w.NOW + timedelta(days=1)), w.make_run(started=w.NOW + timedelta(days=2))
    w.add_snap(r1, "rv1", data_at=w.NOW + timedelta(days=1), captured_at=w.NOW + timedelta(days=1))
    await pr.process_run(r1, now=w.NOW + timedelta(days=1))
    w.add_snap(r2, "rv1", data_at=w.NOW + timedelta(days=2), captured_at=w.NOW + timedelta(days=2))
    await pr.process_run(r2, now=w.NOW + timedelta(days=2))
    states = [(i.state, i.open_key) for i in w.items()]
    assert states == [("corregido", None), ("propuesto", "rv1")]


# ─── C: concurrencia ─────────────────────────────────────────────


async def test_c1_two_clicks_at_once_make_one_write_and_one_event(fake):
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    results = await asyncio.gather(
        actions.act(item_id, "inactivar", k.PAO, expected_state="propuesto"),
        actions.act(item_id, "despublicar", "gabriel@b2box.pro", expected_state="propuesto", motive_code="calidad"),
        return_exceptions=True)
    ok = [r for r in results if isinstance(r, dict)]
    errors = [r for r in results if isinstance(r, actions.ActionError)]
    assert len(ok) == 1 and len(errors) == 1 and errors[0].status == 409
    assert errors[0].extra["state"] == ok[0]["state"] and errors[0].extra["version"] == w.item(item_id).version
    assert fake.writes() == [False]
    assert len([kd for kd in k.kinds(item_id) if kd == "aprobado" or kd.startswith("decision_")]) == 1


async def test_c1_a_stale_read_loses_the_atomic_update_and_gets_the_current_state(c, fake, monkeypatch):
    """Las dos leen la ficha ANTES de que la otra la mueva: el UPDATE … WHERE state AND version deja pasar a una sola."""
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    stale, _ = actions._load(item_id)                       # la segunda pestaña la leyó antes del primer clic
    assert k.post(c, item_id, "approve", expected_state="propuesto").status_code == 200
    real, first = actions._load, [True]

    def load(i: int):
        if first.pop() if first else False:
            return stale, None
        return real(i)

    monkeypatch.setattr(actions, "_load", load)
    r = k.decide(c, item_id, "despublicar", "propuesto", "calidad")
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "estado_cambio" and d["state"] == "en_revision" and d["version"] == w.item(item_id).version
    assert "Otra persona ya decidió" in d["message"]
    assert fake.writes() == [False] and "decision_despublicar" not in k.kinds(item_id)


async def test_c2_an_expected_state_that_does_not_match_is_409_without_writes(c, fake):
    item_id = k.disabled_by_hugo("rv1")
    r = k.decide(c, item_id, "despublicar", "propuesto", "calidad")
    assert r.status_code == 409 and r.json()["detail"]["state"] == "en_revision" and fake.requests == []


# ─── U: Deshacer ─────────────────────────────────────────────────


async def test_u1_undo_inactivate_goes_back_to_proposed_and_turns_it_on_through_the_guards(c, fake):
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    assert k.post(c, item_id, "approve", expected_state="propuesto").status_code == 200
    r = k.post(c, item_id, "undo", expected_state="en_revision")
    assert r.status_code == 200 and r.json()["state"] == "propuesto"
    assert fake.writes() == [False, True] and fake.count("ReviewGuard") == 1 and fake.enabled["rv1"] is True
    item = w.item(item_id)
    assert item.hugo_disabled is False and item.open_key == "rv1"
    assert item.state_since > w.NOW                     # la hora del Deshacer: la pasada sabe qué dato es anterior


async def test_u1_undo_fix_from_in_review_goes_back_without_writing(c, fake):
    item_id = k.disabled_by_hugo("rv1")
    fake.enabled["rv1"] = False
    assert k.decide(c, item_id, "corregir", "en_revision", "caja").status_code == 200
    r = k.post(c, item_id, "undo", expected_state="corrigiendo")
    assert r.status_code == 200 and r.json()["state"] == "en_revision" and fake.mutations == []
    item = w.item(item_id)
    assert item.decision is None and item.hugo_disabled is True


async def test_u1_undo_keep_that_reactivated_goes_back_to_in_review_and_turns_it_off(c, fake):
    item_id = k.disabled_by_hugo("rv1")
    fake.enabled["rv1"] = False
    assert k.decide(c, item_id, "mantener", "en_revision", **KEEP).status_code == 200
    r = k.post(c, item_id, "undo", expected_state="mantenido")
    assert r.status_code == 200 and r.json()["state"] == "en_revision"
    assert fake.writes() == [True, False] and w.item(item_id).hugo_disabled is True
    assert w.item(item_id).kept_ratio is None


async def test_u1_undo_keep_from_proposed_goes_back_without_writing(c, fake):
    item_id = w.make_item("rv1")
    assert k.decide(c, item_id, "mantener", "propuesto", **KEEP).status_code == 200
    r = k.post(c, item_id, "undo", expected_state="mantenido")
    assert r.status_code == 200 and r.json()["state"] == "propuesto" and fake.requests == []
    item = w.item(item_id)
    assert item.decision is None and item.kept_ratio is None and item.open_key == "rv1"


async def test_u1_undo_unpublish_reopens_the_same_ficha(c, fake):
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    assert k.decide(c, item_id, "despublicar", "propuesto", "calidad").status_code == 200
    r = k.post(c, item_id, "undo", expected_state="despublicado")
    assert r.status_code == 200 and r.json()["state"] == "propuesto"
    item = w.item(item_id)
    assert item.open_key == "rv1" and item.closed_at is None and fake.writes() == [False, True]


@pytest.mark.parametrize("age,ok", [(timedelta(days=30), True), (timedelta(days=30, minutes=1), False)])
async def test_u2_undo_works_for_30_days(c, fake, monkeypatch, age, ok):
    item_id = w.make_item("rv1")
    assert k.decide(c, item_id, "mantener", "propuesto", **KEEP).status_code == 200
    decided = w.events("decision_mantener")[0].at
    monkeypatch.setattr(actions, "utcnow", lambda: decided + age)
    r = k.post(c, item_id, "undo", expected_state="mantenido", motive_text="Me equivoqué de ficha")
    if ok:
        assert r.status_code == 200
        ev = w.events("deshacer")[0]
        assert (ev.actor, ev.motive_code, ev.motive_text) == (k.PAO, "revertido", "Me equivoqué de ficha")
    else:
        assert r.status_code == 409 and "venció el plazo para deshacer" in r.json()["detail"]["message"].lower()
        assert w.item(item_id).state == "mantenido"


async def test_u3_undo_is_409_if_the_ficha_changed_after_and_an_undo_cannot_be_undone(c, fake):
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    assert k.post(c, item_id, "approve", expected_state="propuesto").status_code == 200
    assert k.decide(c, item_id, "corregir", "en_revision", "caja").status_code == 200
    assert k.post(c, item_id, "undo", expected_state="corrigiendo").status_code == 200       # deshace Corregir
    r = k.post(c, item_id, "undo", expected_state="en_revision")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "nada_para_deshacer"
    # un cierre solo después de la decisión
    other = w.make_item("rv2")
    assert k.decide(c, other, "mantener", "propuesto", **KEEP).status_code == 200
    with Session(engine) as s:
        it = s.get(PriceReviewItem, other)
        pr.close_item(s, it, note="Dejó de ser rojo.")
        s.commit()
    r = k.post(c, other, "undo", expected_state="cerrado_auto")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "nada_para_deshacer"


async def test_u3_a_reopening_by_the_10_percent_rule_blocks_undo(c, fake):
    item_id = w.make_item("rv1")
    assert k.decide(c, item_id, "mantener", "propuesto", **KEEP).status_code == 200
    with Session(engine) as s:
        it = s.get(PriceReviewItem, item_id)
        pr.move_item(s, it, "propuesto")
        pr.add_event(s, it, "reabierto", from_state="mantenido", to_state="propuesto")
        s.commit()
    r = k.post(c, item_id, "undo", expected_state="propuesto")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "nada_para_deshacer"


async def test_u4_an_undo_that_must_reactivate_and_fails_the_guards_is_409_and_changes_nothing(c, fake):
    item_id = w.make_item("rv1")
    fake.enabled["rv1"] = True
    assert k.post(c, item_id, "approve", expected_state="propuesto").status_code == 200
    with Session(engine) as s:
        s.add(AuditLog(action="duplicate_disabled", source="manual", product_id="rv1", created_at=utcnow() + timedelta(seconds=5)))
        s.commit()
    before = snapshot(item_id)
    r = k.post(c, item_id, "undo", expected_state="en_revision")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "reactivar_a_mano"
    assert fake.writes() == [False] and snapshot(item_id)["state"] == before["state"] == "en_revision"
    assert k.kinds(item_id)[-1] == "reactivar_a_mano"


# ─── UI: lo que el detalle le da a la pantalla ────────────────────


async def test_ui1_the_detail_says_which_buttons_there_are_and_why_they_are_off(fake):
    item_id = w.make_item("rv1")
    d = k.client().get(f"{k.BASE}/items/{item_id}").json()
    assert [a["code"] for a in d["actions"]] == ["inactivar", "corregir", "mantener", "despublicar"]
    assert all(a["enabled"] and a["reason"] is None for a in d["actions"]) and d["decisions_enabled"] is True
    by = {a["code"]: a for a in d["actions"]}
    assert by["inactivar"]["vendure"] == "apagar" and by["mantener"]["vendure"] is None
    assert by["mantener"]["text"] == "siempre" and by["corregir"]["text"] == "otro" and by["inactivar"]["text"] == "opcional"
    assert [m["code"] for m in by["despublicar"]["motives"]] == ["sin_margen", "sin_proveedor", "calidad", "duplicado", "otro"]
    assert by["inactivar"]["to_state"] == "en_revision" and by["inactivar"]["to_state_label"] == "En revisión"
    assert k.client().get(f"{k.BASE}/summary").json()["read_only"] is False

    other = k.client("vendedor@b2box.pro")
    d = other.get(f"{k.BASE}/items/{item_id}").json()
    assert all(not a["enabled"] and a["reason"] == "Solo deciden: ver PRICE_REVIEW_DECIDERS" for a in d["actions"])
    assert d["decisions_enabled"] is False and other.get(f"{k.BASE}/summary").json()["read_only"] is True

    w.set_setting("pm_mode", 0)
    d = k.client().get(f"{k.BASE}/items/{item_id}").json()
    assert all(not a["enabled"] and "modo sombra" in a["reason"] for a in d["actions"]) and d["decisions_enabled"] is False

    corr = k.disabled_by_hugo("rv2", "corrigiendo")
    w.set_setting("pm_mode", 1)
    d = k.client().get(f"{k.BASE}/items/{corr}").json()
    assert [a["code"] for a in d["actions"]] == ["mantener", "despublicar", "reactivar"]
    assert {a["code"]: a["vendure"] for a in d["actions"]} == {"mantener": "reactivar", "despublicar": None, "reactivar": "reactivar"}


async def test_ui4_undo_shows_up_only_for_the_last_human_action_with_its_deadline(c, fake):
    item_id = w.make_item("rv1")
    assert [a["code"] for a in c.get(f"{k.BASE}/items/{item_id}").json()["actions"]] == [
        "inactivar", "corregir", "mantener", "despublicar"]                       # nada para deshacer todavía
    assert k.decide(c, item_id, "mantener", "propuesto", **KEEP).status_code == 200
    d = c.get(f"{k.BASE}/items/{item_id}").json()
    [undo] = d["actions"]
    decided = w.events("decision_mantener")[0].at
    assert undo["code"] == "deshacer" and undo["enabled"] and undo["to_state"] == "propuesto"
    assert undo["deadline"] == (decided + timedelta(days=30)).isoformat() + "Z" and undo["vendure"] is None
    assert d["decision"]["code"] == "mantener" and d["decision"]["by"] == k.PAO
    w.set_setting("pm_mode", 0)                                                   # Deshacer sigue en modo sombra
    assert c.get(f"{k.BASE}/items/{item_id}").json()["actions"][0]["enabled"] is True
    assert k.post(c, item_id, "undo", expected_state="mantenido").status_code == 200
    assert [a["code"] for a in c.get(f"{k.BASE}/items/{item_id}").json()["actions"]] == [
        "inactivar", "corregir", "mantener", "despublicar"]                       # un Deshacer no se deshace


def test_ui6_the_screen_no_longer_says_hugo_only_proposes():
    view = (Path(__file__).resolve().parents[2] / "frontend/src/components/BandejaView.tsx").read_text()
    assert "Por ahora Hugo solo propone" not in view and "Llega en la próxima etapa" not in view
    assert "Solo deciden" not in view                                       # el motivo lo manda el backend
