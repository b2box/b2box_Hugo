"""Comparación de precios fuente entre snapshots.

Hugo NO actualiza el precio de venta de Vendure (el cálculo final con TC + margen
+ IVA lo hace otro sistema interno). Su trabajo es trackear el COSTO del proveedor
en su moneda original (CNY típicamente) y avisar cuando cambia.

Comparación válida: precio_fuente_actual vs último_precio_fuente_visto, ambos en
la misma moneda. Cualquier comparación cross-currency devuelve "skip".

Acciones posibles:
  - "first_observation": no hay baseline previo, solo guardar.
  - "ok"               : drift dentro del umbral, no hay nada que reportar.
  - "alert"            : drift >= threshold, mandar notificación.
  - "alert_critical"   : drift > max_auto, cambio brusco — atención humana.
  - "skip_currency"    : monedas distintas entre observaciones (fuente cambió de moneda).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app import runtime

Action = Literal["first_observation", "ok", "alert", "alert_critical", "skip_currency"]


@dataclass(slots=True)
class PriceDecision:
    drift_pct: float            # firmado: positivo = subió, negativo = bajó
    action: Action
    previous_price_cents: int | None
    current_price_cents: int
    currency: str
    reason: str


def compare_source_snapshots(
    current_price_cents: int,
    current_currency: str,
    previous_price_cents: int | None,
    previous_currency: str | None,
) -> PriceDecision:
    """Compara dos observaciones del precio fuente del MISMO producto."""
    drift_threshold = runtime.get("price_drift_threshold")
    drift_max_auto = runtime.get("price_drift_max_auto")

    if previous_price_cents is None:
        return PriceDecision(
            drift_pct=0.0,
            action="first_observation",
            previous_price_cents=None,
            current_price_cents=current_price_cents,
            currency=current_currency,
            reason="Primera observación de este producto, sin baseline para comparar",
        )

    if previous_currency and previous_currency != current_currency:
        return PriceDecision(
            drift_pct=0.0,
            action="skip_currency",
            previous_price_cents=previous_price_cents,
            current_price_cents=current_price_cents,
            currency=current_currency,
            reason=(
                f"Moneda cambió ({previous_currency} → {current_currency}); "
                f"no se puede comparar directamente"
            ),
        )

    if previous_price_cents <= 0:
        return PriceDecision(
            drift_pct=0.0,
            action="alert_critical",
            previous_price_cents=previous_price_cents,
            current_price_cents=current_price_cents,
            currency=current_currency,
            reason="Snapshot anterior con precio inválido",
        )

    drift = (current_price_cents - previous_price_cents) / previous_price_cents
    abs_drift = abs(drift)

    if abs_drift < drift_threshold:
        return PriceDecision(
            drift_pct=drift,
            action="ok",
            previous_price_cents=previous_price_cents,
            current_price_cents=current_price_cents,
            currency=current_currency,
            reason=f"Variación {drift:+.2%} < umbral {drift_threshold:.2%}",
        )

    if abs_drift > drift_max_auto:
        return PriceDecision(
            drift_pct=drift,
            action="alert_critical",
            previous_price_cents=previous_price_cents,
            current_price_cents=current_price_cents,
            currency=current_currency,
            reason=(
                f"Variación BRUSCA {drift:+.2%} (>{drift_max_auto:.2%}) "
                f"— revisar si la fuente sigue siendo el mismo producto"
            ),
        )

    return PriceDecision(
        drift_pct=drift,
        action="alert",
        previous_price_cents=previous_price_cents,
        current_price_cents=current_price_cents,
        currency=current_currency,
        reason=f"Variación {drift:+.2%} dentro de rango normal pero supera umbral",
    )
