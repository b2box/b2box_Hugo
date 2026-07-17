"""
Regresión: fetch_all_products pide varias páginas en paralelo (gather con
FETCH_CONCURRENCY). Con un gql.Client compartido y un solo transport, el 2do
`async with` concurrente tiraba TransportAlreadyConnected -> 502 en /verify.

El fix (VendureClient._new_client por ejecución) le da a cada corrutina su
propio transport. Este test falla si alguien vuelve a compartir el client.
"""
import asyncio

import pytest
from gql import Client, gql
from gql.transport.async_transport import AsyncTransport
from gql.transport.exceptions import TransportAlreadyConnected
from graphql import ExecutionResult

from app.vendure.client import VendureClient


class _OneShotTransport(AsyncTransport):
    """Transport que permite UNA sola sesión viva a la vez, como el real.
    Un 2do connect() sin close() previo levanta TransportAlreadyConnected."""

    def __init__(self):
        self.live = 0

    async def connect(self):
        if self.live >= 1:
            raise TransportAlreadyConnected("Transport is already connected")
        self.live += 1

    async def close(self):
        self.live -= 1

    async def execute(self, document, *args, **kwargs):
        await asyncio.sleep(0.02)  # simula latencia — fuerza el solapamiento
        return ExecutionResult(data={"ok": True}, errors=None)

    async def subscribe(self, *args, **kwargs):
        raise NotImplementedError


def _fresh_client():
    return Client(transport=_OneShotTransport(), fetch_schema_from_transport=False)


@pytest.mark.asyncio
async def test_new_client_returns_fresh_instances():
    """_new_client no debe devolver un cliente compartido."""
    vc = VendureClient()
    a, b = vc._new_client(), vc._new_client()
    assert a is not b
    assert a.transport is not b.transport


@pytest.mark.asyncio
async def test_concurrent_sessions_do_not_collide():
    """6 ejecuciones concurrentes, cada una con su client, no colisionan."""
    query = gql("{ __typename }")

    async def run_one():
        async with _fresh_client() as session:
            return await session.execute(query)

    results = await asyncio.gather(*(run_one() for _ in range(6)))
    assert len(results) == 6
    assert all(r == {"ok": True} for r in results)


@pytest.mark.asyncio
async def test_shared_client_would_collide():
    """Documenta el bug: un client compartido SÍ rompe bajo concurrencia.
    Si esto deja de levantar, el modelo de fallo del transport cambió y el
    test de arriba hay que revisarlo."""
    query = gql("{ __typename }")
    shared = _fresh_client()

    async def run_one():
        async with shared as session:
            return await session.execute(query)

    with pytest.raises(TransportAlreadyConnected):
        await asyncio.gather(*(run_one() for _ in range(6)))
