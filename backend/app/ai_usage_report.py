"""El gasto de Hugo en la nube (Gastos de IA), igual que Paco: cada fila nueva de `llm_usage` y de `apify_spend` se
manda a la edge function `ai-usage-ingest`.

    POST {ADMIN_AI_USAGE_URL}      cabecera  x-ingest-key: {ADMIN_AI_USAGE_KEY}
    {"events": [{"feature": "hugo", "cost_usd": 0.0123, "model": "gemini-2.5-flash", "quantity": 1,
                 "occurred_at": "2026-11-11T15:04:05Z", "meta": {"fuente": "llm_usage", "id": 42, "uso": "…"}}]}

Reglas:

  * las dos variables o ninguna, y la URL tiene que ser https; sin eso no se manda nada y todo sigue igual;
  * se manda en lote cada `REPORT_EVERY_MIN` minutos, de lo que no se mandó todavía (marca: el último id de cada tabla
    en `settings`). Si la nube no contesta, la marca no avanza y se reintenta en la próxima vuelta: no se pierde gasto;
  * la primera vez que corre arranca desde ahora (no reenvía la historia: la nube nunca la tuvo y duplicaría el día);
  * solo números y códigos (lo mismo que guarda `llm_usage`): nunca el prompt, la respuesta ni un proveedor;
  * nunca lanza.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlmodel import Session, func, select

from app.config import get_settings
from app.db.models import ApifySpend, LlmUsage
from app.db.session import engine

log = logging.getLogger(__name__)

JOB_ID = "ai_usage_report"
REPORT_EVERY_MIN = 15
LOTE_MAX = 200
TIMEOUT_S = 10.0
_MARCAS = {"llm_usage": "_meta:ai_usage:llm_usage", "apify_spend": "_meta:ai_usage:apify_spend"}
_avisado: set[str] = set()


def configurado() -> tuple[str, str] | None:
    """(URL, clave) si las dos están y la URL es https; si no, None."""
    s = get_settings()
    url, key = (s.admin_ai_usage_url or "").strip(), (s.admin_ai_usage_key or "").strip()
    if not url or not key:
        return None
    if not url.lower().startswith("https://"):
        if "http" not in _avisado:
            _avisado.add("http")
            log.error("gasto de IA: ADMIN_AI_USAGE_URL no es https: no se manda nada")
        return None
    return url, key


def _iso(dt: Any) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"


def _evento_llm(row: LlmUsage, feature: str) -> dict[str, Any]:
    return {"feature": feature, "cost_usd": round(float(row.cost_usd or 0.0), 6), "model": row.model or row.provider,
            "quantity": 1, "occurred_at": _iso(row.at),
            "meta": {"fuente": "llm_usage", "id": row.id, "uso": row.purpose, "proveedor": row.provider,
                     "tokens_entrada": row.input_tokens, "tokens_salida": row.output_tokens,
                     "estimado": bool(row.cost_estimated), "ok": bool(row.ok)}}


def _evento_apify(row: ApifySpend, feature: str) -> dict[str, Any]:
    return {"feature": feature, "cost_usd": round(float(row.usd or 0.0), 6), "model": "apify",
            "quantity": int(row.terms or 0), "occurred_at": _iso(row.created_at),
            "meta": {"fuente": "apify_spend", "id": row.id, "uso": row.label, "estimado": bool(row.estimated)}}


def _pendientes(tabla: str, desde: int) -> tuple[list[dict[str, Any]], int]:
    """(eventos con costo > 0, último id leído) de lo posterior a `desde`."""
    feature = (get_settings().hugo_ai_usage_feature or "hugo").strip() or "hugo"
    model, armar = (LlmUsage, _evento_llm) if tabla == "llm_usage" else (ApifySpend, _evento_apify)
    with Session(engine) as s:
        rows = s.exec(select(model).where(model.id > desde).order_by(model.id).limit(LOTE_MAX)).all()  # type: ignore[attr-defined,union-attr]
    ultimo = max((r.id or 0 for r in rows), default=desde)
    return [e for e in (armar(r, feature) for r in rows) if e["cost_usd"] > 0], ultimo


def _max_id(tabla: str) -> int:
    model = LlmUsage if tabla == "llm_usage" else ApifySpend
    with Session(engine) as s:
        return int(s.exec(select(func.max(model.id))).one() or 0)  # type: ignore[arg-type]


async def reportar(http: Any = None) -> dict[str, Any]:
    """Una vuelta: manda lo pendiente de cada tabla y avanza la marca solo si la nube lo aceptó. Nunca lanza."""
    from app.scheduler.jobs import _get_meta, _set_meta  # lazy: jobs importa medio app

    cfg = configurado()
    if cfg is None:
        return {"apagado": True}
    url, key = cfg
    out: dict[str, Any] = {}
    propio = http is None
    try:
        http = http or httpx.AsyncClient(timeout=TIMEOUT_S)
        for tabla, marca in _MARCAS.items():
            guardada = _get_meta(marca)
            if guardada is None:   # primera vez: desde ahora
                _set_meta(marca, str(_max_id(tabla)))
                out[tabla] = {"inicio": True}
                continue
            eventos, ultimo = _pendientes(tabla, int(guardada))
            if eventos:
                r = await http.post(url, json={"events": eventos}, headers={"x-ingest-key": key})
                if r.status_code >= 300:
                    log.warning("gasto de IA: la nube devolvió HTTP %s para %d eventos de %s", r.status_code,
                                len(eventos), tabla)
                    out[tabla] = {"error": r.status_code}
                    continue
            if ultimo != int(guardada):
                _set_meta(marca, str(ultimo))
            out[tabla] = {"enviados": len(eventos)}
    except Exception as exc:  # noqa: BLE001  (el reporte nunca rompe nada; se reintenta en la próxima vuelta)
        # Solo el tipo: el mensaje de httpx puede arrastrar la URL.
        log.warning("gasto de IA: no se pudo mandar (%s)", type(exc).__name__)
        out["error"] = type(exc).__name__
    finally:
        if propio and http is not None:
            try:
                await http.aclose()
            except Exception as exc:  # noqa: BLE001
                log.debug("gasto de IA: no se pudo cerrar el cliente (%s)", type(exc).__name__)
    return out
