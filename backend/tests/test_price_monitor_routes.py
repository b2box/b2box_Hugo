"""Endpoints del semáforo: sesión del dashboard, filtros, paginado, historial
y disparo manual (que no corre de verdad: el job está reemplazado)."""

from __future__ import annotations

import os
import time
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import auth, security  # noqa: E402
from app import main as main_mod  # noqa: E402
from app.api import price_monitor_routes  # noqa: E402
from app.clock import utcnow  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.models import MarketPriceSnapshot, PriceMonitorRun  # noqa: E402
from app.db.session import engine, init_db  # noqa: E402
from app.pricing import price_monitor  # noqa: E402
from app.scheduler import jobs  # noqa: E402


def _use_settings(monkeypatch, **over) -> None:
    base = dict(vendure_api_url="https://example.invalid/admin-api", hugo_env="development",
                dashboard_user="admin", dashboard_password="secreto", dashboard_secret="s" * 32,
                supabase_url="", supabase_anon_key="")
    base.update(over)
    s = Settings(**base)
    for mod in (auth, security, main_mod):
        monkeypatch.setattr(mod, "get_settings", lambda s=s: s)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    init_db()
    with Session(engine) as s:
        for model in (MarketPriceSnapshot, PriceMonitorRun):
            for row in s.exec(select(model)).all():
                s.delete(row)
        s.commit()
    _use_settings(monkeypatch)
    yield


@pytest.fixture
def client() -> TestClient:
    c = TestClient(main_mod.app)  # sin lifespan: ni scheduler ni warm-ups
    c.cookies.set(auth.COOKIE_NAME, auth.issue_session_token("admin"))
    return c


def _seed() -> tuple[int, int]:
    """Dos corridas: la vieja con un snapshot, la última con 30."""
    with Session(engine) as s:
        old = PriceMonitorRun(status="ok", started_at=utcnow() - timedelta(days=1), total_products=1)
        new = PriceMonitorRun(status="ok", total_products=30, n_ok=10)
        s.add(old)
        s.add(new)
        s.commit()
        s.refresh(old)
        s.refresh(new)
        s.add(MarketPriceSnapshot(run_id=old.id, product_id="1", color="rojo", ml_status="ok",
                                  est_margin_pct=5.0, captured_at=utcnow() - timedelta(days=1)))
        colors = ["verde"] * 10 + ["amarillo"] * 5 + ["rojo"] * 3 + ["sin_dato"] * 12
        for i, color in enumerate(colors, 1):
            s.add(MarketPriceSnapshot(
                run_id=new.id, product_id=str(i), color=color,
                ml_status="ok" if color != "sin_dato" else "no_data",
                est_margin_pct=None if color == "sin_dato" else float(i),
                product_name=f"Producto {i}", product_code=f"BX{i:03d}",
            ))
        s.commit()
        return old.id, new.id


def test_endpoints_need_a_dashboard_session():
    anon = TestClient(main_mod.app)
    for path in ("/api/price-monitor/runs", "/api/price-monitor/snapshots",
                 "/api/price-monitor/products/1/history", "/api/price-monitor/summary"):
        assert anon.get(path).status_code == 401, path
    assert anon.post("/api/price-monitor/run").status_code == 401


def test_runs_newest_first(client):
    old_id, new_id = _seed()
    items = client.get("/api/price-monitor/runs").json()["items"]
    assert [r["id"] for r in items] == [new_id, old_id]
    assert items[0]["counts"]["ok"] == 10


def test_snapshots_default_to_the_latest_run_and_paginate(client):
    _, new_id = _seed()
    page0 = client.get("/api/price-monitor/snapshots", params={"page_size": 25}).json()
    assert page0["run_id"] == new_id and page0["total"] == 30 and page0["has_more"] is True
    assert len(page0["items"]) == 25
    assert page0["colors"] == {"verde": 10, "amarillo": 5, "rojo": 3, "sin_dato": 12}
    # Peor margen arriba; los sin dato al final.
    margins = [i["est_margin_pct"] for i in page0["items"] if i["est_margin_pct"] is not None]
    assert margins == sorted(margins)
    page1 = client.get("/api/price-monitor/snapshots", params={"page_size": 25, "page": 1}).json()
    assert len(page1["items"]) == 5 and page1["has_more"] is False


def test_snapshots_filter_by_color_run_and_search(client):
    old_id, _ = _seed()
    rojos = client.get("/api/price-monitor/snapshots", params={"color": "rojo"}).json()
    assert rojos["total"] == 3 and {i["color"] for i in rojos["items"]} == {"rojo"}
    old = client.get("/api/price-monitor/snapshots", params={"run_id": old_id}).json()
    assert old["total"] == 1
    found = client.get("/api/price-monitor/snapshots", params={"q": "bx007"}).json()
    assert [i["product"]["id"] for i in found["items"]] == ["7"]
    # Los comodines de LIKE del usuario se buscan literales.
    assert client.get("/api/price-monitor/snapshots", params={"q": "%"}).json()["total"] == 0


def test_snapshots_reject_unknown_filters(client):
    assert client.get("/api/price-monitor/snapshots", params={"color": "violeta"}).status_code == 400
    assert client.get("/api/price-monitor/snapshots", params={"status": "raro"}).status_code == 400


def test_snapshots_without_runs_is_empty(client):
    body = client.get("/api/price-monitor/snapshots").json()
    assert body["run_id"] is None and body["items"] == []


def test_product_history_is_newest_first(client):
    old_id, new_id = _seed()
    items = client.get("/api/price-monitor/products/1/history").json()["items"]
    assert [i["run_id"] for i in items] == [new_id, old_id]


def test_manual_run_is_scheduled_in_background(client, monkeypatch):
    called = []

    async def fake_job(trigger="cron"):
        called.append(trigger)

    monkeypatch.setattr(jobs, "price_monitor", fake_job)
    resp = client.post("/api/price-monitor/run")
    assert resp.status_code == 202 and resp.json()["mode"] == "sombra"
    # La task corre en el loop del TestClient (otro hilo): se espera un poco.
    for _ in range(100):
        if called:
            break
        time.sleep(0.01)
    assert called == ["manual"]


async def test_manual_run_while_running_is_409(client):
    async with price_monitor.price_monitor_lock:
        resp = client.post("/api/price-monitor/run")
    assert resp.status_code == 409
    assert not price_monitor_routes._background


def test_health_metrics_include_the_monitor_card(client, monkeypatch):
    monkeypatch.setattr(price_monitor, "summary", lambda: {"last_run": None, "mode": 0})
    body = client.get("/api/health-metrics").json()
    assert body["price_monitor"] == {"last_run": None, "mode": 0}
