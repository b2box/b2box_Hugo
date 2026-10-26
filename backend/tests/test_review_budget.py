"""B3: las lecturas de la bandeja se topean en REQUESTS HTTP reales, con un contador del día ART guardado en la base, y la
pasada tiene un tiempo máximo. Lo que no entra no gasta intentos del diagnóstico."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app.pricing import daily_budget  # noqa: E402
from app.pricing import diagnosis_source as ds  # noqa: E402
from app.pricing import price_review as pr  # noqa: E402
from app.vendure.client import VendureClient  # noqa: E402
from tests import review_world as w  # noqa: E402
from tests.review_fixtures import FakeReviewVendure, engine_variant, install  # noqa: E402

NOW = w.NOW           # 2026-10-14 09:00 UTC = 06:00 ART


@pytest.fixture
def vendure(monkeypatch):
    w.reset_db()
    w.set_setting("pm_mode", 1)
    fake = FakeReviewVendure()
    install(monkeypatch, fake)
    v = engine_variant(0)
    fake.breakdowns["101"] = v["breakdowns"]
    fake.variants["101"] = {"id": "101", "priceWithTax": v["priceWithTax"], "currencyCode": "ARS",
                            "customFields": v["customFields"], "bulkPriceTiers": v["tiers"]}
    return fake


def seed(n: int, prev_at: datetime = NOW - timedelta(days=1), today_at: datetime = NOW) -> int:
    prev, today = w.make_run(started=prev_at), w.make_run(started=today_at)
    for i in range(n):
        w.add_snap(prev, f"p{i}", flag="firme", data_at=prev_at, captured_at=prev_at)
        w.add_snap(today, f"p{i}", data_at=today_at, captured_at=today_at)
    return today


# ─── el día ART ──────────────────────────────────────────────────


def test_the_budget_day_is_the_argentine_one():
    assert daily_budget.today_art(datetime(2026, 10, 14, 2, 59)) == "2026-10-13"       # 23:59 ART del 13
    assert daily_budget.today_art(datetime(2026, 10, 14, 3, 0)) == "2026-10-14"        # 00:00 ART del 14
    assert daily_budget.today_art(datetime(2026, 12, 31, 23, 0)) == "2026-12-31"
    assert daily_budget.today_art(datetime(2027, 1, 1, 2, 59)) == "2026-12-31"
    assert daily_budget.today_utc() != "" and len(daily_budget.today_art()) == 10


def test_the_counter_is_per_day_and_the_old_utc_counters_keep_their_day():
    w.reset_db()
    assert daily_budget.reserve("k", 2, day="2026-10-13") == 1
    assert daily_budget.reserve("k", 2, day="2026-10-13") == 2
    assert daily_budget.reserve("k", 2, day="2026-10-13") is None                       # sin cupo
    assert daily_budget.reserve("k", 2, day="2026-10-14") == 1                          # otro día: empieza de cero
    assert daily_budget.used_today("k", day="2026-10-14") == 1
    # sin `day` sigue siendo el día UTC de siempre (OTAPI, ML, juez)
    assert daily_budget.reserve("otro", 5) == 1 and daily_budget.used_today("otro") == 1


# ─── cada request real cuenta ────────────────────────────────────


async def test_every_http_attempt_counts_including_retries(vendure):
    vendure.http_fail.add("TierBreakdowns")                 # 500: el cliente reintenta 3 veces
    seen: list[int] = []
    client = VendureClient()

    async def hook():
        seen.append(1)

    client.set_request_hook(hook)
    with pytest.raises(Exception):  # noqa: B017
        await client.get_tier_breakdowns("101")
    assert len(seen) == 3 and vendure.count("TierBreakdowns") == 3


async def test_a_refusing_hook_stops_the_request_and_the_retries(vendure):
    client = VendureClient()
    calls = []

    async def hook():
        calls.append(1)
        raise ds.ReadBudgetExhausted("sin cupo")

    client.set_request_hook(hook)
    with pytest.raises(ds.ReadBudgetExhausted):
        await client.get_tier_breakdowns("101")
    assert len(calls) == 1 and vendure.requests == []                                    # no salió ni un request


async def test_the_login_is_a_real_request_and_counts(monkeypatch):
    posts = []

    class FakeAsyncClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            posts.append(1)
            raise ConnectionError("sin red")

    from app.vendure import client as vc

    monkeypatch.setattr(vc.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(vc.httpx, "HTTPError", ConnectionError, raising=False)
    c = VendureClient()
    c._user, c._pass = "u", "p"
    counted = []

    async def hook():
        counted.append(1)

    c.set_request_hook(hook)
    try:
        await c._login()
    except Exception:  # noqa: BLE001
        pass
    assert len(counted) == 1 and len(posts) <= 1


# ─── el tope vive en la base y es del día ─────────────────────────


async def test_a_second_pass_the_same_day_cannot_spend_another_150(vendure):
    today = seed(60)
    s1 = await pr.process_run(today, now=NOW + timedelta(hours=1))
    first = len(vendure.requests)
    assert 0 < first <= 150 and s1["consultas_hoy"] == first
    # una corrida manual más tarde, el mismo día ART: el contador sigue donde quedó
    r3 = w.make_run(started=NOW + timedelta(hours=6))
    for i in range(60):
        w.add_snap(r3, f"p{i}", data_at=NOW + timedelta(hours=6), captured_at=NOW + timedelta(hours=6))
    s2 = await pr.process_run(r3, now=NOW + timedelta(hours=7))
    assert len(vendure.requests) <= 150, len(vendure.requests)
    assert s2["consultas_hoy"] <= 150 and s2["tope_alcanzado"] is True


async def test_the_counter_survives_a_restart_because_it_lives_in_the_database(vendure):
    today = seed(20)
    await pr.process_run(today, now=NOW + timedelta(hours=1))
    used = len(vendure.requests)
    fresh = ds.BudgetedSource(VendureClient(), 150, day=daily_budget.today_art(NOW + timedelta(hours=1)))   # «proceso nuevo»
    assert fresh.budget.used() == used and fresh.calls == 0


async def test_the_counter_rolls_over_at_midnight_art_not_at_midnight_utc(vendure):
    """02:30 UTC y 03:30 UTC del 14 son días ART distintos (23:30 del 13 y 00:30 del 14): el cupo se renueva ahí."""
    late = datetime(2026, 10, 14, 2, 30)
    today = seed(80, prev_at=late - timedelta(days=1), today_at=late)
    await pr.process_run(today, now=late)
    assert daily_budget.used_today(ds.RequestBudget.KEY, day="2026-10-13") > 0
    assert daily_budget.used_today(ds.RequestBudget.KEY, day="2026-10-14") == 0
    # 22:00 UTC del 13 y 01:00 UTC del 14 son el MISMO día ART (19:00 y 22:00 del 13): comparten contador
    assert daily_budget.today_art(datetime(2026, 10, 13, 22, 0)) == daily_budget.today_art(datetime(2026, 10, 14, 1, 0))
    after_midnight = datetime(2026, 10, 14, 3, 30)
    r2 = w.make_run(started=after_midnight)
    for i in range(80):
        w.add_snap(r2, f"p{i}", data_at=after_midnight, captured_at=after_midnight)
    await pr.process_run(r2, now=after_midnight)
    # el contador del día nuevo arrancó de cero (la fila guarda un solo día: el del 13 ya no cuenta)
    assert 0 < daily_budget.used_today(ds.RequestBudget.KEY, day="2026-10-14") <= 150


async def test_with_the_day_already_spent_no_request_goes_out_not_even_the_channel(vendure):
    today = seed(5)
    for _ in range(150):
        assert daily_budget.reserve(ds.RequestBudget.KEY, 150, day=daily_budget.today_art(NOW)) is not None
    summary = await pr.process_run(today, now=NOW + timedelta(hours=1))
    assert vendure.requests == [] and summary["nuevas"] == 5 and summary["tope_alcanzado"] is True
    assert {i.diag_status for i in w.items()} == {"pendiente"}
    assert all(json.loads(i.diag)["attempts"] == 0 for i in w.items())             # no cuenta como intento


# ─── lo que no entró no gasta intentos ────────────────────────────


async def test_nights_without_budget_never_use_up_the_five_attempts(vendure):
    today = seed(3)
    await pr.process_run(today, now=NOW + timedelta(hours=1))                          # noche 1: con cupo, se completa
    assert {i.diag_status for i in w.items()} == {"completo"}
    w.reset_db()
    w.set_setting("pm_mode", 1)
    today = seed(3)
    day = daily_budget.today_art(NOW + timedelta(hours=1))
    for _ in range(150):
        daily_budget.reserve(ds.RequestBudget.KEY, 150, day=day)
    await pr.process_run(today, now=NOW + timedelta(hours=1))
    # 8 noches seguidas sin cupo: sigue pendiente (no «parcial») y con 0 intentos gastados
    for n in range(1, 9):
        at = NOW + timedelta(days=n)
        rid = w.make_run(started=at)
        for i in range(3):
            w.add_snap(rid, f"p{i}", data_at=at, captured_at=at)
        for _ in range(150):
            daily_budget.reserve(ds.RequestBudget.KEY, 150, day=daily_budget.today_art(at + timedelta(hours=1)))
        await pr.process_run(rid, now=at + timedelta(hours=1))
    assert {i.diag_status for i in w.items()} == {"pendiente"}
    assert all(json.loads(i.diag)["attempts"] == 0 for i in w.items())
    # la noche 9 hay cupo: se completan
    at = NOW + timedelta(days=9)
    rid = w.make_run(started=at)
    for i in range(3):
        w.add_snap(rid, f"p{i}", data_at=at, captured_at=at)
    await pr.process_run(rid, now=at + timedelta(hours=1))
    assert {i.diag_status for i in w.items()} == {"completo"}


async def test_a_real_failure_does_count_as_an_attempt(vendure):
    vendure.http_fail.add("TierBreakdowns")
    today = seed(1)
    await pr.process_run(today, now=NOW + timedelta(hours=1))
    [item] = w.items()
    assert item.diag_status == "pendiente" and json.loads(item.diag)["attempts"] == 1


# ─── el tiempo máximo de la pasada ────────────────────────────────


async def test_a_deadline_of_zero_reads_nothing_and_still_opens_every_ficha(vendure):
    today = seed(4)
    summary = await pr.process_run(today, now=NOW + timedelta(hours=1), deadline_s=0)
    assert vendure.requests == [] and summary["nuevas"] == 4 and summary["tiempo_agotado"] is True
    assert {i.diag_status for i in w.items()} == {"pendiente"}
    assert all(json.loads(i.diag)["attempts"] == 0 for i in w.items())


async def test_a_slow_vendure_cannot_hold_the_pass_beyond_the_deadline():
    w.reset_db()
    w.set_setting("pm_mode", 1)
    today = seed(6)

    class Slow:
        async def get_tier_breakdowns(self, v):
            await asyncio.sleep(5)
            return []

        async def get_priced_variant(self, v):
            await asyncio.sleep(5)

        async def get_certification(self, p, c):
            await asyncio.sleep(5)

        async def get_channel_pricing(self):
            return None

    import time

    t0 = time.monotonic()
    summary = await pr.process_run(today, now=NOW + timedelta(hours=1), source=Slow(), deadline_s=0.5)
    assert time.monotonic() - t0 < 4, "la pasada se quedó esperando a Vendure"
    assert summary["nuevas"] == 6 and summary["tiempo_agotado"] is True
    assert all(json.loads(i.diag)["attempts"] == 0 for i in w.items()) and len(w.items()) == 6
