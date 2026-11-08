"""Registro común de tokens y costo de cada llamada a un modelo de lenguaje o de visión.

Cada llamada (juez del semáforo, juez y medidas del estimado, rerank de visión del app) deja UNA fila en `llm_usage` con:
proveedor, modelo, tokens de entrada y salida, costo estimado en USD y para qué se usó (`purpose`). Nunca el prompt ni la
respuesta: solo números y códigos.

    record(provider="anthropic", model="claude-opus-5", purpose="app_rerank", input_tokens=1200, output_tokens=80)

`record` nunca lanza (un problema con la base no puede tirar la llamada que ya se pagó) y no bloquea el event loop si se
usa `record_async`. El costo:

  * si quien llama ya lo calculó (p. ej. con los precios de `PM_LLM_PRICE_*` del juez), va tal cual;
  * si no, sale de `PRICES_PER_M` (USD por millón de tokens, precios de lista del proveedor) y queda `cost_estimated`;
  * un modelo que no está en la tabla queda con costo 0 y `cost_estimated = True` (se ve en el resumen como «sin precio»).

Los cupos diarios de llamadas siguen en `pricing/daily_budget.py` (cada consumidor con su contador); acá se mira lo gastado.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlmodel import Session, func, select

from app.clock import utcnow
from app.db.models import LlmUsage
from app.db.session import engine

log = logging.getLogger(__name__)

# USD por millón de tokens (entrada, salida), precios de lista. La clave es un prefijo del id del modelo: gana el más
# largo que coincida. Anthropic: tabla de precios de la API (oct-2026). Gemini 2.5 Flash: 0,30 / 2,50.
PRICES_PER_M: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-5": (0.10, 0.50),
    "claude-haiku-4": (1.0, 5.0),
    "gemini-2.5-flash": (0.30, 2.50),
}

PURPOSE_MAX = 40


def price_for(model: str) -> tuple[float, float] | None:
    """(USD/M entrada, USD/M salida) del modelo, o None si no está en la tabla."""
    key = (model or "").strip().lower()
    best: str | None = None
    for prefix in PRICES_PER_M:
        if key.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    return PRICES_PER_M[best] if best else None


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Costo en USD con la tabla de precios; None si el modelo no está."""
    prices = price_for(model)
    if prices is None:
        return None
    return round((max(0, int(input_tokens)) * prices[0] + max(0, int(output_tokens)) * prices[1]) / 1_000_000.0, 6)


def build_row(*, provider: str, model: str, purpose: str, input_tokens: int = 0, output_tokens: int = 0,
              cost_usd: float | None = None, ok: bool = True, now: datetime | None = None) -> LlmUsage:
    """La fila (pura, sin base): el costo que vino o el de la tabla."""
    estimated = cost_usd is None
    if cost_usd is None:
        cost_usd = estimate_cost(model, input_tokens, output_tokens) or 0.0
    return LlmUsage(at=now or utcnow(), provider=(provider or "")[:24], model=(model or "")[:80],
                    purpose=(purpose or "")[:PURPOSE_MAX], input_tokens=max(0, int(input_tokens or 0)),
                    output_tokens=max(0, int(output_tokens or 0)), cost_usd=round(float(cost_usd), 6),
                    cost_estimated=estimated, ok=bool(ok))


def record(*, provider: str, model: str, purpose: str, input_tokens: int = 0, output_tokens: int = 0,
           cost_usd: float | None = None, ok: bool = True) -> None:
    """Guarda la llamada. Nunca lanza."""
    try:
        row = build_row(provider=provider, model=model, purpose=purpose, input_tokens=input_tokens,
                        output_tokens=output_tokens, cost_usd=cost_usd, ok=ok)
        with Session(engine) as s:
            s.add(row)
            s.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("llm_usage: no se pudo registrar la llamada (%s)", type(exc).__name__)


async def record_async(**kwargs: Any) -> None:
    """`record` fuera del event loop. Nunca lanza."""
    try:
        await asyncio.to_thread(lambda: record(**kwargs))
    except Exception as exc:  # noqa: BLE001
        log.warning("llm_usage: no se pudo registrar la llamada (%s)", type(exc).__name__)


def provider_of_base_url(host: str) -> str:
    """Nombre corto del proveedor de un endpoint compatible con OpenAI (el del juez del semáforo y del estimado)."""
    h = (host or "").lower()
    if "xiaomimimo" in h:
        return "mimo"
    if "aliyuncs" in h or "qwen" in h:
        return "qwen"
    if "openai" in h:
        return "openai"
    if "googleapis" in h:
        return "gemini"
    return h.split(".")[-2] if h.count(".") >= 1 else (h or "desconocido")


def summary(days: int = 30, now: datetime | None = None) -> dict[str, Any]:
    """Gasto de los últimos `days` días: total, por día, por propósito y por modelo."""
    now = now or utcnow()
    since = now - timedelta(days=max(1, int(days)))
    with Session(engine) as s:
        rows = s.exec(select(LlmUsage.at, LlmUsage.provider, LlmUsage.model, LlmUsage.purpose, LlmUsage.input_tokens,
                             LlmUsage.output_tokens, LlmUsage.cost_usd, LlmUsage.cost_estimated)
                      .where(LlmUsage.at >= since)).all()
        total_all = float(s.exec(select(func.coalesce(func.sum(LlmUsage.cost_usd), 0.0))).one() or 0.0)

    def bucket() -> dict[str, Any]:
        return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def add(b: dict[str, Any], r: Any) -> None:
        b["calls"] += 1
        b["input_tokens"] += int(r[4] or 0)
        b["output_tokens"] += int(r[5] or 0)
        b["cost_usd"] = round(b["cost_usd"] + float(r[6] or 0.0), 6)

    total, by_day, by_purpose, by_model = bucket(), {}, {}, {}
    unpriced = 0
    for r in rows:
        add(total, r)
        add(by_day.setdefault(r[0].strftime("%Y-%m-%d"), bucket()), r)
        add(by_purpose.setdefault(r[3] or "?", bucket()), r)
        add(by_model.setdefault(f"{r[1]}/{r[2]}", bucket()), r)
        if r[7] and not r[6] and (r[4] or r[5]):
            unpriced += 1
    return {"days": int(days), "total": total, "by_day": dict(sorted(by_day.items())), "by_purpose": by_purpose,
            "by_model": by_model, "sin_precio": unpriced, "total_historico_usd": round(total_all, 6)}
