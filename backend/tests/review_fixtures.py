"""Vendure falso A NIVEL HTTP para la bandeja de revisión (PR1 a PR3).

Se engancha en el transport de gql con un `MockTransport`: el cliente real de Hugo arma sus
requests de verdad (headers, JSON, paginado) y lo único que "existe" de Vendure es lo que contesta
este objeto. Anota cada pedido. **Cualquier `mutation` queda registrada en `mutations` y, salvo que el
test ponga `writable = True`, se contesta con un error 500**: las pruebas de "cero escrituras" miran ahí.
Con `writable`, `SetEnabled` cambia `enabled[product_id]` y devuelve el valor (o lo que diga `write_reply`).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from gql import Client
from gql.transport.httpx import HTTPXAsyncTransport

from app.vendure import client as vendure_client
from app.vendure.client import VendureClient

_httpx = vendure_client._gql_httpx
GOLDEN = Path(__file__).parent / "golden"
ENGINE_FIXTURE = json.loads((GOLDEN / "breakdowns_engine_fdd7579df.json").read_text())

_OP = re.compile(r"^\s*(query|mutation|subscription)\s+(\w+)?", re.I)


def engine_variant(index: int) -> dict[str, Any]:
    """Uno de los 3 productos del fixture del engine (0 organizador, 1 taza, 2 cargador con certificado)."""
    return ENGINE_FIXTURE["variants"][index]


class FakeReviewVendure:
    """Responde las queries que usa la bandeja. `breakdowns`: {variantId: [tramos crudos]};
    `variants`: {variantId: variante cruda de `productVariant`}; `certifications`: {productId: crudo|None}."""

    def __init__(self) -> None:
        self.products: list[dict[str, Any]] = []
        self.breakdowns: dict[str, list[dict[str, Any]]] = {}
        self.variants: dict[str, dict[str, Any]] = {}
        self.certifications: dict[str, dict[str, Any] | None] = {}
        self.channel: dict[str, Any] = {"id": "2", "customFields": {"minimumPurchaseCny": 500}}
        self.profile: dict[str, Any] | None = {"certificateMaxSharePct": 20.0, "containerCbm": 30.0}
        self.requests: list[dict[str, Any]] = []
        self.mutations: list[dict[str, Any]] = []
        self.errors: dict[str, str] = {}       # operación -> mensaje de error GraphQL (HTTP 200)
        self.unknown_fields: set[str] = set()  # campos que el schema "no tiene": si la query los pide, error
        self.http_fail: set[str] = set()       # operación -> HTTP 500
        # Lo que lee y escribe la bandeja al decidir (GetEnabled, ReviewGuard, SetEnabled).
        self.enabled: dict[str, bool] = {}
        self.approval: dict[str, str | None] = {}
        self.contact: dict[str, str | None] = {}
        self.writable = False
        self.write_reply: dict[str, dict[str, Any]] = {}            # productId -> `updateProduct` que se devuelve tal cual
        self.script: dict[str, list[tuple[int, dict[str, Any]]]] = {}  # operación -> respuestas en orden, antes de lo normal

    # ── el servidor ──
    def handler(self, request: Any) -> Any:
        body = json.loads(request.content)
        query = body["query"]
        variables = body.get("variables") or {}
        match = _OP.match(query)
        kind, op = (match.group(1).lower(), match.group(2) or "") if match else ("?", "")
        record = {"kind": kind, "op": op, "query": query, "variables": variables,
                  "method": request.method, "token": request.headers.get("vendure-token")}
        self.requests.append(record)
        if kind != "query":
            self.mutations.append(record)
            if not self.writable:
                return _httpx.Response(500, json={"errors": [{"message": "escritura prohibida en este test"}]})
        if self.script.get(op):
            status, body = self.script[op].pop(0)
            return _httpx.Response(status, json=body)
        if op in self.http_fail:
            return _httpx.Response(500, text="Internal Server Error")
        if op in self.errors:
            return _httpx.Response(200, json={"errors": [{"message": self.errors[op]}]})
        for name in sorted(self.unknown_fields):
            if re.search(rf"\b{name}\b", query):
                return _httpx.Response(200, json={"errors": [{
                    "message": f'Cannot query field "{name}" on type "Anything".'}]})
        return _httpx.Response(200, json={"data": self._data(op, variables)})

    def _data(self, op: str, v: dict[str, Any]) -> dict[str, Any]:
        if op == "Products":
            skip, take = v["skip"], v["take"]
            return {"products": {"items": self.products[skip:skip + take], "totalItems": len(self.products)}}
        if op == "TierBreakdowns":
            return {"bulkPriceTierBreakdowns": self.breakdowns.get(str(v["id"]), [])}
        if op == "PricedVariant":
            return {"productVariant": self.variants.get(str(v["id"]))}
        if op == "ProductCertification":
            return {"productCertification": self.certifications.get(str(v["productId"]))}
        if op == "ActiveChannelPricing":
            return {"activeChannel": self.channel}
        if op == "FiscalProfile":
            return {"pricingFiscalProfile": self.profile}
        pid = str(v.get("id") or (v.get("input") or {}).get("id"))
        if op == "GetEnabled":
            return {"product": {"id": pid, "enabled": self.enabled[pid]} if pid in self.enabled else None}
        if op == "ReviewGuard":
            return {"product": None if pid not in self.enabled else {
                "id": pid, "enabled": self.enabled[pid],
                "customFields": {"approvalStatus": self.approval.get(pid), "supplierContact": self.contact.get(pid)}}}
        if op == "SetEnabled":
            if pid in self.write_reply:
                return {"updateProduct": self.write_reply[pid]}
            self.enabled[pid] = bool(v["input"]["enabled"])
            return {"updateProduct": {"id": pid, "enabled": self.enabled[pid]}}
        raise AssertionError(f"consulta inesperada a Vendure: {op}")

    # ── lo que miran las pruebas ──
    def ops(self) -> list[str]:
        return [r["op"] for r in self.requests]

    def count(self, op: str) -> int:
        return sum(1 for r in self.requests if r["op"] == op)

    def writes(self) -> list[bool]:
        """Los `enabled` que se pidieron escribir, en orden."""
        return [bool(r["variables"]["input"]["enabled"]) for r in self.mutations if r["op"] == "SetEnabled"]


def install(monkeypatch, fake: FakeReviewVendure) -> None:
    """Hace que todo VendureClient hable con `fake` en vez de con la red."""

    def _new_client(self: VendureClient) -> Client:
        headers = {"Authorization": f"Bearer {self._bearer}"}
        if self._channel_token:
            headers["vendure-token"] = self._channel_token
        transport = HTTPXAsyncTransport(
            url=self._url, headers=headers, timeout=vendure_client._GQL_TIMEOUT,
            transport=_httpx.MockTransport(fake.handler),
        )
        return Client(transport=transport, fetch_schema_from_transport=False, execute_timeout=30.0)

    async def _no_sleep(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(VendureClient, "_new_client", _new_client)
    monkeypatch.setattr(VendureClient, "_shared_bearer", "bearer-de-prueba")
    monkeypatch.setattr(VendureClient, "_dims_supported", True)
    monkeypatch.setattr(VendureClient, "_review_supported", True)
    monkeypatch.setattr(vendure_client.asyncio, "sleep", _no_sleep)   # los reintentos no esperan
