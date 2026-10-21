"""QA de la búsqueda web de ML con Apify (feat/semaforo-busqueda-apify).

Completa test_apify_ml_search.py con lo que los criterios de aceptación piden y ahí no estaba:

  * dos corridas a la vez (como dos procesos: sin el candado del proceso) no se pasan del tope ni buscan dos veces lo mismo;
  * un reinicio a mitad de la noche: el cupo gastado se respeta y las reservas de la corrida muerta vencen solas;
  * la reserva de la Mac vence (una Mac muerta no deja la cola tomada para siempre) y su aviso de bloqueo la suelta;
  * de punta a punta: cola → Apify → ingest → load_fresh → veredicto del semáforo, IGUAL que si lo hubiera mandado la Mac;
  * salida del actor rota (tipos, números enormes, sin id, fotos de otro host, dataset que no es lista);
  * fallas: lanzamiento colgado, corrida FAILED, polls rotos → el producto queda en la cola y se imputa lo que corresponde.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import httpx
import pytest
from sqlmodel import Session, select

from app import runtime
from app.clock import utcnow
from app.db.models import MarketPriceSnapshot, MlWebClaim, MlWebResult
from app.db.session import engine
from app.pricing import apify_ml_search as ams
from app.pricing import oficina_ml, price_monitor
from tests.test_apify_ml_search import ACTOR, TOKEN, FakeApify, _item, _rows, _used, env
from tests.test_price_monitor import FakeVendure, _product, _snaps, world
from tests.test_semaforo_web import PHOTO


def _many(n: int, start: int = 500) -> list[str]:
    pids = [str(start + i) for i in range(n)]
    with Session(engine) as s:
        for pid in pids:
            s.add(MarketPriceSnapshot(run_id=1, product_id=pid, ml_status="no_data", product_name=f"Producto QA {pid}",
                                      product_enabled=True))
        s.commit()
    return pids


def _claims() -> set[tuple[str, str]]:
    with Session(engine) as s:
        return {(c.product_id, c.claimed_by) for c in s.exec(select(MlWebClaim))}


# ─── 2. el tope diario con dos corridas a la vez y con reinicios ─────────────


async def test_dos_corridas_a_la_vez_no_se_pasan_del_tope_ni_buscan_lo_mismo(env, monkeypatch):
    """Dos procesos (el candado del proceso no los frena): las reservas de la cola y el cupo atómico sí."""
    _many(12)
    runtime.set_value("apify_meli_search_daily_max", 5)
    fake = FakeApify().install(monkeypatch)

    a, b = ams.RunReport(), ams.RunReport()
    await asyncio.gather(ams._run(a, time.monotonic()), ams._run(b, time.monotonic()))

    assert _used() == 5
    assert fake.starts == 5 and a.searches + b.searches == 5
    queries = [q[0] for q in fake.queries.values()]
    assert len(set(queries)) == len(queries), "un producto se buscó dos veces"
    assert len({r.product_id for r in _rows()}) == 5
    assert _claims() == set()


async def test_dos_disparos_en_el_mismo_proceso_el_segundo_sale_busy(env, monkeypatch):
    _many(4)
    fake = FakeApify().install(monkeypatch)

    first, second = await asyncio.gather(ams.run_search("cron"), ams.run_search("manual"))

    assert sorted([first.status, second.status]) == ["busy", "ok"]
    assert fake.starts == 4 and _used() == 4


async def test_reinicio_a_mitad_de_la_noche_respeta_el_cupo_y_las_reservas_de_la_corrida_muerta(env, monkeypatch):
    """Un deploy mató la corrida: ya había reservado 3 de cupo y dejó 2 productos reservados (vencen a los 30 min)."""
    pids = _many(6)
    runtime.set_value("apify_meli_search_daily_max", 5)
    from app.pricing import daily_budget

    assert daily_budget.reserve_up_to(ams.COUNTER_KEY, 5, 3) == 3
    assert oficina_ml.claim(pids[:2], "apify-dead0001", ams._CLAIM_LEASE) == pids[:2]
    fake = FakeApify().install(monkeypatch)

    report = await ams.run_search()

    assert report.status == "over_budget" and fake.starts == 2 and _used() == 5
    searched = {r.product_id for r in _rows()}
    assert searched.isdisjoint(pids[:2]), "buscó lo que tenía reservado la corrida muerta"
    # Las reservas de la corrida muerta siguen ahí hasta vencer; las de esta se soltaron.
    assert _claims() == {(p, "apify-dead0001") for p in pids[:2]}
    later = utcnow() + ams._CLAIM_LEASE + timedelta(minutes=1)
    assert {i["product_id"] for i in oficina_ml.build_queue(50, now=later, claimer="apify-new")} >= set(pids[:2])


# ─── 3. la Mac y Apify ───────────────────────────────────────────────────────


async def test_la_reserva_de_la_mac_vence_y_apify_la_toma(env, monkeypatch):
    """Una Mac que pidió la cola y se murió (sin reportar nada) no deja la cola tomada más de 3 h."""
    pids = _many(3)
    taken = oficina_ml.queue_for_oficina(10, now=utcnow() - oficina_ml.OFICINA_CLAIM_LEASE - timedelta(minutes=1))
    assert [i["product_id"] for i in taken] == pids
    fake = FakeApify().install(monkeypatch)

    report = await ams.run_search()

    assert report.searches == 3 and fake.starts == 3
    assert {r.product_id for r in _rows()} == set(pids)


async def test_mac_viva_bloquea_a_apify_y_su_aviso_de_bloqueo_la_libera_esa_misma_noche(env, monkeypatch):
    pids = _many(3)
    assert len(oficina_ml.queue_for_oficina(10)) == 3
    fake = FakeApify().install(monkeypatch)

    first = await ams.run_search()
    assert first.searches == 0 and fake.starts == 0 and first.reason == "la cola está vacía"

    oficina_ml.ingest([{"product_id": pids[0], "query": "q", "status": "blocked", "reason": "captcha", "candidates": [],
                        "fetched_at": (utcnow() - timedelta(minutes=1)).isoformat() + "Z"}])
    second = await ams.run_search()

    assert second.searches == 3 and {r.product_id for r in _rows() if r.origin == "apify"} == set(pids)


async def test_el_resultado_de_apify_saca_el_producto_de_la_cola_de_la_mac(env, monkeypatch):
    _many(2)
    FakeApify().install(monkeypatch)
    await ams.run_search()
    assert oficina_ml.queue_for_oficina(10) == []


# ─── 4. de punta a punta: el semáforo no distingue de dónde vino ─────────────


def _actor_items(query: str, n: int) -> list[dict]:
    """El actor encontró el producto: MLA901 (a $250) con una foto que el CLIP da por igual."""
    return [_item(query, 0, n, ml_id="MLA901", name="Producto Raro Premium", price=250,
                  url_item="https://articulo.mercadolibre.com.ar/MLA-901-producto-raro",
                  thumbnail=PHOTO.format("901-MLA1_012025"), vendor_name="Tienda Uno")]


def _apify_on(monkeypatch):
    settings = ams.get_settings()
    monkeypatch.setattr(settings, "apify_token", TOKEN, raising=False)
    monkeypatch.setattr(settings, "apify_meli_search_actor", ACTOR, raising=False)
    monkeypatch.setattr(settings, "apify_meli_search_build", "1.0.103", raising=False)
    monkeypatch.setattr(settings, "apify_meli_search_timeout_s", 30, raising=False)
    monkeypatch.setattr(ams, "_POLL_INTERVAL_S", 0.0)
    runtime.set_value("apify_meli_search_enabled", 1)
    runtime.set_value("apify_meli_search_daily_max", 50)


@pytest.mark.parametrize("via", ["apify", "oficina"])
async def test_de_punta_a_punta_cola_apify_ingest_semaforo(world, monkeypatch, via):
    """La 1ª noche el semáforo no mide el producto; lo busca Apify (o la Mac, con lo MISMO) y la 2ª noche sale verde,
    con exactamente el mismo veredicto venga de donde venga."""
    with Session(engine) as s:
        for model in (MlWebResult, MlWebClaim):
            for row in s.exec(select(model)).all():
                s.delete(row)
        s.commit()
    FakeVendure.products = [_product("3", "Producto raro")]
    await price_monitor.run_price_monitor()
    assert _snaps()["3"].ml_status == "no_data"
    _apify_on(monkeypatch)

    if via == "apify":
        fake = FakeApify(items_for=_actor_items).install(monkeypatch)
        report = await ams.run_search()
        assert report.ok == 1 and fake.starts == 1
    else:
        (item,) = oficina_ml.queue_for_oficina(10)
        wires = [ams.to_wire(i) for i in _actor_items(item["queries"][0], 1)]
        oficina_ml.ingest([{"product_id": "3", "query": item["queries"][0], "status": "ok", "reason": "",
                            "candidates": wires, "fetched_at": (utcnow() - timedelta(minutes=1)).isoformat() + "Z"}])
    (row,) = [r for r in _rows() if r.product_id == "3"]
    assert row.origin == via
    assert set(oficina_ml.load_fresh()) == {"3"}

    world.image_scores[PHOTO.format("901-MLA1_012025")] = 0.93
    await price_monitor.run_price_monitor()

    snap = max((s for s in _all_snaps("3")), key=lambda s: s.id)
    assert (snap.ml_status, snap.color, snap.match_origin, snap.web_via) == ("ok", "verde", "oficina", "oficina")
    assert snap.ml_median_cents == 25_000 and snap.ml_listing_count == 1
    assert [m["ml_id"] for m in json.loads(snap.matched_listings)] == ["MLA901"]
    # El producto ya tiene precio: sale de la cola de las dos.
    assert oficina_ml.build_queue(10) == []


def _all_snaps(pid: str) -> list[MarketPriceSnapshot]:
    with Session(engine) as s:
        return list(s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.product_id == pid)))


# ─── 5. salida del actor rota ────────────────────────────────────────────────


@pytest.mark.parametrize("over", [
    {"price": 1e308}, {"price": "1e307"}, {"price": 10**400}, {"price": "1e400"}, {"price": "NaN"}, {"price": "-Infinity"},
    {"price": 0.004}, {"price": [100]}, {"price": {"amount": 100}},
    {"ml_id": None}, {"ml_id": ""}, {"ml_id": ["MLA1"]}, {"ml_id": "MLA" + "9" * 40},
    {"name": {"es": "x"}}, {"name": ["x"]}, {"name": 123},
    {"currency": 32}, {"currency": ["ARS"]},
    {"url_item": ["https://articulo.mercadolibre.com.ar/MLA-1"]}, {"url_item": "https://articulo.mercadolibre.com.ar@evil.com/x"},
    {"url_item": "https://" + "a" * 5000 + ".mercadolibre.com.ar/x"},
])
def test_mas_salidas_invalidas_se_descartan_sin_romper(over):
    assert ams.to_wire(_item("termo", 1, 1, **over)) is None


@pytest.mark.parametrize("over", [
    {"thumbnail": "https://evil.example.com/a.jpg", "image_item": "https://http2.mlstatic.com/D_1.jpg"},
    {"thumbnail": None, "image_item": [None, 5, {"u": 1}, "http://http2.mlstatic.com/D_1.jpg"]},
    {"thumbnail": "https://http2.mlstatic.com.evil.com/D_1.jpg"},
    {"thumbnail": "data:image/png;base64,AAAA"},
])
def test_foto_que_no_es_de_mlstatic_se_cae_pero_la_publicacion_queda(over):
    wire = ams.to_wire(_item("termo", 1, 1, **over))
    assert wire is not None and wire["image_urls"] == []


@pytest.mark.parametrize("over", [
    {"domain_id": {"x": 1}}, {"domain_id": "MLA-<script>"}, {"vendor_name": {"x": 1}, "seller": ["y"]},
    {"brand": 12345}, {"brand": None},
])
def test_campos_secundarios_rotos_no_tiran_la_publicacion(over):
    wire = ams.to_wire(_item("termo", 1, 1, **over))
    assert wire is not None
    assert isinstance(wire["domain_id"], str) and isinstance(wire["seller"], str) and isinstance(wire["brand"], str)
    assert "<" not in wire["domain_id"]


async def test_un_precio_gigante_no_le_hace_perder_al_producto_las_otras_publicaciones(env, monkeypatch):
    """1e308 es un float finito: x100 da infinito. No puede tirar la corrida entera (ya cobrada) del producto."""
    _many(1)

    def items(query, n):
        return [_item(query, 0, n, price=1e308), *[_item(query, k, n) for k in range(1, 5)]]

    FakeApify(items_for=items).install(monkeypatch)

    report = await ams.run_search()

    assert report.errors == 0 and report.ok == 1, report.as_dict()
    (row,) = _rows()
    assert row.status == "ok" and row.n_candidates == 4


@pytest.mark.parametrize("dataset", [{"items": []}, "texto", 12, None])
async def test_dataset_que_no_es_lista_es_error_y_el_producto_queda_en_la_cola(env, monkeypatch, dataset):
    pids = _many(1)
    fake = FakeApify().install(monkeypatch)
    real = fake.handler

    async def handler(request):
        if request.url.path.endswith("/items"):
            return httpx.Response(200, content=json.dumps(dataset).encode(), headers={"content-type": "application/json"})
        return await real(request)

    fake.handler = handler

    report = await ams.run_search()

    assert report.errors == 1 and report.usd_imputado == pytest.approx(0.02005)
    assert [i["product_id"] for i in oficina_ml.build_queue(10)] == pids
    assert _claims() == set()


async def test_dataset_se_pide_acotado_y_uno_enorme_no_rompe(env, monkeypatch):
    """Se piden 40 (`limit`); si el actor manda 5000 igual, el body se corta por tamaño: error, no excepción."""
    pids = _many(1)
    fake = FakeApify(items_for=lambda q, n: [_item(q, k, n) for k in range(5000)]).install(monkeypatch)
    report = await ams.run_search()
    items_req = next(r for r in fake.requests if r.url.path.endswith("/items"))
    assert items_req.url.params["limit"] == "40"
    assert report.errors == 1 and report.usd_imputado == pytest.approx(0.02005)
    assert [i["product_id"] for i in oficina_ml.build_queue(10)] == pids


async def test_dataset_de_60_con_basura_adelante_igual_encuentra_las_validas(env, monkeypatch):
    _many(1)
    FakeApify(items_for=lambda q, n: [*([None] * 30), *[_item(q, k, n) for k in range(10)]]).install(monkeypatch)
    report = await ams.run_search()
    assert report.ok == 1 and report.results == 8


# ─── 6. timeouts y fallas ────────────────────────────────────────────────────


async def test_lanzamiento_colgado_no_imputa_y_el_producto_queda_en_la_cola(env, monkeypatch):
    """Sin id de corrida no hay qué abortar ni cobro conocido."""
    pids = _many(1)
    monkeypatch.setattr(env, "apify_meli_search_timeout_s", 1)
    monkeypatch.setattr(ams, "_DEADLINE_MARGIN_S", 0.2)
    fake = FakeApify().install(monkeypatch)
    normal = fake.handler

    async def handler(request):
        if request.method == "GET" and request.url.path.startswith("/v2/acts/"):
            return await normal(request)            # el chequeo de permisos del actor contesta normal
        fake.requests.append(request)
        await asyncio.sleep(5)
        return httpx.Response(500)

    fake.handler = handler

    report = await ams.run_search()

    assert report.errors == 1 and report.usd_imputado == 0.0
    assert [i["product_id"] for i in oficina_ml.build_queue(10)] == pids
    assert _claims() == set()


async def test_corrida_failed_imputa_el_peor_caso_y_queda_en_la_cola(env, monkeypatch):
    pids = _many(1)
    fake = FakeApify("fail").install(monkeypatch)

    report = await ams.run_search()

    assert report.errors == 1 and fake.aborted == []
    assert report.usd_imputado == pytest.approx(0.02005)
    (row,) = _rows()
    assert row.status == "error" and "FAILED" in row.reason
    assert [i["product_id"] for i in oficina_ml.build_queue(10)] == pids


async def test_corrida_colgada_se_aborta_y_la_reserva_se_suelta(env, monkeypatch):
    _many(2)
    monkeypatch.setattr(env, "apify_meli_search_timeout_s", 1)
    monkeypatch.setattr(ams, "_DEADLINE_MARGIN_S", 0.2)
    fake = FakeApify("hang").install(monkeypatch)

    report = await ams.run_search()

    assert sorted(fake.aborted) == ["run1", "run2"]
    assert report.errors == 2 and _used() == 2
    assert _claims() == set()


async def test_seis_errores_seguidos_frenan_la_noche(env, monkeypatch):
    _many(12)
    fake = FakeApify("fail").install(monkeypatch)

    report = await ams.run_search()

    assert report.status == "stopped" and "seguidas" in report.reason
    assert fake.starts == 8 and report.searches == 8          # dos lotes de 4: 8 >= 6


@pytest.mark.parametrize("code", [401, 403, 404])
async def test_errores_fatales_frenan_en_el_primer_lote(env, monkeypatch, code):
    _many(8)
    fake = FakeApify(start_status=code).install(monkeypatch)
    report = await ams.run_search()
    assert report.status == "stopped" and fake.starts == 0 and report.searches == 4
    assert report.usd_imputado == 0.0


async def test_tope_de_dos_horas_no_lanza_otro_lote(env, monkeypatch):
    _many(8)
    fake = FakeApify().install(monkeypatch)
    monkeypatch.setattr(ams, "_JOB_MAX_S", -1)
    report = await ams.run_search()
    assert report.status == "stopped" and fake.starts == 0 and _used() == 0
    assert _claims() == set()


# ─── 1. apagado ──────────────────────────────────────────────────────────────


async def test_apagado_por_runtime_aunque_el_env_diga_otra_cosa(env, monkeypatch):
    _many(3)
    monkeypatch.setattr(env, "apify_meli_search_enabled", 1, raising=False)
    runtime.set_value("apify_meli_search_enabled", 0)
    fake = FakeApify().install(monkeypatch)
    report = await ams.run_search()
    assert report.status == "disabled" and fake.requests == [] and _used() == 0 and _claims() == set()


async def test_tope_cero_no_llama_ni_reserva(env, monkeypatch):
    _many(3)
    runtime.set_value("apify_meli_search_daily_max", 0)
    fake = FakeApify().install(monkeypatch)
    report = await ams.run_search()
    assert report.status == "over_budget" and fake.requests == [] and _used() == 0 and _claims() == set()
