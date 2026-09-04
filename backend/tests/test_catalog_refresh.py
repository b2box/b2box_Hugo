"""
El cache del catálogo Vendure se mantiene con refrescos INCREMENTALES
(productos con updatedAt > watermark) y solo hace un full refresh cada
`catalog_full_refresh_seconds` o cuando detecta drift (totalItems != cache).

Regresión de: refresh_catalog bajaba TODO el catálogo cada 4,5 min y tenía al
PG del admin-server de Vendure encolado (Login 1,6 s, GetOrderDetails 4-9 s).
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from gql.transport.exceptions import TransportQueryError

from app.vendure import catalog
from app.vendure.client import VendureProduct


def _prod(pid: str, updated: str, name: str | None = None) -> VendureProduct:
    return VendureProduct(
        id=pid, name=name or f"p{pid}", slug=f"p{pid}", description="", enabled=True,
        source_url=None, image_urls=[], product_code=None, featured_image_url=None,
        first_variant_price_cents=100, variant_count=1, updated_at=updated,
    )


class FakeClient:
    """Simula Vendure: `remote` es la verdad; cuenta qué tipo de fetch se hizo."""

    remote: dict[str, VendureProduct] = {}
    full_calls = 0
    incremental_calls = 0
    count_calls = 0
    last_since: str | None = None

    async def fetch_all_products(self, with_variants=False, page_size=None, concurrency=None):
        FakeClient.full_calls += 1
        assert concurrency == catalog._FULL_REFRESH_CONCURRENCY
        return list(FakeClient.remote.values())

    async def fetch_products_updated_since(self, since_iso, with_variants=False, page_size=None):
        FakeClient.incremental_calls += 1
        FakeClient.last_since = since_iso
        return [p for p in FakeClient.remote.values() if p.updated_at > since_iso]

    async def count_products(self):
        FakeClient.count_calls += 1
        return len(FakeClient.remote)


@pytest.fixture
def fake_vendure(monkeypatch):
    FakeClient.remote = {
        "1": _prod("1", "2026-09-01T10:00:00.000Z"),
        "2": _prod("2", "2026-09-01T11:00:00.000Z"),
    }
    FakeClient.full_calls = FakeClient.incremental_calls = FakeClient.count_calls = 0
    FakeClient.last_since = None
    monkeypatch.setattr(catalog, "VendureClient", FakeClient)
    monkeypatch.setattr(
        catalog, "get_settings",
        lambda: SimpleNamespace(verify_catalog_ttl_seconds=300, catalog_full_refresh_seconds=43200),
    )
    # Estado limpio del módulo
    monkeypatch.setattr(catalog, "_cache", [])
    monkeypatch.setattr(catalog, "_loaded_at", 0.0)
    monkeypatch.setattr(catalog, "_full_at", None)
    monkeypatch.setattr(catalog, "_watermark", None)
    return FakeClient


@pytest.mark.asyncio
async def test_first_load_is_full_and_sets_watermark(fake_vendure):
    products = await catalog.get_catalog()
    assert {p.id for p in products} == {"1", "2"}
    assert fake_vendure.full_calls == 1
    assert fake_vendure.incremental_calls == 0
    assert catalog._watermark == "2026-09-01T11:00:00.000Z"


@pytest.mark.asyncio
async def test_forced_refresh_is_incremental_and_merges(fake_vendure):
    await catalog.get_catalog()
    # Vendure: producto 2 editado, producto 3 nuevo
    fake_vendure.remote["2"] = _prod("2", "2026-09-02T09:00:00.000Z", name="editado")
    fake_vendure.remote["3"] = _prod("3", "2026-09-02T09:30:00.000Z")

    products = await catalog.get_catalog(force=True)

    assert fake_vendure.full_calls == 1, "el refresh forzado no debe bajar todo"
    assert fake_vendure.incremental_calls == 1
    # since = watermark - 2 min de margen
    assert fake_vendure.last_since == "2026-09-01T10:58:00Z"
    by_id = {p.id: p for p in products}
    assert by_id["2"].name == "editado"
    assert "3" in by_id
    assert [p.id for p in products] == ["1", "2", "3"], "orden original + nuevos al final"
    assert catalog._watermark == "2026-09-02T09:30:00.000Z"


@pytest.mark.asyncio
async def test_no_changes_costs_no_full(fake_vendure):
    await catalog.get_catalog()
    for _ in range(5):
        await catalog.get_catalog(force=True)
    assert fake_vendure.full_calls == 1
    assert fake_vendure.incremental_calls == 5
    assert fake_vendure.count_calls == 5


@pytest.mark.asyncio
async def test_deleted_product_triggers_full(fake_vendure):
    await catalog.get_catalog()
    del fake_vendure.remote["1"]  # borrado en Vendure: updatedAt no lo delata

    products = await catalog.get_catalog(force=True)

    assert fake_vendure.incremental_calls == 1
    assert fake_vendure.full_calls == 2, "totalItems != cache → full"
    assert {p.id for p in products} == {"2"}


@pytest.mark.asyncio
async def test_full_interval_forces_full(fake_vendure, monkeypatch):
    await catalog.get_catalog()
    monkeypatch.setattr(catalog, "_full_at", time.monotonic() - 43201)

    await catalog.get_catalog(force=True)

    assert fake_vendure.full_calls == 2
    assert fake_vendure.incremental_calls == 0


@pytest.mark.asyncio
async def test_explicit_full_flag(fake_vendure):
    await catalog.get_catalog()
    await catalog.get_catalog(full=True)
    assert fake_vendure.full_calls == 2
    assert fake_vendure.incremental_calls == 0


@pytest.mark.asyncio
async def test_failure_keeps_stale_cache(fake_vendure, monkeypatch):
    await catalog.get_catalog()

    async def boom(self, *a, **k):
        raise TimeoutError("vendure lento")

    monkeypatch.setattr(fake_vendure, "fetch_products_updated_since", boom)
    products = await catalog.get_catalog(force=True)
    assert {p.id for p in products} == {"1", "2"}


@pytest.mark.asyncio
async def test_invalidate_then_get_is_incremental(fake_vendure):
    await catalog.get_catalog()
    fake_vendure.remote["1"] = _prod("1", "2026-09-03T00:00:00.000Z", name="deshabilitado")
    catalog.invalidate()

    products = await catalog.get_catalog()

    assert fake_vendure.full_calls == 1
    assert fake_vendure.incremental_calls == 1
    assert {p.id: p.name for p in products}["1"] == "deshabilitado"


@pytest.mark.asyncio
async def test_graphql_rejection_falls_back_to_full(fake_vendure, monkeypatch):
    """Si Vendure rechaza el filtro updatedAt (schema distinto), no nos quedamos
    con cache viejo: se hace full como antes."""
    await catalog.get_catalog()
    fake_vendure.remote["3"] = _prod("3", "2026-09-02T09:30:00.000Z")

    async def rejected(self, *a, **k):
        raise TransportQueryError("Unknown argument 'filter'")

    monkeypatch.setattr(fake_vendure, "fetch_products_updated_since", rejected)
    products = await catalog.get_catalog(force=True)
    assert fake_vendure.full_calls == 2
    assert {p.id for p in products} == {"1", "2", "3"}
