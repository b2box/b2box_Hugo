"""Auth simple por API key.

Endpoints sensibles (los que Luis/Paco u otros agentes externos consumen)
deben requerir el header `X-API-Key` con el valor de HUGO_API_KEY.

Si HUGO_API_KEY no está seteado en .env, el endpoint queda abierto y
loguea un warning prominente al startup. Esto facilita testing local pero
NO debe usarse en producción.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import Header, HTTPException, status

from app.config import get_settings

log = logging.getLogger(__name__)


def verify_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """FastAPI dependency: valida X-API-Key contra HUGO_API_KEY.

    - Si HUGO_API_KEY no está configurado, deja pasar (modo dev).
    - Si está configurado, exige el header y compara en tiempo constante.
    """
    expected = get_settings().hugo_api_key
    if not expected:
        log.warning("HUGO_API_KEY vacío — /verify queda abierto, no usar en producción")
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-API-Key inválida o ausente",
            headers={"WWW-Authenticate": "ApiKey"},
        )
