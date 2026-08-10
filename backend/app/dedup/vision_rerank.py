"""Estrategia 5 — un modelo con visión decide cuál de los candidatos es el producto.

Por qué existe
--------------
CLIP alcanza para ARMAR una lista corta, no para decidir. Medido contra el
catálogo real (1006 productos del canal 'ar'): el producto correcto sale top-1
solo el 52.6% de las veces, pero está en el top-20 el 80.3%. El catálogo es casi
todo la misma categoría (organizadores, estantes, cocina) y CLIP no distingue un
"Organizador Doble Ajustable" de un "Organizador de Baño Multitalle": para el
modelo se ven igual.

Retrieve + rerank: `catalog_index` trae los K más parecidos, y acá un modelo con
visión mira la foto del link al lado de las K fotos del catálogo y elige — o dice
que ninguna es. Ese "ninguna" es la mitad del valor: es lo que evita proponer
cualquier producto cuando el que el cliente pidió no lo tenemos.

Cómo se le muestran los candidatos
----------------------------------
Una lámina de contactos: una sola imagen con los K candidatos en grilla, cada
celda numerada. Mandar las K fotos sueltas costaría ~1600 tokens cada una; la
lámina entera entra en ~4800 (el techo de un solo bloque de imagen). Misma
información, ~4x más barato y una sola pasada de visión.

Dos proveedores
---------------
El armado (bajar fotos, lámina, prompt, parseo, cache) es común; lo único
específico de cada proveedor es la llamada. Están los dos para poder medir cuál
anda mejor contra ESTE catálogo en vez de discutirlo — ver
`app/dedup/calibrate_vision.py` y el endpoint `/app/vision-compare`.

Degrada elegante: sin API key, con el modelo caído o si la respuesta no valida,
`pick_match` devuelve None y el llamador sigue con los umbrales de CLIP.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from app.config import get_settings
from app.dedup.image_hash import _fetch  # mismo fetch con guard SSRF + tope de bytes
from app.vendure.client import VendureProduct

log = logging.getLogger(__name__)

ANTHROPIC = "anthropic"
OPENAI = "openai"
PROVIDERS = (ANTHROPIC, OPENAI)

# Formato de la respuesta. Los dos proveedores saben forzar un JSON schema, así
# que no hay que parsear prosa ni reintentar cuando el modelo decora la salida.
_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "match": {
            "type": ["integer", "null"],
            "description": (
                "Número del candidato que es el MISMO producto que la foto de "
                "referencia, o null si ninguno lo es."
            ),
        },
        "confidence": {
            "type": "number",
            "description": "Qué tan seguro estás, de 0 a 1.",
        },
        "reason": {
            "type": "string",
            "description": "Una frase corta explicando la decisión.",
        },
    },
    "required": ["match", "confidence", "reason"],
    "additionalProperties": False,
}

_SYSTEM = """\
Sos el control de calidad del catálogo de B2Box, un importador. Te damos la foto \
de un producto que un cliente quiere comprar y una lámina con los candidatos que \
ya tenemos en el catálogo, numerados.

Tu trabajo es decir si alguno de los candidatos es EL MISMO PRODUCTO que el de la \
foto de referencia — el mismo artículo, no uno parecido.

El catálogo es casi todo la misma categoría, así que muchos candidatos se van a \
parecer bastante. Parecido no alcanza. Mirá la forma, las proporciones, los \
materiales, la cantidad de piezas, los accesorios, el mecanismo. Dos organizadores \
de plástico blanco pueden ser productos distintos.

Las fotos son de fuentes distintas: la de referencia suele ser de marketplace \
(fondo blanco, producto solo) y las nuestras suelen ser láminas de proveedor \
(varias unidades, fondo de color, texto encima, el producto en uso). Que el estilo \
de foto no coincida no significa que el producto no coincida — y que coincida \
tampoco prueba nada.

Si ninguno es el mismo producto, respondé match: null. Decir "ninguno" es la \
respuesta correcta la mayoría de las veces y es mucho mejor que forzar el que \
más se parece: proponerle al cliente un producto que no pidió cuesta más caro que \
decirle que no lo tenemos."""

_CACHE: "OrderedDict[tuple, Verdict | None]" = OrderedDict()
_CACHE_MAX = 512

# (input_tokens, output_tokens) cuando la llamada no llegó a facturar nada.
_NO_USAGE = (0, 0)


@dataclass
class Verdict:
    """Lo que dijo el modelo. `product` es None cuando ningún candidato matchea."""

    product: VendureProduct | None
    confidence: float
    reason: str
    provider: str = ""
    model: str = ""
    effort: str = ""
    elapsed_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class Options:
    """Qué modelo corre y con cuánto effort.

    Antes esto salía siempre de la config, así que comparar dos modelos exigía
    cambiar una env var y reiniciar. Pasarlo por parámetro deja barrer varias
    combinaciones en una sola corrida — ver `app/dedup/calibrate_vision.py`.
    """

    provider: str
    model: str
    effort: str


def options(
    provider: str | None = None, model: str | None = None, effort: str | None = None
) -> Options:
    """Completa con la config lo que no se pidió explícitamente."""
    s = get_settings()
    provider = provider or s.vision_provider
    default_model = s.vision_model_openai if provider == OPENAI else s.vision_model
    return Options(provider, model or default_model, effort or s.vision_effort)


@dataclass
class _Payload:
    """Todo lo caro de armar, listo para mandarle a cualquier proveedor."""

    query_raws: list[bytes]
    sheet: bytes
    candidates: list[VendureProduct]
    title: str = ""
    parts: list[tuple[str, object]] = field(default_factory=list)


def available(provider: str | None = None) -> bool:
    """True si el rerank puede usarse con ese proveedor (o con el configurado)."""
    s = get_settings()
    if not s.vision_enabled:
        return False
    provider = provider or s.vision_provider
    if provider == ANTHROPIC:
        return bool(s.anthropic_api_key)
    if provider == OPENAI:
        return bool(s.openai_api_key)
    return False


# ─── Lámina de contactos ───────────────────────────────────────────


def _label_font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1: load_default no acepta size
        return ImageFont.load_default()


def _fit(raw: bytes, cell: int) -> Image.Image:
    """Escala la imagen para que entre en una celda cuadrada, sin recortarla.

    Sin recorte a propósito: un center crop se come los accesorios y los bordes,
    que suelen ser justo lo que distingue dos productos parecidos.
    """
    img = Image.open(BytesIO(raw)).convert("RGB")
    img.thumbnail((cell, cell), Image.LANCZOS)
    canvas = Image.new("RGB", (cell, cell), (255, 255, 255))
    canvas.paste(img, ((cell - img.width) // 2, (cell - img.height) // 2))
    return canvas


def build_contact_sheet(images: Sequence[bytes], cell: int, columns: int = 4) -> bytes:
    """Arma la grilla numerada con las fotos de los candidatos. PNG."""
    tiles = []
    for i, raw in enumerate(images):
        try:
            tiles.append((i + 1, _fit(raw, cell)))
        except Exception:  # noqa: BLE001  (imagen corrupta / formato raro)
            log.debug("No pude abrir la foto del candidato %d", i + 1, exc_info=True)

    if not tiles:
        raise ValueError("ninguna foto de candidato se pudo abrir")

    cols = max(1, min(columns, len(tiles)))
    rows = (len(tiles) + cols - 1) // cols
    band = max(24, cell // 12)  # franja del número, arriba de cada celda
    sheet = Image.new("RGB", (cols * cell, rows * (cell + band)), (245, 245, 245))
    draw = ImageDraw.Draw(sheet)
    font = _label_font(max(16, band - 6))

    for pos, (number, tile) in enumerate(tiles):
        x = (pos % cols) * cell
        y = (pos // cols) * (cell + band)
        draw.rectangle([x, y, x + cell, y + band], fill=(20, 20, 20))
        draw.text((x + 8, y + 2), f"{number}", fill=(255, 255, 255), font=font)
        sheet.paste(tile, (x, y + band))

    buf = BytesIO()
    sheet.save(buf, format="PNG")
    return buf.getvalue()


# ─── Armado del pedido (común a los dos proveedores) ───────────────


def _b64(raw: bytes) -> str:
    return base64.standard_b64encode(raw).decode("ascii")


def _prompt_parts(payload: _Payload) -> list[tuple[str, object]]:
    """El prompt como pares (tipo, contenido). Cada proveedor lo traduce al suyo.

    Tipos: "text" → str, "jpeg"/"png" → bytes.
    """
    title = payload.title
    n = len(payload.candidates)
    return [
        (
            "text",
            f"Producto del cliente{f': {title[:200]}' if title else ''}.\n"
            f"{'Estas son sus fotos' if len(payload.query_raws) > 1 else 'Esta es su foto'}:",
        ),
        *(("jpeg", r) for r in payload.query_raws),
        (
            "text",
            f"Y estos son los {n} candidatos del catálogo, numerados del 1 al {n}:",
        ),
        ("png", payload.sheet),
        (
            "text",
            "¿Alguno es el mismo producto? Nombres del catálogo, en orden:\n"
            + "\n".join(f"{i + 1}. {p.name}" for i, p in enumerate(payload.candidates)),
        ),
    ]


async def _prepare(
    query_urls: Sequence[str], candidates: Sequence[VendureProduct], title: str
) -> _Payload | None:
    """Baja las fotos y arma la lámina. None si algo falla."""
    s = get_settings()
    queries = list(query_urls)[: max(1, s.vision_max_query_images)]
    cand_urls = [p.featured_image_url for p in candidates]

    try:
        raws = await asyncio.gather(*(_fetch(u) for u in queries + cand_urls))
    except Exception:  # noqa: BLE001  (SsrfBlocked, HTTP, tamaño, …)
        log.debug("No pude bajar las fotos para el rerank", exc_info=True)
        return None

    try:
        sheet = await asyncio.to_thread(
            build_contact_sheet, raws[len(queries) :], s.vision_cell_px
        )
    except Exception:  # noqa: BLE001
        log.debug("No pude armar la lámina de contactos", exc_info=True)
        return None

    payload = _Payload(
        query_raws=list(raws[: len(queries)]),
        sheet=sheet,
        candidates=list(candidates),
        title=title,
    )
    payload.parts = _prompt_parts(payload)
    return payload


def _shortlist(
    candidates: Sequence[VendureProduct], topk: int
) -> list[VendureProduct]:
    """Los candidatos que pueden ir a la lámina: con foto, y a lo sumo `topk`.

    El filtro va ANTES del recorte: un candidato sin foto ocuparía un lugar del
    top-K y además correría la numeración de la lámina.
    """
    return [p for p in candidates if p.featured_image_url][: max(1, topk)]


# ─── Cache ─────────────────────────────────────────────────────────

# Centinela: un veredicto cacheado puede ser None legítimamente ("no pude
# correr el rerank"), así que `.get()` no alcanza para distinguir miss de hit.
_MISS = object()


def _cache_get(key: tuple) -> "Verdict | None | object":
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    return _MISS


def _cache_put(key: tuple, value: "Verdict | None") -> None:
    _CACHE[key] = value
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


# ─── API pública ───────────────────────────────────────────────────


async def pick_match(
    query_urls: Sequence[str],
    candidates: Sequence[VendureProduct],
    *,
    title: str = "",
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> Verdict | None:
    """Elige cuál de `candidates` es el producto de `query_urls`, o ninguno.

    Devuelve None cuando el rerank no se pudo correr (sin API key, sin fotos, o
    el modelo falló): eso NO significa "ninguno matchea", significa "no sé" y el
    llamador tiene que caer a los umbrales de CLIP. "Ninguno matchea" se devuelve
    como un Verdict con `product=None`.
    """
    opts = options(provider, model, effort)
    if not available(opts.provider) or not query_urls:
        return None

    candidates = _shortlist(candidates, get_settings().vision_topk)
    if not candidates:
        return None

    queries = list(query_urls)[: max(1, get_settings().vision_max_query_images)]
    # El modelo y el effort van en la key: si no, barrer variantes devolvería
    # el veredicto de la primera para todas.
    key = (opts, tuple(queries), tuple(p.id for p in candidates))
    cached = _cache_get(key)
    if cached is not _MISS:
        return cached  # type: ignore[return-value]

    payload = await _prepare(queries, candidates, title)
    if payload is None:
        return None

    verdict = await _ask(opts, payload)
    _cache_put(key, verdict)
    return verdict


async def compare(
    query_urls: Sequence[str],
    candidates: Sequence[VendureProduct],
    *,
    title: str = "",
) -> dict[str, Verdict | None]:
    """Corre TODOS los proveedores disponibles sobre el mismo pedido.

    Misma lámina, mismas fotos, mismo prompt: la única variable es el modelo. Es
    lo que hace comparables los resultados — y lo que alimenta el endpoint
    `/app/vision-compare`.
    """
    usable = [p for p in PROVIDERS if available(p)]
    if not usable or not query_urls:
        return {}

    candidates = _shortlist(candidates, get_settings().vision_topk)
    if not candidates:
        return {}

    payload = await _prepare(query_urls, candidates, title)
    if payload is None:
        return {}

    results = await asyncio.gather(
        *(_ask(options(p), payload) for p in usable), return_exceptions=True
    )
    out: dict[str, Verdict | None] = {}
    for provider, result in zip(usable, results):
        if isinstance(result, BaseException):
            log.warning("El rerank de %s reventó: %s", provider, result)
            out[provider] = None
        else:
            out[provider] = result
    return out


# ─── Proveedores ───────────────────────────────────────────────────


async def _ask(opts: Options, payload: _Payload) -> Verdict | None:
    started = time.monotonic()
    if opts.provider == ANTHROPIC:
        data, usage = await _call_anthropic(opts, payload)
    elif opts.provider == OPENAI:
        data, usage = await _call_openai(opts, payload)
    else:
        log.warning("Proveedor de visión desconocido: %r", opts.provider)
        return None
    elapsed = int((time.monotonic() - started) * 1000)
    return _parse_verdict(data, payload.candidates, opts, elapsed, usage)


# Modelos que aceptan adaptive thinking + output_config.effort (familia 5 y
# 4.6/4.7/4.8). Haiku 4.5, Sonnet 4.5 y anteriores rechazan ese combo con un 400
# ("no contestó"): hay que pedirles la API vieja — sin thinking adaptive, sin
# effort. Structured outputs (output_config.format) sí lo soportan.
_ADAPTIVE_EFFORT_MODELS = (
    "opus-5", "sonnet-5", "fable-5", "mythos-5",
    "opus-4-6", "opus-4-7", "opus-4-8", "sonnet-4-6",
)


def _supports_adaptive_effort(model: str) -> bool:
    return any(tag in model for tag in _ADAPTIVE_EFFORT_MODELS)


async def _call_anthropic(
    opts: Options, payload: _Payload
) -> tuple[dict | None, tuple[int, int]]:
    s = get_settings()
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        log.warning("El SDK de anthropic no está instalado")
        return None, _NO_USAGE

    content = [
        {"type": "text", "text": value}
        if kind == "text"
        else {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": f"image/{'png' if kind == 'png' else 'jpeg'}",
                "data": _b64(value),  # type: ignore[arg-type]
            },
        }
        for kind, value in payload.parts
    ]

    # El JSON de salida son cuatro líneas, pero el thinking sale del mismo
    # presupuesto: con poco margen la respuesta se corta antes del veredicto.
    # Se factura lo que se usa, así que sobrar no cuesta.
    create_kwargs: dict = {
        "model": opts.model,
        "max_tokens": 8192,
        "system": _SYSTEM,
        "messages": [{"role": "user", "content": content}],
    }
    if _supports_adaptive_effort(opts.model):
        create_kwargs["thinking"] = {"type": "adaptive"}
        create_kwargs["output_config"] = {
            "effort": opts.effort,
            "format": {"type": "json_schema", "schema": _VERDICT_SCHEMA},
        }
    else:
        # Haiku 4.5 / Sonnet 4.5 y anteriores: adaptive thinking y effort dan 400.
        # Structured outputs sí anda. Sin thinking, el modelo responde igual — solo
        # razona menos, que es justo lo que se está midiendo al probar Haiku.
        create_kwargs["output_config"] = {
            "format": {"type": "json_schema", "schema": _VERDICT_SCHEMA},
        }

    client = AsyncAnthropic(api_key=s.anthropic_api_key, timeout=s.vision_timeout_seconds)
    try:
        response = await client.messages.create(**create_kwargs)
    except Exception:  # noqa: BLE001  (red, rate limit, 5xx, …)
        log.warning("El rerank de Anthropic falló", exc_info=True)
        return None, _NO_USAGE

    if response.stop_reason == "refusal":
        log.warning("Anthropic rechazó el pedido del rerank")
        return None, _NO_USAGE

    text = next((b.text for b in response.content if b.type == "text"), "")
    usage = getattr(response, "usage", None)
    return _loads(text), (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


async def _call_openai(
    opts: Options, payload: _Payload
) -> tuple[dict | None, tuple[int, int]]:
    s = get_settings()
    try:
        from openai import AsyncOpenAI
    except ImportError:
        log.warning("El SDK de openai no está instalado")
        return None, _NO_USAGE

    content = [
        {"type": "text", "text": value}
        if kind == "text"
        else {
            "type": "image_url",
            "image_url": {
                "url": f"data:image/{'png' if kind == 'png' else 'jpeg'};base64,"
                f"{_b64(value)}",  # type: ignore[arg-type]
                # La lámina es grande a propósito: en "low" la redimensionan y se
                # pierde justo el detalle que distingue dos productos parecidos.
                "detail": "high",
            },
        }
        for kind, value in payload.parts
    ]

    client = AsyncOpenAI(api_key=s.openai_api_key, timeout=s.vision_timeout_seconds)
    try:
        response = await client.chat.completions.create(
            model=opts.model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": content},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "verdict",
                    "strict": True,
                    "schema": _VERDICT_SCHEMA,
                },
            },
        )
    except Exception:  # noqa: BLE001
        log.warning("El rerank de OpenAI falló", exc_info=True)
        return None, _NO_USAGE

    choice = response.choices[0] if response.choices else None
    if choice is None or getattr(choice.message, "refusal", None):
        log.warning("OpenAI rechazó el pedido del rerank")
        return None, _NO_USAGE

    usage = getattr(response, "usage", None)
    return _loads(choice.message.content or ""), (
        int(getattr(usage, "prompt_tokens", 0) or 0),
        int(getattr(usage, "completion_tokens", 0) or 0),
    )


def _loads(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except ValueError:
        log.warning("El rerank devolvió algo que no es JSON: %r", text[:200])
        return None
    return data if isinstance(data, dict) else None


def _parse_verdict(
    data: dict | None,
    candidates: list[VendureProduct],
    opts: Options,
    elapsed_ms: int,
    usage: tuple[int, int] = _NO_USAGE,
) -> Verdict | None:
    if data is None:
        return None

    index = data.get("match")
    confidence = float(data.get("confidence") or 0.0)
    reason = str(data.get("reason") or "")[:300]

    def _verdict(product: VendureProduct | None) -> Verdict:
        return Verdict(
            product, confidence, reason,
            opts.provider, opts.model, opts.effort, elapsed_ms, usage[0], usage[1],
        )

    if index is None:
        log.info("[%s] ningún candidato matchea (%s)", opts.model, reason)
        return _verdict(None)

    # El modelo numera desde 1. Un índice fuera de rango es una alucinación:
    # tratarlo como "no sé" y caer a CLIP es más seguro que agarrar otro producto.
    if not isinstance(index, int) or isinstance(index, bool):
        log.warning("[%s] devolvió un match que no es entero (%r)", opts.model, index)
        return None
    if not 1 <= index <= len(candidates):
        log.warning("[%s] devolvió un índice fuera de rango (%r)", opts.model, index)
        return None

    product = candidates[index - 1]
    log.info(
        "[%s] eligió '%s' (conf %.2f, %d ms): %s",
        opts.model, product.name[:60], confidence, elapsed_ms, reason,
    )
    return _verdict(product)
