"""Prechequeo de Luis (POST/GET /radar/precheck, pricing/radar_precheck.py) y el factor de precio estimado
(pricing/price_factor.py)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import Session, select

from app import runtime
from app.clock import utcnow
from app.config import get_settings
from app.db.models import (
    MlWebClaim,
    MlWebResult,
    PriceFactorSample,
    PriceReviewItem,
    RadarPrecheck,
    Setting,
)
from app.db.session import engine, init_db
from app.pricing import oficina_ml, price_factor, radar_precheck
from app.pricing.market_ml import MlCandidate

LUIS_KEY = "k" * 40
AUTH = {"X-API-Key": LUIS_KEY}
IMG = "https://cbu01.alicdn.com/img/ibank/abc.jpg"


def _clean() -> None:
    init_db()
    with Session(engine) as s:
        for model in (RadarPrecheck, PriceFactorSample, MlWebResult, MlWebClaim):
            s.execute(delete(model))
        for key in (radar_precheck.COUNTER_KEY, radar_precheck.APIFY_COUNTER_KEY, radar_precheck.VENDURE_COUNTER_KEY,
                    "radar_precheck_vendure_daily_max", "radar_precheck_enabled",
                    "radar_precheck_daily_max", "radar_precheck_queue_share_pct", "radar_precheck_apify_daily_max"):
            row = s.get(Setting, key)
            if row is not None:
                s.delete(row)
        s.commit()
    runtime.invalidate()


@pytest.fixture
def client(monkeypatch):
    _clean()
    settings = get_settings()
    monkeypatch.setattr(settings, "hugo_api_keys", f"luis:{LUIS_KEY}", raising=False)
    monkeypatch.setattr(settings, "hugo_api_key", "", raising=False)
    from app import main
    yield TestClient(main.app)
    _clean()


def item(ref: str = "L1", **over):
    base = {"ref": ref, "titulo_es": "Organizador de cocina giratorio", "titulo_zh": "旋转厨房收纳架", "imagen_url": IMG,
            "precio_cny_moq": 25.0, "moq": 50, "peso_kg": 0.6, "medidas_cm": [20, 20, 30], "categoria": "Cocina"}
    base.update(over)
    return base


# ─── API ──────────────────────────────────────────────────────────


def test_needs_the_api_key_and_is_off_by_default(client):
    assert client.post("/radar/precheck", json={"items": [item()]}).status_code == 401
    r = client.post("/radar/precheck", json={"items": [item()]}, headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"items": [{"ref": "L1", "estado": "error", "motivo": "apagado"}]}


def test_validation_max_50_no_dupes_no_extra_fields(client):
    runtime.set_value("radar_precheck_enabled", 1)
    many = [item(f"r{i}") for i in range(51)]
    assert client.post("/radar/precheck", json={"items": many}, headers=AUTH).status_code == 422
    assert client.post("/radar/precheck", json={"items": [item(), item()]}, headers=AUTH).status_code == 422
    assert client.post("/radar/precheck", json={"items": [item(costo=1)]}, headers=AUTH).status_code == 422
    assert client.post("/radar/precheck", json={"items": [item(ref="a b")]}, headers=AUTH).status_code == 422
    assert client.post("/radar/precheck", json={"items": [item(medidas_cm=[1, 2])]}, headers=AUTH).status_code == 422
    assert client.get("/radar/precheck", params={"refs": "a b"}, headers=AUTH).status_code == 422


def test_submit_queues_and_get_reports_pending_then_listo(client, monkeypatch):
    runtime.set_value("radar_precheck_enabled", 1)
    r = client.post("/radar/precheck", json={"items": [item(), item("L2", imagen_url="http://127.0.0.1/x.jpg")]},
                    headers=AUTH)
    out = r.json()["items"]
    assert out[0] == {"ref": "L1", "estado": "pendiente"}
    assert out[1]["estado"] == "error"                       # la foto tiene que ser https pública
    # El job nocturno de Apify NO lo toma (gastaría su cupo); la Mac de la oficina sí, de respaldo.
    assert all(not q["product_id"].startswith("pc-") for q in oficina_ml.build_queue(10, claimer="apify-x"))
    queue = oficina_ml.build_queue(10, claimer=oficina_ml.CLAIM_OFICINA)
    assert queue and queue[0]["product_id"].startswith("pc-") and queue[0]["queries"] == ["Organizador de cocina giratorio"]
    pid = queue[0]["product_id"]
    # Llega el resultado (la Mac o Apify): ingest lo acepta aunque no sea un producto de Vendure.
    cands = [
        {"id": "MLA111", "name": "Organizador giratorio cocina", "image_urls": ["https://http2.mlstatic.com/D_1.jpg"],
         "permalink": "https://articulo.mercadolibre.com.ar/MLA-111-x", "price_cents": 2_000_000, "currency": "ARS",
         "seller": "A", "sold_quantity": 500},
        {"id": "MLA222", "name": "Organizador cocina giratorio 2 pisos", "image_urls": ["https://http2.mlstatic.com/D_2.jpg"],
         "permalink": "https://articulo.mercadolibre.com.ar/MLA-222-x", "price_cents": 2_200_000, "currency": "ARS",
         "seller": "B", "sold_quantity": 1000},
        {"id": "MLA333", "name": "Lámpara", "image_urls": ["https://http2.mlstatic.com/D_3.jpg"],
         "permalink": "https://articulo.mercadolibre.com.ar/MLA-333-x", "price_cents": 100_000, "currency": "ARS"},
    ]
    rep = oficina_ml.ingest([{"product_id": pid, "query": "Organizador de cocina giratorio", "status": "ok",
                              "fetched_at": utcnow().isoformat() + "Z", "candidates": cands}])
    assert rep.stored == 1, rep.as_dict()
    assert pid not in {q["product_id"] for q in oficina_ml.build_queue(10, claimer=oficina_ml.CLAIM_OFICINA)}

    # Factor: 20 desgloses reales con factura/fob = 3 y cambio 200 ARS por CNY (0,14 USD × 1428,57).
    price_factor.upsert([PriceFactorSample(product_id=f"p{i}", tier_id="t", cny_unit=10.0, factura_unit_cents=600_000,
                                           fob_usd=100.0, factura_usd=300.0, total_cbm=1.0, cny_to_usd=0.14,
                                           usd_to_channel=1428.5714) for i in range(20)])

    async def scorer(our, urls):
        return 0.95 if "D_3" not in urls[0] else 0.1

    import asyncio
    async def vendure_down(_inp):
        raise RuntimeError("Vendure caído")

    async def no_neighbors(_url):
        return []

    counts = asyncio.run(radar_precheck.process_pending(["L1"], scorer=scorer, estimator=vendure_down,
                                                        neighbors=no_neighbors))
    assert counts == {"listo": 1}
    got = client.get("/radar/precheck", params={"refs": "L1,nada"}, headers=AUTH).json()["items"]
    assert got[1]["estado"] == "error"
    v = got[0]
    assert v["estado"] == "listo" and v["estimado"] is True
    assert v["ml"] == {"mediana_ars": 21_000.0, "n_identicos": 2, "vendidos_total": 1500, "publicaciones": 3}
    assert v["precio_app_estimado_ars"] == pytest.approx(25 * 3 * 200, rel=1e-3)     # 15.000
    assert v["color"] == "amarillo"                              # (21000·0,87 − 15000) / 15000 ≈ 21,8 %
    assert v["precio_max_ars"] == pytest.approx(21_000 * 0.87 / 1.10, rel=1e-3)
    assert v["fuente_precio"] == "factor" and v["factor"]["vendure"]["vendure_status"] == "error RuntimeError"
    assert v["factor"]["fuente"] == "global" and v["factor"]["n"] == 20 and v["factor"]["mediana_factura_fob"] == 3.0
    assert v["medido_at"]


def test_daily_cap_and_same_item_is_not_requeued(client):
    runtime.set_value("radar_precheck_enabled", 1)
    runtime.set_value("radar_precheck_daily_max", 1)
    a = client.post("/radar/precheck", json={"items": [item("A")]}, headers=AUTH).json()["items"][0]
    again = client.post("/radar/precheck", json={"items": [item("A")]}, headers=AUTH).json()["items"][0]
    b = client.post("/radar/precheck", json={"items": [item("B")]}, headers=AUTH).json()["items"][0]
    assert a["estado"] == "pendiente" and again["estado"] == "pendiente"
    assert b == {"ref": "B", "estado": "error", "motivo": "tope diario"}


def test_queue_share_zero_or_disabled_leaves_the_queue_as_before(client):
    runtime.set_value("radar_precheck_enabled", 1)
    client.post("/radar/precheck", json={"items": [item("Q")]}, headers=AUTH)
    mac = oficina_ml.CLAIM_OFICINA
    assert any(q["product_id"].startswith("pc-") for q in oficina_ml.build_queue(10, claimer=mac))
    runtime.set_value("radar_precheck_queue_share_pct", 0)
    assert all(not q["product_id"].startswith("pc-") for q in oficina_ml.build_queue(10, claimer=mac))
    runtime.set_value("radar_precheck_queue_share_pct", 30)
    runtime.set_value("radar_precheck_enabled", 0)
    assert all(not q["product_id"].startswith("pc-") for q in oficina_ml.build_queue(10, claimer=mac))


def test_unknown_pc_ids_are_still_rejected_by_ingest(client):
    rep = oficina_ml.ingest([{"product_id": "pc-999999", "query": "x", "status": "empty",
                              "fetched_at": utcnow().isoformat() + "Z"}])
    assert rep.stored == 0 and rep.rejected[0][1] == "producto desconocido"


# ─── verdict (puro) ───────────────────────────────────────────────


def _c(i: int, price: int | None, sold: int | None = None) -> MlCandidate:
    return MlCandidate(id=f"MLA{i}", name="x", price_cents=price, currency="ARS", sold_quantity=sold)


def test_verdict_colors_and_motives():
    kw = dict(commission_pct=13.0, shipping_cents=0, green_min=30.0, yellow_min=10.0, min_listings=3)
    green = radar_precheck.verdict(matches=[_c(1, 3_000_000), _c(2, 3_000_000), _c(3, 3_000_000, 10)], publicaciones=5,
                                   precio_app_cents=1_500_000, **kw)
    assert green["color"] == "verde" and green["vendidos_total"] == 10 and "pocos" not in green["motivo"]
    red = radar_precheck.verdict(matches=[_c(1, 1_000_000)], publicaciones=1, precio_app_cents=1_500_000, **kw)
    assert red["color"] == "rojo" and "pocos" in red["motivo"]
    none = radar_precheck.verdict(matches=[], publicaciones=4, precio_app_cents=1_500_000, **kw)
    assert none["color"] == "sin_dato" and none["ml_median_cents"] is None and none["vendidos_total"] is None
    no_price = radar_precheck.verdict(matches=[_c(1, 1_000_000)], publicaciones=1, precio_app_cents=None, **kw)
    assert no_price["color"] == "sin_dato" and "desgloses" in no_price["motivo"]


# ─── factor (puro) ────────────────────────────────────────────────


def _s(ratio: float, *, cat: str | None = None, vm3: float | None = None, fx: float = 200.0, days: int = 0):
    return price_factor.Sample(ratio, fx, vm3, cat, datetime(2026, 11, 1) - timedelta(days=days), 0.14)


def test_factor_uses_category_only_with_20_cases_and_m3_band_only_with_20_cases():
    samples = [_s(2.0) for _ in range(30)] + [_s(4.0, cat="cocina") for _ in range(19)]
    f = price_factor.compute(samples, cny_unit=10.0, category="Cocina")
    assert f.how["fuente"] == "global" and f.how["categoria_casos"] == 19
    samples.append(_s(4.0, cat="cocina"))
    f = price_factor.compute(samples, cny_unit=10.0, category="  COCINA ")
    assert f.how["fuente"] == "categoria" and f.ratio == 4.0 and f.how["n"] == 20
    # Valor por m³ del candidato: 10 CNY × 0,14 / 0,001 m³ = 1.400 USD/m³.
    dense = [_s(1.5, vm3=1400.0) for _ in range(20)] + [_s(6.0, vm3=50.0) for _ in range(20)]
    f = price_factor.compute(dense, cny_unit=10.0, medidas_cm=[10, 10, 10])
    assert f.how["fuente"] == "global+m3" and f.ratio == 1.5 and f.how["m3_casos"] == 20
    assert price_factor.estimate_cents(f, 10.0) == round(10 * 1.5 * 200 * 100)
    empty = price_factor.compute([], cny_unit=10.0)
    assert empty.ratio is None and price_factor.estimate_cents(empty, 10.0) is None


def test_factor_samples_from_the_tray_breakdowns():
    _clean()
    tier = {"tier_id": "T1", "usd_available": True, "cny_unit": 12.0, "cny_to_usd": 0.14, "usd_to_channel": 1400.0,
            "total_cbm": 0.5, "factura_usd": 450.0, "factura_unit_cents": 900_000,
            "components": [{"key": "fabrica", "usd": 150.0}, {"key": "flete", "usd": 100.0}]}
    with Session(engine) as s:
        s.add(PriceReviewItem(product_id="fx1", breakdown=json.dumps({"tier": tier, "tiers": []})))
        s.add(PriceReviewItem(product_id="fx2", breakdown=json.dumps({"tier": {"usd_available": False}})))
        s.commit()
    try:
        assert price_factor.refresh_from_review_items() == 1
        assert price_factor.refresh_from_review_items() == 1          # idempotente: reemplaza, no duplica
        [sample] = price_factor.load_samples()
        assert sample.ratio == 3.0 and sample.fx == pytest.approx(196.0) and sample.value_m3_usd == 300.0
    finally:
        with Session(engine) as s:
            s.execute(delete(PriceReviewItem).where(PriceReviewItem.product_id.in_(["fx1", "fx2"])))  # type: ignore[attr-defined]
            s.execute(delete(PriceFactorSample))
            s.commit()
        _clean()



# ─── búsqueda con Apify (camino vigente) ──────────────────────────


async def test_search_pending_uses_apify_with_its_own_cap_and_records_spend(client, monkeypatch):
    from app.db.models import ApifySpend
    from app.pricing import apify_ml_search as ams

    runtime.set_value("radar_precheck_enabled", 1)
    settings = get_settings()
    monkeypatch.setattr(settings, "apify_token", "t" * 30, raising=False)
    monkeypatch.setattr(settings, "apify_meli_search_actor", "devcake/mercadolibre-scraper", raising=False)
    monkeypatch.setattr(settings, "apify_meli_search_build", "", raising=False)
    monkeypatch.setattr(settings, "apify_meli_search_concurrency", 4, raising=False)
    for ref in ("A1", "A2", "A3"):
        radar_precheck.submit([item(ref)], "luis")
    runtime.set_value("radar_precheck_apify_daily_max", 2)
    with Session(engine) as s:
        s.execute(delete(ApifySpend).where(ApifySpend.label == radar_precheck.SPEND_LABEL))
        row = s.get(Setting, radar_precheck.APIFY_COUNTER_KEY)
        if row is not None:
            s.delete(row)
        s.commit()

    async def permission(*_a, **_k):
        return ams._REQUIRED_PERMISSION

    searched: list[str] = []

    async def search_one(http, pid, query, **_kw):
        searched.append(pid)
        return ams.SearchOutcome(pid, query, "ok", candidates=[{
            "id": "MLA111", "name": "Organizador giratorio", "image_urls": ["https://http2.mlstatic.com/D_1.jpg"],
            "permalink": "https://articulo.mercadolibre.com.ar/MLA-111-x", "price_cents": 2_000_000, "currency": "ARS",
            "seller": "A", "sold_quantity": 250}], raw_items=1, run_id=f"run{len(searched)}", usd_imputado=0.02,
            usage_usd=0.018)

    monkeypatch.setattr(ams, "_actor_permission", permission)
    monkeypatch.setattr(ams, "search_one", search_one)

    class Http:
        async def aclose(self):
            return None

    counts = await radar_precheck.search_pending(http=Http())
    assert counts == {"ok": 2, "tope": 1} and len(searched) == 2
    with Session(engine) as s:
        spend = s.exec(select(ApifySpend).where(ApifySpend.label == radar_precheck.SPEND_LABEL)).all()
        stored = s.exec(select(MlWebResult).where(MlWebResult.product_id.in_(searched))).all()  # type: ignore[attr-defined]
        claims = s.exec(select(MlWebClaim)).all()
    assert [round(x.usd, 3) for x in spend] == [0.018, 0.018] and len(stored) == 2 and claims == []
    assert all(json.loads(r.candidates)[0]["sold_quantity"] == 250 for r in stored)   # los «vendidos» quedan
    # Lo ya buscado no se vuelve a buscar; sin cupo, el que falta queda pendiente.
    assert await radar_precheck.search_pending(http=Http()) == {"tope": 1}


async def test_search_pending_needs_the_switch_the_token_and_limited_permissions(client, monkeypatch):
    from app.pricing import apify_ml_search as ams

    assert await radar_precheck.search_pending() == {"apagado": 1}
    runtime.set_value("radar_precheck_enabled", 1)
    monkeypatch.setattr(get_settings(), "apify_token", "", raising=False)
    assert await radar_precheck.search_pending() == {"sin_apify": 1}
    monkeypatch.setattr(get_settings(), "apify_token", "t" * 30, raising=False)
    monkeypatch.setattr(get_settings(), "apify_meli_search_actor", "devcake/mercadolibre-scraper", raising=False)
    monkeypatch.setattr(get_settings(), "apify_meli_search_build", "", raising=False)
    radar_precheck.submit([item("P1")], "luis")

    async def full(*_a, **_k):
        return "FULL_PERMISSIONS"

    async def never(*_a, **_k):
        raise AssertionError("no tiene que buscar")

    monkeypatch.setattr(ams, "_actor_permission", full)
    monkeypatch.setattr(ams, "search_one", never)

    class Http:
        async def aclose(self):
            return None

    assert await radar_precheck.search_pending(http=Http()) == {"actor_sin_permisos_limitados": 1}


# ─── estimateImportPrice de Vendure (camino principal) ────────────

# Respuesta grabada con la forma exacta del contrato (`importPriceEstimateTypes`, PR b2box#1115; ver test_qa_estimate.py).
RECORDED_OK = {
    "status": "OK", "statusDetail": None, "channelCurrencyCode": "ARS",
    "scenarios": [{"label": "mid", "purchasePriceCny": 25.0, "boxQuantile": 0.5, "cbmPerUnit": 0.012, "kgPerUnit": 0.6,
                   "prices": [{"quantity": 50, "netChannelPerUnit": 1_400_000, "invoiceChannelPerUnit": 1_694_000}]}],
    "boxSource": "INPUT", "boxSampleSize": 0, "referenceProductCount": 3, "taxClassificationCode": "9617.00.10",
    "taxClassificationShare": 0.67, "isImoCargo": False, "isImoCargoShare": 0.0, "goodsKind": "resale",
    "salesVatPct": 21.0, "profileVersion": 4, "computedAt": "2026-11-08T12:00:00Z",
}
RECORDED_NOT_READY = {"status": "PRICING_CONFIG_NOT_READY", "statusDetail": "MISSING_CNY_TO_USD_RATE",
                      "channelCurrencyCode": "ARS", "scenarios": [], "boxSource": "NONE", "boxSampleSize": 0,
                      "referenceProductCount": 0, "isImoCargo": False, "goodsKind": "resale", "salesVatPct": 21.0,
                      "computedAt": "2026-11-08T12:00:00Z"}


def test_estimate_input_matches_vendures_sdl():
    from graphql import build_schema, coerce_input_value

    from tests.test_qa_estimate import VENDURE_SDL

    schema = build_schema(VENDURE_SDL)
    full = radar_precheck.build_estimate_input(25.0, 50, 0.6, [20, 20, 30], ["7", "8"])
    assert full == {"quantities": [50], "referenceProductIds": ["7", "8"],
                    "scenarios": [{"label": "mid", "purchasePriceCny": 25.0, "cbmPerUnit": 0.012, "kgPerUnit": 0.6}]}
    coerce_input_value(full, schema.get_type("EstimateImportPriceInput"))
    no_dims = radar_precheck.build_estimate_input(25.0, 50, None, [20, 20, 30], [])
    assert no_dims["scenarios"][0] == {"label": "mid", "purchasePriceCny": 25.0, "boxQuantile": 0.5}
    coerce_input_value(no_dims, schema.get_type("EstimateImportPriceInput"))


def test_read_estimate_takes_the_invoice_at_the_moq_or_nothing():
    cents, how = radar_precheck.read_estimate(RECORDED_OK, 50)
    assert cents == 1_694_000 and how["ncm"] == "9617.00.10" and how["medidas"] == "unidad"
    assert radar_precheck.read_estimate(RECORDED_OK, 200)[0] is None             # otra cantidad: no se adivina
    assert radar_precheck.read_estimate(RECORDED_NOT_READY, 50) == (None, {"vendure_status": "PRICING_CONFIG_NOT_READY"})
    assert radar_precheck.read_estimate({**RECORDED_OK, "channelCurrencyCode": "USD"}, 50)[0] is None
    assert radar_precheck.read_estimate(None, 50)[0] is None


def _ready_row(client, ref: str) -> None:
    runtime.set_value("radar_precheck_enabled", 1)
    radar_precheck.submit([item(ref)], "luis")
    with Session(engine) as s:
        row = s.exec(select(RadarPrecheck).where(RadarPrecheck.ref == ref)).one()
        pid = radar_precheck.queue_id(int(row.id))
    cands = [{"id": f"MLA{i}", "name": "Organizador giratorio cocina", "image_urls": [f"https://http2.mlstatic.com/D_{i}.jpg"],
              "permalink": f"https://articulo.mercadolibre.com.ar/MLA-{i}-x", "price_cents": 3_000_000, "currency": "ARS",
              "seller": f"S{i}", "sold_quantity": 100} for i in range(1, 4)]
    oficina_ml.ingest([{"product_id": pid, "query": "x", "status": "ok", "fetched_at": utcnow().isoformat() + "Z",
                        "candidates": cands}])


async def _match(_our, _urls):
    return 0.95


def test_vendure_estimate_is_the_main_path(client):
    import asyncio

    _ready_row(client, "V1")
    seen: list[dict] = []

    async def estimator(inp):
        seen.append(inp)
        return RECORDED_OK

    async def neighbors(url):
        assert url == IMG
        return ["7", "8", "9"]

    assert asyncio.run(radar_precheck.process_pending(["V1"], scorer=_match, estimator=estimator,
                                                      neighbors=neighbors)) == {"listo": 1}
    assert seen[0]["quantities"] == [50] and seen[0]["referenceProductIds"] == ["7", "8", "9"]
    assert seen[0]["scenarios"][0]["purchasePriceCny"] == 25.0 and seen[0]["scenarios"][0]["kgPerUnit"] == 0.6
    [v] = client.get("/radar/precheck", params={"refs": "V1"}, headers=AUTH).json()["items"]
    assert v["fuente_precio"] == "vendure_estimate" and v["precio_app_estimado_ars"] == 16_940.0
    assert v["factor"]["fuente_precio"] == "vendure_estimate" and v["factor"]["referencias_enviadas"] == 3
    assert v["color"] == "verde"                    # (30000·0,87 − 16940) / 16940 ≈ 54 %
    assert v["estimado"] is True


def test_vendure_not_ready_or_over_cap_falls_back_to_the_factor(client):
    import asyncio

    price_factor.upsert([PriceFactorSample(product_id=f"q{i}", tier_id="t", cny_unit=10.0, factura_unit_cents=600_000,
                                           fob_usd=100.0, factura_usd=300.0, cny_to_usd=0.14, usd_to_channel=1428.5714)
                         for i in range(20)])

    async def not_ready(_inp):
        return RECORDED_NOT_READY

    async def neighbors(_url):
        return []

    _ready_row(client, "F1")
    asyncio.run(radar_precheck.process_pending(["F1"], scorer=_match, estimator=not_ready, neighbors=neighbors))
    [v] = client.get("/radar/precheck", params={"refs": "F1"}, headers=AUTH).json()["items"]
    assert v["fuente_precio"] == "factor" and v["precio_app_estimado_ars"] == pytest.approx(15_000, rel=1e-3)
    assert v["factor"]["vendure"]["vendure_status"] == "PRICING_CONFIG_NOT_READY"

    runtime.set_value("radar_precheck_vendure_daily_max", 0)

    async def never(_inp):
        raise AssertionError("pasado el tope no se consulta a Vendure")

    _ready_row(client, "F2")
    asyncio.run(radar_precheck.process_pending(["F2"], scorer=_match, estimator=never, neighbors=neighbors))
    [v] = client.get("/radar/precheck", params={"refs": "F2"}, headers=AUTH).json()["items"]
    assert v["fuente_precio"] == "factor" and v["factor"]["vendure"]["vendure_status"] == "tope diario"
