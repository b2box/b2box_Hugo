"""Settings runtime: valores no finitos y umbrales cruzados se rechazan con un
400 que explica qué choca con qué (security L1)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import auth, runtime, security
from app import main as main_mod
from app.config import Settings
from app.db.models import Setting
from app.db.session import engine, init_db

PM_KEYS = ("pm_green_min_pct", "pm_yellow_min_pct", "pm_image_veto", "pm_image_threshold",
           "pm_image_strong", "pm_name_veto", "pm_name_threshold", "pm_ml_daily_budget")


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    with Session(engine) as s:
        for row in s.exec(select(Setting).where(Setting.key.in_(PM_KEYS))).all():  # type: ignore[attr-defined]
            s.delete(row)
        s.commit()
    runtime.invalidate()
    yield
    with Session(engine) as s:
        for row in s.exec(select(Setting).where(Setting.key.in_(PM_KEYS))).all():  # type: ignore[attr-defined]
            s.delete(row)
        s.commit()
    runtime.invalidate()


@pytest.mark.parametrize("value", ["nan", "NaN", float("nan"), "inf", "-inf", float("inf"), "1e999"])
def test_non_finite_floats_are_rejected(value):
    with pytest.raises(ValueError):
        runtime.set_value("pm_green_min_pct", value)
    assert runtime.get("pm_green_min_pct") == 30.0


@pytest.mark.parametrize("value", ["nan", float("inf"), 1e999])
def test_non_finite_values_for_int_settings_are_a_clean_error(value):
    with pytest.raises(ValueError):
        runtime.set_value("pm_ml_daily_budget", value)


def test_yellow_cannot_go_above_green_and_vice_versa():
    with pytest.raises(ValueError, match="no puede ser mayor"):
        runtime.set_value("pm_yellow_min_pct", 35)
    with pytest.raises(ValueError, match="no puede ser menor"):
        runtime.set_value("pm_green_min_pct", 5)
    runtime.set_value("pm_green_min_pct", 10)   # igual al amarillo: vale
    assert runtime.get("pm_green_min_pct") == 10.0


def test_image_thresholds_stay_ordered():
    with pytest.raises(ValueError):
        runtime.set_value("pm_image_veto", 0.70)       # > umbral 0.65
    with pytest.raises(ValueError):
        runtime.set_value("pm_image_threshold", 0.85)  # > imagen sola 0.80
    with pytest.raises(ValueError):
        runtime.set_value("pm_image_strong", 0.60)     # < umbral 0.65
    with pytest.raises(ValueError):
        runtime.set_value("pm_name_veto", 0.70)        # > nombre 0.60
    runtime.set_value("pm_image_threshold", 0.70)
    assert runtime.get("pm_image_threshold") == 0.70


def test_reset_that_would_cross_the_thresholds_is_rejected():
    runtime.set_value("pm_yellow_min_pct", 5)
    runtime.set_value("pm_green_min_pct", 8)   # verde override por debajo del amarillo default (10)
    with pytest.raises(ValueError):
        runtime.reset_to_default("pm_yellow_min_pct")


def test_api_answers_400_with_the_reason(monkeypatch):
    s = Settings(vendure_api_url="https://example.invalid/admin-api", dashboard_password="",
                 supabase_url="", supabase_anon_key="")
    for mod in (auth, security, main_mod):
        monkeypatch.setattr(mod, "get_settings", lambda s=s: s)
    client = TestClient(main_mod.app)
    for value in ("nan", "1e999"):
        r = client.put("/api/settings/pm_green_min_pct", json={"value": value})
        assert r.status_code == 400, r.text
    r = client.put("/api/settings/pm_yellow_min_pct", json={"value": 50})
    assert r.status_code == 400 and "Verde desde" in r.json()["detail"]
