"""QA3, criterio 4: un resultado fresco de la oficina entra por el MISMO pipeline de matching que la web del servidor.

Diferencial a escala: un mundo azaroso de 36 productos (idénticos, packs, impostores, pocas ventas, USD, banda ambigua, marca
declarada, más publicaciones que el tope, API en 429 o sin cupo, nada en ML) se corre dos veces con las MISMAS páginas de ML: una
buscada por el servidor (fuente web) y otra traída por la Mac (la página pasa por `parse_search` → `candidate_to_wire` → POST /
`ingest` → `load_fresh`). Todo el snapshot tiene que ser igual salvo el origen. Más los bordes del TTL, `empty`, `blocked` y
«en lugar de» cuando la oficina buscó y no hay idénticos.
"""

from __future__ import annotations

import json
import os
import random
from datetime import timedelta
from urllib.parse import quote

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import runtime  # noqa: E402
from app.clock import utcnow  # noqa: E402
from app.db.models import MarketPriceSnapshot, MlWebResult, PriceMonitorRun, Setting  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import market_match, market_ml, market_ml_web, oficina_ml, price_monitor  # noqa: E402
from tests import qa2_world as qw  # noqa: E402
from tests.ml_web_fixtures import ld_product  # noqa: E402
from tests.qa3_titles import REALISTAS  # noqa: E402
from tests.test_oficina_semaforo import _clean_results, _store, _wire  # noqa: E402,F401
from tests.test_price_monitor import FakeVendure, _candidate, _listing, _product, _runs, _set, _snaps, world  # noqa: E402,F401
from tests.test_price_monitor_routes import _env, client  # noqa: E402,F401
from tests.test_semaforo_web import _card, _score, _web_page, webw  # noqa: E402,F401
from tests.qa3_cleanup import qa3_clean  # noqa: E402,F401  (fixture autouse)

SKIP_COLS = {"id", "run_id", "captured_at", "match_origin", "web_state", "web_via", "web_searches", "web_bytes", "ml_error",
             "matched_listings", "unpriced_listings", "similar_listings", "other_listings", "ml_variant"}
LIST_COLS = ("matched_listings", "unpriced_listings", "similar_listings", "other_listings")
KINDS = ["ident2", "ident2", "ident_pack", "ident_pocas_ventas", "solo_packs", "solo_impostores", "usd", "ambiguo", "marca",
         "mas_que_el_tope", "api_429_y_web", "api_ok", "nada", "nada"]


def _world(w, seed: int, n: int = 36) -> dict[str, dict]:
    """Productos + páginas de ML (para el servidor) + puntajes de foto. Devuelve, por producto, el HTML de su página (o None)."""
    rng = random.Random(seed)
    titles = rng.sample(REALISTAS, n)
    FakeVendure.products = [_product(str(i), t, price=rng.choice([8_000, 12_000, 30_000])) for i, t in enumerate(titles, 1)]
    pages: dict[str, dict] = {}
    for i, title in enumerate(titles, 1):
        kind, pid = rng.choice(KINDS), str(i)
        q = market_match.search_query(title)
        slug = market_ml_web.slugify(q)
        r = [f"MLA{i}{k:02d}" for k in range(1, 15)]
        cards: list = []
        ld = None
        if kind == "ident2":
            cards = [_card(r[0], title, 250.0), _card(r[1], title, 300.0, seller="Dos")]
            scores = {r[0]: 0.92, r[1]: 0.88}
        elif kind == "ident_pack":
            cards = [_card(r[0], title, 250.0), _card(r[1], f"Pack X6 {title}", 900.0, seller="Dos"), _card(r[2], "Heladera no frost", 5000.0, seller="Tres")]
            scores = {r[0]: 0.92, r[1]: 0.95, r[2]: 0.2}
        elif kind == "ident_pocas_ventas":
            cards = [_card(r[0], title, 80.0, sold=2), _card(r[1], title, 300.0, seller="Dos", sold=None)]
            scores = {r[0]: 0.9, r[1]: 0.9}
        elif kind == "solo_packs":
            cards = [_card(r[0], f"Pack X3 {title}", 700.0), _card(r[1], f"Set X2 {title}", 500.0, seller="Dos")]
            scores = {r[0]: 0.93, r[1]: 0.91}
        elif kind == "solo_impostores":
            cards = [_card(r[0], "Zapatilla running", 990.0), _card(r[1], "Heladera no frost 400 L", 1500.0, seller="Dos")]
            scores = {r[0]: 0.15, r[1]: 0.12}
        elif kind == "usd":
            cards = [_card(r[0], title, 80.0, currency="USD"), _card(r[1], title, 250.0, seller="Dos")]
            scores = {r[0]: 0.9, r[1]: 0.9}
        elif kind == "ambiguo":
            cards = [_card(r[0], f"{title} compatible", 130.0), _card(r[1], "Accesorio similar", 90.0, seller="Dos")]
            scores = {r[0]: 0.5, r[1]: 0.55}
        elif kind == "marca":
            cards = [_card(r[0], f"{title} Stanley", 400.0, catalog="MLA29003349", url="www.mercadolibre.com.ar/x/p/MLA29003349"),
                     _card(r[1], f"{title} generico", 180.0, seller="Dos")]
            ld = [ld_product(f"{title} Stanley", "https://www.mercadolibre.com.ar/x/p/MLA29003349", 400, brand="Stanley"),
                  ld_product(f"{title} generico", "https://articulo.mercadolibre.com.ar/MLA-1-x", 180, brand="Generica")]
            scores = {r[0]: 0.92, r[1]: 0.91}
        elif kind == "mas_que_el_tope":
            cards = [_card(r[k], title, 100.0 + 10 * k, seller=f"V{k}") for k in range(12)]
            scores = {r[k]: 0.9 for k in range(12)}
        elif kind == "api_429_y_web":
            w.ml.search[q] = 429
            cards = [_card(r[0], title, 250.0)]
            scores = {r[0]: 0.9}
        elif kind == "api_ok":
            w.ml.search[q] = [_candidate(f"MLA{i}99", title)]
            w.ml.items[f"MLA{i}99"] = [_listing(f"I{i}", f"{i}01", 200.0)]
            w.ml.users[f"{i}01"] = 900
            w.image_scores[f"https://http2.mlstatic.com/D_NQ_{f'MLA{i}99'}.jpg"] = 0.9
            cards = [_card(r[0], title, 100.0)]
            scores = {r[0]: 0.9}
        else:
            scores = {}
        for ref, v in scores.items():
            _score(w, ref, v)
        pages[pid] = {"kind": kind, "slug": slug, "page": _web_page(*cards, ld=ld) if cards else None}
    return pages


def _reset_run() -> None:
    """Corrida y resultados en blanco, y el cupo del día de ML de nuevo en cero (una corrida sin cupo ni arranca)."""
    with Session(engine) as s:
        for model in (MarketPriceSnapshot, PriceMonitorRun, MlWebResult):
            for row in s.exec(select(model)).all():
                s.delete(row)
        for key in (market_ml.ML_COUNTER_KEY, market_ml_web.WEB_COUNTER_KEY):
            row = s.get(Setting, key)
            if row is not None:
                s.delete(row)
        s.commit()


def _norm_listings(raw: str | None) -> list[dict]:
    return [{k: v for k, v in m.items() if k != "origin"} for m in json.loads(raw or "[]")]


@pytest.mark.parametrize("config", ["defecto", "juez_prendido", "sin_specs", "cupo_api_corto"])
@pytest.mark.parametrize("seed", [3, 77])
async def test_oficina_is_the_server_web_search_column_by_column_except_the_origin(webw, monkeypatch, seed, config):
    _set("pm_ml_concurrency", 1)
    # Este test compara el MISMO filtro con otra puerta de entrada; el chequeo de plausibilidad de precios de la oficina (que la
    # web del servidor no tiene a propósito) se prueba aparte en test_oficina_semaforo.py.
    monkeypatch.setattr(price_monitor, "_implausible_price", lambda c, our_price: False)
    judge_calls: list = []
    if config == "juez_prendido":
        _set("pm_vision_max_calls", 500)
        qw.install_judge(monkeypatch, judge_calls)
    elif config == "sin_specs":
        _set("pm_spec_check", 0)
    elif config == "cupo_api_corto":
        _set("pm_ml_daily_budget", 7)
    pages = _world(webw, seed)
    for pid, info in pages.items():
        if info["page"] is not None:
            webw.web.pages[quote(info["slug"])] = info["page"]

    # 1) el servidor busca en la web
    await price_monitor.run_price_monitor()
    server = {k: v for k, v in _snaps().items()}
    server_judge = sorted(judge_calls)
    assert any(s.match_origin == "web" for s in server.values()), "el mundo tiene que tener productos resueltos por la web"
    kinds_seen = {pages[pid]["kind"] for pid, s in server.items() if s.match_origin == "web"}

    # 2) la Mac trae las MISMAS páginas
    _reset_run()
    judge_calls.clear()
    webw.web.calls.clear()
    wires = []
    for pid, info in pages.items():
        if info["page"] is None:
            status, cands = "empty", []
        else:
            parsed = market_ml_web.parse_search(info["page"].html, int(runtime.get("pm_ml_web_max_results")))
            status, cands = ("ok", [oficina_ml.candidate_to_wire(c) for c in parsed.candidates]) if parsed.candidates else ("empty", [])
        wires.append({"product_id": pid, "query": "q", "fetched_at": (utcnow() - timedelta(hours=2)).isoformat() + "Z",
                      "status": status, "reason": "", "candidates": cands})
    with Session(engine) as s:                                   # Hugo ya conoce a todos los productos
        for pid in pages:
            s.add(MarketPriceSnapshot(run_id=0, product_id=pid, ml_status="no_data", product_name=f"p{pid}"))
        s.commit()
    for j in range(0, len(wires), 50):
        rep = oficina_ml.ingest(wires[j:j + 50])
        assert rep.rejected == [], rep.rejected
    with Session(engine) as s:                                   # y se saca la marca de arranque
        for row in s.exec(select(MarketPriceSnapshot)).all():
            s.delete(row)
        s.commit()
    await price_monitor.run_price_monitor()
    office = {k: v for k, v in _snaps().items()}
    assert webw.web.calls == [], "con resultado fresco el servidor no busca en ML"

    assert office.keys() == server.keys()
    diffs = {}
    for pid in server:
        a, b = server[pid], office[pid]
        for col in (c.name for c in MarketPriceSnapshot.__table__.columns if c.name not in SKIP_COLS):
            if getattr(a, col) != getattr(b, col):
                diffs[(pid, pages[pid]["kind"], col)] = (getattr(a, col), getattr(b, col))
        for col in LIST_COLS:
            if _norm_listings(getattr(a, col)) != _norm_listings(getattr(b, col)):
                diffs[(pid, pages[pid]["kind"], col)] = "distinto"
        assert (a.match_origin, b.match_origin) in {("web", "oficina"), (None, None), ("api", "api")}, (
            pid, pages[pid]["kind"], FakeVendure.products[int(pid) - 1].name, a.match_origin, b.match_origin, a.ml_status, a.ml_error, a.web_state, b.ml_error)
        assert (a.ml_error or "").split(" · ML web")[0] == (b.ml_error or "").split(" · ML web")[0], pid        # lo de la API es igual
        assert a.web_state == b.web_state or (a.web_state, b.web_state) == (None, None), (pid, a.web_state, b.web_state)
    assert diffs == {}, f"{config}/{seed}: la oficina no da lo mismo que la web del servidor"
    assert sorted(judge_calls) == server_judge, "el juez recibe las mismas preguntas"
    assert {pages[p]["kind"] for p, s in office.items() if s.match_origin == "oficina"} == kinds_seen
    run = _runs()[-1]
    assert run.n_oficina_ok == sum(1 for s in office.values() if s.match_origin == "oficina") and run.oficina_fresh == len(pages)
    assert run.web_searches == 0 and run.n_web_ok == 0


async def test_the_real_color_comes_only_from_identicals_from_the_oficina(webw):
    """Mismos precios en una publicación idéntica y en otra «similar» (pack) mucho más barata: el color real sale de la idéntica."""
    FakeVendure.products = [_product("3", "Producto raro", price=10_000)]
    _store("3", [_wire("MLA901", "Producto Raro", 250.0), _wire("MLA902", "Pack X6 Producto Raro", 20.0, seller="Dos")])
    _score(webw, "MLA901", 0.92)
    _score(webw, "MLA902", 0.95)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert (s.ml_median_cents, s.color, s.match_origin) == (25_000, "verde", "oficina")
    assert json.loads(s.similar_listings)[0]["ml_id"] == "MLA902"


# ─── los bordes de lo fresco ────────────────────────────────────────────────


@pytest.mark.parametrize("age, used", [(timedelta(days=6, hours=23, minutes=59), True), (timedelta(days=7, seconds=-30), True),
                                       (timedelta(days=7, seconds=30), False), (timedelta(days=30), False)])
def test_ttl_edge_in_load_fresh(webw, age, used):
    now = utcnow().replace(microsecond=0)
    with Session(engine) as s:
        s.add(MlWebResult(product_id="3", query="q", fetched_at=now - age, candidates="[]", n_candidates=0, status="empty"))
        s.commit()
    assert ("3" in oficina_ml.load_fresh(now=now)) is used


@pytest.mark.parametrize("ttl", [1, 3, 14])
def test_load_fresh_follows_the_configured_ttl(webw, monkeypatch, ttl):
    from app.config import Settings
    monkeypatch.setattr(oficina_ml, "get_settings", lambda: Settings(vendure_api_url="https://example.invalid/x", oficina_result_ttl_days=ttl))
    now = utcnow().replace(microsecond=0)
    with Session(engine) as s:
        s.add(MlWebResult(product_id="3", query="q", fetched_at=now - timedelta(days=ttl, hours=-1), candidates="[]", status="empty"))
        s.add(MlWebResult(product_id="4", query="q", fetched_at=now - timedelta(days=ttl, hours=1), candidates="[]", status="empty"))
        s.commit()
    assert set(oficina_ml.load_fresh(now=now)) == {"3"}


async def test_an_ok_result_with_no_identical_still_replaces_the_server_search(webw):
    """«En lugar de»: si la oficina miró y no hay idénticos, el servidor tampoco busca (vería la misma página)."""
    FakeVendure.products = [_product("3", "Producto raro")]
    webw.web.pages["producto-raro"] = _web_page(_card("MLA777", "Producto Raro", 400.0))
    _score(webw, "MLA777", 0.95)
    _store("3", [_wire("MLA903", "Heladera no frost", 5000.0)])
    _score(webw, "MLA903", 0.2)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert s.ml_status == "no_data" and s.web_via == "oficina" and webw.web.calls == []
    assert json.loads(s.other_listings)[0]["ml_id"] == "MLA903" and "ninguna publicación es igual" in s.ml_error


async def test_blocked_and_error_never_hide_an_older_fresh_ok_nor_trigger_anything(webw):
    FakeVendure.products = [_product("3", "Producto raro")]
    _store("3", [_wire("MLA901", "Producto Raro", 250.0)], ago=timedelta(days=2))
    _store("3", [], ago=timedelta(minutes=10), status="blocked")
    _store("3", [], ago=timedelta(minutes=5), status="error")
    _score(webw, "MLA901", 0.9)
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert s.ml_status == "ok" and s.match_origin == "oficina" and webw.web.calls == []


async def test_only_blocked_and_error_leave_the_server_search_exactly_as_before(webw):
    FakeVendure.products = [_product("3", "Producto raro")]
    webw.web.pages["producto-raro"] = _web_page(_card("MLA777", "Producto Raro", 400.0))
    _score(webw, "MLA777", 0.95)
    _store("3", [_wire("MLA901", "x", 1.0)], status="blocked")
    _store("3", [], ago=timedelta(hours=1), status="error")
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert s.match_origin == "web" and s.web_via is None and len(webw.web.calls) == 1 and _runs()[0].oficina_fresh == 0


async def test_a_product_that_failed_on_the_api_or_ran_out_of_budget_is_still_rescued_by_the_oficina(webw):
    FakeVendure.products = [_product("1", "Organizador cocina"), _product("2", "Lampara LED escritorio")]
    webw.ml.search["Organizador cocina"] = 429
    webw.ml.search["Lampara LED escritorio"] = 429
    for pid, name, ref in (("1", "Organizador cocina", "MLA801"), ("2", "Lampara LED escritorio", "MLA802")):
        _store(pid, [_wire(ref, name, 250.0)])
        _score(webw, ref, 0.9)
    await price_monitor.run_price_monitor()
    assert {pid: (s.ml_status, s.match_origin) for pid, s in _snaps().items()} == {"1": ("ok", "oficina"), "2": ("ok", "oficina")}


async def test_changing_the_candidate_cap_between_ingest_and_run_is_honoured_at_read_time(webw):
    FakeVendure.products = [_product("3", "Producto raro")]
    _store("3", [_wire(f"MLA9{k:02d}", "Producto Raro", 100.0 + k, seller=f"V{k}") for k in range(1, 9)])
    for k in range(1, 9):
        _score(webw, f"MLA9{k:02d}", 0.9)
    _set("pm_ml_web_max_results", 3)
    await price_monitor.run_price_monitor()
    assert _snaps()["3"].candidates_count == 3


async def test_a_fresh_result_for_a_disabled_product_does_nothing_when_disabled_are_off(webw):
    FakeVendure.products = [_product("3", "Producto raro", enabled=False)]
    _store("3", [_wire("MLA901", "Producto Raro", 250.0)])
    await price_monitor.run_price_monitor()
    assert "3" not in _snaps()


async def test_no_clip_score_means_a_note_not_a_crash(webw):
    FakeVendure.products = [_product("3", "Producto raro")]
    _store("3", [_wire("MLA901", "Producto Raro", 250.0)])                    # sin puntaje de foto: el doble devuelve None
    await price_monitor.run_price_monitor()
    s = _snaps()["3"]
    assert s.ml_status in ("no_data", "skipped") and _runs()[0].status in ("ok", "degraded")
