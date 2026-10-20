"""Fotos del bucket propio (las que sube el visitante del comparador).

Llegan como URLs firmadas: la query (X-Amz-Signature, X-Amz-Credential…) es una
credencial temporal y no puede quedar en logs, en el audit log ni en la base.

`ESTIMATE_UPLOAD_IMAGE_PREFIXES`: entradas `host/bucket/prefijo/` separadas por
coma. El S3 de prod es compatible no-AWS con path-style, así que las URLs son
`https://<endpoint>/<bucket>/lookup-photos/<fecha>/<uuid>.<ext>` y el prefijo
incluye el bucket (ej. `s3.b2box.app/b2box-media/lookup-photos/`). Vacío = no
se acepta ninguna foto subida.

Módulo neutro a propósito: lo usan dedup/image_hash (logs), api/app_routes y
estimate/ sin importarse entre ellos.
"""

from __future__ import annotations

import re
import time
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from app.config import get_settings

# Parámetros que delatan una URL firmada (S3 v4/v2, GCS, tokens genéricos).
_SIGNING_PARAMS = frozenset({
    "x-amz-signature", "x-amz-credential", "x-amz-security-token", "signature",
    "x-goog-signature", "x-goog-credential", "awsaccesskeyid", "token", "sig",
})
_EXPIRY_PARAMS = ("x-amz-expires", "x-goog-expires")


def _prefixes() -> list[tuple[str, str]]:
    """[(host, '/bucket/prefijo/')] de ESTIMATE_UPLOAD_IMAGE_PREFIXES."""
    out: list[tuple[str, str]] = []
    for entry in (get_settings().estimate_upload_image_prefixes or "").split(","):
        entry = entry.strip().removeprefix("https://")
        host, sep, path = entry.partition("/")
        if host and sep and path.strip("/"):
            # Con barra final siempre: "lookup-photos" no puede matchear
            # "lookup-photos-viejas/…".
            out.append((host.lower().rstrip("."), "/" + path.strip("/") + "/"))
    return out


def upload_hosts() -> set[str]:
    return {host for host, _ in _prefixes()}


def _query(url: str) -> dict[str, str]:
    try:
        return {k.lower(): v for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True)}
    except ValueError:
        return {}


def _expiry_ok(params: dict[str, str]) -> bool:
    """False si la firma dice que vive más que ESTIMATE_UPLOAD_MAX_EXPIRY_S.
    Sin un parámetro legible no se puede saber: se acepta."""
    cap = int(get_settings().estimate_upload_max_expiry_s)
    for name in _EXPIRY_PARAMS:
        if name in params:
            try:
                return 0 < int(params[name]) <= cap
            except ValueError:
                return False
    if "expires" in params:          # S3 v2: epoch de vencimiento
        try:
            return int(params["expires"]) - time.time() <= cap
        except ValueError:
            return False
    return True


def allowed_upload(url: str) -> bool:
    """¿Es una foto del bucket propio, bajo un prefijo permitido, con firma corta?

    `url` ya pasó por la validación de https/host público. El path se mira
    crudo (sin decodificar): `..`, `%2e`, `%2f`, `\\` o `//` no se aceptan."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    path = parts.path
    lowered = path.lower()
    if ".." in path or "%2e" in lowered or "%2f" in lowered or "%5c" in lowered \
            or "\\" in path or "//" in path:
        return False
    if not any(host == h and path.startswith(p) for h, p in _prefixes()):
        return False
    return _expiry_ok(_query(url))


def redact(url: str | None) -> str | None:
    """La URL sin query ni fragmento si es del bucket propio o tiene firma; tal
    cual si no. Para logs, audit log y base."""
    if not url or not isinstance(url, str):
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return "(url inválida)"
    host = (parts.hostname or "").lower().rstrip(".")
    signed = any(k in _SIGNING_PARAMS for k in _query(url))
    if host in upload_hosts() or signed:
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return url


_URL_RE = re.compile(r"https?://[^\s'\"<>]+")


def redact_text(text: str | None) -> str | None:
    """`redact` sobre cada URL que aparezca en un texto (detalles del audit log)."""
    if not text:
        return text
    return _URL_RE.sub(lambda m: redact(m.group(0)) or "", text)


def scrub_exception(exc: BaseException) -> BaseException:
    """Saca la firma de las URLs que traiga el mensaje de una excepción (httpx
    pone la URL completa en el de raise_for_status). Mismo tipo y mismo objeto:
    quien la ataja por tipo sigue funcionando."""
    try:
        exc.args = tuple(redact_text(a) if isinstance(a, str) else a for a in exc.args)
    except Exception:  # noqa: BLE001
        pass
    return exc
