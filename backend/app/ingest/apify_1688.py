"""Búsqueda en 1688 por foto con un actor de Apify (cotizador rápido por foto).

Para qué: cuando el producto que pega el cliente no está en nuestro catálogo, el
cotizador necesita candidatos de 1688 parecidos a la foto para estimar un rango
de precio. El actor `piotrv1001/1688-image-search-scraper` sube la foto al
buscador por imagen de 1688 y devuelve hasta 20 ofertas (id, título en chino,
precio del primer tramo, miniatura, similitud, vendidos, empresa).

Medido el 20-oct-2026 (`_notas-b2box/cotizador-rapido-foto-medicion.md`): p50
28 s, p90 52 s y 1 de cada 6 corridas colgada hasta el timeout sin crawlear
nada, aunque la misma foto reintentada vuelve en ~19 s. Por eso no se usa
`run-sync` como en apify_meli: se lanza la corrida, se la sigue con polling y,
si a los `apify_1688_hedge_after_s` (12 s) no terminó, se lanza una segunda
igual ("corrida de cobertura"). Gana la primera que termina con candidatos y la
otra se aborta; al deadline (38 s) se abortan todas y vuelve `timeout`. Según el
pricing del actor, las corridas sin resultados no se cobran.

Gasto y abuso, igual que apify_meli:
  * se prende con APIFY_TOKEN y cupo diario runtime (`apify_1688_daily_budget`,
    0 = apagado, que es el default); CADA corrida lanzada reserva cupo, atómico
    y fail-closed;
  * el actor va por lista blanca (cambiarlo cambia el formato de salida);
  * caché LRU por hash de la foto: un hit no gasta cupo;
  * semáforo propio de 3 búsquedas; si están ocupadas se contesta `busy`.

Seguridad: el token viaja SOLO en el header Authorization y nunca se loguea (ni
la URL de la foto, que puede ser una URL firmada de un bucket: solo su host). A
Apify solo va una foto https con host público. Lo que vuelve es input de un
tercero: offerId solo dígitos, foto https de alicdn/1688/aliimg, precio con
rango sano, títulos de una línea, respuestas de más de 2 MB descartadas.

Nunca lanza: cualquier sorpresa termina en `status="error"` y el lookup sigue.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from urllib.parse import quote

import httpx

from app.config import get_settings
from app.net_guard import _ip_is_public
from app.pricing import daily_budget
from app.pricing.market_ml import MAX_ANY_URL_CHARS, _clean_https_parts
from app.pricing.market_ml_web import clean_line

log = logging.getLogger(__name__)

Status = Literal["ok", "no_candidates", "over_budget", "disabled", "timeout", "error", "busy"]

API_BASE = "https://api.apify.com/v2"
COUNTER_KEY = "_meta:apify_1688_calls_today"
# Actores cuyo formato de salida conoce `normalize`. Uno nuevo entra por PR.
_ACTOR_ALLOWLIST = frozenset({"piotrv1001/1688-image-search-scraper"})
# Build o tag fijado (`1.0.12`, `latest`): va al query de la URL.
_BUILD_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
# Ids de Apify (corrida, dataset): van en el path, nada más entra.
_APIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{1,40}$")
_MAX_RESULTS = 20
# 20 ofertas pesan ~20-30 KB de JSON. 2 MB corta a un actor que se vuelva loco
# (o a una respuesta hostil) antes de llenar memoria.
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# Techo de cobro por corrida que Apify respeta. Una corrida cuesta USD 0,02 por
# imagen: el techo es para que un cambio de precio del actor no se note recién
# en la factura.
_MAX_CHARGE_USD = 0.05
# Lo que se imputa por corrida si `usageTotalUsd` viene más bajo (no siempre
# incluye el cargo por imagen): el gasto se estima para arriba, nunca para abajo.
_MIN_USD_PER_RUN = 0.02
_ACTOR_MEMORY_MB = 1024
# Timeout del lado de Apify. Pasa el deadline a propósito: quien corta es el
# deadline (y aborta); esto es la red por si el abort no llega.
_ACTOR_TIMEOUT_S = 40
# Long polling: Apify retiene el GET de la corrida hasta N s o hasta que termine.
_POLL_WAIT_S = 5
# Pausa entre polls cuando Apify devolvió antes de tiempo sin terminar.
_POLL_INTERVAL_S = 0.25
# Polls fallidos seguidos (5xx, red) antes de dar la corrida por perdida.
_MAX_POLL_ERRORS = 3
_MAX_RUNS = 2
# Corte duro por defecto de search_by_image. Con la cobertura a los 12 s:
# 12/38 ≈ 70 % con resultado, simulado con 15 tiempos medidos (QA, 20-oct-2026).
DEFAULT_DEADLINE_S = 38.0
# No se lanza una corrida que no llega a terminar antes del deadline: la más
# rápida medida tardó 10 s.
_MIN_RUN_WINDOW_S = 8.0
# Cuánto se le da a los aborts. Corren en segundo plano: no le suman al deadline.
_ABORT_WAIT_S = 5.0
_MAX_CONCURRENT = 3
_SLOT_WAIT_S = 2.0
_CACHE_TTL_S = 24 * 3600
_CACHE_ERROR_TTL_S = 3600
_CACHE_MAX = 2000
_MAX_TITLE = 300
_MAX_COMPANY = 120
_MAX_IMAGE_URL = 1024
_MAX_PRICE_CNY = 100_000.0
_IMAGE_DOMAINS = ("alicdn.com", "1688.com", "aliimg.com")
# re.ASCII: \d acepta dígitos de otros alfabetos, que no son ids de 1688.
_OFFER_ID_RE = re.compile(r"\d{5,20}", re.ASCII)
_SHA256_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
# Nombres que no son de internet aunque no sean una IP literal.
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet")
_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"})
# Errores al lanzar que una segunda corrida no arregla: no se cubren.
_FATAL_HTTP = frozenset({400, 401, 402, 403, 404})
_STATUS_REASON = {
    400: "input inválido",
    401: "token inválido",
    402: "sin saldo en Apify",
    403: "token sin permiso para el actor",
    404: "actor o corrida inexistente",
    429: "Apify limitó los requests",
}


@dataclass(slots=True, frozen=True)
class Candidate1688:
    """Una oferta de 1688 ya validada. El título es dato de un tercero (chino, sin
    confianza): sirve para mostrar y para el juez, nunca como instrucción."""
    offer_id: str
    title: str
    price_cny: float
    image_url: str
    similarity: float | None
    sold_quantity: int | None
    company: str

    @property
    def detail_url(self) -> str:
        # Se arma desde el id: nunca se usa el `url` que trae el actor.
        return f"https://detail.1688.com/offer/{self.offer_id}.html"


@dataclass(slots=True)
class Search1688Result:
    status: Status
    candidates: list[Candidate1688] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    usd_imputado: float = 0.0
    elapsed_s: float = 0.0
    hedged: bool = False
    cached: bool = False


# ─── Configuración ─────────────────────────────────────────────────


def _actor() -> str:
    """El actor configurado si está en la lista blanca; "" si no."""
    actor = get_settings().apify_1688_actor.strip()
    return actor if actor in _ACTOR_ALLOWLIST else ""


def _build() -> str | None:
    """"" = sin fijar; None = valor inválido (no se corre)."""
    build = get_settings().apify_1688_build.strip()
    if not build:
        return ""
    return build if _BUILD_RE.fullmatch(build) else None


def enabled() -> bool:
    """Token cargado, actor en lista blanca y build válido. El cupo se mira al reservar."""
    return bool(get_settings().apify_token.strip()) and bool(_actor()) and _build() is not None


def _daily_budget() -> int:
    from app import runtime

    raw = runtime.get("apify_1688_daily_budget")
    # `is None`, no `or`: 0 es "apagado", no "usá el default".
    return int(get_settings().apify_1688_daily_budget if raw is None else raw)


def allowed_image_url(url: object) -> str:
    """La foto que se le puede mandar a Apify, o "" si no.

    Solo https, sin userinfo ni puertos raros, y con un host de internet: ni IPs
    privadas/loopback ni nombres locales. No se resuelve DNS: quien descarga es
    Apify, no Hugo; lo que se evita es mandarle a un tercero una dirección
    interna. El fragmento no viaja y el host va en minúsculas (clave de caché).
    """
    parts = _clean_https_parts(url, max_chars=MAX_ANY_URL_CHARS)
    if parts is None:
        return ""
    host = parts.hostname.lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        # Un nombre: tiene que tener dominio, y un TLD nunca es numérico
        # (`https://2130706433/` o `0x7f.1` son 127.0.0.1 escritos raro).
        tld = host.rsplit(".", 1)[-1]
        if "." not in host or tld.isdigit() or tld.startswith("0x"):
            return ""
    else:
        if not _ip_is_public(host):
            return ""
    return parts._replace(netloc=parts.netloc.lower(), fragment="").geturl()


def _actor_input(image_url: str) -> dict[str, Any]:
    # Mismo input que Paco PRO (apify_search.py), con 20 resultados.
    return {
        "imageUrls": [image_url],
        "maxResultsPerImage": _MAX_RESULTS,
        "excludeSponsored": True,
        "proxyConfiguration": {"useApifyProxy": True},
    }


def _start_url(actor: str, build: str) -> str:
    url = (
        f"{API_BASE}/acts/{quote(actor.replace('/', '~'), safe='~')}/runs"
        f"?timeout={_ACTOR_TIMEOUT_S}&memory={_ACTOR_MEMORY_MB}&maxItems={_MAX_RESULTS}"
        f"&maxTotalChargeUsd={_MAX_CHARGE_USD}"
    )
    return f"{url}&build={quote(build, safe='')}" if build else url


def _host(url: str) -> str:
    """Para los logs: la URL de la foto puede ser una firmada, el host no."""
    parts = _clean_https_parts(url, max_chars=MAX_ANY_URL_CHARS)
    return parts.hostname.lower() if parts is not None else "?"


# ─── Normalización de la salida ────────────────────────────────────


def _offer_id(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    # "abb-" es el prefijo con el que OTAPI escribe los ids de 1688.
    raw = str(value).strip().removeprefix("abb-")
    return raw if _OFFER_ID_RE.fullmatch(raw) else None


def _number(value: object) -> float | None:
    """int, float o string numérico finito; None para todo lo demás (bool incluido)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if len(value) > 32:
            return None
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        # Un entero JSON de cientos de dígitos: no entra en un float.
        return None
    return number if math.isfinite(number) else None


def _price_cny(value: object) -> float | None:
    price = _number(value)
    # ¥0 es "consultar"; ¥100.000 o más es basura o un precio carnada al revés.
    return price if price is not None and 0 < price < _MAX_PRICE_CNY else None


def _similarity(value: object) -> float | None:
    """El score del actor, si es un número sano. Medido viene entre 0 y ~1,1;
    se acepta hasta 100 por si un build lo pasa a porcentaje. Fuera de eso es
    basura, y un 1e308 rompería cualquier promedio del filtro de después."""
    score = _number(value)
    return score if score is not None and 0 <= score <= 100 else None


def _count(*values: object) -> int | None:
    for value in values:
        number = _number(value)
        if number is not None and number >= 0:
            return int(min(number, 10**12))
    return None


def safe_image_url(url: object) -> str | None:
    """Miniatura de un candidato: https de un CDN de Alibaba, o None."""
    parts = _clean_https_parts(url, max_chars=_MAX_IMAGE_URL)
    if parts is None:
        return None
    host = parts.hostname.lower()
    if not any(host == d or host.endswith("." + d) for d in _IMAGE_DOMAINS):
        return None
    return parts.geturl()


def _candidate(item: object) -> Candidate1688 | None:
    if not isinstance(item, dict):
        return None
    offer_id = _offer_id(item.get("offerId"))
    image_url = safe_image_url(item.get("imageUrl"))
    price = _price_cny(item.get("price"))
    # Sin foto confiable no se puede verificar con CLIP; sin precio no sirve
    # para el rango.
    if offer_id is None or image_url is None or price is None:
        return None
    return Candidate1688(
        offer_id=offer_id,
        title=clean_line(item.get("title"), _MAX_TITLE),
        price_cny=price,
        image_url=image_url,
        similarity=_similarity(item.get("similarityScore")),
        sold_quantity=_count(item.get("soldQuantity"), item.get("ordersCount")),
        company=clean_line(item.get("companyName"), _MAX_COMPANY),
    )


def normalize(items: object) -> list[Candidate1688]:
    """Dataset del actor → candidatos válidos, sin repetir offerId.

    Quedan en el orden del actor A PROPÓSITO: medido, ese orden no sigue al
    `similarityScore` ni al parecido real. Reordenar (CLIP contra la foto) es
    trabajo del filtro del cotizador, no de este módulo.
    """
    if not isinstance(items, list):
        return []
    out: list[Candidate1688] = []
    seen: set[str] = set()
    for item in items[: _MAX_RESULTS * 3]:
        cand = _candidate(item)
        if cand is None or cand.offer_id in seen:
            continue
        seen.add(cand.offer_id)
        out.append(cand)
        if len(out) >= _MAX_RESULTS:
            break
    return out


# ─── HTTP con tope ─────────────────────────────────────────────────


async def _request_bounded(client: httpx.AsyncClient, method: str, url: str, headers: dict,
                           body: dict | None = None) -> tuple[int, bytes | None]:
    """Request leyendo como mucho _MAX_RESPONSE_BYTES. (status, body | None si se pasó).
    Con un status que no es 2xx el body no se lee (puede traer el input o la cuenta)."""
    async with client.stream(method, url, json=body, headers=headers) as resp:
        if resp.status_code not in (200, 201):
            return resp.status_code, b""
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > _MAX_RESPONSE_BYTES:
                return resp.status_code, None
            chunks.append(chunk)
        return resp.status_code, b"".join(chunks)


def _new_client(total_s: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(total_s, connect=10.0))


def _json(body: bytes | None) -> Any:
    if not body:
        return None
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        # RecursionError: un JSON con cien mil corchetes anidados.
        return None


# ─── Una corrida ───────────────────────────────────────────────────


@dataclass(slots=True)
class _Run:
    """Estado de una corrida. Lo escribe `_drive` a medida que avanza, así quien
    la cancela sabe qué id abortar aunque la tarea no haya terminado."""
    n: int
    run_id: str = ""
    dataset_id: str = ""
    status: str = ""
    usage_usd: float = 0.0

    @property
    def finished(self) -> bool:
        return self.status in _TERMINAL

    def absorb(self, payload: object) -> bool:
        """Toma `data` de una respuesta de Apify. False si no tiene forma de corrida."""
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return False
        run_id = data.get("id")
        if not self.run_id:
            if not isinstance(run_id, str) or not _APIFY_ID_RE.fullmatch(run_id):
                return False
            self.run_id = run_id
        dataset_id = data.get("defaultDatasetId")
        if isinstance(dataset_id, str) and _APIFY_ID_RE.fullmatch(dataset_id):
            self.dataset_id = dataset_id
        status = data.get("status")
        if isinstance(status, str) and len(status) <= 20:
            self.status = status.upper()
        usage = _number(data.get("usageTotalUsd"))
        if usage is not None and usage >= 0:
            self.usage_usd = usage
        return True


# Resultado de una corrida: ok (con candidatos), empty (terminó sin candidatos
# válidos), failed (falla transitoria: vale cubrirla) o fatal (otra corrida
# fallaría igual: token, saldo, respuesta gigante).
_Outcome = tuple[Literal["ok", "empty", "failed", "fatal"], list[Candidate1688]]


async def _drive(http: httpx.AsyncClient, run: _Run, actor: str, build: str,
                 image_url: str, headers: dict) -> _Outcome:
    """Lanza una corrida, la sigue hasta que termina y lee su dataset. No lanza
    (salvo CancelledError, que es como el orquestador la corta)."""
    try:
        return await _drive_inner(http, run, actor, build, image_url, headers)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        # Solo el tipo: el str de algunas excepciones de httpx arrastra el request.
        log.warning("Apify 1688 corrida %d: error %s", run.n, type(exc).__name__)
        return "failed", []


async def _drive_inner(http: httpx.AsyncClient, run: _Run, actor: str, build: str,
                       image_url: str, headers: dict) -> _Outcome:
    status, body = await _request_bounded(http, "POST", _start_url(actor, build), headers,
                                          _actor_input(image_url))
    if status not in (200, 201):
        log.warning("Apify 1688 corrida %d: HTTP %s (%s) al lanzar", run.n, status,
                    _STATUS_REASON.get(status, "error"))
        return ("fatal" if status in _FATAL_HTTP else "failed"), []
    if not run.absorb(_json(body)):
        log.warning("Apify 1688 corrida %d: respuesta sin corrida al lanzar", run.n)
        return "failed", []

    errors = 0
    while not run.finished:
        status, body = await _request_bounded(
            http, "GET", f"{API_BASE}/actor-runs/{run.run_id}?waitForFinish={_POLL_WAIT_S}",
            headers)
        if status in (200, 201) and run.absorb(_json(body)):
            errors = 0
        else:
            errors += 1
            if errors >= _MAX_POLL_ERRORS:
                log.warning("Apify 1688 corrida %d (%s): %d polls fallidos (último HTTP %s)",
                            run.n, run.run_id, errors, status)
                return "failed", []
        if not run.finished:
            await asyncio.sleep(_POLL_INTERVAL_S)

    if run.status != "SUCCEEDED":
        log.warning("Apify 1688 corrida %d (%s) terminó %s", run.n, run.run_id, run.status)
        return "failed", []
    if not run.dataset_id:
        log.warning("Apify 1688 corrida %d (%s): sin dataset", run.n, run.run_id)
        return "failed", []
    status, body = await _request_bounded(
        http, "GET",
        f"{API_BASE}/datasets/{run.dataset_id}/items?clean=true&format=json&limit={_MAX_RESULTS}",
        headers)
    if status not in (200, 201):
        log.warning("Apify 1688 corrida %d (%s): HTTP %s al leer el dataset", run.n,
                    run.run_id, status)
        return "failed", []
    if body is None:
        log.warning("Apify 1688 corrida %d (%s): dataset de más de %d bytes, descartado",
                    run.n, run.run_id, _MAX_RESPONSE_BYTES)
        return "fatal", []
    items = _json(body)
    if not isinstance(items, list):
        log.warning("Apify 1688 corrida %d (%s): dataset no-JSON", run.n, run.run_id)
        return "failed", []
    candidates = normalize(items)
    return ("ok" if candidates else "empty"), candidates


# ─── Aborts en segundo plano ───────────────────────────────────────

# Tareas de abort en curso (referencia fuerte: si no, el GC las puede matar).
_background: set[asyncio.Task] = set()


def _abort_later(run_ids: list[str], headers: dict) -> None:
    """Aborta corridas sin hacer esperar al llamador: el deadline del lookup no
    paga los segundos del abort. Abortar antes de que haya matches no se cobra."""
    if not run_ids:
        return
    task = asyncio.create_task(_abort_runs(run_ids, headers))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _abort_runs(run_ids: list[str], headers: dict) -> None:
    http = _new_client(_ABORT_WAIT_S)
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(_request_bounded(http, "POST", f"{API_BASE}/actor-runs/{rid}/abort",
                                              headers) for rid in run_ids),
                           return_exceptions=True),
            timeout=_ABORT_WAIT_S)
        for rid, res in zip(run_ids, results):
            if isinstance(res, BaseException):
                log.warning("Apify 1688: no se pudo abortar %s (%s)", rid, type(res).__name__)
            elif res[0] not in (200, 201):
                log.warning("Apify 1688: abort de %s devolvió HTTP %s", rid, res[0])
            else:
                log.info("Apify 1688: corrida %s abortada", rid)
    except Exception as exc:  # noqa: BLE001
        log.warning("Apify 1688: aborts sin confirmar (%s)", type(exc).__name__)
    finally:
        await http.aclose()


async def wait_pending_aborts() -> None:
    """Espera los aborts en curso (tests y apagado ordenado)."""
    if _background:
        await asyncio.gather(*list(_background), return_exceptions=True)


# ─── Caché y concurrencia ──────────────────────────────────────────

# clave → (vence_en monotonic, resultado). OrderedDict como LRU.
_cache: OrderedDict[str, tuple[float, Search1688Result]] = OrderedDict()
# Un semáforo por event loop (ver apify_meli._semaphore).
_slots: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _reset_for_tests() -> None:
    global _slots
    _cache.clear()
    _slots = None


def _semaphore() -> asyncio.Semaphore:
    global _slots
    loop = asyncio.get_running_loop()
    if _slots is None or _slots[0] is not loop:
        _slots = (loop, asyncio.Semaphore(_MAX_CONCURRENT))
    return _slots[1]


def _cache_key(image_url: str, trusted_bytes_sha256: str | None) -> str:
    """La misma foto, la misma clave: el sha256 de los bytes si Hugo lo calculó
    (la misma foto subida dos veces trae otra URL firmada); si no, el de la URL
    ya normalizada. El hash es de confianza solo si lo calculó Hugo (ver
    search_by_image)."""
    digest = trusted_bytes_sha256.strip().lower() if isinstance(trusted_bytes_sha256, str) else ""
    if _SHA256_RE.fullmatch(digest):
        return f"bytes:{digest}"
    return f"url:{hashlib.sha256(image_url.encode()).hexdigest()}"


def _cache_get(key: str) -> Search1688Result | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    expires_at, result = hit
    if time.monotonic() >= expires_at:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return result


def _cache_put(key: str, result: Search1688Result, ttl_s: float) -> None:
    _cache[key] = (time.monotonic() + ttl_s, result)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


# ─── Búsqueda ──────────────────────────────────────────────────────


async def search_by_image(image_url: str, *, trusted_bytes_sha256: str | None = None,
                          deadline_s: float = DEFAULT_DEADLINE_S) -> Search1688Result:
    """Busca la foto en 1688. Nunca lanza.

    `trusted_bytes_sha256`: sha256 (hex) de los bytes de la foto, para la caché.
    Lo calcula Hugo sobre los bytes que él mismo descargó o recibió; NUNCA un
    valor que mande el cliente: con un hash ajeno alguien podría leer de la
    caché los candidatos de la foto de otro, o envenenar la clave de una foto
    con los resultados de otra.
    `deadline_s`: corte duro desde que se llama; ahí se abortan las corridas.

    Status: ok | no_candidates (el actor terminó y no trajo nada usable) |
    over_budget (cupo en 0 o agotado: no se llamó) | disabled (sin token o actor
    fuera de lista blanca) | timeout | error (incluye una foto que no se puede
    mandar) | busy (las 3 búsquedas en curso).
    """
    started = time.monotonic()
    if (isinstance(deadline_s, bool) or not isinstance(deadline_s, (int, float))
            or not math.isfinite(deadline_s) or deadline_s <= 0):
        # NaN dejaba el loop sin corte; <= 0 gastaba cupo sin lanzar nada.
        log.warning("Apify 1688: deadline inválido, no se llama")
        return Search1688Result(status="error")
    try:
        result = await _search(image_url, trusted_bytes_sha256, deadline_s, started)
    except Exception as exc:  # noqa: BLE001
        # Solo el tipo: el mensaje puede arrastrar el request o el body.
        log.warning("Apify 1688: error inesperado %s, sigo sin Apify", type(exc).__name__)
        result = Search1688Result(status="error")
    result.elapsed_s = round(time.monotonic() - started, 3)
    return result


async def _search(image_url: str, trusted_bytes_sha256: str | None, deadline_s: float,
                  started: float) -> Search1688Result:
    s = get_settings()
    if not s.apify_token.strip():
        return Search1688Result(status="disabled")
    actor, build = _actor(), _build()
    if not actor or build is None:
        log.warning("Apify 1688: actor o build fuera de la lista blanca, no se llama")
        return Search1688Result(status="disabled")
    target = allowed_image_url(image_url)
    if not target:
        log.info("Apify 1688: la foto no es una URL https pública, no se manda")
        return Search1688Result(status="error")
    key = _cache_key(target, trusted_bytes_sha256)
    cached = _cache_get(key)
    if cached is not None:
        log.info("Apify 1688: foto en caché (%s), no se paga de nuevo", cached.status)
        return replace(cached, candidates=list(cached.candidates), run_ids=list(cached.run_ids),
                       usd_imputado=0.0, hedged=False, cached=True)
    budget = _daily_budget()
    if budget <= 0:
        return Search1688Result(status="over_budget")

    slots = _semaphore()
    try:
        await asyncio.wait_for(slots.acquire(), timeout=_SLOT_WAIT_S)
    except asyncio.TimeoutError:
        log.info("Apify 1688: %d búsquedas en curso, salteo", _MAX_CONCURRENT)
        return Search1688Result(status="busy")
    try:
        reserved = await _reserve(budget, started + deadline_s)
        if reserved is None:
            return Search1688Result(status="timeout")
        if not reserved:
            log.info("Apify 1688: cupo diario agotado (%d), no se manda", budget)
            return Search1688Result(status="over_budget")
        result = await _hedged_search(actor, build, target, budget, started + deadline_s,
                                      started + float(s.apify_1688_hedge_after_s))
        ttl = _CACHE_TTL_S if result.status in ("ok", "no_candidates") else _CACHE_ERROR_TTL_S
        _cache_put(key, result, ttl)
        log.info("Apify 1688 %s: %s, %d candidatos, %d corridas%s, %.1fs (foto de %s)", actor,
                 result.status, len(result.candidates), len(result.run_ids),
                 " con cobertura" if result.hedged else "", time.monotonic() - started,
                 _host(target))
        return result
    finally:
        slots.release()


async def _reserve(budget: int, deadline_at: float) -> bool | None:
    """Reserva una corrida sin pasarse del deadline: la reserva es I/O de DB en
    un thread y una base lenta no puede estirar el lookup. True = reservada,
    False = sin cupo, None = la base no contestó a tiempo (cuenta como sin cupo;
    la reserva puede entrar igual: se pierde una unidad, nunca se gasta de más)."""
    try:
        return await asyncio.wait_for(daily_budget.reserve_async(COUNTER_KEY, budget),
                                      timeout=max(deadline_at - time.monotonic(), 0.0)) is not None
    except asyncio.TimeoutError:
        log.warning("Apify 1688: la reserva de cupo no contestó antes del deadline")
        return None


async def _hedged_search(actor: str, build: str, target: str, budget: int,
                         deadline_at: float, hedge_at: float) -> Search1688Result:
    """La corrida 1 ya tiene cupo. Lanza la de cobertura si hace falta (y hay
    cupo), se queda con la primera que trae candidatos y aborta el resto."""
    headers = {"Authorization": f"Bearer {get_settings().apify_token.strip()}",
               "Accept": "application/json"}
    http = _new_client(_POLL_WAIT_S + 10.0)
    runs: list[_Run] = []
    tasks: dict[asyncio.Task, _Run] = {}
    kinds: list[str] = []
    winner: _Run | None = None
    candidates: list[Candidate1688] = []
    may_hedge = True

    def launch() -> None:
        run = _Run(n=len(runs) + 1)
        runs.append(run)
        tasks[asyncio.create_task(_drive(http, run, actor, build, target, headers))] = run

    try:
        launch()
        while tasks:
            now = time.monotonic()
            if now >= deadline_at:
                break
            wake_at = deadline_at
            if may_hedge and len(runs) < _MAX_RUNS and now < hedge_at:
                wake_at = min(wake_at, hedge_at)
            done, _ = await asyncio.wait(tasks, timeout=wake_at - now,
                                         return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                run = tasks.pop(task)
                kind, found = task.result()
                kinds.append(kind)
                if kind == "ok" and winner is None:
                    winner, candidates = run, found
                # Una respuesta definitiva (el actor no encontró nada) o una falla
                # que se repetiría: no tiene sentido pagar otra corrida.
                may_hedge = may_hedge and kind == "failed"
            if winner is not None:
                break
            now = time.monotonic()
            due = now >= hedge_at or not tasks
            if may_hedge and len(runs) < _MAX_RUNS and due and deadline_at - now < _MIN_RUN_WINDOW_S:
                may_hedge = False
            if may_hedge and len(runs) < _MAX_RUNS and due:
                if not await _reserve(budget, deadline_at):
                    log.info("Apify 1688: sin cupo para la corrida de cobertura")
                    may_hedge = False
                else:
                    log.info("Apify 1688: corrida de cobertura (la 1 %s)",
                             "falló" if not tasks else "sigue sin terminar")
                    launch()
    finally:
        timed_out = bool(tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await http.aclose()
        _abort_later([r.run_id for r in runs if r is not winner and r.run_id and not r.finished],
                     headers)

    if winner is not None:
        status: Status = "ok"
    elif "empty" in kinds:
        status = "no_candidates"
    elif timed_out:
        status = "timeout"
    else:
        status = "error"
    launched = [r for r in runs if r.run_id]
    return Search1688Result(
        status=status,
        candidates=candidates,
        run_ids=[r.run_id for r in launched],
        usd_imputado=round(sum(max(r.usage_usd, _MIN_USD_PER_RUN) for r in launched), 6),
        hedged=len(runs) > 1,
    )
