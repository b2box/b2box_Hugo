"""QA independiente de feat/semaforo-tiendas, criterio 3: el matching contra las tiendas.

  * el veredicto de una ficha de tienda es el MISMO que el de ML para el mismo título y la misma foto (tabla de
    combinaciones corrida por las dos fuentes en la misma corrida);
  * «siempre trae algo»: hasta 6 candidatos por producto y tienda aunque no se parezcan, y qué pasa con una tienda
    chica, vacía o con un producto sin foto;
  * «Es el mismo / No es el mismo / Deshacer» como máquina de estados (secuencias al azar contra un modelo);
  * la marca de la casa («Gadnic») es genérica, con o sin mayúsculas.
"""

from __future__ import annotations

import json
import os
import random

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import runtime  # noqa: E402
from app.db.models import (  # noqa: E402
    MarketPriceSnapshot,
    MarketStore,
    PriceMonitorRun,
    StoreMatch,
    StoreMatchFeedback,
)
from app.db.session import engine  # noqa: E402
from app.pricing import market_judge, market_match, price_monitor, store_catalog, store_match  # noqa: E402
from tests.store_fixtures import store_db  # noqa: E402,F401  (fixture)
from tests.test_price_monitor import (  # noqa: E402,F401
    FakeVendure,
    _candidate,
    _listing,
    _product,
    _runs,
    _set,
    _snaps,
    world,
)
from tests.test_qa_tiendas_color import (  # noqa: E402,F401
    CP,
    GD,
    GD_IMG,
    ML_IMG,
    SCORES,
    _store_id,
    add_item,
    sw,
    webw,
)

# ─── el veredicto es el mismo que el de ML ──────────────────────────────────

GRID_TITLES = [
    "Organizador de cocina",                       # igual
    "Organizador de cocina plegable",              # igual con un adjetivo
    "Pack x3 Organizador de cocina",               # cantidad: similar
    "Organizador de cocina 2 litros",              # capacidad: similar
    "Organizador cocina 40x30 cm",                 # medida
    "Zapatilla running",                           # otro producto
    "Heladera no frost",
]
GRID_SCORES = [0.95, 0.80, 0.70, 0.62, 0.55, 0.40, 0.10]


def _ml_category(ml_id: str, snap: MarketPriceSnapshot) -> str:
    def ids(raw):
        return {m.get("ml_id") for m in (json.loads(raw) if raw else [])}

    if ml_id in ids(snap.matched_listings) | ids(snap.unpriced_listings):
        return "igual"
    if ml_id in ids(snap.similar_listings):
        return "similar"
    if ml_id in ids(snap.other_listings):
        return "diferente"
    return "fuera"


async def _grid(sw, spec_check) -> tuple[list[tuple], set[tuple[str, str]]]:
    """Corre la tabla (título × puntaje de foto) por ML (API) y por la tienda en la MISMA corrida."""
    from app.db.models import StoreCatalogItem

    _set("pm_spec_check", spec_check)
    mismatches: list[tuple] = []
    seen: set[tuple[str, str]] = set()
    for i, title in enumerate(GRID_TITLES):
        for j, score in enumerate(GRID_SCORES):
            ml_id = f"MLA9{i}{j}"
            sw.ml.search["Organizador cocina"] = [_candidate(ml_id, title)]
            sw.ml.items[ml_id] = [_listing(f"I9{i}{j}", "101", 200.0)]
            sw.image_scores[ML_IMG.format(ml_id)] = score
            with Session(engine) as s:
                for row in s.exec(select(StoreCatalogItem)).all():
                    s.delete(row)
                s.commit()
            add_item(GD, f"gd-{i}-{j}", title, 20_000, score)
            await price_monitor.run_price_monitor()
            run_id = _runs()[-1].id
            ml_cat = _ml_category(ml_id, _snaps(run_id)["1"])
            with Session(engine) as s:
                m = s.exec(select(StoreMatch).where(StoreMatch.run_id == run_id)).one()
            seen.add((ml_cat, m.category))
            if ml_cat != m.category:
                mismatches.append((title, score, ml_cat, m.category, m.source, m.reason))
    return mismatches, seen


def _is_unconfirmed_ambiguous(mm: tuple) -> bool:
    return mm[2] == "similar" and mm[3] == "diferente" and (mm[5] or "").startswith("dudoso y sin juez")


@pytest.mark.parametrize("spec_check", [1, 0])
async def test_a_store_item_gets_the_same_verdict_as_the_same_ml_listing_except_in_the_ambiguous_band(sw, spec_check):
    """Mismo título, misma foto (mismo puntaje de CLIP), mismo producto: ML y la tienda clasifican igual en las 49
    combinaciones salvo la banda AMBIGUA sin juez (ver el xfail de abajo)."""
    mismatches, seen = await _grid(sw, spec_check)
    print("pares (ML, tienda) vistos:", sorted(seen))
    assert [m for m in mismatches if not _is_unconfirmed_ambiguous(m)] == []
    assert {c for c, _ in seen} >= {"igual", "similar", "diferente"}, "la tabla no ejercitó las tres categorías"
    assert len(mismatches) < len(GRID_TITLES) * len(GRID_SCORES)


@pytest.mark.xfail(strict=True, reason="DIFERENCIA con ML: lo dudoso sin juez (banda ambigua) es SIMILAR «sin confirmar» en ML y DIFERENTE "
                                       "«dudoso y sin juez IA…» en las tiendas (store_match._category); cambia columna, contadores y filtros")
async def test_the_ambiguous_band_without_a_judge_is_classified_like_ml(sw):
    mismatches, _ = await _grid(sw, 1)
    assert mismatches == []


# ─── siempre trae algo ──────────────────────────────────────────────────────


def _rows(store: str, pid: str = "1") -> list[StoreMatch]:
    with Session(engine) as s:
        return list(s.exec(select(StoreMatch).where(
            StoreMatch.product_id == pid, StoreMatch.store_id == _store_id(store)).order_by(StoreMatch.rank)).all())


async def test_six_candidates_per_store_even_when_nothing_looks_alike_and_the_different_ones_say_why(sw):
    for i in range(40):
        add_item(GD, f"gd-{i}", f"Heladera no frost modelo {i}", 100_000 + i, 0.12)
        add_item(CP, f"cp-{i}", f"Cortina de baño {i}", 5_000 + i, 0.15)
    await price_monitor.run_price_monitor()
    for store in (GD, CP):
        rows = _rows(store)
        assert len(rows) == 6, store
        assert {r.category for r in rows} == {"diferente"}
        assert all(r.reason for r in rows), "un diferente tiene que decir por qué"
        assert [r.rank for r in rows] == [1, 2, 3, 4, 5, 6]
        assert len({r.item_id for r in rows}) == 6
    snap = _snaps()["1"]
    assert snap.color == "amarillo" and snap.price_basis == "ml"           # y ML sigue siendo ML


@pytest.mark.parametrize("n_items", [0, 1, 3, 6, 7])
async def test_a_small_store_gives_all_it_has_and_an_empty_one_gives_nothing_without_failing(sw, n_items):
    for i in range(n_items):
        add_item(CP, f"cp-{i}", f"Organizador de cocina {i}", 8_000 + i, 0.9)
    await price_monitor.run_price_monitor()
    assert len(_rows(CP)) == min(n_items, 6)
    assert _rows(GD) == []
    [run] = _runs()
    assert run.status == "ok"
    stats = json.loads(run.source_stats)
    assert stats[f"store:{_store_id(GD)}"]["nada"] == 1
    assert stats[f"store:{_store_id(CP)}"]["nada"] == (1 if n_items == 0 else 0)


async def test_a_product_without_a_photo_is_not_compared_with_the_stores_and_says_nothing_about_it(sw):
    """Documenta una limitación: ML tampoco puede comparar sin foto (queda `skipped`), y las tiendas heredan eso."""
    prod = _product("1", "Organizador cocina")
    prod.image_urls, prod.featured_image_url = [], None
    FakeVendure.products = [prod]
    add_item(GD, "gd-org", "Organizador de cocina", 20_000, 0.9)
    await price_monitor.run_price_monitor()
    assert _snaps()["1"].ml_status in ("skipped", "no_data", "failed", "ok")
    assert _rows(GD) == []


async def test_the_best_candidate_is_rank_one_and_identicals_come_before_similars_before_differents(sw):
    add_item(GD, "gd-dif", "Cortina de baño", 5_000, 0.20)
    add_item(GD, "gd-pack", "Pack x3 Organizador de cocina", 24_000, 0.95)
    add_item(GD, "gd-igual", "Organizador de cocina", 9_000, 0.95)
    await price_monitor.run_price_monitor()
    assert [(r.title, r.category, r.rank) for r in _rows(GD)] == [
        ("Organizador de cocina", "igual", 1), ("Pack x3 Organizador de cocina", "similar", 2), ("Cortina de baño", "diferente", 3)]


# ─── la marca de la casa ────────────────────────────────────────────────────


@pytest.mark.parametrize("brand,asked", [("Gadnic", False), ("GADNIC", False), ("gadnic", False), (None, False),
                                         ("Stanley", True)])
async def test_the_house_brand_is_generic_and_a_known_brand_goes_to_the_judge(sw, monkeypatch, brand, asked):
    asked_ids: list[str] = []

    async def judge(our_name, our_images, candidates, *, max_calls, on_reserve=None, **kw):
        asked_ids.extend(c.ml_id for c in candidates)
        return market_judge.JudgeResult(verdicts={})

    monkeypatch.setattr(market_judge, "enabled", lambda: True)
    monkeypatch.setattr(market_judge, "make_client", lambda: None)
    monkeypatch.setattr(market_judge, "judge", judge)
    _set("pm_vision_max_calls", 50)
    add_item(GD, "gd-org", "Organizador de cocina", 20_000, 0.95, brand=brand)
    await price_monitor.run_price_monitor()
    gd_asked = [i for i in asked_ids if i.startswith(f"s{_store_id(GD)}:")]
    assert bool(gd_asked) is asked
    assert _rows(GD)[0].category in ("igual", "similar")


# ─── Es el mismo / No es el mismo / Deshacer: máquina de estados ────────────


def _seed_one_match(category: str = "igual") -> tuple[int, int]:
    """Una corrida con un producto y un candidato de tienda (sin color de tiendas): (match_id, snapshot_id)."""
    with Session(engine) as s:
        run = PriceMonitorRun(status="ok", total_products=1)
        s.add(run)
        s.commit()
        s.refresh(run)
        snap = MarketPriceSnapshot(run_id=run.id, product_id="1", product_name="Uno", color="verde", ml_status="ok",
                                   our_price_cents=10_000, commission_pct=13.0, shipping_cents=0, price_basis="ml")
        sid = _store_id(GD)
        m = StoreMatch(run_id=run.id, product_id="1", store_id=sid, item_id=7, rank=1, category=category,
                       auto_category=category, source="clip", title="Algo", url="https://www.gadnic.com.ar/x", price_cents=9_000)
        s.add(snap)
        s.add(m)
        s.commit()
        s.refresh(m)
        s.refresh(snap)
        return int(m.id), int(snap.id)


class Model:
    """Lo que dice la regla de producto: una marca, y la anterior si cambió de opinión."""

    def __init__(self, auto: str) -> None:
        self.auto, self.label, self.prev = auto, None, None

    def set(self, label: str) -> None:
        if self.label is None:
            self.label, self.prev = label, None
        elif self.label != label:
            self.label, self.prev = label, self.label

    def clear(self) -> None:
        if self.prev is not None:
            self.label, self.prev = self.prev, None
        else:
            self.label = None

    @property
    def category(self) -> str:
        return self.auto if self.label is None else ("igual" if self.label == "es" else "diferente")


@pytest.mark.parametrize("auto", ["igual", "similar", "diferente"])
@pytest.mark.parametrize("seed", range(12))
def test_label_and_undo_follow_a_simple_model_in_random_sequences(store_db, auto, seed):
    store_catalog.seed_default_stores()
    mid, _ = _seed_one_match(auto)
    rng = random.Random(seed)
    model = Model(auto)
    for step in range(40):
        op = rng.choice(["es", "no_es", "clear", "clear"])
        with Session(engine) as s:
            if op == "clear":
                store_match.clear_label(s, mid)
                model.clear()
            else:
                store_match.set_label(s, mid, op, actor="qa")
                model.set(op)
            s.commit()
        with Session(engine) as s:
            m = s.get(StoreMatch, mid)
            fb = s.exec(select(StoreMatchFeedback)).all()
            assert (m.human_label, m.category, m.auto_category) == (model.label, model.category, auto), (seed, step, op)
            if model.label is None:
                assert fb == [], (seed, step, op)
            else:
                assert len(fb) == 1 and (fb[0].label, fb[0].previous_label) == (model.label, model.prev), (seed, step, op)


def test_feedback_is_per_product_store_and_item(store_db):
    store_catalog.seed_default_stores()
    mid, _ = _seed_one_match("igual")
    with Session(engine) as s:
        base = s.get(StoreMatch, mid)
        clones = [
            StoreMatch(run_id=base.run_id, product_id="2", store_id=base.store_id, item_id=base.item_id, rank=1,
                       category="igual", auto_category="igual", title="Algo", url=base.url),            # otro producto
            StoreMatch(run_id=base.run_id, product_id="1", store_id=_store_id(CP), item_id=base.item_id, rank=1,
                       category="igual", auto_category="igual", title="Algo", url=base.url),            # otra tienda
            StoreMatch(run_id=base.run_id, product_id="1", store_id=base.store_id, item_id=99, rank=2,
                       category="igual", auto_category="igual", title="Otro", url=base.url),            # otro item
        ]
        s.add_all(clones)
        s.commit()
        ids = [c.id for c in clones]
    with Session(engine) as s:
        store_match.set_label(s, mid, "no_es")
        s.commit()
    with Session(engine) as s:
        assert [s.get(StoreMatch, i).human_label for i in ids] == [None, None, None]
        assert s.get(StoreMatch, mid).category == "diferente"
    loaded = store_match.load_feedback()
    assert loaded == {("1", _store_id(GD)): {7: "no_es"}}


# ─── el juez decide igual para ML y para la tienda ──────────────────────────


@pytest.mark.parametrize("category,confidence", [
    ("igual", 0.95), ("igual", 0.60), ("igual", 0.55), ("igual", 0.30), ("similar", 0.90), ("similar", 0.40), ("diferente", 0.95)])
async def test_the_judge_verdict_has_the_same_effect_on_an_ml_listing_and_on_a_store_item(sw, monkeypatch, category, confidence):
    """Banda ambigua (foto 0,62) con juez: se le pregunta lo mismo por la ficha de ML y por la de la tienda y se aplica
    igual (igual con confianza ≥ 0,60 → idéntico; entre 0,50 y 0,60 → similar; el resto → diferente)."""
    async def judge(our_name, our_images, candidates, *, max_calls, on_reserve=None, **kw):
        from app.pricing import daily_budget

        assert await daily_budget.reserve_async(market_judge.LLM_COUNTER_KEY, max_calls, None, on_reserve)
        return market_judge.JudgeResult(verdicts={c.ml_id: market_judge.JudgeVerdict(
            c.ml_id, category == "igual", confidence, "dice el juez", category=category,
            differences=("marca",) if category == "similar" else ()) for c in candidates})

    monkeypatch.setattr(market_judge, "enabled", lambda: True)
    monkeypatch.setattr(market_judge, "make_client", lambda: None)
    monkeypatch.setattr(market_judge, "judge", judge)
    _set("pm_vision_max_calls", 50)
    sw.ml.search["Organizador cocina"] = [_candidate("MLA77", "Organizador de cocina")]
    sw.ml.items["MLA77"] = [_listing("I77", "101", 200.0)]
    sw.image_scores[ML_IMG.format("MLA77")] = 0.62
    add_item(GD, "gd-77", "Organizador de cocina", 20_000, 0.62)
    await price_monitor.run_price_monitor()
    run_id = _runs()[-1].id
    ml_cat = _ml_category("MLA77", _snaps(run_id)["1"])
    with Session(engine) as s:
        m = s.exec(select(StoreMatch).where(StoreMatch.run_id == run_id)).one()
    assert (m.category, m.source) == (ml_cat, "llm"), (ml_cat, m.category, m.source, m.reason)
    expected = {("igual", 0.95): "igual", ("igual", 0.60): "igual", ("igual", 0.55): "similar", ("igual", 0.30): "diferente", ("similar", 0.90): "similar",
                ("similar", 0.40): "diferente", ("diferente", 0.95): "diferente"}[(category, confidence)]
    assert ml_cat == expected
