"""POST /radar/ml-supply (app.pricing.radar_ml_supply): cuántas publicaciones tiene ML por palabra, para Luis.

Sin red: Apify es un MockTransport de httpx que hace de actor (permisos, lanzar, poll, dataset). Lo que se cubre
es lo que puede salir caro o mentir: auth, validación, que un total faltante o roto sea null y nunca 0, la caché,
el tope diario, sin token, un error de Apify, el registro del gasto por corrida y el tope mensual en USD.
"""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.clock import utcnow
from app.config import get_settings
from app.db.models import ApifySpend, RadarMlSupplyCache, Setting
from app.db.session import engine, init_db
from app.pricing import apify_ml_search as ams
from app.pricing import daily_budget
from app.pricing import radar_ml_supply as rms

TOKEN = "apify_api_SECRETO_radar_no_tiene_que_salir_9876"
LUIS_KEY = "luis-" + "r8a-key-0123456789abcdefghij"
AUTH = {"X-API-Key": LUIS_KEY}
TOTALS = {"organizador cocina": 651, "lampara led recargable mini": 805, "botella termica": 9999}


class FakeApify:
    def __init__(self, rows_for=None, *, start_status: int = 201, final: str = "SUCCEEDED",
                 permission: str = "LIMITED_PERMISSIONS", usage: float | None = 0.01):
        self.rows_for = rows_for or (lambda terms: [
            {"type": "product", "searchTerm": t, "totalResults": TOTALS.get(t.lower(), 3)} for t in terms])
        self.start_status, self.final, self.permission, self.usage = start_status, final, permission, usage
        self.requests: list[httpx.Request] = []
        self.inputs: list[dict] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/v2/acts/lergassy~mercadolibre-reviews-scraper":
            return httpx.Response(200, json={"data": {"id": "m3WG6vSxptfaQ4NUv",
                                                      "actorPermissionLevel": self.permission}})
        if request.method == "POST" and path == "/v2/acts/lergassy~mercadolibre-reviews-scraper/runs":
            if self.start_status not in (200, 201):
                return httpx.Response(self.start_status, json={"error": {"message": f"token {TOKEN}"}})
            self.inputs.append(json.loads(request.content))
            return httpx.Response(201, json={"data": {"id": "run1", "status": "READY", "defaultDatasetId": "ds1"}})
        if path == "/v2/actor-runs/run1":
            data = {"id": "run1", "status": self.final, "defaultDatasetId": "ds1"}
            if self.usage is not None:
                data["usageTotalUsd"] = self.usage
            return httpx.Response(200, json={"data": data})
        if path == "/v2/datasets/ds1/items":
            rows = [{"type": "notice", "message": "aviso del actor", "totalResults": 0}]
            rows += self.rows_for(self.inputs[-1]["searchTerms"])
            return httpx.Response(200, content=json.dumps(rows).encode(),
                                  headers={"content-type": "application/json"})
        return httpx.Response(404)

    @property
    def runs(self) -> int:
        return len(self.inputs)


@pytest.fixture
def client(monkeypatch):
    init_db()
    with Session(engine) as s:
        for model in (RadarMlSupplyCache, ApifySpend):
            for row in s.exec(select(model)).all():
                s.delete(row)
        row = s.get(Setting, rms.COUNTER_KEY)
        if row is not None:
            s.delete(row)
        s.commit()
    settings = get_settings()
    monkeypatch.setattr(settings, "hugo_api_keys", f"luis:{LUIS_KEY}", raising=False)
    monkeypatch.setattr(settings, "hugo_api_key", "", raising=False)
    monkeypatch.setattr(settings, "apify_token", TOKEN, raising=False)
    monkeypatch.setattr(settings, "hugo_radar_ml_daily_cap", 300, raising=False)
    monkeypatch.setattr(settings, "hugo_radar_apify_token", "", raising=False)
    monkeypatch.setattr(settings, "hugo_radar_apify_monthly_usd", 3.0, raising=False)
    monkeypatch.setattr(ams, "_POLL_INTERVAL_S", 0.0)
    from app import main
    return TestClient(main.app)


def _install(monkeypatch, fake: FakeApify) -> FakeApify:
    monkeypatch.setattr(rms, "_new_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    return fake


def _post(client, terms, **extra):
    return client.post("/radar/ml-supply", json={"terms": terms, "source": "luis", **extra}, headers=AUTH)


def _by_term(resp) -> dict:
    assert resp.status_code == 200, resp.text
    return {r["term"]: r for r in resp.json()["results"]}


# ─── Auth y validación ─────────────────────────────────────────────


def test_auth_sin_key_o_key_mala_da_401(client, monkeypatch):
    fake = _install(monkeypatch, FakeApify())
    body = {"terms": ["botella termica"]}
    assert client.post("/radar/ml-supply", json=body).status_code == 401
    assert client.post("/radar/ml-supply", json=body, headers={"X-API-Key": LUIS_KEY + "x"}).status_code == 401
    assert fake.requests == []


def test_produccion_sin_keys_cierra_con_503(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "hugo_api_keys", "", raising=False)
    monkeypatch.setattr(settings, "hugo_env", "production", raising=False)
    assert client.post("/radar/ml-supply", json={"terms": ["x"]}, headers=AUTH).status_code == 503


@pytest.mark.parametrize("body", [
    {"terms": []},
    {"terms": [f"t{i}" for i in range(21)]},
    {"terms": ["x" * 101]},
    {"terms": ["   "]},
    {"terms": "botella"},
    {},
    {"terms": ["botella"], "extra": 1},
])
def test_validacion_422(client, monkeypatch, body):
    fake = _install(monkeypatch, FakeApify())
    assert client.post("/radar/ml-supply", json=body, headers=AUTH).status_code == 422
    assert fake.requests == []


# ─── Totales ───────────────────────────────────────────────────────


def test_totales_ok_en_una_corrida_y_notice_ignorado(client, monkeypatch):
    fake = _install(monkeypatch, FakeApify())
    resp = _post(client, ["organizador cocina", " lampara led recargable mini ", "botella termica"])
    res = _by_term(resp)
    assert {t: r["total"] for t, r in res.items()} == TOTALS
    for r in res.values():
        assert r["ok"] is True and r["source"] == "apify" and r["error"] is None
        assert r["sellers"] is None and r["median_price_ars"] is None and r["fetched_at"].endswith("Z")
    assert resp.json()["budget"] == {"used_today": 3, "daily_cap": 300, "apify_usd_month": 0.01,
                                     "apify_usd_cap_month": 3.0, "apify_usd_today": 0.01}
    assert fake.runs == 1
    assert fake.inputs[0] == {"mode": "search", "country": "AR", "maxItems": 1, "maxConcurrency": 3,
                              "searchTerms": ["organizador cocina", "lampara led recargable mini", "botella termica"]}
    start = next(r for r in fake.requests if r.url.path.endswith("/runs"))
    assert dict(start.url.params) == {"timeout": "240", "maxTotalChargeUsd": "0.08"}
    assert all(TOKEN not in str(r.url) for r in fake.requests)
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in fake.requests)


def test_cero_solo_si_ml_dijo_cero(client, monkeypatch):
    _install(monkeypatch, FakeApify(lambda terms: [{"type": "product", "searchTerm": "nada", "totalResults": 0}]))
    r = _by_term(_post(client, ["nada"]))["nada"]
    assert r["ok"] is True and r["total"] == 0 and r["error"] is None


@pytest.mark.parametrize("bad", ["651", -1, True, 1.5, None, 10**12])
def test_termino_sin_fila_o_total_roto_es_null(client, monkeypatch, bad):
    rows = lambda terms: [
        {"type": "product", "searchTerm": "botella termica", "totalResults": 9999},
        {"type": "product", "searchTerm": "roto", "totalResults": bad},
        {"type": "notice", "searchTerm": "sin fila", "totalResults": 5},
    ]
    _install(monkeypatch, FakeApify(rows))
    res = _by_term(_post(client, ["botella termica", "roto", "sin fila"]))
    assert res["botella termica"]["total"] == 9999 and res["botella termica"]["ok"] is True
    for term in ("roto", "sin fila"):
        assert res[term]["ok"] is False and res[term]["total"] is None and res[term]["error"]
    with Session(engine) as s:
        assert [r.term_key for r in s.exec(select(RadarMlSupplyCache))] == ["botella termica"]


# ─── Caché ─────────────────────────────────────────────────────────


def test_cache_no_paga_y_responde_el_texto_original(client, monkeypatch):
    fake = _install(monkeypatch, FakeApify())
    first = _by_term(_post(client, ["Organizador cocina"]))["Organizador cocina"]
    resp = _post(client, ["  organizador   COCINA "])
    again = _by_term(resp)["organizador   COCINA"]
    assert fake.runs == 1
    assert again["source"] == "cache" and again["total"] == 651 and again["fetched_at"] == first["fetched_at"]
    assert resp.json()["budget"]["used_today"] == 1


def test_cache_vencida_vuelve_a_consultar(client, monkeypatch):
    with Session(engine) as s:
        s.add(RadarMlSupplyCache(term_key="botella termica", total=1, fetched_at=utcnow() - timedelta(days=8)))
        s.commit()
    fake = _install(monkeypatch, FakeApify())
    res = _by_term(_post(client, ["botella termica"]))["botella termica"]
    assert fake.runs == 1 and res["source"] == "apify" and res["total"] == 9999


def test_terminos_que_normalizan_igual_devuelven_uno_cada_uno(client, monkeypatch):
    fake = _install(monkeypatch, FakeApify())
    resp = _post(client, ["Botella Termica", "botella  termica", "Botella Termica "])
    results = resp.json()["results"]
    assert [r["term"] for r in results] == ["Botella Termica", "botella  termica"]
    assert all(r["ok"] and r["total"] == 9999 for r in results)
    assert fake.inputs[0]["searchTerms"] == ["Botella Termica"]
    assert resp.json()["budget"]["used_today"] == 1


# ─── Tope, sin token, errores ──────────────────────────────────────


def test_tope_diario(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "hugo_radar_ml_daily_cap", 2, raising=False)
    fake = _install(monkeypatch, FakeApify())
    resp = _post(client, ["organizador cocina", "lampara led recargable mini", "botella termica"])
    res = _by_term(resp)
    assert res["organizador cocina"]["total"] == 651 and res["lampara led recargable mini"]["total"] == 805
    assert res["botella termica"] == {**res["botella termica"], "ok": False, "total": None, "error": "tope diario"}
    assert fake.inputs[0]["searchTerms"] == ["organizador cocina", "lampara led recargable mini"]
    assert resp.json()["budget"]["used_today"] == 2 and resp.json()["budget"]["daily_cap"] == 2
    # Agotado: lo nuevo no paga, lo de caché sigue saliendo.
    res = _by_term(_post(client, ["botella termica", "organizador cocina"]))
    assert res["botella termica"]["error"] == "tope diario" and res["organizador cocina"]["source"] == "cache"
    assert fake.runs == 1


def test_sin_token(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "apify_token", "  ", raising=False)
    fake = _install(monkeypatch, FakeApify())
    resp = _post(client, ["botella termica", "organizador cocina"])
    assert all(r["ok"] is False and r["total"] is None and r["error"] == "apify no configurado"
               for r in resp.json()["results"])
    assert fake.requests == [] and daily_budget.used_today(rms.COUNTER_KEY) == 0


@pytest.mark.parametrize("fake_kwargs, reason", [
    ({"start_status": 402}, "sin saldo"),
    ({"start_status": 401}, "token inválido"),
    ({"final": "FAILED"}, "FAILED"),
    ({"permission": "FULL_PERMISSIONS"}, "permisos limitados"),
])
def test_error_de_apify_deja_todos_en_null(client, monkeypatch, fake_kwargs, reason):
    _install(monkeypatch, FakeApify(**fake_kwargs))
    resp = _post(client, ["botella termica", "organizador cocina"])
    for r in resp.json()["results"]:
        assert r["ok"] is False and r["total"] is None and reason in r["error"]
        assert TOKEN not in r["error"] and "http" not in r["error"].lower().replace("http ", "")
    with Session(engine) as s:
        assert s.exec(select(RadarMlSupplyCache)).all() == []


def test_excepcion_de_red_no_rompe_el_pedido(client, monkeypatch):
    async def boom(request):
        raise httpx.ConnectError(f"https://api.apify.com/?token={TOKEN}")
    monkeypatch.setattr(rms, "_new_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    r = _by_term(_post(client, ["botella termica"]))["botella termica"]
    assert r["ok"] is False and r["total"] is None and TOKEN not in r["error"]


# ─── Gasto de Apify ────────────────────────────────────────────────


def _spend() -> list[ApifySpend]:
    with Session(engine) as s:
        return list(s.exec(select(ApifySpend).order_by(ApifySpend.id)))


def test_cada_corrida_queda_registrada(client, monkeypatch):
    _install(monkeypatch, FakeApify(usage=0.0075))
    _post(client, ["organizador cocina", "lampara led recargable mini", "botella termica"])
    _post(client, ["organizador cocina"])          # de caché: no corre, no registra
    [row] = _spend()
    assert (row.label, row.run_id, row.terms, row.usd, row.estimated) == ("radar_ml", "run1", 3, 0.0075, False)


def test_corrida_fallida_se_registra_y_la_no_lanzada_no(client, monkeypatch):
    _install(monkeypatch, FakeApify(start_status=402))
    _post(client, ["botella termica"])
    assert _spend() == []
    _install(monkeypatch, FakeApify(final="FAILED", usage=0.004))
    _post(client, ["organizador cocina"])
    assert [(r.run_id, r.usd, r.estimated) for r in _spend()] == [("run1", 0.004, False)]


def test_sin_usage_total_se_estima_y_se_marca(client, monkeypatch):
    _install(monkeypatch, FakeApify(usage=None))
    resp = _post(client, ["organizador cocina", "botella termica"])
    [row] = _spend()
    # 2 filas product cobradas (la notice no cuenta) × USD 0,0025.
    assert row.estimated is True and row.usd == pytest.approx(0.005)
    assert resp.json()["budget"]["apify_usd_today"] == pytest.approx(0.005)


def test_tope_mensual_frena_la_corrida(client, monkeypatch):
    with Session(engine) as s:
        s.add(ApifySpend(label="radar_ml", run_id="viejo", terms=10, usd=2.95))
        s.add(ApifySpend(label="radar_ml", run_id="otro_mes", terms=10, usd=50.0,
                         created_at=utcnow() - timedelta(days=40)))
        s.add(ApifySpend(label="otro_proyecto", run_id="x", terms=1, usd=50.0))
        s.commit()
    fake = _install(monkeypatch, FakeApify())
    # 2,95 + techo de 1 término (0,06) = 3,01 > 3.
    resp = _post(client, ["botella termica"])
    r = _by_term(resp)["botella termica"]
    assert r["ok"] is False and r["total"] is None and r["error"] == "tope mensual de Apify"
    assert fake.requests == [] and daily_budget.used_today(rms.COUNTER_KEY) == 0
    assert resp.json()["budget"]["apify_usd_month"] == pytest.approx(2.95)
    # Con tope más alto corre (el gasto de otro mes y de otro proyecto no cuentan).
    monkeypatch.setattr(get_settings(), "hugo_radar_apify_monthly_usd", 3.02, raising=False)
    assert _by_term(_post(client, ["botella termica"]))["botella termica"]["total"] == 9999


def test_cache_sale_aunque_el_tope_mensual_este_agotado(client, monkeypatch):
    _install(monkeypatch, FakeApify())
    _post(client, ["botella termica"])
    monkeypatch.setattr(get_settings(), "hugo_radar_apify_monthly_usd", 0.0, raising=False)
    res = _by_term(_post(client, ["botella termica", "organizador cocina"]))
    assert res["botella termica"]["source"] == "cache"
    assert res["organizador cocina"]["error"] == "tope mensual de Apify"


@pytest.mark.parametrize("own", ["apify_api_radar_PROPIA_5555", ""])
def test_usa_la_key_propia_del_radar_si_existe(client, monkeypatch, own):
    monkeypatch.setattr(get_settings(), "hugo_radar_apify_token", own, raising=False)
    fake = _install(monkeypatch, FakeApify())
    assert _by_term(_post(client, ["botella termica"]))["botella termica"]["ok"] is True
    assert {r.headers["authorization"] for r in fake.requests} == {f"Bearer {own or TOKEN}"}


def test_key_propia_sola_alcanza(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "apify_token", "", raising=False)
    monkeypatch.setattr(get_settings(), "hugo_radar_apify_token", "apify_api_radar_PROPIA_5555", raising=False)
    _install(monkeypatch, FakeApify())
    assert _by_term(_post(client, ["botella termica"]))["botella termica"]["total"] == 9999


def test_usage_por_dia_y_del_mes(client, monkeypatch):
    with Session(engine) as s:
        s.add(ApifySpend(label="radar_ml", run_id="a", terms=3, usd=0.01))
        s.add(ApifySpend(label="radar_ml", run_id="b", terms=2, usd=0.005, estimated=True))
        s.add(ApifySpend(label="radar_ml", run_id="viejo", terms=1, usd=9.0, created_at=utcnow() - timedelta(days=45)))
        s.commit()
    assert client.get("/radar/ml-supply/usage").status_code == 401
    body = client.get("/radar/ml-supply/usage", headers=AUTH).json()
    today = utcnow().strftime("%Y-%m-%d")
    assert body["days"] == [{"date": today, "usd": 0.015, "runs": 2, "terms": 5, "estimated_runs": 1}]
    assert body["apify_usd_today"] == pytest.approx(0.015) and body["apify_usd_cap_month"] == 3.0
    assert body["label"] == "radar_ml"
