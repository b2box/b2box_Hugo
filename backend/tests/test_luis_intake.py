"""Lo que manda Luis: Google Lens al recibirlo y semáforo al subirlo a Vendure (pestaña «Subidos a Vendure»)."""

from __future__ import annotations

import json
import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import runtime  # noqa: E402
from app.api import routes  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.models import AuditLog, LuisIntake, Setting  # noqa: E402
from app.db.session import engine, init_db  # noqa: E402
from app.dedup.orchestrator import DedupVerdict  # noqa: E402
from app.pricing import apify_ml_search as ams  # noqa: E402
from app.pricing import daily_budget, luis_intake, ml_lens, semaforo  # noqa: E402
from app.vendure.client import VendureProduct  # noqa: E402

SOURCE_URL = "https://detail.1688.com/offer/700100.html"
PHOTO = "https://cbu01.alicdn.com/img/ibank/foto.jpg"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    init_db()
    with Session(engine) as s:
        for model in (LuisIntake, AuditLog, Setting):
            for row in s.exec(select(model)).all():
                s.delete(row)
        s.commit()
    monkeypatch.setattr(get_settings(), "apify_token", "apify_api_TOKEN_de_prueba", raising=False)
    runtime.invalidate()
    yield
    # Sin filas de Luis: si quedaran, los tests de los avisos de Vendure leerían Vendure para compararlas.
    with Session(engine) as s:
        for model in (LuisIntake, Setting):
            for row in s.exec(select(model)).all():
                s.delete(row)
        s.commit()
    runtime.invalidate()


def _lens_item(*matches: dict) -> list[dict]:
    return [{"input_index": 0, "matches": list(matches)}]


def _ml(price: str, currency: str = "ARS", url: str = "https://articulo.mercadolibre.com.ar/MLA-1-termo-_JM",
        title: str = "Termo acero 1 L") -> dict:
    return {"source_domain": "Mercado Libre", "price": price, "currency": currency, "url": url, "title": title,
            "link_is_redirect": False}


@pytest.fixture
def fake_lens(monkeypatch):
    calls: list[list[str]] = []
    state = {"items": _lens_item(_ml("$ 20.000"), _ml("$ 30.000"), _ml("$ 40.000"))}

    async def drive(step, run, urls):  # noqa: ARG001
        calls.append(list(urls))
        run.usage_usd = 0.005
        return state["items"]

    async def permission(http, actor, headers):  # noqa: ARG001
        return ams._REQUIRED_PERMISSION

    monkeypatch.setattr(ml_lens, "_drive", drive)
    monkeypatch.setattr(ams, "_actor_permission", permission)
    return calls, state


# ── Precios y matches ────────────────────────────────────────────────────────

@pytest.mark.parametrize("price, currency, cents", [
    ("$ 12.999", "ARS", 1_299_900),
    ("$12.999,50", "ARS", 1_299_950),
    ("ARS 1.250.000", "", 125_000_000),
    ("$ 15,99", "ARS", 1_599),
    ("US$ 20", "USD", None),
    ("", "ARS", None),
    ("consultar", "ARS", None),
])
def test_parse_price_cents(price, currency, cents):
    assert luis_intake.parse_price_cents(price, currency) == cents


def test_solo_quedan_publicaciones_de_ml_argentina_con_precio():
    out = luis_intake.ml_matches([
        _ml("$ 20.000"),
        _ml("$ 900", currency="MXN", url="https://articulo.mercadolibre.com.mx/MLM-1"),
        _ml("$ 900", currency="", url="https://articulo.mercadolibre.com.mx/MLM-2"),   # "$" sin AR: no
        {"source_domain": "Amazon", "price": "$ 10", "currency": "ARS", "url": "https://amazon.com/x", "title": "x"},
        _ml("sin precio"),
        _ml("$ 25.000", url="https://www.google.com/goto?u=x", title="<b>Termo</b>") | {"link_is_redirect": True},
    ])
    assert [m["price_cents"] for m in out] == [2_000_000, 2_500_000]
    assert out[0]["url"].startswith("https://articulo.mercadolibre.com.ar/") and out[1]["url"] is None


# ── Al recibirlo de Luis ─────────────────────────────────────────────────────

def test_record_es_idempotente_por_oferta_y_exige_link_de_1688_y_foto():
    first = luis_intake.record(SOURCE_URL, "Termo", [PHOTO])
    again = luis_intake.record("https://detail.1688.com/offer/700100.html?spm=x", "Termo acero", [PHOTO])
    assert first is not None and first == again
    assert luis_intake.record("https://www.amazon.com/dp/1", "x", [PHOTO]) is None
    assert luis_intake.record(SOURCE_URL, "x", ["http://127.0.0.1/a.jpg"]) is None


@pytest.mark.asyncio
async def test_lens_guarda_las_publicaciones_y_la_mediana(fake_lens):
    calls, _ = fake_lens
    intake_id = luis_intake.record(SOURCE_URL, "Termo", [PHOTO])
    assert await luis_intake.lens_one(intake_id) == luis_intake.LENS_OK
    assert calls == [[PHOTO]]
    with Session(engine) as s:
        row = s.get(LuisIntake, intake_id)
    assert row.ml_median_cents == 3_000_000 and len(json.loads(row.ml_matches)) == 3
    # Ya tiene Lens: no se paga otra vez.
    assert await luis_intake.lens_one(intake_id) == luis_intake.LENS_OK and len(calls) == 1


@pytest.mark.asyncio
async def test_lens_apagado_o_sin_cupo_no_gasta(fake_lens):
    calls, _ = fake_lens
    intake_id = luis_intake.record(SOURCE_URL, "Termo", [PHOTO])
    runtime.set_value("luis_lens_enabled", 0)
    assert await luis_intake.lens_one(intake_id) == luis_intake.LENS_OFF
    runtime.set_value("luis_lens_enabled", 1)
    runtime.set_value("luis_lens_daily_max", 0)
    with Session(engine) as s:
        s.get(LuisIntake, intake_id).lens_status = luis_intake.LENS_PENDING
        s.commit()
    assert await luis_intake.lens_one(intake_id) == luis_intake.LENS_PENDING
    assert calls == [] and daily_budget.used_today(luis_intake.COUNTER_KEY) == 0


@pytest.mark.asyncio
async def test_verify_de_luis_sin_match_deja_la_fila(monkeypatch):
    async def fake_catalog(force=False, full=False):  # noqa: ARG001
        return []

    async def fake_find(candidate, existing):  # noqa: ARG001
        return DedupVerdict(is_duplicate=False, confidence=0.0)

    started: list[int] = []

    async def fake_soon(intake_id):
        started.append(intake_id)

    monkeypatch.setattr(routes.vendure_catalog, "get_catalog", fake_catalog)
    monkeypatch.setattr(routes, "find_duplicate_in", fake_find)
    monkeypatch.setattr(routes.luis_intake, "lens_soon", fake_soon)
    req = routes.VerifyRequest(name="Termo acero", source_url=SOURCE_URL, image_urls=[PHOTO], source="luis",
                               dedup_only=True)
    await routes.verify(req)
    for task in list(routes._TASKS):
        await task
    with Session(engine) as s:
        [row] = s.exec(select(LuisIntake)).all()
    assert row.offer_id == "700100" and started == [row.id]
    # Otro cliente (Paco PRO) no deja fila.
    await routes.verify(routes.VerifyRequest(name="x", source_url="https://detail.1688.com/offer/900900.html",
                                             image_urls=[PHOTO], source="paco_pro", dedup_only=True))
    with Session(engine) as s:
        assert len(s.exec(select(LuisIntake)).all()) == 1


# ── Al subirlo a Vendure ─────────────────────────────────────────────────────

class FakeVendure:
    def __init__(self, price_cents: int | None, source_url: str = SOURCE_URL) -> None:
        self.price_cents, self.source_url = price_cents, source_url

    async def get_product_priced(self, pid: str) -> VendureProduct:
        variants = ([semaforo.PricedVariant(id="v1", name="Termo", sku="BX1", price_with_tax_cents=self.price_cents,
                                            currency="ARS")] if self.price_cents else [])
        return VendureProduct(id=pid, name="Termo acero inoxidable", slug="termo", description="", enabled=False,
                              source_url=self.source_url, image_urls=[], product_code="BX1",
                              featured_image_url="https://vendure/termo.jpg", first_variant_price_cents=None,
                              variant_count=1, priced_variants=variants)


@pytest.mark.asyncio
async def test_al_subirlo_compara_contra_lens_con_las_reglas_del_semaforo(fake_lens):
    intake_id = luis_intake.record(SOURCE_URL, "Termo", [PHOTO])
    await luis_intake.lens_one(intake_id)
    # Mediana ML 30.000: a 15.000 queda margen alto (verde); a 35.000 es más caro que ML (rojo).
    counts = await luis_intake.compare_products(["55"], client=FakeVendure(1_500_000))
    assert counts == {luis_intake.COMPARED: 1}
    [item] = luis_intake.list_rows()
    assert item["vendure_product_id"] == "55" and item["color"] == semaforo.VERDE and item["margin_pct"] > 0
    assert item["name"] == "Termo acero inoxidable" and item["image_url"] == "https://vendure/termo.jpg"
    await luis_intake.compare_products(["55"], client=FakeVendure(3_500_000))
    assert luis_intake.list_rows()[0]["color"] == semaforo.ROJO


@pytest.mark.asyncio
async def test_sin_publicaciones_de_ml_queda_sin_dato_y_otro_producto_no_se_toca(fake_lens):
    _, state = fake_lens
    state["items"] = _lens_item()
    intake_id = luis_intake.record(SOURCE_URL, "Termo", [PHOTO])
    assert await luis_intake.lens_one(intake_id) == luis_intake.LENS_EMPTY
    await luis_intake.compare_products(["55"], client=FakeVendure(1_500_000))
    assert luis_intake.list_rows()[0]["color"] == semaforo.SIN_DATO
    counts = await luis_intake.compare_products(
        ["66"], client=FakeVendure(1_500_000, source_url="https://detail.1688.com/offer/123123.html"))
    assert counts == {"no_es_de_luis": 1}


@pytest.mark.asyncio
async def test_sin_precio_en_vendure_queda_marcado(fake_lens):
    intake_id = luis_intake.record(SOURCE_URL, "Termo", [PHOTO])
    await luis_intake.lens_one(intake_id)
    assert await luis_intake.compare_products(["55"], client=FakeVendure(None)) == {luis_intake.NO_PRICE: 1}


@pytest.mark.asyncio
async def test_el_listado_filtra_los_subidos():
    luis_intake.record(SOURCE_URL, "Termo", [PHOTO])
    assert len(luis_intake.list_rows()) == 1 and luis_intake.list_rows(only_uploaded=True) == []
