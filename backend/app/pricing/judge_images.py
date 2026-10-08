"""Fotos del juez en base64: las baja Hugo y viajan dentro del request.

Hay proveedores que no descargan URLs remotas: MiMo (08-oct-2026) contesta
400 "failed to download or process media content" con una URL pública y
funciona con `data:image/...;base64`. En ese modo (`PM_LLM_IMAGE_MODE`, ver
market_judge.image_mode) cada foto se baja acá, se achica y se manda inline.

Reglas, fail-closed POR FOTO (si algo no cierra esa foto se omite, el resto
sigue):
  * solo https y solo hosts permitidos: *.mlstatic.com (fichas de ML) y el
    host de VENDURE_API_URL, que es de donde salen las fotos de nuestro
    catálogo (`/assets/...`). Cada redirect se valida igual y el host tiene
    que resolver a IP pública (net_guard);
  * hasta 5 MB, content-type image/jpeg|png|webp|gif|bmp, y el formato real
    (lo que detecta Pillow) también tiene que ser uno de esos;
  * timeout por foto (conexión, lectura y un tope total);
  * se reduce a 768 px de lado y se re-encodea JPEG: menos tokens.

La descarga es async (httpx) y el decode/resize corre en un thread: nada de
esto bloquea el event loop del job.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Sequence
from io import BytesIO
from urllib.parse import urlsplit

import httpx
from PIL import Image

from app import net_guard
from app.config import get_settings
from app.dedup.image_hash import _IMAGE_HEADERS  # mlstatic corta a "python-httpx"
from app.pricing.market_ml import _clean_https_parts, safe_image_url

log = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024
MAX_SIDE = 768
JPEG_QUALITY = 85
# Una imagen chica en bytes puede ser enorme en píxeles (bomba de
# descompresión). 40 MP es ~6.300 × 6.300: de sobra para una foto de producto.
_MAX_PIXELS = 40_000_000
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
# El read timeout de httpx es por chunk: un servidor que gotea bytes podría
# estirar una foto indefinidamente. Este es el tope de punta a punta.
DEADLINE_S = 15.0
_MAX_REDIRECTS = 3
_CONCURRENCY = 4
CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"})
_PIL_FORMATS = ("JPEG", "PNG", "WEBP", "GIF", "BMP")


class ImageRejected(Exception):
    """Esta foto no viaja al juez (host, tamaño, tipo, formato, HTTP…)."""


def _vendure_host() -> str:
    try:
        return (urlsplit(get_settings().vendure_api_url.strip()).hostname or "").lower()
    except ValueError:
        return ""


def allowed_url(url: object) -> str | None:
    """URL https limpia de un host permitido, o None.

    ML: `market_ml.safe_image_url` (*.mlstatic.com; una http:// de mlstatic se
    sube a https). Nuestras fotos: exactamente el host de VENDURE_API_URL.
    """
    ml = safe_image_url(url)
    if ml:
        return ml
    parts = _clean_https_parts(url)
    host = _vendure_host()
    if parts is None or not host or parts.hostname.lower() != host:
        return None
    return parts.geturl()


def make_http_client() -> httpx.AsyncClient:
    """Uno por consulta al juez. Sin redirects automáticos: se siguen a mano
    para validar cada salto."""
    return httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False)


async def _read_capped(resp: httpx.Response) -> bytes:
    declared = resp.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BYTES:
        raise ImageRejected(f"demasiado grande ({declared} bytes declarados)")
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf += chunk
        if len(buf) > MAX_BYTES:
            raise ImageRejected(f"demasiado grande (> {MAX_BYTES} bytes)")
    return bytes(buf)


async def download(http: httpx.AsyncClient, url: str) -> bytes:
    """Bytes de la foto o ImageRejected / SsrfBlocked / error de httpx."""
    current = allowed_url(url)
    if current is None:
        raise ImageRejected("host o esquema no permitido")
    for _ in range(_MAX_REDIRECTS + 1):
        # getaddrinfo es bloqueante: al thread.
        await asyncio.to_thread(net_guard.assert_public_url, current)
        async with http.stream("GET", current, headers=_IMAGE_HEADERS) as resp:
            if resp.is_redirect:
                location = resp.headers.get("location") or ""
                nxt = allowed_url(str(resp.url.join(location))) if location else None
                if nxt is None:
                    raise ImageRejected("redirect a un host no permitido")
                current = nxt
                continue
            if resp.status_code != 200:
                raise ImageRejected(f"HTTP {resp.status_code}")
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype not in CONTENT_TYPES:
                raise ImageRejected(f"content-type no permitido: {ctype or 'vacío'}")
            return await _read_capped(resp)
    raise ImageRejected(f"más de {_MAX_REDIRECTS} redirects")


def _flatten_rgb(img: Image.Image) -> Image.Image:
    """RGB sobre blanco: una transparencia convertida a pelo queda negra y el
    modelo ve otro producto."""
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return img.convert("RGB")


def to_jpeg_data_url(raw: bytes) -> str:
    """Bytes → data URL JPEG de lado máx MAX_SIDE. CPU: correr en un thread."""
    try:
        with Image.open(BytesIO(raw), formats=_PIL_FORMATS) as img:
            width, height = img.size
            if width <= 0 or height <= 0 or width * height > _MAX_PIXELS:
                raise ImageRejected(f"dimensiones fuera de rango ({width}x{height})")
            img.draft("RGB", (MAX_SIDE, MAX_SIDE))  # JPEG: decodifica ya reducida
            frame = _flatten_rgb(img)  # GIF animado: el primer cuadro
        frame.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
        out = BytesIO()
        frame.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    except ImageRejected:
        raise
    except Exception as exc:  # noqa: BLE001  (formato desconocido, truncada, bomba…)
        raise ImageRejected(f"no es una imagen válida ({type(exc).__name__})") from exc
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


async def fetch_inline(http: httpx.AsyncClient, url: str) -> str:
    raw = await asyncio.wait_for(download(http, url), DEADLINE_S)
    return await asyncio.to_thread(to_jpeg_data_url, raw)


async def inline_images(urls: Sequence[str]) -> dict[str, str]:
    """{url: data URL} de las fotos que se pudieron bajar y procesar. Las que
    fallan no aparecen (queda una línea INFO con el motivo). Nunca lanza."""
    unique = list(dict.fromkeys(u for u in urls if u))
    if not unique:
        return {}
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(http: httpx.AsyncClient, url: str) -> tuple[str, str | None]:
        async with sem:
            try:
                return url, await fetch_inline(http, url)
            except Exception as exc:  # noqa: BLE001  (una foto rota no tira el veredicto)
                log.info("Juez LLM: foto omitida %s (%s: %s)",
                         url[:160], type(exc).__name__, str(exc)[:160])
                return url, None

    async with make_http_client() as http:
        pairs = await asyncio.gather(*(one(http, u) for u in unique))
    return {u: data for u, data in pairs if data}
