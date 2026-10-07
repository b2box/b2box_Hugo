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
