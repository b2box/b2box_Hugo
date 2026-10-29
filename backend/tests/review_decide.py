"""Kit para los tests de las decisiones de la bandeja (PR3): Vendure falso que acepta escrituras, una persona que decide
(PRICE_REVIEW_DECIDERS) y helpers para mandar los POST."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import Session, select

from app import auth
from app import main as main_mod
from app.db.models import AuditLog, PriceReviewEvent
from app.db.session import engine
from tests import review_world as w
from tests.review_fixtures import FakeReviewVendure, install
from tests.test_price_monitor_routes import _use_settings

BASE = "/api/price-review"
PAO = "pao@b2box.pro"
DECIDERS = "pao@b2box.pro, gabriel@b2box.pro"


def setup(monkeypatch, *, mode: int = 1, **settings: Any) -> FakeReviewVendure:
    """Base limpia, `pm_mode`, Pao y Gabriel deciden (salvo que se pise) y un Vendure falso que acepta escrituras."""
    w.reset_db()
    clean_audit()
    w.set_setting("pm_mode", mode)
    _use_settings(monkeypatch, **{"price_review_deciders": DECIDERS, **settings})
    fake = FakeReviewVendure()
    install(monkeypatch, fake)
    fake.writable = True
    return fake


def clean_audit() -> None:
    with Session(engine) as s:
        s.execute(delete(AuditLog).where(AuditLog.source == "price_review"))  # type: ignore[arg-type]
        s.execute(delete(AuditLog).where(AuditLog.product_id.like("rv%")))  # type: ignore[attr-defined]
        s.commit()


def client(user: str | None = PAO) -> TestClient:
    c = TestClient(main_mod.app, raise_server_exceptions=False)
    if user is not None:
        c.cookies.set(auth.COOKIE_NAME, auth.issue_session_token(user))
    return c


def post(c: TestClient, item_id: int, path: str, **body: Any):
    return c.post(f"{BASE}/items/{item_id}/{path}", json=body)


def decide(c: TestClient, item_id: int, decision: str, expected_state: str, code: str | None = None,
           text: str | None = None):
    return post(c, item_id, "decision", decision=decision, expected_state=expected_state, motive_code=code, motive_text=text)


def kinds(item_id: int) -> list[str]:
    with Session(engine) as s:
        return [e.kind for e in s.exec(select(PriceReviewEvent).where(PriceReviewEvent.item_id == item_id)
                                       .order_by(PriceReviewEvent.id)).all()]  # type: ignore[arg-type]


def audit(product_id: str) -> list[AuditLog]:
    with Session(engine) as s:
        return list(s.exec(select(AuditLog).where(AuditLog.product_id == product_id).order_by(AuditLog.id)).all())  # type: ignore[arg-type]


def disabled_by_hugo(pid: str, state: str = "en_revision", **fields: Any) -> int:
    """Una ficha cuyo producto apagó Hugo (como la deja «Inactivar y revisar»)."""
    return w.make_item(pid, state, hugo_disabled=True, enabled_before=True, disabled_at=w.NOW, **fields)
