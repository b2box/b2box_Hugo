"""QA independiente (criterio 3, parte final): el tope de 150 consultas a Vendure por noche, contado en REQUESTS HTTP reales."""

from __future__ import annotations

import os
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app.db.models import PriceReviewItem  # noqa: E402
from app.pricing import price_review as pr  # noqa: E402
from app.vendure import client as vendure_client  # noqa: E402
from tests import qa5_bandeja as q  # noqa: E402
from tests.qa5_bandeja import NOW, VendureWorld, healthy, mk_snap  # noqa: E402

N = 100


@pytest.fixture
def stack(monkeypatch):
    q.reset_db()
    q.setting("pm_mode", 1)
    world = VendureWorld()
    world.spy = q.install_spy(monkeypatch, world)

    async def no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(vendure_client.asyncio, "sleep", no_sleep)       # los reintentos del cliente no esperan
    return world


def seed(world: VendureWorld, n: int = N) -> int:
    """n productos rojos firmes dos noches seguidas. Devuelve el id de la corrida de hoy."""
    prev, today = q.new_run(NOW - timedelta(days=1)), q.new_run(NOW)
    for i in range(n):
        sc = healthy(str(i), box=q.BIG_BOX, cbm="0.024", kg="6.0")
        sc.median = q.median_for(healthy("0").our_price, 12)
        world.add(sc)
        for rid, at in ((prev, NOW - timedelta(days=1)), (today, NOW)):
            q.save(mk_snap(rid, str(i), our=sc.our_price, listings=((sc.median, "clip", "a"), (sc.median + 1000, "clip", "b"),
                                                                   (sc.median - 1000, "clip", "c")),
                           variant=sc.variant, tier=f"tier:min_qty={sc.used_min_q}", data_at=at, captured_at=at,
                           flag="firme" if rid == prev else None, specs=sc.specs()))
    return today


def non_listing(world: VendureWorld) -> list[dict]:
    return [r for r in world.log if r["op"] != "Products"]


async def test_the_nightly_read_cap_is_150_http_requests_when_everything_works(stack):
    world = stack
    today = seed(world)
    summary = await pr.process_run(today, now=NOW + timedelta(hours=1))        # sin catálogo: lee también la variante
    assert summary["nuevas"] == N and summary["tope_alcanzado"] is True
    assert summary["consultas_vendure"] <= 150
    assert len(non_listing(world)) <= 150, len(non_listing(world))
    assert world.writes() == []
    # todos tienen ficha; los que se quedaron sin lecturas quedan marcados como pendientes y se reintentan mañana
    items = q.rows(PriceReviewItem)
    assert len(items) == N
    pending = [i for i in items if i.diag_status in ("pendiente", "parcial")]
    done = [i for i in items if i.diag_status == "completo"]
    assert len(done) >= 40 and len(done) + len(pending) == N and pending
    assert all("diag_pendiente" in (i.flags or "") for i in pending)


async def test_the_nightly_cap_with_the_catalog_in_hand_reads_two_per_product(stack):
    world = stack
    today = seed(world)
    from app.vendure.client import VendureClient

    catalog = {p.id: p for p in await VendureClient().fetch_all_products_priced()}
    before = len(non_listing(world))
    summary = await pr.process_run(today, now=NOW + timedelta(hours=1), catalog=catalog)
    assert len(non_listing(world)) - before <= 150
    assert summary["consultas_vendure"] <= 150
    done = [i for i in q.rows(PriceReviewItem) if i.diag_status == "completo"]
    assert 70 <= len(done) <= 74          # (150 - 2 del canal) / 2 por producto


async def test_the_next_night_picks_up_the_pending_ones_first_and_stays_within_the_cap(stack):
    world = stack
    today = seed(world)
    await pr.process_run(today, now=NOW + timedelta(hours=1))
    pending_before = [i.id for i in q.rows(PriceReviewItem) if i.diag_status != "completo"]
    assert pending_before
    tomorrow = q.new_run(NOW + timedelta(days=1))
    for i in range(N):
        sc = world.by_product[str(i)]
        q.save(mk_snap(tomorrow, str(i), our=sc.our_price, listings=((sc.median, "clip", "a"), (sc.median + 1000, "clip", "b"),
                                                                     (sc.median - 1000, "clip", "c")),
                       variant=sc.variant, tier=f"tier:min_qty={sc.used_min_q}", data_at=NOW + timedelta(days=1),
                       captured_at=NOW + timedelta(days=1), specs=sc.specs()))
    n0 = len(non_listing(world))
    await pr.process_run(tomorrow, now=NOW + timedelta(days=1, hours=1))
    assert len(non_listing(world)) - n0 <= 150
    done_after = [i for i in q.rows(PriceReviewItem) if i.diag_status == "completo"]
    assert len(done_after) > N - len(pending_before)             # avanzó: completó parte de los pendientes


@pytest.mark.xfail(strict=True, reason="BUG QA (medio): el tope de 150 cuenta consultas LÓGICAS; el cliente de Vendure reintenta cada "
                                       "consulta fallida 3 veces (max_attempts=3, client.py:920), así que con Vendure fallando la "
                                       "noche puede mandar ~3x más requests reales (hasta ~450) que el tope declarado")
async def test_the_cap_also_holds_when_vendure_fails_and_the_client_retries(stack):
    world = stack
    today = seed(world)
    for sc in world.scenarios.values():
        world.breakdown_error.add(sc.variant)
    summary = await pr.process_run(today, now=NOW + timedelta(hours=1))
    assert summary["consultas_vendure"] <= 150
    assert len(non_listing(world)) <= 150, f"{len(non_listing(world))} requests reales con tope 150"


async def test_the_pass_over_1500_snapshots_with_400_red_firm_ones_takes_under_a_minute(stack, monkeypatch):
    """Criterio 2-9, medido aparte: sin contar las consultas a Vendure (que son instantáneas en el doble) el tiempo es de la pasada misma."""
    import time

    world = stack
    prev, today = q.new_run(NOW - timedelta(days=1)), q.new_run(NOW)
    for i in range(1500):
        red = i < 400
        for rid, at, flag in ((prev, NOW - timedelta(days=1), "firme" if red else None), (today, NOW, None)):
            q.save(mk_snap(rid, str(i), our=1_000_000 if red else 600_000, data_at=at, captured_at=at, flag=flag, variant=f"v{i}"))
    t0 = time.monotonic()
    summary = await pr.process_run(today, now=NOW + timedelta(hours=1))
    elapsed = time.monotonic() - t0
    assert summary["firmes"] == 400 and summary["nuevas"] == 400
    assert elapsed <= 60, elapsed
    print(f"\nTIEMPO pasada 1500 snapshots / 400 fichas: {elapsed:.1f} s")


@pytest.mark.xfail(strict=True, reason="BUG QA (bajo): un producto local_stock (Vendure devuelve el desglose en ceros) queda con el diagnóstico «pendiente» y se "
                                       "reintenta TODAS las noches hasta 5 veces, gastando consultas del tope (price_review._diag_fields: «completo» = hay desglose)")
async def test_a_local_stock_ficha_is_not_left_pending_and_retried_forever(stack):
    from app.db.models import PriceReviewItem as Item

    world = stack
    prev = q.new_run(NOW - timedelta(days=1))
    today = q.new_run(NOW)
    tiers = [q.Tier(1, None, "0", 1, "0", "0", empty=True)]
    sc = q.Scenario("900", "v900", tiers, 1, 2_000_000, sourcing="local_stock", sale_prices={1: 2_500_000})
    world.add(sc)
    for rid, at, flag in ((prev, NOW - timedelta(days=1), "firme"), (today, NOW, None)):
        q.save(mk_snap(rid, "900", our=sc.our_price, listings=((2_000_000, "clip", "a"), (2_001_000, "clip", "b"), (1_999_000, "clip", "c")),
                       variant="v900", tier="tier:min_qty=1", data_at=at, captured_at=at, flag=flag))
    await pr.process_run(today, now=NOW + timedelta(hours=1))
    [item] = q.rows(Item)
    assert item.diag_status == "completo", (item.diag_status, item.probable_cause)
