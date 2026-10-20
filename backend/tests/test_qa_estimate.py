"""QA independiente del estimado por foto (cotizador rápido, parte 3).

Completa `test_estimate_lookup.py` (reusa su fixture `env`: extract, Apify,
Qwen, OTAPI y Vendure mockeados, sin red) con lo que ahí no estaba cubierto:

* contrato EXACTO con `estimateImportPrice` de Vendure (PR b2box#1115): la query
  y las variables de Hugo se validan contra una copia del SDL real, y se
  ejecutan con los enums tal como viajan (`NEIGHBORS`, `MISSING_…`);
* ER-02 con el deadline real de 50 s y todo colgado (Apify real con el HTTP
  colgado; miniaturas, Qwen y Vendure colgados), sin tareas sueltas después;
* ER-04 al centavo; ER-07 ráfaga de 2/min, base caída, key de confianza por
  HTTP; ER-08 en los cuatro caminos (ML, alibaba.com, 1688 y foto subida);
* casi-match ("¿es este?") con estimado pedido.

Los tests de 50 s terminan en `_50s` (para deseleccionarlos con -k).
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from graphql import build_schema, coerce_input_value, graphql_sync, parse, validate
from sqlalchemy.exc import OperationalError

from app import security
from app.api import app_routes
from app.api.app_routes import AppLookupRequest, AppLookupVisitor, _lookup
from app.config import Settings, get_settings
from app.dedup import vision_rerank
from app.estimate import images, limits, otapi_item, pricing
from app.estimate import service as est_service
from app.estimate import vendure_estimate
from app.ingest import apify_1688
from app.ingest.apify_1688 import Search1688Result
from app.vendure.client import VendureProduct
from tests.test_estimate_lookup import (  # noqa: F401  (env es fixture)
    ALIBABA_PHOTO,
    ALIBABA_URL,
    CANDS,
    ML_URL,
    OFFER_URL,
    RUNTIME,
    VISITOR_IP,
    FakeLLM,
    FakeVendure,
    _ask,
    _cand,
    _request,
    _rows,
    _verdicts,
    env,
)

# ─── Contrato con Vendure ──────────────────────────────────────────

# Copia literal de `importPriceEstimateTypes` y del campo de Query de
# packages/backend-core/src/plugins/advanced-pricing/api/api-extensions.ts
# (b2box, PR #1115, commit 4b16cb234). Si Vendure cambia el contrato, este
# bloque se actualiza A MANO junto con la query de Hugo: es el punto del test.
VENDURE_SDL = '''
scalar Money
scalar DateTime

input EstimateImportScenarioInput {
  label: String!
  purchasePriceCny: Float!
  cbmPerUnit: Float
  kgPerUnit: Float
  boxQuantile: Float
}

input EstimateImportPriceInput {
  quantities: [Int!]!
  referenceProductIds: [ID!]
  scenarios: [EstimateImportScenarioInput!]!
}

enum ImportPriceEstimateStatus {
  OK
  PRICING_CONFIG_NOT_READY
  NO_BOX_DATA
}

enum ImportPriceEstimateStatusDetail {
  MISSING_PRICING_PROFILE
  INVALID_PRICING_PROFILE
  MISSING_LOGISTIC_COSTS
  MISSING_USD_TO_CHANNEL_RATE
  MISSING_CNY_TO_USD_RATE
  INVALID_CHANNEL_CONFIGURATION
}

enum ImportPriceEstimateBoxSource {
  INPUT
  NEIGHBORS
  COLLECTION
  GLOBAL
  NONE
}

type ImportPriceEstimatePrice {
  quantity: Int!
  netChannelPerUnit: Money!
  invoiceChannelPerUnit: Money!
}

type ImportPriceEstimateScenario {
  label: String!
  purchasePriceCny: Float!
  boxQuantile: Float!
  cbmPerUnit: Float!
  kgPerUnit: Float!
  prices: [ImportPriceEstimatePrice!]!
}

type ImportPriceEstimate {
  status: ImportPriceEstimateStatus!
  statusDetail: ImportPriceEstimateStatusDetail
  channelCurrencyCode: String!
  scenarios: [ImportPriceEstimateScenario!]!
  boxSource: ImportPriceEstimateBoxSource!
  boxSampleSize: Int!
  referenceProductCount: Int!
  taxClassificationCode: String
  taxClassificationShare: Float
  containsBatteries: Boolean!
  containsBatteriesShare: Float
  goodsKind: String!
  salesVatPct: Float!
  profileVersion: Int
  computedAt: DateTime!
}

type Query {
  estimateImportPrice(input: EstimateImportPriceInput!): ImportPriceEstimate!
}
'''


def _hugo_query() -> str:
    """La query tal cual la manda `VendureClient.estimate_import_price`."""
    seen: dict[str, object] = {}

    async def capture(self, query, variables, *, what, max_attempts):  # noqa: ARG001
        seen["query"], seen["variables"] = query, variables
        return {"estimateImportPrice": None}

    client = vendure_estimate.VendureClient.__new__(vendure_estimate.VendureClient)
    client._execute_with_retry = capture.__get__(client)  # type: ignore[attr-defined]
    asyncio.run(client.estimate_import_price({"x": 1}))
    from graphql import print_ast
    doc = getattr(seen["query"], "document", seen["query"])   # gql 4: GraphQLRequest
    return print_ast(doc)


def _vendure_answer(inp: dict, *, box_source: str = "NEIGHBORS") -> dict:
    return {
        "status": "OK", "statusDetail": None, "channelCurrencyCode": "ARS",
        "scenarios": [{
            "label": s["label"], "purchasePriceCny": s["purchasePriceCny"],
            "boxQuantile": s["boxQuantile"], "cbmPerUnit": 0.002, "kgPerUnit": 0.3,
            "prices": [{"quantity": q, "netChannelPerUnit": 1_000_000 * (i + 1),
                        "invoiceChannelPerUnit": 1_210_000 * (i + 1)} for q in inp["quantities"]],
        } for i, s in enumerate(inp["scenarios"])],
        "boxSource": box_source, "boxSampleSize": 8, "referenceProductCount": 2,
        "taxClassificationCode": "9617.00.10", "taxClassificationShare": 0.6,
        "containsBatteries": False, "containsBatteriesShare": 0.0, "goodsKind": "resale",
        "salesVatPct": 21.0, "profileVersion": 4, "computedAt": "2026-10-20T12:00:00Z",
    }


def test_contrato_la_query_de_hugo_valida_contra_el_sdl_de_vendure():
    schema = build_schema(VENDURE_SDL)
    query = _hugo_query()
    assert validate(schema, parse(query)) == []
    inp = vendure_estimate.build_input([50, 200, 1000], pricing.Scenarios(16.5, 17.6, 18.7),
                                       ["100", "101"])
    coerced = coerce_input_value(inp, schema.get_type("EstimateImportPriceInput"))
    assert coerced["quantities"] == [50, 200, 1000]
    assert [s["label"] for s in coerced["scenarios"]] == ["low", "mid", "high"]


def test_contrato_ejecuta_con_los_enums_reales_y_money_en_centavos():
    schema = build_schema(VENDURE_SDL)
    query = _hugo_query()
    inp = vendure_estimate.build_input([50, 200], pricing.Scenarios(10.0, 12.0, 14.0), ["7"])
    res = graphql_sync(schema, query, root_value={"estimateImportPrice": _vendure_answer(inp)},
                       variable_values={"input": inp})
    assert res.errors is None
    est = vendure_estimate.parse(res.data["estimateImportPrice"], inp["quantities"])
    assert est.status == "OK" and est.currency == "ARS"
    # Money = centavos, enteros, tal cual: nada de ×100 ni /100.
    assert est.invoice_by_quantity(50) == {"low": 1_210_000, "mid": 2_420_000, "high": 3_630_000}
    # statusDetail ahora es enum: un PRICING_CONFIG_NOT_READY con detalle se lee bien.
    not_ready = dict(_vendure_answer(inp), status="PRICING_CONFIG_NOT_READY",
                     statusDetail="MISSING_USD_TO_CHANNEL_RATE", scenarios=[], boxSource="NONE")
    res = graphql_sync(schema, query, root_value={"estimateImportPrice": not_ready},
                       variable_values={"input": inp})
    assert res.errors is None
    assert vendure_estimate.parse(res.data["estimateImportPrice"], [50, 200]).status == \
        "PRICING_CONFIG_NOT_READY"


def test_contrato_input_dentro_de_los_limites_de_vendure():
    """Vendure tira UserInputError con cantidades repetidas, > 3 cantidades,
    > 20 referencias o > 5 escenarios: Hugo nunca tiene que mandarlos."""
    inp = vendure_estimate.build_input([50, 200, 1000],
                                       pricing.Scenarios(1.0, 2.0, 3.0),
                                       [str(i) for i in range(est_service._NEIGHBORS)])
    assert len(set(inp["quantities"])) == len(inp["quantities"]) <= 3
    assert len(inp["referenceProductIds"]) <= 20 and len(inp["scenarios"]) <= 5


async def test_tramos_repetidos_no_viajan_repetidos_a_vendure(env, monkeypatch):
    monkeypatch.setitem(RUNTIME, "estimate_tier_qty_2", 50)
    await _ask()
    q = FakeVendure.calls[0]["quantities"]
    assert len(q) == len(set(q))


async def test_box_source_neighbors_como_lo_manda_vendure_no_ensancha(env):
    """Vendure manda el enum en MAYÚSCULAS (`NEIGHBORS`). Con la caja de los
    vecinos el margen es el base (15), no 15 + 5."""
    FakeVendure.box_source = "NEIGHBORS"
    assert (await _ask()).estimate_status == "ok"
    assert _rows()[-1].margin_pct == 15.0


async def test_box_source_collection_en_mayusculas_si_ensancha(env):
    FakeVendure.box_source = "COLLECTION"
    await _ask()
    assert _rows()[-1].margin_pct == 20.0


async def test_status_detail_enum_no_llega_a_la_respuesta_ni_a_la_base(env, monkeypatch):
    class NotReady(FakeVendure):
        async def estimate_import_price(self, payload):
            FakeVendure.calls.append(payload)
            return {"status": "PRICING_CONFIG_NOT_READY",
                    "statusDetail": "MISSING_USD_TO_CHANNEL_RATE"}
    monkeypatch.setattr(vendure_estimate, "VendureClient", NotReady)
    resp = await _ask()
    assert resp.estimate_status == "error" and resp.estimate is None
    assert "MISSING_USD" not in resp.model_dump_json()
    row = _rows()[-1]
    assert "MISSING_USD" not in json.dumps(row.model_dump(), default=str)


# ─── ER-04: mismo motor, al centavo ────────────────────────────────


async def test_er04_rango_coincide_al_centavo_con_vendure_antes_del_redondeo(env, monkeypatch):
    monkeypatch.setitem(RUNTIME, "estimate_margin_pct", 0.0)
    FakeVendure.box_source = "neighbors"
    env.candidates = [_cand(0, 15.0), _cand(1, 16.0), _cand(2, 20.0)]
    env.llm = FakeLLM(_verdicts([90, 90, 90]))
    resp = await _ask()
    assert resp.estimate_status == "ok"
    sent = FakeVendure.calls[0]
    # Lo que devolvió Vendure, recalculado igual que FakeVendure.
    for tier in resp.estimate.tiers:
        invoices = []
        for sc in sent["scenarios"]:
            net = round(sc["purchasePriceCny"] * 30000 * (1 + 50 / tier.quantity))
            invoices.append(round(net * 1.21))
        assert tier.low_cents == pricing.round_down(min(invoices)), tier
        assert tier.high_cents == pricing.round_up(max(invoices)), tier


# ─── ER-02: deadline de 50 s real, todo colgado ────────────────────


def _pending_tasks() -> list[asyncio.Task]:
    me = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if t is not me and not t.done()]


async def _hang(*a, **k):
    await asyncio.sleep(3600)


async def test_er02_apify_real_con_http_colgado_responde_antes_de_51s_50s(env, monkeypatch):
    """Actor que no contesta: el POST a Apify queda colgado. Se usa el
    search_by_image real (con sus cortes) y el deadline real de 50 s."""
    monkeypatch.setattr(apify_1688, "search_by_image", _REAL_SEARCH)
    monkeypatch.setattr(get_settings(), "apify_token", "apify_api_x", raising=False)
    monkeypatch.setattr(apify_1688, "_POLL_INTERVAL_S", 0.05, raising=False)

    async def stuck(request):  # noqa: ARG001
        await asyncio.sleep(3600)
    monkeypatch.setattr(apify_1688, "_new_client",
                        lambda total_s: httpx.AsyncClient(transport=httpx.MockTransport(stuck)))
    monkeypatch.setattr(vendure_estimate, "VendureClient", _HungVendure)
    t = time.monotonic()
    resp = await _ask()
    took = time.monotonic() - t
    assert took <= 51.0, f"{took:.1f} s"
    assert resp.status == "not_found" and resp.action == "none"
    assert resp.estimate_status == "timeout" and resp.estimate is None
    assert FakeVendure.calls == []
    await asyncio.sleep(0.2)
    await apify_1688.wait_pending_aborts()
    assert _pending_tasks() == []
    row = _rows()[-1]
    assert row.estimate_status == "timeout" and row.deadline_hit in ("apify", "global")


@pytest.mark.parametrize("thumbs", ["colgadas", "lentas"])
async def test_er02_peor_caso_apify_al_limite_y_lo_demas_colgado_50s(env, monkeypatch, thumbs):
    """Apify contesta justo al final de su ventana; Qwen y Vendure colgados; las
    miniaturas colgadas (sin CLIP no hay 2 buenos: low_confidence) o lentas
    (CLIP alcanza y se llega a Vendure, que se cuelga: timeout). Cada paso
    tiene techo y el total no pasa de 51 s."""
    async def slow_search(image_url, *, trusted_bytes_sha256=None, deadline_s=38.0):
        await asyncio.sleep(max(deadline_s - 0.05, 0))
        return Search1688Result(status="ok", candidates=list(CANDS), run_ids=["R"],
                                usd_imputado=0.02)

    real_fetch = images._fetch

    async def fetch(url, *, interactive=False):
        if "cbu01.alicdn" in url:
            await asyncio.sleep(3600 if thumbs == "colgadas" else 3.0)
        return await real_fetch(url, interactive=interactive)

    class HungLLM(FakeLLM):
        async def _create(self, **request):
            self.requests.append(request)
            await asyncio.sleep(3600)

    env.llm = HungLLM("{}")
    monkeypatch.setattr(apify_1688, "search_by_image", slow_search)
    monkeypatch.setattr(images, "_fetch", fetch)
    monkeypatch.setattr(vendure_estimate, "VendureClient", _HungVendure)
    t = time.monotonic()
    resp = await _ask()
    took = time.monotonic() - t
    assert took <= 51.0, f"{took:.1f} s"
    assert resp.status == "not_found" and resp.estimate is None
    assert env.llm.requests, "el juez se tenía que llamar (y colgarse)"
    await asyncio.sleep(0.2)
    assert _pending_tasks() == []
    row = _rows()[-1]
    assert row.total_ms <= 51_000
    if thumbs == "colgadas":
        assert resp.estimate_status == "low_confidence" and FakeVendure.calls == []
    else:
        assert resp.estimate_status == "timeout" and row.deadline_hit == "vendure"
        assert len(FakeVendure.calls) == 1


async def test_er02_otapi_colgado_en_link_de_1688_no_cuelga_el_lookup(env, monkeypatch):
    """La lectura de la oferta de 1688 tiene que respetar el deadline aunque el
    cliente HTTP no corte (respuesta que gotea, DNS colgado)."""
    monkeypatch.setattr(get_settings(), "estimate_deadline_s", 3.0, raising=False)
    monkeypatch.setattr(otapi_item, "read_offer", _hang)

    async def blocked_extract(url):
        from app.ingest.image_from_url import BlockedByCaptcha
        raise BlockedByCaptcha("captcha")
    monkeypatch.setattr(app_routes.image_from_url, "extract", blocked_extract)
    t = time.monotonic()
    resp = await asyncio.wait_for(_ask(OFFER_URL), timeout=30)
    assert time.monotonic() - t < 10
    assert resp.estimate is None


async def test_er02_rerank_de_vision_colgado_tiene_techo(env, monkeypatch):
    monkeypatch.setattr(app_routes, "_ESTIMATE_RERANK_BUDGET_SECONDS", 0.2)
    monkeypatch.setattr(vision_rerank, "pick_match", _hang)
    est = SimpleNamespace(blocked=False)
    t = time.monotonic()
    assert await app_routes._pick_match(est, ["u"], [], "t") is None
    assert time.monotonic() - t < 1.0


class _HungVendure(FakeVendure):
    async def estimate_import_price(self, payload):
        FakeVendure.calls.append(payload)
        await asyncio.sleep(3600)


# ─── ER-07: límites ────────────────────────────────────────────────


async def test_er07_rafaga_de_dos_por_minuto(env):
    assert get_settings().estimate_visitor_per_minute == 2
    assert (await _ask()).estimate_status == "ok"
    assert (await _ask()).estimate_status == "ok"
    searches, vendure_calls = len(env.searches), len(FakeVendure.calls)
    third = await _ask()
    assert third.estimate_status == "rate_limited" and third.status == "not_found"
    assert len(env.searches) == searches and len(FakeVendure.calls) == vendure_calls
    assert env.llm.requests.__len__() == 2
    assert _rows()[-1].estimate_status == "rate_limited"


async def test_er07_base_caida_es_fail_closed_y_el_lookup_sigue(env, monkeypatch):
    class Broken:
        def __init__(self, *a, **k):
            raise OperationalError("SELECT", {}, Exception("db down"))
    monkeypatch.setattr(limits, "Session", Broken)
    # A2: con la base caída, el límite de todo el lookup por visitante del
    # storefront también es fail-closed: 429 sin gastar nada.
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await _ask()
    assert exc.value.status_code == 429
    assert env.searches == [] and FakeVendure.calls == [] and env.llm.requests == []
    # Sin ese límite de por medio (pasa), el estimado sigue siendo fail-closed
    # y el lookup responde como siempre.
    monkeypatch.setattr(limits, "reserve_lookup", lambda *a, **k: None)
    resp = await _ask()
    assert resp.status == "not_found" and resp.estimate is None
    assert resp.estimate_status in ("over_budget", "rate_limited")
    assert env.searches == [] and FakeVendure.calls == [] and env.llm.requests == []


async def test_er07_ip_seis_del_dia_aunque_pasen_los_minutos(env, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(limits.time, "time", lambda: clock[0])
    for _ in range(5):
        assert (await _ask()).estimate_status == "ok"
        clock[0] += 61
    spent = len(env.searches)
    sixth = await _ask()
    assert sixth.estimate_status == "rate_limited" and len(env.searches) == spent


def _http_app(monkeypatch, keys: str) -> TestClient:
    s = Settings(vendure_api_url="https://example.invalid/admin-api", hugo_api_keys=keys,
                 supabase_url="", supabase_anon_key="")
    monkeypatch.setattr(security, "get_settings", lambda: s)
    app = FastAPI()
    app.include_router(app_routes.router)
    return TestClient(app)


def test_er07_ip_del_visitante_solo_con_la_key_del_storefront(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "estimate_visitor_per_minute", 0, raising=False)
    keys = "storefront:sf-key-0123456789abcdef,luis:luis-key-0123456789abcdef"
    with _http_app(monkeypatch, keys) as http:
        def ask(key, ip):
            r = http.post("/app/lookup", headers={"X-API-Key": key},
                          json={"url": ML_URL, "want_estimate": True, "visitor": {"ip": ip}})
            assert r.status_code == 200
            return r.json()["estimate_status"]
        # Otro cliente rota la IP declarada: igual cuenta su socket (5 y basta).
        luis = [ask("luis-key-0123456789abcdef", f"190.2.2.{i}") for i in range(6)]
        assert luis == ["ok"] * 5 + ["rate_limited"]
        # El storefront sí: cada visitante tiene su cupo.
        assert ask("sf-key-0123456789abcdef", "190.3.3.1") == "ok"
        assert ask("sf-key-0123456789abcdef", "190.3.3.2") == "ok"
        # Sin key válida ni siquiera entra.
        r = http.post("/app/lookup", headers={"X-API-Key": "nope"},
                      json={"url": ML_URL, "want_estimate": True})
        assert r.status_code == 401
    hashes = {r.ip_hash for r in _rows()}
    assert len(hashes) == 3       # socket de luis + 2 visitantes del storefront


def test_er07_el_storefront_con_ip_privada_no_inventa_visitantes(env, monkeypatch):
    """Una IP declarada que no es de internet no se cree: cuenta el socket."""
    monkeypatch.setattr(get_settings(), "estimate_visitor_per_minute", 0, raising=False)
    with _http_app(monkeypatch, "storefront:sf-key-0123456789abcdef") as http:
        for i in range(6):
            r = http.post("/app/lookup", headers={"X-API-Key": "sf-key-0123456789abcdef"},
                          json={"url": ML_URL, "want_estimate": True,
                                "visitor": {"ip": f"10.0.0.{i}"}})
        assert r.json()["estimate_status"] == "rate_limited"


# ─── ER-08: nada del proveedor al navegador, en los cuatro caminos ─


_SENSITIVE = ("CNY", "¥", "yuan", "offerId", "offer_id", "company", "supplier",
              "price_cny", "purchasePriceCny", "similarity")


def _assert_no_candidate_leak(body: str) -> None:
    for word in _SENSITIVE:
        assert word not in body, word
    for c in CANDS:
        for value in (c.offer_id, c.image_url, c.title, c.company, c.detail_url):
            assert value not in body, value


@pytest.mark.parametrize("kind", ["ml", "alibaba", "1688", "photo"])
async def test_er08_cuatro_caminos_sin_datos_de_proveedores(env, monkeypatch, kind):
    monkeypatch.setattr(get_settings(), "estimate_upload_image_prefixes", "fotos.b2box.app/u/",
                        raising=False)
    if kind == "1688":
        env.otapi = otapi_item.Offer1688(
            offer_id="666233922092", title="保温杯", price_cny=33.0,
            ladder=[(10, 33.0), (200, 31.0), (1000, 30.0)], moq=10,
            pictures=["https://cbu01.alicdn.com/img/main.jpg"])
        resp = await _ask(OFFER_URL)
    elif kind == "photo":
        resp = await _ask("", image_url="https://fotos.b2box.app/u/9.jpg?sig=1")
    else:
        resp = await _ask(ML_URL if kind == "ml" else ALIBABA_URL)
    assert resp.estimate_status == "ok" and resp.action == "show_estimate"
    data = json.loads(resp.model_dump_json())
    body = json.dumps(data, ensure_ascii=False)
    _assert_no_candidate_leak(body)
    # Escalera de yuanes de 1688: ningún precio del proveedor en la respuesta.
    for cny in ("33.0", "31.0", "30.0", "16.5", "17.6", "18.7"):
        assert cny not in body, cny
    estimate_keys = set(data["estimate"])
    assert estimate_keys == {"currency", "vat_included", "tiers", "valid_until", "label",
                             "estimate_id"}
    if kind in ("ml", "photo"):
        assert "1688" not in body and "alicdn" not in body
    if kind == "alibaba":
        # La única foto de alicdn es la del link del propio visitante.
        assert body.count("alicdn") == body.count(ALIBABA_PHOTO)
        assert "1688" not in body


async def test_er08_link_de_1688_que_eco_trae_la_respuesta(env):
    """Documenta lo que vuelve para un link de 1688: el link y las fotos de la
    MISMA oferta que pegó el visitante (no las de otros proveedores)."""
    env.otapi = otapi_item.Offer1688(
        offer_id="666233922092", title="保温杯", price_cny=33.0,
        ladder=[(10, 33.0)], moq=10, pictures=["https://cbu01.alicdn.com/img/main.jpg"])
    data = json.loads((await _ask(OFFER_URL)).model_dump_json())
    assert data["canonical_url"] == OFFER_URL
    assert data["image_urls"] == ["https://cbu01.alicdn.com/img/main.jpg"]


# ─── Casi-match ("¿es este?") con estimado pedido ──────────────────


def _clip_world(monkeypatch, score: float, confidence: float):
    prod = VendureProduct(id="42", name="Vaso parecido", slug="vaso", description="",
                          enabled=True, source_url=None, image_urls=["https://x/p.jpg"],
                          product_code="BX-42", featured_image_url="https://x/p.jpg",
                          first_variant_price_cents=1000, variant_count=1)

    async def catalog(force=False):  # noqa: ARG001
        return [prod]

    async def noop():
        return None

    async def embed(urls, interactive=False):  # noqa: ARG001
        v = np.ones(8, dtype=np.float32) / np.sqrt(8)
        return [v for _ in urls]

    async def pick(query_urls, candidates, *, title=""):  # noqa: ARG001
        return vision_rerank.Verdict(product=prod, confidence=confidence, reason="parecido")

    idx = app_routes.catalog_index
    monkeypatch.setattr(app_routes.vendure_catalog, "get_catalog", catalog)
    monkeypatch.setattr(app_routes.image_embed, "available", lambda: True)
    monkeypatch.setattr(app_routes.image_embed, "embed_urls_aligned", embed)
    monkeypatch.setattr(idx, "ensure_index", noop)
    monkeypatch.setattr(idx, "is_ready", lambda: True)
    monkeypatch.setattr(idx, "search", lambda vec, top_k=3: [(prod, score, "https://x/p.jpg")])
    monkeypatch.setattr(idx, "score_products", lambda vec, ids: [])
    monkeypatch.setattr(idx, "effective_threshold", lambda key, value: value)
    monkeypatch.setattr(vision_rerank, "pick_match", pick)
    return prod


async def test_casi_match_con_estimado_dice_skipped_found_y_pregunta(env, monkeypatch):
    _clip_world(monkeypatch, score=0.80, confidence=0.60)
    resp = await _ask()
    assert resp.action == "confirm_product" and resp.suggestion is not None
    assert resp.estimate is None
    assert resp.estimate_status == "skipped_found"
    assert [r.estimate_status for r in _rows()] == ["skipped_found"]


async def test_casi_match_rechazado_si_estima(env, monkeypatch):
    _clip_world(monkeypatch, score=0.80, confidence=0.60)
    payload = AppLookupRequest(url=ML_URL, want_estimate=True, reject_suggestion=True,
                               visitor=AppLookupVisitor(ip=VISITOR_IP))
    resp = app_routes._decide_action(await _lookup(payload, _request()))
    await est_service.wait_background()
    assert resp.estimate_status == "ok" and resp.action == "show_estimate"


_REAL_SEARCH = apify_1688.search_by_image
