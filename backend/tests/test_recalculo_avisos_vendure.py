"""Avisos de Vendure al guardar (`vendure-hugo-eventos-contrato.md`, lado Hugo): `POST /api/vendure/events`.

Auth propia (X-Hugo-Events-Key, falla cerrado), body estricto, 202 y el producto se recalcula sin esperar el sondeo: lectura
a los ~20 s y relectura a los ~2 min para tramos y variantes que dieron «sin cambio». Durante la noche se guarda y se procesa
al terminar. Mismo mundo que test_recalculo_al_cambiar (Vendure falso, SQLite de la suite, `pm_mode = 1`, `hugo_env` fijo).
"""

from __future__ import annotations

import os
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app.api import vendure_events_routes as routes  # noqa: E402
from app.config import Settings  # noqa: E402
from app.pricing import price_monitor, price_recalc  # noqa: E402
from tests import review_decide as k  # noqa: E402
from tests import review_world as w  # noqa: E402
from tests import test_recalculo_al_cambiar as base  # noqa: E402
from tests.test_recalculo_al_cambiar import FIXED, LATER, in_vendure, night, product, reads, snap  # noqa: E402

KEY = "k3y-de-prueba-0123456789abcdefghijKLMNOP"
fake = base.fake                       # la fixture autouse del módulo de los criterios
_REAL_START = price_recalc._start_notice_worker


@pytest.fixture(autouse=True)
def events(monkeypatch):
    use_key(monkeypatch, KEY)
    routes.reset_limits()
    monkeypatch.setattr(price_recalc, "_notices", {})
    monkeypatch.setattr(price_recalc, "_is_leader", lambda: True)
    monkeypatch.setattr(price_recalc, "_start_notice_worker", lambda: None)      # los tests procesan a mano, con su reloj
    yield
    routes.reset_limits()


def use_key(monkeypatch, key: str) -> None:
    s = Settings(vendure_api_url="https://example.invalid/admin-api", hugo_env="development", vendure_events_key=key)
    monkeypatch.setattr(routes, "get_settings", lambda: s)


def post(body, key: str | None = KEY):
    headers = {} if key is None else {"X-Hugo-Events-Key": key}
    return k.client(None).post("/api/vendure/events", json=body, headers=headers)     # sin cookie del dashboard


def ev(pid: str, kind: str = "product", **extra) -> dict:
    return {"type": kind, "productId": pid, **extra}


# ─── auth y validación ────────────────────────────────────────────


@pytest.mark.parametrize("key", [None, "", "otra-key-0123456789abcdefghijKLMNOPQR"])
def test_a_missing_empty_or_wrong_key_is_401_and_queues_nothing(key):
    r = post({"events": [ev("1")]}, key=key)
    assert r.status_code == 401 and price_recalc._notices == {}


@pytest.mark.parametrize("configured", ["", "corta", "changeme" * 5])
def test_without_a_real_key_configured_it_fails_closed_with_503(monkeypatch, configured):
    use_key(monkeypatch, configured)
    r = post({"events": [ev("1")]}, key=configured or KEY)
    assert r.status_code == 503 and price_recalc._notices == {}


def test_too_many_wrong_keys_lock_the_ip_but_never_the_right_key():
    for _ in range(routes.FAIL_MAX):
        assert post({"events": [ev("1")]}, key="mala").status_code == 401
    assert post({"events": [ev("1")]}, key="mala").status_code == 429
    assert post({"events": [ev("1")]}).status_code == 202


def test_rate_limit_with_the_right_key(monkeypatch):
    monkeypatch.setattr(routes, "RATE_MAX", 3)
    assert [post({"events": [ev("1")]}).status_code for _ in range(4)] == [202, 202, 202, 429]


@pytest.mark.parametrize("body", [
    {"events": [ev("1", extra="x")]},                                            # campo de más
    {"events": [ev("1", "otro")]},                                               # tipo desconocido
    {"events": [{"type": "product"}]},                                           # sin productId
    {"events": [ev("1")], "otro": 1},
    {"evento": []},
    {"events": [ev("1", at="ayer")]},
    {"events": [ev(f"{i}") for i in range(routes.MAX_EVENTS + 1)]},             # lote de 501
    {"events": [ev("abc")]}, {"events": [ev("12a")]}, {"events": [ev("1" * 21)]}, {"events": [ev("-1")]},
    {"events": [ev("1", variantId="x")]}, {"events": [ev("1", variantId=7)]},    # ids no numéricos
])
def test_an_invalid_body_is_422_and_queues_nothing(body):
    r = post(body)
    assert r.status_code == 422 and price_recalc._notices == {}


def test_a_body_that_is_not_json_is_422():
    r = k.client(None).post("/api/vendure/events", content=b"{no", headers={"X-Hugo-Events-Key": KEY,
                                                                                "content-type": "application/json"})
    assert r.status_code == 422 and price_recalc._notices == {}


# ─── el recálculo ─────────────────────────────────────────────────


async def test_a_valid_notice_recalculates_the_product_without_waiting_for_the_polling(monkeypatch, fake):
    """El aviso (fuera de la sesión del dashboard) anota el producto una vez y se lee a los ~20 s: ni refresh ni vigía."""
    monkeypatch.setattr(price_recalc, "utcnow", lambda: LATER)                  # el mundo de prueba vive en LATER
    _, sid = night("7")
    in_vendure(fake, "7", FIXED)
    r = post({"events": [ev("7"), ev("7", "variant", variantId="101", at="2026-10-30T12:00:00Z")]})
    assert r.status_code == 202 and r.json() == {"queued": 1}
    notice = price_recalc._notices["7"]
    assert notice.recheck is True                                                # una variante: se relee si no cambió
    sent = notice.due - price_recalc.NOTICE_READ_AFTER
    assert await price_recalc.process_notices(now=sent + timedelta(seconds=10)) == {} and reads(fake) == 0
    out = await price_recalc.process_notices(now=sent + timedelta(seconds=20))
    assert out == {"recalculado": 1} and snap(sid).our_price_cents == FIXED and price_recalc._pending == set()


async def test_a_tier_notice_that_read_no_change_is_read_again_two_minutes_later(fake):
    """Vendure recalcula el precio de venta después de guardar el tramo: la primera lectura puede llegar antes."""
    _, sid = night("rv1")
    in_vendure(fake, "rv1", 1_000_000)                                           # todavía el precio de antes
    price_recalc.notify({"rv1": True}, now=LATER)
    assert await price_recalc.process_notices(now=LATER + timedelta(seconds=20)) == {"sin_cambio": 1}
    fake.products[0] = product("rv1", FIXED)                                     # Vendure terminó de recalcular
    assert await price_recalc.process_notices(now=LATER + timedelta(minutes=1)) == {}
    out = await price_recalc.process_notices(now=LATER + timedelta(seconds=20, minutes=2))
    assert out["recalculado"] == 1 and snap(sid).our_price_cents == FIXED
    assert price_recalc._notices == {}                                           # se relee una sola vez


async def test_a_product_notice_with_no_change_is_not_read_again(fake):
    night("rv1")
    in_vendure(fake, "rv1", 1_000_000)
    price_recalc.notify({"rv1": False}, now=LATER)
    assert await price_recalc.process_notices(now=LATER + timedelta(seconds=20)) == {"sin_cambio": 1}
    assert price_recalc._notices == {} and reads(fake) == 1


async def test_a_notice_during_the_night_is_processed_when_it_ends(fake):
    _, sid = night("rv1")
    in_vendure(fake, "rv1", FIXED)
    price_recalc.notify({"rv1": False}, now=LATER)
    async with price_monitor.price_monitor_lock:
        assert await price_recalc.process_notices(now=LATER + timedelta(seconds=20)) == {"corrida": 1}
        assert await price_recalc.run_pending(now=LATER + timedelta(minutes=1)) == {"corrida": 1}
    assert reads(fake) == 0 and price_recalc._pending == {"rv1"}
    await price_recalc.run_pending(now=LATER + timedelta(minutes=5))             # la noche terminó: el refresh lo toma
    assert snap(sid).our_price_cents == FIXED


async def test_a_notice_follows_the_same_caps_and_ignores_what_hugo_does_not_measure(fake):
    w.set_setting("pm_recalc_daily_max", 1)
    run = w.make_run()
    for pid in ("rv1", "rv2"):
        night(pid, run=run)
        in_vendure(fake, pid, FIXED)
    price_recalc.notify({"rv1": False, "rv2": False, "999": False}, now=LATER)
    out = await price_recalc.process_notices(now=LATER + timedelta(seconds=20))
    assert out == {"recalculado": 1, "tope": 1, "sin_snapshot": 1} and reads(fake) == 1


async def test_the_worker_processes_a_notice_by_itself(monkeypatch, fake):
    """Con el worker de verdad: el aviso vence y se procesa solo, sin que nadie llame a nada. (Se avisa desde el loop del
    test: el TestClient corre la app en un loop propio que se cierra con el pedido; en producción el loop es uno solo.)"""
    monkeypatch.setattr(price_recalc, "_start_notice_worker", _REAL_START)
    monkeypatch.setattr(price_recalc, "NOTICE_READ_AFTER", timedelta(0))
    monkeypatch.setattr(price_recalc, "utcnow", lambda: LATER)
    _, sid = night("7")
    in_vendure(fake, "7", FIXED)
    assert price_recalc.notify({"7": False}) == 1
    await price_recalc._notice_task
    assert snap(sid).our_price_cents == FIXED and price_recalc._notices == {}


def test_a_non_leader_does_not_keep_notices(monkeypatch):
    monkeypatch.setattr(price_recalc, "_is_leader", lambda: False)
    r = post({"events": [ev("1")]})
    assert r.status_code == 202 and r.json() == {"queued": 0} and price_recalc._notices == {}
