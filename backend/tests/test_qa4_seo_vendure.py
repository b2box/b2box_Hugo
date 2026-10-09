"""QA independiente de HG1: lo que Hugo le pide a Vendure.

A nivel HTTP (MockTransport), sin tocar el código de producción:
  * ninguna mutation sale hacia Vendure, salvo el `login` que ya existía (y solo si no hay bearer);
  * la paginación lee todo (0, 1, 99, 100, 101, 200, 201, 250 productos), sin saltear ni repetir;
  * un canal caído (HTTP 500, error GraphQL, página 2 que falla, canal que no contesta) deja la corrida
    `degraded` con el motivo y no inventa filas del canal caído; los dos caídos es `failed`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app.seo import text_audit  # noqa: E402
from app.vendure import client as vendure_client  # noqa: E402
from tests.seo_fixtures import FakeVendure, install, raw_product  # noqa: E402
from tests.test_seo_text_audit import env, rows_of, run  # noqa: E402,F401  (fixture `env` autouse)

_httpx = vendure_client._gql_httpx


def _catalog(n: int, *, start: int = 1) -> list[dict]:
    return [raw_product(i) for i in range(start, start + n)]


# ─── Solo lectura a nivel HTTP ─────────────────────────────────────

def test_el_login_es_la_unica_mutation_y_solo_si_no_hay_bearer(monkeypatch):
    """Sin bearer, Hugo se loguea (mutation `login`, la que ya existía) y después solo salen queries."""
    fake = FakeVendure({"ar": _catalog(3), None: _catalog(3)})
    install(monkeypatch, fake)
    monkeypatch.setattr(vendure_client.VendureClient, "_shared_bearer", "")
    st = vendure_client.get_settings()
    monkeypatch.setattr(st, "vendure_user", "robot", raising=False)
    monkeypatch.setattr(st, "vendure_pass", "clave", raising=False)
    monkeypatch.setattr(st, "vendure_bearer", "", raising=False)

    logins: list[dict] = []

    def login_handler(request):
        body = json.loads(request.content)
        logins.append({"query": body["query"], "headers": dict(request.headers)})
        return _httpx.Response(
            200, json={"data": {"login": {"__typename": "CurrentUser", "id": "1", "identifier": "robot"}}},
            headers={"vendure-auth-token": "bearer-nuevo"},
        )

    real_async_client = _httpx.AsyncClient
    monkeypatch.setattr(
        vendure_client.httpx, "AsyncClient",
        lambda **kw: real_async_client(transport=_httpx.MockTransport(login_handler), **{k: v for k, v in kw.items() if k == "timeout"}),
    )
    result = asyncio.run(text_audit.run_text_audit(trigger="manual"))
    assert result["status"] == "ok"
    assert logins, "esperaba un login: no había bearer"
    for lg in logins:
        assert lg["query"].lstrip().startswith("mutation Login(")
        assert "updateProduct" not in lg["query"] and "deleteProduct" not in lg["query"]
    # Lo demás: solo queries de lectura, todas con el bearer recién obtenido.
    assert fake.requests and fake.writes() == []
    for req in fake.requests:
        assert req["query"].lstrip().startswith("query TextAuditProducts(")
        assert "mutation" not in req["query"].lower()


def test_ni_un_pedido_fuera_del_endpoint_de_vendure_ni_con_otro_metodo(monkeypatch):
    fake = FakeVendure({"ar": _catalog(120), None: _catalog(130)})
    result = run(monkeypatch, fake)
    assert result["status"] == "ok"
    assert {r["url"] for r in fake.requests} == {"https://example.invalid/admin-api"}
    assert {r["method"] for r in fake.requests} == {"POST"}
    for r in fake.requests:
        assert r["query"].count("{") == r["query"].count("}")
        assert "mutation" not in r["query"].lower() and "subscription" not in r["query"].lower()
        assert "variantList" not in r["query"], "la auditoría no debe pedir variantes (N+1 del lado de Vendure)"


def test_cada_canal_manda_su_token_y_el_por_defecto_ninguno(monkeypatch):
    fake = FakeVendure({"ar": _catalog(2), None: _catalog(2)})
    run(monkeypatch, fake)
    assert {r["token"] for r in fake.requests} == {"ar", None}
    assert len(fake.queries("ar")) == 1 and len(fake.queries(None)) == 1


# ─── Paginación ────────────────────────────────────────────────────

@pytest.mark.parametrize("n", [0, 1, 99, 100, 101, 199, 200, 201, 250])
def test_la_paginacion_lee_todo_sin_saltear_ni_repetir(monkeypatch, n):
    fake = FakeVendure({"ar": _catalog(n), None: _catalog(n)})
    result = run(monkeypatch, fake)
    assert result["status"] == "ok"
    assert result["products_total"] == n, f"con {n} productos leyó {result['products_total']}"
    for token in ("ar", None):
        reqs = fake.queries(token)
        expected_pages = max(1, math.ceil(n / 100))
        assert len(reqs) == expected_pages, f"canal {token}: {len(reqs)} pedidos para {n} productos"
        assert [r["variables"]["skip"] for r in reqs] == [i * 100 for i in range(expected_pages)]
        assert {r["variables"]["take"] for r in reqs} == {100}
    ids = [pid for pid, _lang in rows_of(result["id"])]
    assert len(ids) == len(set(ids)) == n


def test_las_paginas_se_piden_en_orden_estable_por_id(monkeypatch):
    fake = FakeVendure({"ar": _catalog(150), None: _catalog(150)})
    run(monkeypatch, fake)
    for r in fake.requests:
        assert "sort: { id: ASC }" in r["query"], "sin orden estable, skip/take puede repetir o saltear productos"


def test_un_producto_en_ar_y_en_default_cuenta_una_vez_y_el_que_solo_esta_en_default_se_marca(monkeypatch):
    ar = _catalog(105)
    default = _catalog(105) + _catalog(3, start=1000)          # 3 productos que AR no tiene
    result = run(monkeypatch, FakeVendure({"ar": ar, None: default}))
    assert result["products_total"] == 108
    rows = rows_of(result["id"])
    assert sum(1 for r in rows.values() if r.in_ar is False and r.in_default is True) == 3
    assert sum(1 for r in rows.values() if r.in_ar is True and r.in_default is True) == 105


def test_la_paginacion_corta_si_vendure_deja_de_devolver_items(monkeypatch):
    """Defensa contra un bucle infinito: si totalItems miente (dice 10.000) pero no hay más, termina."""
    cat = {"ar": _catalog(100), None: _catalog(100)}

    class Liar(FakeVendure):
        def handler(self, request):
            resp = super().handler(request)
            if resp.status_code == 200:
                data = json.loads(resp.content)
                data["data"]["products"]["totalItems"] = 10_000
                return _httpx.Response(200, json=data)
            return resp

    fake = Liar(cat)
    result = run(monkeypatch, fake)
    assert result["status"] == "ok" and result["products_total"] == 100
    assert len(fake.requests) <= 6


# ─── Canales caídos ────────────────────────────────────────────────

def test_un_canal_con_error_http_deja_la_corrida_incompleta_con_motivo(monkeypatch):
    fake = FakeVendure({"ar": _catalog(5), None: _catalog(5)})
    fake.fail_tokens = {"ar"}
    result = run(monkeypatch, fake)
    assert result["status"] == "degraded"
    assert result["channels_ok"] == ["default"]
    assert set(result["channels_failed"]) == {"ar"}
    assert "500" in result["channels_failed"]["ar"] or "Server" in result["channels_failed"]["ar"]
    rows = rows_of(result["id"])
    assert len(rows) == 5 and all(r.in_ar is None and r.in_default is True for r in rows.values()), \
        "el canal caído no se puede contar como 'el producto no está en AR'"


def test_un_error_graphql_en_un_canal_tambien_es_incompleta(monkeypatch, caplog):
    class GraphqlError(FakeVendure):
        def handler(self, request):
            if request.headers.get("vendure-token") is None:
                self.requests.append({"token": None, "query": json.loads(request.content)["query"],
                                      "variables": {}, "method": request.method, "url": str(request.url)})
                return _httpx.Response(200, json={"errors": [{"message": "Internal server error"}], "data": None})
            return super().handler(request)

    with caplog.at_level(logging.ERROR):
        result = run(monkeypatch, GraphqlError({"ar": _catalog(4), None: _catalog(4)}))
    assert result["status"] == "degraded" and set(result["channels_failed"]) == {"default"}
    # Lo que se guarda y se muestra: el tipo y una frase fija. El texto del servidor queda en el log.
    assert result["channels_failed"]["default"] == "TransportQueryError: no se pudo leer Vendure (canal default)"
    assert "Internal server error" not in result["channels_failed"]["default"]
    assert "Internal server error" in "\n".join(r.getMessage() for r in caplog.records)


def test_si_la_pagina_2_falla_el_canal_queda_caido_entero_y_no_con_datos_a_medias(monkeypatch, caplog):
    class Page2Fails(FakeVendure):
        def handler(self, request):
            body = json.loads(request.content)
            if request.headers.get("vendure-token") == "ar" and body["variables"]["skip"] >= 100:
                self.requests.append({"token": "ar", "query": body["query"], "variables": body["variables"],
                                      "method": request.method, "url": str(request.url)})
                return _httpx.Response(503, text="Service Unavailable")
            return super().handler(request)

    fake = Page2Fails({"ar": _catalog(250), None: _catalog(250)})
    with caplog.at_level(logging.ERROR):
        result = run(monkeypatch, fake)
    assert result["status"] == "degraded"
    assert set(result["channels_failed"]) == {"ar"}
    assert result["channels_failed"]["ar"] == "TransportServerError: no se pudo leer Vendure (canal ar)"
    assert "503" in "\n".join(r.getMessage() for r in caplog.records)
    rows = rows_of(result["id"])
    assert len(rows) == 250 and all(r.in_ar is None for r in rows.values())
    assert result["products_total"] == 250


def test_un_canal_que_no_contesta_se_corta_por_tiempo_y_la_corrida_queda_incompleta(monkeypatch):
    monkeypatch.setattr(text_audit, "READ_TIMEOUT_S", 0.2)

    async def reader(token):
        if token is None:
            await asyncio.Event().wait()        # nunca contesta (asyncio.sleep está neutralizado por `install`)
        return await text_audit.read_channel(token)

    install(monkeypatch, FakeVendure({"ar": _catalog(3), None: _catalog(3)}))
    result = asyncio.run(text_audit.run_text_audit(trigger="manual", reader=reader))
    assert result["status"] == "degraded"
    assert set(result["channels_failed"]) == {"default"}
    assert result["channels_failed"]["default"], "el motivo no puede venir vacío"


def test_los_dos_canales_caidos_es_failed_con_motivo_y_sin_filas(monkeypatch):
    fake = FakeVendure({"ar": _catalog(2), None: _catalog(2)})
    fake.fail_tokens = {"ar", None}
    result = run(monkeypatch, fake)
    assert result["status"] == "failed" and result["error"]
    assert "ar" in result["error"] and "default" in result["error"]
    assert rows_of(result["id"]) == {}
    # Una corrida fallida no puede ser "la última corrida buena" del dashboard
    from sqlmodel import Session
    from app.db.session import engine
    with Session(engine) as s:
        latest = text_audit.latest_run(s)
    assert latest is not None and latest.status == "failed"   # no hay otra: es la única que existe


def test_una_corrida_fallida_no_pisa_a_la_ultima_buena_en_el_dashboard(monkeypatch):
    ok = run(monkeypatch, FakeVendure({"ar": _catalog(2), None: _catalog(2)}))
    bad_fake = FakeVendure({"ar": [], None: []})
    bad_fake.fail_tokens = {"ar", None}
    bad = run(monkeypatch, bad_fake)
    assert ok["status"] == "ok" and bad["status"] == "failed"
    from sqlmodel import Session
    from app.db.session import engine
    with Session(engine) as s:
        assert text_audit.latest_run(s).id == ok["id"]


# ─── Datos raros que no deberían tirar toda la auditoría ───────────

def test_una_traduccion_repetida_no_tira_la_corrida_entera(monkeypatch):
    weird = raw_product(7, translations=[("es_AR", "Taza uno", "taza-uno", "Taza larga y descriptiva para todos."),
                                         ("es-AR", "Taza dos", "taza-dos", "Otra taza larga y descriptiva.")])
    result = run(monkeypatch, FakeVendure({"ar": [weird, raw_product(8)], None: [weird, raw_product(8)]}))
    assert result["status"] == "ok" and result["products_total"] == 2
    assert "INSERT" not in (result.get("error") or "")
