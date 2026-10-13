"""Cómo se guarda y se muestra un error en la bandeja: el TIPO de la excepción y, si lo hay, un código corto (el HTTP del
transporte o el `extensions.code` de GraphQL). Nunca el mensaje: el de una respuesta de Vendure o el de un desglose que no
cierra puede traer montos, ids, URLs o pedazos de la respuesta. El detalle va al log del servidor, no a la base ni a la API.
"""

from __future__ import annotations

import re
from typing import Any

_CODE = re.compile(r"[A-Za-z0-9_.\-]{1,40}")


def _clean(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, str)) and _CODE.fullmatch(str(value)):
        return str(value)
    return None


def error_code(exc: BaseException) -> str:
    """«TransportServerError 502», «TransportQueryError GRAPHQL_VALIDATION_FAILED», «BreakdownError»…"""
    name = type(exc).__name__
    code = _clean(getattr(exc, "code", None))
    if code is None:
        errors = getattr(exc, "errors", None)                     # TransportQueryError.errors: lista de errores GraphQL
        first = errors[0] if isinstance(errors, list) and errors else None
        ext = first.get("extensions") if isinstance(first, dict) else None
        code = _clean(ext.get("code")) if isinstance(ext, dict) else None
    return f"{name} {code}" if code else name
