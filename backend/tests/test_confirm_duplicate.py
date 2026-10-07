"""“Confirmar duplicado” no puede apagar el producto ORIGINAL.

Regresión del bug crítico de la auditoría de oct-2026: /verify guardaba el
`duplicate_flagged` con product_id = el producto que YA existe en Vendure (el
candidato de Luis/Cloud todavía no está en el catálogo), y confirm-duplicate /
bulk-confirm hacían disable_product(product_id) → apagaban el original.

Dos orígenes, dos semánticas:
  · /verify → confirmar NO toca Vendure: registra y archiva.
  · auditoría de catálogo → apaga el MÁS NUEVO (drop), nunca el canónico (keep).
"""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.api import routes  # noqa: E402
from app.db.models import AuditLog  # noqa: E402
from app.db.session import engine, init_db  # noqa: E402
from app.dedup.confirm_target import canonical_for, disable_target_for  # noqa: E402
from app.dedup.orchestrator import DedupVerdict  # noqa: E402

ORIGINAL = "42"       # ya está en Vendure; es contra el que matcheó el candidato
NEWER, OLDER = "1001", "999"   # par de la auditoría de catálogo: ambos en Vendure


class FakeVendure:
    """Vendure de mentira: todos los productos existen y están enabled."""

    disabled: list[str] = []
    status_reads: list[str] = []

    async def get_enabled_status(self, product_id):
        FakeVendure.status_reads.append(product_id)
        return True

    async def disable_product(self, product_id):
        FakeVendure.disabled.append(product_id)


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    init_db()
    with Session(engine) as s:
        for row in s.exec(select(AuditLog)).all():
            s.delete(row)
        s.commit()
    FakeVendure.disabled = []
    FakeVendure.status_reads = []
    monkeypatch.setattr(routes, "VendureClient", FakeVendure)
    monkeypatch.setattr(routes.vendure_catalog, "invalidate", lambda: None)
    yield


def _verify_flag(**over) -> AuditLog:
    """Fila tal como la escribía /verify antes del fix (sin columnas explícitas)."""
    data = dict(
        action="duplicate_flagged", source="luis", product_id=ORIGINAL,
        related_product_id=None, confidence=0.995,
        product_name="Lámpara LED (candidato de Luis)",
        product_source_url="https://detail.1688.com/offer/777.html",
    )
    data.update(over)
    return AuditLog(**data)


def _audit_flag(**over) -> AuditLog:
    """Fila de la auditoría de catálogo: product_id = más nuevo, related = canónico."""
    data = dict(
        action="duplicate_flagged", source="audit", product_id=NEWER,
        related_product_id=OLDER, confidence=0.995,
        product_name="Lámpara LED (copia nueva)", related_product_name="Lámpara LED",
    )
    data.update(over)
    return AuditLog(**data)


def _save(*rows: AuditLog) -> list[int]:
    with Session(engine) as s:
        for r in rows:
            s.add(r)
        s.commit()
        return [r.id for r in rows]


def _rows(action: str) -> list[AuditLog]:
    with Session(engine) as s:
        return list(s.exec(select(AuditLog).where(AuditLog.action == action)))


# ─── Qué se apaga: la regla pura ──────────────────────────────────────────────


def test_verify_flag_without_explicit_columns_has_nothing_to_disable():
    e = _verify_flag()
    assert disable_target_for(e) is None
    assert canonical_for(e) == ORIGINAL


def test_catalog_audit_flag_disables_the_newest_never_the_canonical():
    e = _audit_flag()
    assert disable_target_for(e) == NEWER
    assert canonical_for(e) == OLDER


def test_explicit_columns_win_over_source_heuristics():
    e = _verify_flag(source="luis", disable_target_id=NEWER, canonical_product_id=OLDER)
    assert disable_target_for(e) == NEWER
    assert canonical_for(e) == OLDER


def test_explicit_canonical_without_target_means_no_vendure_action():
    e = _audit_flag(disable_target_id=None, canonical_product_id=ORIGINAL)
    assert disable_target_for(e) is None


def test_target_equal_to_canonical_is_refused():
    e = _audit_flag(disable_target_id=OLDER, canonical_product_id=OLDER)
    assert disable_target_for(e) is None


# ─── confirm-duplicate ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirming_a_verify_flag_does_not_touch_vendure():
    """El corazón del bug: antes esto hacía disable_product('42') — el original."""
    (event_id,) = _save(_verify_flag())

    with Session(engine) as s:
        out = await routes.confirm_duplicate(event_id, s)

    assert FakeVendure.disabled == [], "no se apaga nada: el candidato nunca entró a Vendure"
    assert FakeVendure.status_reads == []
    assert out["ok"] is True
    assert out["vendure_action"] == "none"
    assert out["canonical_product_id"] == ORIGINAL

    with Session(engine) as s:
        entry = s.get(AuditLog, event_id)
        assert entry.dismissed is True
    confirmed = _rows("duplicate_confirmed")
    assert len(confirmed) == 1
    assert confirmed[0].canonical_product_id == ORIGINAL
    assert confirmed[0].disable_target_id is None
    assert _rows("duplicate_disabled") == []


@pytest.mark.asyncio
async def test_confirming_a_catalog_audit_flag_disables_the_newest():
    (event_id,) = _save(_audit_flag())

    with Session(engine) as s:
        out = await routes.confirm_duplicate(event_id, s)

    assert FakeVendure.disabled == [NEWER]
    assert OLDER not in FakeVendure.disabled
    assert out["action"] == "disabled"
    assert out["product_id"] == NEWER
    assert out["canonical_product_id"] == OLDER

    disabled = _rows("duplicate_disabled")
    assert len(disabled) == 1
    assert disabled[0].product_id == NEWER
    assert disabled[0].related_product_id == OLDER
    assert disabled[0].disable_target_id == NEWER
    assert disabled[0].canonical_product_id == OLDER


@pytest.mark.asyncio
async def test_confirming_uses_the_explicit_target_not_product_id():
    """Si la fila dice explícitamente a quién apagar, product_id no manda."""
    (event_id,) = _save(_audit_flag(product_id=OLDER, related_product_id=NEWER,
                                    disable_target_id=NEWER, canonical_product_id=OLDER))
    with Session(engine) as s:
        await routes.confirm_duplicate(event_id, s)
    assert FakeVendure.disabled == [NEWER]


@pytest.mark.asyncio
async def test_confirm_rejects_other_actions():
    from fastapi import HTTPException

    (event_id,) = _save(_verify_flag(action="verify_passed_to_paco"))
    with Session(engine) as s, pytest.raises(HTTPException) as exc:
        await routes.confirm_duplicate(event_id, s)
    assert exc.value.status_code == 400
    assert FakeVendure.disabled == []


# ─── bulk-confirm ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bulk_confirm_dry_run_separates_both_kinds():
    _save(_verify_flag(), _audit_flag())
    with Session(engine) as s:
        out = await routes.bulk_confirm_duplicates(min_confidence=0.99, confirm=False, session=s)
    assert out["would_disable"] == 1
    assert out["would_confirm_only"] == 1
    assert out["preview_ids"] == [NEWER]
    assert FakeVendure.disabled == []


@pytest.mark.asyncio
async def test_bulk_confirm_only_disables_catalog_duplicates():
    """Antes bulk-confirm apagaba TODOS los product_id, incluido el '42' original."""
    _save(_verify_flag(), _verify_flag(product_id="43"), _audit_flag())

    with Session(engine) as s:
        out = await routes.bulk_confirm_duplicates(min_confidence=0.99, confirm=True, session=s)

    assert FakeVendure.disabled == [NEWER]
    assert out["disabled"] == 1
    assert out["confirmed_only"] == 2
    assert out["failed"] == 0
    with Session(engine) as s:
        pending = list(s.exec(select(AuditLog).where(
            AuditLog.action == "duplicate_flagged", AuditLog.dismissed.is_not(True),
        )))
    assert pending == [], "los tres flags quedaron archivados"
    assert len(_rows("duplicate_confirmed")) == 2
    assert len(_rows("duplicate_disabled")) == 1


@pytest.mark.asyncio
async def test_bulk_confirm_respects_min_confidence():
    _save(_audit_flag(confidence=0.90))
    with Session(engine) as s:
        out = await routes.bulk_confirm_duplicates(min_confidence=0.99, confirm=True, session=s)
    assert out["disabled"] == 0
    assert FakeVendure.disabled == []


# ─── Las filas nuevas nacen con la semántica explícita ────────────────────────


def test_record_verify_marks_the_match_as_canonical_with_no_target():
    payload = routes.VerifyRequest(
        name="Lámpara LED", source_url="https://detail.1688.com/offer/777.html",
        image_urls=["https://img/1.jpg"], source="luis",
    )
    verdict = DedupVerdict(is_duplicate=True, confidence=1.0, matched_by=["url"],
                           per_strategy_scores={"url": 1.0, "image": 0.0, "text": 0.0},
                           candidate_id=ORIGINAL)
    routes._record_verify(payload, verdict, action="duplicate_flagged", detail="dup")

    rows = _rows("duplicate_flagged")
    assert len(rows) == 1
    row = rows[0]
    assert row.product_id == ORIGINAL
    assert row.canonical_product_id == ORIGINAL
    assert row.disable_target_id is None
    assert disable_target_for(row) is None


@pytest.mark.asyncio
async def test_audit_duplicates_rows_say_who_gets_disabled(monkeypatch):
    from app.dedup.orchestrator import DuplicatePair
    from app.scheduler import jobs
    from app.vendure.client import VendureProduct

    def _prod(pid):
        return VendureProduct(
            id=pid, name="Lámpara LED", slug=f"p{pid}", description="", enabled=True,
            source_url="https://detail.1688.com/offer/777.html", image_urls=[],
            product_code=None, featured_image_url=None, first_variant_price_cents=100,
            variant_count=1, updated_at="2026-10-01T00:00:00.000Z",
        )

    drop, keep = _prod(NEWER), _prod(OLDER)
    verdict = DedupVerdict(is_duplicate=True, confidence=1.0, matched_by=["url"],
                           per_strategy_scores={"url": 1.0, "image": 0.0, "text": 0.0})

    async def fake_flatten(client):  # noqa: ARG001
        return [drop, keep]

    async def fake_pairs(products, changed_ids=None):  # noqa: ARG001
        return [DuplicatePair(drop=drop, keep=keep, verdict=verdict)]

    monkeypatch.setattr(jobs, "VendureClient", lambda: object())
    monkeypatch.setattr(jobs, "_flatten_products", fake_flatten)
    monkeypatch.setattr(jobs, "find_duplicate_pairs", fake_pairs)

    await jobs.audit_duplicates()

    rows = _rows("duplicate_flagged")
    assert len(rows) == 1
    assert rows[0].product_id == NEWER
    assert rows[0].related_product_id == OLDER
    assert rows[0].disable_target_id == NEWER
    assert rows[0].canonical_product_id == OLDER
    assert disable_target_for(rows[0]) == NEWER


@pytest.mark.asyncio
async def test_catalog_audit_keeps_the_numerically_older_id():
    """'1000' < '999' como string: el canónico salía el producto NUEVO."""
    from app.dedup.orchestrator import find_duplicate_pairs
    from app.vendure.client import VendureProduct

    def _prod(pid):
        return VendureProduct(
            id=pid, name=f"Producto {pid}", slug=f"p{pid}", description="", enabled=True,
            source_url="https://detail.1688.com/offer/555.html", image_urls=[],
            product_code=None, featured_image_url=None, first_variant_price_cents=100,
            variant_count=1,
        )

    pairs = await find_duplicate_pairs([_prod("1000"), _prod("999")])
    assert len(pairs) == 1
    assert pairs[0].keep.id == "999"
    assert pairs[0].drop.id == "1000"


def test_humanize_exposes_the_semantics_to_the_dashboard():
    verify_view = routes._humanize(_verify_flag())
    assert verify_view["duplicate"] == {
        "disable_target_id": None, "canonical_product_id": ORIGINAL, "vendure_action": "none",
    }
    audit_view = routes._humanize(_audit_flag())
    assert audit_view["duplicate"] == {
        "disable_target_id": NEWER, "canonical_product_id": OLDER, "vendure_action": "disable",
    }
    assert routes._humanize(_verify_flag(action="price_flagged"))["duplicate"] is None


# ─── Casos borde (QA, auditoría oct-2026) ────────────────────────────────────


@pytest.mark.asyncio
async def test_bulk_confirm_vendure_failure_on_an_audit_event_does_not_block_verify_confirmations(monkeypatch):
    """Vendure caído: los de /verify (que no lo necesitan) igual se confirman y
    archivan; el de la auditoría queda pendiente, sin apagar nada."""
    class FlakyVendure(FakeVendure):
        async def get_enabled_status(self, product_id):
            raise RuntimeError("Vendure 502")

    monkeypatch.setattr(routes, "VendureClient", FlakyVendure)
    verify_id, audit_id = _save(_verify_flag(), _audit_flag())

    with Session(engine) as s:
        out = await routes.bulk_confirm_duplicates(min_confidence=0.99, confirm=True, session=s)

    assert out == {**out, "disabled": 0, "confirmed_only": 1, "failed": 1}
    assert out["failed_details"][0]["product_id"] == NEWER
    assert FakeVendure.disabled == []
    with Session(engine) as s:
        assert s.get(AuditLog, verify_id).dismissed is True
        assert s.get(AuditLog, audit_id).dismissed is not True, "queda pendiente para reintentar"
    assert len(_rows("duplicate_confirmed")) == 1


@pytest.mark.asyncio
async def test_a_client_sending_source_audit_through_verify_cannot_get_the_original_disabled():
    """`source` lo elige el cliente. Si alguien manda source="audit" por /verify, la
    fila nueva igual trae canonical sin target y confirmar no apaga nada."""
    payload = routes.VerifyRequest(
        name="Lámpara LED", source_url="https://detail.1688.com/offer/777.html",
        image_urls=["https://img/1.jpg"], source="audit",
    )
    verdict = DedupVerdict(is_duplicate=True, confidence=1.0, matched_by=["url"],
                           per_strategy_scores={"url": 1.0, "image": 0.0, "text": 0.0},
                           candidate_id=ORIGINAL)
    routes._record_verify(payload, verdict, action="duplicate_flagged", detail="dup")
    (row,) = _rows("duplicate_flagged")
    assert row.source == "audit" and row.related_product_id is None
    assert disable_target_for(row) is None

    with Session(engine) as s:
        out = await routes.confirm_duplicate(row.id, s)
    assert out["vendure_action"] == "none"
    assert FakeVendure.disabled == []


def test_old_audit_row_with_product_equal_to_related_has_nothing_to_disable():
    e = _audit_flag(product_id=OLDER, related_product_id=OLDER)
    assert disable_target_for(e) is None


def test_old_audit_row_without_related_is_not_a_catalog_pair():
    e = _audit_flag(related_product_id=None)
    assert disable_target_for(e) is None


def test_placeholder_target_is_never_disabled():
    from app.dedup.confirm_target import PLACEHOLDER_NEW

    e = _audit_flag(product_id=PLACEHOLDER_NEW)
    assert disable_target_for(e) is None


@pytest.mark.asyncio
async def test_bulk_confirm_with_both_kinds_touches_vendure_exactly_once_for_the_newest():
    """Dry-run y confirm cuentan lo mismo; Vendure solo ve lecturas/escrituras del más nuevo."""
    _save(_verify_flag(), _verify_flag(product_id="43"), _audit_flag())
    with Session(engine) as s:
        dry = await routes.bulk_confirm_duplicates(min_confidence=0.99, confirm=False, session=s)
        out = await routes.bulk_confirm_duplicates(min_confidence=0.99, confirm=True, session=s)
    assert (dry["would_disable"], dry["would_confirm_only"]) == (out["disabled"], out["confirmed_only"]) == (1, 2)
    assert FakeVendure.status_reads == [NEWER]
    assert FakeVendure.disabled == [NEWER]


@pytest.mark.asyncio
async def test_restore_re_enables_the_newest_that_was_disabled_not_the_canonical(monkeypatch):
    class RestoringVendure(FakeVendure):
        enabled: list[str] = []

        async def get_enabled_status(self, product_id):
            FakeVendure.status_reads.append(product_id)
            return product_id not in FakeVendure.disabled

        async def enable_product(self, product_id):
            RestoringVendure.enabled.append(product_id)

    RestoringVendure.enabled = []
    monkeypatch.setattr(routes, "VendureClient", RestoringVendure)
    (event_id,) = _save(_audit_flag())
    with Session(engine) as s:
        await routes.confirm_duplicate(event_id, s)
    assert FakeVendure.disabled == [NEWER]

    with Session(engine) as s:
        await routes.restore_duplicates(confirm=True, safe=True, session=s)
    assert RestoringVendure.enabled == [NEWER]
    assert OLDER not in RestoringVendure.enabled
