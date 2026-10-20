"""Rol admin del dashboard (DASHBOARD_ADMINS): solo un admin cambia los settings
que gastan plata o mueven el precio estimado, y corre pruebas pagas del
estimado. Sin admins configurados: nadie (fail-closed)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from app import auth, runtime, security
from app import main as main_mod
from app.api import app_routes
from app.config import Settings, get_settings
from app.db.models import AuditLog, EstimateUsage, Setting
from app.db.session import engine, init_db

KEYS = ("apify_1688_daily_budget", "estimate_price_factor_k", "estimate_margin_pct",
        "estimate_tier_qty_1", "vision_rerank_daily_budget", "pm_green_min_pct")


def _wipe():
    with Session(engine) as s:
        s.execute(delete(Setting).where(Setting.key.in_(KEYS)))  # type: ignore[attr-defined]
        s.execute(delete(AuditLog).where(AuditLog.action == "setting_changed"))
        s.execute(delete(EstimateUsage))
        s.commit()
    runtime.invalidate()


def _client(monkeypatch, *, admins: str, user: str | None) -> TestClient:
    s = Settings(vendure_api_url="https://example.invalid/admin-api", dashboard_password="",
                 supabase_url="", supabase_anon_key="", dashboard_admins=admins)
    for mod in (auth, security, main_mod):
        monkeypatch.setattr(mod, "get_settings", lambda s=s: s)
    monkeypatch.setattr(auth, "session_username", lambda token: user)
    return TestClient(main_mod.app)


@pytest.fixture(autouse=True)
def _db():
    init_db()
    _wipe()
    yield
    _wipe()


@pytest.mark.parametrize("key,value", [
    ("apify_1688_daily_budget", 20), ("estimate_price_factor_k", 1.2),
    ("estimate_margin_pct", 20), ("estimate_tier_qty_1", 60),
    ("vision_rerank_daily_budget", 100),
])
def test_admin_cambia_y_queda_auditado(monkeypatch, key, value):
    http = _client(monkeypatch, admins="nico@b2box.pro, otro@b2box.pro", user="Nico@B2box.pro")
    assert http.put(f"/api/settings/{key}", json={"value": value}).status_code == 200
    with Session(engine) as s:
        rows = s.exec(select(AuditLog).where(AuditLog.action == "setting_changed")).all()
    assert len(rows) == 1 and key in rows[0].detail
    assert http.delete(f"/api/settings/{key}").status_code == 200


def test_no_admin_recibe_403_y_no_cambia_nada(monkeypatch):
    http = _client(monkeypatch, admins="nico@b2box.pro", user="pao@b2box.pro")
    before = runtime.get("apify_1688_daily_budget")
    assert http.put("/api/settings/apify_1688_daily_budget", json={"value": 500}).status_code == 403
    assert http.delete("/api/settings/estimate_margin_pct").status_code == 403
    runtime.invalidate()
    assert runtime.get("apify_1688_daily_budget") == before
    # Un setting que no gasta sigue siendo de cualquiera con sesión.
    assert http.put("/api/settings/pm_green_min_pct", json={"value": 35}).status_code == 200


def test_sin_admins_configurados_nadie_puede(monkeypatch):
    http = _client(monkeypatch, admins="", user="nico@b2box.pro")
    assert http.put("/api/settings/estimate_price_factor_k", json={"value": 1.3}).status_code == 403
    assert http.post("/api/estimate-test", json={"url": "https://x.com"}).status_code == 403


def test_estimate_test_solo_admin_y_con_tope_por_usuario(monkeypatch):
    calls = []

    async def fake_run(url, image_url):
        calls.append(url)
        return {"estimate_status": "ok"}
    monkeypatch.setattr(app_routes, "run_estimate_test", fake_run)
    monkeypatch.setattr(get_settings(), "estimate_test_user_daily_limit", 2, raising=False)
    other = _client(monkeypatch, admins="nico@b2box.pro", user="pao@b2box.pro")
    assert other.post("/api/estimate-test", json={"url": "https://a.com"}).status_code == 403
    http = _client(monkeypatch, admins="nico@b2box.pro", user="nico@b2box.pro")
    for _ in range(2):
        assert http.post("/api/estimate-test", json={"url": "https://a.com"}).status_code == 200
    r = http.post("/api/estimate-test", json={"url": "https://a.com"})
    assert r.status_code == 429 and "Retry-After" in r.headers
    assert len(calls) == 2
    # El tope del dashboard no toca el cupo de los visitantes.
    with Session(engine) as s:
        scopes = [u.scope for u in s.exec(select(EstimateUsage)).all()]
    assert scopes and all(sc.startswith("dash:") for sc in scopes)
