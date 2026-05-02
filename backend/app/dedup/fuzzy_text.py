"""Estrategia 3 — fuzzy matching de título + descripción.

Última red de seguridad: si las dos estrategias anteriores no pescaron el
duplicado pero los textos son muy parecidos, lo flaggeamos.

Pondera 70% título + 30% descripción, ya que el título es mucho más diagnóstico.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

_WHITESPACE_RE = re.compile(r"\s+")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _normalize(text: str) -> str:
    """lowercase + strip HTML + colapsa whitespace."""
    if not text:
        return ""
    txt = _HTML_TAG_RE.sub(" ", text)
    txt = _WHITESPACE_RE.sub(" ", txt).strip().lower()
    return txt


def text_similarity(
    name_a: str,
    desc_a: str,
    name_b: str,
    desc_b: str,
) -> float:
    """Score [0..1] combinando similitud de título (70%) y descripción (30%)."""
    na, nb = _normalize(name_a), _normalize(name_b)
    da, db = _normalize(desc_a), _normalize(desc_b)

    # token_set_ratio tolera reordenamientos y palabras extra
    name_score = fuzz.token_set_ratio(na, nb) / 100.0 if na and nb else 0.0
    if not da or not db:
        return name_score  # sin descripción, solo el título cuenta
    desc_score = fuzz.token_set_ratio(da, db) / 100.0
    return 0.7 * name_score + 0.3 * desc_score
