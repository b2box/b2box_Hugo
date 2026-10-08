"""QA independiente de «Siempre trae algo» (PR #20), criterios 1, 3, 4 y 5.

Complementa los tests del developer con:

  * un DORADO sacado de 9c6dc45 (la rama ANTES de «Siempre trae algo») con la web
    PRENDIDA y un mundo mezclado (idénticos por API y por web, solo similares, solo
    diferentes, banda ambigua, sin resultados, USD, pocas ventas, 429): todo lo que ya
    existía (snapshot real, contadores de la corrida, requests a ML y a la web) tiene
    que salir EXACTAMENTE igual. Lo nuevo (estimado, diferentes, estado) se mira aparte;
  * coherencia entre el botón y la próxima corrida: lo que la API deja en el snapshot al
    apretar "Es el mismo" / "No es el mismo" es lo que daría correr de nuevo con la marca;
  * el tope con muchos idénticos (tamaño de la fila y JSON servido);
  * el riesgo del color estimado (ambiguo sin juez y packs): se documenta con números.

Regenerar el dorado (solo desde 9c6dc45, nunca desde la rama):
    QA_WRITE_GOLDEN=1 pytest backend/tests/test_qa_siempre_trae_algo.py -k golden
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest
from sqlmodel import Session, select

from app.db.models import MarketMatchFeedback, MarketPriceSnapshot
from app.db.session import engine
from app.pricing import price_monitor
from tests.test_price_monitor import (  # noqa: F401
    FakeVendure,
    _candidate,
    _listing,
    _product,
    _runs,
    _set,
    _snaps,
    world,
)
from tests.test_semaforo_web import _card, _score, _web_page, webw  # noqa: F401

GOLDEN_PATH = Path(__file__).parent / "golden" / "semaforo_9c6dc45_web_on.json"
ML_IMG = "https://http2.mlstatic.com/D_NQ_{}.jpg"

SNAP_COLS = [
    "product_id", "ml_status", "ml_error", "ml_median_cents", "ml_min_cents", "ml_listing_count",
    "ml_seller_count", "ml_currency", "match_source", "match_confidence", "image_score_max",
    "name_score_max", "candidates_count", "ambiguous_count", "our_price_cents", "variant_id",
    "tier_used", "commission_pct", "shipping_cents", "est_margin_pct", "color", "prev_color",
    "product_name", "product_code", "match_origin", "web_searches", "web_bytes", "web_state"]
RUN_COLS = [
    "status", "mode", "total_products", "processed", "n_ok", "n_no_data", "n_failed", "n_skipped",
    "n_verde", "n_amarillo", "n_rojo", "n_sin_dato", "ml_requests_used", "llm_calls",
    "llm_input_tokens", "llm_output_tokens", "error", "web_searches", "web_bytes", "web_blocked",
    "n_web_ok", "web_status"]
MATCH_KEYS = ["ml_id", "title", "permalink", "listings", "min_cents", "median_cents", "source",
              "image_score", "name_score", "confidence", "origin", "prices_cents", "seller"]


def _entries(raw):
    return json.loads(raw) if raw else []


# ─── el mundo mezclado (se arma igual en 9c6dc45 y en la rama) ────────────


def _mixed_world(w):
    """API + web prendidas, sin juez. Un producto de cada situación."""
    FakeVendure.products = [
        _product("1", "Organizador cocina"),                     # idéntico por API (+ un ambiguo y un impostor)
        _product("2", "Producto raro"),                          # web: idéntico + pack similar + impostor
        _product("3", "Lampara LED escritorio"),                 # web: solo packs (similares)
        _product("4", "Cable usb tipo c reforzado"),             # web: solo impostores
        _product("5", "Mate de calabaza"),                       # web: nada
        _product("6", "Botella termica acero"),                  # web: idéntico con pocas ventas + similar
        _product("7", "Cargador inalambrico rapido"),            # API 429
        _product("8", "Reloj digital"),                          # web: idéntico en USD
        _product("9", "Funda silicona celular"),                 # web: solo banda ambigua
        _product("10", "Soporte celular auto magnetico"),        # web: dos idénticos + uno sin ventas
    ]
    ml = w.ml
    ml.search.update({
        "Organizador cocina": [_candidate("MLA1", "Organizador de cocina"),
                               _candidate("MLA2", "Organizador de pared multiuso"),
                               _candidate("MLA3", "Zapatilla running")],
        "Cargador inalambrico rapido": 429,
    })
    w.image_scores[ML_IMG.format("MLA2")] = 0.50           # banda ambigua
    w.image_scores[ML_IMG.format("MLA3")] = 0.10           # impostor
    pages = w.web.pages
    pages["producto-raro"] = _web_page(
        _card("MLA901", "Producto Raro", 250.0), _card("MLA902", "Pack X6 Producto Raro", 900.0, seller="Dos"),
        _card("MLA903", "Heladera no frost", 5000.0, seller="Tres"))
    pages["lampara-led-escritorio"] = _web_page(
        _card("MLA911", "Pack X3 Lampara LED escritorio", 300.0), _card("MLA912", "Set X2 Lampara LED escritorio", 220.0, seller="Dos"))
    pages["cable-usb-tipo-c-reforzado"] = _web_page(
        _card("MLA921", "Zapatilla running", 990.0), _card("MLA922", "Heladera no frost 400 L", 1500.0, seller="Dos"))
    pages["botella-termica-acero"] = _web_page(
        _card("MLA931", "Botella termica acero", 400.0, sold=2), _card("MLA932", "Pack X4 Botella termica acero", 1500.0, seller="Dos"))
    pages["reloj-digital"] = _web_page(_card("MLA941", "Reloj digital", 80.0, currency="USD"))
    pages["funda-silicona-celular"] = _web_page(
        _card("MLA951", "Funda silicona celular iPhone", 130.0), _card("MLA952", "Funda celular transparente", 90.0, seller="Dos"))
    pages["soporte-celular-auto-magnetico"] = _web_page(
        _card("MLA961", "Soporte celular auto magnetico", 120.0), _card("MLA962", "Soporte celular auto magnetico", 140.0, seller="Dos"),
        _card("MLA963", "Soporte celular auto magnetico", 100.0, seller="Tres", sold=1))
    for ref, v in {"MLA901": 0.92, "MLA902": 0.95, "MLA903": 0.20, "MLA911": 0.93, "MLA912": 0.91,
                   "MLA921": 0.15, "MLA922": 0.12, "MLA931": 0.90, "MLA932": 0.94, "MLA941": 0.9,
                   "MLA951": 0.50, "MLA952": 0.55, "MLA961": 0.9, "MLA962": 0.88, "MLA963": 0.87}.items():
        _score(w, ref, v)


def _dump(w) -> dict:
    snaps = {}
    for pid, s in sorted(_snaps().items(), key=lambda kv: int(kv[0])):
        d = {c: getattr(s, c) for c in SNAP_COLS}
        d["matched_listings"] = [{k: m.get(k) for k in MATCH_KEYS} for m in _entries(s.matched_listings)]
        snaps[pid] = d
    run = _runs()[-1]
    return {"snaps": snaps, "run": {c: getattr(run, c) for c in RUN_COLS},
            "ml_calls": sorted(w.ml.calls), "web_calls": sorted(w.web.calls)}


def _roundtrip(d: dict) -> dict:
    return json.loads(json.dumps(d, sort_keys=True, default=str))


@pytest.mark.parametrize("spec_check", [1, 0])
async def test_golden_everything_that_already_existed_is_exactly_as_in_9c6dc45(webw, spec_check):
    _set("pm_spec_check", spec_check)
    _mixed_world(webw)
    await price_monitor.run_price_monitor()
    got = _roundtrip(_dump(webw))
    if os.environ.get("QA_WRITE_GOLDEN"):
        data = json.loads(GOLDEN_PATH.read_text()) if GOLDEN_PATH.exists() else {}
        data[f"spec_check_{spec_check}"] = got
        GOLDEN_PATH.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
        return
    want = json.loads(GOLDEN_PATH.read_text())[f"spec_check_{spec_check}"]
    assert got["run"] == want["run"], "contadores de la corrida (reales) cambiaron"
    assert got["ml_calls"] == want["ml_calls"], "requests a la API de ML cambiaron"
    assert got["web_calls"] == want["web_calls"], "búsquedas web cambiaron"
    assert got["snaps"].keys() == want["snaps"].keys()
    diffs = {}
    for pid, row in want["snaps"].items():
        for col, value in row.items():
            if got["snaps"][pid][col] != value:
                diffs[(pid, col)] = (value, got["snaps"][pid][col])
    # Lo único que cambia a propósito: la nota ahora cuenta también los similares "sin confirmar"
    # ("ninguna publicación es igual" -> "... (2 similares)"). Nada de lo real se mueve.
    for (pid, col), (before, after) in list(diffs.items()):
        if col == "ml_error" and re.fullmatch(re.escape(before) + r" \(\d+ similares?\)", after):
            del diffs[(pid, col)]
    assert diffs == {}, "lo real cambió"


# ─── criterio 3: el botón deja el snapshot como lo dejaría la próxima corrida ─


from fastapi.testclient import TestClient

from app import auth
from app import main as main_mod
from app.db.models import PriceMonitorRun
from tests.test_price_monitor_routes import _env, client  # noqa: F401

REAL_KEYS = ["ml_status", "color", "ml_median_cents", "ml_min_cents", "ml_listing_count", "ml_seller_count",
             "est_margin_pct", "match_state", "estimated_color", "estimated_median_cents",
             "estimated_margin_pct", "estimated_listing_count", "estimated_from", "similar_count", "other_count"]
LIST_KEYS = ["matched_listings", "unpriced_listings", "similar_listings", "other_listings"]
COUNTERS = ["n_ok", "n_no_data", "n_failed", "n_skipped", "n_verde", "n_amarillo", "n_rojo", "n_sin_dato",
            "n_con_similares", "n_est_verde", "n_est_amarillo", "n_est_rojo", "n_solo_diferentes", "processed"]


def _shape(d: dict) -> dict:
    """Lo que el dashboard ve de un producto: números y quién está en cada lista."""
    out = {k: d[k] for k in REAL_KEYS}
    out["lists"] = {k: sorted(m["ml_id"] for m in d[k]) for k in LIST_KEYS}
    return out


def _counters(run_id: int) -> dict:
    with Session(engine) as s:
        run = s.get(PriceMonitorRun, run_id)
        return {c: getattr(run, c) for c in COUNTERS}


def _snap_id(pid: str, run_id: int) -> int:
    with Session(engine) as s:
        return s.exec(select(MarketPriceSnapshot).where(
            MarketPriceSnapshot.product_id == pid, MarketPriceSnapshot.run_id == run_id)).one().id


def _correction_world(w):
    """Un producto con un idéntico, un pack similar, un ambiguo y un impostor; otro producto con solo
    similares (para comprobar que sus contadores y su fila no se mueven con las marcas del primero)."""
    FakeVendure.products = [_product("3", "Producto raro"), _product("4", "Mate de calabaza")]
    w.web.pages["producto-raro"] = _web_page(
        _card("MLA901", "Producto Raro", 250.0), _card("MLA902", "Pack X6 Producto Raro", 900.0, seller="Dos"),
        _card("MLA903", "Heladera no frost", 5000.0, seller="Tres"),
        _card("MLA904", "Producto raro premium", 400.0, seller="Cuatro"))
    w.web.pages["mate-de-calabaza"] = _web_page(
        _card("MLA901", "Pack X3 Mate de calabaza", 300.0), _card("MLA902", "Pack X6 Mate de calabaza", 600.0, seller="Dos"))
    for ref, v in {"MLA901": 0.92, "MLA902": 0.95, "MLA903": 0.20, "MLA904": 0.50}.items():
        _score(w, ref, v)


async def test_the_button_leaves_the_snapshot_as_the_next_run_with_the_mark_would(webw, client):
    """Para cada click ("Es el mismo" sobre un similar, un ambiguo, un diferente; "No es el mismo" sobre
    el idéntico): lo que devuelve la API es lo que da correr de nuevo con esa marca. Los ids MLA901/902
    se repiten en el otro producto: la marca de uno no toca al otro."""
    _correction_world(webw)
    await price_monitor.run_price_monitor()
    base_run = _runs()[-1].id
    other_before = _shape(price_monitor.snapshot_to_dict(_snaps(base_run)["4"]))

    steps = [("same", "MLA902"), ("same", "MLA904"), ("same", "MLA903"), ("not-same", "MLA901"),
             ("not-same", "MLA902"), ("same", "MLA901")]
    for path, ml_id in steps:
        sid = _snap_id("3", base_run)
        r = client.post(f"/api/price-monitor/snapshots/{sid}/{path}", json={"ml_id": ml_id})
        assert r.status_code == 200, (path, ml_id, r.text)
        button = _shape(r.json()["snapshot"])
        # contadores de la corrida: los de la API == los que daría contar de cero
        after_button = _counters(base_run)
        with Session(engine) as s:
            run = s.get(PriceMonitorRun, base_run)
            price_monitor.recount_run(s, base_run)
            s.commit()
            s.refresh(run)
        assert _counters(base_run) == after_button, f"{path} {ml_id}: contadores desactualizados"

        await price_monitor.run_price_monitor()
        rerun = _runs()[-1]
        assert rerun.id != base_run
        fresh = _shape(price_monitor.snapshot_to_dict(_snaps(rerun.id)["3"]))
        assert button == fresh, f"{path} {ml_id}: el botón y la próxima corrida no coinciden"
        # …y los contadores de la corrida ya corregida == los de la corrida nueva
        assert _counters(base_run) == _counters(rerun.id), f"{path} {ml_id}: contadores del botón vs corrida"
        # el otro producto (mismos ml_id) no se movió
        assert _shape(price_monitor.snapshot_to_dict(_snaps(rerun.id)["4"])) == other_before
        assert _shape(price_monitor.snapshot_to_dict(_snaps(base_run)["4"])) == other_before


async def test_undo_leaves_the_marks_exactly_as_before_and_the_next_run_goes_back_to_the_start(webw, client):
    _correction_world(webw)
    await price_monitor.run_price_monitor()
    run0 = _runs()[-1].id
    start = _shape(price_monitor.snapshot_to_dict(_snaps(run0)["3"]))
    start_counters = _counters(run0)
    sid = _snap_id("3", run0)

    for path in ("same", "not-same"):
        ml_id = "MLA902" if path == "same" else "MLA901"
        assert client.post(f"/api/price-monitor/snapshots/{sid}/{path}", json={"ml_id": ml_id}).status_code == 200
        with Session(engine) as s:
            [mark] = s.exec(select(MarketMatchFeedback)).all()
            assert (mark.product_id, mark.ml_id, mark.label) == ("3", ml_id, 1 if path == "same" else 0)
        assert client.delete(f"/api/price-monitor/products/3/feedback/{ml_id}").json() == {"removed": True}
        with Session(engine) as s:
            assert s.exec(select(MarketMatchFeedback)).all() == []              # sin marcas: como antes
        await price_monitor.run_price_monitor()
        again = _runs()[-1].id
        assert _shape(price_monitor.snapshot_to_dict(_snaps(again)["3"])) == start
        assert _counters(again) == start_counters
        run0, sid = again, _snap_id("3", again)


# ─── criterio 4: muchos idénticos (entran todos) ──────────────────────────


LONG_TITLE = "Soporte celular auto magnetico reforzado con ventosa y base giratoria 360 grados premium negro"


async def test_many_identicals_all_enter_and_the_row_and_the_json_stay_small(webw, client):
    """24 resultados (el máximo de pm_ml_web_max_results), todos idénticos, más de 8: entran todos, no
    queda lugar para similares/diferentes, el JSON sale entero y la fila pesa poco."""
    _set("pm_ml_web_max_results", 24)
    _set("pm_ml_keep_listings", 8)
    FakeVendure.products = [_product("3", "Soporte celular auto magnetico")]
    cards = [_card(f"MLA{1000 + i}", f"{LONG_TITLE} {i}", 100.0 + i, seller=f"Vendedor con nombre largo {i}" * 2)
             for i in range(24)]
    webw.web.pages["soporte-celular-auto-magnetico"] = _web_page(*cards)
    for i in range(24):
        _score(webw, f"MLA{1000 + i}", 0.90 - i * 0.001)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    matched = _entries(s.matched_listings)
    assert s.ml_status == "ok" and len(matched) == 24                       # entran todos
    assert (s.similar_count, s.other_count) == (0, 0)                       # y no queda lugar para lo demás
    assert s.ml_listing_count == 24
    row_bytes = sum(len((getattr(s, c) or "").encode()) for c in (
        "matched_listings", "unpriced_listings", "similar_listings", "other_listings", "our_specs", "ml_error"))
    assert row_bytes < 40_000, row_bytes                                    # ~1,4 KB por publicación
    body = client.get("/api/price-monitor/snapshots").json()
    [item] = body["items"]
    assert len(item["matched_listings"]) == 24 and body["colors"]["verde"] + body["colors"]["amarillo"] + body["colors"]["rojo"] == 1
    json.dumps(item)                                                        # serializable entero
    assert len(json.dumps(item)) < 60_000


async def test_identical_priced_ones_beyond_the_cap_leave_no_room_but_the_extra_unpriced_ones_do_not_break_it(webw, client):
    """9 idénticos con precio + 5 sin ventas: ninguno se recorta, los sin precio quedan en su lista."""
    _set("pm_ml_web_max_results", 24)
    FakeVendure.products = [_product("3", "Soporte celular auto magnetico")]
    cards = [_card(f"MLA{1100 + i}", f"Soporte celular auto magnetico {i}", 100.0 + i) for i in range(9)]
    cards += [_card(f"MLA{1200 + i}", f"Soporte celular auto magnetico modelo {i}", 300.0, seller=f"V{i}", sold=1) for i in range(5)]
    webw.web.pages["soporte-celular-auto-magnetico"] = _web_page(*cards)
    for i in range(9):
        _score(webw, f"MLA{1100 + i}", 0.9)
    for i in range(5):
        _score(webw, f"MLA{1200 + i}", 0.9)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert (len(_entries(s.matched_listings)), len(_entries(s.unpriced_listings))) == (9, 5)
    assert s.similar_count == 0 and s.other_count == 0
    item = client.get("/api/price-monitor/snapshots").json()["items"][0]
    assert len(item["matched_listings"]) == 9 and len(item["unpriced_listings"]) == 5
    assert item["match_state"] == "igual"


# ─── criterio 2: la fórmula y los cortes del estimado son los del color real ──


@pytest.mark.parametrize("ours", [1, 100, 7_777, 10_000, 123_457])
@pytest.mark.parametrize("commission,shipping", [(0.0, 0), (13.0, 0), (13.0, 2_500), (25.5, 999)])
def test_the_estimate_uses_exactly_the_formula_and_cuts_of_the_real_color(ours, commission, shipping):
    """Para una grilla (incluidos los bordes de 30 % y 10 %) el color/ganancia ESTIMADO de un similar a precio P
    es el mismo que el color/ganancia REAL de un idéntico a precio P."""
    for green, yellow in ((30.0, 10.0), (50.0, 0.0), (10.0, 10.0)):
        # precios justo en los cortes: el que da exactamente `green` %, un centavo menos y un centavo más
        edge = [round((ours * (1 + pct / 100.0) + shipping) / (1 - commission / 100.0)) + d
                for pct in (green, yellow, 0.0) for d in (-1, 0, 1)]
        for price in [*edge, ours // 2 or 1, ours, ours * 3]:
            if price <= 0:
                continue
            real = MarketPriceSnapshot(product_id="r", run_id=1, our_price_cents=ours, commission_pct=commission,
                                       shipping_cents=shipping)
            price_monitor._apply_prices(real, [price], 1, green_min=green, yellow_min=yellow)
            est = MarketPriceSnapshot(product_id="e", run_id=1, our_price_cents=ours, commission_pct=commission,
                                      shipping_cents=shipping, ml_status="no_data")
            sim = [{"ml_id": "MLA1", "price_cents": price, "est_ok": True}]
            price_monitor._set_estimate(est, sim, green_min=green, yellow_min=yellow)
            assert (est.estimated_color, est.estimated_margin_pct, est.estimated_median_cents) == (
                real.color, real.est_margin_pct, real.ml_median_cents), (ours, commission, shipping, green, yellow, price)
            assert est.color is None or est.color != "verde"          # el real del que estima nunca se toca
            assert est.ml_median_cents is None and est.est_margin_pct is None


def test_an_estimate_is_never_computed_for_a_product_with_a_real_price_or_that_failed():
    for status in ("ok", "failed", "skipped"):
        s = MarketPriceSnapshot(product_id="x", run_id=1, our_price_cents=10_000, commission_pct=13.0, shipping_cents=0,
                                ml_status=status)
        price_monitor._set_estimate(s, [{"ml_id": "MLA1", "price_cents": 50_000, "est_ok": True}],
                                    green_min=30.0, yellow_min=10.0)
        assert s.estimated_color is None and s.estimated_listing_count == 0, status


# ─── criterio 3: marcas de un producto no pisan a otro (también "Es el mismo") ─


async def test_a_same_mark_on_another_product_does_not_promote_the_card_here(webw):
    webw.web.pages["producto-raro"] = _web_page(_card("MLA901", "Heladera no frost", 5000.0))
    _score(webw, "MLA901", 0.20)
    with Session(engine) as s:
        s.add(MarketMatchFeedback(product_id="99", ml_id="MLA901", label=1))
        s.commit()
    FakeVendure.products = [_product("3", "Producto raro")]
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert (s.ml_status, s.match_state, s.other_count) == ("no_data", "diferente", 1)


def test_auth_and_validation_of_the_new_endpoints(client):
    anon = TestClient(main_mod.app)
    for call in (lambda: anon.post("/api/price-monitor/snapshots/1/same", json={"ml_id": "MLA1"}),
                 lambda: anon.post("/api/price-monitor/snapshots/1/not-same", json={"ml_id": "MLA1"}),
                 lambda: anon.delete("/api/price-monitor/products/1/feedback/MLA1"),
                 lambda: anon.delete("/api/price-monitor/products/1/not-same/MLA1")):
        assert call().status_code == 401
    # sesión inválida / vencida
    bad = TestClient(main_mod.app)
    bad.cookies.set(auth.COOKIE_NAME, "no-es-un-token")
    assert bad.post("/api/price-monitor/snapshots/1/same", json={"ml_id": "MLA1"}).status_code == 401
    # estimated= fuera de la lista y SQL en los filtros
    assert client.get("/api/price-monitor/snapshots", params={"estimated": "azul"}).status_code == 400
    assert client.get("/api/price-monitor/snapshots", params={"match": "x' OR '1'='1"}).status_code == 400
    assert client.get("/api/price-monitor/snapshots", params={"estimated": "any'--"}).status_code == 400


# ─── estados vs filas fallidas (cómo se cuentan) ──────────────────────────


async def test_a_failed_product_with_stored_lists_has_no_state_and_no_estimate(webw, client):
    """API 429 + la web trae solo packs: queda failed (como en 9c6dc45), con las listas guardadas."""
    FakeVendure.products = [_product("2", "Lampara LED escritorio")]          # el mundo base le da 429 por API
    webw.web.pages["lampara-led-escritorio"] = _web_page(
        _card("MLA911", "Pack X3 Lampara LED escritorio", 300.0), _card("MLA912", "Heladera no frost", 9000.0, seller="Dos"))
    _score(webw, "MLA911", 0.93)
    _score(webw, "MLA912", 0.10)
    await price_monitor.run_price_monitor()
    s = _snaps()["2"]
    assert (s.ml_status, s.match_state, s.estimated_color, s.color) == ("failed", None, None, "sin_dato")
    assert (s.similar_count, s.other_count) == (1, 1)
    run = _runs()[-1]
    assert (run.n_failed, run.n_est_verde + run.n_est_amarillo + run.n_est_rojo, run.n_solo_diferentes) == (1, 0, 0)
    body = client.get("/api/price-monitor/snapshots").json()
    assert body["items"][0]["match_state"] is None
    # OBSERVACIÓN: un failed con listas guardadas SÍ cuenta en los chips por estado (solo_similar = 1) aunque su
    # estado sea None y no figure ni en el contador de la corrida ni en el color estimado.
    assert body["states"]["solo_similar"] == 1


# ─── criterio 5: propuestas de QA (xfail estricto: pasan cuando se implementen) ──


PROPOSAL = "PROPUESTA QA (observación, no bug del PR): ver reporte; si se acepta, sacar el xfail"


@pytest.mark.xfail(strict=True, reason=PROPOSAL + " - el estimado no debería salir de similares 'sin confirmar'")
async def test_proposal_unconfirmed_similars_do_not_feed_the_estimate(webw):
    """Sin juez (pm_vision_max_calls=0, el default) toda la banda ambigua (foto 0,40 a 0,80 sin llegar a foto+nombre)
    queda 'similar sin confirmar'. Acá, dos fundas que por foto 0,50/0,55 y nombre >= 0,6 pueden ser de otro
    modelo, y el estimado ya dice 'rojo' con su mediana."""
    FakeVendure.products = [_product("3", "Funda silicona celular")]
    webw.web.pages["funda-silicona-celular"] = _web_page(
        _card("MLA951", "Funda silicona celular iPhone", 130.0), _card("MLA952", "Funda celular transparente", 90.0, seller="Dos"))
    _score(webw, "MLA951", 0.50)
    _score(webw, "MLA952", 0.55)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert [e["source"] for e in _entries(s.similar_listings)] == ["ambiguo", "ambiguo"]      # lo que pasa hoy
    assert s.estimated_color is None


@pytest.mark.xfail(strict=True, reason=PROPOSAL + " - un pack distinto no tiene un precio comparable")
async def test_proposal_similars_that_differ_in_quantity_do_not_feed_the_estimate(webw):
    """Nuestro precio $100 (una unidad). 'Pack X4' a $1.500 es SIMILAR por cantidad: hoy su precio entero da
    'verde estimado' con 1.205 % de ganancia."""
    FakeVendure.products = [_product("3", "Botella termica acero")]
    webw.web.pages["botella-termica-acero"] = _web_page(_card("MLA932", "Pack X4 Botella termica acero", 1500.0))
    _score(webw, "MLA932", 0.94)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    [e] = _entries(s.similar_listings)
    assert e["differences"] == ["cantidad"] and e["source"] == "specs"                       # lo que pasa hoy
    assert s.estimated_color is None


# ─── hallazgo: "No es el mismo" con más idénticos que el tope ─────────────


@pytest.mark.xfail(strict=True, reason="BUG menor (repro): con idénticos >= pm_ml_keep_listings, la publicación marcada "
                                       "'No es el mismo' no cabe en ninguna lista y desaparece del detalle de hoy")
async def test_not_the_same_with_more_identicals_than_the_cap_keeps_the_card_visible(webw, client):
    _set("pm_ml_web_max_results", 24)
    _set("pm_ml_keep_listings", 8)
    FakeVendure.products = [_product("3", "Soporte celular auto magnetico")]
    webw.web.pages["soporte-celular-auto-magnetico"] = _web_page(
        *[_card(f"MLA{1100 + i}", f"Soporte celular auto magnetico {i}", 100.0 + i) for i in range(10)])
    for i in range(10):
        _score(webw, f"MLA{1100 + i}", 0.9)
    await price_monitor.run_price_monitor()
    run_id = _runs()[-1].id
    r = client.post(f"/api/price-monitor/snapshots/{_snap_id('3', run_id)}/not-same", json={"ml_id": "MLA1103"})
    snap = r.json()["snapshot"]
    assert len(snap["matched_listings"]) == 9                                # los otros 9 siguen
    shown = {m["ml_id"] for k in LIST_KEYS for m in snap[k]}
    assert "MLA1103" in shown                                                # ← hoy NO está en ninguna lista
