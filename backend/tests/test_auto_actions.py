"""«¿Qué prendió y qué apagó Hugo solo?» (pricing/auto_actions.py y GET /api/price-monitor/gate/auto-actions): cada
escritura automática queda con el producto, cuándo, por qué regla y con qué corrida, y se puede listar por días."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import delete
from sqlmodel import Session, select

from app import prohibidos
from app.db.models import LuisIntake, PriceReviewEvent
from app.db.session import engine
from app.pricing import auto_actions, price_gate
from app.pricing import price_review as pr
from app.pricing import price_review_actions as actions
from tests import review_decide as kit
from tests import review_world as w

GREEN_PRICES = (1_900_000, 1_950_000, 2_000_000)
URL = "/api/price-monitor/gate/auto-actions"


@pytest.fixture
def fake(monkeypatch):
    f = kit.setup(monkeypatch, mode=2)
    w.set_setting("pm_gate_measures_check", 0)
    prohibidos.reiniciar()
    with Session(engine) as s:
        s.execute(delete(LuisIntake))
        s.commit()
    yield f
    w.reset_db()                    # deja los settings y la bandeja como estaban: otras pruebas comparten la base
    with Session(engine) as s:
        s.execute(delete(LuisIntake))
        s.commit()


def _upload(pid: str) -> None:
    with Session(engine) as s:
        s.add(LuisIntake(offer_id=f"o{pid}", source_url=f"https://detail.1688.com/offer/{pid}.html", name="x",
                         image_url="https://cbu01.alicdn.com/x.jpg", vendure_product_id=pid,
                         uploaded_at=w.NOW - timedelta(days=1)))
        s.commit()


async def _night(fake) -> int:
    """Una noche con un verde de Paco, un verde del catálogo apagado y un rojo firme publicado."""
    run = w.make_run()
    w.add_snap(run, "a1", color="verde", prices=GREEN_PRICES, enabled=False, name="Organizador de cocina")
    w.add_snap(run, "a2", color="verde", prices=GREEN_PRICES, enabled=False, name="Lámpara de escritorio")
    fake.enabled.update({"a1": False, "a2": False, "a3": True})
    _upload("a1")
    red = w.add_snap(run, "a3", color="rojo", enabled=True, name="Soporte de celular", flag=pr.FIRME)
    w.make_item("a3", pr.PROPUESTO, last_run_id=run, last_snapshot_id=red)
    counts = await price_gate.process_run(run, now=w.NOW)
    assert counts == {"apagado": 1, "activado": 1, "catalogo_activado": 1}, counts
    return run


async def test_cada_escritura_automatica_queda_con_regla_y_corrida(fake):
    run = await _night(fake)
    out = auto_actions.list_auto_actions(7, now=w.NOW + timedelta(hours=1))
    assert out["totals"] == {"activados": 2, "apagados": 1, "deshechos_por_una_persona": 0}
    by_pid = {i["product_id"]: i for i in out["items"]}
    assert by_pid["a1"]["quedo"] == "activo" and by_pid["a1"]["regla"] == "verde_firme_subido_por_paco"
    assert by_pid["a2"]["quedo"] == "activo" and by_pid["a2"]["regla"] == "verde_firme_catalogo_apagado"
    assert by_pid["a3"]["quedo"] == "inactivo" and by_pid["a3"]["regla"] == "rojo_firme_publicado"
    for row in by_pid.values():
        assert row["corrida"] == run and row["regla_deducida"] is False and row["at"]
        assert row["accion"] in (actions.ACTIVAR_AUTO, actions.INACTIVAR_AUTO) and row["accion_texto"]
    assert by_pid["a1"]["product_name"] == "Organizador de cocina" and by_pid["a1"]["product_code"] == "BXa1"
    # El evento que confirma la escritura lleva la corrida y la regla (es de solo agregar).
    with Session(engine) as s:
        ev = s.exec(select(PriceReviewEvent).where(PriceReviewEvent.product_id == "a2",
                                                   PriceReviewEvent.kind == "reactivado")).one()
    assert ev.actor == pr.ACTOR_AUTO and ev.run_id == run and pr._loads(ev.payload)["regla"] == "verde_firme_catalogo_apagado"


async def test_dice_si_una_persona_lo_deshizo_y_respeta_la_ventana_de_dias(fake):
    await _night(fake)
    item = next(i for i in w.items() if i.product_id == "a2")
    assert kit.post(kit.client(), int(item.id), "undo", expected_state=pr.ACTIVADO).status_code == 200
    out = auto_actions.list_auto_actions(7)
    row = next(i for i in out["items"] if i["product_id"] == "a2")
    assert row["deshecho_por"] == kit.PAO and out["totals"]["deshechos_por_una_persona"] == 1
    # Lo que hace una persona (el Deshacer apagó el producto) no es de Hugo: no suma.
    assert out["totals"]["apagados"] == 1
    assert auto_actions.list_auto_actions(1, now=w.NOW + timedelta(days=30))["items"] == []


async def test_una_escritura_vieja_sin_regla_la_deduce_del_porton(fake):
    """Las de antes de que la regla viajara en el evento: se deduce del `porton` de la misma ficha y se marca."""
    fake.enabled["o1"] = False
    item_id = w.make_item("o1", pr.POR_ACTIVAR, open_key="o1")
    with Session(engine, expire_on_commit=False) as s:
        item = s.get(pr.PriceReviewItem, item_id)
        pr.add_event(s, item, "porton", to_state=pr.POR_ACTIVAR, run_id=41, motive_code="verde_firme", now=w.NOW)
        s.commit()
    await actions.act(item_id, actions.ACTIVAR_AUTO, pr.ACTOR_AUTO, expected_state=pr.POR_ACTIVAR)   # sin `audit`
    [row] = auto_actions.list_auto_actions(7)["items"]
    assert (row["regla"], row["corrida"], row["regla_deducida"]) == ("verde_firme", 41, True)


async def test_una_escritura_conciliada_conserva_la_regla(fake, monkeypatch):
    """El proceso muere entre escribir y confirmar: la conciliación completa el evento con la misma regla y corrida."""
    from app.clock import utcnow

    fake.enabled["k1"] = False
    item_id = w.make_item("k1", pr.POR_ACTIVAR, open_key="k1")

    def dies(*_a, **_k):
        raise RuntimeError("el proceso murió")

    with monkeypatch.context() as m:
        m.setattr(actions, "_confirm", dies)
        with pytest.raises(RuntimeError):
            await actions.auto_gate(item_id, actions.ACTIVAR_AUTO, pr.POR_ACTIVAR, rule="verde_firme_subido_por_paco",
                                    run_id=77)
    assert fake.enabled["k1"] is True and auto_actions.list_auto_actions(7)["items"] == []
    assert await actions.reconcile(now=utcnow() + actions.INTENT_STALE + timedelta(minutes=1)) == 1
    [row] = auto_actions.list_auto_actions(7)["items"]
    assert (row["regla"], row["corrida"], row["conciliado"], row["regla_deducida"]) == (
        "verde_firme_subido_por_paco", 77, True, False)


def test_el_endpoint_es_solo_para_un_administrador_y_no_escribe(fake):
    assert kit.client(None).get(URL).status_code == 401
    assert kit.client().get(URL).status_code == 403                     # Pao decide, pero no es admin
    r = kit.client(kit.ADMIN).get(f"{URL}?days=3")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days"] == 3 and body["items"] == [] and body["totals"]["activados"] == 0
    assert body["modo"] == {"pm_mode": 2, "pm_autopilot": 1, "prohibidos_gate": 1}
    assert body["lista_prohibidos"]["fuente"] == "archivo" and body["lista_prohibidos"]["completa"] is True
    assert body["piloto_automatico"] == {"modo_cambiado": None, "corridas": []}
    assert fake.mutations == []
    assert kit.client(kit.ADMIN).get(f"{URL}?days=0").status_code == 422


async def test_el_endpoint_lista_lo_que_hizo_el_porton(fake):
    run = await _night(fake)
    body = kit.client(kit.ADMIN).get(URL).json()
    assert body["shown"] == 3 and {i["corrida"] for i in body["items"]} == {run}
    assert kit.client(kit.ADMIN).get(f"{URL}?limit=1").json()["shown"] == 1
