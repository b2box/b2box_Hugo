"""Tests del login vía Supabase Auth (Cloud_B2BOX). httpx mockeado — no red."""

import pytest

from app import auth


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """Reemplaza httpx.AsyncClient: devuelve una respuesta fija."""
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return self._resp


def _patch(monkeypatch, settings_over, resp):
    from app.config import Settings, get_settings

    base = {
        "vendure_api_url": "https://x/admin-api",
        "supabase_url": "https://ref.supabase.co",
        "supabase_anon_key": "anon",
        "supabase_allowed_emails": "",
    }
    base.update(settings_over)
    monkeypatch.setattr(auth, "get_settings", lambda: Settings(**base))

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(resp))


def test_enabled_when_url_and_key(monkeypatch):
    _patch(monkeypatch, {}, _FakeResp(200, {}))
    assert auth.supabase_enabled() is True
    assert auth.login_enabled() is True


@pytest.mark.asyncio
async def test_login_ok(monkeypatch):
    resp = _FakeResp(200, {"access_token": "jwt", "user": {"email": "gabriel@b2box.pro"}})
    _patch(monkeypatch, {}, resp)
    ok, email = await auth.supabase_login("gabriel@b2box.pro", "realpass")
    assert ok is True
    assert email == "gabriel@b2box.pro"


@pytest.mark.asyncio
async def test_login_bad_credentials(monkeypatch):
    _patch(monkeypatch, {}, _FakeResp(400, {"error": "invalid_grant"}))
    ok, email = await auth.supabase_login("x@b2box.pro", "wrong")
    assert ok is False
    assert email is None


@pytest.mark.asyncio
async def test_allowlist_blocks_outsider(monkeypatch):
    resp = _FakeResp(200, {"user": {"email": "intruso@gmail.com"}})
    _patch(monkeypatch, {"supabase_allowed_emails": "gabriel@b2box.pro, tech@b2box.pro"}, resp)
    ok, email = await auth.supabase_login("intruso@gmail.com", "validpass")
    assert ok is False


@pytest.mark.asyncio
async def test_allowlist_allows_listed(monkeypatch):
    resp = _FakeResp(200, {"user": {"email": "tech@b2box.pro"}})
    _patch(monkeypatch, {"supabase_allowed_emails": "gabriel@b2box.pro, tech@b2box.pro"}, resp)
    ok, email = await auth.supabase_login("tech@b2box.pro", "validpass")
    assert ok is True
    assert email == "tech@b2box.pro"


# ─── Allowlist fail-closed en producción ──────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_allowlist_in_production_rejects_everyone(monkeypatch):
    """Antes allowlist vacía = entraban todos. Ahora en producción no entra nadie."""
    resp = _FakeResp(200, {"user": {"email": "tech@b2box.pro"}})
    _patch(monkeypatch, {"hugo_env": "production", "supabase_allowed_emails": ""}, resp)
    assert auth.allowlist_misconfigured() is True
    ok, email = await auth.supabase_login("tech@b2box.pro", "validpass")
    assert ok is False
    assert email is None


@pytest.mark.asyncio
async def test_star_opens_to_every_cloud_user(monkeypatch):
    resp = _FakeResp(200, {"user": {"email": "cualquiera@b2box.pro"}})
    _patch(monkeypatch, {"hugo_env": "production", "supabase_allowed_emails": "*"}, resp)
    assert auth.allowlist_misconfigured() is False
    ok, email = await auth.supabase_login("cualquiera@b2box.pro", "validpass")
    assert ok is True
    assert email == "cualquiera@b2box.pro"


@pytest.mark.asyncio
async def test_empty_allowlist_in_development_stays_open(monkeypatch):
    resp = _FakeResp(200, {"user": {"email": "dev@b2box.pro"}})
    _patch(monkeypatch, {"hugo_env": "development", "supabase_allowed_emails": ""}, resp)
    assert auth.allowlist_misconfigured() is False
    ok, _ = await auth.supabase_login("dev@b2box.pro", "validpass")
    assert ok is True


def test_misconfigured_only_when_supabase_is_the_login_method(monkeypatch):
    _patch(monkeypatch, {"hugo_env": "production", "supabase_url": "", "supabase_anon_key": "",
                         "dashboard_password": "local", "supabase_allowed_emails": ""},
           _FakeResp(200, {}))
    assert auth.allowlist_misconfigured() is False


def test_login_endpoint_returns_403_generic_to_the_client_and_detailed_in_the_log(monkeypatch, caplog):
    """La app no se cae: responde 403. El nombre de la variable va al log del
    servidor, no a quien está probando el login. No llama a Supabase."""
    import logging

    from fastapi.testclient import TestClient

    from app import main as main_mod

    called = {"n": 0}

    class _Boom:
        async def __aenter__(self):
            called["n"] += 1
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _FakeResp(200, {"user": {"email": "tech@b2box.pro"}})

    _patch(monkeypatch, {"hugo_env": "production", "supabase_allowed_emails": ""}, None)
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Boom())

    client = TestClient(main_mod.app)  # sin lifespan: no arranca scheduler ni warm-ups
    with caplog.at_level(logging.ERROR, logger="app.main"):
        resp = client.post("/api/login", json={"username": "tech@b2box.pro", "password": "x"})
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert "SUPABASE_ALLOWED_EMAILS" not in detail, "el nombre de la env var no sale al cliente"
    assert "deshabilitado por configuración" in detail
    assert any("SUPABASE_ALLOWED_EMAILS no configurado" in r.getMessage() for r in caplog.records)
    assert called["n"] == 0, "la contraseña no viaja a Supabase si nadie puede entrar"

    # Otros caminos siguen vivos: la app no entró en restart loop.
    assert client.get("/health").status_code == 200


def test_startup_check_warns_but_does_not_raise(monkeypatch, caplog):
    import logging

    from app import main as main_mod

    _patch(monkeypatch, {"hugo_env": "production", "supabase_allowed_emails": ""}, _FakeResp(200, {}))
    from app import security
    from app.config import Settings
    s = Settings(
        vendure_api_url="https://x/admin-api", hugo_env="production",
        supabase_url="https://ref.supabase.co", supabase_anon_key="anon",
        hugo_api_keys="luis:k-a1b2c3d4e5f6g7h8i9j0a1b2c3d4e5f6g7h8i9j0",
    )
    monkeypatch.setattr(main_mod, "get_settings", lambda: s)
    monkeypatch.setattr(security, "get_settings", lambda: s)
    with caplog.at_level(logging.ERROR, logger="app.main"):
        main_mod._enforce_prod_secrets()  # no levanta
    assert any("SUPABASE_ALLOWED_EMAILS" in r.getMessage() for r in caplog.records)
