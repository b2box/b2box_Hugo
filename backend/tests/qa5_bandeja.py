"""QA independiente de la bandeja de Pao (PR1+PR2): modelo de precios y Vendure falso HECHOS APARTE.

Nada de acá importa los helpers del developer (`review_fixtures`, `review_world`): el modelo de precios replica las
identidades que se verificaron contra el golden del motor real de Vendure (fdd7579df):

    subtop  = fob + flete + seguro + aranceles + servicios            (cif + aranceles + servicios)
    fee     = 20 % de subtop
    nbg     = subtop + fee + costo financiero + certificado
    neto    = nbg / (1 - 3,5 %)         → pasarela = neto - nbg
    factura = neto * 1,21               → IVA de la venta = factura - neto

Con eso se arman desgloses CONSISTENTES (las 10 partes suman la factura) de cualquier producto, y se puede
recalcular "qué precio daría con la caja bien cargada" sin pasar por el código a probar.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal as D
from typing import Any

CNY_USD = D("0.1427")
USD_ARS = D("1500")
FLETE_PER_M3 = D("103.17")        # 3.095 USD por contenedor de 30 m³
SERV_PER_M3 = D("358.80")         # servicios en destino (con depósito fiscal)
CHINA_PER_M3 = D("14.27")         # 3.000 ¥ por contenedor de 30 m³
DUTY = D("0.23")                  # derecho 20 % + tasa estadística 3 %
FEE = D("0.20")
FIN = D("0.002")                  # costo financiero: 0,2 % del subtotal
GATEWAY = D("0.035")
VAT = D("1.21")
INSURANCE = D("0.005")

COMMISSION = 13.0
YELLOW = 10.0
GREEN = 30.0


def q4(x: D) -> D:
    return x.quantize(D("0.0001"), rounding=ROUND_HALF_UP)


def ex(x: D) -> dict[str, str]:
    return {"exact": str(q4(x)), "currencyCode": "USD", "minor": None}


@dataclass
class Tier:
    """Un tramo: cuánto cuesta en yuanes, cuántas unidades, cómo es la caja y qué otros cargos lleva."""
    min_q: int
    max_q: int | None
    cny: str
    q: int
    cbm_unit: str                 # m³ de la caja individual
    kg_unit: str                  # kg brutos de la caja individual
    cert_usd: str = "0"
    service_mode: str = "cbm"
    empty: bool = False           # tramo sin precio (PA00060): Vendure lo devuelve todo en ceros

    def build(self, variant: str) -> dict[str, Any]:
        if self.empty:
            return self._zeros(variant)
        cny, q = D(self.cny), D(self.q)
        cbm_total = D(self.cbm_unit) * q
        tons = D(self.kg_unit) * q / 1000
        chargeable = max(cbm_total, tons)
        fob = cny * q * CNY_USD
        flete = chargeable * FLETE_PER_M3
        seguro = (fob + flete) * INSURANCE
        cif = fob + flete + seguro
        aranceles = cif * DUTY
        china = chargeable * CHINA_PER_M3
        serv_lines = chargeable * SERV_PER_M3
        servicios = serv_lines + china
        subtop = cif + aranceles + servicios
        fee = subtop * FEE
        fin = subtop * FIN
        cert = D(self.cert_usd)
        nbg = subtop + fee + fin + cert
        neto = nbg / (1 - GATEWAY)
        gw = neto - nbg
        factura = neto * VAT
        neto_unit = int((neto / q * USD_ARS * 100).quantize(D("1"), rounding=ROUND_HALF_UP))
        fact_unit = int((factura / q * USD_ARS * 100).quantize(D("1"), rounding=ROUND_HALF_UP))
        return {
            "tierId": f"{variant}-{self.min_q}", "productVariantId": variant,
            "minQuantity": self.min_q, "maxQuantity": self.max_q, "goodsKind": "resale", "b2boxFeeSource": "CHANNEL_DEFAULT",
            "inputs": {
                "purchasePriceCny": {"exact": self.cny}, "packagingCny": {"exact": "0"}, "quantity": self.q,
                "cbmPerUnit": float(self.cbm_unit), "totalCbm": float(cbm_total), "containerCbm": 30.0,
                "weightKgPerUnit": float(self.kg_unit), "chargeableUnits": float(chargeable),
                "cbmRatio": float(chargeable / 30), "cnyToUsdRate": str(CNY_USD), "usdToChannelRate": str(USD_ARS)},
            "fobUsd": ex(fob), "fleteUsd": ex(flete), "seguroUsd": ex(seguro), "cifUsd": ex(cif),
            "arancelesUsd": ex(aranceles), "chinaLogisticsUsd": ex(china), "serviciosUsd": ex(servicios),
            "servicios": [{"code": "deposito_fiscal", "appliedUsd": ex(serv_lines), "prorataMode": self.service_mode},
                          {"code": "china_logistics", "appliedUsd": ex(china), "prorataMode": "cbm"}],
            "subtopUsd": ex(subtop), "b2boxFeeUsd": ex(fee), "paymentGatewayFeeUsd": ex(gw),
            "certificationUsd": ex(cert), "finCostUsd": ex(fin), "netoUsd": ex(neto), "facturaUsd": ex(factura),
            "netoChannelPerUnit": {"exact": "x", "minor": neto_unit, "currencyCode": "ARS"},
            "facturaChannelPerUnit": {"exact": "x", "minor": fact_unit, "currencyCode": "ARS"},
            "pctApplied": {"b2boxFee": "20"},
        }

    def _zeros(self, variant: str) -> dict[str, Any]:
        z = ex(D(0))
        return {
            "tierId": f"{variant}-{self.min_q}", "productVariantId": variant, "minQuantity": self.min_q,
            "maxQuantity": self.max_q, "goodsKind": "resale", "b2boxFeeSource": "CHANNEL_DEFAULT",
            "inputs": {"purchasePriceCny": {"exact": "0"}, "packagingCny": {"exact": "0"}, "quantity": self.q,
                       "cbmPerUnit": 0.0, "totalCbm": 0.0, "containerCbm": 30.0, "weightKgPerUnit": 0.0,
                       "chargeableUnits": 0.0, "cbmRatio": 0.0, "cnyToUsdRate": str(CNY_USD), "usdToChannelRate": str(USD_ARS)},
            "fobUsd": z, "fleteUsd": z, "seguroUsd": z, "cifUsd": z, "arancelesUsd": z, "chinaLogisticsUsd": z,
            "serviciosUsd": z, "servicios": [], "subtopUsd": z, "b2boxFeeUsd": z, "paymentGatewayFeeUsd": z,
            "certificationUsd": z, "finCostUsd": z, "netoUsd": z, "facturaUsd": z,
            "netoChannelPerUnit": {"exact": "0", "minor": 0, "currencyCode": "ARS"},
            "facturaChannelPerUnit": {"exact": "0", "minor": 0, "currencyCode": "ARS"}, "pctApplied": {"b2boxFee": "20"},
        }

    def facts(self, b: dict[str, Any]) -> dict[str, int]:
        return {"neto_unit": b["netoChannelPerUnit"]["minor"], "factura_unit": b["facturaChannelPerUnit"]["minor"]}


def price_max(median: int, commission: float = COMMISSION, shipping: int = 0, yellow: float = YELLOW) -> float:
    return (median * (1 - commission / 100) - shipping) / (1 + yellow / 100)


def margin_pct(median: int, our: int, commission: float = COMMISSION, shipping: int = 0) -> float:
    return (median - median * commission / 100 - shipping - our) / our * 100


def color_of(median: int, our: int) -> str:
    """El semáforo, escrito aparte: por encima de ML → rojo; margen ≥ 30 verde; ≥ 10 amarillo; si no rojo."""
    if our > median:
        return "rojo"
    m = margin_pct(median, our)
    return "verde" if m >= GREEN else "amarillo" if m >= YELLOW else "rojo"


@dataclass
class Scenario:
    """Un producto de prueba: sus tramos (para el desglose), la caja y el producto cargados, y lo que dice ML."""
    pid: str
    variant: str
    tiers: list[Tier]
    used_min_q: int                       # el tramo con el que se calculó nuestro precio
    median: int                           # mediana de ML (centavos)
    box: tuple[float, float, float, float] | None = None       # L, W, H cm; kg
    product: tuple[float, float, float, float] | None = None
    sourcing: str | None = "import"
    sale_prices: dict[int, int] | None = None                  # min_q → salePrice guardado (pisa el calculado)
    cost_overrides: dict[int, int | None] = field(default_factory=dict)   # min_q → price en centavos de ¥ (None = sin costo)
    cert: dict[str, Any] | None = None
    name: str = ""

    def breakdowns(self) -> list[dict[str, Any]]:
        return [t.build(self.variant) for t in self.tiers]

    def used(self) -> dict[str, Any]:
        return next(b for b in self.breakdowns() if b["minQuantity"] == self.used_min_q)

    @property
    def our_price(self) -> int:
        """priceWithTax = el neto guardado del tramo usado por el IVA."""
        sale = (self.sale_prices or {}).get(self.used_min_q)
        if sale is None:
            sale = self.used()["netoChannelPerUnit"]["minor"]
        return int((D(sale) * VAT).quantize(D("1"), rounding=ROUND_HALF_UP))

    def tier_rows(self) -> list[dict[str, Any]]:
        out = []
        for pos, (t, b) in enumerate(zip(self.tiers, self.breakdowns())):
            if t.empty:
                out.append({"position": pos, "enabled": True, "minQuantity": t.min_q, "maxQuantity": t.max_q,
                            "salePrice": (self.sale_prices or {}).get(t.min_q), "price": None})
                continue
            sale = (self.sale_prices or {}).get(t.min_q, b["netoChannelPerUnit"]["minor"])
            cost = self.cost_overrides.get(t.min_q, int((D(t.cny) * 100).to_integral_value()))
            out.append({"position": pos, "enabled": True, "minQuantity": t.min_q, "maxQuantity": t.max_q,
                        "salePrice": sale, "price": cost})
        return out

    def custom_fields(self) -> dict[str, Any]:
        cf: dict[str, Any] = {"sourcingMode": self.sourcing, "minQuantityOverride": None}
        if self.box:
            cf.update(boxLength=self.box[0], boxWidth=self.box[1], boxHeight=self.box[2], boxWeight=self.box[3])
        if self.product:
            cf.update(length=self.product[0], width=self.product[1], height=self.product[2], weight=self.product[3])
        return cf

    def raw_variant(self) -> dict[str, Any]:
        return {"id": self.variant, "name": "", "sku": f"SKU{self.pid}", "priceWithTax": self.our_price,
                "currencyCode": "ARS", "customFields": self.custom_fields(), "bulkPriceTiers": self.tier_rows()}

    def raw_product(self, *, enabled: bool = True) -> dict[str, Any]:
        return {"id": self.pid, "name": self.name or f"Producto {self.pid}", "slug": f"producto-{self.pid}",
                "description": "", "enabled": enabled, "updatedAt": "2026-10-01T12:00:00.000Z",
                "customFields": {"b2boxProductCode": f"BX{self.pid}"},
                "featuredAsset": {"preview": f"https://cdn.b2box/{self.pid}-p.jpg", "source": f"https://cdn.b2box/{self.pid}.jpg"},
                "variantList": {"totalItems": 1, "items": [self.raw_variant()]}}

    def specs(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for prefix, dims in (("box_", self.box), ("", self.product)):
            if dims:
                out.update({f"{prefix}length": dims[0], f"{prefix}width": dims[1], f"{prefix}height": dims[2],
                            f"{prefix}weight": dims[3]})
        return out


_OP = re.compile(r"^\s*(query|mutation|subscription)\s+(\w+)?", re.I)


class VendureWorld:
    """Vendure falso A NIVEL HTTP, responde por el cuerpo GraphQL. Registra TODO (método, host, tipo y operación)."""

    HOST = "example.invalid"

    def __init__(self) -> None:
        self.scenarios: dict[str, Scenario] = {}           # por variante
        self.by_product: dict[str, Scenario] = {}
        self.enabled: dict[str, bool] = {}
        self.log: list[dict[str, Any]] = []
        self.certifications: dict[str, dict[str, Any] | None] = {}
        self.breakdown_error: set[str] = set()
        self.min_purchase = 500
        self.dump_rate: dict[str, int] = {}

    def add(self, sc: Scenario, *, enabled: bool = True) -> Scenario:
        self.scenarios[sc.variant] = sc
        self.by_product[sc.pid] = sc
        self.enabled[sc.pid] = enabled
        return sc

    # ── lo que miran las pruebas ──
    def writes(self) -> list[dict[str, Any]]:
        """Todo lo que NO es una lectura: cualquier mutation salvo el login, y cualquier request que no sea POST a GraphQL."""
        return [r for r in self.log if (r["kind"] != "query" and r["op"] != "Login") or r["method"] != "POST"]

    def ops(self) -> list[str]:
        return [r["op"] for r in self.log]

    # ── el servidor ──
    def respond(self, body: dict[str, Any], method: str = "POST") -> tuple[int, dict[str, Any]]:
        query = body.get("query", "")
        v = body.get("variables") or {}
        m = _OP.match(query)
        kind, op = (m.group(1).lower(), m.group(2) or "") if m else ("?", "")
        self.log.append({"kind": kind, "op": op, "method": method, "query": query, "variables": v})
        if kind != "query":
            return 500, {"errors": [{"message": "escritura prohibida en QA"}]}
        if op == "Products":
            items = [sc.raw_product(enabled=self.enabled[sc.pid]) for sc in self.by_product.values()]
            return 200, {"data": {"products": {"items": items[v["skip"]:v["skip"] + v["take"]], "totalItems": len(items)}}}
        if op == "TierBreakdowns":
            vid = str(v["id"])
            if vid in self.breakdown_error:
                return 200, {"errors": [{"message": "boom"}]}
            sc = self.scenarios.get(vid)
            return 200, {"data": {"bulkPriceTierBreakdowns": sc.breakdowns() if sc else []}}
        if op == "PricedVariant":
            sc = self.scenarios.get(str(v["id"]))
            return 200, {"data": {"productVariant": sc.raw_variant() if sc else None}}
        if op == "ProductCertification":
            sc = self.by_product.get(str(v["productId"]))
            return 200, {"data": {"productCertification": sc.cert if sc else None}}
        if op == "ActiveChannelPricing":
            return 200, {"data": {"activeChannel": {"id": "2", "customFields": {"minimumPurchaseCny": self.min_purchase}}}}
        if op == "FiscalProfile":
            return 200, {"data": {"pricingFiscalProfile": {"certificateMaxSharePct": 20.0, "containerCbm": 30.0}}}
        raise AssertionError(f"operación inesperada contra Vendure: {op}")


# ─── El guardia de red: TODO lo que sale por httpx (sync o async) pasa por acá ─────────────────────────


class NetSpy:
    """Intercepta `send` de httpx y de httpx2 (el que usa el transporte de gql). Vendure (`example.invalid`) lo contesta
    `VendureWorld`; los hosts de Paco y cualquier otro host desconocido quedan registrados como violación y se
    contestan con un error; lo que viaja por un `MockTransport` propio del test (ML falso) se deja pasar y se registra."""

    PACO_HOSTS = ("paco.invalid", "paco-pro.invalid")

    def __init__(self, world: VendureWorld) -> None:
        self.world = world
        self.requests: list[dict[str, Any]] = []
        self.violations: list[dict[str, Any]] = []
        self.allowed_other_hosts: set[str] = {"testserver", "api.mercadolibre.com", "auth.mercadolibre.com"}

    def _record(self, request: Any) -> dict[str, Any]:
        body = request.content[:2000].decode("utf-8", "replace") if request.content else ""
        rec = {"method": request.method, "host": request.url.host, "path": request.url.path, "body": body}
        self.requests.append(rec)
        return rec

    def handle(self, mod: Any, request: Any) -> Any | None:
        rec = self._record(request)
        host = rec["host"]
        if host == VendureWorld.HOST:
            try:
                body = json.loads(rec["body"] or "{}")
            except ValueError:
                body = {}
            if request.method != "POST" or "query" not in body:
                self.world.log.append({"kind": "non-graphql", "op": "", "method": request.method, "query": rec["body"],
                                       "variables": {}})
                return mod.Response(500, json={"errors": [{"message": "no es una consulta"}]}, request=request)
            status, payload = self.world.respond(body, request.method)
            return mod.Response(status, json=payload, request=request)
        if host in self.PACO_HOSTS:
            self.violations.append(rec)
            return mod.Response(500, json={"error": "Paco prohibido en QA"}, request=request)
        if host in self.allowed_other_hosts:
            return None                                     # lo atiende el transporte (MockTransport / ASGI) del propio test
        self.violations.append(rec)
        return mod.Response(500, json={"error": f"host desconocido en QA: {host}"}, request=request)


def install_spy(monkeypatch, world: VendureWorld) -> NetSpy:
    """Instala el guardia sobre httpx, httpx2 y los sockets. Devuelve el espía."""
    import socket

    import httpx

    from app.vendure import client as vendure_client

    spy = NetSpy(world)
    mods = {httpx, vendure_client._gql_httpx}
    for mod in mods:
        orig_async, orig_sync = mod.AsyncClient.send, mod.Client.send

        def make(mod=mod, orig_async=orig_async, orig_sync=orig_sync):
            async def asend(self, request, **kw):
                handled = spy.handle(mod, request)
                return handled if handled is not None else await orig_async(self, request, **kw)

            def ssend(self, request, **kw):
                handled = spy.handle(mod, request)
                return handled if handled is not None else orig_sync(self, request, **kw)
            return asend, ssend

        a, s = make()
        monkeypatch.setattr(mod.AsyncClient, "send", a)
        monkeypatch.setattr(mod.Client, "send", s)

    real_connect = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost") and not str(host).startswith("/"):
            spy.violations.append({"method": "SOCKET", "host": str(host), "path": "", "body": ""})
            raise OSError(f"QA: conexión de red bloqueada a {host}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    # el bearer ya está "cacheado": no hay login
    from app.vendure.client import VendureClient

    monkeypatch.setattr(VendureClient, "_shared_bearer", "bearer-qa")
    monkeypatch.setattr(VendureClient, "_dims_supported", True)
    monkeypatch.setattr(VendureClient, "_review_supported", True)
    return spy


# ─── Catálogo de escenarios del diagnóstico ───────────────────────────────────────────────────────


def neighbors(dims=(30, 20, 15), kg=2.5):
    """Diez cajas de productos parecidos: ±10 % alrededor de `dims` / `kg` (determinístico)."""
    from app.pricing.price_diagnosis import Box

    f = [0.90, 0.95, 1.00, 1.00, 1.05, 1.10, 0.92, 1.08, 0.97, 1.03]
    return [Box(dims[0] * x, dims[1] * x, dims[2] * x, kg * x ** 3) for x in f]


def median_for(price_ok: int, margin: float) -> int:
    """La mediana de ML con la que un precio `price_ok` deja `margin` % de ganancia al revendedor."""
    return int(round(price_ok * (1 + margin / 100) / (1 - COMMISSION / 100)))


def snap_of(sc: Scenario, *, tier_label: str | None = None, median: int | None = None, shipping: int = 0):
    from types import SimpleNamespace

    return SimpleNamespace(
        product_id=sc.pid, variant_id=sc.variant, our_price_cents=sc.our_price, ml_median_cents=median or sc.median,
        commission_pct=COMMISSION, shipping_cents=shipping, tier_used=tier_label or f"tier:min_qty={sc.used_min_q}",
        our_specs=json.dumps(sc.specs()) if sc.specs() else None)


def call_diag(world: VendureWorld, sc: Scenario, neigh=None, **kw):
    """El diagnóstico REAL (cliente de Vendure + parser + reglas) de un escenario, contra el Vendure falso."""
    import asyncio

    from app.pricing import diagnosis_source as ds
    from app.vendure.client import VendureClient

    async def go():
        client = VendureClient()
        channel = await client.get_channel_pricing()
        return await ds.diagnose_product(
            snap_of(sc, **kw), client, yellow_min_pct=YELLOW, channel=channel, variant=None, neighbor_boxes_=neigh)

    return asyncio.run(go())


def ladder_tiers(costs: list[str], cbm_unit: str, kg_unit: str, *, cert_usd: str = "0", threshold: int = 500,
                 service_mode: str = "cbm") -> list[Tier]:
    """Tramos con las cantidades que da la regla de compra mínima de Vendure (escalera 1-5-10-25-50-100-250-500):
    el primero arranca en el primer escalón ≥ ¥500 / costo; cada siguiente, en el suyo si es mayor que el anterior o
    en el escalón siguiente. La cantidad de cada desglose es la mínima del tramo."""
    ladder = (1, 5, 10, 25, 50, 100, 250, 500)

    def up(n: float) -> int:
        return next((r for r in ladder if n <= r), int(-(-n // 1)))

    starts: list[int] = []
    prev = 0
    for c in costs:
        cost = D(c)
        derived = 1 if cost >= threshold else up(float(-(-D(threshold) // cost)))
        start = derived if derived > prev else up(prev + 1)
        starts.append(start)
        prev = start
    out = []
    for i, (c, s) in enumerate(zip(costs, starts)):
        mx = starts[i + 1] - 1 if i + 1 < len(starts) else None
        out.append(Tier(s, mx, c, s, cbm_unit, kg_unit, cert_usd=cert_usd, service_mode=service_mode))
    return out


OK_COSTS = ["60", "55", "50"]
OK_BOX = (30.0, 20.0, 15.0, 2.5)           # caja normal de la familia: 9.000 cm³ = 0,009 m³, 2,5 kg
OK_PRODUCT = (25.0, 15.0, 10.0, 2.0)
BIG_BOX = (40.0, 30.0, 20.0, 6.0)          # la misma caja mal cargada: 24.000 cm³ = 0,024 m³ (2,7 veces)


def healthy(pid: str = "0", costs=None, box=OK_BOX, product=OK_PRODUCT, cert: str = "0", cbm: str | None = None,
            kg: str | None = None, **kw) -> Scenario:
    """Un producto bien armado: tramos de la escalera, caja normal, sin recargos."""
    cbm = cbm or str(box[0] * box[1] * box[2] / 1_000_000)
    kg = kg or str(box[3])
    tiers = ladder_tiers(costs or OK_COSTS, cbm, kg, cert_usd=cert)
    return Scenario(pid, f"v{pid}", tiers, tiers[0].min_q, kw.pop("median", 0), box=box, product=product,
                    name=kw.pop("name", f"Producto {pid}"), **kw)
