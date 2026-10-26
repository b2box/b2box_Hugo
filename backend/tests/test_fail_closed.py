"""El panel no puede quedar abierto a internet por una env var olvidada.

Regresión de: hugo.b2box.app sirvió el dashboard, /api/debug-config y /docs sin
login porque HUGO_ENV en Coolify no decía exactamente "production" (tenía
"production.", con punto) y el código lo trataba como development: el default
era "development" y los chequeos comparaban `!= "production"`, así que
_enforce_prod_secrets nunca corrió.
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


def _use(monkeypatch, s):
    """Todos los módulos que leen Settings ven el mismo: main, auth y security."""
    from app import main, security

    for mod in (auth, security, main):
        monkeypatch.setattr(mod, "get_settings", lambda: s)


def test_default_env_is_production(monkeypatch):
    """Sin HUGO_ENV, el entorno es production → el guardarraíl corre."""
    monkeypatch.delenv("HUGO_ENV", raising=False)
    monkeypatch.delenv("hugo_env", raising=False)
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

    _use(monkeypatch, _settings(hugo_env="production."))
    with pytest.raises(RuntimeError):
        main._enforce_prod_secrets()


def test_production_sin_login_aborta_el_arranque(monkeypatch):
    from app import main

    _use(monkeypatch, _settings(hugo_env="production"))
    with pytest.raises(RuntimeError) as exc:
        main._enforce_prod_secrets()
    assert "HUGO_API_KEY" in str(exc.value)
    assert "DASHBOARD_PASSWORD" in str(exc.value)


def test_production_con_supabase_y_api_key_arranca(monkeypatch):
    from app import main

    _use(
        monkeypatch,
        _settings(
            hugo_env="production",
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


# ─── QA: paridad Settings ↔ main._DEV, guardas de producción y /api/debug-config ──
#
# _DEV (main.py) sale de EnvSettings, que lee HUGO_ENV igual que Settings (entorno
# + .env, sin distinguir mayúsculas, mismo validador). Estos tests fijan que /docs
# se publique si y solo si Settings dice development. Corren en un directorio sin
# .env para no depender del .env local del checkout.

import contextlib  # noqa: E402
import importlib  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app import security  # noqa: E402

_GOOD_KEY = "qa-" + "a1b2c3d4e5f6g7h8i9j0k1l2m3"  # >= 24 chars: válida en production
_RAROS = [None, "", "production", "production.", "Production ", "prod", "dev", "develop",
          "development.", "developmentt", "development,production", "'development'", "\"development\""]
_DEVS = ["development", "  Development ", "DEVELOPMENT", "development\n", "\tdevelopment"]


def _set_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


@contextlib.contextmanager
def _main_con_env(**env):
    """app.main recargado con esas env vars; al salir restaura el entorno y recarga."""
    from app import main

    prev = {k: os.environ.get(k) for k in env}
    for k in env:  # se borran primero: el orden de inserción importa (último gana)
        os.environ.pop(k, None)
    for k, v in env.items():
        _set_env(k, v)
    try:
        yield importlib.reload(main)
    finally:
        for k in env:
            os.environ.pop(k, None)
        for k, v in prev.items():
            _set_env(k, v)
        importlib.reload(main)


@pytest.fixture
def sin_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize("raw", _RAROS)
def test_docs_ocultos_si_hugo_env_no_es_development(raw, sin_dotenv):
    with _main_con_env(HUGO_ENV=raw) as main:
        assert main._DEV is False
        assert (main.app.docs_url, main.app.redoc_url, main.app.openapi_url) == (None, None, None)
        c = TestClient(main.app)
        for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
            r = c.get(path)
            assert r.status_code in (401, 404), (path, r.status_code)
            assert "swagger" not in r.text.lower() and '"openapi"' not in r.text


@pytest.mark.parametrize("raw", _DEVS)
def test_docs_publicados_solo_en_development(raw, sin_dotenv):
    with _main_con_env(HUGO_ENV=raw) as main:
        assert main._DEV is True
        assert main.app.docs_url == "/docs" and main.app.openapi_url == "/openapi.json"


@pytest.mark.parametrize("raw", _RAROS + _DEVS)
def test_paridad_settings_y_docs_con_una_sola_variable(raw, sin_dotenv):
    """Settings.hugo_env == development  <=>  /docs publicado. Sin .env de por medio."""
    with _main_con_env(HUGO_ENV=raw) as main:
        s = config.Settings(vendure_api_url="https://example.test/admin-api", _env_file=None)
        assert (s.hugo_env == "development") is main._DEV


def test_settings_production_nunca_publica_docs_aunque_haya_variable_duplicada(sin_dotenv):
    """pydantic-settings no distingue mayúsculas en el nombre (gana la última). Antes
    _DEV usaba os.getenv("HUGO_ENV") y con HUGO_ENV=development + hugo_env=production
    Settings decía production pero /docs quedaba publicado."""
    with _main_con_env(HUGO_ENV="development", hugo_env="production") as main:
        s = config.Settings(vendure_api_url="https://example.test/admin-api", _env_file=None)
        assert s.hugo_env == "production"
        assert main.app.openapi_url is None and main.app.docs_url is None


@pytest.mark.parametrize("dotenv, publicado", [("development", True), ("production.", False)])
def test_dotenv_decide_igual_settings_y_docs(tmp_path, monkeypatch, dotenv, publicado):
    """Correr local con el .env (sin exportar la variable): /docs sigue a Settings."""
    (tmp_path / ".env").write_text(f"HUGO_ENV={dotenv}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with _main_con_env(HUGO_ENV=None, hugo_env=None) as main:
        s = config.Settings(vendure_api_url="https://example.test/admin-api")
        assert (s.hugo_env == "development") is publicado
        assert (main.app.openapi_url == "/openapi.json") is publicado


@pytest.mark.parametrize("raw", ["production.", "prod", "", "Production ", "developmentt"])
def test_typo_endurece_todos_los_chequeos(monkeypatch, raw):
    s = _settings(hugo_env=raw, supabase_url="https://ref.supabase.co", supabase_anon_key="a",
                  supabase_allowed_emails="", hugo_api_keys="luis:corta")
    monkeypatch.setattr(auth, "get_settings", lambda: s)
    monkeypatch.setattr(security, "get_settings", lambda: s)
    assert auth._is_production() is True
    assert security._is_production() is True
    assert auth.allowlist_misconfigured() is True        # allowlist vacía cierra el login
    assert auth._email_allowed("x@b2box.pro") is False
    assert security.configured_api_keys() == {}           # key corta descartada
    assert security.api_keys_configured() is False


@pytest.mark.parametrize("raw", ["development", " DEVELOPMENT "])
def test_development_relaja_los_chequeos(monkeypatch, raw):
    s = _settings(hugo_env=raw, supabase_url="https://ref.supabase.co", supabase_anon_key="a",
                  supabase_allowed_emails="", hugo_api_keys="luis:corta")
    monkeypatch.setattr(auth, "get_settings", lambda: s)
    monkeypatch.setattr(security, "get_settings", lambda: s)
    assert auth._is_production() is False and security._is_production() is False
    assert auth.allowlist_misconfigured() is False
    assert security.configured_api_keys() == {"luis": "corta"}


def _session_cookie(monkeypatch, s) -> dict[str, str]:
    monkeypatch.setattr(auth, "get_settings", lambda: s)
    return {auth.COOKIE_NAME: auth.issue_session_token("qa")}


@pytest.fixture
def prod_client(monkeypatch):
    """Production por typo, con login local y una key válida."""
    s = _settings(hugo_env="production.", dashboard_password="pw-qa", hugo_api_keys=f"qa:{_GOOD_KEY}")
    _use(monkeypatch, s)
    from app import main

    return TestClient(main.app), _session_cookie(monkeypatch, s)


def test_debug_config_sin_nada_es_401(prod_client):
    c, _ = prod_client
    assert c.get("/api/debug-config").status_code == 401


def test_debug_config_con_key_sin_login_es_401(prod_client):
    c, _ = prod_client
    r = c.get("/api/debug-config", headers={"X-API-Key": _GOOD_KEY})
    assert r.status_code == 401


def test_debug_config_con_login_sin_key_es_401(prod_client):
    c, cookies = prod_client
    c.cookies.update(cookies)
    r = c.get("/api/debug-config")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "ApiKey"


def test_debug_config_con_login_y_key_mala_es_401(prod_client):
    c, cookies = prod_client
    c.cookies.update(cookies)
    assert c.get("/api/debug-config", headers={"X-API-Key": _GOOD_KEY + "x"}).status_code == 401


def test_debug_config_con_login_y_key_valida_es_200(prod_client):
    c, cookies = prod_client
    c.cookies.update(cookies)
    r = c.get("/api/debug-config", headers={"X-API-Key": _GOOD_KEY})
    assert r.status_code == 200, r.text
    assert _GOOD_KEY not in r.text  # enmascara, no filtra valores


def test_debug_config_en_production_sin_keys_es_503(monkeypatch):
    """Segunda traba: si algo se salteó _enforce_prod_secrets, la ruta igual cierra."""
    s = _settings(hugo_env="prod", dashboard_password="pw-qa", hugo_api_keys="qa:corta")
    _use(monkeypatch, s)
    from app import main

    c = TestClient(main.app)
    c.cookies.update(_session_cookie(monkeypatch, s))
    assert c.get("/api/debug-config", headers={"X-API-Key": "corta"}).status_code == 503


def test_debug_config_en_development_sin_login_ni_keys_queda_abierto(monkeypatch):
    """Documenta el modo dev: sin credenciales, todo abierto (con warnings)."""
    s = _settings(hugo_env="development")
    _use(monkeypatch, s)
    from app import main

    assert TestClient(main.app).get("/api/debug-config").status_code == 200
