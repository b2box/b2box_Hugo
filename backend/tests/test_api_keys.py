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


def _prod_settings(**kw):
    from app.config import Settings

    base = dict(
        vendure_api_url="https://x/admin-api", hugo_env="production",
        supabase_url="https://ref.supabase.co", supabase_anon_key="anon",
        supabase_allowed_emails="tech@b2box.pro",
    )
    base.update(kw)
    return Settings(**base)


def test_production_requires_some_key(monkeypatch):
    from app import main as main_mod

    ok = _prod_settings(hugo_api_keys="luis:xxx")
    monkeypatch.setattr(main_mod, "get_settings", lambda: ok)
    monkeypatch.setattr(security, "get_settings", lambda: ok)
    main_mod._enforce_prod_secrets()  # HUGO_API_KEYS alcanza

    none = _prod_settings(hugo_api_keys="", hugo_api_key="")
    monkeypatch.setattr(main_mod, "get_settings", lambda: none)
    monkeypatch.setattr(security, "get_settings", lambda: none)
    with pytest.raises(RuntimeError, match="HUGO_API_KEYS"):
        main_mod._enforce_prod_secrets()


# ─── Casos borde (QA, auditoría oct-2026) ────────────────────────────────────


def test_duplicate_client_name_keeps_the_last_key_and_warns_without_leaking(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="app.security"):
        assert security.parse_api_keys("luis:primera,luis:segunda") == {"luis": "segunda"}
    assert any("repetido" in r.getMessage() for r in caplog.records)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "primera" not in joined and "segunda" not in joined, "ni la key vieja ni la nueva se loguean"


def test_two_clients_sharing_one_key_resolve_to_the_first_declared(monkeypatch):
    _settings(monkeypatch, keys="luis:same,cloud:same")
    assert security.match_api_key("same") == "luis"


def test_legacy_key_equal_to_a_named_key_is_attributed_to_the_named_client(monkeypatch):
    _settings(monkeypatch, keys="luis:xxx", legacy="xxx")
    assert security.configured_api_keys() == {"luis": "xxx", "legacy": "xxx"}
    assert security.match_api_key("xxx") == "luis"


def test_presented_key_with_surrounding_whitespace_is_rejected(monkeypatch):
    _settings(monkeypatch, keys="luis:xxx")
    assert security.match_api_key(" xxx") is None
    assert security.match_api_key("xxx\n") is None


def test_rejection_log_contains_neither_presented_nor_configured_key(monkeypatch, caplog):
    import logging

    _settings(monkeypatch, keys="luis:secreta-real")
    with caplog.at_level(logging.INFO, logger="app.security"), pytest.raises(HTTPException):
        security.verify_api_key(_request(), x_api_key="intento-malo")
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "secreta-real" not in joined
    assert "intento-malo" not in joined


def test_whitespace_only_env_values_mean_nothing_configured(monkeypatch):
    _settings(monkeypatch, keys="  , ,  ", legacy="   ")
    assert security.configured_api_keys() == {}
    assert security.api_keys_configured() is False


@pytest.mark.parametrize("kw", [
    {"hugo_api_keys": "una-key-pegada-sin-nombre"},
    {"hugo_api_keys": "luis:"},
    {"hugo_api_key": "   "},
])
def test_production_startup_refuses_keys_that_the_parser_discards(monkeypatch, kw):
    """Regresión (QA F1): el chequeo de arranque miraba la truthiness del string
    y el parser descartaba la entrada → Hugo arrancaba en producción abierto."""
    from app import main as main_mod

    s = _prod_settings(**kw)
    monkeypatch.setattr(main_mod, "get_settings", lambda: s)
    monkeypatch.setattr(security, "get_settings", lambda: s)
    assert security.api_keys_configured() is False, "precondición: el parser no ve ninguna key"
    with pytest.raises(RuntimeError, match="HUGO_API_KEYS"):
        main_mod._enforce_prod_secrets()


def test_production_rejects_when_all_keys_are_malformed(monkeypatch):
    """La segunda traba: aunque el arranque se saltee, verify_api_key cierra con
    503 en producción si ninguna key parsea. Nunca `return None` (abierto)."""
    from app import main as main_mod

    s = _prod_settings(hugo_api_keys="luis-pegada-sin-dos-puntos, cloud:", hugo_api_key=" ")
    monkeypatch.setattr(main_mod, "get_settings", lambda: s)
    monkeypatch.setattr(security, "get_settings", lambda: s)

    with pytest.raises(RuntimeError, match="HUGO_API_KEYS"):
        main_mod._enforce_prod_secrets()
    for presented in (None, "luis-pegada-sin-dos-puntos", "cloud", ""):
        with pytest.raises(HTTPException) as exc:
            security.verify_api_key(_request(), x_api_key=presented)
        assert exc.value.status_code == 503, "fail-closed: ni abierto ni 401 engañoso"


def test_development_without_keys_stays_open_but_production_does_not(monkeypatch):
    dev = _prod_settings(hugo_env="development")
    monkeypatch.setattr(security, "get_settings", lambda: dev)
    assert security.verify_api_key(_request(), x_api_key=None) is None

    prod = _prod_settings()
    monkeypatch.setattr(security, "get_settings", lambda: prod)
    with pytest.raises(HTTPException) as exc:
        security.verify_api_key(_request(), x_api_key=None)
    assert exc.value.status_code == 503
