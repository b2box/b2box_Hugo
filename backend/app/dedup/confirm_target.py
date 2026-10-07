"""Qué producto se apaga (si alguno) al confirmar un `duplicate_flagged`.

Un flag de duplicado llega por dos caminos y el id que trae en `product_id`
significa cosas distintas en cada uno:

  · /verify (source luis / orders / b2box-pro / …): el candidato todavía NO está
    en Vendure — lo está evaluando Luis o lo cargó el app. `product_id` es el
    producto que YA existe y contra el que matcheó. Confirmar acá significa
    "sí, era duplicado, no lo subimos": no hay nada que apagar en Vendure.

  · auditoría de catálogo (source "audit"): los dos productos están en Vendure.
    `product_id` es el más nuevo (drop) y `related_product_id` el canónico
    (keep). Confirmar apaga el más nuevo, nunca el canónico.

Las filas nuevas traen esto explícito en `disable_target_id` /
`canonical_product_id` (ver AuditLog). Para las filas anteriores a esos campos
se infiere por `source`, que es el único dato que distinguía los dos orígenes.
"""

from __future__ import annotations

from app.db.models import AuditLog

PLACEHOLDER_NEW = "(nuevo)"


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _is_catalog_audit(entry: AuditLog) -> bool:
    return _clean(entry.source).lower() == "audit" and bool(_clean(entry.related_product_id))


def canonical_for(entry: AuditLog) -> str | None:
    """Id del producto original (el que se conserva), o None si no se conoce."""
    explicit = _clean(entry.canonical_product_id)
    if explicit:
        return explicit
    if _is_catalog_audit(entry):
        return _clean(entry.related_product_id)
    # Fila vieja de /verify: el "original" era el product_id.
    pid = _clean(entry.product_id)
    return pid if pid and pid != PLACEHOLDER_NEW else None


def disable_target_for(entry: AuditLog) -> str | None:
    """Id de Vendure que "Confirmar duplicado" debe deshabilitar, o None.

    None significa "confirmar no toca Vendure" (el candidato nunca entró al
    catálogo). Nunca devuelve el canónico, aunque la fila venga inconsistente.
    """
    target = _clean(entry.disable_target_id)
    canonical = _clean(entry.canonical_product_id)
    if target:
        if canonical and target == canonical:
            return None
        return target
    if canonical:
        # Fila nueva sin target: evento de /verify, explícitamente sin acción.
        return None
    if _is_catalog_audit(entry):
        pid = _clean(entry.product_id)
        if pid and pid != PLACEHOLDER_NEW and pid != _clean(entry.related_product_id):
            return pid
    return None
