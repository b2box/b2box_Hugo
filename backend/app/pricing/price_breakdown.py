"""Desglose del precio de un tramo, tal como lo calcula Vendure. Funciones PURAS: sin red, sin DB.

Hugo no recalcula la fórmula de importación: usa lo que devuelve la query
`bulkPriceTierBreakdowns` de la Admin API (plugin advanced-pricing de Vendure). Este módulo
solo **lee** esa respuesta, la valida y la reordena en las 10 partes que se le muestran a
Pao (sección 3.9 del diseño de la bandeja):

     1  Fábrica                     fobUsd
     2  Flete y seguro              fleteUsd + seguroUsd
     3  Aranceles                   arancelesUsd (derecho + antidumping + tasa estadística)
     4  Servicios en destino        serviciosUsd - chinaLogisticsUsd   (con el detalle por servicio)
     5  Logística China             chinaLogisticsUsd
     6  Fee B2BOX                   b2boxFeeUsd
     7  Costo financiero            finCostUsd
     8  Certificado                 certificationUsd
     9  Pasarela de pago            paymentGatewayFeeUsd
    10  IVA de la venta             facturaUsd - netoUsd

Las 10 partes suman la factura (`facturaUsd`) con una tolerancia de 0,01 USD. El IVA de
importación y las percepciones NO se suman: son crédito fiscal, solo entra su costo
financiero (7). Si no suman, `parse_tier_breakdown` falla con un `BreakdownError` explícito: no
inventa una diferencia ni "arregla" una parte para que cierre.

Unidades: los montos en USD son TOTALES de la cantidad del tramo (`quantity`); `netoChannelPerUnit`
y `facturaChannelPerUnit` son POR UNIDAD, en centavos de la moneda del canal (ARS).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

# Diferencia máxima aceptada entre la suma de las 10 partes y la factura (USD).
SUM_TOLERANCE_USD = Decimal("0.01")
# La factura es el neto más el IVA de la venta, redondeada hacia arriba al centavo: puede pasarse hasta un centavo.
INVOICE_TOLERANCE_USD = Decimal("0.011")
# El precio por unidad en pesos contra los dólares y el cambio efectivo: el motor redondea hacia arriba por unidad.
PER_UNIT_REL_TOLERANCE = Decimal("0.005")
PER_UNIT_MIN_TOLERANCE_MINOR = 2

# Qué se le pide a Vendure por tramo. Solo lo que este módulo y el diagnóstico usan.
BREAKDOWN_SELECTION = """
    tierId productVariantId minQuantity maxQuantity goodsKind b2boxFeeSource
    inputs {
      purchasePriceCny { exact } packagingCny { exact } quantity cbmPerUnit totalCbm containerCbm
      weightKgPerUnit chargeableUnits cbmRatio cnyToUsdRate usdToChannelRate
    }
    fobUsd { exact } fleteUsd { exact } seguroUsd { exact } cifUsd { exact } arancelesUsd { exact }
    chinaLogisticsUsd { exact } serviciosUsd { exact }
    servicios { code appliedUsd { exact } prorataMode }
    subtopUsd { exact } b2boxFeeUsd { exact } paymentGatewayFeeUsd { exact } certificationUsd { exact }
    finCostUsd { exact } netoUsd { exact } facturaUsd { exact }
    netoChannelPerUnit { exact minor currencyCode } facturaChannelPerUnit { exact minor currencyCode }
    pctApplied { b2boxFee salesVat }
"""


class BreakdownError(ValueError):
    """La respuesta de Vendure no tiene la forma esperada o el desglose no cierra."""


@dataclass(slots=True, frozen=True)
class ServiceLine:
    code: str
    applied_usd: Decimal
    prorata_mode: str


@dataclass(slots=True, frozen=True)
class Component:
    """Una de las 10 partes del precio."""
    key: str
    label: str
    usd: Decimal                  # total de la cantidad del tramo
    pct_of_invoice: float | None  # sobre la factura (None si la factura es 0)
    ars_per_unit: float | None    # pesos por unidad al cambio efectivo del canal
    detail: tuple[tuple[str, Decimal], ...] = ()


@dataclass(slots=True, frozen=True)
class TierBreakdown:
    tier_id: str
    variant_id: str
    min_quantity: int | None
    max_quantity: int | None
    # inputs
    quantity: int
    purchase_cny: Decimal           # por unidad
    packaging_cny: Decimal
    cbm_per_unit: float
    total_cbm: float
    container_cbm: float
    weight_kg_per_unit: float
    chargeable_units: float         # el mayor entre m³ y toneladas del tramo
    cbm_ratio: float
    cny_to_usd: Decimal             # cambio efectivo (con colchón)
    usd_to_channel: Decimal
    # USD, totales del tramo
    fob_usd: Decimal
    flete_usd: Decimal
    seguro_usd: Decimal
    cif_usd: Decimal
    aranceles_usd: Decimal
    china_logistics_usd: Decimal
    servicios_usd: Decimal
    subtotal_usd: Decimal
    fee_usd: Decimal
    gateway_usd: Decimal
    certification_usd: Decimal
    fin_cost_usd: Decimal
    neto_usd: Decimal
    factura_usd: Decimal
    servicios_lines: tuple[ServiceLine, ...]
    fee_pct: Decimal | None
    # por unidad, en centavos del canal
    neto_per_unit_minor: int
    factura_per_unit_minor: int
    channel_currency: str
    sales_vat: Decimal | None = None       # % de IVA de la venta con el que se armó la factura (None = Vendure no lo mandó)
    # False cuando no hay desglose en dólares que mostrar (tramo sin precio o abastecimiento local):
    # todo viene en cero y no hay nada que sumar ni que explicar.
    usd_available: bool = True
    extras: dict[str, Any] = field(default_factory=dict)

    # ── derivados que usa el diagnóstico ──────────────────────────
    @property
    def nbg_usd(self) -> Decimal:
        """Neto antes de pasarela y de IVA de venta: subtotal + fee + costo financiero + certificado."""
        return self.subtotal_usd + self.fee_usd + self.fin_cost_usd + self.certification_usd

    @property
    def logistics_usd(self) -> Decimal:
        """Flete + servicios (los servicios ya incluyen la logística en China). Se prorratea por volumen o peso."""
        return self.flete_usd + self.servicios_usd

    @property
    def iva_venta_usd(self) -> Decimal:
        return self.factura_usd - self.neto_usd

    def parts(self) -> tuple[tuple[str, str, Decimal], ...]:
        """(clave, rótulo, USD) de las 10 partes, en el orden de la pantalla."""
        return (
            ("fabrica", "Fábrica", self.fob_usd),
            ("flete", "Flete y seguro", self.flete_usd + self.seguro_usd),
            ("aranceles", "Aranceles", self.aranceles_usd),
            ("servicios", "Servicios en destino", self.servicios_usd - self.china_logistics_usd),
            ("logistica_china", "Logística China", self.china_logistics_usd),
            ("fee", "Fee B2BOX", self.fee_usd),
            ("costo_financiero", "Costo financiero de los impuestos", self.fin_cost_usd),
            ("certificado", "Certificado", self.certification_usd),
            ("pasarela", "Pasarela de pago", self.gateway_usd),
            ("iva_venta", "IVA de la venta", self.iva_venta_usd),
        )

    def parts_sum_usd(self) -> Decimal:
        return sum((usd for _k, _l, usd in self.parts()), Decimal(0))

    def components(self) -> list[Component]:
        """Las 10 partes con su % de la factura y sus pesos por unidad (cambio efectivo del canal)."""
        if not self.usd_available:
            return []
        out: list[Component] = []
        for key, label, usd in self.parts():
            pct = float(usd / self.factura_usd * 100) if self.factura_usd else None
            ars = float(usd / self.quantity * self.usd_to_channel) if self.quantity else None
            detail: tuple[tuple[str, Decimal], ...] = ()
            if key == "servicios":
                detail = tuple((s.code, s.applied_usd) for s in self.servicios_lines)
            out.append(Component(key, label, usd, pct, ars, detail))
        return out

    def to_dict(self) -> dict[str, Any]:
        """Lo que la bandeja guarda y muestra del tramo (números y códigos; nada de texto de terceros)."""
        return {
            "tier_id": self.tier_id,
            "min_quantity": self.min_quantity,
            "max_quantity": self.max_quantity,
            "quantity": self.quantity,
            "usd_available": self.usd_available,
            "cny_unit": float(self.purchase_cny),
            "cny_to_usd": float(self.cny_to_usd),
            "usd_to_channel": float(self.usd_to_channel),
            "chargeable": self.chargeable_units,
            "total_cbm": self.total_cbm,
            "factura_usd": float(self.factura_usd),
            "neto_usd": float(self.neto_usd),
            "factura_unit_cents": self.factura_per_unit_minor,
            "neto_unit_cents": self.neto_per_unit_minor,
            "currency": self.channel_currency,
            "fee_pct": float(self.fee_pct) if self.fee_pct is not None else None,
            "components": [
                {"key": c.key, "label": c.label, "usd": float(c.usd), "pct": _round(c.pct_of_invoice, 2),
                 "ars_unit": _round(c.ars_per_unit, 2),
                 "detail": [{"code": code, "usd": float(v)} for code, v in c.detail]}
                for c in self.components()
            ],
        }


def _round(v: float | None, digits: int) -> float | None:
    return None if v is None else round(v, digits)


# ─── Parser ───────────────────────────────────────────────────────


def _need(raw: Mapping[str, Any], key: str, where: str) -> Any:
    if not isinstance(raw, Mapping) or key not in raw or raw[key] is None:
        raise BreakdownError(f"{where}: falta «{key}» en la respuesta de Vendure")
    return raw[key]


def _dec(raw: Mapping[str, Any], key: str, where: str) -> Decimal:
    """`{exact: "123.45"}` → Decimal."""
    node = _need(raw, key, where)
    text = node.get("exact") if isinstance(node, Mapping) else node
    try:
        value = Decimal(str(text))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise BreakdownError(f"{where}: «{key}» no es un número ({text!r})") from exc
    if not value.is_finite():
        raise BreakdownError(f"{where}: «{key}» no es finito ({text!r})")
    return value


def _num(raw: Mapping[str, Any], key: str, where: str) -> float:
    value = _need(raw, key, where)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            value = float(str(value))
        except ValueError as exc:
            raise BreakdownError(f"{where}: «{key}» no es un número ({value!r})") from exc
    if value != value or value in (float("inf"), float("-inf")):
        raise BreakdownError(f"{where}: «{key}» no es finito")
    return float(value)


def _rate(raw: Mapping[str, Any], key: str, where: str) -> Decimal:
    text = _need(raw, key, where)
    try:
        value = Decimal(str(text))
    except (InvalidOperation, ValueError) as exc:
        raise BreakdownError(f"{where}: «{key}» no es un número ({text!r})") from exc
    if not value.is_finite() or value < 0:
        raise BreakdownError(f"{where}: «{key}» inválido ({text!r})")
    return value


def _minor(raw: Mapping[str, Any], key: str, where: str) -> tuple[int, str]:
    node = _need(raw, key, where)
    if not isinstance(node, Mapping) or node.get("minor") is None:
        raise BreakdownError(f"{where}: «{key}» no trae el valor en centavos")
    try:
        minor = int(node["minor"])
    except (TypeError, ValueError) as exc:
        raise BreakdownError(f"{where}: «{key}.minor» no es un entero ({node['minor']!r})") from exc
    return minor, str(node.get("currencyCode") or "")


def _opt_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_tier_breakdown(raw: Mapping[str, Any]) -> TierBreakdown:
    """Un `BulkPriceTierBreakdown` de la Admin API → `TierBreakdown`, validado.

    Falla con `BreakdownError` si falta un campo, si algo no es un número, o si las 10 partes no
    suman la factura (diferencia mayor a `SUM_TOLERANCE_USD`)."""
    if not isinstance(raw, Mapping):
        raise BreakdownError("el desglose no es un objeto")
    tier = str(raw.get("tierId") or "?")
    where = f"tramo {tier}"
    inputs = _need(raw, "inputs", where)
    quantity = int(_num(inputs, "quantity", where))
    if quantity <= 0:
        raise BreakdownError(f"{where}: cantidad inválida ({quantity})")
    lines = tuple(
        ServiceLine(str(s.get("code") or ""), _dec(s, "appliedUsd", where), str(s.get("prorataMode") or ""))
        for s in (raw.get("servicios") or []) if isinstance(s, Mapping)
    )
    neto_minor, currency = _minor(raw, "netoChannelPerUnit", where)
    factura_minor, _ = _minor(raw, "facturaChannelPerUnit", where)
    pct = raw.get("pctApplied") if isinstance(raw.get("pctApplied"), Mapping) else {}
    try:
        fee_pct = Decimal(str(pct.get("b2boxFee"))) if pct.get("b2boxFee") is not None else None
    except InvalidOperation:
        fee_pct = None
    try:
        sales_vat = Decimal(str(pct.get("salesVat"))) if pct.get("salesVat") is not None else None
        if sales_vat is not None and (not sales_vat.is_finite() or sales_vat < 0):
            sales_vat = None
    except InvalidOperation:
        sales_vat = None
    bd = TierBreakdown(
        tier_id=tier,
        variant_id=str(raw.get("productVariantId") or ""),
        min_quantity=_opt_int(raw.get("minQuantity")),
        max_quantity=_opt_int(raw.get("maxQuantity")),
        quantity=quantity,
        purchase_cny=_dec(inputs, "purchasePriceCny", where),
        packaging_cny=_dec(inputs, "packagingCny", where),
        cbm_per_unit=_num(inputs, "cbmPerUnit", where),
        total_cbm=_num(inputs, "totalCbm", where),
        container_cbm=_num(inputs, "containerCbm", where),
        weight_kg_per_unit=_num(inputs, "weightKgPerUnit", where),
        chargeable_units=_num(inputs, "chargeableUnits", where),
        cbm_ratio=_num(inputs, "cbmRatio", where),
        cny_to_usd=_rate(inputs, "cnyToUsdRate", where),
        usd_to_channel=_rate(inputs, "usdToChannelRate", where),
        fob_usd=_dec(raw, "fobUsd", where),
        flete_usd=_dec(raw, "fleteUsd", where),
        seguro_usd=_dec(raw, "seguroUsd", where),
        cif_usd=_dec(raw, "cifUsd", where),
        aranceles_usd=_dec(raw, "arancelesUsd", where),
        china_logistics_usd=_dec(raw, "chinaLogisticsUsd", where),
        servicios_usd=_dec(raw, "serviciosUsd", where),
        subtotal_usd=_dec(raw, "subtopUsd", where),
        fee_usd=_dec(raw, "b2boxFeeUsd", where),
        gateway_usd=_dec(raw, "paymentGatewayFeeUsd", where),
        certification_usd=_dec(raw, "certificationUsd", where),
        fin_cost_usd=_dec(raw, "finCostUsd", where),
        neto_usd=_dec(raw, "netoUsd", where),
        factura_usd=_dec(raw, "facturaUsd", where),
        servicios_lines=lines,
        fee_pct=fee_pct,
        sales_vat=sales_vat,
        neto_per_unit_minor=neto_minor,
        factura_per_unit_minor=factura_minor,
        channel_currency=currency,
    )
    # Sin dólares que explicar: tramo sin precio de fábrica, o abastecimiento local (Vendure devuelve todo en ceros). Solo si
    # TODO está en cero: un tramo con fábrica, factura y neto en 0 pero con flete, aranceles, servicios o fee es un desglose
    # roto, no un tramo «sin dólares». (El certificado no cuenta: un abastecimiento local lo trae.)
    if bd.factura_usd == 0 and bd.fob_usd == 0 and bd.neto_usd == 0:
        stray = [name for name, v in (
            ("flete", bd.flete_usd), ("seguro", bd.seguro_usd), ("cif", bd.cif_usd), ("aranceles", bd.aranceles_usd),
            ("logística China", bd.china_logistics_usd), ("servicios", bd.servicios_usd), ("subtotal", bd.subtotal_usd),
            ("fee", bd.fee_usd), ("pasarela", bd.gateway_usd), ("costo financiero", bd.fin_cost_usd)) if v != 0]
        if stray:
            raise BreakdownError(f"{where}: fábrica, factura y neto en 0 pero hay montos distintos de cero ({', '.join(stray)})")
        return replace(bd, usd_available=False)
    gap = abs(bd.parts_sum_usd() - bd.factura_usd)
    if gap > SUM_TOLERANCE_USD:
        raise BreakdownError(
            f"{where}: las 10 partes suman {bd.parts_sum_usd()} USD y la factura es {bd.factura_usd} USD "
            f"(diferencia {gap} USD, tolerancia {SUM_TOLERANCE_USD})")
    # La parte 10 es factura - neto: por sí sola no ata la factura a nada. Se controla además contra el IVA de la venta.
    if bd.sales_vat is not None:
        expected = bd.neto_usd * (1 + bd.sales_vat / 100)
        if abs(bd.factura_usd - expected) > INVOICE_TOLERANCE_USD:
            raise BreakdownError(
                f"{where}: la factura es {bd.factura_usd} USD y neto + IVA de la venta da {expected.quantize(Decimal('0.0001'))} USD")
    # Y el precio por unidad en pesos contra los dólares al cambio efectivo.
    for label, usd_total, minor in (("factura", bd.factura_usd, factura_minor), ("neto", bd.neto_usd, neto_minor)):
        expected_minor = usd_total * bd.usd_to_channel * 100 / bd.quantity
        tolerance = max(Decimal(PER_UNIT_MIN_TOLERANCE_MINOR), abs(expected_minor) * PER_UNIT_REL_TOLERANCE)
        if abs(Decimal(minor) - expected_minor) > tolerance:
            raise BreakdownError(
                f"{where}: el {label} por unidad es {minor} centavos y los dólares al cambio efectivo dan {expected_minor.quantize(Decimal('1'))}")
    return bd


def parse_tier_breakdowns(raw: Any) -> list[TierBreakdown]:
    """La lista completa de `bulkPriceTierBreakdowns` (un elemento por tramo habilitado)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise BreakdownError("bulkPriceTierBreakdowns no devolvió una lista")
    return [parse_tier_breakdown(item) for item in raw]
