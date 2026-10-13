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
