"""El transport de gql recibe un Timeout del mismo httpx que usa por dentro.

gql 4.4 importa `httpx2` si está instalado (lo traen anthropic>=1 y
openai>=3). Un httpx.Timeout de httpx 0.28 pasado a un cliente httpx2 llega
crudo a httpcore2 y toda query a Vendure revienta con "unsupported operand
type(s) for +: 'float' and 'Timeout'" (08-oct-2026). Este test falla si alguien
vuelve a armar el timeout con nuestro `httpx`.
"""

from __future__ import annotations

import httpx
from gql.transport import httpx as gql_httpx_transport

from app.vendure import client as vendure_client
from app.vendure.client import VendureClient


def test_el_timeout_es_del_httpx_del_transport():
    c = VendureClient()
    c._bearer = "x"
    transport = c._new_client().transport
    timeout = transport.kwargs["timeout"]
    assert isinstance(timeout, gql_httpx_transport.httpx.Timeout)
    assert timeout.connect == 10.0
    assert timeout.read == 60.0


def test_los_errores_http_del_transport_se_reintentan():
    errores = vendure_client._TRANSPORT_HTTP_ERRORS
    assert httpx.HTTPError in errores
    assert gql_httpx_transport.httpx.HTTPError in errores
