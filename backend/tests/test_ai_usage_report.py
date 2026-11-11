"""Gasto de Hugo a la nube (app/ai_usage_report.py): forma del lote, marca que solo avanza si la nube aceptó, arranque
desde ahora y apagado sin configuración o sin https. Sin red: el servidor es un cliente falso."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlmodel import Session

from app import ai_usage_report as aur
from app.db.models import ApifySpend, LlmUsage, Setting
from app.db.session import engine, init_db

URL = "https://nube.example/functions/v1/ai-usage-ingest"


class _Server:
    def __init__(self, status: int = 200, exc: Exception | None = None) -> None:
        self.status, self.exc = status, exc
        self.calls: list[dict] = []

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": dict(headers or {})})
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(status_code=self.status)


@pytest.fixture
def conf(monkeypatch):
    def _set(url: str = URL, key: str = "clave-de-prueba"):
        monkeypatch.setattr(aur, "get_settings", lambda: SimpleNamespace(
            admin_ai_usage_url=url, admin_ai_usage_key=key, hugo_ai_usage_feature="hugo"))
    _set()
    init_db()
    with Session(engine) as s:
        for marca in aur._MARCAS.values():
            row = s.get(Setting, marca)
            if row is not None:
                s.delete(row)
        s.commit()
    return _set


def _add(*rows) -> None:
    with Session(engine) as s:
        for r in rows:
            s.add(r)
        s.commit()


async def test_sin_configuracion_o_sin_https_no_manda_nada(conf):
    server = _Server()
    conf(url="", key="")
    assert await aur.reportar(http=server) == {"apagado": True}
    conf(url="http://nube.example/x")
    assert await aur.reportar(http=server) == {"apagado": True}
    assert server.calls == []


async def test_arranca_desde_ahora_y_despues_manda_solo_lo_nuevo_con_costo(conf):
    _add(LlmUsage(provider="gemini", model="gemini-2.5-flash", purpose="viejo", cost_usd=9.0))
    server = _Server()
    first = await aur.reportar(http=server)
    assert first == {"llm_usage": {"inicio": True}, "apify_spend": {"inicio": True}} and server.calls == []

    at = datetime(2026, 11, 11, 15, 4, 5, 123)  # noqa: DTZ001  (la base guarda UTC sin zona, como utcnow())
    _add(LlmUsage(at=at, provider="gemini", model="gemini-2.5-flash", purpose="estimado_medidas_gemini",
                  input_tokens=1200, output_tokens=80, cost_usd=0.0123, cost_estimated=True),
         LlmUsage(at=at, provider="mimo", model="mimo-x", purpose="juez", cost_usd=0.0),
         ApifySpend(created_at=at, label="fast_ready", terms=3, usd=0.25, estimated=False))
    out = await aur.reportar(http=server)
    assert out == {"llm_usage": {"enviados": 1}, "apify_spend": {"enviados": 1}}
    assert all(c["url"] == URL and c["headers"] == {"x-ingest-key": "clave-de-prueba"} for c in server.calls)
    [llm] = server.calls[0]["json"]["events"]
    assert (llm["feature"], llm["cost_usd"], llm["model"], llm["occurred_at"]) == (
        "hugo", 0.0123, "gemini-2.5-flash", "2026-11-11T15:04:05Z")
    assert llm["meta"]["uso"] == "estimado_medidas_gemini" and llm["meta"]["fuente"] == "llm_usage"
    [apify] = server.calls[1]["json"]["events"]
    assert (apify["cost_usd"], apify["model"], apify["quantity"], apify["meta"]["uso"]) == (0.25, "apify", 3, "fast_ready")

    # Lo ya mandado (y la fila sin costo) no vuelve a salir.
    server.calls.clear()
    assert await aur.reportar(http=server) == {"llm_usage": {"enviados": 0}, "apify_spend": {"enviados": 0}}
    assert server.calls == []


async def test_si_la_nube_falla_la_marca_no_avanza_y_se_reintenta(conf):
    await aur.reportar(http=_Server())   # marca inicial
    _add(LlmUsage(provider="gemini", model="gemini-2.5-flash", purpose="juez", cost_usd=0.5))

    caida = _Server(status=500)
    assert (await aur.reportar(http=caida))["llm_usage"] == {"error": 500}
    sin_red = _Server(exc=RuntimeError("boom"))
    assert (await aur.reportar(http=sin_red))["error"] == "RuntimeError"

    ok = _Server()
    assert (await aur.reportar(http=ok))["llm_usage"] == {"enviados": 1}
    assert [e["cost_usd"] for e in ok.calls[0]["json"]["events"]] == [0.5]
