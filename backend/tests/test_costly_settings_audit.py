"""Los settings que gastan plata (cupo de Apify, OTAPI, juez IA) dejan en el
audit log quién los cambió y de cuánto a cuánto; el resto no ensucia el log."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import auth, runtime, security
from app import main as main_mod
from app.config import Settings
from app.db.models import AuditLog, Setting
from app.db.session import engine, init_db

KEYS = ("apify_meli_daily_budget", "apify_1688_daily_budget", "otapi_daily_budget",
        "pm_green_min_pct")


def _wipe():
    with Session(engine) as s:
        for row in s.exec(select(Setting).where(Setting.key.in_(KEYS))).all():  # type: ignore[attr-defined]
            s.delete(row)
        for row in s.exec(select(AuditLog).where(AuditLog.action == "setting_changed")).all():
            s.delete(row)
        s.commit()
    runtime.invalidate()


@pytest.fixture
def client(monkeypatch):
    init_db()
    _wipe()
    s = Settings(vendure_api_url="https://example.invalid/admin-api", dashboard_password="",
                 supabase_url="", supabase_anon_key="")
    for mod in (auth, security, main_mod):
        monkeypatch.setattr(mod, "get_settings", lambda s=s: s)
    monkeypatch.setattr(auth, "session_username", lambda token: "nico@b2box.pro")
    yield TestClient(main_mod.app)
    _wipe()


def _rows():
    with Session(engine) as s:
        return s.exec(select(AuditLog).where(AuditLog.action == "setting_changed")).all()


def test_cambio_del_cupo_de_apify_queda_en_el_audit_log(client):
    assert client.put("/api/settings/apify_meli_daily_budget", json={"value": 50}).status_code == 200
    (row,) = _rows()
    assert row.detail == "nico@b2box.pro cambió apify_meli_daily_budget: 100 → 50"
    assert row.before == '{"apify_meli_daily_budget": 100}'
    assert row.after == '{"apify_meli_daily_budget": 50}'

    assert client.delete("/api/settings/apify_meli_daily_budget").status_code == 200
    assert [r.detail for r in _rows()][-1].endswith("50 → 100")


def test_otapi_tambien_y_sin_cambio_no_registra(client):
    client.put("/api/settings/otapi_daily_budget", json={"value": 120})
    client.put("/api/settings/otapi_daily_budget", json={"value": 120})
    assert len(_rows()) == 1


def test_setting_que_no_gasta_no_se_registra(client):
    assert client.put("/api/settings/pm_green_min_pct", json={"value": 35}).status_code == 200
    assert _rows() == []


def test_cupo_de_apify_tope_500(client):
    assert client.put("/api/settings/apify_meli_daily_budget", json={"value": 501}).status_code == 400
    with pytest.raises(ValueError):
        runtime.set_value("apify_meli_daily_budget", 5000)
    assert _rows() == []


def test_cupo_de_apify_1688_queda_en_el_audit_log_y_tope_500(client):
    assert client.put("/api/settings/apify_1688_daily_budget", json={"value": 20}).status_code == 200
    (row,) = _rows()
    assert row.detail == "nico@b2box.pro cambió apify_1688_daily_budget: 0 → 20"
    assert client.put("/api/settings/apify_1688_daily_budget", json={"value": 501}).status_code == 400
    assert client.delete("/api/settings/apify_1688_daily_budget").status_code == 200
    assert [r.detail for r in _rows()][-1].endswith("20 → 0")
