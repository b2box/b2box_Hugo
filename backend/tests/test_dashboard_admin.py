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


def _client(monkeypatch, *, admins: str, user: str | None, secret: str = "k" * 40) -> TestClient:
    s = Settings(vendure_api_url="https://example.invalid/admin-api", dashboard_password="",
                 supabase_url="", supabase_anon_key="", dashboard_admins=admins,
                 dashboard_secret=secret)
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


def test_sin_dashboard_secret_nadie_es_admin(monkeypatch):
    # Clave derivada de la password del login local: alcanza para entrar, no
    # para gastar plata.
    http = _client(monkeypatch, admins="nico@b2box.pro", user="nico@b2box.pro", secret="")
    assert http.put("/api/settings/apify_1688_daily_budget", json={"value": 5}).status_code == 403


# ─── Clave de la cookie de sesión ──────────────────────────────────


def _forge(key: bytes, user: str = "nico@b2box.pro") -> str:
    import base64
    import hashlib
    import hmac
    import json
    import time

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    payload = b64(json.dumps({"u": user, "exp": int(time.time()) + 3600}).encode())
    return payload + "." + b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())


def _auth_settings(monkeypatch, **over):
    base = dict(vendure_api_url="https://example.invalid/admin-api", dashboard_password="",
                dashboard_secret="", supabase_url="https://x.supabase.co", supabase_anon_key="a",
                hugo_env="development")
    base.update(over)
    s = Settings(**base)
    monkeypatch.setattr(auth, "get_settings", lambda: s)
    return s


def test_cookie_firmada_con_la_clave_publica_vieja_se_rechaza(monkeypatch):
    import hashlib

    public = hashlib.sha256(b"hugo-session::").digest()
    for env in ("development", "production"):
        _auth_settings(monkeypatch, hugo_env=env)
        assert auth.session_username(_forge(public)) is None
        with pytest.raises(auth.SessionKeyMissing):
            auth.issue_session_token("nico@b2box.pro")


def test_con_dashboard_secret_la_sesion_anda(monkeypatch):
    _auth_settings(monkeypatch, dashboard_secret="z" * 40, hugo_env="production")
    token = auth.issue_session_token("nico@b2box.pro")
    assert auth.session_username(token) == "nico@b2box.pro"
    assert auth.session_username(_forge(b"z" * 40)) == "nico@b2box.pro"
    assert auth.session_username(_forge(b"otra-clave" * 4)) is None


def test_en_produccion_un_secreto_corto_o_la_password_no_alcanzan(monkeypatch):
    _auth_settings(monkeypatch, dashboard_secret="corto", dashboard_password="pw",
                   hugo_env="production")
    with pytest.raises(auth.SessionKeyMissing):
        auth.issue_session_token("admin")
    # En desarrollo, una password NO vacía todavía sirve para el login local.
    _auth_settings(monkeypatch, dashboard_password="pw", supabase_url="", supabase_anon_key="")
    assert auth.session_username(auth.issue_session_token("admin")) == "admin"


def test_login_sin_clave_responde_503_y_no_tira_la_app(monkeypatch):
    s = _auth_settings(monkeypatch, supabase_url="", supabase_anon_key="",
                       dashboard_password="pw", hugo_env="production")
    for mod in (security, main_mod):
        monkeypatch.setattr(mod, "get_settings", lambda s=s: s)
    http = TestClient(main_mod.app)
    r = http.post("/api/login", json={"username": "admin", "password": "pw"})
    assert r.status_code == 503 and "hugo_session" not in r.cookies
