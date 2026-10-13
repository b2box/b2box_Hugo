"""Con LOG_LEVEL=DEBUG ningún logger de transporte (gql, httpx, httpcore, httpx2, httpcore2…) puede escribir: sus registros
traen el header `vendure-auth-token` y las respuestas de Vendure (datos del proveedor y costos de fábrica).

Servidor HTTP local + el transporte real de gql (el mismo módulo httpx que usa el cliente, httpx2 con gql 4.4)."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from gql import Client, gql  # noqa: E402
from gql.transport.httpx import HTTPXAsyncTransport  # noqa: E402

from app.vendure import client as vc  # noqa: E402

SECRET_TOKEN = "SECRET_BEARER_TOKEN_XYZ"
SECRET_BODY = "SECRET_FOB_123.45"
TRANSPORT = ("gql", "httpx", "httpcore", "h2", "hpack", "hyperframe")


@pytest.fixture
def server():
    body = json.dumps({"data": {"bulkPriceTierBreakdowns": [{"fobUsd": {"exact": SECRET_BODY}}]}}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("content-length", 0)))
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("vendure-auth-token", SECRET_TOKEN)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/admin-api"
    srv.shutdown()


async def _one_request(url: str) -> None:
    transport = HTTPXAsyncTransport(url=url, headers={"authorization": "Bearer SECRET_REQ_BEARER"}, timeout=5)
    async with Client(transport=transport, fetch_schema_from_transport=False) as session:
        await session.execute(gql("query X { bulkPriceTierBreakdowns { fobUsd { exact } } }"))


@pytest.fixture
def debug_logging():
    """LOG_LEVEL=DEBUG como lo deja main._configure_logging: raíz en DEBUG y un handler que junta todo."""
    root = logging.getLogger()
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    old_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    # el estado de los loggers de transporte antes del test (para devolverlos como estaban)
    saved = {n: logging.getLogger(n).level for n in list(logging.root.manager.loggerDict) if n.startswith(TRANSPORT + ("httpx2", "httpcore2"))}
    yield buf
    root.removeHandler(handler)
    root.setLevel(old_level)
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


async def test_at_debug_no_transport_logger_writes_and_the_secrets_stay_out(server, debug_logging):
    vc.quiet_http_loggers()                              # lo que hace main._configure_logging
    await _one_request(server)
    out = debug_logging.getvalue()
    names = {line.split(":")[0] for line in out.splitlines() if ":" in line}
    transport_names = {n for n in names if n.startswith(TRANSPORT + ("httpx2", "httpcore2"))}
    assert not transport_names, f"loggers de transporte que escribieron: {sorted(transport_names)}"
    for secret in (SECRET_TOKEN, SECRET_BODY, "SECRET_REQ_BEARER", "vendure-auth-token", "Bearer "):
        assert secret.lower() not in out.lower(), secret


async def test_the_probe_really_leaks_without_the_fix(server, debug_logging):
    """Control del test de arriba: si no se silencia, el header SÍ aparece (si no, el test anterior no probaría nada)."""
    for name in ("httpx2", "httpcore2", "httpx", "httpcore", "gql"):
        logging.getLogger(name).setLevel(logging.NOTSET)
    for name in list(logging.root.manager.loggerDict):
        if name.startswith(("httpx", "httpcore", "gql")):
            logging.getLogger(name).setLevel(logging.NOTSET)
    await _one_request(server)
    assert SECRET_TOKEN in debug_logging.getvalue()


async def test_loggers_created_after_the_call_are_silenced_too(server, debug_logging):
    """httpcore2 crea sus loggers al importar/usar los módulos: el padre silenciado alcanza para los hijos nuevos."""
    vc.quiet_http_loggers()
    child = logging.getLogger("httpcore2.una_conexion_nueva")
    child.debug("receive_response_headers.complete return_value=%s", SECRET_TOKEN)
    logging.getLogger("httpx2.cliente_nuevo").info("HTTP Request: POST https://admin/%s", SECRET_TOKEN)
    assert SECRET_TOKEN not in debug_logging.getvalue()


def test_main_configure_logging_silences_the_transport(monkeypatch, debug_logging):
    from app import main

    monkeypatch.setattr(logging, "basicConfig", lambda **kw: None)   # no tocar la configuración global del runner de tests
    main._configure_logging()
    for name in ("httpx2", "httpcore2", "httpx", "httpcore", "gql"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name
    assert logging.getLogger("httpcore2.http11").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpx2._client").getEffectiveLevel() >= logging.WARNING


async def test_a_run_of_the_new_reads_at_debug_leaves_no_cost_data_in_the_logs(monkeypatch, debug_logging):
    from tests import review_world as w
    from tests.review_fixtures import FakeReviewVendure, engine_variant, install
    from tests.test_price_review_api import open_items

    w.reset_db()
    w.set_setting("pm_mode", 1)
    fake = FakeReviewVendure()
    install(monkeypatch, fake)
    v = engine_variant(0)
    fake.breakdowns[v["variant"]] = v["breakdowns"]
    fake.variants[v["variant"]] = {"id": "101", "priceWithTax": v["priceWithTax"], "currencyCode": "ARS",
                                   "customFields": v["customFields"], "bulkPriceTiers": v["tiers"]}
    vc.quiet_http_loggers()
    await open_items(["a", "b"])
    out = debug_logging.getvalue()
    for needle in ("fobUsd", "purchasePriceCny", "arancelesUsd", "b2boxFeeUsd", "facturaUsd", "cny_unit"):
        assert needle not in out, needle
    assert not {line.split(":")[0] for line in out.splitlines() if line.startswith(TRANSPORT + ("httpx2", "httpcore2"))}
    assert fake.mutations == [] and len(fake.requests) > 0
