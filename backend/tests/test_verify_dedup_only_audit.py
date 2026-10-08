"""dedup_only y el dashboard: la fila de AuditLog que deja una consulta.

Cada consulta dedup_only sin match deja un `verify_no_match` con imagen. Antes el
dashboard lo mostraba como "sin imagen para mandar a Paco" con un botón
"Reintentar a Paco": un click pagaba la segunda búsqueda que dedup_only evita.
Acá se usa la DB real de tests (sin mockear _record_verify).
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.api import routes  # noqa: E402
from app.db.models import AuditLog  # noqa: E402
from app.db.session import engine, init_db  # noqa: E402
from app.dedup.orchestrator import DedupVerdict  # noqa: E402

SOURCE_URL = "https://detail.1688.com/offer/777.html"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    init_db()
    with Session(engine) as s:
        for row in s.exec(select(AuditLog)).all():
            s.delete(row)
        s.commit()

    paco_calls: list[str] = []

    async def fake_catalog(force=False, full=False):  # noqa: ARG001
        return []

    async def fake_find(candidate, existing):  # noqa: ARG001
        return DedupVerdict(is_duplicate=False, confidence=0.0)

    class _Result:
        search_id = "paco-1"
        status = "queued"

    async def fake_submit(image_url, **kw):  # noqa: ARG001
        paco_calls.append("submit")
        return _Result()

    async def fake_submit_pro(image_url, **kw):  # noqa: ARG001
        paco_calls.append("submit_pro")
        return _Result()

    monkeypatch.setattr(routes.vendure_catalog, "get_catalog", fake_catalog)
    monkeypatch.setattr(routes, "find_duplicate_in", fake_find)
    monkeypatch.setattr(routes.paco_integration, "submit", fake_submit)
    monkeypatch.setattr(routes.paco_integration, "submit_pro", fake_submit_pro)
    return paco_calls


def _request(**over) -> routes.VerifyRequest:
    data = dict(name="Organizador de cocina", source_url=SOURCE_URL,
                image_urls=["https://img/1.jpg"], source="luis")
    data.update(over)
    return routes.VerifyRequest(**data)


def _rows(action: str) -> list[AuditLog]:
    with Session(engine) as s:
        return list(s.exec(select(AuditLog).where(AuditLog.action == action)).all())


# ── La fila que deja la consulta ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_la_consulta_deja_una_fila_archivada_y_marcada_dedup_only(env):
    await routes.verify(_request(dedup_only=True))

    [row] = _rows("verify_no_match")
    assert row.dismissed is True and row.dismissed_at is not None
    assert json.loads(row.verify_ctx)["dedup_only"] is True
    assert row.product_image_url == "https://img/1.jpg"
    assert env == []


@pytest.mark.asyncio
async def test_no_llena_la_bandeja_pero_se_puede_ver_archivada():
    await routes.verify(_request(dedup_only=True))
    with Session(engine) as s:
        default = await routes.list_audit_log(session=s)
        assert default["total"] == 0 and default["items"] == []
        with_all = await routes.list_audit_log(include_dismissed=True, session=s)
    [item] = with_all["items"]
    assert item["dedup_only"] is True and item["dismissed"] is True


@pytest.mark.asyncio
async def test_el_titulo_no_dice_que_falta_la_imagen():
    await routes.verify(_request(dedup_only=True))
    with Session(engine) as s:
        [item] = (await routes.list_audit_log(include_dismissed=True, session=s))["items"]
    assert "sin imagen" not in item["title"].lower()
    assert "consulta de duplicados" in item["title"].lower()
    assert "no lo mandó a Paco" in item["title"]
    assert "Hugo no lo mandó a Paco" in item["detail"]
    assert "dedup-only, consultado por luis" in item["detail"]


@pytest.mark.asyncio
async def test_sin_el_flag_el_ctx_no_trae_dedup_only():
    await routes.verify(_request())  # camino de siempre: reenvía a Paco
    [row] = _rows("verify_passed_to_paco")
    assert "dedup_only" not in json.loads(row.verify_ctx)
    assert row.dismissed is False


# ── Reintentar a Paco ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_paco_de_una_consulta_dedup_only_es_409_y_no_paga_otra_busqueda(env):
    await routes.verify(_request(dedup_only=True))
    [row] = _rows("verify_no_match")

    with Session(engine) as s:
        with pytest.raises(HTTPException) as exc:
            await routes.retry_paco(row.id, s)
    assert exc.value.status_code == 409
    assert "dedup_only" in exc.value.detail

    assert env == []                                   # Paco no se tocó
    assert _rows("verify_passed_to_paco") == []        # ni quedó una fila de "enviado"
    assert len(_rows("verify_no_match")) == 1


@pytest.mark.asyncio
async def test_retry_paco_del_verify_no_match_sin_imagen_sigue_andando(env):
    """El otro origen de verify_no_match (producto nuevo sin imagen) no cambia."""
    with Session(engine) as s:
        row = AuditLog(
            action="verify_no_match", source="luis", product_id="(nuevo)", product_name="x",
            product_image_url="https://img/2.jpg", product_source_url=SOURCE_URL,
            verify_ctx=json.dumps({"source": "luis", "callback_ctx": None, "text_specs": "", "use_browser": False}),
        )
        s.add(row)
        s.commit()
        event_id = row.id
    with Session(engine) as s:
        out = await routes.retry_paco(event_id, s)
    assert out["ok"] is True and out["paco"] == "APP"
    assert env == ["submit"]


@pytest.mark.asyncio
async def test_verify_no_match_comun_conserva_su_titulo_y_sigue_a_la_vista():
    with Session(engine) as s:
        s.add(AuditLog(
            action="verify_no_match", source="luis", product_id="(nuevo)", product_name="x",
            product_image_url="https://img/2.jpg",
        ))
        s.commit()
        [item] = (await routes.list_audit_log(session=s))["items"]
    assert item["title"] == "Producto nuevo · sin imagen para mandar a Paco"
    assert item["dedup_only"] is False
