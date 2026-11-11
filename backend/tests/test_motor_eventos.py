"""Avisos de cambio de estado al tablero del motor (app/motor_eventos.py): forma del evento, lista cerrada de pasos, y que
el aviso nunca rompe ni frena a quien lo dispara. Sin red: el servidor es un cliente falso."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from app import motor_eventos as me


def _config(monkeypatch, url: str = "https://nube.example/functions/v1/radar-ingesta", key: str = "clave-de-prueba"):
    monkeypatch.setattr(me, "get_settings", lambda: SimpleNamespace(motor_eventos_url=url, motor_eventos_key=key))


class _Server:
    def __init__(self, status: int = 200, exc: Exception | None = None, delay: float = 0.0) -> None:
        self.status, self.exc, self.delay = status, exc, delay
        self.calls: list[dict] = []

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": dict(headers or {})})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(status_code=self.status)


def test_el_evento_tiene_la_forma_del_contrato():
    ev = me.evento("luis-123", "medido", "ok", costo_usd=0.0213456789, ms=61234.7, ref=4521,
                   motivo="  verde   (ok) ", en=datetime(2026, 11, 11, 15, 4, 5, 999))
    assert ev == {"hilo": "luis-123", "sistema": "hugo", "paso": "medido", "estado": "ok", "en": "2026-11-11T15:04:05Z",
                  "costo_usd": 0.021346, "ms": 61234, "ref": "4521", "motivo": "verde (ok)"}
    # Lo opcional que no viene, no viaja.
    assert set(me.evento("h", "listo")) == {"hilo", "sistema", "paso", "estado", "en"}


@pytest.mark.parametrize("hilo, paso, estado", [
    (None, "medido", "ok"), ("", "medido", "ok"), ("   ", "medido", "ok"), ("x" * 129, "medido", "ok"),
    ("con\nsalto", "medido", "ok"), (123, "medido", "ok"),
    ("h", "inventado", "ok"), ("h", "medido", "raro"),
])
def test_sin_hilo_o_fuera_de_la_lista_cerrada_no_hay_evento(hilo, paso, estado):
    assert me.evento(hilo, paso, estado) is None


def test_el_hilo_se_limpia_y_el_motivo_se_recorta():
    assert me.limpiar_hilo("  luis-9  ") == "luis-9"
    assert me.limpiar_hilo("x" * 128) == "x" * 128
    assert len(me.evento("h", "error", "error", motivo="m" * 500)["motivo"]) == me.MOTIVO_MAX
    assert "costo_usd" not in me.evento("h", "medido", costo_usd=-1) and "ms" not in me.evento("h", "medido", ms=True)


async def test_enviar_manda_la_accion_eventos_con_la_cabecera_del_contrato(monkeypatch):
    _config(monkeypatch)
    server = _Server()
    assert await me.enviar([me.evento("h1", "medido"), None, me.evento("h1", "listo")], http=server) is True
    [call] = server.calls
    assert call["url"] == "https://nube.example/functions/v1/radar-ingesta"
    assert call["headers"] == {"x-radar-key": "clave-de-prueba"}
    assert call["json"]["accion"] == "eventos" and [e["paso"] for e in call["json"]["eventos"]] == ["medido", "listo"]


async def test_de_a_200_por_llamada(monkeypatch):
    _config(monkeypatch)
    server = _Server()
    assert await me.enviar([me.evento(f"h{i}", "medido") for i in range(450)], http=server) is True
    assert [len(c["json"]["eventos"]) for c in server.calls] == [200, 200, 50]


@pytest.mark.parametrize("url, key", [("", "k"), ("https://nube.example/x", ""), ("http://nube.example/x", "k")])
async def test_sin_url_sin_clave_o_sin_https_no_se_manda_nada(monkeypatch, url, key):
    _config(monkeypatch, url, key)
    server = _Server()
    assert await me.enviar([me.evento("h", "medido")], http=server) is False
    me.avisar(me.evento("h", "medido"))
    assert server.calls == []


@pytest.mark.parametrize("server", [_Server(status=500), _Server(status=401), _Server(exc=RuntimeError("red")),
                                    _Server(exc=TimeoutError())])
async def test_si_el_aviso_falla_no_lanza(monkeypatch, server):
    _config(monkeypatch)
    assert await me.enviar([me.evento("h", "medido")], http=server) is False


async def test_el_aviso_tiene_tope_de_tiempo(monkeypatch):
    _config(monkeypatch)
    monkeypatch.setattr(me, "TIMEOUT_S", 0.05)
    assert await me.enviar([me.evento("h", "medido")], http=_Server(delay=5)) is False


async def test_avisar_no_espera_ni_rompe(monkeypatch):
    _config(monkeypatch)
    sent: list[list[dict]] = []

    async def fake_enviar(eventos, http=None):
        sent.append(eventos)
        raise RuntimeError("aunque el envío reviente")

    monkeypatch.setattr(me, "enviar", fake_enviar)
    me.avisar(me.evento("h", "medido"), None, me.evento(None, "listo"))
    assert sent == []                                   # todavía no corrió: va en segundo plano
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(sent) == 1 and [e["paso"] for e in sent[0]] == ["medido"]


def test_avisar_sin_event_loop_no_hace_nada(monkeypatch):
    _config(monkeypatch)
    me.avisar(me.evento("h", "medido"))                 # no hay loop corriendo: no lanza
