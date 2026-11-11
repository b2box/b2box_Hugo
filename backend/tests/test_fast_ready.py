"""Vía rápida (pricing/fast_ready.py): un producto recién subido queda «listo para publicar» en minutos, sin activarse.

Sin red: Vendure es el falso de la bandeja, y la medición contra Mercado Libre, el catálogo y la IA de medidas se
reemplazan por dobles. Lo que se prueba es qué deja pasar la vía rápida, qué deja anotado y que NUNCA escribe en Vendure.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete
from sqlmodel import Session, select

from app import motor_eventos, prohibidos
from app.clock import utcnow
from app.db.models import ApifySpend, FastReady, LuisIntake, MarketPriceSnapshot, PriceReviewEvent
from app.db.session import engine
from app.pricing import fast_ready, measures_check, price_monitor
from app.pricing import price_review as pr
from app.vendure.client import VendureProduct
from tests import review_decide as kit
from tests import review_world as w
from tests.test_recalculo_al_cambiar import product as raw_product

GREEN_PRICES = (1_900_000, 1_950_000, 2_000_000)
YELLOW_PRICES = (1_350_000, 1_380_000, 1_400_000)


def link_of(pid: str) -> str:
    """Un link de oferta de proveedor (id numérico) propio de cada producto de prueba."""
    return f"https://detail.1688.com/offer/7{''.join(str(ord(c)) for c in pid)}.html"

URL = "/api/price-monitor/fast-ready"


class World:
    """Lo que la vía rápida ve alrededor: la medición, el catálogo, las medidas y los avisos."""

    def __init__(self, fake) -> None:
        self.fake = fake
        self.run = w.make_run()
        self.scratch = w.make_run(status="failed")        # donde se arman los snapshots de mentira
        self.snaps: dict[str, dict] = {}
        self.catalog: list[VendureProduct] = []
        self.measures = SimpleNamespace(verdict=measures_check.OK, motive=None, cost_usd=0.0002)
        self.measured: list[str] = []
        self.sent: list[dict] = []

    def upload(self, pid: str, *, name: str = "Organizador de cocina", enabled: bool = False, link: bool = True,
               **snap) -> None:
        """Un producto recién subido (apagado, con link de proveedor) y lo que va a dar su medición."""
        raw = raw_product(pid, 1_000_000, enabled=enabled, name=name)
        if link:
            raw["customFields"]["supplierLink"] = link_of(pid)
        self.fake.products.append(raw)
        self.fake.enabled[pid] = enabled
        self.fake.names[pid] = name
        snap.setdefault("color", "verde")
        snap.setdefault("prices", GREEN_PRICES)
        self.snaps[pid] = {"name": name, "enabled": enabled, **snap}

    def snapshot(self, pid: str, run_id: int) -> MarketPriceSnapshot:
        """El snapshot que devolvería la medición, SIN guardar."""
        w.add_snap(self.scratch, pid, **self.snaps[pid])
        with Session(engine) as s:
            saved = s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.run_id == self.scratch,
                                                             MarketPriceSnapshot.product_id == pid)).one()
            data = saved.model_dump(exclude={"id"})
            s.delete(saved)
            s.commit()
        return MarketPriceSnapshot(**{**data, "run_id": run_id, "captured_at": utcnow(), "data_at": utcnow()})


@pytest.fixture
def world(monkeypatch):
    fake = kit.setup(monkeypatch, mode=1)
    w.set_setting("fast_ready_enabled", 1)
    prohibidos.reiniciar()
    fast_ready.reset_for_tests()
    with Session(engine) as s:
        for model in (FastReady, LuisIntake):
            s.execute(delete(model))
        s.execute(delete(ApifySpend).where(ApifySpend.label == fast_ready.SPEND_LABEL))  # type: ignore[arg-type]
        s.commit()
    wd = World(fake)

    async def fake_measure(product, run_id):
        wd.measured.append(product.id)
        snap = wd.snapshot(product.id, run_id)
        if wd.snaps[product.id].get("unmeasurable"):
            snap.ml_status, snap.ml_error = price_monitor.SKIPPED, "sin precio en Vendure"
        return snap, wd.snaps[product.id].get("apify_usd", 0.0)

    async def fake_catalog(force=False, full=False):
        return wd.catalog

    async def fake_check(snap, **_kw):
        return wd.measures

    monkeypatch.setattr(fast_ready, "_measure", fake_measure)
    monkeypatch.setattr("app.vendure.catalog.get_catalog", fake_catalog)
    monkeypatch.setattr(measures_check, "check_snapshot", fake_check)
    monkeypatch.setattr(motor_eventos, "avisar", lambda *evs: wd.sent.extend(e for e in evs if e))
    monkeypatch.setattr(motor_eventos, "configurado", lambda: ("https://nube.example/x", "k"))
    yield wd
    fast_ready.reset_for_tests()
    with Session(engine) as s:
        for model in (FastReady, LuisIntake):
            s.execute(delete(model))
        s.commit()
    w.reset_db()                    # deja los settings y la bandeja como estaban: otras pruebas comparten la base


@pytest.fixture
def clean_after():
    """Para las pruebas que arman su propio mundo: lo mismo, al terminar."""
    yield
    w.reset_db()


def row(pid: str) -> FastReady:
    with Session(engine) as s:
        return s.exec(select(FastReady).where(FastReady.product_id == pid)).one()


def events(item_id: int) -> list[PriceReviewEvent]:
    with Session(engine) as s:
        return list(s.exec(select(PriceReviewEvent).where(PriceReviewEvent.item_id == item_id)
                           .order_by(PriceReviewEvent.id)).all())  # type: ignore[arg-type]


# ─── Apagada por defecto ───────────────────────────────────────────


async def test_nace_apagada_y_no_hace_nada(world):
    w.set_setting("fast_ready_enabled", 0)
    world.upload("n1")
    assert fast_ready.enabled() is False
    assert fast_ready.notify(["n1"]) == 0
    assert await fast_ready.process_one("n1") == "apagada"
    assert world.measured == [] and w.items() == []
    with Session(engine) as s:
        assert s.exec(select(FastReady)).all() == []


def test_el_default_del_codigo_es_apagada():
    from app.config import Settings

    fields = Settings.model_fields
    assert fields["fast_ready_enabled"].default == 0
    assert fields["motor_eventos_url"].default == "" and fields["motor_eventos_key"].default == ""


async def test_con_la_bandeja_en_modo_sombra_tampoco_corre(world):
    w.set_setting("pm_mode", 0)
    world.upload("n2")
    assert await fast_ready.process_one("n2") == "apagada" and world.measured == []


# ─── El camino entero ──────────────────────────────────────────────


async def test_verde_firme_con_todo_bien_queda_listo_y_no_se_activa(world):
    world.upload("f1", apify_usd=0.02)
    seen = utcnow() - timedelta(seconds=100)
    assert await fast_ready.process_one("f1", first_seen=seen) == "listo"
    # Nada se escribió en Vendure: sigue apagado.
    assert world.fake.mutations == [] and world.fake.enabled["f1"] is False
    # La ficha quedó en la bandeja, marcada.
    [item] = w.items()
    assert item.state == pr.POR_ACTIVAR and fast_ready.READY_FLAG in (item.flags or "")
    porton = next(e for e in events(int(item.id)) if e.kind == "porton")
    assert porton.motive_code == "listo_para_publicar" and "Listo para publicar" in (porton.motive_text or "")
    # El intento quedó anotado, con el tiempo desde el aviso de la subida.
    r = row("f1")
    assert (r.status, r.reason, r.color, r.item_id) == ("listo", "listo_para_publicar", "verde", item.id)
    assert r.seconds_to_finish is not None and 100 <= r.seconds_to_finish < 130
    assert r.apify_usd == 0.02 and r.measures_usd == 0.0002 and r.listing_count == 3
    # El snapshot quedó en la última corrida terminada y sus contadores lo cuentan.
    snap = w.snap_of(world.run, "f1")
    assert snap.id == r.snapshot_id and snap.product_enabled is False
    run = w.run_of(world.run)
    assert (run.total_products, run.processed, run.n_verde) == (1, 1, 1)


async def test_una_persona_lo_publica_con_un_click(world):
    world.upload("f2")
    assert await fast_ready.process_one("f2") == "listo"
    [item] = w.items()
    r = kit.decide(kit.client(), int(item.id), "activar", pr.POR_ACTIVAR)
    assert r.status_code == 200, r.text
    assert world.fake.writes() == [True] and world.fake.enabled["f2"] is True
    assert w.item(int(item.id)).state == pr.ACTIVADO


async def test_un_producto_se_procesa_una_sola_vez(world):
    world.upload("f3")
    assert await fast_ready.process_one("f3") == "listo"
    assert await fast_ready.process_one("f3") == "ya_medido"
    assert world.measured == ["f3"] and len(w.items()) == 1


# ─── Lo que NO llega a «listo» ─────────────────────────────────────


async def test_un_prohibido_no_gasta_la_medicion(world):
    world.upload("x1", name="Vitamina C 1000 mg x 60")
    assert await fast_ready.process_one("x1") == "prohibido"
    assert world.measured == [] and w.items() == []
    r = row("x1")
    assert (r.status, r.reason) == ("no_listo", "prohibido") and "no vendemos suplementos" in (r.reason_text or "")


async def test_sin_lista_completa_nada_queda_listo(world, monkeypatch, tmp_path):
    monkeypatch.setattr(prohibidos, "RUTA", tmp_path / "no-existe.yaml")
    prohibidos.reiniciar()
    world.upload("x2")
    assert await fast_ready.process_one("x2") == "sin_lista_prohibidos"
    assert world.measured == [] and w.items() == []


async def test_un_duplicado_de_un_producto_inactivo_no_queda_listo(world):
    world.upload("x3")
    world.catalog = [VendureProduct(id="900", name="Otro nombre", slug="otro", description="", enabled=False,
                                    source_url=link_of("x3"), image_urls=[], product_code=None,
                                    featured_image_url=None, first_variant_price_cents=100, variant_count=1)]
    assert await fast_ready.process_one("x3") == "duplicado"
    r = row("x3")
    assert (r.status, r.reason) == ("no_listo", "duplicado") and "#900" in (r.reason_text or "")
    assert w.items() == [] and world.fake.mutations == []


async def test_si_no_se_pueden_chequear_los_duplicados_no_hay_listo(world, monkeypatch):
    async def boom(force=False, full=False):
        raise RuntimeError("Vendure no responde")

    monkeypatch.setattr("app.vendure.catalog.get_catalog", boom)
    world.upload("x4")
    with pytest.raises(RuntimeError):
        await fast_ready.process_one("x4")
    assert row("x4").status == "error" and w.items() == []


@pytest.mark.parametrize("snap, reason", [
    ({"color": "amarillo", "prices": YELLOW_PRICES}, "amarillo"),
    ({"color": "sin_dato", "ml_status": "no_data"}, "sin_dato"),
    ({"color": "rojo"}, "rojo"),
    ({"color": "verde", "sources": ("llm", "llm", "llm")}, "verde_dudoso:fuentes"),
    ({"color": "verde", "prices": (4_000_000, 4_000_000, 4_000_000), "our": 500_000}, "precio_bajo"),
])
async def test_solo_el_verde_firme_queda_listo(world, snap, reason):
    world.upload("x5", **snap)
    assert await fast_ready.process_one("x5") == "no_verde_firme"
    r = row("x5")
    assert r.status == "no_listo" and (r.reason or "").startswith(reason)
    assert w.items() == [], "lo que no está listo no abre ficha: lo toma la noche"
    assert w.snap_of(world.run, "x5").color == snap["color"]            # la medición igual queda en el semáforo


@pytest.mark.parametrize("verdict, code", [(measures_check.MAL, "medidas_mal"), (measures_check.DUDOSO, "medidas_dudoso"),
                                           (measures_check.SIN_MEDIDAS, "medidas_sin_medidas"),
                                           (measures_check.ERROR, "medidas_error")])
async def test_sin_medidas_aprobadas_no_hay_listo_aunque_la_guarda_del_porton_este_apagada(world, verdict, code):
    w.set_setting("pm_gate_measures_check", 0)
    world.measures = SimpleNamespace(verdict=verdict, motive="la caja no cierra", cost_usd=0.0002)
    world.upload("x6")
    assert await fast_ready.process_one("x6") == "medidas"
    assert (row("x6").status, row("x6").reason) == ("no_listo", code) and w.items() == []


async def test_pendiente_en_el_portal_de_proveedores_no_queda_listo(world):
    world.upload("x7")
    world.fake.approval["x7"] = "pending"
    assert await fast_ready.process_one("x7") == "aprobacion_pendiente"
    assert row("x7").status == "no_listo" and w.items() == []


# ─── Lo que ni se mide ─────────────────────────────────────────────


async def test_lo_que_no_es_una_subida_nueva_se_omite(world):
    world.upload("o1", enabled=True)                          # ya publicado
    world.upload("o2", link=False)                            # sin link de proveedor ni fila del radar
    world.upload("o3")
    w.add_snap(world.run, "o3", color="verde", prices=GREEN_PRICES, enabled=False)      # ya medido
    world.upload("o4")
    w.make_item("o4", pr.POR_ACTIVAR, open_key="o4")           # ya tiene ficha
    got = [await fast_ready.process_one(pid) for pid in ("o1", "o2", "o3", "o4", "o5")]
    assert got == ["ya_activo", "no_es_subida", "ya_medido", "ya_en_bandeja", "no_existe"]
    assert world.measured == [] and {row(p).status for p in ("o1", "o2", "o5")} == {"omitido"}
    # Lo que ya se midió o ya está en la bandeja ni se anota: son los avisos de todos los días.
    with Session(engine) as s:
        assert s.exec(select(FastReady).where(FastReady.product_id.in_(("o3", "o4")))).all() == []  # type: ignore[attr-defined]


async def test_un_producto_del_radar_entra_aunque_no_tenga_link_de_1688(world):
    world.upload("o6", link=False)
    with Session(engine) as s:
        s.add(LuisIntake(offer_id="u-o6", source_url="https://tienda.example/p/1", name="Organizador",
                         image_url="https://cdn.example/x.jpg", vendure_product_id="o6"))
        s.commit()
    assert await fast_ready.process_one("o6") == "listo"


async def test_sin_una_corrida_terminada_no_hay_donde_guardar_la_medicion(world):
    with Session(engine) as s:
        s.execute(delete(price_monitor.PriceMonitorRun))
        s.commit()
    world.upload("o7")
    assert await fast_ready.process_one("o7") == "sin_corrida" and world.measured == []


async def test_tope_diario(world):
    w.set_setting("fast_ready_daily_max", 1)
    world.upload("t1")
    world.upload("t2")
    assert await fast_ready.process_one("t1") == "listo"
    assert await fast_ready.process_one("t2") == "tope_del_dia"
    assert world.measured == ["t1"] and row("t2").status == "omitido"


async def test_nunca_con_la_corrida_de_la_noche_en_curso(world):
    world.upload("t3")
    async with price_monitor.price_monitor_lock:
        assert await fast_ready.process_one("t3") == "corrida_en_curso"
    w.make_run(status="running", started=utcnow())
    assert await fast_ready.process_one("t3") == "corrida_en_curso"
    assert world.measured == []


async def test_un_producto_a_medio_subir_se_reintenta_con_el_proximo_aviso(world):
    world.upload("t4", unmeasurable="sin_precio")
    assert await fast_ready.process_one("t4") == "incompleto"
    assert row("t4").status == "pendiente" and w.items() == []
    with Session(engine) as s:
        assert s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.product_id == "t4")).all() == []
    world.snaps["t4"].pop("unmeasurable")                     # terminó de subirse
    assert await fast_ready.process_one("t4") == "listo"
    # Y uno que nunca se completa deja de intentarse.
    world.upload("t5", unmeasurable="sin_foto")
    got = [await fast_ready.process_one("t5") for _ in range(fast_ready.MAX_ATTEMPTS + 1)]
    assert got == ["incompleto"] * fast_ready.MAX_ATTEMPTS + ["ya_visto"]


async def test_si_no_se_puede_medir_ahora_no_se_guarda_nada_y_se_reintenta(world, monkeypatch):
    """Recién reiniciado el índice de fotos no está listo (o no queda cupo de Mercado Libre): no es «sin dato»."""
    world.upload("t6")
    real = fast_ready._measure

    async def skipped(product, run_id):
        snap, usd = await real(product, run_id)
        snap.ml_status, snap.ml_error, snap.color = price_monitor.SKIPPED, "sin fotos en el índice CLIP", "sin_dato"
        return snap, usd

    with monkeypatch.context() as m:
        m.setattr(fast_ready, "_measure", skipped)
        assert await fast_ready.process_one("t6") == "no_medible_ahora"
    assert (row("t6").status, row("t6").reason) == ("pendiente", "no_medible_ahora") and w.items() == []
    with Session(engine) as s:
        assert s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.product_id == "t6")).all() == []
    assert await fast_ready.process_one("t6") == "listo"


# ─── Avisos de Vendure ─────────────────────────────────────────────


async def test_el_aviso_espera_a_que_la_subida_termine_y_despues_mide(world, monkeypatch):
    monkeypatch.setattr(fast_ready, "_is_leader", lambda: True)
    monkeypatch.setattr(fast_ready, "_start_worker", lambda: None)
    world.upload("a1")
    t0 = utcnow()
    assert fast_ready.notify(["a1", "a1"], now=t0) == 1
    assert fast_ready.notify(["a1"], now=t0 + timedelta(seconds=30)) == 1      # otro aviso: se corre la espera
    assert await fast_ready.process_due(now=t0 + timedelta(seconds=50)) == {}
    assert await fast_ready.process_due(now=t0 + timedelta(seconds=80)) == {"listo": 1}
    # El tiempo se cuenta desde el PRIMER aviso.
    assert row("a1").first_seen_at == t0


async def test_un_aviso_en_una_instancia_que_no_es_la_lider_no_hace_nada(world, monkeypatch):
    monkeypatch.setattr(fast_ready, "_is_leader", lambda: False)
    assert fast_ready.notify(["a2"]) == 0


def test_el_endpoint_de_avisos_llama_a_la_via_rapida(world, monkeypatch):
    from app.api import vendure_events_routes as ver

    got: list[list[str]] = []
    monkeypatch.setattr(ver.fast_ready, "notify", lambda ids: got.append(list(ids)) or 0)
    monkeypatch.setattr(ver, "configured_key", lambda: "k" * 40)

    async def nothing(*_a, **_k):
        return {}

    monkeypatch.setattr(ver.luis_intake, "compare_products", nothing)
    c = kit.client(None)
    r = c.post("/api/vendure/events", json={"events": [{"type": "product", "productId": "77"}]},
               headers={ver.HEADER: "k" * 40})
    assert r.status_code == 202, r.text
    assert got == [["77"]]


# ─── El hilo y los avisos al tablero ───────────────────────────────


async def test_con_hilo_avisa_medido_y_listo(world):
    world.upload("h1", apify_usd=0.02)
    with Session(engine) as s:
        s.add(LuisIntake(offer_id="oh1", source_url=link_of("h1"), name="Organizador", hilo="luis-777",
                         image_url="https://cdn.example/x.jpg", vendure_product_id="h1"))
        s.commit()
    assert await fast_ready.process_one("h1") == "listo"
    assert [(e["paso"], e["estado"], e["hilo"], e["ref"]) for e in world.sent] == [
        ("medido", "ok", "luis-777", "h1"), ("listo", "ok", "luis-777", "h1")]
    assert world.sent[0]["costo_usd"] == 0.02 and world.sent[0]["ms"] >= 0
    assert row("h1").hilo == "luis-777"
    # Y al publicarlo una persona, avisa `publicado` sin decir quién.
    [item] = w.items()
    assert kit.decide(kit.client(), int(item.id), "activar", pr.POR_ACTIVAR).status_code == 200
    last = world.sent[-1]
    assert (last["paso"], last["hilo"], last["ref"]) == ("publicado", "luis-777", "h1")
    assert "@" not in last["motivo"] and "persona" in last["motivo"]


async def test_lo_frenado_avisa_con_su_motivo_y_sin_hilo_no_avisa_nada(world):
    world.upload("h2", color="amarillo", prices=YELLOW_PRICES)
    with Session(engine) as s:
        s.add(LuisIntake(offer_id="oh2", source_url=link_of("h2"), name="Organizador", hilo="luis-778",
                         image_url="https://cdn.example/x.jpg", vendure_product_id="h2"))
        s.commit()
    await fast_ready.process_one("h2")
    assert [(e["paso"], e["estado"]) for e in world.sent] == [("medido", "ok"), ("listo", "frenado")]
    assert world.sent[1]["motivo"] == "amarillo"
    world.sent.clear()
    world.upload("h3")
    assert await fast_ready.process_one("h3") == "listo"
    assert world.sent == []


# ─── Lo que se ve ──────────────────────────────────────────────────


async def test_el_informe_dice_cuantos_llegaron_a_listo_y_en_cuanto(world):
    world.upload("r1")
    world.upload("r2")
    world.upload("r3", color="amarillo", prices=YELLOW_PRICES)
    await fast_ready.process_one("r1", first_seen=utcnow() - timedelta(seconds=120))
    await fast_ready.process_one("r2", first_seen=utcnow() - timedelta(seconds=400))
    await fast_ready.process_one("r3")
    out = fast_ready.report(7)
    assert out["enabled"] is True and out["total"] == 3
    assert out["by_status"] == {"listo": 2, "no_listo": 1} and out["not_ready_reasons"] == {"amarillo": 1}
    assert out["ready"]["count"] == 2 and out["ready"]["under_target"] == 1 and out["ready"]["target_seconds"] == 300
    assert 120 <= out["ready"]["median_seconds"] and out["ready"]["max_seconds"] >= 400
    assert out["used_today"] == 3 and [i["product_id"] for i in out["items"]][0] in ("r1", "r2", "r3")


async def test_el_endpoint_es_solo_para_un_administrador(world):
    assert kit.client(None).get(URL).status_code == 401
    assert kit.client().get(URL).status_code == 403
    world.upload("r4")
    await fast_ready.process_one("r4")
    body = kit.client(kit.ADMIN).get(f"{URL}?days=1").json()
    assert body["total"] == 1 and body["items"][0]["status"] == "listo" and world.fake.mutations == []


# ─── La medición de verdad (sin los dobles de arriba) ──────────────


async def test_la_medicion_prueba_la_api_y_despues_una_busqueda_paga(monkeypatch, clean_after):
    kit.setup(monkeypatch, mode=1)
    from app.dedup import catalog_index

    calls: list[dict] = []
    fresh = SimpleNamespace(product_id="m1")

    async def fake_eval(ctx, product):
        calls.append({"api": ctx.ml_api, "oficina": dict(ctx.oficina), "judge": ctx.judge_max_calls})
        status = price_monitor.NO_DATA if ctx.ml_api else price_monitor.OK
        return MarketPriceSnapshot(run_id=ctx.run_id, product_id=product.id, ml_status=status)

    async def fake_search(product, http=None):
        return fresh, 0.021

    async def noop():
        return None

    monkeypatch.setattr(price_monitor, "_evaluate_ml", fake_eval)
    monkeypatch.setattr(fast_ready, "_web_search", fake_search)
    monkeypatch.setattr(catalog_index, "ensure_index", noop)
    snap, usd = await fast_ready._measure(SimpleNamespace(id="m1", name="Organizador"), 5)
    assert (snap.ml_status, usd) == (price_monitor.OK, 0.021)
    assert calls == [{"api": True, "oficina": {}, "judge": 0}, {"api": False, "oficina": {"m1": fresh}, "judge": 0}]


async def test_si_la_api_alcanza_no_se_paga_la_busqueda(monkeypatch, clean_after):
    kit.setup(monkeypatch, mode=1)
    from app.dedup import catalog_index

    searched: list[str] = []

    async def fake_eval(ctx, product):
        return MarketPriceSnapshot(run_id=ctx.run_id, product_id=product.id, ml_status=price_monitor.OK)

    async def fake_search(product, http=None):
        searched.append(product.id)
        return None, 0.0

    async def noop():
        return None

    monkeypatch.setattr(price_monitor, "_evaluate_ml", fake_eval)
    monkeypatch.setattr(fast_ready, "_web_search", fake_search)
    monkeypatch.setattr(catalog_index, "ensure_index", noop)
    snap, usd = await fast_ready._measure(SimpleNamespace(id="m2", name="Organizador"), 5)
    assert snap.ml_status == price_monitor.OK and usd == 0.0 and searched == []


async def test_la_busqueda_paga_tiene_cupo_propio_y_deja_su_gasto(monkeypatch, clean_after):
    kit.setup(monkeypatch, mode=1)
    from app.config import get_settings
    from app.pricing import apify_ml_search as ams
    from app.pricing import oficina_ml

    with Session(engine) as s:
        s.execute(delete(ApifySpend).where(ApifySpend.label == fast_ready.SPEND_LABEL))  # type: ignore[arg-type]
        s.commit()
    product = SimpleNamespace(id="901", name="Organizador de cocina acrílico")
    # Sin token no se busca ni se gasta.
    monkeypatch.setattr(get_settings(), "apify_token", "", raising=False)
    assert fast_ready.apify_reason() == "falta APIFY_TOKEN"
    assert await fast_ready._web_search(product) == (None, 0.0)
    monkeypatch.setattr(get_settings(), "apify_token", "token-de-prueba", raising=False)
    w.set_setting("fast_ready_apify_daily_max", 0)
    assert "sin cupo" in (fast_ready.apify_reason() or "")
    w.set_setting("fast_ready_apify_daily_max", 1)
    assert fast_ready.apify_reason() is None

    searches: list[str] = []

    async def permission(http, actor, headers):
        return ams._REQUIRED_PERMISSION

    async def search_one(http, product_id, query, **_kw):
        searches.append(query)
        return ams.SearchOutcome(product_id, query, "empty", run_id="run-1", usd_imputado=0.02005)

    monkeypatch.setattr(ams, "_actor_permission", permission)
    monkeypatch.setattr(ams, "search_one", search_one)
    monkeypatch.setattr(oficina_ml, "claim", lambda ids, claimer, lease, **_k: list(ids))
    monkeypatch.setattr(oficina_ml, "release", lambda *a, **k: 0)
    monkeypatch.setattr(oficina_ml, "ingest", lambda items, **_k: None)
    monkeypatch.setattr(oficina_ml, "load_fresh", lambda **_k: {"901": "fresco"})
    http = SimpleNamespace()
    assert await fast_ready._web_search(product, http=http) == ("fresco", 0.02005)
    assert searches == ["Organizador de cocina acrílico"]
    with Session(engine) as s:
        [spend] = s.exec(select(ApifySpend).where(ApifySpend.label == fast_ready.SPEND_LABEL)).all()
    assert (spend.usd, spend.estimated, spend.run_id) == (0.02005, True, "run-1")
    # El cupo del día era 1: la segunda no sale.
    assert await fast_ready._web_search(product, http=http) == (None, 0.0)
    assert len(searches) == 1
    with Session(engine) as s:
        s.execute(delete(ApifySpend).where(ApifySpend.label == fast_ready.SPEND_LABEL))  # type: ignore[arg-type]
        s.commit()


async def test_un_intento_que_no_termina_a_tiempo_se_corta(world, monkeypatch):
    monkeypatch.setattr(fast_ready, "DEADLINE_S", 0.05)

    async def slow(product, run_id):
        await asyncio.Event().wait()            # nunca termina (el `sleep` de la suite no duerme)

    monkeypatch.setattr(fast_ready, "_measure", slow)
    world.upload("s1")
    assert await fast_ready.process_one("s1") == "sin_tiempo"
    assert (row("s1").status, row("s1").reason) == ("error", "sin_tiempo") and w.items() == []
