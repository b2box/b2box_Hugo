"""“Confirmar duplicado” de punta a punta por HTTP (TestClient, sin lifespan).

Reproduce el bug crítico de la auditoría de oct-2026 tal como pasa desde el
dashboard: /verify flaguea contra el producto ORIGINAL (el candidato de Luis no
está en Vendure) → el usuario aprieta "Confirmar duplicado" → Vendure NO se
toca. Y el camino bueno de la auditoría de catálogo: se apaga el más nuevo,
nunca el canónico. Catálogo, Vendure y Paco están mockeados; la DB es la
sqlite de tests.

También cubre el criterio 2 de la misma auditoría: HUGO_ENV=production con
SUPABASE_ALLOWED_EMAILS vacía → /api/login 403, pero /health y los clientes por
API key (/verify) siguen vivos.
"""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import auth, security  # noqa: E402
from app import main as main_mod  # noqa: E402
from app.api import routes  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.models import AuditLog  # noqa: E402
from app.db.session import engine, init_db  # noqa: E402
from app.vendure.client import VendureProduct  # noqa: E402

ORIGINAL = "42"               # ya está en Vendure; contra él matcheó el candidato
NEWER, OLDER = "1001", "999"  # par de la auditoría de catálogo: ambos en Vendure
SOURCE_URL = "https://detail.1688.com/offer/777.html"


def _prod(pid: str, enabled: bool = True) -> VendureProduct:
    return VendureProduct(
        id=pid, name="Lámpara LED", slug=f"p{pid}", description="", enabled=enabled,
        source_url=SOURCE_URL, image_urls=[], product_code=None, featured_image_url=None,
        first_variant_price_cents=100, variant_count=1,
    )


class FakeVendure:
    disabled: list[str] = []
    status_reads: list[str] = []

    async def get_product_full(self, product_id):
        return {"id": product_id, "name": "Lámpara LED"}

    async def get_enabled_status(self, product_id):
        FakeVendure.status_reads.append(product_id)
        return True

    async def disable_product(self, product_id):
        FakeVendure.disabled.append(product_id)


def _settings(**over) -> Settings:
    base = dict(vendure_api_url="https://example.invalid/admin-api", hugo_env="development")
    base.update(over)
    return Settings(**base)


def _use_settings(monkeypatch, s: Settings) -> None:
    monkeypatch.setattr(auth, "get_settings", lambda: s)
    monkeypatch.setattr(security, "get_settings", lambda: s)
    monkeypatch.setattr(main_mod, "get_settings", lambda: s)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    init_db()
    with Session(engine) as s:
        for row in s.exec(select(AuditLog)).all():
            s.delete(row)
        s.commit()
    FakeVendure.disabled = []
    FakeVendure.status_reads = []
    monkeypatch.setattr(routes, "VendureClient", FakeVendure)
    monkeypatch.setattr(routes.vendure_catalog, "invalidate", lambda: None)

    async def fake_get_catalog(force=False, full=False):  # noqa: ARG001
        return [_prod(ORIGINAL)]

    monkeypatch.setattr(routes.vendure_catalog, "get_catalog", fake_get_catalog)
    # Dashboard sin login y /verify sin API key (modo dev), pase lo que pase en el entorno.
    _use_settings(monkeypatch, _settings())
    security._verify_hits.clear()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(main_mod.app)  # sin lifespan: ni scheduler ni warm-ups


def _verify(client: TestClient, **over) -> dict:
    body = dict(name="Lámpara LED", source_url=SOURCE_URL, image_urls=["https://img/1.jpg"], source="luis")
    body.update(over)
    resp = client.post("/verify", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _pending_flag_ids() -> list[int]:
    with Session(engine) as s:
        return [r.id for r in s.exec(select(AuditLog).where(
            AuditLog.action == "duplicate_flagged", AuditLog.dismissed.is_not(True),  # type: ignore[union-attr]
        ))]


def _save(*rows: AuditLog) -> list[int]:
    with Session(engine) as s:
        for r in rows:
            s.add(r)
        s.commit()
        return [r.id for r in rows]


def _audit_flag(**over) -> AuditLog:
    """Tal como la escribe audit_duplicates en esta rama."""
    data = dict(
        action="duplicate_flagged", source="audit", product_id=NEWER, related_product_id=OLDER,
        disable_target_id=NEWER, canonical_product_id=OLDER, confidence=0.995,
        product_name="Lámpara LED (copia)", related_product_name="Lámpara LED",
    )
    data.update(over)
    return AuditLog(**data)


def _event(client: TestClient, event_id: int) -> dict:
    listing = client.get("/audit-log", params={"section": "duplicates", "include_dismissed": "true"})
    assert listing.status_code == 200, listing.text
    return next(e for e in listing.json()["items"] if e["id"] == event_id)


# ─── Criterio 1 ──────────────────────────────────────────────────────────────


def test_verify_flag_confirmed_from_the_dashboard_never_disables_the_original(client):
    out = _verify(client)
    assert out["is_duplicate"] is True and out["candidate_id"] == ORIGINAL
    (event_id,) = _pending_flag_ids()

    # El dashboard recibe la semántica: nada que apagar, el original es #42.
    assert _event(client, event_id)["duplicate"] == {
        "disable_target_id": None, "canonical_product_id": ORIGINAL, "vendure_action": "none",
    }

    resp = client.post(f"/api/audit-log/{event_id}/confirm-duplicate")
    assert resp.status_code == 200, resp.text
    assert resp.json()["vendure_action"] == "none"
    assert resp.json()["canonical_product_id"] == ORIGINAL

    assert FakeVendure.disabled == [], "disable_product NO fue llamado"
    assert FakeVendure.status_reads == [], "ni siquiera se leyó el estado en Vendure"
    assert _pending_flag_ids() == []
    with Session(engine) as s:
        confirmed = list(s.exec(select(AuditLog).where(AuditLog.action == "duplicate_confirmed")))
        assert len(confirmed) == 1 and confirmed[0].canonical_product_id == ORIGINAL
        assert list(s.exec(select(AuditLog).where(AuditLog.action == "duplicate_disabled"))) == []


def test_catalog_audit_flag_confirmed_from_the_dashboard_disables_the_newest(client):
    (event_id,) = _save(_audit_flag())
    assert _event(client, event_id)["duplicate"] == {
        "disable_target_id": NEWER, "canonical_product_id": OLDER, "vendure_action": "disable",
    }

    resp = client.post(f"/api/audit-log/{event_id}/confirm-duplicate")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["action"], body["product_id"], body["canonical_product_id"]) == ("disabled", NEWER, OLDER)
    assert FakeVendure.disabled == [NEWER]
    assert OLDER not in FakeVendure.disabled


def test_bulk_confirm_over_http_mixing_verify_and_audit_flags(client):
    _verify(client)  # flag de /verify contra el original #42 (Luis)
    _verify(client, source="orders-pro", callback_ctx={"quotation_item_id": "q-1"})  # otro cliente
    _save(_audit_flag())

    dry = client.post("/api/duplicates/bulk-confirm", params={"min_confidence": 0.99}).json()
    assert (dry["would_disable"], dry["would_confirm_only"], dry["preview_ids"]) == (1, 2, [NEWER])
    assert FakeVendure.disabled == []

    out = client.post(
        "/api/duplicates/bulk-confirm", params={"min_confidence": 0.99, "confirm": "true"},
    ).json()
    assert (out["disabled"], out["confirmed_only"], out["failed"]) == (1, 2, 0)
    assert FakeVendure.disabled == [NEWER]
    assert _pending_flag_ids() == []


def test_rows_written_before_the_new_columns_fall_back_by_source(client):
    """Filas de producción anteriores a disable_target_id / canonical_product_id."""
    verify_old, audit_old = _save(
        AuditLog(action="duplicate_flagged", source="luis", product_id=ORIGINAL, confidence=0.99,
                 product_name="candidato viejo"),
        AuditLog(action="duplicate_flagged", source="audit", product_id=NEWER, related_product_id=OLDER,
                 confidence=0.99, product_name="copia vieja"),
    )
    assert client.post(f"/api/audit-log/{verify_old}/confirm-duplicate").json()["vendure_action"] == "none"
    assert FakeVendure.disabled == []
    assert client.post(f"/api/audit-log/{audit_old}/confirm-duplicate").json()["product_id"] == NEWER
    assert FakeVendure.disabled == [NEWER]


# ─── Criterio 2: allowlist vacía en producción no tumba a los clientes máquina ─


def test_production_without_allowlist_blocks_login_but_not_api_key_clients(monkeypatch, client):
    _use_settings(monkeypatch, _settings(
        hugo_env="production", supabase_url="https://ref.supabase.co", supabase_anon_key="anon",
        supabase_allowed_emails="",
        # En producción una key < 24 chars se ignora: acá van keys "de verdad".
        hugo_api_keys="luis:k-luis-a1b2c3d4e5f6g7h8i9j0,cloud:k-cloud-a1b2c3d4e5f6g7h8i9j0",
    ))
    import httpx

    class _NeverCalled:
        async def __aenter__(self):
            raise AssertionError("la contraseña no debe viajar a Supabase")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _NeverCalled())

    login = client.post("/api/login", json={"username": "tech@b2box.pro", "password": "x"})
    assert login.status_code == 403
    assert "SUPABASE_ALLOWED_EMAILS" not in login.json()["detail"], "la env var no se revela al cliente"
    assert "deshabilitado" in login.json()["detail"]

    assert client.get("/health").status_code == 200
    assert client.post("/verify", json={"name": "x", "source_url": SOURCE_URL}).status_code == 401
    ok = client.post("/verify", json={"name": "Lámpara LED", "source_url": SOURCE_URL},
                     headers={"X-API-Key": "k-cloud-a1b2c3d4e5f6g7h8i9j0"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["is_duplicate"] is True
    # El dashboard (cookie) sigue cerrado para quien no tiene sesión.
    assert client.get("/audit-log").status_code == 401
