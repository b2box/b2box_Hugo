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
        # Desde #28 las sesiones del panel se firman solo con DASHBOARD_SECRET (32+): sin ella
        # ningún test de acá podría loguearse en production.
        "dashboard_secret": "s" * 40,
        "supabase_url": "",
        "supabase_anon_key": "",
        "_env_file": None,
    }
    base.update(over)
    return config.Settings(**base)


def _use(monkeypatch, s):
    """Todos los módulos que leen Settings ven el mismo: main, auth, security y
    config (las rutas que hacen `from app.config import get_settings` adentro)."""
    from app import main, security

    for mod in (auth, security, main, config):
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


@pytest.mark.parametrize(
    "path",
    ["/login", "/health", "/static/app.js", "/static/assets/index-abc.js", "/verify", "/verify/",
     "/app/lookup", "/app/index-status", "/api/login", "/api/logout", "/api/oficina/ml-queue",
     "/favicon.ico"],
)
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
        # Entorno, caché y app.main vuelven a como estaban: si no, los tests que
        # siguen heredan los settings de production (y la app recargada).
        monkeypatch.undo()
        config.get_settings.cache_clear()
        importlib.reload(main)


# ─── QA: paridad Settings ↔ main._DEV, guardas de producción y /api/debug-config ──
#
# _DEV (main.py) sale de EnvSettings, que lee HUGO_ENV igual que Settings (entorno
# + .env, sin distinguir mayúsculas, mismo validador). Estos tests fijan que /docs
# se publique si y solo si Settings dice development. Corren en un directorio sin
# .env para no depender del .env local del checkout.

import contextlib  # noqa: E402
import importlib  # noqa: E402
import json  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app import security  # noqa: E402

_GOOD_KEY = "qa-" + "a1b2c3d4e5f6g7h8i9j0k1l2m3"  # >= 24 chars: válida en production
_LUIS_KEY = "luis-" + "z9y8x7w6v5u4t3s2r1q0p9o8n7"  # válida, pero no es cliente admin
# Desde #28 las sesiones del panel se firman solo con DASHBOARD_SECRET (32+ caracteres).
_SECRET = "s" * 40
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
    config.get_settings.cache_clear()
    try:
        yield importlib.reload(main)
    finally:
        for k in env:
            os.environ.pop(k, None)
        for k, v in prev.items():
            _set_env(k, v)
        # Sin esto el caché de get_settings() queda con el entorno del test y los
        # tests que siguen corren con esos settings (p. ej. production sin sal).
        config.get_settings.cache_clear()
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
    """Production por typo, con login local, una key admin y una que no lo es."""
    s = _settings(hugo_env="production.", dashboard_password="pw-qa", dashboard_secret=_SECRET,
                  hugo_api_keys=f"qa:{_GOOD_KEY},luis:{_LUIS_KEY}", hugo_admin_clients="qa")
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
    s = _settings(hugo_env="prod", dashboard_password="pw-qa", dashboard_secret=_SECRET, hugo_api_keys="qa:corta")
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


def test_debug_config_no_filtra_token_del_webhook_ni_password_de_la_db(monkeypatch):
    s = _settings(
        hugo_env="production",
        dashboard_password="pw-qa",
        dashboard_secret=_SECRET,
        hugo_api_keys=f"qa:{_GOOD_KEY}",
        hugo_admin_clients="qa",
        alert_webhook_url="https://hooks.slack.com/services/T000/B000/tok3n-secreto",
        database_url="postgresql://hu:s3cr3t-pass@db.example.test:5432/hugo",
    )
    _use(monkeypatch, s)
    from app import main

    c = TestClient(main.app)
    c.cookies.update(_session_cookie(monkeypatch, s))
    r = c.get("/api/debug-config", headers={"X-API-Key": _GOOD_KEY})
    assert r.status_code == 200, r.text
    assert "tok3n" not in r.text and "s3cr3t" not in r.text and "hu:" not in r.text
    body = r.json()
    assert body["alerts"]["webhook_url"] == {"set": True, "length": 56, "host": "hooks.slack.com"}
    assert body["database"]["host"] == "db.example.test"


# ─── Hallazgos bajos de la revisión de seguridad de #30 ─────────────────────────


@pytest.mark.parametrize(
    "path",
    ["/verify-admin", "/verifyx", "/verify.json", "/healthz", "/health-admin", "/login-admin",
     "/loginx", "/api/login-as", "/api/loginx", "/api/logoutall", "/favicon.ico.bak",
     "/static", "/app", "/apps/lookup", "/api/oficina", "/api/oficinas/x"],
)
def test_allowlist_corta_en_limite_de_segmento(path):
    """Una ruta que solo EMPIEZA con el nombre de una pública no se saltea el login."""
    assert not auth._is_public_path(path)


def test_ruta_que_empieza_como_publica_pide_sesion(prod_client):
    """Por el middleware entero: sin sesión es 401, no un 404 que pasó de largo."""
    c, _ = prod_client
    for path in ("/verify-admin", "/healthz", "/login-admin"):
        assert c.get(path).status_code == 401, path


def _login_set_cookie(monkeypatch, hugo_env: str, headers: dict | None = None) -> str:
    s = _settings(hugo_env=hugo_env, dashboard_password="pw-qa", hugo_api_keys=f"qa:{_GOOD_KEY}")
    _use(monkeypatch, s)
    from app import main

    auth.record_successful_login("testclient")  # limpia un lockout que haya dejado otro test
    r = TestClient(main.app).post(
        "/api/login", json={"username": "admin", "password": "pw-qa"}, headers=headers or {}
    )
    assert r.status_code == 200, r.text
    return r.headers["set-cookie"].lower()


def test_cookie_de_sesion_es_secure_en_produccion_aunque_el_proxy_no_diga_https(monkeypatch):
    cookie = _login_set_cookie(monkeypatch, "production")
    assert auth.COOKIE_NAME in cookie and "secure" in cookie and "httponly" in cookie


def test_cookie_de_sesion_en_development_sigue_al_proxy(monkeypatch):
    assert "secure" not in _login_set_cookie(monkeypatch, "development")
    https = _login_set_cookie(monkeypatch, "development", {"X-Forwarded-Proto": "https"})
    assert "secure" in https


def test_development_avisa_al_arrancar_que_se_relaja(monkeypatch, caplog):
    from app import main

    _use(monkeypatch, _settings(hugo_env="development"))
    with caplog.at_level("ERROR", logger="app.main"):
        main._enforce_prod_secrets()
    msgs = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(msgs) == 1, msgs
    for parte in ("development", "login", "SUPABASE_ALLOWED_EMAILS", "API keys débiles", "/docs"):
        assert parte in msgs[0], parte


def test_production_no_emite_el_aviso_de_development(monkeypatch, caplog):
    from app import main

    _use(monkeypatch, _settings(hugo_env="production", dashboard_password="pw-qa",
                                hugo_api_keys=f"admin:{_GOOD_KEY}"))
    with caplog.at_level("WARNING", logger="app.main"):
        main._enforce_prod_secrets()
    assert not [r for r in caplog.records if "development" in r.getMessage()]
    assert not [r for r in caplog.records if "debug-config" in r.getMessage()]


def test_production_sin_cliente_admin_avisa_que_debug_config_queda_cerrado(monkeypatch, caplog):
    from app import main

    _use(monkeypatch, _settings(hugo_env="production", dashboard_password="pw-qa",
                                hugo_api_keys=f"luis:{_LUIS_KEY}"))
    with caplog.at_level("WARNING", logger="app.main"):
        main._enforce_prod_secrets()  # no tira el arranque
    assert any("HUGO_ADMIN_CLIENTS" in r.getMessage() for r in caplog.records)


def test_debug_config_con_login_y_key_no_admin_es_403(prod_client):
    """La key de Luis vale para /verify, no para leer la config del servidor."""
    c, cookies = prod_client
    c.cookies.update(cookies)
    r = c.get("/api/debug-config", headers={"X-API-Key": _LUIS_KEY})
    assert r.status_code == 403
    assert "HUGO_ADMIN_CLIENTS" not in r.text, "la env var no se revela al cliente"


def test_debug_config_cliente_admin_por_default_se_llama_admin(monkeypatch):
    assert _settings().hugo_admin_clients == "admin"
    s = _settings(hugo_env="production", dashboard_password="pw-qa",
                  hugo_api_keys=f"admin:{_GOOD_KEY},luis:{_LUIS_KEY}")
    _use(monkeypatch, s)
    from app import main

    c = TestClient(main.app)
    c.cookies.update(_session_cookie(monkeypatch, s))
    assert c.get("/api/debug-config", headers={"X-API-Key": _GOOD_KEY}).status_code == 200
    assert c.get("/api/debug-config", headers={"X-API-Key": _LUIS_KEY}).status_code == 403


def test_debug_config_en_development_con_keys_tambien_pide_admin(monkeypatch):
    s = _settings(hugo_env="development", hugo_api_keys=f"luis:{_LUIS_KEY}")
    _use(monkeypatch, s)
    from app import main

    r = TestClient(main.app).get("/api/debug-config", headers={"X-API-Key": _LUIS_KEY})
    assert r.status_code == 403


def _debug_config(monkeypatch, **over) -> dict:
    s = _settings(hugo_env="production", dashboard_password="pw-qa",
                  hugo_api_keys=f"qa:{_GOOD_KEY}", hugo_admin_clients="qa", **over)
    _use(monkeypatch, s)
    from app import main

    c = TestClient(main.app)
    c.cookies.update(_session_cookie(monkeypatch, s))
    r = c.get("/api/debug-config", headers={"X-API-Key": _GOOD_KEY})
    assert r.status_code == 200, r.text
    return r.json()


def test_debug_config_enmascara_channel_token_email_y_error_de_anthropic(monkeypatch):
    from app.dedup import vision_rerank

    err = "sonnet-5: AuthenticationError: invalid x-api-key sk-ant-qa-123"
    monkeypatch.setattr(vision_rerank, "_last_anthropic_error", err)
    body = _debug_config(monkeypatch, vendure_channel_token="canal-secreto-qa",
                         alert_email_to="ops-qa@b2box.pro")
    texto = json.dumps(body)
    for secreto in ("canal-secreto-qa", "ops-qa@", "sk-ant-qa", "invalid x-api-key"):
        assert secreto not in texto, secreto
    assert body["vendure"]["channel_token"] == {"set": True, "length": 16}
    assert body["alerts"]["email_to"] == {"set": True, "length": 16}
    assert body["vision"]["last_anthropic_error"] == {
        "set": True, "length": len(err), "type": "AuthenticationError",
    }


def test_debug_config_sin_error_de_anthropic(monkeypatch):
    from app.dedup import vision_rerank

    monkeypatch.setattr(vision_rerank, "_last_anthropic_error", "")
    body = _debug_config(monkeypatch)
    assert body["vision"]["last_anthropic_error"] == {"set": False, "length": 0, "type": None}
    assert body["vendure"]["channel_token"] == {"set": False, "length": 0}


@pytest.mark.parametrize(
    "database_url, host",
    [
        ("postgresql://hugo_qa:pa/ss@db.example.test:5432/hugo", None),
        ("postgresql://hugo_qa:pa#ss@db.example.test:5432/hugo", None),
        ("postgresql://hugo_qa:pa?ss@db.example.test:5432/hugo", None),
        ("postgresql://hugo_qa:p@ss/w0rd@db.example.test:5432/hugo", None),
        ("postgresql://hugo_qa:p@ss@db.example.test:5432/hugo", "db.example.test"),
        ("postgresql://hugo_qa:pa%2Fss@db.example.test:5432/hugo", "db.example.test"),
        ("postgresql://db.example.test:5432/hugo", "db.example.test"),
        ("sqlite:///./hugo.db", None),
    ],
)
def test_debug_config_host_de_la_db_nunca_es_el_usuario(monkeypatch, database_url, host):
    """Con "/", "#" o "?" sin escapar en la contraseña, urlparse devolvía el usuario."""
    body = _debug_config(monkeypatch, database_url=database_url)
    assert body["database"]["host"] == host
    assert "hugo_qa" not in json.dumps(body)


# ─── QA: casos límite de los hallazgos bajos ────────────────────────────────────


def _cookie_attrs(set_cookie: str) -> set[str]:
    """Atributos del Set-Cookie (sin el nombre=valor), para no matchear "secure" en el token."""
    return {part.strip().split("=", 1)[0] for part in set_cookie.split(";")[1:]}


@pytest.mark.parametrize("headers", [None, {"X-Forwarded-Proto": "http"}, {"X-Forwarded-Proto": ""}])
def test_qa_cookie_secure_en_produccion_es_atributo(monkeypatch, headers):
    attrs = _cookie_attrs(_login_set_cookie(monkeypatch, "production", headers))
    assert {"secure", "httponly"} <= attrs, attrs


def test_qa_cookie_development_sin_proxy_https_no_lleva_atributo_secure(monkeypatch):
    assert "secure" not in _cookie_attrs(_login_set_cookie(monkeypatch, "development"))


@pytest.mark.parametrize(
    "method, path",
    [("GET", "/health"), ("POST", "/verify"), ("GET", "/app/index-status"),
     ("GET", "/api/oficina/ml-queue"), ("POST", "/api/logout")],
)
def test_qa_rutas_publicas_pasan_el_middleware_sin_sesion(prod_client, method, path):
    """Por el middleware entero: lo público no recibe el 401 de "No autenticado"."""
    c, _ = prod_client
    r = c.request(method, path, json={} if method == "POST" else None)
    assert not (r.status_code == 401 and r.json().get("detail") == "No autenticado"), (path, r.text)


def test_qa_hugo_admin_clients_se_lee_de_la_env_var(monkeypatch):
    monkeypatch.setenv("HUGO_ADMIN_CLIENTS", " ops , admin ,, ")
    s = config.Settings(vendure_api_url="https://example.test/admin-api", _env_file=None)
    monkeypatch.setattr(security, "get_settings", lambda: s)
    assert security.admin_clients() == frozenset({"ops", "admin"})


def _get_debug_config(monkeypatch, key: str, **over) -> int:
    s = _settings(hugo_env="production", dashboard_password="pw-qa", **over)
    _use(monkeypatch, s)
    from app import main

    c = TestClient(main.app)
    c.cookies.update(_session_cookie(monkeypatch, s))
    return c.get("/api/debug-config", headers={"X-API-Key": key}).status_code


@pytest.mark.parametrize("raw", ["", " ", ",", " , "])
def test_qa_hugo_admin_clients_vacio_cierra_debug_config_a_todos(monkeypatch, raw):
    assert _get_debug_config(monkeypatch, _GOOD_KEY, hugo_api_keys=f"admin:{_GOOD_KEY}",
                             hugo_admin_clients=raw) == 403


@pytest.mark.parametrize("admins", ["admin", "legacy", "admin, legacy"])
def test_qa_debug_config_con_la_key_legacy(monkeypatch, admins):
    """HUGO_API_KEY (cliente "legacy") es compartida: nunca es admin, aunque HUGO_ADMIN_CLIENTS lo nombre."""
    assert _get_debug_config(monkeypatch, _GOOD_KEY, hugo_api_key=_GOOD_KEY,
                             hugo_admin_clients=admins) == 403


def test_qa_legacy_y_admin_juntos_solo_entra_admin(monkeypatch):
    over = {"hugo_api_key": _LUIS_KEY, "hugo_api_keys": f"admin:{_GOOD_KEY}"}
    assert _get_debug_config(monkeypatch, _GOOD_KEY, **over) == 200
    assert _get_debug_config(monkeypatch, _LUIS_KEY, **over) == 403


def test_qa_arranque_con_solo_legacy_avisa_que_debug_config_queda_cerrado(monkeypatch, caplog):
    from app import main

    _use(monkeypatch, _settings(hugo_env="production", dashboard_password="pw-qa", hugo_api_key=_GOOD_KEY))
    with caplog.at_level("WARNING", logger="app.main"):
        main._enforce_prod_secrets()
    assert any("HUGO_ADMIN_CLIENTS" in r.getMessage() for r in caplog.records)


def test_qa_cliente_admin_con_key_debil_no_entra_en_produccion(monkeypatch, caplog):
    from app import main

    over = {"hugo_api_keys": f"admin:corta,luis:{_LUIS_KEY}"}
    _use(monkeypatch, _settings(hugo_env="production", dashboard_password="pw-qa", **over))
    with caplog.at_level("WARNING", logger="app.main"):
        main._enforce_prod_secrets()
    assert any("HUGO_ADMIN_CLIENTS" in r.getMessage() for r in caplog.records)
    assert _get_debug_config(monkeypatch, "corta", **over) == 401


def test_qa_debug_config_en_development_con_key_admin_es_200(monkeypatch):
    s = _settings(hugo_env="development", hugo_api_keys=f"admin:{_GOOD_KEY},luis:{_LUIS_KEY}")
    _use(monkeypatch, s)
    from app import main

    assert TestClient(main.app).get("/api/debug-config", headers={"X-API-Key": _GOOD_KEY}).status_code == 200


@pytest.mark.parametrize(
    "err, tipo",
    [
        ("sonnet-5: RateLimitError: ", "RateLimitError"),                     # mensaje vacío
        ("sonnet-5: APIStatusError: a: b sk-ant-qa-999", "APIStatusError"),   # mensaje con ": "
        ("texto suelto sk-ant-qa-999", None),                                 # sin formato
        ("sonnet-5: sk-ant-qa-999", None),                                    # dos partes
        ("sonnet-5: Error con espacios: sk-ant-qa-999", None),                # tipo no identificador
    ],
)
def test_qa_last_anthropic_error_formatos_raros(monkeypatch, err, tipo):
    from app.dedup import vision_rerank

    monkeypatch.setattr(vision_rerank, "_last_anthropic_error", err)
    body = _debug_config(monkeypatch)
    assert body["vision"]["last_anthropic_error"] == {"set": True, "length": len(err), "type": tipo}
    assert "sk-ant-qa" not in json.dumps(body)


@pytest.mark.parametrize(
    "database_url, host",
    [
        ("postgresql://hugo_qa:pa[ss@db.example.test:5432/hugo", None),   # urlparse: ValueError
        ("postgresql://hugo_qa:pa]ss@db.example.test:5432/hugo", None),
        ("postgresql://hugo_qa:pa:/ss@db.example.test:5432/hugo", None),
        ("postgresql://hugo_qa:pw@db.example.test/hugo?options=a@b", None),  # @ en la query: se pierde, no filtra
        ("postgresql://hugo_qa:pw@[::1]:5432/hugo", "::1"),
        ("hugo_qa:pa/ss@db.example.test:5432/hugo", None),                # sin esquema
        ("postgresql://hugo_qa:pa%40ss@db.example.test/hugo", "db.example.test"),
    ],
)
def test_qa_host_de_la_db_urls_raras(monkeypatch, database_url, host):
    body = _debug_config(monkeypatch, database_url=database_url)
    assert body["database"]["host"] == host
    assert "hugo_qa" not in json.dumps(body)


def test_qa_host_del_webhook_con_credenciales_rotas_no_filtra(monkeypatch):
    body = _debug_config(monkeypatch, alert_webhook_url="https://hugo_qa:t0k3nqa/en@hooks.example.test/x")
    assert body["alerts"]["webhook_url"]["host"] is None
    texto = json.dumps(body)
    assert "hugo_qa" not in texto and "t0k3nqa" not in texto


# ─── Revisión de seguridad: bypass por Host y admin con key compartida ──────────


@pytest.mark.parametrize("host", ["testserver/verify", "x/static", "x/health", "x/app", "x/login"])
def test_header_host_con_barra_no_saltea_el_login(prod_client, host):
    """Starlette arma request.url con el header Host: con "Host: x/verify",
    request.url.path era /verify/api/settings (pública) y el router servía
    /api/settings sin sesión. El middleware tiene que mirar scope["path"]."""
    c, _ = prod_client
    for path in ("/api/settings", "/audit-log", "/api/debug-config"):
        r = c.get(path, headers={"Host": host, "X-API-Key": _GOOD_KEY})
        assert r.status_code == 401, (host, path, r.status_code)
        assert r.json() == {"detail": "No autenticado"}


@pytest.mark.parametrize(
    "keys",
    [f"admin:{_GOOD_KEY},luis:{_GOOD_KEY}", f"luis:{_GOOD_KEY},admin:{_GOOD_KEY}"],
)
def test_admin_con_key_compartida_no_entra_en_ningun_orden(monkeypatch, caplog, keys):
    """Antes el orden de HUGO_API_KEYS decidía si la key compartida era admin."""
    from app import main

    assert _get_debug_config(monkeypatch, _GOOD_KEY, hugo_api_keys=keys) == 403
    with caplog.at_level("WARNING", logger="app.main"):
        main._enforce_prod_secrets()
    assert any("cerrado" in r.getMessage() for r in caplog.records)


def test_dos_admins_con_la_misma_key_siguen_entrando(monkeypatch):
    keys = f"admin:{_GOOD_KEY},ops:{_GOOD_KEY}"
    assert _get_debug_config(monkeypatch, _GOOD_KEY, hugo_api_keys=keys,
                             hugo_admin_clients="admin,ops") == 200


def test_admin_clients_nunca_incluye_legacy(monkeypatch):
    s = _settings(hugo_admin_clients="legacy, admin")
    monkeypatch.setattr(security, "get_settings", lambda: s)
    assert security.admin_clients() == frozenset({"admin"})


def test_arranque_avisa_si_hugo_admin_clients_nombra_a_legacy(monkeypatch, caplog):
    from app import main

    _use(monkeypatch, _settings(hugo_env="production", dashboard_password="pw-qa",
                                hugo_api_keys=f"admin:{_GOOD_KEY}", hugo_admin_clients="admin,legacy"))
    with caplog.at_level("WARNING", logger="app.main"):
        main._enforce_prod_secrets()
    msgs = [r.getMessage() for r in caplog.records]
    assert any("'legacy'" in m for m in msgs), msgs
    assert not any("cerrado" in m for m in msgs), msgs  # admin sigue teniendo key propia
