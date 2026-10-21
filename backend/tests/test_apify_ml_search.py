"""Búsqueda web de ML con Apify para el semáforo (app.pricing.apify_ml_search).

Sin red: la API de Apify es un MockTransport de httpx que hace de corridas (lanzar, poll, dataset, abort) y
devuelve, para cada corrida, publicaciones armadas con la consulta que recibió. Lo que se cubre es lo que
puede salir caro o mal: que cada producto se quede con SUS publicaciones (una corrida por consulta), que el
cupo diario corte (y se reserve de a lote), que lo que vuelve del actor se valide, que apagado no gaste nada
y que la Mac y Apify no busquen el mismo producto.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import timedelta

import httpx
import pytest
from sqlmodel import Session, select

from app import runtime
from app.clock import utcnow
from app.config import get_settings
from app.db.models import AuditLog, MarketPriceSnapshot, MlWebClaim, MlWebResult, Setting
from app.db.session import engine, init_db
from app.pricing import apify_ml_search as ams
from app.pricing import daily_budget, oficina_ml

TOKEN = "apify_api_SECRETO_ml_search_no_tiene_que_salir_4321"
ACTOR = "devcake/mercadolibre-scraper"
NAMES = [
    "Organizador de cocina bambú", "Lámpara escritorio LED", "Mochila urbana impermeable",
    "Termo acero inoxidable", "Auriculares bluetooth deportivos", "Cafetera italiana aluminio",
]


@pytest.fixture
def env(monkeypatch):
    """Base limpia, Apify con token y actor fijado, búsqueda PRENDIDA con tope 200, tiempos chicos."""
    init_db()
    with Session(engine) as s:
        for model in (MlWebResult, MlWebClaim, MarketPriceSnapshot, Setting):
            for row in s.exec(select(model)).all():
                s.delete(row)
        for row in s.exec(select(AuditLog).where(AuditLog.action == "apify_ml_search_run")).all():
            s.delete(row)
        s.commit()
    settings = get_settings()
    monkeypatch.setattr(settings, "apify_token", TOKEN, raising=False)
    monkeypatch.setattr(settings, "apify_meli_search_actor", ACTOR, raising=False)
    monkeypatch.setattr(settings, "apify_meli_search_build", "1.0.103", raising=False)
    monkeypatch.setattr(settings, "apify_meli_search_timeout_s", 30, raising=False)
    monkeypatch.setattr(settings, "apify_meli_search_concurrency", 4, raising=False)
    monkeypatch.setattr(ams, "_POLL_INTERVAL_S", 0.0)
    runtime.invalidate()
    runtime.set_value("apify_meli_search_enabled", 1)
    runtime.set_value("apify_meli_search_daily_max", 200)
    yield settings
    runtime.invalidate()


def _snaps(n: int = len(NAMES)) -> list[str]:
    pids = [str(100 + i) for i in range(n)]
    with Session(engine) as s:
        for pid, name in zip(pids, NAMES[:n]):
            s.add(MarketPriceSnapshot(run_id=1, product_id=pid, ml_status="no_data", product_name=name,
                                      product_enabled=True))
        s.commit()
    return pids


def _item(query: str, k: int, run: int, **over) -> dict:
    ref = f"MLA{run * 1000 + k}"
    item = {
        "name": f"{query} publicación {k}", "price": 15000 + k, "currency": "ARS", "ml_id": ref,
        "url_item": f"https://articulo.mercadolibre.com.ar/MLA-{run * 1000 + k}-algo",
        "vendor_name": "Tienda Uno", "seller": "Tienda Uno",
        "thumbnail": "https://http2.mlstatic.com/D_NQ_NP_123-MLA456_789-I.webp",
        "image_item": [], "free_shipping": True, "domain_id": "MLA-KITCHEN", "category_id": "MLA1234",
        "official_store": None, "original_price": None, "discount_percent": None,
    }
    item.update(over)
    return item


class FakeApify:
    """Hace de la API de Apify. Cada corrida devuelve `per_run` publicaciones con la consulta que recibió.
    `behavior`: ok | hang (RUNNING para siempre) | fail (FAILED). `start_status`: HTTP al lanzar."""

    def __init__(self, behavior: str = "ok", *, per_run: int = 20, items_for=None, start_status: int = 201):
        self.behavior = behavior
        self.per_run = per_run
        self.items_for = items_for
        self.start_status = start_status
        self.requests: list[httpx.Request] = []
        self.queries: dict[int, list[str]] = {}
        self.aborted: list[str] = []
        self.max_in_flight = 0
        self._in_flight = 0

    def _items(self, n: int) -> list:
        query = self.queries[n][0]
        if self.items_for is not None:
            return self.items_for(query, n)
        return [_item(query, k, n) for k in range(self.per_run)]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/runs"):
            if self.start_status not in (200, 201):
                return httpx.Response(self.start_status, json={"error": {"message": f"token {TOKEN}"}})
            n = len(self.queries) + 1
            self.queries[n] = json.loads(request.content)["queries"]
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            return httpx.Response(201, json={"data": {"id": f"run{n}", "status": "READY",
                                                      "defaultDatasetId": f"ds{n}"}})
        if m := re.fullmatch(r"/v2/actor-runs/run(\d+)/abort", path):
            self.aborted.append(f"run{m.group(1)}")
            return httpx.Response(200, json={"data": {"id": f"run{m.group(1)}", "status": "ABORTED"}})
        if m := re.fullmatch(r"/v2/actor-runs/run(\d+)", path):
            n = int(m.group(1))
            if self.behavior == "hang":
                await asyncio.sleep(0.05)
                status = "RUNNING"
            else:
                status = "SUCCEEDED" if self.behavior == "ok" else "FAILED"
                self._in_flight -= 1
            return httpx.Response(200, json={"data": {"id": f"run{n}", "status": status,
                                                      "defaultDatasetId": f"ds{n}", "usageTotalUsd": 0.001}})
        if m := re.fullmatch(r"/v2/datasets/ds(\d+)/items", path):
            # A mano: un actor roto puede mandar NaN, y httpx.Response(json=...) no lo serializa.
            return httpx.Response(200, content=json.dumps(self._items(int(m.group(1)))).encode(),
                                  headers={"content-type": "application/json"})
        return httpx.Response(404)

    def install(self, monkeypatch) -> FakeApify:
        monkeypatch.setattr(ams, "_new_client",
                            lambda total_s: httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))
        return self

    @property
    def starts(self) -> int:
        return len(self.queries)


def _rows() -> list[MlWebResult]:
    with Session(engine) as s:
        return list(s.exec(select(MlWebResult).order_by(MlWebResult.id)))


def _used() -> int:
    return daily_budget.used_today(ams.COUNTER_KEY)


# ─── Lotes y atribución ────────────────────────────────────────────


async def test_cada_producto_se_queda_con_sus_publicaciones(env, monkeypatch):
    pids = _snaps()
    fake = FakeApify().install(monkeypatch)

    report = await ams.run_search()

    assert report.status == "ok", report.reason
    assert report.searches == 6 and report.ok == 6 and report.errors == 0
    # Una corrida por producto y UNA consulta por corrida: es lo que garantiza la atribución.
    assert fake.starts == 6
    assert all(len(q) == 1 for q in fake.queries.values())
    # Lotes de 4 (concurrency): nunca más de 4 corridas en vuelo.
    assert fake.max_in_flight <= 4
    rows = _rows()
    assert sorted(r.product_id for r in rows) == sorted(pids)
    for row in rows:
        assert row.origin == "apify" and row.status == "ok"
        cands = json.loads(row.candidates)
        # Se guardan las primeras 8 (pm_ml_web_max_results) de las 20 que cobra el actor, en su orden.
        assert row.n_candidates == len(cands) == 8
        assert all(c["name"].startswith(row.query) for c in cands)
        run = next(n for n, q in fake.queries.items() if q == [row.query])
        assert [c["id"] for c in cands] == [f"MLA{run * 1000 + k}" for k in range(8)]
        assert cands[0]["price_cents"] == 1_500_000 and cands[0]["currency"] == "ARS"
    assert report.results == 48
    assert _used() == 6


async def test_el_input_del_actor_y_el_token(env, monkeypatch):
    _snaps(1)
    fake = FakeApify().install(monkeypatch)

    await ams.run_search()

    start = next(r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/runs"))
    assert start.url.path == "/v2/acts/devcake~mercadolibre-scraper/runs"
    assert dict(start.url.params) == {"timeout": "30", "memory": "512", "maxTotalChargeUsd": "0.03",
                                      "build": "1.0.103"}
    body = json.loads(start.content)
    assert body == {"queries": body["queries"], "country": "AR", "maxItems": 20, "enrichDetailPage": False,
                    "sort": "relevance", "condition": "new"}
    for req in fake.requests:
        assert req.headers["authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(req.url)


async def test_lo_guardado_lo_lee_el_semaforo_como_lo_de_la_oficina(env, monkeypatch):
    pids = _snaps(2)
    FakeApify().install(monkeypatch)

    await ams.run_search()

    fresh = oficina_ml.load_fresh()
    assert set(fresh) == set(pids)
    for res in fresh.values():
        assert res.status == "ok" and len(res.candidates) == 8
        assert all(c.origin == "oficina" for c in res.candidates)      # mismo camino que la Mac
    # Con resultado fresco, el producto sale de la cola.
    assert oficina_ml.build_queue(50) == []


async def test_corrida_vacia_se_guarda_como_empty(env, monkeypatch):
    _snaps(1)
    FakeApify(per_run=0).install(monkeypatch)

    report = await ams.run_search()

    assert report.empty == 1
    (row,) = _rows()
    assert row.status == "empty" and row.n_candidates == 0


# ─── Cupo diario ───────────────────────────────────────────────────


async def test_el_cupo_corta_y_se_reserva_por_lote(env, monkeypatch):
    _snaps()
    runtime.set_value("apify_meli_search_daily_max", 3)
    fake = FakeApify().install(monkeypatch)

    report = await ams.run_search()

    assert report.status == "over_budget"
    assert fake.starts == 3 and report.searches == 3
    assert _used() == 3
    assert len(_rows()) == 3
    # Los que no entraron quedan libres para mañana (o para la Mac).
    with Session(engine) as s:
        assert s.exec(select(MlWebClaim)).all() == []

    again = await ams.run_search()
    assert again.status == "over_budget" and fake.starts == 3


async def test_cupo_ya_gastado_por_otra_corrida(env, monkeypatch):
    _snaps()
    runtime.set_value("apify_meli_search_daily_max", 5)
    daily_budget.reserve_up_to(ams.COUNTER_KEY, 5, 4)          # otra corrida ya usó 4
    fake = FakeApify().install(monkeypatch)

    report = await ams.run_search()

    assert fake.starts == 1 and report.searches == 1 and _used() == 5


def test_reserve_up_to_da_lo_que_queda_y_despues_cero(env):
    key = "_meta:test_reserve_up_to"
    assert daily_budget.reserve_up_to(key, 10, 4) == 4
    assert daily_budget.reserve_up_to(key, 10, 4) == 4
    assert daily_budget.reserve_up_to(key, 10, 4) == 2
    assert daily_budget.reserve_up_to(key, 10, 4) == 0
    assert daily_budget.reserve_up_to(key, 10, 0) == 0
    assert daily_budget.used_today(key) == 10
    # `reserve` (uno por vez) sigue igual sobre el mismo contador.
    assert daily_budget.reserve(key, 10) is None
    assert daily_budget.reserve(key, 11) == 11


# ─── Apagado ───────────────────────────────────────────────────────


async def test_apagado_no_llama_ni_gasta(env, monkeypatch):
    _snaps()
    runtime.set_value("apify_meli_search_enabled", 0)
    fake = FakeApify().install(monkeypatch)

    report = await ams.run_search()

    assert report.status == "disabled"
    assert fake.requests == [] and _used() == 0 and _rows() == []


async def test_sin_token_o_con_actor_fuera_de_lista_no_corre(env, monkeypatch):
    _snaps()
    fake = FakeApify().install(monkeypatch)
    monkeypatch.setattr(env, "apify_token", "")
    assert (await ams.run_search()).status == "disabled"
    monkeypatch.setattr(env, "apify_token", TOKEN)
    monkeypatch.setattr(env, "apify_meli_search_actor", "otro/actor")
    assert (await ams.run_search()).status == "disabled"
    monkeypatch.setattr(env, "apify_meli_search_actor", ACTOR)
    monkeypatch.setattr(env, "apify_meli_search_build", "1.0; rm -rf")
    assert (await ams.run_search()).status == "disabled"
    assert fake.requests == []


def test_los_settings_nuevos_quedan_auditados_y_apagados_por_defecto():
    from app.api.routes import _COSTLY_SETTINGS

    assert {"apify_meli_search_enabled", "apify_meli_search_daily_max"} <= _COSTLY_SETTINGS
    s = get_settings().__class__(vendure_api_url="https://example.invalid/x")
    assert s.apify_meli_search_enabled == 0 and s.apify_meli_search_daily_max == 200
    assert s.apify_meli_search_actor == ACTOR and s.apify_meli_search_build
    with pytest.raises(ValueError):
        runtime.set_value("apify_meli_search_enabled", 2)


# ─── Validación de lo que devuelve el actor ────────────────────────


@pytest.mark.parametrize("over", [
    {"url_item": "https://evil.example.com/MLA-1"},
    {"url_item": "https://www.mercadolibre.com.mx/algo"},
    {"url_item": "https://articulo.mercadolibre.com.ar.evil.com/MLA-1"},
    {"url_item": "http://articulo.mercadolibre.com.ar/MLA-1"},
    {"url_item": "javascript:alert(1)"},
    {"url_item": None, "permalink": None},
    {"price": 0}, {"price": -5}, {"price": float("nan")}, {"price": float("inf")}, {"price": True},
    {"price": "gratis"}, {"price": None}, {"price": 10**12},
    {"currency": None}, {"currency": "pesos"},
    {"ml_id": "MLM123"}, {"ml_id": "MLA12x"}, {"ml_id": 123},
    {"name": ""}, {"name": None},
])
def test_publicacion_invalida_se_descarta(over):
    assert ams.to_wire(_item("termo", 1, 1, **over)) is None


def test_publicacion_valida_se_normaliza():
    wire = ams.to_wire(_item("termo", 1, 1, price="15001.5", name="Termo\n<b>acero</b>​",
                             url_item="https://www.mercadolibre.com.ar/termo/p/MLA987654#reviews",
                             thumbnail="https://evil.example.com/x.jpg",
                             image_item=["https://http2.mlstatic.com/D_1-O.jpg"]))
    assert wire is not None
    assert wire["price_cents"] == 1_500_150
    assert "\n" not in wire["name"] and "​" not in wire["name"]
    assert wire["permalink"] == "https://www.mercadolibre.com.ar/termo/p/MLA987654"
    assert wire["catalog_id"] == "MLA987654"
    assert wire["image_urls"] == ["https://http2.mlstatic.com/D_1-O.jpg"]
    # Un click-tracker de publicidad se cambia por el link canónico del id.
    ad = ams.to_wire(_item("termo", 1, 1, url_item="https://click1.mercadolibre.com.ar/mclics/clicks/x"))
    assert ad["permalink"] == "https://articulo.mercadolibre.com.ar/MLA-1001"


async def test_salida_invalida_se_descarta_y_lo_valido_se_guarda(env, monkeypatch):
    _snaps(1)

    def items(query, n):
        good = _item(query, 1, n)
        return [
            "no soy un objeto", None, [1, 2],
            _item(query, 2, n, price=float("nan")),
            _item(query, 3, n, url_item="https://evil.example.com/MLA-3"),
            _item(query, 4, n, ml_id="DROP TABLE"),
            good, dict(good),                        # repetida: una sola vez
        ]

    FakeApify(items_for=items).install(monkeypatch)

    report = await ams.run_search()

    assert report.ok == 1 and report.results == 1
    (row,) = _rows()
    assert [c["id"] for c in json.loads(row.candidates)] == ["MLA1001"]


async def test_todo_invalido_queda_empty(env, monkeypatch):
    _snaps(1)
    FakeApify(items_for=lambda q, n: [_item(q, k, n, price=-1) for k in range(20)]).install(monkeypatch)

    report = await ams.run_search()

    assert report.empty == 1
    (row,) = _rows()
    assert row.status == "empty" and "validación" in (row.reason or "")


# ─── Timeouts y fallas ─────────────────────────────────────────────


async def test_corrida_colgada_se_aborta(env, monkeypatch):
    _snaps(1)
    monkeypatch.setattr(env, "apify_meli_search_timeout_s", 1)
    monkeypatch.setattr(ams, "_DEADLINE_MARGIN_S", 0.2)
    fake = FakeApify("hang").install(monkeypatch)

    report = await ams.run_search()

    assert fake.aborted == ["run1"]
    assert report.errors == 1
    (row,) = _rows()
    assert row.status == "error" and "abortada" in row.reason
    # Se imputa el peor caso: no se sabe cuánto alcanzó a cobrar.
    assert report.usd_imputado == pytest.approx(0.02005)
    # Un error no es resultado: el producto sigue en la cola para la próxima.
    assert [i["product_id"] for i in oficina_ml.build_queue(10)] == ["100"]


async def test_sin_saldo_frena_la_noche(env, monkeypatch, caplog):
    _snaps()
    fake = FakeApify(start_status=402).install(monkeypatch)

    report = await ams.run_search()

    assert report.status == "stopped" and "sin saldo" in report.reason
    assert report.searches == 4                      # un solo lote, y se frena
    assert len([r for r in fake.requests if r.url.path.endswith("/runs")]) == 4
    assert report.usd_imputado == 0.0                # nada llegó a lanzarse
    assert TOKEN not in caplog.text
    with Session(engine) as s:
        assert s.exec(select(MlWebClaim)).all() == []


async def test_la_corrida_queda_en_el_audit_log(env, monkeypatch):
    _snaps(2)
    FakeApify().install(monkeypatch)

    await ams.run_search()

    with Session(engine) as s:
        (row,) = s.exec(select(AuditLog).where(AuditLog.action == "apify_ml_search_run")).all()
    data = json.loads(row.after)
    assert data["searches"] == 2 and data["results"] == 16
    # 20 publicaciones cobradas + el arranque, por corrida.
    assert math.isclose(data["usd_imputado"], 2 * (20 * 0.001 + 0.00005), rel_tol=1e-6)
    assert row.notified is True


# ─── La Mac y Apify no buscan lo mismo ─────────────────────────────


async def test_lo_que_tomo_la_mac_no_lo_busca_apify(env, monkeypatch):
    pids = _snaps()
    oficina_items = oficina_ml.queue_for_oficina(2)
    taken = {i["product_id"] for i in oficina_items}
    assert len(taken) == 2
    fake = FakeApify().install(monkeypatch)

    report = await ams.run_search()

    assert report.searches == 4
    assert {r.product_id for r in _rows()} == set(pids) - taken
    assert fake.starts == 4
    # Las reservas de la Mac siguen vivas; las de Apify se soltaron.
    with Session(engine) as s:
        assert {(c.product_id, c.claimed_by) for c in s.exec(select(MlWebClaim))} == {(p, "oficina") for p in taken}


def test_lo_que_tiene_apify_no_se_lo_da_a_la_mac(env):
    pids = _snaps(3)
    assert oficina_ml.claim([pids[0]], "apify-abcd1234", timedelta(minutes=30)) == [pids[0]]

    items = oficina_ml.queue_for_oficina(10)

    assert [i["product_id"] for i in items] == pids[1:]


def test_un_producto_lo_reserva_uno_solo(env):
    pids = _snaps(2)
    assert oficina_ml.claim(pids, "apify-aaaa", timedelta(minutes=30)) == pids
    assert oficina_ml.claim(pids, "oficina", timedelta(hours=3)) == []
    # El mismo dueño puede renovar (la Mac que vuelve a pedir la cola tras un corte).
    assert oficina_ml.claim(pids, "apify-aaaa", timedelta(minutes=30)) == pids
    # Vencida, la toma otro.
    later = utcnow() + timedelta(hours=1)
    assert oficina_ml.claim(pids[:1], "oficina", timedelta(hours=3), now=later) == pids[:1]


async def test_reserva_en_paralelo_uno_solo_gana(env):
    pids = _snaps(1)
    got = await asyncio.gather(*(asyncio.to_thread(oficina_ml.claim, pids, f"apify-{i:04d}", timedelta(minutes=5))
                                 for i in range(8)))
    assert sum(len(g) for g in got) == 1


def test_si_la_mac_reporta_bloqueo_suelta_todo(env):
    pids = _snaps(4)
    assert len(oficina_ml.queue_for_oficina(10)) == 4
    blocked = {"product_id": pids[0], "query": "q", "fetched_at": (utcnow() - timedelta(minutes=1)).isoformat() + "Z",
               "status": "blocked", "reason": "captcha", "candidates": []}

    oficina_ml.ingest([blocked])

    with Session(engine) as s:
        assert s.exec(select(MlWebClaim)).all() == []
    # Y Apify puede tomarlos esa misma noche.
    assert len(oficina_ml.build_queue(10, claimer="apify-x")) == 4


def test_lo_que_reporta_la_mac_se_suelta_y_lo_demas_sigue_reservado(env):
    pids = _snaps(3)
    oficina_ml.queue_for_oficina(10)
    ok = {"product_id": pids[0], "query": "q", "fetched_at": (utcnow() - timedelta(minutes=1)).isoformat() + "Z",
          "status": "empty", "reason": "", "candidates": []}

    oficina_ml.ingest([ok])

    with Session(engine) as s:
        assert {c.product_id for c in s.exec(select(MlWebClaim))} == set(pids[1:])


def test_la_ruta_de_la_mac_reserva_la_cola(env, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as main_mod
    from app.api import oficina_routes
    from app.config import Settings
    from tests.test_price_monitor_routes import _use_settings

    pids = _snaps(2)
    key = "oficina-key-" + "0123456789abcdef" * 2
    _use_settings(monkeypatch)
    settings = Settings(vendure_api_url="https://example.invalid/admin-api", oficina_search_key=key)
    monkeypatch.setattr(oficina_ml, "get_settings", lambda: settings)
    oficina_routes.reset_limits()

    resp = TestClient(main_mod.app).get("/api/oficina/ml-queue", headers={"x-oficina-key": key})

    assert resp.status_code == 200
    assert [i["product_id"] for i in resp.json()["items"]] == pids
    with Session(engine) as s:
        assert {(c.product_id, c.claimed_by) for c in s.exec(select(MlWebClaim))} == {(p, "oficina") for p in pids}
    oficina_routes.reset_limits()


# ─── Scheduler ─────────────────────────────────────────────────────


def test_el_job_queda_programado_de_madrugada(monkeypatch):
    from app.scheduler import jobs

    added = {}
    monkeypatch.setattr(jobs.scheduler, "add_job", lambda fn, trigger, id, **kw: added.setdefault(id, (fn, trigger)))
    jobs.register_jobs()

    fn, trigger = added[ams.JOB_ID]
    assert fn is jobs.apify_ml_search
    assert str(trigger) == str(jobs.CronTrigger.from_crontab("30 4 * * *", timezone="UTC"))


async def test_el_job_corre_apagado_sin_gastar(env, monkeypatch):
    from app.scheduler import jobs

    runtime.set_value("apify_meli_search_enabled", 0)
    fake = FakeApify().install(monkeypatch)

    out = await jobs.apify_ml_search()

    assert out["status"] == "disabled" and fake.requests == []
