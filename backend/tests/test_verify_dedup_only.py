"""POST /verify con dedup_only=True — el caller consulta SOLO el veredicto.

Paco PRO llama a /verify desde su propio pipeline solo para saber si el producto
ya está en el catálogo (el caller YA ES Paco), y Luis hace lo mismo antes de
mandar a Paco APP por su cuenta. En ambos casos Hugo no debe reenviar nada
(submit/submit_pro) ni chequear idempotencia.

Casos:
  1. dedup_only + duplicado     → veredicto completo + matched_product, sin Paco
  2. dedup_only + nuevo         → sin Paco, audit "verify_no_match (dedup-only)"
                                  y candidate_id del match cercano viaja igual
  3. sin dedup_only (default)   → comportamiento idéntico al de siempre (forward)
"""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")
os.environ.setdefault("VENDURE_ADMIN_TOKEN", "test-token")

import pytest  # noqa: E402

from app.api import routes  # noqa: E402
from app.api.routes import VerifyRequest, verify  # noqa: E402
from app.dedup.orchestrator import DedupVerdict  # noqa: E402
from app.vendure.client import VendureProduct  # noqa: E402

IMG = "https://cdn.ejemplo.com/lampara.jpg"

_FULL = {
    "id": "42",
    "name": "Lámpara LED táctil",
    "product_code": "BX-1001",
    "image_urls": ["https://cdn.b2box.app/lampara-1.jpg"],
}


def _catalog_product(pid: str = "42") -> VendureProduct:
    return VendureProduct(
        id=pid,
        name="Lámpara LED táctil",
        slug="lampara-led-tactil",
        description="Lámpara de pared recargable",
        enabled=True,
        source_url="https://detail.1688.com/offer/987.html",
        image_urls=["https://cdn.b2box.app/lampara-1.jpg"],
        product_code="BX-1001",
        featured_image_url="https://cdn.b2box.app/lampara-1.jpg",
        first_variant_price_cents=189900,
        variant_count=2,
    )


@pytest.fixture
def env(monkeypatch):
    """Aísla el endpoint: sin red, sin DB, sin Vendure, con espías de Paco."""
    recorded: list[dict] = []
    paco_calls: list[str] = []
    idem_calls: list[str | None] = []

    async def fake_catalog(force=False):  # noqa: ARG001
        return [_catalog_product()]

    class FakeVendureClient:
        async def get_product_full(self, product_id):  # noqa: ARG002
            return _FULL

    class _SubmitResult:
        search_id = "paco-123"
        status = "queued"

    async def fake_submit(image_url, **kw):  # noqa: ARG001
        paco_calls.append("submit")
        return _SubmitResult()

    async def fake_submit_pro(image_url, **kw):  # noqa: ARG001
        paco_calls.append("submit_pro")
        return _SubmitResult()

    def fake_idempotency(source_url, source):  # noqa: ARG001
        idem_calls.append(source_url)
        return None

    def fake_record(payload, verdict, *, action, detail):  # noqa: ARG001
        recorded.append({"action": action, "detail": detail})

    monkeypatch.setattr(routes.vendure_catalog, "get_catalog", fake_catalog)
    monkeypatch.setattr(routes, "VendureClient", FakeVendureClient)
    monkeypatch.setattr(routes.paco_integration, "submit", fake_submit)
    monkeypatch.setattr(routes.paco_integration, "submit_pro", fake_submit_pro)
    monkeypatch.setattr(routes, "_source_already_sent_to_paco", fake_idempotency)
    monkeypatch.setattr(routes, "_record_verify", fake_record)
    return {"recorded": recorded, "paco_calls": paco_calls, "idem_calls": idem_calls}


def _set_verdict(monkeypatch, verdict: DedupVerdict) -> None:
    async def fake_find(candidate, existing):  # noqa: ARG001
        return verdict

    monkeypatch.setattr(routes, "find_duplicate_in", fake_find)


def _request(**overrides) -> VerifyRequest:
    base = dict(
        name="Lámpara LED táctil",
        description="Lámpara de pared recargable",
        source_url="https://detail.1688.com/offer/987.html",
        image_urls=[IMG],
        source="b2box-pro",
    )
    base.update(overrides)
    return VerifyRequest(**base)


# ── dedup_only=True ──────────────────────────────────────────────


async def test_dedup_only_duplicado_devuelve_match_sin_tocar_paco(env, monkeypatch):
    _set_verdict(monkeypatch, DedupVerdict(
        is_duplicate=True, confidence=0.97, matched_by=["image"],
        per_strategy_scores={"url": 0.0, "image": 0.97, "text": 0.8},
        candidate_id="42",
    ))

    resp = await verify(_request(dedup_only=True))

    assert resp.is_duplicate is True
    assert resp.candidate_id == "42"
    assert resp.confidence == 0.97
    assert resp.per_strategy_scores["image"] == 0.97
    assert resp.matched_product == _FULL
    # Ni forward ni idempotencia: el caller ya es Paco.
    assert env["paco_calls"] == []
    assert env["idem_calls"] == []
    # Audit igual que hoy para duplicados.
    assert [r["action"] for r in env["recorded"]] == ["duplicate_flagged"]


async def test_dedup_only_nuevo_no_reenvia_y_audita_como_consulta(env, monkeypatch):
    _set_verdict(monkeypatch, DedupVerdict(is_duplicate=False, confidence=0.0))

    resp = await verify(_request(dedup_only=True))

    assert resp.is_duplicate is False
    assert resp.paco_search_id is None
    assert resp.paco_status is None
    assert env["paco_calls"] == []
    assert env["idem_calls"] == []  # se saltea _source_already_sent_to_paco
    assert [r["action"] for r in env["recorded"]] == ["verify_no_match"]
    assert "dedup-only, consultado por b2box-pro" in env["recorded"][0]["detail"]


async def test_dedup_only_nuevo_con_candidato_cercano_viaja_en_response(env, monkeypatch):
    # find_duplicate_in setea candidate_id en el best match aunque NO sea dup:
    # el admin lo usa para el aviso "parecido al catálogo".
    _set_verdict(monkeypatch, DedupVerdict(
        is_duplicate=False, confidence=0.55, matched_by=[],
        per_strategy_scores={"url": 0.0, "image": 0.55, "text": 0.4},
        candidate_id="42",
    ))

    resp = await verify(_request(dedup_only=True))

    assert resp.is_duplicate is False
    assert resp.candidate_id == "42"
    assert resp.confidence == 0.55
    assert resp.matched_product is None  # solo se llena si ES duplicado
    assert env["paco_calls"] == []


async def test_dedup_only_desde_luis_nuevo_falla_si_se_llama_a_paco(env, monkeypatch):
    # Criterio R0: producto nuevo + dedup_only → veredicto, CERO llamadas a Paco
    # (los mocks revientan si alguien los toca) y AuditLog "verify_no_match".
    _set_verdict(monkeypatch, DedupVerdict(is_duplicate=False, confidence=0.0))

    async def _boom(*a, **kw):  # noqa: ARG001
        raise AssertionError("dedup_only no debe llamar a Paco")

    monkeypatch.setattr(routes.paco_integration, "submit", _boom)
    monkeypatch.setattr(routes.paco_integration, "submit_pro", _boom)

    resp = await verify(_request(source="luis", dedup_only=True))

    assert resp.is_duplicate is False
    assert resp.paco_search_id is None
    assert resp.paco_status is None
    assert resp.paco_error is None
    assert [r["action"] for r in env["recorded"]] == ["verify_no_match"]
    assert "dedup-only, consultado por luis" in env["recorded"][0]["detail"]


async def test_dedup_only_ignora_idempotencia_aunque_ya_se_haya_enviado(env, monkeypatch):
    # Aunque el source_url figure como ya enviado a Paco, dedup_only devuelve el
    # veredicto puro: no responde "already_sent" (eso es del camino con forward).
    _set_verdict(monkeypatch, DedupVerdict(is_duplicate=False, confidence=0.0))
    monkeypatch.setattr(routes, "_source_already_sent_to_paco", lambda *a, **kw: "99")

    resp = await verify(_request(source="luis", dedup_only=True))

    assert resp.paco_status is None
    assert env["paco_calls"] == []
    assert [r["action"] for r in env["recorded"]] == ["verify_no_match"]


# ── Backward compatible: sin el flag, comportamiento idéntico ────


async def test_sin_flag_producto_nuevo_pro_reenvia_a_paco_pro(env, monkeypatch):
    _set_verdict(monkeypatch, DedupVerdict(is_duplicate=False, confidence=0.0))

    resp = await verify(_request())  # dedup_only default = False

    assert resp.paco_search_id == "paco-123"
    assert resp.paco_status == "queued"
    assert env["paco_calls"] == ["submit_pro"]
    assert env["idem_calls"] == ["https://detail.1688.com/offer/987.html"]
    assert [r["action"] for r in env["recorded"]] == ["verify_passed_to_paco"]


async def test_sin_flag_source_app_reenvia_a_paco_app(env, monkeypatch):
    _set_verdict(monkeypatch, DedupVerdict(is_duplicate=False, confidence=0.0))

    resp = await verify(_request(source="luis"))

    assert resp.paco_search_id == "paco-123"
    assert env["paco_calls"] == ["submit"]


async def test_sin_flag_duplicado_no_cambia(env, monkeypatch):
    _set_verdict(monkeypatch, DedupVerdict(
        is_duplicate=True, confidence=1.0, matched_by=["url"],
        per_strategy_scores={"url": 1.0, "image": 0.0, "text": 0.0},
        candidate_id="42",
    ))

    resp = await verify(_request())

    assert resp.is_duplicate is True
    assert resp.matched_product == _FULL
    assert env["paco_calls"] == []
    assert [r["action"] for r in env["recorded"]] == ["duplicate_flagged"]
