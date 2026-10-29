"""Bordes del permiso de admin en los 4 endpoints que prenden o apagan productos en Vendure
(fuera de la bandeja de Pao).

Además de Vendure, espía la sesión de base que recibe el endpoint: el 403 tiene que salir antes de cualquier
lectura o escritura, no solo antes de escribir. Casos: sin cookie (login prendido y dashboard abierto), cookie
de otro, cookie adulterada o vencida, DASHBOARD_ADMINS vacío o solo comas, DASHBOARD_SECRET ausente o corto,
evento inexistente, y un admin escrito con otras mayúsculas o espacios (que sí pasa) contra un usuario parecido
(que no)."""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import json  # noqa: E402
import time  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, delete  # noqa: E402

from app import auth, security  # noqa: E402
from app import main as main_mod  # noqa: E402
from app.api import routes  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.models import AuditLog  # noqa: E402
from app.db.session import engine, get_session, init_db  # noqa: E402

ADMIN = "nico@b2box.pro"
FORBIDDEN = "Solo un administrador del dashboard puede hacer esto."
CROSS_SITE = "Solo se puede hacer desde el dashboard de Hugo."
SECRET = "s" * 32
PATHS = [
    "/api/restore-duplicates",
    "/api/restore-duplicates?confirm=true&safe=false",
    "/api/duplicates/bulk-confirm",
    "/api/duplicates/bulk-confirm?confirm=true&min_confidence=0",
    "/api/audit-log/{dup}/confirm-duplicate",
    "/api/audit-log/{bx}/confirm-disable-bx",
]


class SpyVendure:
    calls: list[tuple[str, str]] = []

    async def get_enabled_status(self, product_id):
        SpyVendure.calls.append(("read", product_id))
        return product_id != "bd1"

    async def disable_product(self, product_id):
        SpyVendure.calls.append(("disable", product_id))

    async def enable_product(self, product_id):
        SpyVendure.calls.append(("enable", product_id))


class SpySession(Session):
    """La sesión que recibe el endpoint: anota cada lectura o escritura."""

    calls: list[str] = []

    def _note(name):  # noqa: N805
        def wrapper(self, *a, **kw):
            SpySession.calls.append(name)
            return getattr(Session, name)(self, *a, **kw)
        return wrapper

    exec = _note("exec")
    execute = _note("execute")
    get = _note("get")
    scalar = _note("scalar")
    scalars = _note("scalars")
    add = _note("add")
    delete = _note("delete")
    merge = _note("merge")
    flush = _note("flush")
    commit = _note("commit")


def _spy_session():
    with SpySession(engine) as s:
        yield s


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    init_db()
    with Session(engine) as s:
        s.execute(delete(AuditLog))
        dup = AuditLog(action="duplicate_flagged", source="audit", product_id="bd2", related_product_id="bd0",
                       disable_target_id="bd2", canonical_product_id="bd0", confidence=1.0)
        bx = AuditLog(action="bx_no_image_flagged", source="audit", product_id="bd3", product_name="BXbd3")
        s.add(AuditLog(action="duplicate_disabled", source="manual", product_id="bd1",
                       before=json.dumps({"enabled": True}), after=json.dumps({"enabled": False})))
        s.add_all([dup, bx])
        s.commit()
        ids = {"dup": dup.id, "bx": bx.id}
    SpyVendure.calls = []
    SpySession.calls = []
    monkeypatch.setattr(routes, "VendureClient", SpyVendure)
    monkeypatch.setattr(routes.vendure_catalog, "invalidate", lambda: None)
    monkeypatch.setitem(main_mod.app.dependency_overrides, get_session, _spy_session)
    yield ids
    with Session(engine) as s:
        s.execute(delete(AuditLog))
        s.commit()


def _settings(monkeypatch, **over) -> None:
    base = dict(vendure_api_url="https://example.invalid/admin-api", hugo_env="development",
                dashboard_user="admin", dashboard_password="secreto", dashboard_secret=SECRET,
                supabase_url="", supabase_anon_key="", dashboard_admins=ADMIN)
    base.update(over)
    s = Settings(**base)
    for mod in (auth, security, main_mod):
        monkeypatch.setattr(mod, "get_settings", lambda s=s: s)


def _client(cookie: str | None = None) -> TestClient:
    c = TestClient(main_mod.app)
    if cookie is not None:
        c.cookies.set(auth.COOKIE_NAME, cookie)
    return c


def _assert_untouched(r, status: int, detail: str = FORBIDDEN) -> None:
    assert r.status_code == status, r.text
    if status == 403:
        assert r.json()["detail"] == detail
    assert SpyVendure.calls == []
    assert SpySession.calls == [], "el 403 tiene que salir antes de leer la base"


def _token(monkeypatch, user: str, **settings) -> str:
    """Cookie firmada con la configuración `settings` (después se cambia a la del caso)."""
    _settings(monkeypatch, **settings)
    return auth.issue_session_token(user)


@pytest.mark.parametrize("path", PATHS)
def test_sin_cookie_con_login_prendido_401_del_middleware(monkeypatch, _env, path):
    _settings(monkeypatch)
    _assert_untouched(_client().post(path.format(**_env)), 401)


@pytest.mark.parametrize("path", PATHS)
def test_sin_cookie_con_el_dashboard_abierto_403(monkeypatch, _env, path):
    """Login apagado (sin password ni Supabase, solo desarrollo): el middleware deja pasar todo y
    decide require_admin."""
    _settings(monkeypatch, dashboard_password="")
    _assert_untouched(_client().post(path.format(**_env)), 403)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("admins,user", [
    (ADMIN, "pao@b2box.pro"),               # sesión válida de otra persona
    ("", ADMIN),                            # DASHBOARD_ADMINS vacío
    (" , ,", ADMIN),                        # solo comas y espacios: también vacío
    (ADMIN, "nico@b2box.pro.ar"),           # parecido no alcanza: no es un prefijo
    (ADMIN, "ico@b2box.pro"),               # ni un sufijo
    (f"{ADMIN},otro@b2box.pro", "nico@b2box.pro,otro@b2box.pro"),   # ni la lista entera como usuario
])
def test_sesion_valida_que_no_es_admin_403_sin_leer_nada(monkeypatch, _env, path, admins, user):
    cookie = _token(monkeypatch, user, dashboard_admins=admins)
    _assert_untouched(_client(cookie).post(path.format(**_env)), 403)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("secret", ["", "corto" * 6])   # sin DASHBOARD_SECRET o con menos de 32 caracteres
def test_admin_sin_dashboard_secret_403(monkeypatch, _env, path, secret):
    """En desarrollo la sesión se firma con la password del login local: entra al dashboard, pero no es admin."""
    cookie = _token(monkeypatch, ADMIN, dashboard_secret=secret)
    http = _client(cookie)
    assert http.get("/api/health-metrics").status_code == 200   # la sesión vale para el resto del dashboard
    SpySession.calls = []
    _assert_untouched(http.post(path.format(**_env)), 403)


@pytest.mark.parametrize("path", PATHS)
def test_cookie_de_admin_firmada_con_otra_clave_401(monkeypatch, _env, path):
    cookie = _token(monkeypatch, ADMIN, dashboard_secret="x" * 32)
    _settings(monkeypatch)
    _assert_untouched(_client(cookie).post(path.format(**_env)), 401)


@pytest.mark.parametrize("path", PATHS)
def test_cookie_de_admin_vencida_401(monkeypatch, _env, path):
    cookie = _token(monkeypatch, ADMIN)
    real = time.time
    monkeypatch.setattr(auth, "time", SimpleNamespace(time=lambda: real() + 13 * 3600))   # la sesión dura 12 h
    _assert_untouched(_client(cookie).post(path.format(**_env)), 401)


@pytest.mark.parametrize("flow", ["confirm-duplicate", "confirm-disable-bx"])
def test_evento_inexistente_sin_admin_da_403_no_404(monkeypatch, _env, flow):
    """El 403 sale antes de buscar el evento: sin permiso no se puede saber qué ids existen."""
    cookie = _token(monkeypatch, "pao@b2box.pro")
    _assert_untouched(_client(cookie).post(f"/api/audit-log/999999/{flow}"), 403)


WRITES = [
    ("/api/restore-duplicates?confirm=true", ("enable", "bd1")),
    ("/api/duplicates/bulk-confirm?confirm=true&min_confidence=0.99", ("disable", "bd2")),
    ("/api/audit-log/{dup}/confirm-duplicate", ("disable", "bd2")),
    ("/api/audit-log/{bx}/confirm-disable-bx", ("disable", "bd3")),
]


@pytest.mark.parametrize("path,write", WRITES)
@pytest.mark.parametrize("admins,user", [
    ("  OTRO@b2box.pro ,  Nico@B2BOX.pro  ", ADMIN),   # la lista con mayúsculas y espacios
    (ADMIN, "NICO@B2BOX.PRO"),                          # la sesión con mayúsculas
    (ADMIN, f"  {ADMIN} "),                             # la sesión con espacios
])
def test_admin_con_otras_mayusculas_o_espacios_pasa(monkeypatch, _env, path, write, admins, user):
    cookie = _token(monkeypatch, user, dashboard_admins=admins)
    r = _client(cookie).post(path.format(**_env))
    assert r.status_code == 200, r.text
    assert [c for c in SpyVendure.calls if c[0] != "read"] == [write]
    assert "get" in SpySession.calls or "exec" in SpySession.calls   # el espía de la base anda


@pytest.mark.parametrize("path", [w[0] for w in WRITES])
@pytest.mark.parametrize("site", ["same-site", "cross-site"])
def test_admin_desde_otro_sitio_403_sin_leer_nada(monkeypatch, _env, path, site):
    """CSRF, como en la bandeja: la cookie del admin viaja sola en un POST que armó otra página."""
    cookie = _token(monkeypatch, ADMIN)
    r = _client(cookie).post(path.format(**_env), headers={"Sec-Fetch-Site": site})
    _assert_untouched(r, 403, CROSS_SITE)


@pytest.mark.parametrize("path,write", WRITES)
@pytest.mark.parametrize("site", ["same-origin", None])   # el dashboard, o un cliente que no es un navegador
def test_admin_desde_el_dashboard_o_sin_header_escribe(monkeypatch, _env, path, write, site):
    cookie = _token(monkeypatch, ADMIN)
    headers = {} if site is None else {"Sec-Fetch-Site": site}
    r = _client(cookie).post(path.format(**_env), headers=headers)
    assert r.status_code == 200, r.text
    assert [c for c in SpyVendure.calls if c[0] != "read"] == [write]

