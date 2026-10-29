"""QA independiente del PR3 de la bandeja: casos que los tests del PR no cubren.

* la tabla 3.1 completa (8 estados x 6 acciones), contra la tabla de los criterios y no contra `TRANSITIONS`;
* `pm_mode` 0, 1 y 2; Deshacer encadenado; la regla del 10 % reabriendo dos veces;
* carreras y fallas ambiguas entre una escritura en curso, la conciliación y la pasada de la noche.

Los `xfail(strict=True)` documentan bugs abiertos: cuando se arreglen, el test pasa y el `strict` obliga a sacar la marca.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.clock import utcnow  # noqa: E402
from app.db.models import REVIEW_STATES  # noqa: E402
from app.pricing import price_review as pr  # noqa: E402
from app.pricing import price_review_actions as actions  # noqa: E402
from app.vendure.client import VendureClient  # noqa: E402
from tests import review_decide as k  # noqa: E402
from tests import review_world as w  # noqa: E402

GABRIEL = "gabriel@b2box.pro"
KEEP = dict(motive_code="nicho", motive_text="Se vende poco pero nos interesa tenerlo")
MOTIVE = {"corregir": dict(motive_code="caja"), "mantener": KEEP, "despublicar": dict(motive_code="calidad"),
          "inactivar": {}, "reactivar": {}, "deshacer": {}}
BOOM = {"errors": [{"message": "Internal error"}]}

# Tabla 3.1 de los criterios (sin «Buscar otro proveedor», que es del PR5): (estado, acción) → (estado nuevo, escrituras).
SPEC_31 = {
    ("propuesto", "inactivar"): ("en_revision", [False]),
    ("propuesto", "corregir"): ("corrigiendo", [False]),
    ("propuesto", "mantener"): ("mantenido", []),
    ("propuesto", "despublicar"): ("despublicado", [False]),
    ("en_revision", "corregir"): ("corrigiendo", []),
    ("en_revision", "mantener"): ("mantenido", [True]),
    ("en_revision", "despublicar"): ("despublicado", []),
    ("corrigiendo", "mantener"): ("mantenido", [True]),
    ("corrigiendo", "despublicar"): ("despublicado", []),
    ("corrigiendo", "reactivar"): ("corregido", [True]),
    ("buscando_prov", "corregir"): ("corrigiendo", []),
    ("buscando_prov", "mantener"): ("mantenido", [True]),
    ("buscando_prov", "despublicar"): ("despublicado", []),
}
ACTIONS = ("inactivar", "corregir", "mantener", "despublicar", "reactivar")


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    f = k.setup(monkeypatch)
    yield f
    k.clean_audit()


@pytest.fixture
def c():
    return k.client()


def later(minutes: float = 3):
    return utcnow() + timedelta(minutes=minutes)


def _ficha(fake, state: str, pid: str = "rv1") -> int:
    """Un `propuesto` con el producto activo, o cualquier otro estado con el producto apagado por Hugo."""
    if state == "propuesto":
        fake.enabled[pid] = True
        return w.make_item(pid)
    fake.enabled[pid] = False
    return k.disabled_by_hugo(pid, state)


# ─── Tabla 3.1 completa ──────────────────────────────────────────


@pytest.mark.parametrize("state", REVIEW_STATES)
@pytest.mark.parametrize("action", ACTIONS)
async def test_qa_every_state_by_every_action_follows_table_3_1(fake, state, action):
    item_id = _ficha(fake, state)
    before = w.item(item_id)
    spec = SPEC_31.get((state, action))
    if spec is None:
        with pytest.raises(actions.ActionError) as exc:
            await actions.act(item_id, action, k.PAO, expected_state=state, **MOTIVE[action])
        e = exc.value
        assert (e.status, e.code, e.extra["state"]) == (409, "no_permitida", state)
        assert e.extra["allowed"] == [a for a in ACTIONS if (state, a) in SPEC_31]
        after = w.item(item_id)
        assert (after.state, after.version) == (before.state, before.version) and fake.requests == []
        assert k.kinds(item_id) == ["propuesto"]
        return
    to_state, writes = spec
    out = await actions.act(item_id, action, k.PAO, expected_state=state, **MOTIVE[action])
    after = w.item(item_id)
    assert out["state"] == after.state == to_state and fake.writes() == writes
    assert (after.open_key is None) is (to_state in pr.CLOSED_STATES)
    assert after.hugo_disabled is {(False,): True, (True,): False, (): before.hugo_disabled}[tuple(writes)]


@pytest.mark.parametrize("state", REVIEW_STATES)
async def test_qa_undo_without_a_human_action_is_409_in_every_state(fake, state):
    item_id = _ficha(fake, state)
    with pytest.raises(actions.ActionError) as exc:
        await actions.act(item_id, "deshacer", k.PAO, expected_state=state)
    assert (exc.value.status, exc.value.code) == (409, "nada_para_deshacer") and fake.requests == []


# ─── pm_mode ─────────────────────────────────────────────────────


@pytest.mark.parametrize("mode,ok", [(0, False), (1, True), (2, True)])
async def test_qa_pm_mode_0_stops_every_decision_and_1_and_2_let_them_through(fake, mode, ok):
    w.set_setting("pm_mode", mode)
    for state, action in SPEC_31:
        pid = f"m{state[:4]}{action[:4]}"
        item_id = _ficha(fake, state, pid)
        if ok:
            assert (await actions.act(item_id, action, k.PAO, expected_state=state, **MOTIVE[action]))["state"] == \
                SPEC_31[(state, action)][0]
            continue
        with pytest.raises(actions.ActionError) as exc:
            await actions.act(item_id, action, k.PAO, expected_state=state, **MOTIVE[action])
        assert (exc.value.status, exc.value.code) == (409, "modo_sombra") and w.item(item_id).state == state
    if not ok:
        assert fake.requests == []


# ─── Deshacer encadenado ─────────────────────────────────────────


async def test_qa_inactivate_undo_inactivate_undo_goes_back_and_forth(c, fake):
    item_id = _ficha(fake, "propuesto")
    for _ in range(2):
        assert k.post(c, item_id, "approve", expected_state="propuesto").json()["state"] == "en_revision"
        assert k.post(c, item_id, "undo", expected_state="en_revision").json()["state"] == "propuesto"
    assert fake.writes() == [False, True, False, True] and fake.enabled["rv1"] is True
    item = w.item(item_id)
    assert item.hugo_disabled is False and item.open_key == "rv1"
    assert [a.action for a in k.audit("rv1")] == ["price_review_disabled", "price_review_enabled"] * 2


async def test_qa_undo_reactivate_is_409_when_the_night_already_opened_a_new_ficha(c, fake):
    item_id = _ficha(fake, "corrigiendo")
    assert k.decide(c, item_id, "reactivar", "corrigiendo").json()["state"] == "corregido"
    w.make_item("rv1")                                             # la noche abrió otra ficha del mismo producto
    r = k.post(c, item_id, "undo", expected_state="corregido")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "otra_ficha_abierta"
    assert fake.writes() == [True] and w.item(item_id).state == "corregido"


@pytest.mark.parametrize("failure", ["vendure_error_en_el_deshacer", "reactivar_a_mano_en_el_deshacer",
                                     "vendure_error_en_otra_decision"])
async def test_qa_a_failed_attempt_does_not_take_undo_away(c, fake, failure):
    """La ficha quedó como estaba: Deshacer tiene que seguir disponible (la respuesta del 502 dice «probá de nuevo»)."""
    item_id = _ficha(fake, "propuesto")
    assert k.post(c, item_id, "approve", expected_state="propuesto").status_code == 200
    if failure == "vendure_error_en_el_deshacer":
        fake.script["SetEnabled"] = [(500, BOOM)]
        assert k.post(c, item_id, "undo", expected_state="en_revision").status_code == 502
    elif failure == "reactivar_a_mano_en_el_deshacer":
        fake.approval["rv1"] = "pending"
        assert k.post(c, item_id, "undo", expected_state="en_revision").status_code == 409
        fake.approval["rv1"] = "approved"                          # el portal ya lo aprobó
    else:
        fake.script["SetEnabled"] = [(500, BOOM)]
        assert k.decide(c, item_id, "mantener", "en_revision", KEEP["motive_code"], KEEP["motive_text"]).status_code == 502
    assert w.item(item_id).state == "en_revision"
    assert "deshacer" in [a["code"] for a in c.get(f"{k.BASE}/items/{item_id}").json()["actions"]]
    r = k.post(c, item_id, "undo", expected_state="en_revision")
    assert r.status_code == 200 and r.json()["state"] == "propuesto" and fake.enabled["rv1"] is True


# ─── Regla del 10 %: reabrir dos veces ───────────────────────────

NET = 783_000          # mediana 900.000 con 13 % de comisión


async def _night(day: int, ratio: float) -> None:
    """Rojo firme con racha cumplida (la noche anterior también fue firme) y `reff_ratio` = `ratio`."""
    prev, today = w.make_run(started=w.NOW + timedelta(days=day - 1)), w.make_run(started=w.NOW + timedelta(days=day))
    w.add_snap(prev, "p1", flag="firme", data_at=w.NOW + timedelta(days=day - 1),
               captured_at=w.NOW + timedelta(days=day - 1))
    w.add_snap(today, "p1", our=round(ratio * NET), data_at=w.NOW + timedelta(days=day),
               captured_at=w.NOW + timedelta(days=day))
    await pr.process_run(today, now=w.NOW + timedelta(days=day))


async def test_qa_a_kept_ficha_reopens_twice_with_the_new_baseline_each_time(fake, monkeypatch):
    item_id = w.make_item("p1", "mantenido", kept_ratio=0.958, decision="mantener")
    await _night(0, 1.06)
    assert (w.item(item_id).state, w.item(item_id).reopen_count) == ("propuesto", 1)
    # La decisión ocurre entre las noches (la pasada no decide con un dato anterior a la decisión: QA PR3 #3).
    monkeypatch.setattr(actions, "utcnow", lambda: w.NOW + timedelta(days=1))
    await actions.act(item_id, "mantener", k.PAO, expected_state="propuesto", **KEEP)
    assert w.item(item_id).kept_ratio == 1.06
    await _night(2, 1.16)                                          # 1,06 x 1,10 = 1,166: no alcanza
    assert w.item(item_id).state == "mantenido"
    await _night(4, 1.17)
    item = w.item(item_id)
    assert (item.state, item.reopen_count, item.kept_ratio) == ("propuesto", 2, 1.06)
    payloads = [json.loads(e.payload) for e in w.events("reabierto")]
    assert [p["kept_ratio"] for p in payloads] == [0.958, 1.06] and payloads[1]["ratio_hoy"] == 1.17


# ─── Carreras y fallas ambiguas (bug QA PR3 #1) ──────────────────


def _night_sees_it_off() -> int:
    run = w.make_run()
    w.add_snap(run, "rv1", enabled=False)
    return run


@pytest.mark.parametrize("who", ["conciliacion", "otra_decision"])
async def test_qa_a_reconcile_while_the_turn_off_is_in_flight_must_not_hide_that_hugo_turned_it_off(fake, monkeypatch, who):
    """Apagar con 3 reintentos tardaba hasta ~4,5 min (60-90 s por intento) y la intención se daba por muerta a los 2 min.
    Mientras la escritura viaja, corre la conciliación (la pasada, el job de arranque o el clic de otra persona con una
    pestaña vieja): ahora la escritura tiene un tope de 4 min y la intención recién vence a los 10, así que nadie la
    descarta (la otra persona recibe 409 «en curso») y la escritura que llega queda anotada."""
    item_id = _ficha(fake, "propuesto")
    real = VendureClient.set_enabled

    async def slow(self, pid, enabled):
        if who == "conciliacion":
            await actions.reconcile(item_ids=[item_id], now=later())
        else:
            monkeypatch.setattr(actions, "utcnow", later)
            with pytest.raises(actions.ActionError) as exc:
                await actions.act(item_id, "mantener", GABRIEL, expected_state="propuesto", **KEEP)
            assert (exc.value.status, exc.value.code) == (409, "en_curso")
            monkeypatch.setattr(actions, "utcnow", utcnow)
        return await real(self, pid, enabled)

    monkeypatch.setattr(VendureClient, "set_enabled", slow)
    try:
        await actions.act(item_id, "inactivar", k.PAO, expected_state="propuesto")
    except actions.ActionError:
        pass
    assert fake.enabled["rv1"] is False                            # la escritura de Hugo llegó
    await pr.process_run(_night_sees_it_off(), now=w.NOW)
    item = w.item(item_id)
    trail = (item.state, item.hugo_disabled, k.kinds(item_id), [a.action for a in k.audit("rv1")])
    assert item.hugo_disabled is True, trail
    assert [a.action for a in k.audit("rv1")] == ["price_review_disabled"], trail


def _landed_but_timed_out(fake) -> None:
    """Vendure aplica `SetEnabled` pero la respuesta no llega (timeout de lectura)."""
    handler = fake.handler

    def h(request):
        out = handler(request)
        if "SetEnabled" in json.loads(request.content)["query"]:
            raise httpx.ReadTimeout("timeout", request=request)
        return out

    fake.handler = h


async def test_qa_a_turn_off_that_landed_but_timed_out_must_not_leave_the_ficha_saying_otherwise(c, fake):
    item_id = _ficha(fake, "propuesto")
    _landed_but_timed_out(fake)
    r = k.post(c, item_id, "approve", expected_state="propuesto")
    assert fake.enabled["rv1"] is False                            # Vendure lo apagó
    await pr.process_run(_night_sees_it_off(), now=w.NOW)
    item = w.item(item_id)
    trail = (r.status_code, item.state, item.hugo_disabled, k.kinds(item_id), [a.action for a in k.audit("rv1")])
    assert item.hugo_disabled is True and item.state != "cerrado_auto", trail


async def test_qa_a_reactivation_that_landed_but_timed_out_then_unpublish_leaves_it_published(c, fake):
    item_id = _ficha(fake, "en_revision")
    _landed_but_timed_out(fake)
    r = k.decide(c, item_id, "mantener", "en_revision", KEEP["motive_code"], KEEP["motive_text"])
    assert fake.enabled["rv1"] is True                             # Vendure lo reactivó
    fake.handler = type(fake).handler.__get__(fake)
    r2 = k.decide(c, item_id, "despublicar", w.item(item_id).state, "calidad")
    trail = (r.status_code, r2.status_code, w.item(item_id).state, fake.enabled["rv1"], k.kinds(item_id))
    assert not (w.item(item_id).state == "despublicado" and fake.enabled["rv1"] is True), trail


# ─── La pasada con un snapshot de antes de la decisión (bug QA PR3 #3) ─


@pytest.mark.parametrize("decision", ["mantener", "deshacer"])
async def test_qa_a_snapshot_taken_before_the_decision_does_not_close_the_ficha(fake, monkeypatch, decision):
    """La corrida midió el producto apagado (lo había apagado Hugo); la pasada corre DESPUÉS de que Pao lo reactivó."""
    item_id = _ficha(fake, "propuesto")
    monkeypatch.setattr(actions, "utcnow", lambda: w.NOW - timedelta(hours=1))
    await actions.act(item_id, "inactivar", k.PAO, expected_state="propuesto")
    run = _night_sees_it_off()                                     # snapshot de las w.NOW: apagado
    monkeypatch.setattr(actions, "utcnow", lambda: w.NOW + timedelta(hours=1))
    if decision == "mantener":
        await actions.act(item_id, "mantener", k.PAO, expected_state="en_revision", **KEEP)
    else:
        await actions.act(item_id, "deshacer", k.PAO, expected_state="en_revision")
    assert fake.enabled["rv1"] is True
    state = w.item(item_id).state
    await pr.process_run(run, now=w.NOW + timedelta(hours=2))
    item = w.item(item_id)
    assert (item.state, item.open_key) == (state, "rv1"), k.kinds(item_id)
