"""Cliente de la API oficial de MercadoLibre.

Por qué existe este módulo: ML no le contesta a un servidor. Pedirle la ficha de
un producto desde la IP de Hugo devuelve HTTP 200 con su interstitial de
"tráfico sospechoso" — sin og:image, sin JSON-LD, sin nada que parsear. Y su API
pública dejó de ser pública (403 `PA_UNAUTHORIZED_RESULT_FROM_POLICIES`).

La puerta legítima es la API con OAuth: se registra una aplicación en
developers.mercadolibre.com, y con client_id + client_secret se pide un token de
tipo `client_credentials` (no hace falta que ningún usuario autorice nada, porque
solo leemos publicaciones públicas).

Sin credenciales configuradas el módulo se apaga solo (`enabled()` → False) y
`image_from_url` sigue con el scraping de siempre, que para ML va a fallar pero
para el resto de los sitios funciona.

Endpoints que usamos:
    POST /oauth/token          → access_token (dura ~6 h)
    GET  /items/{MLA123}       → publicación puntual: pictures[], title
    GET  /products/{MLA123}    → ficha de catálogo (las URLs /up/ y /p/)
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

API_BASE = "https://api.mercadolibre.com"
_TIMEOUT = httpx.Timeout(15.0, connect=6.0)
# Renovamos el token un rato antes de que venza, para no cortarnos en el medio
# de un lookup por un token que expiró entre el chequeo y el request.
_TOKEN_MARGIN_SECONDS = 300
_MAX_PICTURES = 6

# ─── Formas de URL que usa ML ──────────────────────────────────────
# Las publicaciones viven en varias formas y conviene reconocerlas todas:
#   articulo.mercadolibre.com.ar/MLA-1755665491-nombre-_JM   → item MLA1755665491
#   mercadolibre.com.ar/algo/up/MLAU3916488174               → ficha de catálogo
#   mercadolibre.com.ar/p/MLA123456789                       → ficha de catálogo
#   ...?pdp_filters=item_id:MLA1755665491                    → item, dentro del query
_RE_ITEM_DASHED = re.compile(r"/(ML[A-Z]|MCO)-?(\d{6,})", re.I)
_RE_CATALOG_PATH = re.compile(r"/(?:up|p)/((?:ML[A-Z]U?|MCO)\d{6,})", re.I)
_RE_ITEM_ID_ANY = re.compile(r"item_id[:=]((?:ML[A-Z]|MCO)\d{6,})", re.I)
_RE_BARE_ID = re.compile(r"\b((?:ML[A-Z]U?|MCO)\d{6,})\b", re.I)


class MeliError(RuntimeError):
    pass


@dataclass(slots=True)
class MeliItem:
    id: str
    title: str = ""
    image_urls: list[str] = field(default_factory=list)
    permalink: str = ""


@dataclass(slots=True)
class ParsedRef:
    """Qué identificamos en la URL: una publicación o una ficha de catálogo."""
    id: str
    kind: str  # "item" | "product"


# ─── Parseo de la URL ──────────────────────────────────────────────


def parse_url(url: str) -> ParsedRef | None:
    """Saca el id de publicación (o de catálogo) de una URL de MercadoLibre.

    Prioriza el `item_id` explícito del query: cuando la URL trae los dos, ese
    apunta a la publicación concreta que el cliente estaba mirando, mientras que
    el id de la ruta puede ser la ficha de catálogo genérica.
    """
    if not url:
        return None
    decoded = unquote(url)

    m = _RE_ITEM_ID_ANY.search(decoded)
    if m:
        return ParsedRef(id=m.group(1).upper(), kind="item")

    # pdp_filters puede venir url-encodeado dentro del query.
    try:
        query = parse_qs(urlparse(decoded).query)
    except ValueError:
        query = {}
    for values in query.values():
        for v in values:
            m = _RE_ITEM_ID_ANY.search(unquote(v))
            if m:
                return ParsedRef(id=m.group(1).upper(), kind="item")

    path = urlparse(decoded).path
    m = _RE_CATALOG_PATH.search(path)
    if m:
        return ParsedRef(id=m.group(1).upper(), kind="product")

    m = _RE_ITEM_DASHED.search(path)
    if m:
        return ParsedRef(id=f"{m.group(1).upper()}{m.group(2)}", kind="item")

    m = _RE_BARE_ID.search(path)
    if m:
        raw = m.group(1).upper()
        # Los ids de catálogo llevan una U extra (MLAU…); los de publicación no.
        kind = "product" if re.match(r"^ML[A-Z]U\d+$", raw) else "item"
        return ParsedRef(id=raw, kind=kind)
    return None


# ─── Token (client_credentials) ────────────────────────────────────

_token: str = ""
_token_expires_at: float = 0.0
_token_lock = asyncio.Lock()


def enabled() -> bool:
    s = get_settings()
    return bool(s.meli_client_id and s.meli_client_secret)


def _reset_token_for_tests() -> None:
    global _token, _token_expires_at
    _token, _token_expires_at = "", 0.0


async def get_token() -> str:
    """Devuelve un access_token válido, pidiéndolo solo cuando hace falta."""
    global _token, _token_expires_at
    now = time.monotonic()
    if _token and now < _token_expires_at:
        return _token

    async with _token_lock:
        now = time.monotonic()
        if _token and now < _token_expires_at:
            return _token

        s = get_settings()
        if not enabled():
            raise MeliError("MELI_CLIENT_ID / MELI_CLIENT_SECRET no configurados")

        payload = {
            "grant_type": "client_credentials",
            "client_id": s.meli_client_id,
            "client_secret": s.meli_client_secret,
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{API_BASE}/oauth/token",
                data=payload,
                headers={"Accept": "application/json"},
            )
        if resp.status_code >= 400:
            log.error("MELI token: %s %s", resp.status_code, resp.text[:250])
            raise MeliError(f"No se pudo obtener el token de ML: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise MeliError("Respuesta no-JSON al pedir el token de ML") from exc

        token = data.get("access_token")
        if not token:
            raise MeliError(f"Respuesta sin access_token: {str(data)[:150]}")
        _token = str(token)
        _token_expires_at = time.monotonic() + max(
            60, int(data.get("expires_in", 21600)) - _TOKEN_MARGIN_SECONDS
        )
        log.info("Token de MercadoLibre renovado (vence en %ss)", data.get("expires_in"))
        return _token


# ─── Lectura de publicaciones ──────────────────────────────────────


def _pictures_from(raw: dict) -> list[str]:
    """Saca las URLs de foto de un item o de una ficha de catálogo.

    Los items traen `pictures[].secure_url`; las fichas de catálogo traen
    `pictures[].url`. Se prefiere https cuando están las dos.
    """
    out: list[str] = []
    for pic in (raw.get("pictures") or []):
        if not isinstance(pic, dict):
            continue
        url = pic.get("secure_url") or pic.get("url")
        if url and url not in out:
            out.append(url)
    if not out:
        thumb = raw.get("thumbnail") or raw.get("secure_thumbnail")
        if thumb:
            out.append(thumb)
    return out[:_MAX_PICTURES]


async def _get(path: str) -> dict:
    token = await get_token()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{API_BASE}{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
    if resp.status_code == 404:
        raise MeliError(f"ML no encontró {path}")
    if resp.status_code >= 400:
        log.warning("MELI %s: %s %s", path, resp.status_code, resp.text[:200])
        raise MeliError(f"HTTP {resp.status_code} en {path}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise MeliError(f"Respuesta no-JSON en {path}") from exc
    if not isinstance(data, dict):
        raise MeliError(f"Respuesta inesperada en {path}")
    return data


async def fetch(ref: ParsedRef) -> MeliItem:
    """Trae título y fotos de una publicación o ficha de catálogo."""
    path = f"/items/{ref.id}" if ref.kind == "item" else f"/products/{ref.id}"
    raw = await _get(path)
    return MeliItem(
        id=str(raw.get("id") or ref.id),
        title=str(raw.get("title") or raw.get("name") or ""),
        image_urls=_pictures_from(raw),
        permalink=str(raw.get("permalink") or ""),
    )


async def fetch_from_url(url: str) -> MeliItem | None:
    """Atajo: URL de ML → item. None si no se pudo identificar o leer.

    No propaga errores: si la API falla, el llamador tiene que poder seguir con
    el scraping normal en vez de romper el lookup entero.
    """
    ref = parse_url(url)
    if ref is None:
        log.info("No pude sacar el id de ML de %s", url[:120])
        return None
    try:
        item = await fetch(ref)
    except MeliError as exc:
        log.warning("API de ML falló para %s (%s): %s", ref.id, ref.kind, exc)
        # Una URL /up/ puede apuntar a una ficha que no existe como catálogo;
        # probamos como publicación antes de rendirnos.
        if ref.kind == "product":
            try:
                item = await fetch(ParsedRef(id=ref.id, kind="item"))
            except MeliError:
                return None
        else:
            return None
    return item if item.image_urls else None
