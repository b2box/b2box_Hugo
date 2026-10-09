"""Vendure falso A NIVEL HTTP para las pruebas de la auditoría de textos (HG1).

Se engancha en el transport de gql con un `MockTransport`, o sea que el cliente
real de Hugo arma sus requests de verdad (headers, JSON, paginado) y lo que
contesta este objeto es lo único que "existe" de Vendure. Anota cada pedido:
las pruebas miran ahí que no haya ninguna mutation.
"""

from __future__ import annotations

import json
from typing import Any

from gql import Client
from gql.transport.httpx import HTTPXAsyncTransport

from app.vendure import client as vendure_client
from app.vendure.client import VendureClient

_httpx = vendure_client._gql_httpx


def raw_product(
    pid: str | int,
    *,
    translations: list[tuple[str, str, str, str]] | None = None,
    enabled: bool = True,
    code: str | None = None,
    business: str | None = None,
    size_model: str | None = None,
    link: str | None = None,
) -> dict[str, Any]:
    """Un producto como lo devuelve la Admin API. `translations`: (idioma, nombre, slug, descripción)."""
    if translations is None:
        translations = [("es_AR", f"Producto {pid}", f"producto-{pid}", f"Descripción larga y útil del producto {pid}.")]
    return {
        "id": str(pid),
        "enabled": enabled,
        "updatedAt": "2026-10-01T12:00:00.000Z",
        "translations": [
            {"languageCode": lang, "name": name, "slug": slug, "description": desc}
            for lang, name, slug, desc in translations
        ],
        "customFields": {
            "supplierBusiness": business,
            "supplierSizeModel": size_model,
            "supplierLink": link,
            "b2boxProductCode": code,
        },
    }


class FakeVendure:
    """`channels`: {token del canal (None = canal por defecto): [productos crudos]}."""

    def __init__(self, channels: dict[str | None, list[dict[str, Any]]]) -> None:
        self.channels = channels
        self.requests: list[dict[str, Any]] = []
        self.fail_tokens: set[str | None] = set()
        self.reject_supplier_fields = False

    # ── el servidor ──
    def handler(self, request: Any) -> Any:
        body = json.loads(request.content)
        token = request.headers.get("vendure-token")
        self.requests.append({
            "token": token, "query": body["query"], "variables": body.get("variables") or {},
            "method": request.method, "url": str(request.url),
        })
        if token in self.fail_tokens:
            return _httpx.Response(500, text="Internal Server Error")
        if self.reject_supplier_fields and "supplierBusiness" in body["query"]:
            return _httpx.Response(200, json={"errors": [{
                "message": 'Cannot query field "supplierBusiness" on type "ProductCustomFields".'}]})
        items = self.channels[token]
        skip, take = body["variables"]["skip"], body["variables"]["take"]
        page = [self._only_requested(it, body["query"]) for it in items[skip:skip + take]]
        return _httpx.Response(200, json={"data": {"products": {"items": page, "totalItems": len(items)}}})

    @staticmethod
    def _only_requested(item: dict[str, Any], query: str) -> dict[str, Any]:
        """Como un GraphQL de verdad: los custom fields que la query no pidió no vuelven."""
        custom = {k: v for k, v in (item.get("customFields") or {}).items() if k in query}
        return {**item, "customFields": custom}

    # ── lo que miran las pruebas ──
    def queries(self, token: str | None = ...) -> list[dict[str, Any]]:  # type: ignore[assignment]
        return [r for r in self.requests if token is ... or r["token"] == token]

    def writes(self) -> list[dict[str, Any]]:
        return [r for r in self.requests
                if r["method"] != "POST" or not r["query"].lstrip().startswith("query")
                or "mutation" in r["query"].lower()]


def install(monkeypatch, fake: FakeVendure) -> None:
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
    monkeypatch.setattr(vendure_client.asyncio, "sleep", _no_sleep)   # los reintentos no esperan
