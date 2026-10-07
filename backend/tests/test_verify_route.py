"""POST /verify — lo que decide antes de hablar con Paco.

Sin red: catálogo, Paco y Vendure mockeados. La DB es la sqlite de tests.
"""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.api import routes  # noqa: E402
from app.db.models import AuditLog  # noqa: E402
from app.db.session import engine, init_db  # noqa: E402
from app.vendure.client import VendureProduct  # noqa: E402

SOURCE_URL = "https://detail.1688.com/offer/4242.html"


def _prod(pid: str, enabled: bool, source_url: str | None = SOURCE_URL) -> VendureProduct:
    return VendureProduct(
        id=pid, name="Organizador de cocina", slug=f"p{pid}", description="",
        enabled=enabled, source_url=source_url, image_urls=[], product_code=None,
        featured_image_url=None, first_variant_price_cents=100, variant_count=1,
    )


class FakePaco:
    submitted: list[dict] = []

    @staticmethod
    async def submit(image_url, product_url=None):
        FakePaco.submitted.append({"image_url": image_url, "product_url": product_url, "pro": False})
        return routes.paco_integration.PacoSubmitResult(search_id="s-1", status="queued", raw={})

    @staticmethod
    async def submit_pro(image_url, callback_ctx=None, text_specs="", use_browser=False, product_link=None):
        FakePaco.submitted.append({
            "image_url": image_url, "callback_ctx": callback_ctx, "text_specs": text_specs,
            "use_browser": use_browser, "product_link": product_link, "pro": True,
        })
        return routes.paco_integration.PacoSubmitResult(search_id="s-pro", status="queued", raw={})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    init_db()
    with Session(engine) as s:
        for row in s.exec(select(AuditLog)).all():
            s.delete(row)
        s.commit()
    FakePaco.submitted = []
    monkeypatch.setattr(routes.paco_integration, "submit", FakePaco.submit)
    monkeypatch.setattr(routes.paco_integration, "submit_pro", FakePaco.submit_pro)
    yield


def _catalog(monkeypatch, products: list[VendureProduct]) -> None:
    async def fake_get_catalog(force=False, full=False):  # noqa: ARG001
        return products

    monkeypatch.setattr(routes.vendure_catalog, "get_catalog", fake_get_catalog)


def _payload(**over) -> routes.VerifyRequest:
    data = dict(name="Organizador de cocina", source_url=SOURCE_URL,
                image_urls=["https://img/1.jpg"], source="luis")
    data.update(over)
    return routes.VerifyRequest(**data)


# ─── Solo se compara contra productos habilitados ────────────────────────────


@pytest.mark.asyncio
async def test_disabled_products_do_not_count_as_duplicates(monkeypatch):
    """El catálogo cacheado trae los deshabilitados; un producto apagado no es
    "ya lo tenemos". Antes el match por URL contra el disabled cortaba el envío a Paco."""
    _catalog(monkeypatch, [_prod("7", enabled=False)])
    out = await routes.verify(_payload())
    assert out.is_duplicate is False
    assert out.candidate_id is None
    assert len(FakePaco.submitted) == 1, "se mandó a Paco como producto nuevo"


@pytest.mark.asyncio
async def test_enabled_match_is_still_a_duplicate(monkeypatch):
    class FakeVendure:
        async def get_product_full(self, pid):
            return {"id": pid, "name": "Organizador de cocina"}

    monkeypatch.setattr(routes, "VendureClient", FakeVendure)
    _catalog(monkeypatch, [_prod("7", enabled=False), _prod("8", enabled=True)])
    out = await routes.verify(_payload())
    assert out.is_duplicate is True
    assert out.candidate_id == "8"
    assert FakePaco.submitted == []


# ─── Idempotencia hacia Paco: por (source_url, source), no por URL sola ──────


@pytest.mark.asyncio
async def test_same_client_same_url_is_not_resent(monkeypatch):
    _catalog(monkeypatch, [])
    first = await routes.verify(_payload(source="luis"))
    second = await routes.verify(_payload(source="luis"))
    assert first.paco_search_id == "s-1"
    assert second.paco_status == "already_sent"
    assert second.paco_search_id is None
    assert len(FakePaco.submitted) == 1


@pytest.mark.asyncio
async def test_another_client_with_the_same_url_gets_its_own_search(monkeypatch):
    """Luis ya lo mandó a Paco APP; un pedido de Orders (Paco PRO, con callback)
    tiene que generar SU búsqueda. Antes el chequeo era global por URL y el
    segundo cliente se quedaba con already_sent."""
    _catalog(monkeypatch, [])
    await routes.verify(_payload(source="luis"))
    out = await routes.verify(_payload(source="orders-pro", callback_ctx={"quotation_item_id": "q-9"}))
    assert out.paco_status != "already_sent"
    assert out.paco_search_id == "s-pro"
    assert [c["pro"] for c in FakePaco.submitted] == [False, True]


@pytest.mark.asyncio
async def test_dismissed_rows_do_not_block_a_resend(monkeypatch):
    _catalog(monkeypatch, [])
    await routes.verify(_payload(source="luis"))
    with Session(engine) as s:
        for row in s.exec(select(AuditLog)).all():
            row.dismissed = True
            s.add(row)
        s.commit()
    out = await routes.verify(_payload(source="luis"))
    assert out.paco_search_id == "s-1"
    assert len(FakePaco.submitted) == 2


# ─── retry-paco: misma Paco y mismo callback que el /verify original ─────────


def _failed_event(**over) -> int:
    data = dict(
        action="paco_failed", source="luis", product_id="(nuevo)",
        product_name="Organizador", product_image_url="https://img/1.jpg",
        product_source_url=SOURCE_URL,
    )
    data.update(over)
    with Session(engine) as s:
        row = AuditLog(**data)
        s.add(row)
        s.commit()
        return row.id


@pytest.mark.asyncio
async def test_verify_persists_the_context_a_retry_needs(monkeypatch):
    import json

    _catalog(monkeypatch, [])
    await routes.verify(_payload(
        source="orders-pro", callback_ctx={"quotation_item_id": "q-9"},
        text_specs="rojo, 20cm", use_browser=True,
    ))
    with Session(engine) as s:
        row = s.exec(select(AuditLog).where(AuditLog.action == "verify_passed_to_paco")).one()
    ctx = json.loads(row.verify_ctx)
    assert ctx == {
        "source": "orders-pro", "callback_ctx": {"quotation_item_id": "q-9"},
        "text_specs": "rojo, 20cm", "use_browser": True,
    }


@pytest.mark.asyncio
async def test_retry_of_a_pro_event_goes_to_paco_pro_with_its_callback():
    """Antes TODO reintento iba a Paco APP y sin callback_ctx."""
    import json

    event_id = _failed_event(
        source="orders-pro",
        verify_ctx=json.dumps({
            "source": "orders-pro", "callback_ctx": {"quotation_item_id": "q-9"},
            "text_specs": "rojo", "use_browser": False,
        }),
    )
    with Session(engine) as s:
        out = await routes.retry_paco(event_id, s)

    assert out["paco"] == "PRO"
    assert out["paco_search_id"] == "s-pro"
    assert len(FakePaco.submitted) == 1
    sent = FakePaco.submitted[0]
    assert sent["pro"] is True
    assert sent["callback_ctx"] == {"quotation_item_id": "q-9"}
    assert sent["text_specs"] == "rojo"
    assert sent["product_link"] == SOURCE_URL

    with Session(engine) as s:
        new = s.exec(select(AuditLog).where(AuditLog.action == "verify_passed_to_paco")).one()
        assert new.source == "orders-pro", "misma tab e idempotencia que el original"
        assert json.loads(new.verify_ctx)["callback_ctx"] == {"quotation_item_id": "q-9"}
        assert s.get(AuditLog, event_id).dismissed is True


@pytest.mark.asyncio
async def test_retry_of_a_luis_event_goes_to_paco_app():
    event_id = _failed_event(source="luis")
    with Session(engine) as s:
        out = await routes.retry_paco(event_id, s)
    assert out["paco"] == "APP"
    assert FakePaco.submitted == [{"image_url": "https://img/1.jpg", "product_url": SOURCE_URL, "pro": False}]


@pytest.mark.asyncio
async def test_retry_of_a_pro_event_without_saved_ctx_still_goes_to_pro():
    """Fila vieja (sin verify_ctx): por lo menos va a la Paco correcta."""
    event_id = _failed_event(source="b2box-pro")
    with Session(engine) as s:
        await routes.retry_paco(event_id, s)
    assert FakePaco.submitted[0]["pro"] is True
    assert FakePaco.submitted[0]["callback_ctx"] is None


@pytest.mark.asyncio
async def test_retry_failure_is_logged_with_the_destination(monkeypatch):
    from fastapi import HTTPException

    async def boom(*a, **k):  # noqa: ARG001
        raise routes.paco_integration.PacoError("HTTP 500")

    monkeypatch.setattr(routes.paco_integration, "submit_pro", boom)
    event_id = _failed_event(source="orders-pro")
    with Session(engine) as s, pytest.raises(HTTPException) as exc:
        await routes.retry_paco(event_id, s)
    assert exc.value.status_code == 502
    assert "PRO" in exc.value.detail
    with Session(engine) as s:
        rows = list(s.exec(select(AuditLog).where(AuditLog.action == "paco_failed")))
    assert len(rows) == 2 and rows[-1].source == "orders-pro"


# ─── Casos borde (QA, auditoría oct-2026) ────────────────────────────────────


@pytest.mark.asyncio
async def test_omitted_source_shares_idempotency_with_an_explicit_luis(monkeypatch):
    """El default del modelo y el default de la fila tienen que coincidir."""
    _catalog(monkeypatch, [])
    await routes.verify(routes.VerifyRequest(
        name="Organizador de cocina", source_url=SOURCE_URL, image_urls=["https://img/1.jpg"],
    ))
    out = await routes.verify(_payload(source="luis"))
    assert out.paco_status == "already_sent"
    assert len(FakePaco.submitted) == 1


@pytest.mark.asyncio
async def test_retry_with_corrupt_verify_ctx_still_routes_by_the_row_source():
    event_id = _failed_event(source="orders-pro", verify_ctx="{esto no es json")
    with Session(engine) as s:
        out = await routes.retry_paco(event_id, s)
    assert out["paco"] == "PRO"
    assert FakePaco.submitted[0]["pro"] is True
    assert FakePaco.submitted[0]["callback_ctx"] is None


@pytest.mark.asyncio
async def test_retry_ignores_a_callback_ctx_that_is_not_an_object():
    import json

    event_id = _failed_event(
        source="orders-pro",
        verify_ctx=json.dumps({"source": "orders-pro", "callback_ctx": "q-9"}),
    )
    with Session(engine) as s:
        await routes.retry_paco(event_id, s)
    assert FakePaco.submitted[0]["pro"] is True
    assert FakePaco.submitted[0]["callback_ctx"] is None


@pytest.mark.asyncio
async def test_the_flag_written_by_verify_has_no_disable_target_and_keeps_its_ctx(monkeypatch):
    import json

    class FakeVendure:
        async def get_product_full(self, pid):
            return {"id": pid}

    monkeypatch.setattr(routes, "VendureClient", FakeVendure)
    _catalog(monkeypatch, [_prod("8", enabled=True)])
    await routes.verify(_payload(source="orders-pro", callback_ctx={"quotation_item_id": "q-9"}))
    with Session(engine) as s:
        row = s.exec(select(AuditLog).where(AuditLog.action == "duplicate_flagged")).one()
    assert (row.product_id, row.canonical_product_id, row.disable_target_id) == ("8", "8", None)
    assert json.loads(row.verify_ctx)["callback_ctx"] == {"quotation_item_id": "q-9"}
    assert json.loads(row.after)["matched_by"] == ["url"]


# ─── verify_ctx acotado ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_oversized_callback_ctx_is_rejected_with_413(monkeypatch):
    from fastapi import HTTPException

    _catalog(monkeypatch, [])
    huge = {"blob": "x" * (routes.MAX_CALLBACK_CTX_BYTES + 1)}
    with pytest.raises(HTTPException) as exc:
        await routes.verify(_payload(source="orders-pro", callback_ctx=huge))
    assert exc.value.status_code == 413
    assert FakePaco.submitted == [], "se corta antes de hablar con Paco"
    with Session(engine) as s:
        assert list(s.exec(select(AuditLog))) == []


@pytest.mark.asyncio
async def test_long_text_specs_go_to_paco_whole_but_are_stored_truncated(monkeypatch):
    import json

    _catalog(monkeypatch, [])
    long_specs = "rojo " * 1000  # 5000 chars
    await routes.verify(_payload(source="orders-pro", text_specs=long_specs))
    assert FakePaco.submitted[0]["text_specs"] == long_specs, "Paco recibe todo"
    with Session(engine) as s:
        row = s.exec(select(AuditLog).where(AuditLog.action == "verify_passed_to_paco")).one()
    ctx = json.loads(row.verify_ctx)
    assert len(ctx["text_specs"]) == routes.MAX_TEXT_SPECS_STORED
    assert ctx["text_specs_truncated"] is True
    assert len(row.verify_ctx.encode("utf-8")) <= routes.MAX_VERIFY_CTX_BYTES


def test_verify_ctx_never_exceeds_the_cap_and_marks_what_it_dropped():
    import json

    # Pasa el tope total aunque cada parte esté dentro del suyo: se construye
    # directo (sin pasar por el 413) para ejercitar la última defensa.
    payload = routes.VerifyRequest(
        source="orders-pro",
        callback_ctx={"blob": "y" * (routes.MAX_VERIFY_CTX_BYTES)},
        text_specs="z" * 100,
    )
    raw = routes._verify_ctx_json(payload)
    assert len(raw.encode("utf-8")) <= routes.MAX_VERIFY_CTX_BYTES
    ctx = json.loads(raw)
    assert ctx["callback_ctx"] is None
    assert ctx["callback_ctx_dropped"] is True
    assert ctx["source"] == "orders-pro" and ctx["text_specs"] == "z" * 100


def test_normal_sized_ctx_is_stored_whole():
    import json

    payload = routes.VerifyRequest(source="orders-pro", callback_ctx={"quotation_item_id": "q-9"},
                                   text_specs="rojo")
    ctx = json.loads(routes._verify_ctx_json(payload))
    assert ctx["callback_ctx"] == {"quotation_item_id": "q-9"}
    assert "text_specs_truncated" not in ctx and "callback_ctx_dropped" not in ctx


# ─── source normalizado ──────────────────────────────────────────────────────


@pytest.mark.parametrize("raw, expected", [
    ("  B2BOX-PRO ", "b2box-pro"),
    ("LUIS", "luis"),
    ("", "luis"),
    (None, "luis"),
    ("   ", "luis"),
    ("x" * 100, "x" * routes.MAX_SOURCE_LEN),
])
def test_source_is_normalized_on_input(raw, expected):
    assert routes.VerifyRequest(source=raw).source == expected


def test_source_omitted_defaults_to_luis():
    assert routes.VerifyRequest().source == "luis"


@pytest.mark.asyncio
async def test_uppercase_pro_source_routes_to_pro_and_is_stored_normalized(monkeypatch):
    _catalog(monkeypatch, [])
    await routes.verify(_payload(source="B2BOX-PRO", callback_ctx={"quotation_item_id": "q-1"}))
    assert FakePaco.submitted[0]["pro"] is True
    with Session(engine) as s:
        row = s.exec(select(AuditLog).where(AuditLog.action == "verify_passed_to_paco")).one()
    assert row.source == "b2box-pro", "misma tab y misma idempotencia que el valor canónico"


@pytest.mark.asyncio
async def test_case_variants_share_idempotency(monkeypatch):
    """Antes "LUIS" y "luis" eran clientes distintos: dos búsquedas en Paco."""
    _catalog(monkeypatch, [])
    await routes.verify(_payload(source="luis"))
    out = await routes.verify(_payload(source=" LUIS "))
    assert out.paco_status == "already_sent"
    assert len(FakePaco.submitted) == 1
