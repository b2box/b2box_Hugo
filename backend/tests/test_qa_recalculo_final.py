"""QA final del recálculo (ajustes 30-oct (2)): cambio masivo y cupo diario, tope de memoria, catálogo sin cargar al
arrancar, `precio_bajo` pegado y cómo se libera, y el audit log de `pm_tier_policy` de punta a punta.

Mismo mundo que test_recalculo_al_cambiar (Vendure falso que acepta escrituras, SQLite de la suite, `pm_mode = 1`). Los que
llevan `xfail(strict=True)` son bajos «para después»: describen el comportamiento deseado y hoy no pasa.
"""

from __future__ import annotations

import os
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth, runtime, security  # noqa: E402
from app import main as main_mod  # noqa: E402
from app.config import Settings  # noqa: E402
from app.pricing import price_recalc  # noqa: E402
from app.pricing import price_review_actions as actions  # noqa: E402
from app.vendure import catalog  # noqa: E402
from tests import review_decide as k  # noqa: E402
from tests import review_world as w  # noqa: E402
from tests import test_recalculo_al_cambiar as base  # noqa: E402
from tests.test_qa_recalculo_reqa import tiers  # noqa: E402
from tests.test_recalculo_al_cambiar import (  # noqa: E402
    FIXED, LATER, WORSE, _tiered, _vp, confirm, in_vendure, item_events, night, product, reads, recalc, snap)

fake = base.fake                     # la fixture autouse del módulo de los criterios (Vendure falso, base limpia, pm_mode 1)


@pytest.fixture(autouse=True)
def notices(monkeypatch):
    monkeypatch.setattr(price_recalc, "_notices", {})
    monkeypatch.setattr(price_recalc, "_is_leader", lambda: True)
    monkeypatch.setattr(price_recalc, "_start_notice_worker", lambda: None)
    monkeypatch.setattr(catalog, "_cache", [])
    yield


# ─── cambio masivo y cupo diario ──────────────────────────────────


async def test_qa_r4_a_slow_mass_change_in_batches_under_200_still_stops_at_the_daily_cap(fake):
    """Lo que cubría el test de 2.000 productos antes de la regla de «masivo»: diez tandas de 199 (ninguna es masiva)
    siguen sin pasar de las 300 lecturas del día."""
    run = w.make_run()
    pids = [f"rv{i:04d}" for i in range(1990)]
    for pid in pids:
        w.add_snap(run, pid, tier="priceWithTax", flag="firme")
        fake.products.append(product(pid, FIXED))
    totals: dict[str, int] = {}
    for n in range(10):
        out = await recalc(*pids[n * 199:(n + 1) * 199], now=LATER + timedelta(minutes=5 * n))
        for key, value in out.items():
            totals[key] = totals.get(key, 0) + value
    assert totals == {"recalculado": 300, "tope": 1690} and reads(fake) == 300 and "masivo" not in totals


async def test_qa_r4_a_mass_batch_reads_live_items_first_and_they_still_respect_the_cap(fake):
    w.set_setting("pm_recalc_daily_max", 2)
    run = w.make_run()
    pids = [f"rv{i:03d}" for i in range(250)]
    for pid in pids:
        w.add_snap(run, pid, tier="priceWithTax", flag="firme")
        fake.products.append(product(pid, FIXED))
    for pid in pids[:3]:
        w.make_item(pid, "mantenido", kept_ratio=1.277)
    out = await recalc(*pids)
    assert out == {"recalculado": 2, "tope": 1, "masivo": 247} and reads(fake) == 2


# ─── tope de memoria y catálogo sin cargar ────────────────────────


def test_qa_mem_without_catalog_at_boot_notices_are_taken_up_to_the_hard_cap(monkeypatch):
    monkeypatch.setattr(price_recalc, "MAX_QUEUED", 50)
    assert catalog.known_ids() is None
    assert price_recalc.notify({str(i): False for i in range(80)}, now=LATER) == 50
    assert len(price_recalc._notices) == 50
    assert price_recalc.notify({"3": True}, now=LATER) == 1                 # uno ya anotado se actualiza aunque esté lleno
    assert price_recalc._notices["3"].recheck is True


def test_qa_mem_with_the_catalog_loaded_only_its_ids_are_taken(monkeypatch):
    monkeypatch.setattr(catalog, "_cache", [_vp("7", "2026-10-14T10:00:00Z"), _vp("8", "2026-10-14T10:00:00Z")])
    assert price_recalc.notify({"7": False, "9": True, "99999999999999999999": True}, now=LATER) == 1
    assert set(price_recalc._notices) == {"7"}


def test_qa_mem_the_queue_and_the_notices_share_the_cap(monkeypatch):
    """Lleno de avisos (catálogo sin cargar, recién arrancado), lo que ve el sondeo queda para la noche."""
    monkeypatch.setattr(price_recalc, "MAX_QUEUED", 10)
    price_recalc.notify({str(i): False for i in range(10)}, now=LATER)
    assert price_recalc.enqueue(["rv1"]) == 0 and price_recalc._pending == set()
    price_recalc._notices.clear()
    assert price_recalc.enqueue(["rv1", "rv1", "rv2"]) == 2 and price_recalc._pending == {"rv1", "rv2"}


# ─── precio_bajo pegado ───────────────────────────────────────────


async def _held(fake) -> int:
    night("rv1", our=1_000_000)
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", 10_000, enabled=False)
    await recalc("rv1")
    await confirm()
    assert item_events(item_id, "reactivar_a_mano")[0].motive_code == "precio_bajo"
    fake.products[0] = product("rv1", FIXED, enabled=False)                       # lo arreglan bien
    await recalc("rv1", now=LATER + timedelta(minutes=40))
    return item_id


async def test_qa_hold_a_fixed_price_after_precio_bajo_never_turns_on_alone_not_even_after_the_night(fake):
    from app.clock import utcnow
    from app.pricing import price_review as pr

    item_id = await _held(fake)
    for minutes in (80, 120, 300):
        await confirm(minutes)
    night_at = utcnow() + timedelta(hours=1)
    tomorrow = w.make_run(started=night_at)
    w.add_snap(tomorrow, "rv1", our=FIXED, color="verde", tier="priceWithTax", captured_at=night_at, data_at=night_at,
               enabled=False)
    await pr.process_run(tomorrow, now=night_at + timedelta(minutes=5))
    await price_recalc.watch_live_items(now=night_at + timedelta(minutes=40))
    assert w.item(item_id).state == "corrigiendo" and fake.writes() == []


async def test_qa_hold_a_person_can_always_reactivate_it_by_hand(fake):
    item_id = await _held(fake)
    await actions.act(item_id, actions.REACTIVAR, k.PAO, expected_state="corrigiendo")
    assert w.item(item_id).state == "corregido" and fake.writes() == [True]


async def test_qa_hold_hugo_cannot_release_its_own_hold(fake):
    """Un evento «humano» con actor hugo:auto (o un intento automático) no libera el precio_bajo."""
    from sqlmodel import Session

    from app.db.models import PriceReviewEvent
    from app.db.session import engine

    item_id = await _held(fake)
    with Session(engine) as s:
        s.add(PriceReviewEvent(item_id=item_id, product_id="rv1", actor="hugo:auto", kind="decision_corregir",
                               at=LATER + timedelta(minutes=45)))
        s.commit()
    await confirm(90)
    assert w.item(item_id).state == "corrigiendo" and fake.writes() == []


@pytest.mark.xfail(strict=True, reason="para después: con precio_bajo pegado, un precio que ya quedó bien no deja ninguna "
                                       "señal en la ficha (ni «ya no está en rojo» ni evento): solo queda el mail viejo")
async def test_qa_hold_a_fixed_price_after_precio_bajo_tells_the_person(fake):
    import json

    item_id = await _held(fake)
    await confirm(80)
    assert price_recalc.FLAG_NOT_RED in json.loads(w.item(item_id).flags or "[]")


# ─── audit log de pm_tier_policy ──────────────────────────────────


def _admin(monkeypatch) -> TestClient:
    s = Settings(vendure_api_url="https://example.invalid/admin-api", dashboard_password="", supabase_url="",
                 supabase_anon_key="", dashboard_admins="nico@b2box.pro", dashboard_secret="k" * 40)
    for mod in (auth, security, main_mod):
        monkeypatch.setattr(mod, "get_settings", lambda s=s: s)
    monkeypatch.setattr(auth, "session_username", lambda token: "nico@b2box.pro")
    return TestClient(main_mod.app)


async def test_qa_policy_a_change_made_by_an_admin_in_the_dashboard_is_recognised(monkeypatch, fake):
    """De punta a punta: el PUT real del dashboard deja la fila que mira R-2 (no una armada a mano)."""
    sid = w.add_snap(w.make_run(), "rv1", our=1_125_000, tier="tier:min_qty=10", flag="firme")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    fake.products.append(_tiered("rv1", enabled=False))
    fake.enabled["rv1"] = False
    assert _admin(monkeypatch).put("/api/settings/pm_tier_policy", json={"value": 1}).status_code == 200
    try:
        before = snap(sid).model_dump()
        out = await recalc("rv1")                         # la fila del PUT lleva el reloj real: posterior a la medición
        assert out == {"sin_cambio": 1} and snap(sid).model_dump() == before
        await confirm()
        assert item_events(item_id, "recalculado") == [] and fake.mutations == []
    finally:
        runtime.reset_to_default("pm_tier_policy")


async def test_qa_policy_a_reset_to_default_is_also_recognised(monkeypatch, fake):
    w.set_setting("pm_tier_policy", 1)
    sid = w.add_snap(w.make_run(), "rv1", our=1_000_000, tier="priceWithTax", flag="firme")
    fake.products.append(_tiered("rv1", enabled=False))
    fake.enabled["rv1"] = False
    k.disabled_by_hugo("rv1", "corrigiendo")
    assert _admin(monkeypatch).delete("/api/settings/pm_tier_policy").status_code == 200     # vuelve a 0
    before = snap(sid).model_dump()
    assert await recalc("rv1") == {"sin_cambio": 1} and snap(sid).model_dump() == before


@pytest.mark.xfail(strict=True, reason="para después: el día que un admin cambia pm_tier_policy vuelve QA-11 (un tramo "
                                       "mínimo más caro guardado en Vendure se toma por cambio de política y reactiva)")
async def test_qa_policy_qa11_is_back_on_the_day_the_policy_changed(monkeypatch, fake):
    _, sid = night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    await recalc("rv1")
    assert _admin(monkeypatch).put("/api/settings/pm_tier_policy", json={"value": 1}).status_code == 200
    assert _admin(monkeypatch).put("/api/settings/pm_tier_policy", json={"value": 0}).status_code == 200   # y vuelve
    try:
        fake.products[0] = tiers(product("rv1", FIXED, enabled=False), (1, round(WORSE / 1.21)), (10, round(FIXED / 1.21)))
        await confirm()
        assert fake.writes() == [] and w.item(item_id).state == "corrigiendo"
    finally:
        runtime.reset_to_default("pm_tier_policy")
