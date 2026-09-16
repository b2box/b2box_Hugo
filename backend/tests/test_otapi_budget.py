"""El budget diario de OTAPI tiene que contar requests, no snapshots guardados.

Regresión del bug que disparó los mails de "85% de la cuota" mientras el
dashboard de Hugo mostraba 0/120: se contaban filas de PriceHistory, así que
todo request que OTAPI contestaba con error (item delistado, por ejemplo) se
pagaba sin mover el contador y el tope nunca cortaba.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")
os.environ.setdefault("VENDURE_BEARER", "test-token")
# Antes de importar la app: el engine se arma a nivel de módulo.
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".sqlite3")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import pytest  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

from app.db.models import PriceHistory, Setting  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import source_check  # noqa: E402

BUDGET = 1000  # holgado: los tests que no miran el tope no deberían chocarlo


@pytest.fixture(autouse=True)
def _clean_db():
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        for row in s.exec(select(Setting)).all():
            s.delete(row)
        for row in s.exec(select(PriceHistory)).all():
            s.delete(row)
        s.commit()
    yield


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ─── parseo del contador (puro, sin DB) ────────────────────────────────────


def test_counter_reads_todays_value():
    assert source_check._parse_day_counter("2026-09-16:57", "2026-09-16") == 57


def test_counter_from_another_day_starts_over():
    assert source_check._parse_day_counter("2026-09-15:900", "2026-09-16") == 0


@pytest.mark.parametrize("raw", [None, "", "basura", "2026-09-16:", "2026-09-16:x"])
def test_corrupt_counter_falls_back_to_zero(raw):
    # Fail-safe hacia 0: un valor roto re-cuenta desde cero, nunca deja el
    # budget trabado en "agotado" para siempre.
    assert source_check._parse_day_counter(raw, "2026-09-16") == 0


def test_negative_counter_is_clamped():
    assert source_check._parse_day_counter("2026-09-16:-5", "2026-09-16") == 0


# ─── reserva de cupo ───────────────────────────────────────────────────────


def test_each_reservation_increments_the_counter():
    assert source_check._otapi_calls_today() == 0
    assert source_check._reserve_otapi_call(BUDGET) == 1
    assert source_check._reserve_otapi_call(BUDGET) == 2
    assert source_check._otapi_calls_today() == 2


def test_counter_uses_a_single_row():
    for _ in range(5):
        source_check._reserve_otapi_call(BUDGET)
    with Session(engine) as s:
        rows = [r for r in s.exec(select(Setting)).all() if "otapi" in r.key]
    assert len(rows) == 1


def test_counter_resets_when_the_utc_day_changes():
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    with Session(engine) as s:
        s.add(Setting(key=source_check._OTAPI_COUNTER_KEY, value=f"{yesterday}:900"))
        s.commit()
    assert source_check._otapi_calls_today() == 0
    assert source_check._reserve_otapi_call(BUDGET) == 1


def test_failed_requests_count_too():
    """El corazón del bug: sin snapshots, el contador igual tiene que subir."""
    for _ in range(10):
        source_check._reserve_otapi_call(BUDGET)
    with Session(engine) as s:
        assert len(s.exec(select(PriceHistory)).all()) == 0
    assert source_check._otapi_calls_today() == 10


def test_existing_snapshots_are_a_floor_on_deploy_day():
    # El contador nuevo arranca en 0 aunque el día ya venga gastado: los
    # snapshots de hoy son el piso para no correr el budget al desplegar.
    with Session(engine) as s:
        for _ in range(3):
            s.add(PriceHistory(
                product_id="1", source="1688_otapi", price_cents=100, currency="CNY",
            ))
        s.commit()
    assert source_check._otapi_calls_today() == 3
    assert source_check._reserve_otapi_call(BUDGET) == 4
    assert source_check._otapi_calls_today() == 4
    assert source_check._reserve_otapi_call(BUDGET) == 5


def test_snapshot_floor_is_read_once_per_day(monkeypatch):
    """El piso cuesta un COUNT(*); no puede pagarse en cada request."""
    calls = {"n": 0}
    real = source_check._snapshots_today

    def _counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(source_check, "_snapshots_today", _counting)
    for _ in range(25):
        source_check._reserve_otapi_call(BUDGET)
        source_check._otapi_calls_today()
    assert calls["n"] == 1


def test_budget_status_reflects_real_calls():
    for _ in range(4):
        source_check._reserve_otapi_call(BUDGET)
    status = source_check.otapi_budget_status()
    assert status["used"] == 4
    assert status["remaining"] == status["budget"] - 4


# ─── el cupo se agota y no se pasa ─────────────────────────────────────────


def test_reservation_stops_at_the_budget():
    assert [source_check._reserve_otapi_call(3) for _ in range(3)] == [1, 2, 3]
    # Cuarta: sin cupo. Devuelve None y NO incrementa; si incrementara, un loop
    # de reintentos rechazados inflaría el contador solo.
    assert source_check._reserve_otapi_call(3) is None
    assert source_check._otapi_calls_today() == 3
    assert source_check._reserve_otapi_call(3) is None
    assert source_check._otapi_calls_today() == 3


def test_budget_of_zero_blocks_everything():
    assert source_check._reserve_otapi_call(0) is None
    assert source_check._otapi_calls_today() == 0


def test_concurrent_reservations_never_exceed_the_budget():
    """Chequear y después incrementar dejaba pasar de más a los 10 fetchers."""
    import threading

    budget, granted = 20, []
    lock = threading.Lock()

    def _worker():
        for _ in range(10):
            got = source_check._reserve_otapi_call(budget)
            if got is not None:
                with lock:
                    granted.append(got)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(granted) == budget, "se otorgó exactamente el cupo, ni uno más"
    assert sorted(granted) == list(range(1, budget + 1)), "sin números repetidos"
    assert source_check._otapi_calls_today() == budget


def test_reservation_fails_closed_when_the_db_is_down(monkeypatch):
    """Presupuesto desconocido no puede significar 'gastá tranquilo'."""
    def _boom():
        raise RuntimeError("la base no responde")

    monkeypatch.setattr(source_check, "_snapshots_today", _boom)
    assert source_check._reserve_otapi_call(BUDGET) is None


# ─── end-to-end: el fetch que falla igual consume budget ───────────────────


class _FakeResponse:
    """Respuesta de OTAPI para un item delistado: HTTP 200 y ErrorCode != Ok.

    Es el caso que rompía el budget: OTAPI cobra el request, Hugo no guarda
    snapshot y el contador viejo no se movía nunca.
    """

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload, hits):
        self._payload, self._hits = payload, hits

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **kw):
        self._hits.append(kw.get("params", {}).get("itemId"))
        return _FakeResponse(self._payload)


@pytest.mark.anyio
async def test_failed_fetch_still_consumes_budget_and_trips_the_cap(monkeypatch):
    import httpx

    hits: list = []
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **kw: _FakeClient({"ErrorCode": "ItemNotFound"}, hits),
    )
    monkeypatch.setattr(
        source_check, "get_settings",
        lambda: type("S", (), {
            "rapidapi_key": "k", "otapi_1688_host": "h", "otapi_daily_budget": 3,
        })(),
    )
    monkeypatch.setattr(source_check.runtime_settings, "get", lambda *_a, **_k: None)

    fetcher = source_check.Detail1688Fetcher()
    url = "https://detail.1688.com/offer/123456.html"

    for _ in range(3):
        assert await fetcher.fetch_price(url) is None
    assert len(hits) == 3, "los 3 requests se hicieron y se pagaron"
    assert source_check._otapi_calls_today() == 3

    # Cuarto intento: el budget (3) ya está agotado y ni sale a la red.
    assert await fetcher.fetch_price(url) is None
    assert len(hits) == 3, "el budget cortó ANTES del request"
