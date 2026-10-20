"""IP del visitante → hash con sal. Nunca se guarda ni se loguea la IP en claro.

Adaptado de `cotizador/identity.py` (rama feat/cotizador-quote): misma
normalización de IP (IPv4 mapeada a IPv4, IPv6 reducida a su /64) y la misma
regla de sal (obligatoria en producción, derivada de secretos en desarrollo).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from functools import lru_cache

from app.config import get_settings

MIN_SALT_LEN = 32


class SaltMissing(RuntimeError):
    """Producción sin ESTIMATE_HASH_SALT: el estimado no corre (fail-closed)."""


def clean_ip(raw: object) -> str | None:
    """IP de internet creíble, o None. Privadas, loopback, link-local,
    reservadas y multicast no son un visitante real."""
    if not isinstance(raw, str) or len(raw) > 64:
        return None
    text = raw.strip().strip("[]")
    if "%" in text:                       # zona de IPv6 (fe80::1%eth0)
        text = text.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if not addr.is_global or addr.is_multicast:
        return None
    return str(addr)


def normalize_ip(raw: object) -> str | None:
    """`clean_ip` para contar: una IPv6 se reduce a su /64 (un cliente recibe un
    /64 entero; rotar dentro de él no lo vuelve otra persona)."""
    addr = clean_ip(raw)
    if addr is None or ":" not in addr:
        return addr
    return f"{ipaddress.ip_network(f'{addr}/64', strict=False).network_address}/64"


@lru_cache(maxsize=8)
def _derived_salt(keys: str, legacy: str, secret: str) -> bytes:
    material = "|".join((keys, legacy, secret))
    return hashlib.sha256(b"hugo-estimate-v1|" + material.encode("utf-8")).digest()


def _salt() -> bytes:
    s = get_settings()
    explicit = (s.estimate_hash_salt or "").strip()
    if (getattr(s, "hugo_env", "") or "").strip().lower() == "production":
        if len(explicit) < MIN_SALT_LEN:
            raise SaltMissing("ESTIMATE_HASH_SALT (32+ caracteres) es obligatoria en producción")
        return explicit.encode("utf-8")
    if explicit:
        return explicit.encode("utf-8")
    # Desarrollo: derivada de secretos que ya existen (estable entre reinicios).
    return _derived_salt(s.hugo_api_keys or "", s.hugo_api_key or "",
                         s.dashboard_secret or s.dashboard_password or "")


def hash_value(kind: str, value: str) -> str:
    """HMAC-SHA256(sal, kind:valor), 32 hex. Lanza SaltMissing en producción sin sal."""
    return hmac.new(_salt(), f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()[:32]


def ip_hash(raw_ip: object, *, kind: str = "ip") -> str | None:
    """Hash de la IP normalizada; None si no es una IP de internet.

    `kind="sock"` para la IP del socket (puede ser privada en una red interna:
    igual se cuenta, tal cual)."""
    ip = normalize_ip(raw_ip)
    if ip is None and kind == "sock" and isinstance(raw_ip, str):
        raw = raw_ip.strip().lower()[:64]
        ip = raw if raw and raw != "unknown" else None
    return hash_value(kind, ip) if ip else None
