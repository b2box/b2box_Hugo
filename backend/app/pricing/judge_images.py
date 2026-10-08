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
  * después de conectar y antes de leer el body se valida la IP REAL del
    servidor (el DNS puede cambiar entre el chequeo y la conexión);
  * hasta 5 MB, content-type image/jpeg|png|webp|gif|bmp, y el formato real
    (lo que detecta Pillow) también tiene que ser uno de esos. Sin compresión
    (Accept-Encoding: identity; un content-encoding distinto se rechaza): el
    tope de bytes se mide sobre lo que viaja, no sobre un gzip descomprimido;
  * timeouts de conexión y lectura por foto y un tope GLOBAL (DEADLINE_S) para
    todas las fotos de la consulta juntas: lo que no llegó a tiempo se omite;
  * se reduce a 768 px de lado y se re-encodea JPEG: menos tokens;
  * memoria acotada (el container es de 3 GB y lo comparte Camoufox): un PNG
    de 40 MP pesa unos KB y descomprimido ~160 MB. Las que Pillow no puede
    decodificar ya reducidas (PNG, WebP, GIF, BMP) tienen tope de 16 MP
    (se mira el header antes de decodificar), se reducen ANTES de aplanar la
    transparencia, y hay UN solo decode a la vez en todo el proceso.

La descarga es async (httpx) y el decode/resize corre en un thread aparte:
nada de esto bloquea el event loop del job.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from urllib.parse import urlsplit

import httpx
from PIL import Image

from app import net_guard
from app.config import get_settings
from app.dedup.image_hash import _IMAGE_HEADERS  # mlstatic corta a "python-httpx"
from app.pricing import store_urls
from app.pricing.market_ml import _clean_https_parts, safe_image_url

log = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024
MAX_SIDE = 768
JPEG_QUALITY = 85
# Una imagen chica en bytes puede ser enorme en píxeles (bomba de
# descompresión). Tope de píxeles según el formato:
#  * PNG, WebP, GIF, BMP: se decodifican enteros (sin draft), RGBA = 4 bytes
#    por píxel. 16 MP (4.000 x 4.000) son ~64 MB y sobra para una foto de
#    producto.
#  * JPEG: `draft` lo decodifica ya reducido (hasta 1/8 por lado), así que
#    aguanta un tope más alto (una cámara de 40 MP entra).
_MAX_PIXELS = 16_000_000
_MAX_PIXELS_JPEG = 40_000_000
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
# El read timeout de httpx es por chunk: un servidor que gotea bytes podría
# estirar una foto indefinidamente. Este es el tope de punta a punta de TODAS
# las fotos de una consulta juntas (descarga + decode, contando la espera por
# un lugar de la concurrencia). Un tope por foto con 4 en paralelo y 8 fotos
# lentas tardaba 2 x DEADLINE_S.
DEADLINE_S = 15.0
_MAX_REDIRECTS = 3
_CONCURRENCY = 4
# Decode/resize: UN solo thread para todo el proceso, sin importar cuántas
# consultas (o loops) haya a la vez. Es el "semáforo global" del decode: la
# descarga es concurrente y barata, el decode no (decenas de MB por foto).
# Un pool con cola y no un asyncio.Semaphore: ese queda atado al loop donde
# se usa por primera vez. Un trabajo en cola cuya tarea se cancela no corre.
_DECODE_WORKERS = 1
_DECODE_POOL = ThreadPoolExecutor(max_workers=_DECODE_WORKERS, thread_name_prefix="judge-decode")
CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"})
_PIL_FORMATS = ("JPEG", "PNG", "WEBP", "GIF", "BMP")
# httpx descomprime solo (gzip, br…): un body chico en el cable podría
# inflarse mucho antes de que el tope de bytes lo vea.
_HEADERS = {**_IMAGE_HEADERS, "Accept-Encoding": "identity"}


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
    sube a https). Tiendas: los hosts de foto de las tiendas activas. Nuestras
    fotos: exactamente el host de VENDURE_API_URL.
    """
    ml = safe_image_url(url)
    if ml:
        return ml
    # Fotos de las tiendas activas (Casa Perfecta, Gadnic…): su lista de hosts la
    # carga el semáforo y vale solo para ellas (ver store_urls).
    store = store_urls.allowed_store_image(url)
    if store:
        return store
    parts = _clean_https_parts(url)
    host = _vendure_host()
    if parts is None or not host or parts.hostname.lower() != host:
        return None
    return parts.geturl()


def make_http_client() -> httpx.AsyncClient:
    """Uno por consulta al juez. Sin redirects automáticos: se siguen a mano
    para validar cada salto. `trust_env=False`: ni HTTP(S)_PROXY / ALL_PROXY
    (un proxy de entorno haría que el chequeo de IP validara al proxy y no al
    destino), ni SSL_CERT_*, ni ~/.netrc."""
    return httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False, trust_env=False)


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
        async with http.stream("GET", current, headers=_HEADERS) as resp:
            # Ya conectado, todavía sin leer el body: ¿a quién nos conectamos de verdad?
            net_guard.assert_peer_public(resp)
            if resp.is_redirect:
                location = resp.headers.get("location") or ""
                nxt = allowed_url(str(resp.url.join(location))) if location else None
                if nxt is None:
                    raise ImageRejected("redirect a un host no permitido")
                current = nxt
                continue
            if resp.status_code != 200:
                raise ImageRejected(f"HTTP {resp.status_code}")
            encoding = (resp.headers.get("content-encoding") or "").strip().lower()
            if encoding not in ("", "identity"):
                raise ImageRejected(f"content-encoding no permitido: {encoding[:20]}")
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype not in CONTENT_TYPES:
                raise ImageRejected(f"content-type no permitido: {ctype or 'vacío'}")
            return await _read_capped(resp)
    raise ImageRejected(f"más de {_MAX_REDIRECTS} redirects")


# Modos que Image.thumbnail reduce con filtro de calidad. Paleta ("P"/"PA"),
# "1", CMYK, "I;16"… se pasan antes a RGB(A): el resize de paleta es NEAREST.
_RESIZABLE_MODES = frozenset({"RGB", "RGBA", "L", "LA"})


def _has_alpha(img: Image.Image) -> bool:
    return img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)


def _flatten_rgb(img: Image.Image) -> Image.Image:
    """RGB sobre blanco: una transparencia convertida a pelo queda negra y el
    modelo ve otro producto."""
    if _has_alpha(img):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return img.convert("RGB")


def _small_rgb(img: Image.Image) -> Image.Image:
    """RGB de lado máx MAX_SIDE. Se REDUCE primero y se aplana después:
    aplanar a tamaño completo copiaba la imagen entera dos veces más (RGBA →
    RGBA → canvas RGB), unos 500 MB para un PNG de 40 MP."""
    if img.mode not in _RESIZABLE_MODES:
        img = img.convert("RGBA" if _has_alpha(img) else "RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    return _flatten_rgb(img)


def to_jpeg_data_url(raw: bytes) -> str:
    """Bytes → data URL JPEG de lado máx MAX_SIDE. CPU y memoria: correr en el
    pool de decode (`_DECODE_POOL`), nunca en el event loop."""
    try:
        with Image.open(BytesIO(raw), formats=_PIL_FORMATS) as img:
            width, height = img.size  # del header: todavía no se decodificó nada
            limit = _MAX_PIXELS_JPEG if img.format == "JPEG" else _MAX_PIXELS
            if width <= 0 or height <= 0 or width * height > limit:
                raise ImageRejected(f"dimensiones fuera de rango ({width}x{height})")
            img.draft("RGB", (MAX_SIDE, MAX_SIDE))  # JPEG: decodifica ya reducida
            frame = _small_rgb(img)  # GIF animado: el primer cuadro
        # Sin metadata: ni el comentario COM del JPEG, ni EXIF, ni perfil ICC.
        frame.info.clear()
        out = BytesIO()
        frame.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    except ImageRejected:
        raise
    except Exception as exc:  # noqa: BLE001  (formato desconocido, truncada, bomba…)
        raise ImageRejected(f"no es una imagen válida ({type(exc).__name__})") from exc
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


async def fetch_inline(http: httpx.AsyncClient, url: str) -> str:
    """Una foto → data URL. Sin tope propio: lo pone `inline_images`."""
    raw = await download(http, url)
    return await asyncio.get_running_loop().run_in_executor(_DECODE_POOL, to_jpeg_data_url, raw)


async def inline_images(urls: Sequence[str]) -> dict[str, str]:
    """{url: data URL} de las fotos que se pudieron bajar y procesar. Las que
    fallan o no llegan antes de DEADLINE_S no aparecen (queda una línea INFO
    con el motivo). Nunca lanza."""
    unique = list(dict.fromkeys(u for u in urls if u))
    if not unique:
        return {}
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(http: httpx.AsyncClient, url: str) -> str:
        async with sem:
            return await fetch_inline(http, url)

    out: dict[str, str] = {}
    async with make_http_client() as http:
        tasks = {asyncio.ensure_future(one(http, u)): u for u in unique}
        try:
            # Deadline GLOBAL: lo que terminó, termina; el resto se cancela.
            await asyncio.wait(tasks, timeout=DEADLINE_S)
        finally:
            pending = [t for t in tasks if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
    for task, url in tasks.items():
        if task.cancelled():
            log.info("Juez LLM: foto omitida %s (no llegó en %.0f s)", url[:160], DEADLINE_S)
        elif (exc := task.exception()) is not None:  # una foto rota no tira el veredicto
            log.info("Juez LLM: foto omitida %s (%s: %s)", url[:160], type(exc).__name__, str(exc)[:160])
        else:
            out[url] = task.result()
    return out
