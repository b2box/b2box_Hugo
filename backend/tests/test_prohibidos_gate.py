"""El freno de lo que no vendemos antes de CUALQUIER activación (pricing/price_gate.py y price_review_actions.py), en
`/verify` (api/routes.py) y en la restauración de duplicados.

  * un verde firme cuyo nombre cae en la lista NO se activa: queda en la bandeja con el motivo;
  * sin una lista completa (ni la de la nube ni el archivo), Hugo no activa nada solo;
  * una persona tampoco lo activa desde la bandeja;
  * `/verify` informa el motivo y, con `prohibidos_verify = 1`, no reenvía a buscar proveedor;
  * `include_disabled` hace que `/verify` compare también contra lo inactivo.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import delete
from sqlmodel import Session, select

from app import prohibidos
from app.api import routes
from app.db.models import AuditLog, LuisIntake, PriceReviewEvent
from app.db.session import engine
from app.pricing import price_gate
from app.pricing import price_review as pr
from app.pricing import price_review_actions as actions
from app.vendure.client import VendureProduct
from tests import review_decide as kit
from tests import review_world as w

GREEN_PRICES = (1_900_000, 1_950_000, 2_000_000)


@pytest.fixture
def fake(monkeypatch):
    f = kit.setup(monkeypatch, mode=2)
    w.set_setting("pm_gate_catalog_greens", 0)
    w.set_setting("pm_gate_measures_check", 0)
    prohibidos.reiniciar()
    with Session(engine) as s:
        s.execute(delete(LuisIntake))
        s.commit()
    yield f
    prohibidos.reiniciar()
    with Session(engine) as s:
        s.execute(delete(LuisIntake))
        s.commit()


def paco_upload(pid: str, name: str = "x", vendure_name: str | None = None) -> int:
    with Session(engine) as s:
        row = LuisIntake(offer_id=f"o{pid}", source_url=f"https://detail.1688.com/offer/{pid}.html", name=name,
                         vendure_name=vendure_name, image_url="https://cbu01.alicdn.com/x.jpg", vendure_product_id=pid,
                         uploaded_at=w.NOW - timedelta(days=1))
        s.add(row)
        s.commit()
        return int(row.id)  # type: ignore[arg-type]


def intake(pid: str) -> LuisIntake:
    with Session(engine) as s:
        return s.exec(select(LuisIntake).where(LuisIntake.vendure_product_id == pid)).one()


def events(item_id: int) -> list[PriceReviewEvent]:
    with Session(engine) as s:
        return list(s.exec(select(PriceReviewEvent).where(PriceReviewEvent.item_id == item_id)
                           .order_by(PriceReviewEvent.id)).all())  # type: ignore[arg-type]


def green(fake, pid: str, name: str, *, original: str = "x") -> int:
    run = w.make_run()
    w.add_snap(run, pid, color="verde", prices=GREEN_PRICES, enabled=False, name=name)
    fake.enabled[pid] = False
    fake.names[pid] = name
    paco_upload(pid, original)
    return run


def sin_lista(monkeypatch, tmp_path) -> None:
    """Ni nube ni archivo: solo queda el mínimo de código."""
    monkeypatch.setattr(prohibidos, "RUTA", tmp_path / "no-existe.yaml")
    prohibidos.reiniciar()


def test_los_codigos_del_porton_son_los_de_las_acciones():
    assert (price_gate.NOT_SOLD, price_gate.NO_LIST) == (actions.PROHIBIDO, actions.SIN_LISTA)


# ─── El portón (Hugo solo) ─────────────────────────────────────────


async def test_un_verde_firme_prohibido_no_se_activa_y_queda_en_la_bandeja_con_el_motivo(fake):
    run = green(fake, "p1", "Vitamina C 1000 mg x 60 cápsulas")
    counts = await price_gate.process_run(run, now=w.NOW)
    assert counts == {"bandeja": 1}
    assert fake.writes() == [] and fake.enabled["p1"] is False
    [item] = w.items()
    assert item.state == pr.POR_ACTIVAR and "porton:prohibido" in (item.flags or "")
    porton = next(e for e in events(int(item.id)) if e.kind == "porton")
    assert porton.motive_code == "prohibido" and "no vendemos suplementos (vitamina)" in (porton.motive_text or "")
    row = intake("p1")
    assert row.gate_status == "bandeja" and "no vendemos suplementos" in (row.gate_reason or "")


async def test_lo_frena_tambien_el_titulo_original_aunque_el_nombre_en_la_tienda_no_lo_diga(fake):
    run = green(fake, "p2", "Frasco surtido x 60", original="保健品 维生素 片")
    assert await price_gate.process_run(run, now=w.NOW) == {"bandeja": 1}
    assert fake.writes() == []


async def test_un_accesorio_que_nombra_un_consumible_si_se_activa(fake):
    run = green(fake, "p3", "Molinillo de café manual de acero")
    assert await price_gate.process_run(run, now=w.NOW) == {"activado": 1}
    assert fake.writes() == [True] and fake.enabled["p3"] is True


async def test_con_el_interruptor_en_cero_no_se_mira_la_lista(fake):
    w.set_setting("prohibidos_gate", 0)
    run = green(fake, "p4", "Vitamina C 1000 mg")
    assert await price_gate.process_run(run, now=w.NOW) == {"activado": 1}


async def test_sin_una_lista_completa_hugo_no_activa_nada_solo(fake, monkeypatch, tmp_path):
    sin_lista(monkeypatch, tmp_path)
    run = green(fake, "p5", "Mochila escolar impermeable")           # nada prohibido: igual no se activa
    counts = await price_gate.process_run(run, now=w.NOW)
    assert counts == {"bandeja": 1} and fake.writes() == []
    [item] = w.items()
    porton = next(e for e in events(int(item.id)) if e.kind == "porton")
    assert porton.motive_code == "sin_lista_prohibidos"
    assert "no se pudo cargar la lista" in (intake("p5").gate_reason or "")


async def test_si_el_filtro_mismo_falla_tampoco_se_activa(fake, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("sin base")

    monkeypatch.setattr(actions, "prohibido_check", boom)
    run = green(fake, "p6", "Mochila escolar impermeable")
    assert await price_gate.process_run(run, now=w.NOW) == {"bandeja": 1}
    assert fake.writes() == []


async def test_catalogo_apagado_el_prohibido_va_a_la_bandeja_y_el_resto_se_activa(fake):
    w.set_setting("pm_gate_catalog_greens", 1)
    run = w.make_run()
    for pid, name in (("c1", "Shampoo anticaspa 400 ml"), ("c2", "Organizador de cocina acrílico")):
        w.add_snap(run, pid, color="verde", prices=GREEN_PRICES, enabled=False, name=name)
        fake.enabled[pid] = False
        fake.names[pid] = name
    counts = await price_gate.process_run(run, now=w.NOW)
    assert counts == {"catalogo_prohibido_a_bandeja": 1, "catalogo_activado": 1}
    assert fake.enabled == {"c1": False, "c2": True}
    states = {i.product_id: i.state for i in w.items()}
    assert states == {"c1": pr.POR_ACTIVAR, "c2": pr.ACTIVADO}


async def test_catalogo_apagado_sin_lista_corta_la_pasada_sin_llenar_la_bandeja(fake, monkeypatch, tmp_path):
    w.set_setting("pm_gate_catalog_greens", 1)
    sin_lista(monkeypatch, tmp_path)
    run = w.make_run()
    for pid in ("c3", "c4", "c5"):
        w.add_snap(run, pid, color="verde", prices=GREEN_PRICES, enabled=False, name=f"Organizador {pid}")
        fake.enabled[pid] = False
    counts = await price_gate.process_run(run, now=w.NOW)
    assert counts == {"catalogo_sin_lista": 1}
    assert fake.writes() == [] and w.items() == []


async def test_una_ficha_que_ya_estaba_en_la_bandeja_recibe_el_motivo_una_sola_vez(fake):
    w.set_setting("pm_gate_catalog_greens", 1)
    run = w.make_run()
    w.add_snap(run, "c6", color="verde", prices=GREEN_PRICES, enabled=False, name="Perfume floral 50 ml")
    fake.enabled["c6"] = False
    await price_gate.process_run(run, now=w.NOW)
    await price_gate.process_run(run, now=w.NOW)
    [item] = w.items()
    assert [e.motive_code for e in events(int(item.id)) if e.kind == "porton"] == ["prohibido"]


# ─── La defensa de fondo: la única escritura de `enabled` ──────────


async def test_la_accion_automatica_se_frena_aunque_el_porton_no_haya_mirado(fake):
    """Si algún camino llegara a `auto_gate` sin pasar por la guarda del portón, la escritura igual no sale."""
    fake.enabled["d1"] = False
    fake.names["d1"] = "Insecticida concentrado 1 L"
    item_id = w.make_item("d1", pr.POR_ACTIVAR, open_key="d1")
    with pytest.raises(actions.ActionError) as exc:
        await actions.auto_gate(item_id, actions.ACTIVAR_AUTO, pr.POR_ACTIVAR)
    assert exc.value.status == 409 and exc.value.extra["reason"] == "prohibido"
    assert fake.writes() == [] and w.item(item_id).state == pr.POR_ACTIVAR
    last = events(item_id)[-1]
    assert (last.kind, last.motive_code) == ("reactivar_a_mano", "prohibido")
    assert "no vendemos quimicos (insecticida)" in (last.motive_text or "")


async def test_la_accion_automatica_se_frena_sin_lista_completa(fake, monkeypatch, tmp_path):
    sin_lista(monkeypatch, tmp_path)
    fake.enabled["d2"] = False
    item_id = w.make_item("d2", pr.POR_ACTIVAR, open_key="d2")
    with pytest.raises(actions.ActionError) as exc:
        await actions.auto_gate(item_id, actions.ACTIVAR_AUTO, pr.POR_ACTIVAR)
    assert exc.value.extra["reason"] == "sin_lista_prohibidos" and fake.writes() == []


def test_una_persona_tampoco_activa_un_prohibido_desde_la_bandeja(fake):
    fake.enabled["h1"] = False
    fake.names["h1"] = "Vaper desechable 5000 puff"
    item_id = w.make_item("h1", pr.POR_ACTIVAR, open_key="h1")
    r = kit.decide(kit.client(), item_id, "activar", pr.POR_ACTIVAR)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "prohibido" and "no vendemos tabaco y vapeo (vaper)" in detail["message"]
    assert fake.writes() == [] and fake.enabled["h1"] is False and w.item(item_id).state == pr.POR_ACTIVAR


def test_una_persona_si_activa_un_producto_normal_aunque_solo_quede_el_minimo(fake, monkeypatch, tmp_path):
    """Sin lista completa se niega Hugo SOLO; una persona decide con lo que hay (el mínimo sigue frenando lo suyo)."""
    sin_lista(monkeypatch, tmp_path)
    fake.enabled.update({"h2": False, "h3": False})
    fake.names.update({"h2": "Mochila escolar", "h3": "Creatina monohidrato 300 g"})
    ok = w.make_item("h2", pr.POR_ACTIVAR, open_key="h2")
    r = kit.decide(kit.client(), ok, "activar", pr.POR_ACTIVAR)
    assert r.status_code == 200, r.text
    assert fake.enabled["h2"] is True
    no = w.make_item("h3", pr.POR_ACTIVAR, open_key="h3")
    r = kit.decide(kit.client(), no, "activar", pr.POR_ACTIVAR)
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "prohibido"
    assert fake.enabled["h3"] is False


def test_apagar_nunca_lo_frena_la_lista(fake):
    fake.enabled["h4"] = True
    fake.names["h4"] = "Vitamina C"
    item_id = w.make_item("h4")
    r = kit.post(kit.client(), item_id, "approve", expected_state="propuesto")
    assert r.status_code == 200, r.text
    assert fake.enabled["h4"] is False


def test_restaurar_duplicados_no_prende_un_prohibido(fake):
    fake.enabled.update({"r1": False, "r2": False})
    with Session(engine) as s:
        s.add(AuditLog(action="duplicate_disabled", source="audit", product_id="r1", product_name="Pesticida 500 ml",
                       before='{"enabled": true}'))
        s.add(AuditLog(action="duplicate_disabled", source="audit", product_id="r2", product_name="Organizador",
                       before='{"enabled": true}'))
        s.commit()
    try:
        out = kit.client(kit.ADMIN).post("/api/restore-duplicates?confirm=true&safe=true").json()
        assert out["skipped_prohibido"] == 1 and out["restored"] == 1
        assert fake.enabled == {"r1": False, "r2": True}
    finally:
        with Session(engine) as s:
            s.execute(delete(AuditLog).where(AuditLog.product_id.in_(("r1", "r2", "(bulk)"))))  # type: ignore[attr-defined]
            s.commit()


# ─── /verify ───────────────────────────────────────────────────────

SOURCE_URL = "https://detail.1688.com/offer/777.html"


class FakePaco:
    submitted: list[dict] = []

    @staticmethod
    async def submit(image_url, product_url=None, **_kw):
        FakePaco.submitted.append({"image_url": image_url, "product_url": product_url})
        return routes.paco_integration.PacoSubmitResult(search_id="s-1", status="queued", raw={})


@pytest.fixture
def verify_env(monkeypatch, fake):
    FakePaco.submitted = []
    monkeypatch.setattr(routes.paco_integration, "submit", FakePaco.submit)

    def catalog(products: list[VendureProduct]) -> None:
        async def fake_get_catalog(force=False, full=False):
            return products

        monkeypatch.setattr(routes.vendure_catalog, "get_catalog", fake_get_catalog)

    catalog([])
    yield catalog
    with Session(engine) as s:
        s.execute(delete(AuditLog).where(AuditLog.product_source_url == SOURCE_URL))
        s.commit()


def _payload(**over) -> routes.VerifyRequest:
    data = dict(name="Vitaminas para el cabello x 60", source_url=SOURCE_URL, image_urls=["https://img/1.jpg"],
                source="orders")
    data.update(over)
    return routes.VerifyRequest(**data)


def _audit_actions() -> list[tuple[str, bool]]:
    with Session(engine) as s:
        return [(a.action, bool(a.dismissed)) for a in s.exec(
            select(AuditLog).where(AuditLog.product_source_url == SOURCE_URL).order_by(AuditLog.id)).all()]  # type: ignore[arg-type]


async def test_verify_informa_el_motivo_y_por_defecto_reenvia_como_siempre(verify_env):
    out = await routes.verify(_payload())
    assert out.prohibido == {"categoria": "suplementos", "palabra": "vitaminas",
                             "motivo": "no vendemos suplementos (vitaminas)"}
    assert out.paco_search_id == "s-1" and len(FakePaco.submitted) == 1


async def test_verify_con_el_interruptor_no_reenvia_y_lo_deja_anotado(verify_env):
    w.set_setting("prohibidos_verify", 1)
    out = await routes.verify(_payload())
    assert out.is_duplicate is False and out.paco_status == "prohibido" and out.paco_search_id is None
    assert out.prohibido and out.prohibido["categoria"] == "suplementos"
    assert FakePaco.submitted == []
    assert _audit_actions() == [("verify_prohibido", False)]


async def test_verify_dedup_only_con_el_interruptor_informa_y_archiva(verify_env):
    w.set_setting("prohibidos_verify", 1)
    out = await routes.verify(_payload(dedup_only=True))
    assert out.prohibido is not None and out.paco_status is None
    assert _audit_actions() == [("verify_prohibido", True)]


async def test_verify_no_frena_un_accesorio_ni_inventa_motivo(verify_env):
    w.set_setting("prohibidos_verify", 1)
    out = await routes.verify(_payload(name="Dispensador de jabón automático"))
    assert out.prohibido is None and out.paco_search_id == "s-1"


def _prod(pid: str, enabled: bool) -> VendureProduct:
    return VendureProduct(id=pid, name="Organizador de cocina", slug=f"p{pid}", description="", enabled=enabled,
                          source_url=SOURCE_URL, image_urls=[], product_code=None, featured_image_url=None,
                          first_variant_price_cents=100, variant_count=1)


async def test_verify_include_disabled_cuenta_lo_inactivo_y_dice_que_lo_es(verify_env, monkeypatch):
    class FakeVendure:
        async def get_product_full(self, pid):
            return {"id": pid, "name": "Organizador de cocina"}

    monkeypatch.setattr(routes, "VendureClient", FakeVendure)
    verify_env([_prod("7", enabled=False)])
    # Default: como siempre, lo inactivo no cuenta.
    out = await routes.verify(_payload(name="Organizador de cocina", dedup_only=True))
    assert out.is_duplicate is False and out.matched_enabled is None
    out = await routes.verify(_payload(name="Organizador de cocina", dedup_only=True, include_disabled=True))
    assert out.is_duplicate is True and out.candidate_id == "7" and out.matched_enabled is False
    assert FakePaco.submitted == []
    verify_env([_prod("7", enabled=False), _prod("8", enabled=True)])
    out = await routes.verify(_payload(name="Organizador de cocina", dedup_only=True, include_disabled=True))
    assert out.is_duplicate is True and out.candidate_id == "8" and out.matched_enabled is True
