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
