"""El panel no puede quedar abierto a internet por una env var olvidada.

Regresión de: hugo.b2box.app sirvió el dashboard, /api/debug-config y /docs sin
login porque HUGO_ENV no estaba seteado en Coolify (default "development") y por
lo tanto _enforce_prod_secrets nunca corrió.
"""

import os

import pytest

# app.db.session llama get_settings() al importarse, y Settings exige
# VENDURE_API_URL. Lo seteamos antes de cualquier import de app.* para poder
# importar app.main en los tests de abajo.
os.environ.setdefault("VENDURE_API_URL", "https://example.test/admin-api")

from app import auth, config  # noqa: E402


def _settings(**over):
    """Settings con los campos obligatorios mínimos, sin leer el .env del repo."""
    base = {
        "vendure_api_url": "https://example.test/admin-api",
        "hugo_api_key": "",
        "dashboard_password": "",
        "supabase_url": "",
        "supabase_anon_key": "",
        "_env_file": None,
    }
    base.update(over)
    return config.Settings(**base)


def test_default_env_is_production():
    """Sin HUGO_ENV, el entorno es production → el guardarraíl corre."""
    assert _settings().hugo_env == "production"


@pytest.mark.parametrize(
    "raw",
    ["production.", "production ", "Production", "prod", "", "produccion", "PRODUCTION"],
)
def test_valores_raros_caen_en_production(raw):
    """Un typo en HUGO_ENV tiene que endurecer, no abrir.

    Regresión del incidente: el server tenía "production." (con punto) y el
    chequeo `!= "production"` se salteaba el guardarraíl entero.
    """
    assert _settings(hugo_env=raw).hugo_env == "production"


@pytest.mark.parametrize("raw", ["development", "  development  ", "DEVELOPMENT"])
def test_development_sigue_siendo_development(raw):
    assert _settings(hugo_env=raw).hugo_env == "development"


def test_production_con_punto_aborta_el_arranque(monkeypatch):
    """El valor exacto que tenía el server no debe dejar arrancar sin login."""
    from app import main

    monkeypatch.setattr(main, "get_settings", lambda: _settings(hugo_env="production."))
    with pytest.raises(RuntimeError):
        main._enforce_prod_secrets()


def test_production_sin_login_aborta_el_arranque(monkeypatch):
    from app import main

    monkeypatch.setattr(main, "get_settings", lambda: _settings())
    with pytest.raises(RuntimeError) as exc:
        main._enforce_prod_secrets()
    assert "HUGO_API_KEY" in str(exc.value)
    assert "DASHBOARD_PASSWORD" in str(exc.value)


def test_production_con_supabase_y_api_key_arranca(monkeypatch):
    from app import main

    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: _settings(
            supabase_url="https://ref.supabase.co",
            supabase_anon_key="anon-key",
            hugo_api_key="k" * 43,
        ),
    )
    main._enforce_prod_secrets()  # no levanta


@pytest.mark.parametrize(
    "path",
    ["/", "/api/debug-config", "/docs", "/openapi.json", "/audit-log", "/api/metrics"],
)
def test_rutas_sensibles_exigen_sesion(path):
    """Ninguna de estas está en la allowlist pública del middleware."""
    assert not auth._is_public_path(path)


@pytest.mark.parametrize("path", ["/login", "/health", "/static/app.js", "/verify", "/app/lookup"])
def test_allowlist_publica_sigue_abierta(path):
    """Lo que tiene auth propia (o no es sensible) no debe pedir cookie."""
    assert auth._is_public_path(path)


def test_login_enabled_necesita_supabase_o_password(monkeypatch):
    monkeypatch.setattr(auth, "get_settings", lambda: _settings())
    assert auth.login_enabled() is False

    monkeypatch.setattr(auth, "get_settings", lambda: _settings(dashboard_password="x"))
    assert auth.login_enabled() is True

    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: _settings(supabase_url="https://ref.supabase.co", supabase_anon_key="a"),
    )
    assert auth.login_enabled() is True


def test_docs_apagado_en_production(monkeypatch):
    """/docs y /openapi.json no se publican con HUGO_ENV=production."""
    monkeypatch.setenv("HUGO_ENV", "production")
    monkeypatch.setenv("VENDURE_API_URL", "https://example.test/admin-api")
    config.get_settings.cache_clear()
    import importlib

    from app import main

    try:
        importlib.reload(main)
        assert main.app.docs_url is None
        assert main.app.openapi_url is None
        assert main.app.redoc_url is None
    finally:
        config.get_settings.cache_clear()
