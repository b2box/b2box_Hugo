"""Estrategia 4 — embeddings CLIP (similitud semántica de imagen).

pHash (ver image_hash.py) solo reconoce imágenes casi idénticas: sirve cuando
el producto viene de Alibaba/MercadoLibre y reusa la foto oficial del proveedor.
No sirve cuando el cliente saca su propia foto: otro ángulo, otro fondo, otra
luz → distancia de Hamming enorme aunque sea el mismo producto.

Acá usamos la torre visual de CLIP ViT-B/32 exportada a ONNX. Devuelve un vector
de 512 dims; el coseno entre dos vectores mide "mismo producto" y no "mismos
píxeles". Referencias de score (vectores normalizados):

    misma imagen                     ≈ 1.00
    mismo producto, otra foto        ≈ 0.85 – 0.95
    productos distintos, misma cat.  ≈ 0.70 – 0.80
    productos sin relación           ≈ 0.40 – 0.65

Corremos ONNX Runtime en CPU (sin torch): el preprocesado es PIL + numpy, así
que la dependencia pesada es solo el .onnx (~350 MB), bakeado en la imagen.

Todo el módulo degrada elegante: si el modelo no está o falla, `available()`
devuelve False y el llamador cae a pHash.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import threading
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from io import BytesIO

import numpy as np
from PIL import Image
from sqlmodel import Session

from app.config import get_settings
from app.dedup.image_hash import _fetch  # mismo fetch con guard SSRF + tope de bytes

log = logging.getLogger(__name__)

MODEL_NAME = "clip-vit-b32"
EMBED_DIM = 512
_IMAGE_SIZE = 224
# Constantes de normalización de CLIP (las mismas del preprocesador de OpenAI).
_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32).reshape(3, 1, 1)
_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32).reshape(3, 1, 1)
_MAX_URL_LEN = 1024

# L1: cache en memoria (proceso vivo). L2: tabla image_embed_cache.
_CACHE: "OrderedDict[str, np.ndarray]" = OrderedDict()

_session = None            # onnxruntime.InferenceSession
_session_failed = False    # ya intentamos cargar y no se pudo → no reintentar en loop
_session_lock = threading.Lock()
_input_name = ""
_output_name = ""


# ─── Modelo ────────────────────────────────────────────────────────


def _load_session():
    """Carga (una vez) la InferenceSession de ONNX Runtime. None si no se puede."""
    global _session, _session_failed, _input_name, _output_name
    if _session is not None or _session_failed:
        return _session
    with _session_lock:
        if _session is not None or _session_failed:
            return _session
        s = get_settings()
        path = s.embed_model_path
        if not path or not os.path.exists(path):
            log.warning(
                "CLIP deshabilitado: no existe el modelo en %s. "
                "El match de /app/lookup cae a pHash (solo fotos idénticas).",
                path,
            )
            _session_failed = True
            return None
        try:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            # El container tiene 1 CPU: más threads solo agregan contención.
            opts.intra_op_num_threads = max(1, int(s.embed_onnx_threads))
            opts.inter_op_num_threads = 1
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sess = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
            _input_name = sess.get_inputs()[0].name
            _output_name = _pick_output(sess)
            _session = sess
            log.info(
                "CLIP ONNX cargado (%s) — input=%s output=%s", path, _input_name, _output_name
            )
            return _session
        except Exception:  # noqa: BLE001
            log.exception("No se pudo cargar el modelo CLIP; sigo con pHash")
            _session_failed = True
            return None


def _pick_output(sess) -> str:
    """Elige la salida del embedding (2D con última dim = 512).

    Las exportaciones de CLIP varían: algunas emiten `image_embeds`, otras
    `pooler_output` + `last_hidden_state`. Elegimos por forma en vez de por
    nombre para no atarnos a un export concreto.
    """
    for out in sess.get_outputs():
        shape = list(out.shape or [])
        if len(shape) == 2 and isinstance(shape[-1], int) and shape[-1] == EMBED_DIM:
            return out.name
    return sess.get_outputs()[0].name


def available() -> bool:
    """True si el matching por embeddings puede usarse."""
    if not get_settings().embed_enabled:
        return False
    return _load_session() is not None


# ─── Preprocesado ──────────────────────────────────────────────────


def _preprocess(raw: bytes) -> np.ndarray:
    """bytes → tensor float32 [1, 3, 224, 224] normalizado como CLIP."""
    img = Image.open(BytesIO(raw)).convert("RGB")
    # Resize del lado corto a 224 y center crop (idéntico al preproceso de CLIP).
    w, h = img.size
    scale = _IMAGE_SIZE / min(w, h)
    new_size = (max(_IMAGE_SIZE, round(w * scale)), max(_IMAGE_SIZE, round(h * scale)))
    img = img.resize(new_size, Image.BICUBIC)
    left = (img.width - _IMAGE_SIZE) // 2
    top = (img.height - _IMAGE_SIZE) // 2
    img = img.crop((left, top, left + _IMAGE_SIZE, top + _IMAGE_SIZE))

    arr = np.asarray(img, dtype=np.float32) / 255.0   # HWC
    arr = arr.transpose(2, 0, 1)                       # CHW
    arr = (arr - _MEAN) / _STD
    return arr[None, ...]


def _normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    if norm <= 0.0:
        return vec.astype(np.float32)
    return (vec / norm).astype(np.float32)


def _infer(raw: bytes) -> np.ndarray | None:
    """Sync: preprocesa + corre ONNX + normaliza. Para usar con to_thread."""
    sess = _load_session()
    if sess is None:
        return None
    try:
        tensor = _preprocess(raw)
        out = sess.run([_output_name], {_input_name: tensor})[0]
        return _normalize(np.asarray(out, dtype=np.float32).reshape(-1))
    except Exception:  # noqa: BLE001
        log.debug("Inferencia CLIP falló", exc_info=True)
        return None


# ─── Serialización + cache L2 ──────────────────────────────────────


def encode_vector(vec: np.ndarray) -> str:
    return base64.b64encode(np.asarray(vec, dtype="<f4").tobytes()).decode("ascii")


def decode_vector(b64: str) -> np.ndarray | None:
    try:
        arr = np.frombuffer(base64.b64decode(b64), dtype="<f4")
    except Exception:  # noqa: BLE001
        return None
    if arr.size != EMBED_DIM:
        return None
    return arr.astype(np.float32)


def _db_get(url: str) -> np.ndarray | None:
    if len(url) > _MAX_URL_LEN:
        return None
    try:
        from app.db.models import ImageEmbedCache
        from app.db.session import engine

        with Session(engine) as session:
            row = session.get(ImageEmbedCache, url)
            if row is None or row.model != MODEL_NAME:
                return None
            return decode_vector(row.vector_b64)
    except Exception:  # noqa: BLE001
        return None


def _db_put(url: str, vec: np.ndarray) -> None:
    if len(url) > _MAX_URL_LEN:
        return
    try:
        from app.clock import utcnow
        from app.db.models import ImageEmbedCache
        from app.db.session import engine

        payload = encode_vector(vec)
        with Session(engine) as session:
            row = session.get(ImageEmbedCache, url)
            if row is None:
                session.add(ImageEmbedCache(
                    url=url, model=MODEL_NAME, dim=EMBED_DIM, vector_b64=payload,
                ))
            else:
                row.model = MODEL_NAME
                row.dim = EMBED_DIM
                row.vector_b64 = payload
                row.updated_at = utcnow()
                session.add(row)
            session.commit()
    except Exception:  # noqa: BLE001
        log.debug("No se pudo persistir el embedding de %s", url, exc_info=True)


def _cache_put(url: str, vec: np.ndarray) -> None:
    maxlen = get_settings().embed_cache_max
    _CACHE[url] = vec
    _CACHE.move_to_end(url)
    while len(_CACHE) > maxlen:
        _CACHE.popitem(last=False)


# ─── API pública ───────────────────────────────────────────────────


async def embed_url(url: str) -> np.ndarray | None:
    """Embedding normalizado de la imagen en `url`, o None si no se pudo.

    Orden: L1 (memoria) → L2 (DB) → descarga + inferencia.
    """
    if not url or not available():
        return None
    cached = _CACHE.get(url)
    if cached is not None:
        _CACHE.move_to_end(url)
        return cached

    from_db = await asyncio.to_thread(_db_get, url)
    if from_db is not None:
        _cache_put(url, from_db)
        return from_db

    try:
        raw = await _fetch(url)
    except Exception:  # noqa: BLE001  (SsrfBlocked, HTTP, tamaño, …)
        return None

    vec = await asyncio.to_thread(_infer, raw)
    if vec is None:
        return None
    _cache_put(url, vec)
    await asyncio.to_thread(_db_put, url, vec)
    return vec


async def embed_urls_aligned(
    urls: Sequence[str], *, concurrency: int = 4
) -> list[np.ndarray | None]:
    """Embebe varias URLs conservando el orden y la posición de las que fallan.

    Usar esta (y no `embed_urls`) cuando después hay que aparear cada vector con
    su URL: `embed_urls` descarta los fallos y desalinea el zip.

    `concurrency` acota las descargas simultáneas: la inferencia es CPU-bound y
    en 1 core no gana nada por paralelizar más, pero sí llenaría memoria con
    imágenes descargadas esperando su turno.
    """
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(u: str) -> np.ndarray | None:
        async with sem:
            return await embed_url(u)

    return list(await asyncio.gather(*(_one(u) for u in urls)))


async def embed_urls(urls: Iterable[str], *, concurrency: int = 4) -> list[np.ndarray]:
    """Embebe varias URLs, descartando las que fallan."""
    results = await embed_urls_aligned(list(urls), concurrency=concurrency)
    return [v for v in results if v is not None]


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Coseno entre dos vectores YA normalizados → producto punto."""
    return float(np.dot(a, b))


def best_match(query: np.ndarray, matrix: np.ndarray) -> tuple[int, float]:
    """Índice y score de la fila de `matrix` (N×512, normalizada) más parecida.

    Devuelve (-1, 0.0) si la matriz está vacía.
    """
    if matrix.size == 0 or matrix.ndim != 2:
        return -1, 0.0
    scores = matrix @ query
    idx = int(np.argmax(scores))
    return idx, float(scores[idx])


async def similarity(urls_a: Sequence[str], urls_b: Sequence[str]) -> float:
    """Mejor coseno entre cualquier par (a, b). 0.0 si alguno no se pudo embeber."""
    if not urls_a or not urls_b:
        return 0.0
    vecs_a, vecs_b = await asyncio.gather(embed_urls(urls_a), embed_urls(urls_b))
    if not vecs_a or not vecs_b:
        return 0.0
    mat_b = np.vstack(vecs_b)
    best = 0.0
    for va in vecs_a:
        _, score = best_match(va, mat_b)
        best = max(best, score)
    return best
