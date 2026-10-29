"""Recálculo del semáforo y de la bandeja cuando un producto cambia en Vendure (`semaforo-recalculo-al-cambiar-criterios.md`):
detección (R-*), semáforo (S-*), bandeja (B-*), reactivación automática (A-*) y regresión (K-*).

Vendure es el doble HTTP de `review_fixtures` (acepta escrituras): el recálculo lee con el cliente real (`PricedProduct`) y
la reactivación escribe por `price_review_actions`, que queda anotada en `fake.mutations`. La base es la SQLite de la suite;
`review_decide.setup` fija `hugo_env` y deja `pm_mode = 1`.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta
from types import SimpleNamespace

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.db.models import AuditLog, MarketPriceSnapshot, PriceReviewEvent  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import daily_budget, price_monitor, price_recalc, price_review_digest, semaforo  # noqa: E402
from app.pricing import price_review as pr  # noqa: E402
from app.vendure import catalog  # noqa: E402
from app.vendure.client import VendureClient, VendureProduct  # noqa: E402
from tests import review_decide as k  # noqa: E402
from tests import review_world as w  # noqa: E402
from tests.review_fixtures import engine_variant  # noqa: E402

LATER = w.NOW + timedelta(hours=2)        # el cambio en Vendure, dos horas después de la corrida
FIXED, CHEAPER, WORSE = 600_000, 700_000, 1_200_000      # contra la mediana 900.000: verde, amarillo, rojo peor


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    f = k.setup(monkeypatch)
    price_recalc._pending.clear()
    yield f
    price_recalc._pending.clear()
    k.clean_audit()


def product(pid: str, price: int, *, enabled: bool = True, currency: str = "ARS", name: str | None = None) -> dict:
    """Un producto crudo como lo devuelve Vendure, con una variante sin tramos: el precio usado es `priceWithTax`."""
    return {"id": pid, "name": name or f"Producto {pid}", "slug": f"producto-{pid}", "description": "", "enabled": enabled,
            "updatedAt": "2026-10-14T11:00:00Z", "customFields": {"b2boxProductCode": f"BX{pid}"},
            "featuredAsset": {"source": f"https://cdn.b2box/{pid}.jpg", "preview": None},
            "variantList": {"items": [{"id": "101", "name": "Único", "sku": f"SKU{pid}", "priceWithTax": price,
                                       "currencyCode": currency, "customFields": {}, "bulkPriceTiers": []}],
                            "totalItems": 1}}


def night(pid: str, *, run: int | None = None, **kw) -> tuple[int, int]:
    """La corrida de la noche con el snapshot del producto (rojo firme por defecto, precio 1.000.000 con `priceWithTax`)."""
    run = run or w.make_run()
    kw.setdefault("flag", "firme")
    return run, w.add_snap(run, pid, tier="priceWithTax", **kw)


def in_vendure(fake, pid: str, price: int, **kw) -> None:
    fake.products.append(product(pid, price, **kw))
    fake.enabled.setdefault(pid, kw.get("enabled", True))


async def recalc(*pids: str, now=LATER) -> dict:
    price_recalc.enqueue(pids)
    return await price_recalc.run_pending(now=now)


def snap(sid: int) -> MarketPriceSnapshot:
    with Session(engine) as s:
        return s.get(MarketPriceSnapshot, sid)


def item_events(item_id: int, kind: str | None = None) -> list[PriceReviewEvent]:
    return [e for e in w.events(kind) if e.item_id == item_id]


def reads(fake) -> int:
    return fake.count("PricedProduct")


# ─── R: detección ─────────────────────────────────────────────────


def _vp(pid: str, updated: str) -> VendureProduct:
    return VendureProduct(id=pid, name=pid, slug=pid, description="", enabled=True, source_url=None, image_urls=[],
                          product_code=None, featured_image_url=None, first_variant_price_cents=1, variant_count=1,
                          updated_at=updated)


async def test_r1_the_incremental_refresh_notes_only_products_saved_again_in_vendure(monkeypatch):
    """Cuenta solo lo que ya estaba en el cache con otro `updatedAt`: ni los del margen del watermark (mismo `updatedAt`)
    ni los nuevos (no tienen nada medido)."""
    remote = {"1": _vp("1", "2026-10-14T10:00:00.000Z"), "2": _vp("2", "2026-10-14T10:05:00.000Z")}

    class Fake:
        async def fetch_products_updated_since(self, since, with_variants=False, page_size=None):
            return [p for p in remote.values() if p.updated_at > since]

        async def count_products(self):
            return len(remote)

        async def fetch_all_products(self, **_k):
            return list(remote.values())

    monkeypatch.setattr(catalog, "VendureClient", Fake)
    monkeypatch.setattr(catalog, "get_settings",
                        lambda: SimpleNamespace(verify_catalog_ttl_seconds=300, catalog_full_refresh_seconds=43200))
    for name, value in (("_cache", []), ("_loaded_at", 0.0), ("_full_at", None), ("_watermark", None)):
        monkeypatch.setattr(catalog, name, value)
    monkeypatch.setattr(catalog, "_changed", set())
    await catalog.get_catalog()
    assert catalog.pop_changed() == []                                        # el primer full no sabe qué cambió
    remote["1"] = _vp("1", "2026-10-14T11:00:00.000Z")                          # alguien lo guardó en Vendure
    remote["3"] = _vp("3", "2026-10-14T11:01:00.000Z")                          # producto nuevo
    await catalog.get_catalog(force=True)
    assert catalog.pop_changed() == ["1"] and catalog.pop_changed() == []
    await catalog.get_catalog(force=True)                                      # el margen trae otra vez a 1 y 3, iguales
    assert catalog.pop_changed() == []
    remote["2"] = _vp("2", "2026-10-14T12:00:00.000Z")
    await catalog.get_catalog(full=True)                                       # el full de cada 12 h también lo ve
    assert catalog.pop_changed() == ["2"]


async def test_r1_refresh_catalog_queues_what_changed_and_recalculates_it(monkeypatch, fake):
    from app.scheduler import jobs

    _, sid = night("rv1")
    in_vendure(fake, "rv1", FIXED)

    async def fake_catalog(force=False, full=False):
        catalog._changed.add("rv1")
        return []

    monkeypatch.setattr(catalog, "get_catalog", fake_catalog)
    monkeypatch.setattr(price_recalc, "utcnow", lambda: LATER)
    await jobs.refresh_catalog()
    assert snap(sid).recalc_at == LATER and snap(sid).our_price_cents == FIXED


def test_r1_the_refresh_job_runs_well_within_ten_minutes():
    """≤ 10 minutos desde que se guardó: el job corre cada TTL - 30 s (270 s con el default) y recalcula en la misma vuelta."""
    from app.config import Settings
    from app.scheduler import jobs

    jobs.register_jobs()
    try:
        job = jobs.scheduler.get_job("refresh_catalog")
        assert job is not None and job.trigger.interval <= timedelta(minutes=5)
    finally:
        for job in jobs.scheduler.get_jobs():                                   # como clean_scheduler de test_price_monitor
            jobs.scheduler.remove_job(job.id)
    assert Settings(vendure_api_url="https://x.invalid/admin-api").verify_catalog_ttl_seconds - 30 <= 300


async def test_r2_same_price_and_tier_does_nothing_at_all(fake):
    run, sid = night("rv1", our=1_000_000)
    item_id = w.make_item("rv1")
    in_vendure(fake, "rv1", 1_000_000)
    before = snap(sid).model_dump()
    assert await recalc("rv1") == {"sin_cambio": 1}
    assert reads(fake) == 1 and snap(sid).model_dump() == before
    assert item_events(item_id) == item_events(item_id, "propuesto") and fake.mutations == []


async def test_r2_the_read_is_the_nightly_one_and_survives_a_schema_without_the_optional_fields(fake):
    """La lectura del producto pide lo mismo que el listado de la noche (tramos, medidas, costo) y, si el schema no tiene
    alguna parte opcional, la apaga y repite, como la noche: el recálculo no se queda sin precio por un campo."""
    _, sid = night("rv1")
    in_vendure(fake, "rv1", FIXED)
    fake.unknown_fields = {"sourcingMode"}
    assert await recalc("rv1") == {"recalculado": 1} and snap(sid).our_price_cents == FIXED
    queries = [r["query"] for r in fake.requests if r["op"] == "PricedProduct"]
    assert "sourcingMode" in queries[0] and "sourcingMode" not in queries[-1] and "bulkPriceTiers" in queries[-1]


@pytest.mark.parametrize("case,kw,reason", [
    ("viejo", dict(data_at=w.NOW - timedelta(hours=47)), "dato_viejo"),          # 49 h al momento del cambio
    ("sin_ml", dict(ml_status="no_data", color="sin_dato"), "sin_dato_ml"),
    ("oficina_sin_fecha", dict(origin="oficina", data_at=None), "dato_viejo"),
])
async def test_r3_without_fresh_ml_data_nothing_is_touched_and_vendure_is_not_read(fake, case, kw, reason):
    _, sid = night("rv1", **kw)
    in_vendure(fake, "rv1", FIXED)
    before = snap(sid).model_dump()
    assert await recalc("rv1") == {reason: 1}
    assert reads(fake) == 0 and snap(sid).model_dump() == before


async def test_r3_a_product_never_measured_is_left_for_the_night(fake):
    in_vendure(fake, "rv9", FIXED)
    assert await recalc("rv9") == {"sin_snapshot": 1} and reads(fake) == 0


async def test_r4_daily_cap_of_reads_and_the_rest_goes_to_the_night(fake):
    w.set_setting("pm_recalc_daily_max", 1)
    run = w.make_run()
    for pid in ("rv1", "rv2"):
        night(pid, run=run)
        in_vendure(fake, pid, FIXED)
    out = await recalc("rv1", "rv2")
    assert out == {"recalculado": 1, "tope": 1} and reads(fake) == 1
    assert price_recalc._pending == set()                                     # lo que pasó el tope no queda en la cola
    assert daily_budget.used_today(price_recalc.READS_KEY, day=daily_budget.today_art(LATER)) == 1


async def test_r4_a_mass_change_reads_two_products_at_a_time(fake):
    run = w.make_run()
    pids = [f"rv{i}" for i in range(8)]
    for pid in pids:
        night(pid, run=run)
    inflight = peak = 0
    paired = asyncio.Event()

    class Slow:
        async def get_product_priced(self, pid):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            if inflight >= 2:
                paired.set()
            try:                                         # la primera espera a que haya otra en vuelo (sin depender del reloj)
                await asyncio.wait_for(paired.wait(), 2.0)
            except (asyncio.TimeoutError, TimeoutError):
                pass
            loop = asyncio.get_running_loop()
            done = loop.create_future()                  # una espera real (review_fixtures anula asyncio.sleep): una
            loop.call_later(0.02, done.set_result, None)  # tercera lectura en paralelo entraría acá
            await done
            inflight -= 1
            return VendureClient()._map_page([product(pid, FIXED)], with_variants=False, pricing=True)[0]

    price_recalc.enqueue(pids)
    out = await price_recalc.run_pending(client=Slow(), now=LATER)
    assert out["recalculado"] == 8 and peak == price_recalc.CONCURRENCY == 2


def test_r4_both_caps_are_admin_only_settings():
    from app.api import routes

    assert {"pm_recalc_daily_max", "pm_recalc_reactivations_daily_max"} <= routes._COSTLY_SETTINGS
    c = k.client()                                                              # Pao no es admin
    assert c.put("/api/settings/pm_recalc_daily_max", json={"value": 2000}).status_code == 403
    assert c.put("/api/settings/pm_recalc_reactivations_daily_max", json={"value": 500}).status_code == 403


async def test_r5_nothing_runs_while_the_night_is_running_and_the_queue_waits(fake):
    _, sid = night("rv1")
    in_vendure(fake, "rv1", FIXED)
    async with price_monitor.price_monitor_lock:
        assert await recalc("rv1") == {"corrida": 1}
    assert reads(fake) == 0 and price_recalc._pending == {"rv1"} and snap(sid).recalc_at is None
    assert await price_recalc.run_pending(now=LATER) == {"recalculado": 1}


async def test_r5_if_the_night_starts_midway_what_is_left_stays_queued(fake):
    run = w.make_run()
    for pid in ("rv1", "rv2", "rv3", "rv4"):
        night(pid, run=run)
    night_started = asyncio.Event()

    class StartsTheNight:
        async def get_product_priced(self, pid):
            if not night_started.is_set():
                night_started.set()
                await price_monitor.price_monitor_lock.acquire()               # arranca la corrida de la noche
            return VendureClient()._map_page([product(pid, FIXED)], with_variants=False, pricing=True)[0]

    price_recalc.enqueue(["rv1", "rv2", "rv3", "rv4"])
    try:
        out = await price_recalc.run_pending(client=StartsTheNight(), now=LATER)
    finally:
        price_monitor.price_monitor_lock.release()
    assert out.get("corrida", 0) >= 2 and price_recalc._pending and len(price_recalc._pending) == out["corrida"]


async def test_r5_a_snapshot_of_an_unfinished_run_waits_in_the_queue(fake):
    run = w.make_run(status="running")
    night("rv1", run=run)
    in_vendure(fake, "rv1", FIXED)
    assert await recalc("rv1") == {"corrida": 1} and price_recalc._pending == {"rv1"} and reads(fake) == 0


async def test_r5_an_error_never_breaks_refresh_catalog(monkeypatch):
    from app.scheduler import jobs

    async def boom(*_a, **_k):
        raise RuntimeError("recálculo roto")

    async def no_catalog(force=False, full=False):
        raise RuntimeError("Vendure caído")

    monkeypatch.setattr(catalog, "get_catalog", no_catalog)
    monkeypatch.setattr(price_recalc, "run_pending", boom)
    await jobs.refresh_catalog()                                                # no levanta


async def test_r5_a_failing_product_does_not_stop_the_others(fake):
    run = w.make_run()
    for pid in ("rv1", "rv2"):
        night(pid, run=run)

    class Flaky:
        async def get_product_priced(self, pid):
            if pid == "rv1":
                raise RuntimeError("Vendure no respondió")
            return VendureClient()._map_page([product(pid, FIXED)], with_variants=False, pricing=True)[0]

    price_recalc.enqueue(["rv1", "rv2"])
    assert await price_recalc.run_pending(client=Flaky(), now=LATER) == {"error": 1, "recalculado": 1}


# ─── S: semáforo ──────────────────────────────────────────────────


async def test_s1_the_snapshot_is_recalculated_against_the_same_identicals_and_keeps_the_before(fake):
    run, sid = night("rv1", our=1_000_000)
    in_vendure(fake, "rv1", FIXED, enabled=True)
    before = snap(sid)
    assert await recalc("rv1") == {"recalculado": 1}
    after = snap(sid)
    margin = semaforo.estimated_margin_pct(900_000, FIXED, 13.0, 0)
    assert after.our_price_cents == FIXED and after.color == "verde" and after.est_margin_pct == round(margin, 2)
    assert (after.ml_median_cents, after.ml_min_cents, after.commission_pct, after.shipping_cents, after.matched_listings,
            after.data_at, after.captured_at) == (before.ml_median_cents, before.ml_min_cents, before.commission_pct,
                                                  before.shipping_cents, before.matched_listings, before.data_at,
                                                  before.captured_at)
    assert after.recalc_at == LATER and after.review_flag is None
    assert json.loads(after.recalc_before) == {"our_price_cents": 1_000_000, "tier_used": "priceWithTax", "color": "rojo",
                                               "est_margin_pct": before.est_margin_pct, "review_flag": "firme"}
    r = w.run_of(run)
    assert (r.n_rojo, r.n_verde) == (0, 1)
    # un segundo cambio el mismo día: el «antes» sigue siendo lo que midió la noche
    fake.products[0] = product("rv1", CHEAPER)
    await recalc("rv1", now=LATER + timedelta(minutes=30))
    again = snap(sid)
    assert again.color == "amarillo" and json.loads(again.recalc_before)["our_price_cents"] == 1_000_000


async def test_s2_s3_the_table_shows_the_recalculated_state_with_its_mark_and_filters_by_it(fake):
    run, sid = night("rv1")
    night("rv2", run=run)                                                        # otro rojo, sin cambios
    in_vendure(fake, "rv1", FIXED)
    await recalc("rv1")
    c = k.client()
    body = c.get("/api/price-monitor/snapshots").json()
    row = next(i for i in body["items"] if i["product"]["id"] == "rv1")
    assert row["color"] == "verde" and row["our_price_cents"] == FIXED
    assert row["recalc"]["at"] == LATER.isoformat() + "Z"
    assert row["recalc"]["before"]["color"] == "rojo" and row["recalc"]["before"]["our_price_cents"] == 1_000_000
    assert body["colors"]["verde"] == 1 and body["colors"]["rojo"] == 1
    greens = c.get("/api/price-monitor/snapshots", params={"color": "verde"}).json()["items"]
    reds = c.get("/api/price-monitor/snapshots", params={"color": "rojo"}).json()["items"]
    assert [i["product"]["id"] for i in greens] == ["rv1"] and [i["product"]["id"] for i in reds] == ["rv2"]
    hist = c.get("/api/price-monitor/products/rv1/history").json()["items"]
    assert hist[0]["recalc"]["before"]["color"] == "rojo"                       # la historia conserva el antes


async def test_s4_the_next_night_replaces_the_recalculation(fake):
    run, sid = night("rv1")
    in_vendure(fake, "rv1", FIXED)
    await recalc("rv1")
    tomorrow = w.make_run(started=w.NOW + timedelta(days=1))
    w.add_snap(tomorrow, "rv1", our=FIXED, color="verde", tier="priceWithTax", captured_at=w.NOW + timedelta(days=1))
    body = k.client().get("/api/price-monitor/snapshots").json()
    assert body["run_id"] == tomorrow and body["items"][0]["recalc"] is None
    assert snap(sid).recalc_at == LATER                                          # el de ayer queda como historia


# ─── B: bandeja ───────────────────────────────────────────────────


async def test_b1_a_live_item_gets_the_last_figures_and_a_recalculado_event_with_the_numbers(fake):
    _, sid = night("rv1")
    item_id = w.make_item("rv1", "corrigiendo", hugo_disabled=False)
    in_vendure(fake, "rv1", WORSE)
    await recalc("rv1")
    it = w.item(item_id)
    assert (it.last_snapshot_id, it.last_color, it.last_margin_pct) == (sid, "rojo", snap(sid).est_margin_pct)
    [ev] = item_events(item_id, "recalculado")
    p = json.loads(ev.payload)
    assert p == {"precio_antes": 1_000_000, "precio_despues": WORSE, "color_antes": "rojo", "color_despues": "rojo",
                 "ratio_antes": round(1_000_000 / 783_000, 3), "ratio_despues": round(WORSE / 783_000, 3),
                 "sentido": "empeoro"}
    assert ev.actor == pr.ACTOR_AUTO and ev.snapshot_id == sid


async def test_b2_a_proposed_item_that_stops_being_red_closes_on_the_spot(fake):
    night("rv1")
    item_id = w.make_item("rv1")
    in_vendure(fake, "rv1", CHEAPER)
    await recalc("rv1")
    it = w.item(item_id)
    assert it.state == "cerrado_auto" and it.open_key is None
    [ev] = item_events(item_id, "cerrado_auto")
    assert ev.motive_code == "corregido_en_vendure" and ev.motive_text == "Se corrigió en Vendure: ahora está en amarillo."
    assert json.loads(item_events(item_id, "recalculado")[0].payload)["sentido"] == "mejoro"
    assert fake.mutations == []


async def test_b3_a_kept_item_that_got_10_percent_worse_reopens_without_waiting_for_nights(fake):
    night("rv1")
    item_id = w.make_item("rv1", "mantenido", kept_ratio=round(1_000_000 / 783_000, 3), streak_nights=0)
    in_vendure(fake, "rv1", WORSE)                                               # +20 % sobre la línea base
    await recalc("rv1")
    it = w.item(item_id)
    assert it.state == "propuesto" and it.reopen_count == 1
    [ev] = item_events(item_id, "reabierto")
    assert json.loads(ev.payload)["empeoro_pct"] == pytest.approx(20.0, abs=0.2)


async def test_b3_a_kept_item_that_got_only_a_bit_worse_stays_kept(fake):
    night("rv1")
    item_id = w.make_item("rv1", "mantenido", kept_ratio=round(1_000_000 / 783_000, 3))
    in_vendure(fake, "rv1", 1_050_000)                                           # +5 %
    await recalc("rv1")
    assert w.item(item_id).state == "mantenido" and item_events(item_id, "reabierto") == []


async def test_b4_a_product_without_item_that_turns_red_is_shown_but_no_item_is_opened(fake):
    _, sid = night("rv1", our=FIXED, color="verde", flag=None)
    in_vendure(fake, "rv1", WORSE)
    await recalc("rv1")
    assert snap(sid).color == "rojo" and snap(sid).review_flag == "firme"
    assert w.items() == [] and fake.mutations == []


async def test_b5_the_diagnosis_is_redone_with_the_new_breakdown_within_the_review_read_cap(fake):
    raw = engine_variant(1)
    variant = {"id": raw["variant"], "name": "", "sku": "", "priceWithTax": raw["priceWithTax"], "currencyCode": "ARS",
               "customFields": raw["customFields"], "bulkPriceTiers": raw["tiers"]}
    fake.breakdowns[raw["variant"]] = raw["breakdowns"]
    mapped = VendureClient._map_priced_variants([variant])
    our = semaforo.pick_our_price(mapped, 0)
    median = int(our.price_cents * 0.9)                                         # sigue rojo con el precio nuevo
    run = w.make_run()
    sid = w.add_snap(run, "rv1", our=our.price_cents + 100_000, prices=(median,) * 3, tier=our.tier_used,
                     variant=raw["variant"], flag="firme")
    item_id = w.make_item("rv1", diag_status="pendiente", probable_cause=None)
    fake.products.append({**product("rv1", 0), "variantList": {"items": [variant], "totalItems": 1}})
    out = await recalc("rv1")
    it = w.item(item_id)
    assert out["recalculado"] == 1 and out["diagnosticos"] == 1
    assert it.state == "propuesto" and it.diag_status == "completo" and it.probable_cause is not None
    assert it.our_price_cents == our.price_cents == snap(sid).our_price_cents  # cifras firmes del diagnóstico nuevo
    assert json.loads(it.diag)["p_cents"] == our.price_cents
    assert fake.count("TierBreakdowns") == 1 and fake.count("PricedVariant") == 0   # la variante ya vino con el producto
    used = daily_budget.used_today("vendure_review_requests", day=daily_budget.today_art(LATER))
    assert used >= 2                                                             # cuenta en el cupo de la bandeja


async def test_b5_without_review_reads_left_the_diagnosis_waits(fake):
    from app.pricing import diagnosis_source as ds

    night("rv1")
    item_id = w.make_item("rv1", diag_status="pendiente")
    in_vendure(fake, "rv1", WORSE)
    daily_budget.reserve_up_to(ds.RequestBudget.KEY, ds.MAX_VENDURE_CALLS, ds.MAX_VENDURE_CALLS)  # hoy (UTC) ...
    for _ in range(ds.MAX_VENDURE_CALLS):                                        # ... y el día ART del cambio, lleno
        daily_budget.reserve(ds.RequestBudget.KEY, ds.MAX_VENDURE_CALLS, day=daily_budget.today_art(LATER))
    out = await recalc("rv1")
    assert out.get("diagnosticos", 0) == 0 and w.item(item_id).diag_status == "pendiente"
    assert fake.count("TierBreakdowns") == 0


async def test_b6_an_item_with_a_write_in_flight_is_not_touched(fake):
    _, sid = night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    with Session(engine) as s:
        s.add(PriceReviewEvent(item_id=item_id, product_id="rv1", actor=k.PAO, kind="intent", at=w.NOW,
                               enabled_before=False, enabled_after=True))
        s.commit()
    before = w.item(item_id).model_dump()
    in_vendure(fake, "rv1", FIXED, enabled=False)
    await recalc("rv1")
    assert snap(sid).color == "verde"                                            # el semáforo sí
    assert w.item(item_id).model_dump() == before and item_events(item_id, "recalculado") == []
    assert fake.mutations == []


# ─── A: reactivación automática ───────────────────────────────────


async def test_a3_what_hugo_turned_off_and_is_no_longer_firm_red_is_turned_back_on_by_the_protocol(fake):
    _, sid = night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    await recalc("rv1")
    it = w.item(item_id)
    assert it.state == "corregido" and it.open_key is None and it.hugo_disabled is False and it.reactivated_at
    assert it.decision == "reactivar_auto" and it.decision_by == pr.ACTOR_AUTO
    assert fake.writes() == [True] and fake.enabled["rv1"] is True
    assert fake.ops().count("ReviewGuard") == 1                                  # las guardas de una reactivación humana
    kinds = k.kinds(item_id)
    assert kinds[-4:] == ["recalculado", "decision_reactivar_auto", "intent", "reactivado"]
    assert {e.actor for e in item_events(item_id) if e.kind != "propuesto"} == {pr.ACTOR_AUTO}
    [row] = [a for a in k.audit("rv1") if a.action == "price_review_enabled"]    # A-6
    assert "hugo:auto" in row.detail and "Hugo lo reactivó solo" in row.detail
    assert snap(sid).product_enabled is True


async def test_a1_from_en_revision_too(fake):
    night("rv1")
    item_id = k.disabled_by_hugo("rv1", "en_revision")
    in_vendure(fake, "rv1", CHEAPER, enabled=False)                             # amarillo firme también reactiva
    await recalc("rv1")
    assert w.item(item_id).state == "corregido" and fake.writes() == [True]


@pytest.mark.parametrize("state,fields", [
    ("despublicado", dict(hugo_disabled=True, closed_at=w.NOW)),                 # decisión explícita de una persona
    ("corregido", dict(hugo_disabled=False, closed_at=w.NOW)),                   # cerrada
    ("corrigiendo", dict(hugo_disabled=False)),                                  # no lo apagó Hugo
    ("buscando_prov", dict(hugo_disabled=True)),
])
async def test_a1_never_reactivates_closed_items_despublicado_or_what_hugo_did_not_turn_off(fake, state, fields):
    night("rv1")
    item_id = w.make_item("rv1", state, **fields)
    in_vendure(fake, "rv1", FIXED, enabled=False)
    await recalc("rv1")
    assert w.item(item_id).state == state and fake.mutations == []


@pytest.mark.parametrize("weak", [
    dict(sellers=("a", "a", "a")),                                               # pocos vendedores
    dict(prices=(900_000, 905_000)),                                             # pocas publicaciones
    dict(sources=("llm", "llm", "llm")),                                         # idénticos que confirmó solo el juez
])
async def test_a2_a_not_red_with_weak_data_does_not_reactivate(fake, weak):
    _, sid = night("rv1", **weak)
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    await recalc("rv1")
    assert snap(sid).color == "verde" and w.item(item_id).state == "corrigiendo" and fake.mutations == []
    assert item_events(item_id, "recalculado")


@pytest.mark.parametrize("guard,code", [
    (dict(approval="pending"), "aprobacion_pending"),
    (dict(approval="rejected"), "aprobacion_rejected"),
    (dict(duplicate=True), "duplicado"),
])
async def test_a4_a_guard_that_stops_it_leaves_the_item_as_it_was_for_manual_review(fake, guard, code):
    night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    fake.approval["rv1"] = guard.get("approval")
    if guard.get("duplicate"):
        with Session(engine) as s:
            s.add(AuditLog(action="duplicate_disabled", source="audit", product_id="rv1", created_at=w.NOW + timedelta(hours=1)))
            s.commit()
    await recalc("rv1")
    it = w.item(item_id)
    assert it.state == "corrigiendo" and it.open_key == "rv1" and it.hugo_disabled is True and fake.writes() == []
    [ev] = item_events(item_id, "reactivar_a_mano")
    assert ev.motive_code == code and ev.actor == pr.ACTOR_AUTO
    data = price_review_digest.collect(Session(engine), LATER)
    assert [(e.kind, i.id) for e, i in data.attention] == [("reactivar_a_mano", item_id)]


async def test_a4_if_someone_already_turned_it_on_nothing_is_written(fake):
    night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", FIXED, enabled=True)                                 # otra persona lo prendió a mano
    await recalc("rv1")
    assert w.item(item_id).state == "corregido" and fake.writes() == [] and item_events(item_id, "ya_activo")


async def test_a5_with_pm_mode_0_it_shows_but_does_not_reactivate_or_touch_items(fake):
    w.set_setting("pm_mode", 0)
    _, sid = night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    await recalc("rv1")
    assert snap(sid).color == "verde" and snap(sid).recalc_at == LATER
    assert w.item(item_id).state == "corrigiendo" and item_events(item_id, "recalculado") == [] and fake.mutations == []


async def test_a5_daily_cap_of_automatic_reactivations_leaves_the_warning(fake):
    w.set_setting("pm_recalc_reactivations_daily_max", 1)
    run = w.make_run()
    ids = {}
    for pid in ("rv1", "rv2"):
        night(pid, run=run)
        ids[pid] = k.disabled_by_hugo(pid, "corrigiendo")
        in_vendure(fake, pid, FIXED, enabled=False)
    await recalc("rv1", "rv2")
    states = sorted(w.item(i).state for i in ids.values())
    assert states == ["corregido", "corrigiendo"] and fake.writes() == [True]
    waiting = next(i for i in ids.values() if w.item(i).state == "corrigiendo")
    [ev] = item_events(waiting, "aviso_reactivar")
    assert ev.motive_text == "Ya no está en rojo: reactivala con «Ya lo corregí»."
    assert price_recalc.FLAG_NOT_RED in json.loads(w.item(waiting).flags)
    # al volver a rojo, el aviso se va
    fake.products[[p["id"] for p in fake.products].index(w.item(waiting).product_id)] = product(
        w.item(waiting).product_id, WORSE, enabled=False)
    await recalc(w.item(waiting).product_id, now=LATER + timedelta(minutes=10))
    assert price_recalc.FLAG_NOT_RED not in json.loads(w.item(waiting).flags)


async def test_a5_cap_zero_never_reactivates(fake):
    w.set_setting("pm_recalc_reactivations_daily_max", 0)
    night("rv1")
    item_id = k.disabled_by_hugo("rv1", "en_revision")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    await recalc("rv1")
    assert w.item(item_id).state == "en_revision" and fake.mutations == []
    assert item_events(item_id, "aviso_reactivar")[0].motive_text.startswith("Ya no está en rojo: pasala a «Corregir»")


async def test_a6_the_next_mail_lists_it_once_under_reactivados_solos(fake):
    night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    await recalc("rv1")
    with Session(engine, expire_on_commit=False) as s:
        data = price_review_digest.collect(s, LATER)
        assert [i.id for _, i in data.reactivated] == [item_id] and not data.empty
        _, body = price_review_digest.render(data)
        assert "REACTIVADOS SOLOS (1)" in body and "Hugo lo reactivó" in body
        price_review_digest.mark_sent(s, data, LATER)
        s.commit()
        assert price_review_digest.collect(s, LATER).reactivated == []


async def test_a7_the_next_night_follows_the_normal_path_and_needs_the_streak_again(fake):
    run, _ = night("rv1")
    k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    await recalc("rv1")
    tomorrow = w.make_run(started=w.NOW + timedelta(days=1))
    w.add_snap(tomorrow, "rv1", tier="priceWithTax", captured_at=w.NOW + timedelta(days=1),
               data_at=w.NOW + timedelta(days=1))
    await pr.process_run(tomorrow, now=w.NOW + timedelta(days=1, hours=1))
    with Session(engine) as s:
        assert pr.streaks(s, tomorrow, ["rv1"]) == {"rv1": 1}                    # el recálculo cortó la racha
    assert [i.state for i in w.items()] == ["corregido"] and fake.writes() == [True]   # no se apaga solo ni abre ficha


# ─── K: regresión y seguridad ─────────────────────────────────────


def _only_numbers_and_codes(kind: str, value) -> None:
    """Números, códigos cortos y fechas ISO (el «antes» del protocolo de escritura): nada de texto libre."""
    if isinstance(value, dict):
        for v in value.values():
            _only_numbers_and_codes(kind, v)
        return
    assert isinstance(value, (int, float, bool, type(None))) or (
        isinstance(value, str) and value.isascii() and len(value) <= 32 and "<" not in value), (kind, value)


async def test_k3_no_vendure_text_reaches_events_or_the_mail(fake):
    evil = "Taza <script>x</script>\u202egpj.exe \U0001f525"
    night("rv1", name=evil)
    item_id = k.disabled_by_hugo("rv1", "corrigiendo", product_name=evil)
    in_vendure(fake, "rv1", FIXED, enabled=False, name=evil)
    night("rv2")
    other = k.disabled_by_hugo("rv2", "corrigiendo")
    in_vendure(fake, "rv2", FIXED, enabled=False)
    fake.approval["rv2"] = "algo raro de vendure <b>"
    await recalc("rv1", "rv2")
    for ev in w.events():
        if ev.item_id not in (item_id, other):
            continue
        assert "script" not in (ev.payload or "") + (ev.motive_text or "") and "raro" not in (ev.payload or "") + (ev.motive_text or "")
        _only_numbers_and_codes(ev.kind, json.loads(ev.payload) if ev.payload else {})
    _, body = price_review_digest.render(price_review_digest.collect(Session(engine), LATER))
    # El nombre sale como en el resto del mail: saneado con `clean()` (sin bidi ni control); el texto de Vendure de la
    # guarda (approvalStatus) no sale nunca.
    assert price_review_digest.clean(evil) in body and "\u202e" not in body and "raro" not in body


def test_k2_the_recalc_module_never_reaches_a_vendure_write_method():
    """El recálculo solo lee: la reactivación la escribe `price_review_actions` (ver también el test estático K-2 de la
    bandeja, que ahora incluye este módulo)."""
    import ast
    from pathlib import Path

    from tests.test_qa5_bandeja_cero_escrituras import _names_used

    src = Path(price_recalc.__file__).read_text()
    used, imported = _names_used(ast.parse(src))
    banned = {"disable_product", "enable_product", "_set_enabled", "set_enabled", "submit_pro", "submit", "get_status",
              "post", "put", "patch", "delete", "aiosmtplib"}
    assert not (banned & (used | imported)) and "auto_reactivate" in used
