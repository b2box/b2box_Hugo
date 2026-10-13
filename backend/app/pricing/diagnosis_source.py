"""Arma el diagnóstico a-e de UN producto: junta lo que hace falta (SOLO lecturas de Vendure, las medidas del
snapshot y las cajas de los productos parecidos) y llama a las reglas puras de `price_diagnosis`.

Reglas de oro:
  * Solo QUERIES: el origen de datos (`ReadSource`) no tiene ningún método de escritura.
  * Tope duro de consultas por pasada (`ReadBudget`, 150 por noche): cuando se acaba, lo que falta queda
    como «sin dato», no se pide igual.
  * Si algo no se puede leer (permiso, red, un desglose que no cierra), esa regla dice «sin dato» y el
    motivo queda en `errors`; nunca se inventa y nunca tira la pasada.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from app.pricing.price_breakdown import BreakdownError, TierBreakdown
from app.pricing.price_diagnosis import (
    Box,
    CertFacts,
    Diagnosis,
    DiagFacts,
    DiagThresholds,
    Economics,
    box_from_specs,
    diagnose,
    economics_from_snapshot,
    select_tier,
    tiers_from_variant,
)
from app.pricing.semaforo import PricedVariant
from app.vendure.client import CertificationInfo, ChannelPricing

log = logging.getLogger(__name__)

# Tope duro de consultas nuevas a Vendure por noche (criterio 1-6 del diseño).
MAX_VENDURE_CALLS = 150
# Cuántos productos parecidos se miran para la referencia de caja.
NEIGHBORS_TOP_K = 10


class ReadBudgetExhausted(RuntimeError):
    """Se acabó el tope de consultas a Vendure de esta pasada."""


class ReadSource(Protocol):
    """Lo ÚNICO que el diagnóstico puede pedirle a Vendure. Cuatro lecturas, ninguna escritura."""

    async def get_tier_breakdowns(self, variant_id: str) -> list[TierBreakdown]: ...
    async def get_priced_variant(self, variant_id: str) -> PricedVariant | None: ...
    async def get_certification(self, product_id: str, channel_id: str) -> CertificationInfo | None: ...
    async def get_channel_pricing(self) -> ChannelPricing | None: ...


class BudgetedSource:
    """Envuelve un `ReadSource` y cuenta cada consulta; pasado el tope levanta `ReadBudgetExhausted`.
    Solo reexpone las cuatro lecturas: aunque el cliente real tenga `disable_product`, desde acá no se llega."""

    def __init__(self, client: ReadSource, max_calls: int = MAX_VENDURE_CALLS) -> None:
        self._client = client
        self.max_calls = max_calls
        self.calls = 0

    def _spend(self, n: int = 1) -> None:
        if self.calls + n > self.max_calls:
            raise ReadBudgetExhausted(f"tope de {self.max_calls} consultas a Vendure por pasada")
        self.calls += n

    @property
    def exhausted(self) -> bool:
        return self.calls >= self.max_calls

    async def get_tier_breakdowns(self, variant_id: str) -> list[TierBreakdown]:
        self._spend()
        return await self._client.get_tier_breakdowns(variant_id)

    async def get_priced_variant(self, variant_id: str) -> PricedVariant | None:
        self._spend()
        return await self._client.get_priced_variant(variant_id)

    async def get_certification(self, product_id: str, channel_id: str) -> CertificationInfo | None:
        self._spend()
        return await self._client.get_certification(product_id, channel_id)

    async def get_channel_pricing(self) -> ChannelPricing | None:
        self._spend(2)    # activeChannel + pricingFiscalProfile
        return await self._client.get_channel_pricing()


@dataclass(slots=True)
class ProductDiagnosis:
    diagnosis: Diagnosis
    breakdown: dict[str, Any] | None            # el tramo usado, listo para guardar y mostrar
    tiers_breakdown: list[dict[str, Any]] = field(default_factory=list)   # todos los tramos (resumido)
    errors: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.diagnosis.complete and not self.errors


# ─── Cajas de los productos parecidos ─────────────────────────────


def neighbor_boxes(
    product_id: str,
    specs_by_product: Mapping[str, Mapping[str, float]],
    neighbors_fn: Callable[[str, int], list[tuple[str, float]]],
    top_k: int = NEIGHBORS_TOP_K,
) -> list[Box] | None:
    """Cajas (de la variante comparada) de los `top_k` productos más parecidos por foto. None si el
    índice CLIP no está disponible para este producto (→ la regla (a1/a2) queda sin referencia)."""
    near = neighbors_fn(product_id, top_k)
    if not near:
        return None
    return [box_from_specs(dict(specs_by_product[pid])) for pid, _score in near if pid in specs_by_product]


def parse_specs(raw: str | None) -> dict[str, float]:
    """`our_specs` del snapshot (JSON) → dict de medidas; vacío si no hay o está roto."""
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return {k: float(v) for k, v in data.items() if isinstance(v, (int, float)) and not isinstance(v, bool)} \
        if isinstance(data, dict) else {}


# ─── El diagnóstico de un producto ────────────────────────────────


async def diagnose_product(
    snap: Any,
    source: ReadSource,
    *,
    yellow_min_pct: float,
    channel: ChannelPricing | None,
    variant: PricedVariant | None = None,
    neighbor_boxes_: list[Box] | None = None,
    thresholds: DiagThresholds | None = None,
) -> ProductDiagnosis | None:
    """Diagnóstico de un snapshot rojo. None si no tiene precio nuestro o mediana de ML (no hay nada que explicar).

    `variant`: la variante tal como la bajó el listado nocturno (sin consultas nuevas); si no se pasa se lee
    una vez. Cada lectura fallida suma un mensaje a `errors` y deja esa parte en «sin dato»."""
    econ: Economics | None = economics_from_snapshot(snap, yellow_min_pct)
    if econ is None:
        return None
    errors: list[str] = []

    def note(what: str, exc: Exception) -> None:
        errors.append(f"{what}: {type(exc).__name__}: {str(exc)[:160]}")
        log.warning("diagnóstico de %s: %s", snap.product_id, errors[-1])

    if variant is None and snap.variant_id:
        try:
            variant = await source.get_priced_variant(str(snap.variant_id))
        except Exception as exc:  # noqa: BLE001
            note("tramos de la variante", exc)

    breakdowns: list[TierBreakdown] | None = None
    if snap.variant_id:
        try:
            breakdowns = await source.get_tier_breakdowns(str(snap.variant_id))
        except BreakdownError as exc:
            note("desglose de precio (no cierra)", exc)
        except Exception as exc:  # noqa: BLE001
            note("desglose de precio", exc)

    cert_facts: CertFacts | None = None
    cert_read = False
    if channel is not None:
        try:
            info = await source.get_certification(str(snap.product_id), channel.channel_id)
            cert_read = True
            if info is not None:
                cert_facts = CertFacts(info.status, info.cost_usd_cents, info.expires_at, info.holder,
                                       channel.certificate_max_share_pct)
        except Exception as exc:  # noqa: BLE001
            note("certificado", exc)

    specs = parse_specs(getattr(snap, "our_specs", None))
    facts = DiagFacts(
        economics=econ,
        sourcing_mode=variant.sourcing_mode if variant else None,
        tier_used=snap.tier_used,
        breakdowns=breakdowns,
        tiers=tiers_from_variant(variant),
        min_quantity_override=variant.min_quantity_override if variant else None,
        min_purchase_cny=channel.minimum_purchase_cny if channel else None,
        box=box_from_specs(specs),
        product=box_from_specs(specs, prefix=""),
        neighbor_boxes=neighbor_boxes_,
        certification=cert_facts,
        certification_read=cert_read,
    )
    diag = diagnose(facts, thresholds)
    row = select_tier(breakdowns, snap.tier_used)
    return ProductDiagnosis(
        diagnosis=diag,
        breakdown=row.to_dict() if row is not None else None,
        tiers_breakdown=[_tier_line(b, econ) for b in (breakdowns or []) if b.usd_available],
        errors=errors,
    )


def _tier_line(b: TierBreakdown, econ: Economics) -> dict[str, Any]:
    """Una línea de la tabla de tramos: rango, costo en yuanes, neto y precio con IVA, y margen contra ML."""
    from app.pricing import semaforo

    margin = semaforo.estimated_margin_pct(econ.ml_median_cents, b.factura_per_unit_minor, econ.commission_pct,
                                           econ.shipping_cents)
    return {
        "min_quantity": b.min_quantity, "max_quantity": b.max_quantity, "cny_unit": float(b.purchase_cny),
        "neto_cents": b.neto_per_unit_minor, "price_cents": b.factura_per_unit_minor,
        "margin_pct": None if margin is None else round(margin, 1),
    }
