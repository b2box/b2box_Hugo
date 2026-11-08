"""Factor para ESTIMAR el precio de la app de un producto que todavía no está en Vendure, sin otra fórmula de importación.

    precio_app_estimado (ARS con IVA, por unidad) = precio de fábrica (CNY) × mediana(factura / fob) × cambio (ARS por CNY)

`factura / fob` sale de desgloses REALES de Vendure (`bulkPriceTierBreakdowns`, la misma query que la bandeja; Hugo no
recalcula nada) guardados en `price_factor_sample`:

  * los que ya leyó la bandeja de Pao (`price_review_item.breakdown`, se copian con `refresh_from_review_items`);
  * opcional, una muestra del catálogo activo (`sample_catalog`, job diario con tope `pm_factor_sample_daily_max`, 0 =
    apagado). Los de la bandeja son rojos: solos sesgan el factor hacia arriba (ver `how.sesgo`).

Cómo se elige la muestra (pura, `compute`):

  1. categoría: si el candidato trae `categoria` y hay >= `MIN_CASES` casos con esa categoría, solo esos; si no, todos;
  2. valor por m³: si el candidato trae medidas y, dentro de lo anterior, hay >= `MIN_CASES` casos con un valor de fábrica
     por m³ dentro de ×/÷ `M3_BAND` del suyo, solo esos (lo liviano y caro paga menos flete por peso de precio);
  3. la mediana de factura/fob de lo que quedó, y el cambio = mediana de `cny_to_usd × usd_to_channel` de los 20 casos más
     nuevos (el cambio se mueve: los viejos no sirven para eso).

Todo queda en `how` (de dónde, n, mediana, cambio) y el precio SIEMPRE se rotula estimado.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.clock import utcnow
from app.db.models import PriceFactorSample, PriceReviewItem
from app.db.session import engine

log = logging.getLogger(__name__)

MIN_CASES = 20
M3_BAND = 2.0
FX_RECENT = 20
# Un factura/fob fuera de esto es un desglose roto (o un tramo de 1 unidad con todo el fijo encima): no entra.
RATIO_MIN, RATIO_MAX = 1.0, 30.0


@dataclass(slots=True, frozen=True)
class Sample:
    ratio: float                 # factura_usd / fob_usd
    fx: float | None             # ARS por CNY (cny_to_usd × usd_to_channel)
    value_m3_usd: float | None   # fob_usd / total_cbm
    category: str | None
    at: datetime
    cny_to_usd: float | None = None


@dataclass(slots=True)
class Factor:
    ratio: float | None
    fx: float | None
    how: dict[str, Any] = field(default_factory=dict)

    @property
    def ars_per_cny(self) -> float | None:
        return None if self.ratio is None or self.fx is None else self.ratio * self.fx


def _norm_cat(text: str | None) -> str | None:
    t = " ".join((text or "").split()).casefold()
    return t[:80] or None


def to_sample(row: PriceFactorSample) -> Sample | None:
    if not row.fob_usd or row.fob_usd <= 0 or not row.factura_usd or row.factura_usd <= 0:
        return None
    ratio = row.factura_usd / row.fob_usd
    if not (RATIO_MIN <= ratio <= RATIO_MAX) or not math.isfinite(ratio):
        return None
    fx = (row.cny_to_usd * row.usd_to_channel) if row.cny_to_usd and row.usd_to_channel else None
    vm3 = (row.fob_usd / row.total_cbm) if row.total_cbm and row.total_cbm > 0 else None
    return Sample(ratio, fx if fx and fx > 0 else None, vm3, _norm_cat(row.category), row.captured_at,
                  row.cny_to_usd if row.cny_to_usd and row.cny_to_usd > 0 else None)


def compute(samples: Sequence[Sample], *, cny_unit: float, category: str | None = None,
            medidas_cm: Sequence[float] | None = None) -> Factor:
    """El factor para un candidato. PURA. `ratio` None si no hay casos."""
    how: dict[str, Any] = {"metodo": "precio_fabrica × mediana(factura/fob) × cambio", "estimado": True,
                           "min_casos": MIN_CASES}
    pool = list(samples)
    how["n_total"] = len(pool)
    if not pool:
        how["fuente"] = "sin_casos"
        return Factor(None, None, how)
    cat = _norm_cat(category)
    same = [x for x in pool if cat and x.category == cat]
    if cat and len(same) >= MIN_CASES:
        pool, how["fuente"] = same, "categoria"
    else:
        how["fuente"] = "global"
        if cat:
            how["categoria_casos"] = len(same)
    fxs = [x.fx for x in sorted(samples, key=lambda x: x.at, reverse=True) if x.fx][:FX_RECENT]
    fx = statistics.median(fxs) if fxs else None
    cny_usd = [x.cny_to_usd for x in samples if x.cny_to_usd]
    if medidas_cm and len(medidas_cm) == 3 and all(isinstance(v, (int, float)) and v > 0 for v in medidas_cm) \
            and cny_usd and cny_unit > 0:
        # Valor de fábrica por m³ del candidato, en USD (como lo guardado: fob_usd / total_cbm), con el cambio CNY→USD
        # mediano de los casos.
        cbm = medidas_cm[0] * medidas_cm[1] * medidas_cm[2] / 1_000_000.0
        vm3 = statistics.median(cny_usd) * cny_unit / cbm
        band = [x for x in pool if x.value_m3_usd and vm3 / M3_BAND <= x.value_m3_usd <= vm3 * M3_BAND]
        how["valor_m3_usd"] = round(vm3, 1)
        how["m3_casos"] = len(band)
        if len(band) >= MIN_CASES:
            pool = band
            how["fuente"] += "+m3"
    ratios = [x.ratio for x in pool]
    ratio = statistics.median(ratios)
    how.update({"n": len(pool), "mediana_factura_fob": round(ratio, 3), "cambio_ars_por_cny": None if fx is None
                else round(fx, 3), "n_cambio": len(fxs)})
    return Factor(ratio, fx, how)


def estimate_cents(factor: Factor, cny_unit: float) -> int | None:
    """Precio de la app estimado en centavos de ARS (con IVA, por unidad)."""
    k = factor.ars_per_cny
    if k is None or cny_unit <= 0:
        return None
    return int(round(cny_unit * k * 100))


# ─── Base ─────────────────────────────────────────────────────────


def load_samples() -> list[Sample]:
    with Session(engine) as s:
        rows = s.exec(select(PriceFactorSample)).all()
    return [x for x in (to_sample(r) for r in rows) if x is not None]


def sample_from_tier(product_id: str, tier: dict[str, Any], *, source: str, category: str | None = None,
                     now: datetime | None = None) -> PriceFactorSample | None:
    """Un `TierBreakdown.to_dict()` (lo que guarda la bandeja) → la fila. None si no sirve."""
    if not isinstance(tier, dict) or not tier.get("usd_available", True):
        return None
    fob = next((c.get("usd") for c in tier.get("components") or [] if isinstance(c, dict) and c.get("key") == "fabrica"),
               None)
    try:
        fob = float(fob) if fob is not None else 0.0
        factura = float(tier.get("factura_usd") or 0.0)
        cny = float(tier.get("cny_unit") or 0.0)
        unit_cents = int(tier.get("factura_unit_cents") or 0)
    except (TypeError, ValueError):
        return None
    if fob <= 0 or factura <= 0 or cny <= 0 or unit_cents <= 0:
        return None
    return PriceFactorSample(
        product_id=str(product_id)[:64], tier_id=str(tier.get("tier_id") or "")[:64], category=_norm_cat(category),
        cny_unit=cny, factura_unit_cents=unit_cents, fob_usd=fob, factura_usd=factura,
        total_cbm=_float(tier.get("total_cbm")), cny_to_usd=_float(tier.get("cny_to_usd")),
        usd_to_channel=_float(tier.get("usd_to_channel")), source=source, captured_at=now or utcnow())


def _float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def upsert(rows: Sequence[PriceFactorSample]) -> int:
    """Guarda (o reemplaza) por (producto, tramo). Devuelve cuántas filas quedaron."""
    done = 0
    with Session(engine) as s:
        for row in rows:
            old = s.exec(select(PriceFactorSample).where(PriceFactorSample.product_id == row.product_id,
                                                         PriceFactorSample.tier_id == row.tier_id)).first()
            if old is not None:
                for k in ("category", "cny_unit", "factura_unit_cents", "fob_usd", "factura_usd", "total_cbm",
                          "cny_to_usd", "usd_to_channel", "source", "captured_at"):
                    if k == "category" and getattr(row, k) is None:
                        continue
                    setattr(old, k, getattr(row, k))
                s.add(old)
            else:
                s.add(row)
            try:
                s.commit()
                done += 1
            except IntegrityError:
                s.rollback()
    return done


def refresh_from_review_items() -> int:
    """Copia los desgloses que ya guardó la bandeja (`price_review_item.breakdown`, el tramo usado)."""
    with Session(engine) as s:
        rows = s.exec(select(PriceReviewItem.product_id, PriceReviewItem.breakdown, PriceReviewItem.state_since)
                      .where(PriceReviewItem.breakdown.is_not(None))).all()  # type: ignore[union-attr]
    samples: list[PriceFactorSample] = []
    for pid, raw, at in rows:
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            continue
        tier = data.get("tier") if isinstance(data, dict) else None
        row = sample_from_tier(pid, tier, source="bandeja", now=at) if tier else None
        if row is not None:
            samples.append(row)
    return upsert(samples) if samples else 0


async def sample_catalog(max_reads: int, client: Any = None) -> int:
    """Muestra del catálogo ACTIVO (job diario, apagado con 0): lee el desglose del primer tramo de productos activos que
    todavía no tienen muestra, con tope de lecturas. Solo `query` a Vendure. Devuelve cuántas muestras guardó."""
    if max_reads <= 0:
        return 0
    from app.vendure import catalog as vendure_catalog
    from app.vendure.client import VendureClient

    client = client or VendureClient()
    with Session(engine) as s:
        have = set(s.exec(select(PriceFactorSample.product_id)).all())
    products = [p for p in await vendure_catalog.get_catalog() if p.enabled and p.id not in have]
    saved = 0
    for p in products[:max_reads]:
        try:
            full = await client.get_product_priced(p.id)
            variant = next((v for v in (full.priced_variants or []) if v.price_with_tax_cents), None) if full else None
            if variant is None:
                continue
            tiers = await client.get_tier_breakdowns(variant.id)
        except Exception as exc:  # noqa: BLE001
            log.info("factor: no se pudo leer el desglose de %s (%s)", p.id, type(exc).__name__)
            continue
        usable = [t for t in tiers if t.usd_available]
        if not usable:
            continue
        first = min(usable, key=lambda t: t.min_quantity or 0)
        row = sample_from_tier(p.id, first.to_dict(), source="muestreo")
        if row is not None and upsert([row]):
            saved += 1
    return saved
