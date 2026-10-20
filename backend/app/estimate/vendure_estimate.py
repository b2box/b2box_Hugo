"""Cliente de la query `estimateImportPrice` de Vendure (contrato de la parte 2).

Se lee `status` ANTES que cualquier número: con PRICING_CONFIG_NOT_READY o
NO_BOX_DATA no hay rango. `statusDetail` va solo al log (puede describir la
configuración del canal) y nunca a la respuesta ni a la base.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.estimate.pricing import BOX_QUANTILES, Scenarios
from app.vendure.client import VendureClient

log = logging.getLogger(__name__)

STATUS_OK = "OK"
STATUS_NOT_READY = "PRICING_CONFIG_NOT_READY"
STATUS_NO_BOX = "NO_BOX_DATA"
_MAX_CENTS = 10**12


@dataclass(slots=True)
class ScenarioPrices:
    label: str
    purchase_price_cny: float | None = None
    box_quantile: float | None = None
    cbm_per_unit: float | None = None
    kg_per_unit: float | None = None
    # cantidad → (neto, con IVA) por unidad, en centavos de la moneda del canal
    prices: dict[int, tuple[int, int]] = field(default_factory=dict)


@dataclass(slots=True)
class VendureEstimate:
    status: str
    currency: str = ""
    scenarios: list[ScenarioPrices] = field(default_factory=list)
    box_source: str | None = None
    box_sample_size: int | None = None
    reference_product_count: int | None = None
    tax_classification_code: str | None = None
    tax_classification_share: float | None = None
    contains_batteries: bool | None = None
    sales_vat_pct: float | None = None
    profile_version: int | None = None
    computed_at: str | None = None

    def invoice_by_quantity(self, quantity: int) -> dict[str, int]:
        """{label: invoiceChannelPerUnit} a esa cantidad."""
        return {s.label: s.prices[quantity][1] for s in self.scenarios if quantity in s.prices}


def build_input(quantities: Sequence[int], scenarios: Scenarios,
                reference_product_ids: Sequence[str]) -> dict:
    return {
        "quantities": [int(q) for q in quantities],
        "referenceProductIds": [str(pid) for pid in reference_product_ids],
        "scenarios": [
            {"label": label, "purchasePriceCny": float(price), "boxQuantile": BOX_QUANTILES[label]}
            for label, price in scenarios.as_dict().items()
        ],
    }


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _cents(value: object) -> int | None:
    number = _num(value)
    if number is None or number <= 0 or number > _MAX_CENTS:
        return None
    return int(round(number))


def _short(value: object, limit: int) -> str | None:
    return str(value)[:limit] if isinstance(value, (str, int)) and str(value) else None


def parse(raw: object, quantities: Sequence[int]) -> VendureEstimate:
    """Respuesta cruda → VendureEstimate. Ante una forma que no se entiende,
    status ERROR (nunca un número inventado)."""
    if not isinstance(raw, dict):
        return VendureEstimate("ERROR")
    status = raw.get("status")
    if not isinstance(status, str):
        return VendureEstimate("ERROR")
    detail = raw.get("statusDetail")
    if status != STATUS_OK:
        log.info("estimado: Vendure respondió %s (%s)", status[:40], str(detail or "")[:200])
        return VendureEstimate(status[:40])
    wanted = {int(q) for q in quantities}
    out = VendureEstimate(STATUS_OK)
    out.currency = (_short(raw.get("channelCurrencyCode"), 8) or "").upper()
    for entry in raw.get("scenarios") or []:
        if not isinstance(entry, dict) or entry.get("label") not in BOX_QUANTILES:
            continue
        sc = ScenarioPrices(
            label=entry["label"],
            purchase_price_cny=_num(entry.get("purchasePriceCny")),
            box_quantile=_num(entry.get("boxQuantile")),
            cbm_per_unit=_num(entry.get("cbmPerUnit")),
            kg_per_unit=_num(entry.get("kgPerUnit")),
        )
        for price in entry.get("prices") or []:
            if not isinstance(price, dict):
                continue
            qty = price.get("quantity")
            if isinstance(qty, bool) or not isinstance(qty, int) or qty not in wanted:
                continue
            net, invoice = _cents(price.get("netChannelPerUnit")), _cents(
                price.get("invoiceChannelPerUnit"))
            if net is not None and invoice is not None:
                sc.prices[qty] = (net, invoice)
        out.scenarios.append(sc)
    out.box_source = _short(raw.get("boxSource"), 24)
    sample = _num(raw.get("boxSampleSize"))
    out.box_sample_size = int(sample) if sample is not None else None
    refs = _num(raw.get("referenceProductCount"))
    out.reference_product_count = int(refs) if refs is not None else None
    out.tax_classification_code = _short(raw.get("taxClassificationCode"), 24)
    out.tax_classification_share = _num(raw.get("taxClassificationShare"))
    batteries = raw.get("containsBatteries")
    out.contains_batteries = batteries if isinstance(batteries, bool) else None
    out.sales_vat_pct = _num(raw.get("salesVatPct"))
    version = _num(raw.get("profileVersion"))
    out.profile_version = int(version) if version is not None else None
    out.computed_at = _short(raw.get("computedAt"), 40)
    if not out.currency or not any(s.prices for s in out.scenarios):
        log.warning("estimado: Vendure dijo OK pero sin moneda o sin precios")
        return VendureEstimate("ERROR")
    return out


async def estimate(quantities: Sequence[int], scenarios: Scenarios,
                   reference_product_ids: Sequence[str]) -> VendureEstimate:
    """Llama a Vendure. Lanza lo que lance el cliente (el servicio lo ataja)."""
    raw = await VendureClient().estimate_import_price(
        build_input(quantities, scenarios, reference_product_ids))
    return parse(raw, quantities)
