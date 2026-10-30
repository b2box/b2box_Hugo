"""QA del recálculo al cambiar en Vendure: intentos de romper la reactivación automática y la detección.

Mismo mundo que test_recalculo_al_cambiar (Vendure falso que acepta escrituras, SQLite de la suite, `pm_mode = 1`). Los
que llevaban `xfail(strict=True)` (QA-1 a QA-4) se arreglaron en los ajustes del 30-oct y pasan. Desde esos ajustes Hugo
reactiva solo con dos lecturas del mismo precio separadas por 30 min (`confirm`: la del vigía) y solo desde `corrigiendo`.
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest.mock as um
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")


from app.pricing import price_monitor, price_recalc  # noqa: E402
from app.pricing import price_review_actions as actions  # noqa: E402
from app.vendure import catalog  # noqa: E402
from tests import review_decide as k  # noqa: E402
from tests import review_world as w  # noqa: E402
from tests import test_recalculo_al_cambiar as base  # noqa: E402
from tests.test_recalculo_al_cambiar import (  # noqa: E402
    FIXED, LATER, WORSE, _clean_catalog, _refresh, _variant, confirm, in_vendure, item_events, night, product, reads, recalc,
    snap)

fake = base.fake                     # la fixture autouse del módulo de los criterios (Vendure falso, base limpia, pm_mode 1)


# ─── A: la reactivación automática ────────────────────────────────


async def test_qa_a2_a_price_typo_far_below_ml_is_not_reactivated_alone(fake):
    """QA-1 (arreglado): A-2 pide «sin precio extremo» y R7 solo miraba para arriba. Ahora hay piso de precio."""
    night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", 10_000, enabled=False)          # $100 contra la mediana de $9.000: le faltan dos ceros
    await recalc("rv1")
    await confirm()
    assert fake.writes() == [] and w.item(item_id).state == "corrigiendo"
    [ev] = item_events(item_id, "reactivar_a_mano")
    assert ev.motive_code == "precio_bajo"


async def test_qa_a3_a_person_who_unpublishes_between_the_recalc_and_the_write_wins(monkeypatch, fake):
    """Pao despublica justo después de que el recálculo decidió reactivar: la reactivación ve otro estado y no escribe."""
    night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    real = actions.auto_reactivate

    async def pao_first(iid, state, client=None):
        await actions.act(iid, actions.DESPUBLICAR, k.PAO, expected_state=state, motive_code="sin_margen")
        return await real(iid, state, client)

    monkeypatch.setattr(actions, "auto_reactivate", pao_first)
    await recalc("rv1")
    await confirm()
    it = w.item(item_id)
    assert it.state == "despublicado" and True not in fake.writes() and fake.enabled["rv1"] is False
    assert item_events(item_id, "decision_reactivar_auto") == []


async def test_qa_a3_a_person_and_hugo_reactivating_at_once_write_once(monkeypatch, fake):
    """«Ya lo corregí» y la reactivación automática a la vez: una sola escritura, una sola fila de AuditLog."""
    night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    real = actions.auto_reactivate

    async def both(iid, state, client=None):
        results = await asyncio.gather(actions.act(iid, actions.REACTIVAR, k.PAO, expected_state=state),
                                       real(iid, state, client), return_exceptions=True)
        if all(isinstance(r, Exception) for r in results):
            raise results[1]
        return results[1]

    monkeypatch.setattr(actions, "auto_reactivate", both)
    await recalc("rv1")
    await confirm()
    assert w.item(item_id).state == "corregido" and fake.writes() == [True]
    assert len([a for a in k.audit("rv1") if a.action == "price_review_enabled"]) == 1


async def test_qa_a3_after_an_automatic_reactivation_there_is_no_undo_of_the_previous_human_action(fake):
    night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    await recalc("rv1")
    await confirm()
    assert w.item(item_id).state == "corregido"
    evs = sorted(w.events(), key=lambda e: e.id)
    assert actions.last_human_action([e for e in evs if e.item_id == item_id]) is None


async def test_qa_a3_a_transient_vendure_error_rolls_back_and_is_not_retried_in_a_loop(fake):
    """El guardia no se puede leer (HTTP 500): la ficha vuelve a como estaba, sale en PARA MIRAR A MANO y las lecturas
    siguientes con el mismo precio no lo reintentan (un intento por precio), ni leen de más."""
    night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    fake.http_fail.add("ReviewGuard")
    await recalc("rv1")
    await confirm()
    it = w.item(item_id)
    assert it.state == "corrigiendo" and it.hugo_disabled is True and fake.writes() == []
    assert item_events(item_id, "vendure_error")
    fake.http_fail.clear()
    assert await recalc("rv1", now=LATER + timedelta(minutes=35)) == {"sin_cambio": 1}
    assert await confirm(60) == {"sin_cambio": 1}
    assert fake.writes() == [] and reads(fake) == 4 and len(item_events(item_id, "vendure_error")) == 1


async def test_qa_a5_the_mode_switched_off_mid_run_stops_the_write(monkeypatch, fake):
    """`pm_mode` pasa a 0 entre el recálculo y la escritura: `act` lo vuelve a mirar y no escribe."""
    night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    real = actions.auto_reactivate

    async def switch_off(iid, state, client=None):
        w.set_setting("pm_mode", 0)
        return await real(iid, state, client)

    monkeypatch.setattr(actions, "auto_reactivate", switch_off)
    await recalc("rv1")
    await confirm()
    assert w.item(item_id).state == "corrigiendo" and fake.writes() == []


async def test_qa_a5_the_daily_cap_holds_across_the_queue_and_the_watch(fake):
    """El tope de reactivaciones es uno solo para la cola y el vigía, por día ART. (Re-QA: con la doble lectura, la versión
    anterior pasaba sin probar nada: rv2 nunca llegaba a intentarse. Ahora la cola reactiva rv1 con su segunda lectura y el
    vigía encuentra el tope gastado para rv2.)"""
    w.set_setting("pm_recalc_reactivations_daily_max", 1)
    run = w.make_run()
    ids = {}
    for pid in ("rv1", "rv2"):
        night(pid, run=run)
        ids[pid] = k.disabled_by_hugo(pid, "corrigiendo")
        in_vendure(fake, pid, FIXED, enabled=False)
    await recalc("rv1", "rv2")                                                   # primera lectura de los dos
    assert await recalc("rv1", now=LATER + timedelta(minutes=30)) == {"sin_cambio": 1}   # la cola confirma rv1
    assert w.item(ids["rv1"]).state == "corregido" and fake.writes() == [True]
    await price_recalc.watch_live_items(now=LATER + timedelta(minutes=31))       # el vigía confirma rv2: tope gastado
    assert w.item(ids["rv2"]).state == "corrigiendo" and fake.writes() == [True]
    assert item_events(ids["rv2"], "aviso_reactivar")[0].motive_code == "tope_del_dia"


async def test_qa_a5_the_not_red_warning_goes_away_when_the_next_night_measures_it_red_again(fake):
    """QA-3 (arreglado): la marca «ya_no_rojo» del tope ya no sobrevive a una noche que lo mide rojo otra vez."""
    from app.pricing import price_review as pr

    w.set_setting("pm_recalc_reactivations_daily_max", 0)
    night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    await recalc("rv1")
    await confirm()
    assert price_recalc.FLAG_NOT_RED in json.loads(w.item(item_id).flags)
    tomorrow = w.make_run(started=w.NOW + timedelta(days=1))
    w.add_snap(tomorrow, "rv1", our=FIXED, prices=(500_000, 505_000, 495_000), tier="priceWithTax",   # ML bajó: rojo
               captured_at=w.NOW + timedelta(days=1), data_at=w.NOW + timedelta(days=1), enabled=False)
    await pr.process_run(tomorrow, now=w.NOW + timedelta(days=1, hours=1))
    it = w.item(item_id)
    assert it.last_color == "rojo" and price_recalc.FLAG_NOT_RED not in json.loads(it.flags)


async def test_qa_b6_what_was_skipped_for_a_write_in_flight_is_applied_once_the_write_ends(fake):
    """QA-4 (arreglado). Hugo está en medio de una escritura (intención abierta) y justo lo corrigen en Vendure: la ficha no
    se toca (B-6). Cuando la escritura termina, el vigía de 30 min ve que ya no es rojo y, con el mismo precio que leyó el
    recálculo, lo reactiva. Desde los ajustes del 30-oct eso es solo desde `corrigiendo` (antes el test usaba `en_revision`)."""
    from sqlmodel import Session

    from app.db.models import PriceReviewEvent
    from app.db.session import engine

    night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    with Session(engine) as s:
        s.add(PriceReviewEvent(item_id=item_id, product_id="rv1", actor=k.PAO, kind="intent", at=LATER,
                               enabled_before=True, enabled_after=False))
        s.commit()
    in_vendure(fake, "rv1", FIXED, enabled=False)
    await recalc("rv1")
    assert w.item(item_id).last_color is None or w.item(item_id).last_color == "rojo"    # B-6: no se tocó
    with Session(engine) as s:
        s.add(PriceReviewEvent(item_id=item_id, product_id="rv1", actor=k.PAO, kind="inactivado", at=LATER,
                               enabled_before=True, enabled_after=False))
        s.commit()
    await price_recalc.watch_live_items(now=LATER + timedelta(minutes=30))
    assert w.item(item_id).state == "corregido" and fake.writes() == [True]


async def test_qa_a4_a_bx_without_photo_disabled_after_hugo_stops_the_automatic_reactivation(fake):
    from sqlmodel import Session

    from app.db.models import AuditLog
    from app.db.session import engine

    night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    with Session(engine) as s:
        s.add(AuditLog(action="bx_no_image_disabled", source="audit", product_id="rv1",
                       created_at=w.NOW + timedelta(hours=1), before=json.dumps({"enabled": False})))
        s.commit()
    await recalc("rv1")
    await confirm()
    assert w.item(item_id).state == "corrigiendo" and fake.writes() == []
    [ev] = item_events(item_id, "reactivar_a_mano")
    assert ev.motive_code == "bx_sin_imagen"


async def test_qa_r2_another_currency_touches_nothing(fake):
    """R-2: la moneda también cuenta. Un precio en otra moneda no se compara ni se recalcula (ni reactiva)."""
    _, sid = night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", 6_000, enabled=False, currency="USD")
    before = snap(sid).model_dump()
    assert await recalc("rv1") == {"moneda": 1}
    assert snap(sid).model_dump() == before and w.item(item_id).state == "corrigiendo" and fake.writes() == []


async def test_qa_a_hugo_own_write_does_not_loop(monkeypatch, fake):
    """La reactivación cambia el `updatedAt` del producto: el refresh siguiente lo encola, se lee una vez y no pasa nada."""
    night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    await recalc("rv1")
    await confirm()
    n_events = len(item_events(item_id))
    fake.products[0] = {**product("rv1", FIXED), "updatedAt": "2026-10-14T11:30:00Z", "enabled": True}
    for minutes in (35, 40, 45):
        assert await recalc("rv1", now=LATER + timedelta(minutes=minutes)) == {"sin_cambio": 1}
    assert fake.writes() == [True] and len(item_events(item_id)) == n_events


async def test_qa_a7_red_again_after_the_automatic_reactivation_never_turns_it_off_nor_opens_an_item(fake):
    night("rv1")
    k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    await recalc("rv1")
    await confirm()
    fake.products[0] = product("rv1", WORSE)
    await recalc("rv1", now=LATER + timedelta(minutes=40))
    assert fake.writes() == [True] and [i.state for i in w.items()] == ["corregido"]


# ─── R: detección, carreras y cambios masivos ─────────────────────


async def test_qa_r4_a_mass_change_of_2000_products_reads_only_the_cap_and_ends(fake):
    """2.000 productos cambiados en un refresh (un cambio de fórmula): se leen 300, el resto va a la noche, la cola queda
    vacía y no hay escrituras."""
    run = w.make_run()
    pids = [f"rv{i:04d}" for i in range(2000)]
    for pid in pids:
        w.add_snap(run, pid, tier="priceWithTax", flag="firme")
        fake.products.append(product(pid, FIXED))
    out = await recalc(*pids)
    assert out == {"recalculado": 300, "tope": 1700} and reads(fake) == 300
    assert price_recalc._pending == set() and fake.mutations == []
    r = w.run_of(run)
    assert r.n_verde == 300 and r.n_rojo == 1700


async def test_qa_r1a_a_mass_variant_save_enqueues_every_product_once(monkeypatch, fake):
    """10.000 variantes guardadas (2.000 productos x 5) entre dos refresh: se paginan y cada producto entra una vez."""
    _clean_catalog(monkeypatch)
    w.set_setting("pm_recalc_daily_max", 0)                 # solo la detección: nada que leer
    pids = [f"rv{i:04d}" for i in range(2000)]
    for pid in pids:
        fake.products.append(product(pid, 1_000_000))
    fake.variant_updates = [_variant(pids[0], "2026-10-14T10:00:00Z", "seed")]
    await _refresh(monkeypatch)
    fake.variant_updates += [_variant(pid, f"2026-10-14T11:{(i // 200):02d}:00Z", f"v{pid}-{j}")
                             for i, pid in enumerate(pids) for j in range(5)]
    seen: list[str] = []
    monkeypatch.setattr(price_recalc, "enqueue", lambda ids: seen.extend(ids))
    await _refresh(monkeypatch)
    assert sorted(seen) == pids
    assert catalog._variant_watermark == "2026-10-14T11:09:00Z"


async def test_qa_r1a_a_variant_committed_late_inside_the_margin_is_still_seen(monkeypatch, fake):
    """Una variante con `updatedAt` anterior al watermark (la transacción tardó en confirmar) pero dentro del margen."""
    _clean_catalog(monkeypatch)
    _, sid = night("rv1")
    night("rv2", run=w.run_of(snap(sid).run_id).id)
    in_vendure(fake, "rv1", 1_000_000)
    in_vendure(fake, "rv2", 1_000_000)
    fake.variant_updates = [_variant("rv2", "2026-10-14T10:00:00Z")]
    await _refresh(monkeypatch)
    fake.variant_updates.append(_variant("rv2", "2026-10-14T11:00:00Z", "v2b"))     # mueve el watermark a 11:00
    await _refresh(monkeypatch)
    fake.products[0] = product("rv1", FIXED)
    fake.variant_updates.append(_variant("rv1", "2026-10-14T10:59:00Z"))            # confirmó tarde, 1 min antes
    await _refresh(monkeypatch)
    assert snap(sid).our_price_cents == FIXED


async def test_qa_r1a_a_transient_graphql_error_does_not_switch_variants_off_forever(monkeypatch, fake):
    """QA-2 (arreglado): solo un error de schema apaga la detección por variantes; uno pasajero se reintenta."""
    _clean_catalog(monkeypatch)
    _, sid = night("rv1")
    in_vendure(fake, "rv1", 1_000_000)
    fake.variant_updates = [_variant("rv1", "2026-10-14T10:00:00Z")]
    await _refresh(monkeypatch)                                                       # arranque
    fake.script["VariantsUpdatedSince"] = [
        (200, {"errors": [{"message": "Connection terminated due to connection timeout"}]})]
    await _refresh(monkeypatch)                                                       # un error de una vez
    fake.products[0] = product("rv1", FIXED)                                          # después se guarda la variante
    fake.variant_updates[0] = _variant("rv1", "2026-10-14T11:15:00Z")
    await _refresh(monkeypatch)
    assert snap(sid).our_price_cents == FIXED


async def test_qa_r5_refresh_catalog_survives_a_broken_database_for_the_recalc(monkeypatch, fake):
    """La base no responde al armar el contexto del recálculo: el job no levanta."""
    from app.scheduler import jobs

    async def no_catalog(force=False, full=False):
        catalog._changed.add("rv1")
        return []

    def broken(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(catalog, "get_catalog", no_catalog)
    monkeypatch.setattr(price_recalc.pr.ReviewParams, "from_runtime", classmethod(lambda cls: broken()))
    await jobs.refresh_catalog()


async def test_qa_r5_the_queue_and_the_watch_never_recalculate_at_once(fake):
    """La cola y el vigía comparten `_busy`: el mismo producto por los dos caminos se lee de a uno y se recalcula una vez."""
    night("rv1")
    w.make_item("rv1", "mantenido", kept_ratio=1.277)
    in_vendure(fake, "rv1", FIXED)
    price_recalc.enqueue(["rv1"])
    a, b = await asyncio.gather(price_recalc.run_pending(now=LATER), price_recalc.watch_live_items(now=LATER))
    assert sorted([*a, *b]) == ["recalculado", "sin_cambio"] and reads(fake) == 2
    assert len(w.events("recalculado")) == 1


async def test_qa_r5_the_night_started_during_the_queue_leaves_no_snapshot_half_done(fake):
    """La noche arranca mientras la cola lee: lo que ya se leyó termina entero, lo demás queda en la cola y la noche
    misma no ve una corrida con los contadores a medias."""
    run = w.make_run()
    for pid in ("rv1", "rv2", "rv3"):
        night(pid, run=run)
        in_vendure(fake, pid, FIXED)
    real = price_recalc.VendureClient.get_product_priced
    started = False

    async def start_night(self, pid):
        nonlocal started
        if not started:
            started = True
            await price_monitor.price_monitor_lock.acquire()
        return await real(self, pid)

    price_recalc.enqueue(["rv1", "rv2", "rv3"])
    try:
        with um.patch.object(price_recalc.VendureClient, "get_product_priced", start_night):
            out = await price_recalc.run_pending(now=LATER)
    finally:
        price_monitor.price_monitor_lock.release()
    done = out.get("recalculado", 0)
    r = w.run_of(run)
    assert done + out.get("corrida", 0) == 3 and len(price_recalc._pending) == out.get("corrida", 0)
    assert (r.n_verde, r.n_rojo) == (done, 3 - done)
    assert all(json.loads(snap(s.id).recalc_before)["color"] == "rojo" for s in [w.snap_of(run, p) for p in
               ("rv1", "rv2", "rv3")] if snap(s.id).recalc_at)
