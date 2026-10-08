"""QA de cierre de feat/semaforo-tiendas, criterio 1: Mercado Libre no cambia.

El DORADO sale de origin/main (77645f9), NO de b052e4f ni de esta rama: se corrió `qa2_world.py` (16 productos: API,
web, packs, ambiguos, marca conocida, rojo/verde, sin foto, deshabilitado, sin precio, 429, «No es el mismo»; con el juez
apagado, con cupo de sobra, justo y corto; otros umbrales y presupuesto corto) en un worktree de 77645f9 y se volcaron
TODAS las columnas del snapshot y de la corrida. Regenerarlo (solo desde origin/main):

    git worktree add --detach ../hugo-main-qa 77645f9
    cp backend/tests/qa2_world.py ../hugo-main-qa/backend/tests/          # y un test_qa2_write_golden.py que llame
    QA2_GOLDEN_OUT=backend/tests/golden/semaforo_origin_main_77645f9_qa2.json pytest backend/tests/test_qa2_write_golden.py

Acá se corre el mismo mundo en esta rama en tres situaciones y todo lo de ML tiene que ser idéntico, columna a columna:
  * tiendas apagadas (filas apagadas, o sin ninguna fila);
  * tiendas prendidas, `pm_stores_affect_color=1`, sin ningún idéntico de tienda que valga (índice vacío, solo diferentes,
    idénticos agotados o con precio dudoso, o un «idéntico» que solo confirma el juez);
  * el juez de ML prendido con cupo de sobra, justo y corto, con las tiendas consultando al suyo: ML no pierde llamadas.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlalchemy import delete, update  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import runtime  # noqa: E402
from app.clock import utcnow  # noqa: E402
from app.db.models import MarketStore, StoreCatalogItem, StoreMatch  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import daily_budget, market_judge, market_match, price_monitor, store_catalog  # noqa: E402
from tests import qa2_world as qw  # noqa: E402
from tests.store_fixtures import store_db  # noqa: E402,F401  (fixture)
from tests.test_price_monitor import FakeVendure, world  # noqa: E402,F401
from tests.test_semaforo_web import webw  # noqa: E402,F401

GOLDEN = json.loads((Path(__file__).parent / "golden" / "semaforo_origin_main_77645f9_qa2.json").read_text())
ADDED_BY_THIS_BRANCH = {"price_basis", "source_stats"}
CP_IMG = "https://acdn-us.mitiendanube.com/stores/001/133/924/products/{}.webp"
GD_IMG = "https://static.bidcom.com.ar/publicacionesML/productos/{}.jpg"


def _store_ids() -> dict[str, int]:
    with Session(engine) as s:
        return {r.name: int(r.id) for r in s.exec(select(MarketStore)).all()}


def fill_index(*, stock: int | None = 5, price_factor: float = 1.0, doubtful: bool = False) -> None:
    """Un producto en cada tienda por cada producto nuestro, con el MISMO nombre (los más tentadores)."""
    ids = _store_ids()
    with Session(engine) as s:
        for p in FakeVendure.products:
            for store, base, img in (("Casa Perfecta", "https://www.casaperfecta.com.ar/productos", CP_IMG),
                                     ("Gadnic", "https://www.gadnic.com.ar/catalogo", GD_IMG)):
                key = f"{store[:2].lower()}-{p.id}"
                s.add(StoreCatalogItem(
                    store_id=ids[store], url=f"{base}/{key}/", title=p.name, sku=key.upper(),
                    price_cents=int((p.first_variant_price_cents or 10_000) * price_factor), price_doubtful=doubtful,
                    price_note="dudoso" if doubtful else None, image_url=img.format(key), brand=None, stock=stock,
                    last_seen_at=utcnow(), last_checked_at=utcnow()))
        s.commit()


@pytest.fixture
def run_world(webw, store_db, monkeypatch):  # noqa: F811
    store_catalog.seed_default_stores()
    runtime.set_value("pm_stores_topup_minutes", 0)
    runtime.set_value("pm_stores_affect_color", 1)
    box = {"score": 0.20, "calls": []}

    async def clip(our, urls):
        """Solo la ficha de la tienda que lleva el id de ESTE producto se parece a su foto; las otras, no."""
        if not urls:
            return None
        pid = urls[0].rsplit("-", 1)[-1].split(".", 1)[0]
        return box["score"] if pid == our.id else 0.20

    monkeypatch.setattr(market_match, "clip_score_urls", clip)
    box["monkeypatch"] = monkeypatch
    yield webw, box
    runtime.invalidate()


async def run_variant(name: str, run_world, *, stores_judge_cap: int | None = None) -> dict:
    w, box = run_world
    needed = GOLDEN[name]["judge_calls_needed"]
    opts = qw.apply_variant(name, judge_calls_needed=needed)
    if stores_judge_cap is not None:
        runtime.set_value("pm_stores_vision_max_calls", stores_judge_cap)
    if opts.get("judge"):
        qw.install_judge(box["monkeypatch"], box["calls"])
    qw.build_world(w, feedback=bool(opts.get("feedback")))
    return w, opts


def assert_ml_equal(name: str, got: dict, *, where: str = "", stores_llm_calls: int = 0) -> None:
    """`run.llm_calls` es el total de la corrida (ML + tiendas): lo de las tiendas se resta antes de comparar."""
    want = GOLDEN[name]
    assert got["ml_calls"] == want["ml_calls"], f"{where}: requests a la API de ML"
    assert got["web_calls"] == want["web_calls"], f"{where}: búsquedas web"
    assert got["snaps"].keys() == want["snaps"].keys(), f"{where}: productos del snapshot"
    extra = {k for row in got["snaps"].values() for k in row} - {k for row in want["snaps"].values() for k in row}
    assert extra <= ADDED_BY_THIS_BRANCH, f"columnas nuevas inesperadas: {extra}"
    diffs = {(pid, col): (v, got["snaps"][pid][col])
             for pid, row in want["snaps"].items() for col, v in row.items() if got["snaps"][pid][col] != v}
    assert diffs == {}, f"{where}: el snapshot de ML cambió respecto de origin/main"
    assert {r["price_basis"] for r in got["snaps"].values()} == {"ml"}, f"{where}: price_basis no es ml"
    got_run = dict(got["run"], llm_calls=got["run"]["llm_calls"] - stores_llm_calls)
    run_diffs = {c: (v, got_run[c]) for c, v in want["run"].items() if got_run[c] != v}
    assert run_diffs == {}, f"{where}: los contadores de la corrida cambiaron"
    assert set(got["run"]) - set(want["run"]) <= ADDED_BY_THIS_BRANCH
    assert got["judge_used_ml"] == want["judge_used_ml"], f"{where}: llamadas del juez de ML"
    if "judge_calls_ml" in want:
        assert got["judge_calls_ml"] == want["judge_calls_ml"], f"{where}: a quién le preguntó el juez de ML"


def test_the_golden_is_the_one_of_origin_main_and_has_every_variant():
    assert set(GOLDEN) == set(qw.VARIANTS)
    # el mundo ejercita las cuatro situaciones de color y los dos orígenes
    base = GOLDEN["base_spec1"]["snaps"]
    assert {s["color"] for s in base.values()} == {"verde", "amarillo", "rojo", "sin_dato"}
    assert {s["match_origin"] for s in base.values()} >= {"api", "web"}
    assert GOLDEN["juez_cupo_de_sobra"]["judge_used_ml"] == 3 and GOLDEN["juez_cupo_justo"]["judge_used_ml"] == 3
    assert GOLDEN["juez_cupo_corto"]["judge_used_ml"] == 1, "el cupo corto tiene que agotarse a mitad de la corrida"


# ─── 1) tiendas apagadas ─────────────────────────────────────────────────────


@pytest.mark.parametrize("name", list(qw.VARIANTS))
@pytest.mark.parametrize("how", ["filas_apagadas", "sin_filas"])
async def test_stores_off_ml_is_column_by_column_what_origin_main_gives(run_world, name, how):
    w, opts = await run_variant(name, run_world)
    fill_index()
    box = run_world[1]
    box["score"] = 0.95                                     # si se compararan, sería lo más tentador
    with Session(engine) as s:
        if how == "filas_apagadas":
            s.execute(update(MarketStore).values(enabled=False))
        else:
            s.execute(delete(StoreCatalogItem))
            s.execute(delete(MarketStore))
        s.commit()
    await price_monitor.run_price_monitor()
    assert_ml_equal(name, qw.dump_all(w, box["calls"]), where=f"{name}/{how}")
    with Session(engine) as s:
        assert s.exec(select(StoreMatch)).all() == []
    assert daily_budget.used_today(market_judge.STORES_LLM_COUNTER_KEY) == 0


# ─── 2) prendidas y contando, sin un idéntico de tienda que valga ────────────

NOTHING_COUNTS = {
    "indice_vacio": dict(fill=False, score=0.95),
    "solo_diferentes": dict(fill=True, score=0.20),
    "identicos_agotados": dict(fill=True, score=0.95, stock=0),
    "precio_dudoso": dict(fill=True, score=0.95, doubtful=True),
    "precio_10_veces_menor": dict(fill=True, score=0.95, price_factor=0.05),
    "precio_10_veces_mayor": dict(fill=True, score=0.95, price_factor=40.0),
}


@pytest.mark.parametrize("name", ["base_spec1", "base_spec0", "include_disabled", "con_feedback", "umbrales_otros",
                                  "presupuesto_ml_corto"])
@pytest.mark.parametrize("situation", list(NOTHING_COUNTS))
async def test_stores_on_and_counting_but_without_a_valid_identical_ml_is_column_by_column_the_same(run_world, name, situation):
    cfg = dict(NOTHING_COUNTS[situation])
    w, opts = await run_variant(name, run_world)
    box = run_world[1]
    box["score"] = cfg.pop("score")
    if cfg.pop("fill"):
        fill_index(**cfg)
    await price_monitor.run_price_monitor()
    assert_ml_equal(name, qw.dump_all(w, box["calls"]), where=f"{name}/{situation}")
    with Session(engine) as s:
        n = len(s.exec(select(StoreMatch)).all())
    assert (n == 0) == (situation == "indice_vacio"), "el índice vacío no compara nada; los demás, sí"


@pytest.mark.parametrize("name", ["base_spec1", "base_spec0"])
async def test_stores_on_with_identicals_that_count_only_the_color_of_products_with_a_store_identical_moves(run_world, name):
    """El contraste del caso anterior: con idénticos que SÍ valen, las columnas de ML siguen intactas y solo cambian el
    color, la ganancia y price_basis de los productos que tienen un idéntico de tienda (los demás, ni un bit)."""
    w, opts = await run_variant(name, run_world)
    box = run_world[1]
    box["score"] = 0.95
    fill_index(price_factor=1.5)
    await price_monitor.run_price_monitor()
    got = qw.dump_all(w, box["calls"])
    want = GOLDEN[name]
    ml_cols_moved = {(pid, col) for pid, row in want["snaps"].items() for col, v in row.items()
                     if col not in ("color", "est_margin_pct", "estimated_color", "estimated_margin_pct",
                                    "estimated_median_cents", "estimated_listing_count", "estimated_from", "status")
                     and got["snaps"][pid][col] != v}
    assert ml_cols_moved == set(), f"las columnas ml_* / match_* no se tocan nunca: {sorted(ml_cols_moved)}"
    moved = {pid for pid, row in want["snaps"].items() if got["snaps"][pid]["color"] != row["color"]
             or got["snaps"][pid]["est_margin_pct"] != row["est_margin_pct"]}
    with_store_basis = {pid for pid, r in got["snaps"].items() if r["price_basis"] != "ml"}
    assert moved <= with_store_basis, "cambió el color de un producto que no usa precios de tienda"
    assert with_store_basis, "el mundo no ejercitó el color por tienda: el test no probaría nada"


# ─── 3) el juez: cupo justo, ML no pierde llamadas ───────────────────────────


COLOR_DERIVED = {"color", "est_margin_pct", "estimated_color", "estimated_margin_pct", "estimated_median_cents",
                 "estimated_listing_count", "estimated_from", "price_basis"}


@pytest.mark.parametrize("affect_color", [0, 1])
@pytest.mark.parametrize("stores_cap", [500, 1, 0])
@pytest.mark.parametrize("name", ["juez_cupo_de_sobra", "juez_cupo_justo", "juez_cupo_corto", "juez_sin_specs"])
@pytest.mark.parametrize("score", [0.50, 0.95])
async def test_the_judge_of_ml_loses_no_calls_whatever_the_stores_judge_does(run_world, name, stores_cap, score, affect_color):
    """Con `pm_stores_affect_color=0` TODO queda igual que en origin/main. Con 1, el color y el estimado pueden moverse
    (es la decisión de Nico) pero las columnas de ML (mediana, mínimo, publicaciones, listas, motivo, fuente) y las
    llamadas del juez de ML siguen siendo las de origin/main."""
    w, opts = await run_variant(name, run_world, stores_judge_cap=stores_cap)
    runtime.set_value("pm_stores_affect_color", affect_color)
    box = run_world[1]
    box["score"] = score                                    # 0.50 = banda ambigua: las tiendas le preguntan al juez
    fill_index()
    await price_monitor.run_price_monitor()
    got = qw.dump_all(w, box["calls"])
    where = f"{name}/tope tiendas {stores_cap}/foto {score}/color {affect_color}"
    store_calls = [n for n, k in box["calls"] if k == market_judge.STORES_LLM_COUNTER_KEY]
    if affect_color == 0:
        assert_ml_equal(name, got, where=where, stores_llm_calls=len(store_calls))
    else:
        want = GOLDEN[name]
        assert got["ml_calls"] == want["ml_calls"] and got["web_calls"] == want["web_calls"], where
        assert got["judge_used_ml"] == want["judge_used_ml"] and got["judge_calls_ml"] == want["judge_calls_ml"], where
        moved = {(pid, col) for pid, row in want["snaps"].items() for col, v in row.items()
                 if col not in COLOR_DERIVED and got["snaps"][pid][col] != v}
        assert moved == set(), f"{where}: columnas de ML que se movieron: {sorted(moved)[:6]}"
    used = daily_budget.used_today(market_judge.STORES_LLM_COUNTER_KEY)
    assert used == len(store_calls) <= stores_cap
    if score == 0.50 and stores_cap == 500:
        assert store_calls, "las tiendas tenían que consultar a su juez en la banda ambigua"
    if stores_cap == 0:
        assert store_calls == [] and used == 0


async def test_with_the_ml_judge_off_the_stores_do_not_use_theirs_even_with_a_cap(run_world):
    w, opts = await run_variant("base_spec1", run_world, stores_judge_cap=500)
    box = run_world[1]
    box["score"] = 0.50
    qw.install_judge(box["monkeypatch"], box["calls"])
    runtime.set_value("pm_vision_max_calls", 0)
    fill_index()
    await price_monitor.run_price_monitor()
    assert box["calls"] == [] and daily_budget.used_today(market_judge.STORES_LLM_COUNTER_KEY) == 0
    assert_ml_equal("base_spec1", qw.dump_all(w, box["calls"]))
