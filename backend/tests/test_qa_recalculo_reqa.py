"""Re-QA del recálculo (ajustes 30-oct y avisos de Vendure): piso de precio, doble lectura, política de tramos, receptor
`POST /api/vendure/events` y su contrato con el emisor de Vendure (repo b2box, plugin hugo-events).

Mismo mundo que test_recalculo_al_cambiar (Vendure falso que acepta escrituras, SQLite de la suite, `pm_mode = 1`). Los que
llevan `xfail(strict=True)` son hallazgos abiertos: describen lo que pide el criterio y hoy no pasa.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.api import vendure_events_routes as routes  # noqa: E402
from app.clock import utcnow  # noqa: E402
from app.config import Settings  # noqa: E402
from app.pricing import price_recalc  # noqa: E402
from app.pricing import price_review as pr  # noqa: E402
from tests import review_decide as k  # noqa: E402
from tests import review_world as w  # noqa: E402
from tests import test_recalculo_al_cambiar as base  # noqa: E402
from tests.test_recalculo_al_cambiar import (  # noqa: E402
    FIXED, LATER, WORSE, confirm, in_vendure, item_events, night, product, reads, recalc, snap)

fake = base.fake                     # la fixture autouse del módulo de los criterios (Vendure falso, base limpia, pm_mode 1)
KEY = "qa-0123456789abcdefghijKLMNOPQRSTUV"
_REAL_SLEEP = asyncio.sleep          # review_fixtures anula asyncio.sleep (global) para que los reintentos no esperen
_REAL_START = price_recalc._start_notice_worker


@pytest.fixture(autouse=True)
def events(monkeypatch):
    s = Settings(vendure_api_url="https://example.invalid/admin-api", hugo_env="development", vendure_events_key=KEY)
    monkeypatch.setattr(routes, "get_settings", lambda: s)
    routes.reset_limits()
    monkeypatch.setattr(price_recalc, "_notices", {})
    monkeypatch.setattr(price_recalc, "_watched_at", {})
    monkeypatch.setattr(price_recalc, "_is_leader", lambda: True)
    monkeypatch.setattr(price_recalc, "_start_notice_worker", lambda: None)      # salvo el e2e, se procesa a mano
    yield
    routes.reset_limits()


def tiers(raw: dict, *rows: tuple[int, int]) -> dict:
    """Tramos (cantidad mínima, salePrice SIN IVA) en la única variante del producto crudo."""
    raw["variantList"]["items"][0]["bulkPriceTiers"] = [
        {"id": f"t{i}", "position": i, "enabled": True, "minQuantity": q, "maxQuantity": None, "salePrice": p}
        for i, (q, p) in enumerate(rows)]
    return raw


# ─── piso de precio y doble lectura ───────────────────────────────


@pytest.mark.xfail(strict=True, reason="QA-11: el «sin cambio» que compara también contra la otra política de tramos "
                                       "confunde un cambio de tramos en Vendure con un cambio de configuración y "
                                       "reactiva con el precio viejo")
async def test_qa11_a_pricier_minimum_tier_saved_in_vendure_is_not_taken_for_a_policy_change(fake):
    """Política 0 (tramo mínimo, el default). La noche lo midió rojo sin tramos (tier_used = priceWithTax). Pao «Corregir:
    los tramos». Bajan el precio (verde, recálculo). Después cargan tramos: el de 1 unidad caro (rojo) y el más barato igual
    al precio de recién, así que priceWithTax no se mueve. Con la política vigente el precio SUBIÓ; con la otra, no cambió.
    Hoy da «sin cambio» y Hugo lo prende solo con el precio verde de antes."""
    _, sid = night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    assert (await recalc("rv1"))["recalculado"] == 1                               # verde, tier_used = priceWithTax
    fake.products[0] = tiers(product("rv1", FIXED, enabled=False), (1, round(WORSE / 1.21)), (10, round(FIXED / 1.21)))
    await confirm()
    assert fake.writes() == [] and w.item(item_id).state == "corrigiendo"
    assert snap(sid).color == "rojo"


@pytest.mark.xfail(strict=True, reason="QA-12: el piso del 50 % contra la noche se olvida a la noche siguiente: lo que "
                                       "Hugo dejó «reactivar a mano: precio_bajo» lo prende solo a la mañana")
async def test_qa12_a_price_left_for_a_person_is_not_turned_on_alone_after_the_next_night(fake):
    night("rv1", our=1_000_000)
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", 450_000, enabled=False)                  # 0,5 x ML (pasa el cociente), -55 % contra la noche
    await recalc("rv1")
    await confirm()
    [ev] = item_events(item_id, "reactivar_a_mano")
    assert ev.motive_code == "precio_bajo" and fake.writes() == []
    # La noche siguiente mide ese mismo precio: ahora «el de la noche» es 450.000 y la caída da 0 %.
    night_at = utcnow() + timedelta(hours=1)
    tomorrow = w.make_run(started=night_at)
    w.add_snap(tomorrow, "rv1", our=450_000, color="verde", tier="priceWithTax", captured_at=night_at, data_at=night_at,
               enabled=False)
    await pr.process_run(tomorrow, now=night_at + timedelta(minutes=5))
    await price_recalc.watch_live_items(now=night_at + timedelta(minutes=40))
    assert fake.writes() == [] and w.item(item_id).state == "corrigiendo"


@pytest.mark.parametrize("new,reactivated", [
    (500_000, True),                       # -50 % justo y 0,56 x ML: pasa
    (499_999, False),                      # un centavo más abajo: precio_bajo
])
async def test_qa_a2_the_50_percent_floor_boundary(fake, new, reactivated):
    night("rv1", our=1_000_000)
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", new, enabled=False)
    await recalc("rv1")
    await confirm()
    assert (w.item(item_id).state == "corregido") is reactivated and (fake.writes() == [True]) is reactivated
    assert bool(item_events(item_id, "reactivar_a_mano")) is not reactivated


async def test_qa_a2_the_ratio_floor_against_ml_holds_after_the_night_too(fake):
    """Un precio con un cero de menos (0,1 x ML) que la noche ya midió: el cociente contra ML lo sigue frenando."""
    night("rv1", our=90_000, color="verde", flag=None)
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", 90_000, enabled=False)
    await price_recalc.watch_live_items(now=w.NOW + timedelta(minutes=40))
    assert fake.writes() == [] and item_events(item_id, "reactivar_a_mano")[0].motive_code == "precio_bajo"


async def test_qa_a2_the_price_floor_hold_never_reads_nor_writes_vendure_nor_spends_the_cap(fake):
    w.set_setting("pm_recalc_reactivations_daily_max", 1)
    night("rv1", our=1_000_000)
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", 10_000, enabled=False)
    await recalc("rv1")
    await confirm()
    assert fake.count("ReviewGuard") == 0 and fake.count("GetEnabled") == 0 and fake.mutations == []
    from app.pricing import daily_budget
    assert daily_budget.used_today(price_recalc.REACTIVATIONS_KEY, day=daily_budget.today_art(LATER)) == 0
    await confirm(60)
    await confirm(90)
    assert len(item_events(item_id, "reactivar_a_mano")) == 1                     # un intento por precio


async def test_qa_a3_the_notice_reads_never_count_as_the_second_read(fake):
    """El aviso lee a los 20 s y relee a los 2 min: ninguna de las dos está a 30 min de la que trajo el precio."""
    night("rv1")
    item_id = k.disabled_by_hugo("rv1", "corrigiendo")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    price_recalc.notify({"rv1": True}, now=LATER)
    assert (await price_recalc.process_notices(now=LATER + timedelta(seconds=20)))["recalculado"] == 1
    for minutes in (1, 3, 10, 29):
        price_recalc.notify({"rv1": True}, now=LATER + timedelta(minutes=minutes))
        await price_recalc.process_notices(now=LATER + timedelta(minutes=minutes, seconds=20))
    assert w.item(item_id).state == "corrigiendo" and fake.writes() == []
    await confirm(31)
    assert w.item(item_id).state == "corregido" and fake.writes() == [True]


async def test_qa_a1_en_revision_never_turns_on_and_warns_once(fake):
    night("rv1")
    item_id = k.disabled_by_hugo("rv1", "en_revision")
    in_vendure(fake, "rv1", FIXED, enabled=False)
    await recalc("rv1")
    for minutes in (30, 60, 90):
        await confirm(minutes)
    assert w.item(item_id).state == "en_revision" and fake.mutations == []
    assert len(item_events(item_id, "aviso_reactivar")) == 1


# ─── receptor POST /api/vendure/events ────────────────────────────


def _post(content: bytes | None = None, *, key: str | None = KEY, json_body=None, headers: dict | None = None):
    h = {} if key is None else {"X-Hugo-Events-Key": key}
    h.update(headers or {})
    c = k.client(None)
    if json_body is not None:
        return c.post("/api/vendure/events", json=json_body, headers=h)
    return c.post("/api/vendure/events", content=content, headers={"content-type": "application/json", **h})


def _emitter_batch(n: int) -> bytes:
    """Lo que manda el plugin de Vendure (`postToHugo`): JSON.stringify({events}), `at` con milisegundos de toISOString,
    `variantId` solo si lo hay, ids de hasta 20 dígitos."""
    kinds = ("product", "variant", "tier", "price")
    events = []
    for i in range(n):
        kind = kinds[i % 4]
        ev = {"type": kind, "productId": str(10**19 + i)}
        if kind in ("variant", "tier"):
            ev["variantId"] = str(10**19 + 7 * i)
        ev["at"] = "2026-10-30T12:00:00.123Z"
        events.append(ev)
    return json.dumps({"events": events}, separators=(",", ":")).encode()


def test_qa_contract_a_full_batch_shaped_like_the_emitter_is_accepted():
    raw = _emitter_batch(500)
    assert len(raw) < routes.MAX_BODY_BYTES / 2                                   # 500 avisos con ids de 20 dígitos
    r = _post(raw)
    assert r.status_code == 202 and r.json() == {"queued": 500}
    assert _post(_emitter_batch(501)).status_code == 422                          # el plugin parte en lotes de 500


def test_qa_contract_header_name_is_case_insensitive():
    r = _post(_emitter_batch(1), key=None, headers={"x-hugo-events-key": KEY})
    assert r.status_code == 202


def test_qa_receiver_auth_runs_before_the_body_is_read():
    big = b'{"events":[' + b" " * (routes.MAX_BODY_BYTES + 10) + b"]}"
    assert _post(big, key="mala").status_code == 401                              # no lee el body sin la key
    assert _post(big).status_code == 413


def test_qa_receiver_a_chunked_body_over_the_cap_is_cut():
    def gen():
        yield b'{"events":['
        for _ in range(300):
            yield b" " * 1024
        yield b"]}"

    r = k.client(None).post("/api/vendure/events", content=gen(), headers={"X-Hugo-Events-Key": KEY,
                                                                           "content-type": "application/json"})
    assert r.status_code == 413 and price_recalc._notices == {}


async def test_qa_receiver_the_same_product_many_times_is_read_once(fake):
    night("rv1")
    in_vendure(fake, "rv1", FIXED)
    for second in range(0, 15, 3):                                                # cinco lotes en 15 s
        price_recalc.notify({"rv1": second % 2 == 0}, now=LATER + timedelta(seconds=second))
    assert await price_recalc.process_notices(now=LATER + timedelta(seconds=30)) == {}   # 20 s desde el último
    assert (await price_recalc.process_notices(now=LATER + timedelta(seconds=32)))["recalculado"] == 1
    assert reads(fake) == 1 and price_recalc._notices == {}


async def test_qa_receiver_notices_wait_while_the_watch_is_busy_and_then_run(fake):
    night("rv1")
    in_vendure(fake, "rv1", FIXED)
    price_recalc.notify({"rv1": False}, now=LATER)
    async with price_recalc._busy:
        assert await price_recalc.process_notices(now=LATER + timedelta(seconds=20)) == {"ocupado": 1}
    assert (await price_recalc.process_notices(now=LATER + timedelta(seconds=21)))["recalculado"] == 1


async def test_qa_receiver_unknown_products_cost_no_reads_and_do_not_stay(fake):
    price_recalc.notify({str(10**19 + i): True for i in range(500)}, now=LATER)
    out = await price_recalc.process_notices(now=LATER + timedelta(seconds=20))
    assert out == {"sin_snapshot": 500} and reads(fake) == 0 and price_recalc._notices == {}


@pytest.mark.xfail(strict=True, reason="QA-13: el sondeo vuelve a leer lo que el aviso ya leyó: cada guardado gasta 2 "
                                       "lecturas del cupo diario (3 con la relectura de tramo/variante)")
async def test_qa13_a_change_already_read_by_the_notice_is_not_read_again_by_the_polling(monkeypatch, fake):
    from app.scheduler import jobs
    from app.vendure import catalog

    _, sid = night("rv1")
    in_vendure(fake, "rv1", FIXED)
    price_recalc.notify({"rv1": False}, now=LATER)
    assert (await price_recalc.process_notices(now=LATER + timedelta(seconds=20)))["recalculado"] == 1

    async def polling_sees_it(force=False, full=False):
        catalog._changed.add("rv1")                                                # el updatedAt nuevo del mismo guardado
        return []

    monkeypatch.setattr(catalog, "get_catalog", polling_sees_it)
    monkeypatch.setattr(price_recalc, "utcnow", lambda: LATER + timedelta(minutes=4))
    await jobs.refresh_catalog()
    assert reads(fake) == 1


async def test_qa_e2e_a_notice_over_http_recalculates_within_a_minute_without_polling(monkeypatch, fake):
    """De punta a punta, con el reloj y la espera de verdad (20 s): el POST pasa la auth y el body, el worker arranca solo
    en el loop de la app y el semáforo cambia antes de 1 minuto. Sin refresh_catalog ni vigía."""
    monkeypatch.setattr(asyncio, "sleep", _REAL_SLEEP)
    monkeypatch.setattr(price_recalc, "_start_notice_worker", _REAL_START)
    from app import main as main_mod
    from app.scheduler import jobs

    called = []
    monkeypatch.setattr(jobs, "refresh_catalog", lambda: called.append("refresh"))
    monkeypatch.setattr(price_recalc, "watch_live_items", lambda *a, **kw: called.append("watch"))
    now = utcnow()
    run = w.make_run(started=now - timedelta(hours=2))
    sid = w.add_snap(run, "4242", tier="priceWithTax", flag="firme", captured_at=now - timedelta(hours=2),
                     data_at=now - timedelta(hours=2))
    in_vendure(fake, "4242", FIXED)
    started = time.monotonic()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main_mod.app), base_url="http://hugo") as c:
        r = await c.post("/api/vendure/events", content=_one("4242"),
                         headers={"X-Hugo-Events-Key": KEY, "content-type": "application/json"})
    assert r.status_code == 202 and r.json() == {"queued": 1}
    while snap(sid).recalc_at is None and time.monotonic() - started < 60:
        await _REAL_SLEEP(0.5)
    elapsed = time.monotonic() - started
    assert snap(sid).our_price_cents == FIXED and snap(sid).color == "verde", elapsed
    assert 15 <= elapsed <= 30 and called == [] and reads(fake) == 1
    task = price_recalc._notice_task
    if task is not None:
        await asyncio.wait_for(task, 5)


def _one(pid: str) -> bytes:
    return json.dumps({"events": [{"type": "product", "productId": pid, "at": "2026-10-30T12:00:00.000Z"}]}).encode()



async def test_qa_b6_en_revision_skipped_for_a_write_in_flight_gets_its_warning_once_the_write_ends(fake):
    """La mitad de QA-4 que el dev cambió a `corrigiendo`: en `en_revision` lo que se salteó por una escritura en curso
    tiene que terminar en el aviso «ya no está en rojo» (no en una reactivación)."""
    from sqlmodel import Session

    from app.db.models import PriceReviewEvent
    from app.db.session import engine

    night("rv1")
    item_id = k.disabled_by_hugo("rv1", "en_revision")
    with Session(engine) as s:
        s.add(PriceReviewEvent(item_id=item_id, product_id="rv1", actor=k.PAO, kind="intent", at=LATER,
                               enabled_before=True, enabled_after=False))
        s.commit()
    in_vendure(fake, "rv1", FIXED, enabled=False)
    await recalc("rv1")
    assert item_events(item_id, "aviso_reactivar") == []                          # B-6: no se tocó
    with Session(engine) as s:
        s.add(PriceReviewEvent(item_id=item_id, product_id="rv1", actor=k.PAO, kind="inactivado", at=LATER,
                               enabled_before=True, enabled_after=False))
        s.commit()
    await confirm()
    assert w.item(item_id).state == "en_revision" and fake.mutations == []
    assert len(item_events(item_id, "aviso_reactivar")) == 1


def test_qa_receiver_is_public_only_by_its_key_in_production(monkeypatch):
    """En producción el middleware no pide la cookie para /api/vendure/events, y sin la key es 401 (no un redirect al
    login ni un 403 de CSRF)."""
    from tests.test_price_monitor_routes import _use_settings

    _use_settings(monkeypatch, hugo_env="production", dashboard_secret="s" * 40)
    assert _post(_emitter_batch(1), key=None).status_code == 401
    assert _post(_emitter_batch(1), headers={"Sec-Fetch-Site": "cross-site"}).status_code == 202
