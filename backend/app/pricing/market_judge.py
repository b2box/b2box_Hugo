"""Juez LLM para la banda ambigua del filtro "mismo producto".

CLIP + nombre deciden solos los casos claros (market_match.classify). Lo que
queda en el medio —foto parecida pero no igual, nombre a medias— lo mira un
modelo multimodal barato por una API OpenAI-compatible: nuestras 1-2 fotos y el
nombre contra hasta N fichas de ML (foto + título + precio). Responde, por
ficha, si es el mismo producto y con qué confianza.

Proveedor: el que diga `PM_LLM_BASE_URL`. Por defecto el modelo es Qwen
(`qwen3-vl-plus`, Alibaba Model Studio); también sirven Xiaomi MiMo
(`mimo-v2-omni`) u OpenRouter. No se hardcodea ninguna URL: ver README.

Costo bajo control:
  * solo corre si `pm_vision_max_calls` > 0 (default 0 = sombra sin IA);
  * tope diario con reserva atómica (misma mecánica que el budget de ML);
  * tokens y costo estimado quedan en `price_monitor_run`.

Nunca rompe el job: sin API key, sin cupo, timeout o JSON ilegible → "sin
veredicto" (None) y el producto sigue como ambiguo.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.pricing import daily_budget

log = logging.getLogger(__name__)

LLM_COUNTER_KEY = "_meta:pm_llm_calls_today"
# Confianza mínima del juez para tomar su "sí" como match.
MIN_CONFIDENCE = 0.60
_MAX_CANDIDATES = 6
_MAX_OUR_PHOTOS = 2
_MAX_TOKENS = 700

_SYSTEM = (
    "Sos un verificador de catálogo de productos. Comparás UN producto nuestro "
    "(fotos + nombre) contra varias publicaciones de Mercado Libre y decidís, para "
    "cada una, si es EXACTAMENTE el mismo producto (mismo tipo, forma, función y "
    "tamaño aproximado; el color o la marca pueden variar). Respondé SOLO con JSON "
    "válido, sin texto antes ni después, con este formato: "
    '{"results":[{"ml_id":"MLA123","same_product":true,"confidence":0.0,"reason":"..."}]}. '
    "`confidence` va de 0 a 1. `reason` es una frase corta en español."
)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I | re.M)


@dataclass(slots=True, frozen=True)
class JudgeCandidate:
    ml_id: str
    title: str
    image_url: str | None
    price_cents: int | None = None


@dataclass(slots=True, frozen=True)
class JudgeVerdict:
    ml_id: str
    same_product: bool
    confidence: float
    reason: str = ""


@dataclass(slots=True)
class JudgeResult:
    verdicts: dict[str, JudgeVerdict] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    raw: str = ""


def enabled() -> bool:
    s = get_settings()
    return bool(s.pm_llm_base_url and s.pm_llm_api_key)


def estimate_cost(input_tokens: int, output_tokens: int,
                  price_in_per_m: float, price_out_per_m: float) -> float:
    return round(
        (max(0, input_tokens) * float(price_in_per_m) + max(0, output_tokens) * float(price_out_per_m))
        / 1_000_000.0,
        6,
    )


def _image_part(url: str) -> dict[str, Any]:
    # URLs públicas (nuestro CDN, mlstatic): el proveedor las descarga. Más
    # barato que base64 y evita bajar las fotos acá. Pendiente de verificar
    # contra cada proveedor que acepte URL remota (OpenAI y Qwen sí).
    return {"type": "image_url", "image_url": {"url": url}}


def build_messages(
    our_name: str, our_image_urls: Sequence[str], candidates: Sequence[JudgeCandidate],
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {"type": "text", "text": f"NUESTRO PRODUCTO: {our_name.strip()[:200]}"},
    ]
    content.extend(_image_part(u) for u in list(our_image_urls)[:_MAX_OUR_PHOTOS] if u)
    content.append({"type": "text", "text": "PUBLICACIONES DE MERCADO LIBRE:"})
    for c in list(candidates)[:_MAX_CANDIDATES]:
        price = f" · precio ARS {c.price_cents / 100:.0f}" if c.price_cents else ""
        content.append({"type": "text", "text": f"- {c.ml_id}: {c.title.strip()[:160]}{price}"})
        if c.image_url:
            content.append(_image_part(c.image_url))
    content.append({"type": "text", "text": "Respondé solo el JSON pedido."})
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": content},
    ]


def _to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "si", "sí", "yes", "1"):
            return True
        if v in ("false", "no", "0"):
            return False
    return None


def _to_conf(value: Any) -> float:
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return 0.0
    if conf > 1.0:
        conf = conf / 100.0 if conf <= 100.0 else 1.0
    return max(0.0, min(1.0, conf))


def parse_verdicts(text: str) -> list[JudgeVerdict] | None:
    """JSON del modelo → veredictos. Tolerante: saca ``` fences, acepta una
    lista suelta, {"results": [...]} o un dict por ml_id. None si no se
    entiende nada (= sin veredicto, nunca una excepción)."""
    if not text:
        return None
    cleaned = _FENCE.sub("", text.strip())
    start = min((i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0), default=-1)
    if start < 0:
        return None
    try:
        data = json.loads(cleaned[start:])
    except ValueError:
        # Último intento: cortar en el último cierre.
        end = max(cleaned.rfind("}"), cleaned.rfind("]"))
        if end <= start:
            return None
        try:
            data = json.loads(cleaned[start:end + 1])
        except ValueError:
            return None

    entries: list[Any]
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        inner = next((data[k] for k in ("results", "verdicts", "fichas", "items") if k in data), None)
        if isinstance(inner, list):
            entries = inner
        else:
            # {"MLA1": {"same_product": ...}, ...}
            entries = [{"ml_id": k, **v} for k, v in data.items() if isinstance(v, dict)]
    else:
        return None

    out: list[JudgeVerdict] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        ml_id = str(e.get("ml_id") or e.get("id") or "").strip()
        same = _to_bool(e.get("same_product", e.get("same", e.get("mismo"))))
        if not ml_id or same is None:
            continue
        out.append(JudgeVerdict(
            ml_id=ml_id, same_product=same,
            confidence=_to_conf(e.get("confidence", e.get("confianza", 0))),
            reason=str(e.get("reason") or e.get("motivo") or "")[:300],
        ))
    return out or None


def _make_client():
    from openai import AsyncOpenAI  # ya es dependencia (vision_rerank)

    s = get_settings()
    return AsyncOpenAI(
        base_url=s.pm_llm_base_url, api_key=s.pm_llm_api_key, timeout=s.pm_llm_timeout_s,
    )


async def judge(
    our_name: str,
    our_image_urls: Sequence[str],
    candidates: Sequence[JudgeCandidate],
    *,
    max_calls: int,
    client: Any | None = None,
    on_reserve: Callable[[Any], None] | None = None,
) -> JudgeResult | None:
    """Pregunta al modelo. None = sin veredicto (apagado, sin cupo, falló o no
    se entendió la respuesta). El llamador trata None como "sigue ambiguo".

    `on_reserve` corre en la transacción que reserva el cupo del día: la
    llamada queda contada aunque después falle (timeout, 5xx)."""
    if max_calls <= 0 or not candidates or not enabled():
        return None
    if await daily_budget.reserve_async(LLM_COUNTER_KEY, int(max_calls), None, on_reserve) is None:
        log.info("Juez LLM: tope diario alcanzado (%d), no se consulta", max_calls)
        return None

    s = get_settings()
    client = client or _make_client()
    try:
        response = await client.chat.completions.create(
            model=s.pm_llm_model,
            messages=build_messages(our_name, our_image_urls, candidates),
            temperature=0,
            max_tokens=_MAX_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001  (timeout, 4xx/5xx, red)
        log.warning("Juez LLM falló (%s): %s", type(exc).__name__, str(exc)[:200])
        return None

    choice = response.choices[0] if getattr(response, "choices", None) else None
    text = (getattr(getattr(choice, "message", None), "content", None) or "") if choice else ""
    usage = getattr(response, "usage", None)
    in_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
    out_tok = int(getattr(usage, "completion_tokens", 0) or 0)
    result = JudgeResult(
        input_tokens=in_tok, output_tokens=out_tok,
        cost_usd=estimate_cost(in_tok, out_tok, s.pm_llm_price_in_per_m, s.pm_llm_price_out_per_m),
        model=s.pm_llm_model, raw=text[:2000],
    )
    verdicts = parse_verdicts(text)
    if verdicts is None:
        log.warning("Juez LLM: respuesta ilegible (%d chars), sin veredicto", len(text))
        return result  # tokens gastados igual: se contabilizan, sin veredictos
    wanted = {c.ml_id for c in candidates}
    result.verdicts = {v.ml_id: v for v in verdicts if v.ml_id in wanted}
    return result
