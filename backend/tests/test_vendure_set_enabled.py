"""`VendureClient.set_enabled` y `get_review_guard` (PR3 de la bandeja, H2), contra el doble HTTP de `review_fixtures`.

`set_enabled` devuelve el `enabled` que respondió Vendure; reactivar hace UN intento (más el re-login) y apagar reintenta
como siempre. `_set_enabled`, el de los flujos de duplicados, queda igual (K-3).
"""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from gql.transport.exceptions import TransportQueryError  # noqa: E402

from app.vendure.client import VendureClient  # noqa: E402
from tests.review_fixtures import FakeReviewVendure, install  # noqa: E402

FORBIDDEN = {"errors": [{"message": "You are not currently authorized", "extensions": {"code": "FORBIDDEN"}}]}
BOOM = {"errors": [{"message": "Internal error"}]}


@pytest.fixture
def fake(monkeypatch):
    f = FakeReviewVendure()
    install(monkeypatch, f)
    f.writable = True
    return f


@pytest.fixture
def relogins(monkeypatch):
    calls: list[int] = []

    async def login(self) -> bool:
        calls.append(1)
        return True

    monkeypatch.setattr(VendureClient, "_login", login)
    return calls


async def test_set_enabled_returns_what_vendure_answered(fake):
    fake.enabled["rv1"] = True
    assert await VendureClient().set_enabled("rv1", False) is False
    fake.write_reply["rv2"] = {"id": "rv2"}
    assert await VendureClient().set_enabled("rv2", False) is None
    fake.write_reply["rv3"] = {"id": "rv3", "enabled": "false"}
    assert await VendureClient().set_enabled("rv3", True) is None             # solo un bool cuenta


async def test_reactivating_tries_once_and_turning_off_keeps_the_retries(fake):
    fake.script["SetEnabled"] = [(500, BOOM)] * 3
    with pytest.raises(TransportQueryError):
        await VendureClient().set_enabled("rv1", True)
    assert len(fake.mutations) == 1
    fake.mutations.clear()
    fake.script["SetEnabled"] = [(500, BOOM), (500, BOOM)]
    fake.enabled["rv1"] = True
    assert await VendureClient().set_enabled("rv1", False) is False
    assert len(fake.mutations) == 3


async def test_reactivating_with_an_expired_bearer_relogs_and_tries_once_more(fake, relogins):
    fake.enabled["rv1"] = False
    fake.script["SetEnabled"] = [(200, FORBIDDEN)]
    assert await VendureClient().set_enabled("rv1", True) is True
    assert len(fake.mutations) == 2 and relogins == [1]


async def test_the_old_set_enabled_of_the_duplicate_flows_is_unchanged(fake):
    """K-3: `_set_enabled` sigue reintentando 3 veces y no devuelve nada (lo usan duplicados, BX y restaurar)."""
    fake.script["SetEnabled"] = [(500, BOOM)] * 3
    with pytest.raises(TransportQueryError):
        await VendureClient()._set_enabled("rv1", True)
    assert len(fake.mutations) == 3


async def test_the_review_guard_reads_enabled_approval_and_contact(fake):
    fake.enabled["rv1"], fake.approval["rv1"], fake.contact["rv1"] = False, " Rejected ", "+86 138 0000"
    g = await VendureClient().get_review_guard("rv1")
    assert (g.enabled, g.approval_status, g.has_supplier_contact) == (False, "rejected", True)
    fake.enabled["rv2"] = True
    g = await VendureClient().get_review_guard("rv2")
    assert (g.enabled, g.approval_status, g.has_supplier_contact) == (True, None, False)
    assert await VendureClient().get_review_guard("nope") is None
    assert all(r["kind"] == "query" for r in fake.requests)
    fake.unknown_fields.add("approvalStatus")
    with pytest.raises(TransportQueryError):
        await VendureClient().get_review_guard("rv1")
