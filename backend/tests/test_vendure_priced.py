"""Lectura de precios con tramos (VendureClient) para el semáforo.

Sin red: `_execute_with_retry` devuelve páginas armadas acá.
"""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

from graphql import print_ast  # noqa: E402

from app.vendure.client import VendureClient  # noqa: E402


def _raw_product(pid: str, enabled: bool = True) -> dict:
    return {
        "id": pid, "name": f"Producto {pid}", "slug": f"p{pid}", "description": "",
        "enabled": enabled, "customFields": {"b2boxProductCode": f"BX{pid}"},
        "featuredAsset": {"preview": f"https://cdn/{pid}-p.jpg", "source": f"https://cdn/{pid}.jpg"},
        "variantList": {"totalItems": 1, "items": [{
            "id": f"v{pid}", "name": "", "sku": f"SKU{pid}", "priceWithTax": 12100, "currencyCode": "ARS",
            "bulkPriceTiers": [
                {"position": 1, "enabled": True, "minQuantity": 50, "maxQuantity": None, "salePrice": 10000},
                {"position": 0, "enabled": True, "minQuantity": 1, "maxQuantity": 49, "salePrice": 15000},
                "basura",
            ],
        }]},
    }


async def test_fetch_all_products_priced_reads_tiers_with_low_concurrency(monkeypatch):
    seen: list[tuple[str, dict]] = []

    async def fake_execute(self, query, variables=None, *, what="", **kw):  # noqa: ARG001
        seen.append((print_ast(getattr(query, "document", query)), dict(variables or {})))
        skip = variables["skip"]
        items = [_raw_product(str(skip + i)) for i in range(variables["take"])] if skip < 250 else []
        return {"products": {"items": items[: max(0, 250 - skip)], "totalItems": 250}}

    monkeypatch.setattr(VendureClient, "_execute_with_retry", fake_execute)
    products = await VendureClient().fetch_all_products_priced()

    assert len(products) == 250
    query = seen[0][0]
    assert "bulkPriceTiers" in query and "salePrice" in query and "minQuantity" in query
    assert "mutation" not in query  # solo lectura
    p = products[0]
    [v] = p.priced_variants
    assert (v.id, v.price_with_tax_cents, v.currency) == ("v0", 12100, "ARS")
    assert [t.position for t in v.tiers] == [0, 1]  # ordenados por position
    assert (v.tiers[0].min_quantity, v.tiers[0].sale_price_cents) == (1, 15000)


async def test_concurrency_is_capped_at_two(monkeypatch):
    import asyncio

    active = {"now": 0, "max": 0}

    async def fake_execute(self, query, variables=None, *, what="", **kw):  # noqa: ARG001
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.01)
        active["now"] -= 1
        skip = variables["skip"]
        return {"products": {"items": [_raw_product(str(skip))] * variables["take"], "totalItems": 1000}}

    monkeypatch.setattr(VendureClient, "_execute_with_retry", fake_execute)
    await VendureClient().fetch_all_products_priced()
    assert active["max"] <= 2
