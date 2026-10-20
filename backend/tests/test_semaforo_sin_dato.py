"""Corrida #15 (20-oct-2026): 1.810 de 1.869 productos quedaron «sin dato». Lo que se corrige acá:

  a) un 404 al pedir los vendedores de UNA ficha de la API no deja el producto `failed`: esa ficha queda idéntica
     sin precio, con su motivo, y el producto sigue (otras fichas, la web, la oficina). 429/5xx siguen siendo fallo.

Vendure, ML (API y web), CLIP y el juez son dobles: nada sale a la red."""

from __future__ import annotations

import json
import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.db.models import MlWebResult  # noqa: E402
from app.db.session import engine, init_db  # noqa: E402
from app.pricing import price_monitor  # noqa: E402
from tests.test_oficina_semaforo import _store, _wire  # noqa: E402
from tests.test_price_monitor import (  # noqa: E402,F401
    ML_IMG, FakeVendure, _candidate, _listing, _product, _runs, _snaps, world)
from tests.test_semaforo_web import PHOTO  # noqa: E402


def _clear_oficina() -> None:
    with Session(engine) as s:
        for row in s.exec(select(MlWebResult)).all():
            s.delete(row)
        s.commit()


@pytest.fixture(autouse=True)
def _no_oficina_rows():
    """Antes y DESPUÉS: un resultado fresco de la oficina que quede le cambia el camino a los tests de otros módulos."""
    init_db()
    _clear_oficina()
    yield
    _clear_oficina()


def _only(world, *products):
    FakeVendure.products = list(products)


# ─── a) 404 de una ficha ─────────────────────────────────────────────────────


async def test_a_404_on_the_sellers_of_the_only_igual_is_igual_sin_precio_not_failed(world):
    _only(world, _product("1", "Organizador cocina"))
    world.ml.items["MLA1"] = 404

    result = await price_monitor.run_price_monitor()

    s = _snaps()["1"]
    assert s.ml_status == "no_data" and s.match_state == "igual_sin_precio"
    assert "404" in s.ml_error and "vendedores ML" not in s.ml_error
    [unpriced] = json.loads(s.unpriced_listings)
    assert unpriced["ml_id"] == "MLA1" and "ML no tiene vendedores para esta ficha (404)" in unpriced["notes"]
    assert result["counts"]["failed"] == 0 and _runs()[0].status == "ok"


async def test_a_404_on_one_ficha_does_not_hide_the_price_of_another(world):
    _only(world, _product("1", "Organizador cocina"))
    world.ml.search["Organizador cocina"] = [_candidate("MLA1", "Organizador de cocina"),
                                             _candidate("MLA7", "Organizador cocina")]
    world.image_scores[ML_IMG.format("MLA7")] = 0.95          # la más parecida: se le pide primero
    world.ml.items["MLA7"] = 404

    await price_monitor.run_price_monitor()

    s = _snaps()["1"]
    assert s.ml_status == "ok" and s.match_origin == "api" and s.ml_median_cents == 21_000
    assert [u["ml_id"] for u in json.loads(s.unpriced_listings)] == ["MLA7"]


async def test_a_404_then_the_oficina_without_an_igual_is_no_data_with_both_reasons(world):
    _only(world, _product("1", "Organizador cocina"))
    world.ml.items["MLA1"] = 404
    _store("1", [_wire("MLA901", "Heladera no frost", 250.0)])
    world.image_scores[PHOTO.format("901-MLA1_012025")] = 0.10

    await price_monitor.run_price_monitor()

    s = _snaps()["1"]
    assert s.ml_status == "no_data" and s.match_state == "igual_sin_precio"
    assert "404" in s.ml_error and "ML web (oficina)" in s.ml_error


async def test_a_404_then_the_oficina_with_an_igual_with_price_is_ok(world):
    _only(world, _product("1", "Organizador cocina"))
    world.ml.items["MLA1"] = 404
    _store("1", [_wire("MLA901", "Organizador cocina", 250.0)])
    world.image_scores[PHOTO.format("901-MLA1_012025")] = 0.92

    await price_monitor.run_price_monitor()

    s = _snaps()["1"]
    assert s.ml_status == "ok" and s.match_origin == "oficina" and s.ml_median_cents == 25_000


async def test_a_429_on_the_sellers_is_still_a_failure_even_if_the_oficina_ran_without_a_price(world):
    """Conservador: un 429/5xx persistente puede esconder el precio de una ficha que ES el producto. Sin ningún
    precio, el producto queda `failed` (y vuelve a la cabeza de la fila la noche siguiente), aunque la oficina
    haya corrido."""
    _only(world, _product("1", "Organizador cocina"))
    world.ml.items["MLA1"] = 429
    _store("1", [_wire("MLA901", "Heladera no frost", 250.0)])
    world.image_scores[PHOTO.format("901-MLA1_012025")] = 0.10

    await price_monitor.run_price_monitor()

    s = _snaps()["1"]
    assert s.ml_status == "failed" and s.ml_error.startswith("vendedores ML: ML no respondió")
    assert "ML web (oficina)" in s.ml_error


async def test_a_429_on_the_sellers_but_the_oficina_has_the_price_is_ok(world):
    _only(world, _product("1", "Organizador cocina"))
    world.ml.items["MLA1"] = 503
    _store("1", [_wire("MLA901", "Organizador cocina", 250.0)])
    world.image_scores[PHOTO.format("901-MLA1_012025")] = 0.92

    await price_monitor.run_price_monitor()

    assert _snaps()["1"].ml_status == "ok"
