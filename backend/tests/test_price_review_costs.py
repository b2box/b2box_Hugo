"""M1: el costo de fábrica y el fee de B2BOX los ve solo quien está en PRICE_REVIEW_VIEWERS (fail-closed).

Dos sesiones sobre las mismas fichas: la responsable ve el detalle completo y la otra no recibe, en NINGÚN lugar de la
respuesta, el costo de fábrica (yuanes, FOB, componente «fábrica»), el fee en % ni las evidencias con costos por tramo."""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app import main as main_mod  # noqa: E402
from app.api import price_review_routes as routes  # noqa: E402
from app.config import Settings  # noqa: E402
from tests import review_world as w  # noqa: E402

from tests.test_price_review_api import SPECS, open_items, world  # noqa: E402,F401  (world: fixture autouse)

BASE = "/api/price-review"
RESPONSIBLE = "responsable@b2box.pro"
OTHER = "otra.persona@b2box.pro"


def session(email: str) -> TestClient:
    c = TestClient(main_mod.app, raise_server_exceptions=False)
    c.cookies.set(auth.COOKIE_NAME, auth.issue_session_token(email))
    return c


def set_viewers(monkeypatch, value: str) -> None:
    cfg = Settings(vendure_api_url="https://example.invalid/admin-api", price_review_viewers=value)
    monkeypatch.setattr(routes, "get_settings", lambda: cfg)


def first_item_id() -> int:
    return w.items()[0].id


# El tramo de 25 unidades del organizador: fábrica 149,84 USD, ¥42 por unidad, fee de 20 %.
FOB_USD = "149.84"


async def test_the_responsible_session_sees_the_factory_cost_and_the_fee(monkeypatch):
    set_viewers(monkeypatch, RESPONSIBLE)
    await open_items(["a"])
    d = session(RESPONSIBLE).get(f"{BASE}/items/{first_item_id()}").json()
    assert d["costs_visible"] is True
    bd = d["breakdown"]
    assert bd["cny_unit"] == 42.0 and bd["fee_pct"] == 20.0 and bd["factura_usd"] > 0 and bd["neto_usd"] > 0
    assert "restricted" not in bd and "fabrica" in {c["key"] for c in bd["components"]}
    fab = next(c for c in bd["components"] if c["key"] == "fabrica")
    assert fab["usd"] == pytest.approx(float(FOB_USD)) and fab["pct"] is not None and fab["ars_unit"] is not None
    assert all(t["cny_unit"] is not None for t in d["tiers"])
    rules = {r["code"]: r for r in d["diagnosis"]["rules"]}
    assert rules["e"]["evidence"]["target_cny"] is not None or rules["e"]["evidence"]["free_factory_not_enough"]
    assert rules["b"]["evidence"]["tiers"][0]["cny_cents"] == 4200
    assert "USD" in rules["d"]["summary"]            # la frase completa nombra la fábrica


async def test_any_other_session_gets_the_detail_without_one_factory_number(monkeypatch):
    set_viewers(monkeypatch, RESPONSIBLE)
    await open_items(["a"])
    item_id = first_item_id()
    detail = session(OTHER).get(f"{BASE}/items/{item_id}")
    assert detail.status_code == 200
    d, text = detail.json(), detail.text
    assert d["costs_visible"] is False
    # ningún campo de costo, en ninguna parte de la respuesta
    for forbidden in ('"cny_', '"fee_pct"', '"evidence"', '"target_cny"', '"factura_usd"', '"neto_usd"', '"usd_to_channel"',
                      '"fabrica"', '"summary_public"', FOB_USD, "149.8", "cny_cents"):
        assert forbidden not in text, forbidden
    bd = d["breakdown"]
    assert bd["restricted"] is True and "fabrica" not in {c["key"] for c in bd["components"]}
    assert all(set(c) <= {"key", "label", "usd", "detail"} for c in bd["components"])       # sin % ni pesos por unidad
    assert {c["key"] for c in bd["components"]} >= {"flete", "aranceles", "servicios", "certificado", "pasarela"}
    assert d["tiers"] and all("cny_unit" not in t for t in d["tiers"]) and "tiers" not in d["diagnosis"]
    # lo que el diagnóstico dice de (d) y (e) viene sin cifras de fábrica
    rules = {r["code"]: r for r in d["diagnosis"]["rules"]}
    for code in ("d", "e"):
        assert rules[code]["status"] == "dispara", rules[code]
        assert not any(ch.isdigit() for ch in rules[code]["summary"]), rules[code]["summary"]
    # y lo de siempre sigue ahí
    assert [r["code"] for r in d["diagnosis"]["rules"]] == list("abcde") and d["diagnosis"]["probable_cause"] in list("abcde")
    assert d["snapshot"]["matched_listings"] and d["our_price_cents"] and d["ml_median_cents"]


async def test_the_list_cause_sentence_has_no_factory_numbers_for_the_other_session(monkeypatch):
    set_viewers(monkeypatch, RESPONSIBLE)
    await open_items(["a"])
    from sqlmodel import Session

    from app.db.session import engine

    # forzar que la causa probable sea (d), cuya frase completa nombra la fábrica
    from app.db.models import PriceReviewItem

    with Session(engine) as s:
        it = s.get(PriceReviewItem, first_item_id())
        it.probable_cause = "d"
        s.add(it)
        s.commit()
    full = session(RESPONSIBLE).get(BASE + "/items").json()["items"][0]["cause_summary"]
    redacted = session(OTHER).get(BASE + "/items").json()["items"][0]["cause_summary"]
    assert "USD" in full and "USD" not in redacted and not any(ch.isdigit() for ch in redacted)
    assert "fábrica" in redacted


@pytest.mark.parametrize("viewers", ["", "   ", ",,", "no-es-un-mail", "otro@b2box.pro"])
async def test_with_the_setting_empty_or_without_you_nobody_sees_costs(monkeypatch, viewers):
    """Fail-closed: PRICE_REVIEW_VIEWERS vacío (o sin tu email) = nadie ve costos, tampoco la sesión «admin» del .env."""
    set_viewers(monkeypatch, viewers)
    await open_items(["a"])
    for email in (RESPONSIBLE, "admin", "Pao@B2Box.pro"):
        d = session(email).get(f"{BASE}/items/{first_item_id()}").json()
        assert d["costs_visible"] is False and d["breakdown"]["restricted"] is True, email


async def test_the_match_ignores_case_and_spaces_in_the_setting(monkeypatch):
    set_viewers(monkeypatch, "  Uno@b2box.pro ; RESPONSABLE@B2BOX.PRO ,otro@b2box.pro")
    await open_items(["a"])
    assert session(RESPONSIBLE).get(f"{BASE}/items/{first_item_id()}").json()["costs_visible"] is True
    assert session("Responsable@b2box.pro").get(f"{BASE}/items/{first_item_id()}").json()["costs_visible"] is True
    assert session(OTHER).get(f"{BASE}/items/{first_item_id()}").json()["costs_visible"] is False


async def test_a_session_without_a_user_and_no_session_at_all_see_no_costs(monkeypatch):
    set_viewers(monkeypatch, RESPONSIBLE)
    await open_items(["a"])
    assert session("").get(f"{BASE}/items/{first_item_id()}").json()["costs_visible"] is False
    anon = TestClient(main_mod.app)
    assert anon.get(f"{BASE}/items/{first_item_id()}").status_code == 401


async def test_the_summary_mail_never_carried_costs(monkeypatch):
    """El resumen diario no lleva costo de fábrica ni fee (solo código, nombre, margen y causa)."""
    from app.pricing import price_review_digest as digest
    from sqlmodel import Session

    from app.db.session import engine

    set_viewers(monkeypatch, RESPONSIBLE)
    await open_items(["a"])
    with Session(engine) as s:
        data = digest.collect(s)
    _subject, body = digest.render(data)
    lowered = body.lower()
    for forbidden in (FOB_USD, "fábrica", "fee", "yuan", "¥", "cny", "usd"):
        assert forbidden not in lowered, forbidden
