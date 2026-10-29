"""Solo un admin del dashboard (DASHBOARD_ADMINS) prende o apaga productos en Vendure fuera de la bandeja de Pao:
restaurar duplicados, confirmar duplicados (de a uno o en bloque) y confirmar un BX sin foto. Cualquier otra sesión, o
DASHBOARD_ADMINS vacío, recibe 403 antes de leer Vendure o tocar la base."""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import json  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, delete, select  # noqa: E402

from app import auth, security  # noqa: E402
from app import main as main_mod  # noqa: E402
from app.api import routes  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.models import AuditLog  # noqa: E402
from app.db.session import engine, init_db  # noqa: E402

ADMIN = "nico@b2box.pro"
FORBIDDEN = "Solo un administrador del dashboard puede hacer esto."
# (ruta, lo que Vendure ve con un admin, la fila nueva del audit log). {dup} y {bx} son los ids de los flags.
FLOWS = [
    ("/api/restore-duplicates?confirm=true", ("enable", "adm1"), "duplicates_restored_bulk"),
    ("/api/duplicates/bulk-confirm?confirm=true&min_confidence=0.99", ("disable", "adm2"), "duplicate_disabled"),
    ("/api/audit-log/{dup}/confirm-duplicate", ("disable", "adm2"), "duplicate_disabled"),
    ("/api/audit-log/{bx}/confirm-disable-bx", ("disable", "adm3"), "bx_no_image_disabled"),
]
# Los dry-runs no escriben, pero leen la base y Vendure: también son de admin.
PATHS = [f[0] for f in FLOWS] + ["/api/restore-duplicates", "/api/duplicates/bulk-confirm"]


class SpyVendure:
    """Anota todo lo que le piden. adm1 está apagado (Hugo lo apagó: se restaura); el resto, prendido."""

    calls: list[tuple[str, str]] = []

    async def get_enabled_status(self, product_id):
        SpyVendure.calls.append(("read", product_id))
        return product_id != "adm1"

    async def disable_product(self, product_id):
        SpyVendure.calls.append(("disable", product_id))

    async def enable_product(self, product_id):
        SpyVendure.calls.append(("enable", product_id))


def _wipe() -> None:
    with Session(engine) as s:
        s.execute(delete(AuditLog))
        s.commit()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    init_db()
    _wipe()
    SpyVendure.calls = []
    monkeypatch.setattr(routes, "VendureClient", SpyVendure)
    monkeypatch.setattr(routes.vendure_catalog, "invalidate", lambda: None)
    with Session(engine) as s:
        dup = AuditLog(action="duplicate_flagged", source="audit", product_id="adm2", related_product_id="adm0",
                       disable_target_id="adm2", canonical_product_id="adm0", confidence=1.0)
        bx = AuditLog(action="bx_no_image_flagged", source="audit", product_id="adm3", product_name="BXadm3")
        s.add(AuditLog(action="duplicate_disabled", source="manual", product_id="adm1",
                       before=json.dumps({"enabled": True}), after=json.dumps({"enabled": False})))
        s.add(dup)
        s.add(bx)
        s.commit()
        ids = {"dup": dup.id, "bx": bx.id}
    yield ids
    _wipe()


def _client(monkeypatch, *, admins: str, user: str) -> TestClient:
    """Login local prendido y una sesión válida de `user`: el middleware deja pasar, decide `require_admin`."""
    s = Settings(vendure_api_url="https://example.invalid/admin-api", hugo_env="development",
                 dashboard_user="admin", dashboard_password="secreto", dashboard_secret="s" * 32,
                 supabase_url="", supabase_anon_key="", dashboard_admins=admins)
    for mod in (auth, security, main_mod):
        monkeypatch.setattr(mod, "get_settings", lambda s=s: s)
    c = TestClient(main_mod.app)
    c.cookies.set(auth.COOKIE_NAME, auth.issue_session_token(user))
    return c


def _audit() -> list[tuple]:
    with Session(engine) as s:
        return [(r.id, r.action, r.dismissed, r.detail) for r in s.exec(select(AuditLog).order_by(AuditLog.id))]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("admins,user", [
    (ADMIN, "pao@b2box.pro"),   # sesión válida que no es admin (Pao decide en la bandeja, no acá)
    ("", ADMIN),                # DASHBOARD_ADMINS vacío: nadie, fail-closed
])
def test_sin_admin_403_sin_tocar_vendure_ni_la_base(monkeypatch, _env, path, admins, user):
    http = _client(monkeypatch, admins=admins, user=user)
    before = _audit()
    r = http.post(path.format(**_env))
    assert r.status_code == 403 and r.json()["detail"] == FORBIDDEN
    assert SpyVendure.calls == []
    assert _audit() == before


@pytest.mark.parametrize("path,write,action", FLOWS)
def test_admin_escribe_como_hoy_y_queda_quien_fue(monkeypatch, _env, path, write, action):
    http = _client(monkeypatch, admins=f"otro@b2box.pro, {ADMIN.upper()}", user=ADMIN)
    before = {row[0] for row in _audit()}
    r = http.post(path.format(**_env))
    assert r.status_code == 200, r.text
    assert [c for c in SpyVendure.calls if c[0] != "read"] == [write]
    (new,) = [row for row in _audit() if row[0] not in before]
    assert new[1] == action and f"por {ADMIN}" in new[3]
