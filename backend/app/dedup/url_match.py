"""Estrategia 1 — match por source URL.

Si dos productos vienen de la misma URL de origen (Alibaba/AliExpress/etc.),
son el mismo producto. Match exacto, score 1.0.

Normaliza la URL para tolerar variaciones triviales:
  - lowercase
  - sin query params de tracking (utm_*, spm, scm, etc.)
  - sin trailing slash
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Parámetros de tracking que ignoramos al comparar URLs
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "scm", "pvid", "algo_pvid", "algo_exp_id", "ref", "aff_trace_key",
    "terminal_id", "_t", "_p", "tt_from", "share_app_id",
}


def normalize_url(url: str | None) -> str | None:
    """Devuelve una versión canónica de la URL para comparar."""
    if not url:
        return None
    parsed = urlparse(url.strip().lower())
    # filtrar tracking params
    clean_qs = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k not in _TRACKING_PARAMS
    ]
    clean_qs.sort()
    path = parsed.path.rstrip("/")
    return urlunparse(parsed._replace(query=urlencode(clean_qs), path=path, fragment=""))


def url_similarity(a: str | None, b: str | None) -> float:
    """1.0 si las URLs normalizadas coinciden, 0.0 si no."""
    na, nb = normalize_url(a), normalize_url(b)
    if not na or not nb:
        return 0.0
    return 1.0 if na == nb else 0.0
