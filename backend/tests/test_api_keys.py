"""Una API key por cliente (HUGO_API_KEYS), con HUGO_API_KEY como legacy."""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app import security  # noqa: E402


def _settings(monkeypatch, keys: str = "", legacy: str = "") -> None:
    monkeypatch.setattr(
        security, "get_settings",
        lambda: type("S", (), {"hugo_api_keys": keys, "hugo_api_key": legacy, "trusted_proxy_hops": 1})(),
    )


def _request() -> Request:
    return Request({
        "type": "http", "method": "POST", "path": "/verify", "headers": [],
        "client": ("10.0.0.7", 1234), "scheme": "http", "server": ("hugo", 8000),
        "query_string": b"", "root_path": "",
    })


# ─── parseo ──────────────────────────────────────────────────────────────────


def test_parse_keys_by_client():
    assert security.parse_api_keys("luis:xxx,cloud:yyy, b2box-app : zzz ") == {
        "luis": "xxx", "cloud": "yyy", "b2box-app": "zzz",
    }


@pytest.mark.parametrize("raw", ["", "   ", ",,,", "sin-dos-puntos", "luis:", ":key"])
def test_malformed_entries_are_skipped(raw):
    assert security.parse_api_keys(raw) == {}


def test_malformed_entry_does_not_drop_the_others():
    assert security.parse_api_keys("luis:xxx,basura,cloud:yyy") == {"luis": "xxx", "cloud": "yyy"}


def test_key_may_contain_colons():
    assert security.parse_api_keys("luis:a:b:c") == {"luis": "a:b:c"}


def test_legacy_key_is_a_client_too(monkeypatch):
    _settings(monkeypatch, keys="luis:xxx", legacy="old-shared")
    assert security.configured_api_keys() == {"luis": "xxx", "legacy": "old-shared"}


def test_nothing_configured(monkeypatch):
    _settings(monkeypatch)
    assert security.configured_api_keys() == {}
    assert security.api_keys_configured() is False


# ─── match ───────────────────────────────────────────────────────────────────


def test_match_returns_the_client_name(monkeypatch):
    _settings(monkeypatch, keys="luis:xxx,cloud:yyy")
    assert security.match_api_key("yyy") == "cloud"
    assert security.match_api_key("xxx") == "luis"


def test_match_rejects_unknown_empty_and_prefixes(monkeypatch):
    _settings(monkeypatch, keys="luis:xxx")
    assert security.match_api_key("xx") is None
    assert security.match_api_key("xxxx") is None
    assert security.match_api_key("") is None
    assert security.match_api_key(None) is None


def test_match_uses_constant_time_comparison(monkeypatch):
    calls: list[tuple[bytes, bytes]] = []
    real = security.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(security.hmac, "compare_digest", spy)
    _settings(monkeypatch, keys="luis:xxx,cloud:yyy,app:zzz")
    assert security.match_api_key("xxx") == "luis"
    assert len(calls) == 3, "se comparan TODAS las keys, sin cortar en la primera"


# ─── dependency ──────────────────────────────────────────────────────────────


def test_dependency_accepts_and_records_the_client(monkeypatch):
    _settings(monkeypatch, keys="luis:xxx,cloud:yyy")
    req = _request()
    assert security.verify_api_key(req, x_api_key="yyy") == "cloud"
    assert req.state.api_client == "cloud"


def test_dependency_accepts_legacy_key(monkeypatch):
    _settings(monkeypatch, legacy="old-shared")
    assert security.verify_api_key(_request(), x_api_key="old-shared") == "legacy"


@pytest.mark.parametrize("presented", [None, "", "nope"])
def test_dependency_rejects_with_401(monkeypatch, presented):
    _settings(monkeypatch, keys="luis:xxx")
    with pytest.raises(HTTPException) as exc:
        security.verify_api_key(_request(), x_api_key=presented)
    assert exc.value.status_code == 401


def test_dependency_is_open_without_keys(monkeypatch):
    _settings(monkeypatch)
    assert security.verify_api_key(_request(), x_api_key=None) is None


def test_authenticated_client_is_logged(monkeypatch, caplog):
    import logging

    _settings(monkeypatch, keys="luis:xxx")
    with caplog.at_level(logging.INFO, logger="app.security"):
        security.verify_api_key(_request(), x_api_key="xxx")
    assert any("cliente=luis" in r.getMessage() for r in caplog.records)
    assert not any("xxx" in r.getMessage() for r in caplog.records), "la key nunca se loguea"


def test_production_requires_some_key():
    from app.main import _enforce_prod_secrets
    from app import main as main_mod

    class S:
        hugo_env = "production"
        supabase_url = "https://x"
        supabase_anon_key = "k"
        dashboard_password = ""
        hugo_api_key = ""
        hugo_api_keys = "luis:xxx"

    orig = main_mod.get_settings
    main_mod.get_settings = lambda: S()
    try:
        _enforce_prod_secrets()  # HUGO_API_KEYS alcanza
        S.hugo_api_keys = ""
        with pytest.raises(RuntimeError, match="HUGO_API_KEYS"):
            _enforce_prod_secrets()
    finally:
        main_mod.get_settings = orig
