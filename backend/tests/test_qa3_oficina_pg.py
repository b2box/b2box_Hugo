"""QA3: el lado Hugo del buscador de la oficina sobre Postgres 16 descartable (se salta sola sin HUGO_TEST_PG_URL).

SQLite serializa las escrituras y acepta casi cualquier texto: acá se prueba lo que solo Postgres cumple o rechaza: el índice
único bajo carrera de verdad (varios hilos, conexiones distintas), NUL y sustitutos sueltos, emoji de 4 bytes y texto de
derecha a izquierda, y el ciclo completo POST → cola → fresco.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import main as main_mod  # noqa: E402
from app.api import oficina_routes  # noqa: E402
from app.clock import utcnow  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.models import MarketPriceSnapshot, MlWebResult  # noqa: E402
from app.pricing import oficina_ml  # noqa: E402
from tests.test_oficina_api import H, KEY, _card, _post, _res  # noqa: E402
from tests.test_oficina_pg import PG_URL, _iso, pg, pg_base  # noqa: E402,F401
from tests.test_price_monitor_routes import _use_settings  # noqa: E402

pytestmark = pytest.mark.skipif(not PG_URL, reason="falta HUGO_TEST_PG_URL (Postgres descartable)")


@pytest.fixture
def pgapi(pg, monkeypatch):
    _use_settings(monkeypatch)
    settings = Settings(vendure_api_url="https://example.invalid/admin-api", oficina_search_key=KEY)
    monkeypatch.setattr(oficina_ml, "get_settings", lambda: settings)
    oficina_routes.reset_limits()
    with Session(pg) as s:                                   # pg_base ya trae los productos 11 y 12
        for pid in range(20, 60):
            s.add(MarketPriceSnapshot(run_id=1, product_id=str(pid), ml_status="no_data", product_name=f"Organizador cocina {pid}",
                                      product_enabled=True))
        s.commit()
    yield TestClient(main_mod.app), pg
    oficina_routes.reset_limits()


def _count(pg) -> int:
    with Session(pg) as s:
        return len(s.exec(select(MlWebResult)).all())


def test_eight_connections_posting_the_same_batch_store_it_once(pgapi):
    client, pg = pgapi
    batch = [_res(str(pid), fetched_at=_iso(-timedelta(minutes=5, seconds=pid))) for pid in range(20, 60)]
    for _round in range(3):
        oficina_routes.reset_limits()
        with pg.begin() as c:
            c.execute(text("DELETE FROM ml_web_result"))
        out, errors, barrier = [None] * 8, [], threading.Barrier(8)

        def go(i: int, out=out, errors=errors, barrier=barrier) -> None:
            try:
                barrier.wait(timeout=10)
                out[i] = _post(TestClient(main_mod.app), batch).json()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        assert errors == []
        assert sum(o["stored"] for o in out) == 40 and sum(o["stored"] + o["duplicates"] for o in out) == 8 * 40
        assert _count(pg) == 40


def test_overlapping_batches_from_two_runners_leave_no_gaps_and_no_duplicates(pgapi):
    client, pg = pgapi
    stamp = {pid: _iso(-timedelta(minutes=10, seconds=pid)) for pid in range(20, 60)}
    a = [_res(str(p), fetched_at=stamp[p]) for p in range(20, 45)]
    b = [_res(str(p), fetched_at=stamp[p]) for p in range(35, 60)]
    out, barrier = [None, None], threading.Barrier(2)

    def go(i: int, batch: list) -> None:
        barrier.wait(timeout=10)
        out[i] = _post(TestClient(main_mod.app), batch).json()

    threads = [threading.Thread(target=go, args=(0, a)), threading.Thread(target=go, args=(1, b))]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert out[0]["stored"] + out[1]["stored"] == 40 and out[0]["duplicates"] + out[1]["duplicates"] == 10
    assert _count(pg) == 40


def test_hostile_text_goes_through_http_into_postgres_clean(pgapi):
    client, pg = pgapi
    evil = "Hola\\u0000 \\ud800 mundo \U0001F600 \\u202eevil\\u2066 \\u200b {x} fin"
    body = ('{"results": [{"product_id": "20", "query": "q\\u0000uery \\ud800", "fetched_at": "%s", "status": "ok", "reason": "%s", '
            '"candidates": [{"id": "MLA5", "name": "%s", "brand": "%s", "seller": "%s", "price_cents": 2500}]}]}'
            % (_iso(-timedelta(minutes=2)), evil, evil, evil, evil))
    r = client.post("/api/oficina/ml-results", content=body, headers={**H, "content-type": "application/json"})
    assert r.status_code == 200 and r.json()["stored"] == 1, r.text
    with Session(pg) as s:
        row = s.exec(select(MlWebResult)).one()
    cand = json.loads(row.candidates)[0]
    assert row.query == "query" and cand["name"] == "Hola mundo \U0001F600 evil fin"


def test_a_near_limit_body_is_stored_and_history_is_bounded_on_postgres(pgapi):
    client, pg = pgapi
    cards = [_card(f"MLA{i}", name="x" * 200, seller="y" * 60, brand="z" * 40) for i in range(1, 9)]
    for k in range(7):
        r = _post(client, [_res("20", cards, fetched_at=_iso(-timedelta(hours=k + 1)))])
        assert r.status_code == 200 and r.json()["stored"] == 1
    assert _count(pg) == 5


def test_post_then_queue_then_fresh_on_postgres(pgapi):
    client, pg = pgapi
    ids_before = [i["product_id"] for i in client.get("/api/oficina/ml-queue", params={"limit": 500}, headers=H).json()["items"]]
    assert "20" in ids_before and ids_before[:2] == ["11", "12"]
    _post(client, [_res("20", [_card("MLA20")], fetched_at=_iso(-timedelta(hours=1)))])
    after = [i["product_id"] for i in client.get("/api/oficina/ml-queue", params={"limit": 500}, headers=H).json()["items"]]
    assert "20" not in after and set(ids_before) - set(after) == {"20"}
    assert oficina_ml.load_fresh()["20"].candidates[0].id == "MLA20"
    assert oficina_ml.load_fresh(now=utcnow() + timedelta(days=7, hours=2)) == {}
