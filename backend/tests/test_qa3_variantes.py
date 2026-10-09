"""QA3, criterio 2: las variantes de búsqueda de la API de ML, sobre títulos de bazar / ferretería / regalería en el estilo
del catálogo (`qa3_titles.py`), más basura azarosa; y lo que cuestan contra `pm_ml_daily_budget`.

Lo que se afirma acá es lo que NO puede pasar nunca (consulta vacía, repetida, inventada, con código interno, demasiado
larga, no determinista, una corrida que se pasa del cupo). Lo discutible (las variantes que pierden un número de modelo)
queda como caracterización en `test_known_lossy_variants_are_documented`.
"""

from __future__ import annotations

import os
import random
import string

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app.pricing import daily_budget, market_match, market_ml, market_query, price_monitor  # noqa: E402
from tests.qa3_titles import BORDE, REALISTAS, TODOS  # noqa: E402
from tests.test_price_monitor import (  # noqa: E402,F401
    ML_IMG, FakeVendure, _candidate, _listing, _product, _runs, _set, _snaps, world)
from tests.qa3_cleanup import qa3_clean  # noqa: E402,F401  (fixture autouse)

_CODE = market_match._CODE_TOKEN


def _folded(text: str) -> set[str]:
    return {market_query._fold(w) for w in market_query._TOKEN.findall(text)}


def _assert_sane(title: str) -> None:
    plan = market_query.query_plan(title, 3)
    base = market_match.search_query(title)
    if not base:
        assert plan == []
        return
    queries = [s.query for s in plan]
    assert queries[0] == base and 1 <= len(queries) <= 3
    keys = [market_query._key(q) for q in queries]
    assert len(set(keys)) == len(keys), f"consultas repetidas: {queries}"
    for q in queries:
        assert q == q.strip() and q and "  " not in q and len(q) <= market_query.MAX_QUERY_CHARS
        assert not any(_CODE.match(w) for w in q.split()), f"código interno en {q!r}"
    # nada inventado: toda palabra de una variante sale del título
    for q in queries[1:]:
        assert _folded(q) <= _folded(base), (title, q)
    # determinista, y 2 variantes = las dos primeras de 3
    assert market_query.query_variants(title, 3) == queries
    assert market_query.query_variants(title, 2) == queries[:2]
    one = market_query.query_plan(title, 1)
    fallback = market_match.fallback_query(title)
    assert [s.query for s in one] == [base, *([fallback] if fallback else [])]
    assert [s.only_if_empty for s in one] == [False, *([True] if fallback else [])]


@pytest.mark.parametrize("title", TODOS)
def test_variants_are_sane_for_catalog_style_titles(title):
    _assert_sane(title)


def test_no_realistic_title_ends_with_a_single_variant_by_accident_of_emptiness():
    """Ningún título realista queda sin variante corta por vacía: si se repite es porque ya era corto."""
    for t in REALISTAS:
        plan = market_query.query_plan(t, 3)
        assert len(plan) >= 2, (t, [s.query for s in plan])


def test_garbage_titles_never_break_the_plan():
    rng = random.Random(7)
    alphabet = string.ascii_letters + string.digits + "  áéíóúñÁÉÍÓÚÑüç-_/.,;:()[]{}%&#@!?'\"\t\n​‮\u0000x×°ª😀漢字"
    for _ in range(4000):
        title = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 160)))
        _assert_sane(title)
    for title in ("", " ", "\n", None, "x" * 100_000, "Taza " * 20_000, "🙂" * 500, "漢字" * 300, "BX1 BX2 PA-3 PA4"):
        _assert_sane(title)  # type: ignore[arg-type]


@pytest.mark.parametrize("value, expected", [(1, 1), (2, 2), (3, 3), (0, 1), (-4, 1), (99, 3), ("2", 2), (2.9, 2), (None, 1),
                                             ("abc", 1), (True, 1), (float("nan"), 1)])
def test_clamp_variants(value, expected):
    assert market_query.clamp_variants(value) == expected


# ─── lo que el catálogo real (a mano) obtiene: caracterización ──────────────────

REASONABLE = {
    "Organizador Doble Ajustable 3 Niveles 40x30 Blanco": ["Organizador Doble Ajustable Niveles", "Organizador Ajustable Niveles"],
    "Set x6 Vasos de Vidrio 300ml Transparente": ["Set Vasos Vidrio Transparente", "Vasos Vidrio Transparente"],
    "Frasco Hermético de Vidrio 500 ml con Tapa de Bambú": ["Frasco Hermético Vidrio Tapa Bambú", "Frasco Hermético Vidrio"],
    "Pack x3 Vaso Térmico Acero Inoxidable 500 ml BX0123": ["Vaso Térmico Acero Inoxidable", "Vaso Térmico Acero"],
    "Kit de Herramientas 108 Piezas con Maletín": ["Kit Herramientas Maletín", "Herramientas Maletín"],
    "Taladro Percutor Inalámbrico 20V con 2 Baterías y Maletín": ["Taladro Percutor Inalámbrico Baterías Maletín",
                                                                  "Taladro Percutor Inalámbrico"],
    "Mini Ventilador USB Portátil Recargable Rosa": ["Mini Ventilador USB Portátil Recargable", "Ventilador USB Portátil"],
    "Sartén Antiadherente 28 cm Granito Negro": ["Sartén Antiadherente Granito"],        # corto == claves: una sola
}


@pytest.mark.parametrize("title", list(REASONABLE))
def test_reasonable_variants(title):
    assert market_query.query_variants(title, 3)[1:] == REASONABLE[title]


def test_known_lossy_variants_are_documented():
    """Lo que las variantes siguen perdiendo A PROPÓSITO (medidas, cantidades, colores, relleno) y lo que ya no: antes se iban
    el número de modelo (iPhone 13, i12), el tamaño (Número 5) y el conector (USB-C), y quedaban consultas de una sola palabra
    genérica ("Juego", "Limpieza"). Si cambia la heurística este test avisa."""
    q = lambda t: market_query.query_variants(t, 3)  # noqa: E731
    assert q("Funda Silicona iPhone 13 Pro Max Transparente")[1] == "Funda Silicona iPhone 13 Pro Max"      # conserva el 13
    assert q("Auriculares Bluetooth Inalámbricos TWS i12 Blanco")[1] == "Auriculares Bluetooth Inalámbricos TWS i12"
    assert q("Globo Metalizado Número 5 Dorado 40 cm")[1] == "Globo Metalizado Número 5"                    # conserva el 5, no los 40 cm
    assert q("Cargador Rápido 20W USB-C PD Cable Incluido")[1] == "Cargador Rápido USB-C PD Cable"         # USB-C entero; 20W afuera
    assert q("Set de Juego") == ["Set de Juego", "Set Juego"]                                              # "Juego" solo ya no se busca
    assert q("Kit de Limpieza") == ["Kit de Limpieza", "Kit Limpieza"]                                     # ni "Limpieza"
    assert q("Taza (350ml) - Blanca/Negra, c/ cuchara")[1] == "Taza cuchara"                              # "c/" → nada (a propósito)
    assert q("Soldadora Inverter 200A Portátil")[1] == "Soldadora Inverter Portátil"                      # los amperes son una medida


# ─── presupuesto y costo ───────────────────────────────────────────────────────


def _searches(w) -> list[str]:
    return [c.removeprefix("search:") for c in w.ml.calls if c.startswith("search:")]


@pytest.fixture
def catalog(world):
    FakeVendure.products = [_product(str(i), t) for i, t in enumerate(REALISTAS, 1)]
    _set("pm_ml_concurrency", 1)
    world.ml.search.clear()
    world.ml.items.clear()
    return world


async def test_nothing_found_costs_exactly_the_plan_of_each_product(catalog):
    """Si ML no tiene nada, cada producto gasta tantas búsquedas como variantes distintas tiene: ni una más, y la corrida
    y el contador del día coinciden con las llamadas reales (sin doble conteo)."""
    _set("pm_ml_query_variants", 3)
    await price_monitor.run_price_monitor()
    want = sorted(q for t in REALISTAS for q in market_query.query_variants(t, 3))
    assert sorted(_searches(catalog)) == want
    [run] = _runs()
    assert run.ml_requests_used == len(catalog.ml.calls) == daily_budget.used_today(market_ml.ML_COUNTER_KEY) == len(want)
    assert {s.ml_status for s in _snaps().values()} == {"no_data"}


async def test_the_first_variant_that_resolves_is_the_only_search(catalog):
    _set("pm_ml_query_variants", 3)
    for i, t in enumerate(REALISTAS, 1):
        catalog.ml.search[market_match.search_query(t)] = [_candidate(f"MLA{i}", t)]
        catalog.ml.items[f"MLA{i}"] = [_listing(f"I{i}", f"{i}01", 200.0), _listing(f"J{i}", f"{i}02", 210.0)]
        catalog.ml.users.update({f"{i}01": 500, f"{i}02": 400})
        catalog.image_scores[ML_IMG.format(f"MLA{i}")] = 0.9
    await price_monitor.run_price_monitor()
    assert sorted(_searches(catalog)) == sorted(market_match.search_query(t) for t in REALISTAS)
    assert {s.ml_variant for s in _snaps().values()} == {"titulo"} and {s.ml_status for s in _snaps().values()} == {"ok"}


@pytest.mark.parametrize("budget", [1, 5, 40, 77, 120])
async def test_the_run_never_passes_the_daily_budget_even_with_concurrency(catalog, budget):
    _set("pm_ml_concurrency", 4)
    _set("pm_ml_query_variants", 3)
    _set("pm_ml_daily_budget", budget)
    await price_monitor.run_price_monitor()
    used = daily_budget.used_today(market_ml.ML_COUNTER_KEY)
    assert used <= budget and len(catalog.ml.calls) <= budget
    assert _runs()[0].ml_requests_used == used == len(catalog.ml.calls)
    statuses = [s.ml_status for s in _snaps().values()]
    assert set(statuses) <= {"no_data", "skipped"}
    if budget < 60:
        assert "skipped" in statuses                      # el producto sin cupo queda skipped, no no_data


async def test_a_persistent_503_on_the_second_variant_counts_every_attempt_once_and_stops(catalog):
    _set("pm_ml_query_variants", 3)
    title = "Organizador Doble Ajustable 3 Niveles 40x30 Blanco"
    FakeVendure.products = [_product("1", title)]
    v1, v2, v3 = market_query.query_variants(title, 3)
    catalog.ml.search[v2] = 503
    await price_monitor.run_price_monitor()
    assert _searches(catalog) == [v1] + [v2] * market_ml._MAX_ATTEMPTS and v3 not in _searches(catalog)
    assert _snaps()["1"].ml_status == "failed"
    assert _runs()[0].ml_requests_used == 1 + market_ml._MAX_ATTEMPTS == daily_budget.used_today(market_ml.ML_COUNTER_KEY)


async def test_worst_case_cost_of_a_product_whose_fichas_are_identical_but_have_no_sellers(catalog):
    """Costo máximo de UN producto: cada variante trae 4+ fichas IGUAL sin vendedores que cuenten. Además de las 3 búsquedas se
    piden los vendedores (`/items`) de hasta MAX_MATCHED_PRODUCTS fichas POR VARIANTE: el README dice "hasta 2 requests más"
    y esto es más. Se fija el número para que un cambio no pase desapercibido."""
    title = "Organizador Doble Ajustable 3 Niveles 40x30 Blanco"
    FakeVendure.products = [_product("1", title)]
    queries = market_query.query_variants(title, 3)
    n = price_monitor.MAX_MATCHED_PRODUCTS
    for v, q in enumerate(queries):
        cands = [_candidate(f"MLA{v}{k}", title) for k in range(n + 2)]
        catalog.ml.search[q] = cands
        for c in cands:
            catalog.image_scores[ML_IMG.format(c["id"])] = 0.9
            catalog.ml.items[c["id"]] = []                # sin vendedores
    _set("pm_ml_query_variants", 1)
    await price_monitor.run_price_monitor()
    v1_cost = len(catalog.ml.calls)
    catalog.ml.calls.clear()
    from sqlmodel import Session, select  # noqa: PLC0415
    from app.db.models import MarketPriceSnapshot  # noqa: PLC0415
    from app.db.session import engine  # noqa: PLC0415
    with Session(engine) as s:
        for row in s.exec(select(MarketPriceSnapshot)).all():
            s.delete(row)
        s.commit()
    _set("pm_ml_query_variants", 3)
    await price_monitor.run_price_monitor()
    v3_cost = len(catalog.ml.calls)
    assert len(queries) == 3
    assert v1_cost == 1 + n                                   # 1 búsqueda + /items de 4 fichas
    assert v3_cost == 3 * (1 + n) + 0                         # 3 búsquedas + /items de 4 fichas en CADA variante
    assert _snaps()["1"].ml_status == "no_data"


@pytest.mark.parametrize("variants, judge_calls", [(1, 1), (2, 2), (3, 3)])
async def test_the_llm_judge_is_asked_once_per_variant_that_brings_ambiguous_fichas(catalog, monkeypatch, variants, judge_calls):
    """Costo que el README no menciona: cada variante con fichas ambiguas es UNA llamada más al juez de IA (si está prendido,
    `pm_vision_max_calls` > 0; apagado por defecto). Un producto sin dato con fichas dudosas en las tres búsquedas pasa de 1 a 3
    llamadas. El tope diario del juez acota el gasto."""
    from tests import qa2_world as qw
    title = "Organizador Doble Ajustable 3 Niveles 40x30 Blanco"
    FakeVendure.products = [_product("1", title)]
    _set("pm_ml_query_variants", variants)
    _set("pm_vision_max_calls", 100)
    calls: list = []
    qw.install_judge(monkeypatch, calls)
    for k, q in enumerate(s.query for s in market_query.query_plan(title, variants) if not s.only_if_empty):
        catalog.ml.search[q] = [_candidate(f"MLA{k}0", title + " compatible")]
        catalog.image_scores[ML_IMG.format(f"MLA{k}0")] = 0.5          # ambigua: la mira el juez
    await price_monitor.run_price_monitor()
    assert len(calls) == judge_calls and _snaps()["1"].ml_status == "no_data"
