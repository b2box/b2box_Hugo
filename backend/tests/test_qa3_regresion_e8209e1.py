"""QA3, criterio 1: con `pm_ml_query_variants` = 1 y SIN resultados de la oficina, el semáforo da, columna a columna,
lo mismo que origin/main (e8209e1).

El DORADO sale de un worktree de e8209e1 (NO de esta rama): este mismo archivo, copiado a ese worktree, corre un mundo
AZAROSO (semilla fija, 40 productos por mundo: API con idéntico / ambiguo / impostor / sin vendedores / 429 / 503 en el
respaldo, respaldo de las 4 primeras palabras, web con idéntico / packs / impostores / USD / bloqueo, sin precio, sin
foto, deshabilitado, «No es el mismo», con las tiendas prendidas e índice vacío) en cinco configuraciones y vuelca TODAS las
columnas del snapshot y de la corrida y la API del dashboard. Regenerarlo (solo desde origin/main):

    git worktree add --detach ../hugo-qa-base e8209e1
    cp backend/tests/test_qa3_regresion_e8209e1.py ../hugo-qa-base/backend/tests/
    cd ../hugo-qa-base && QA3_OUT=/ruta/golden.json pytest backend/tests/test_qa3_regresion_e8209e1.py
    gzip -9 -c /ruta/golden.json > backend/tests/golden/semaforo_origin_main_e8209e1_qa3.json.gz

En esta rama el mismo archivo compara contra el dorado. Lo que la rama agrega (`ml_variant`, `variant_stats`, `web_via`,
`oficina_fresh`, `n_oficina_ok`, y las claves nuevas de la API) se chequea aparte: con 1 variante y sin oficina,
`ml_variant` solo puede ser titulo | inicio | None, y los contadores nuevos son 0/None.
"""

from __future__ import annotations

import gzip
import json
import os
import random
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app import runtime  # noqa: E402
from app.db.models import MarketMatchFeedback, MarketPriceSnapshot, PriceMonitorRun  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import market_match, market_ml_web, price_monitor, store_catalog  # noqa: E402
from tests.ml_web_fixtures import ANTIBOT_HTML, page  # noqa: E402
from tests.qa3_titles import REALISTAS  # noqa: E402
from tests.store_fixtures import store_db  # noqa: E402,F401  (fixture)
from tests.test_price_monitor import (  # noqa: E402,F401
    ML_IMG, FakeVendure, _candidate, _listing, _product, _runs, _set, _snaps, world)
from tests.test_price_monitor_routes import _env, client  # noqa: E402,F401
from tests.test_semaforo_web import PHOTO, _card, _score, _web_page, webw  # noqa: E402,F401
from tests.qa3_cleanup import qa3_clean  # noqa: E402,F401  (fixture autouse)

OUT = os.environ.get("QA3_OUT")
GOLDEN = Path(__file__).parent / "golden" / "semaforo_origin_main_e8209e1_qa3.json.gz"
SNAP_SKIP = {"id", "run_id", "captured_at"}
RUN_SKIP = {"id", "started_at", "finished_at"}
ADDED_BY_THIS_BRANCH = {"ml_variant", "variant_stats", "web_via", "oficina_fresh", "n_oficina_ok"}
NEW_ITEM_KEYS = {"ml_variant", "web_via"}
NEW_RUN_KEYS = {"variants", "oficina"}
NEW_SUMMARY_KEYS = {"oficina"}

CONFIGS: dict[str, dict] = {
    "defecto": {},
    "sin_specs": {"pm_spec_check": 0},
    "con_deshabilitados": {"pm_include_disabled": 1},
    "umbrales": {"pm_green_min_pct": 80.0, "pm_yellow_min_pct": 40.0, "pm_ml_commission_pct": 20.0},
    "cupo_corto": {"pm_ml_daily_budget": 37},
    # las tiendas (Gadnic, Casa Perfecta) con el índice LLENO de productos con el mismo nombre que los nuestros y el color afectado
    "tiendas_llenas": {},
}
SEEDS = (11, 2026)
API_PATHS = ["/api/price-monitor/snapshots?page_size=200", "/api/price-monitor/snapshots?page_size=200&color=verde",
             "/api/price-monitor/snapshots?page_size=200&color=sin_dato", "/api/price-monitor/snapshots?page_size=200&match=igual",
             "/api/price-monitor/snapshots?page_size=200&match=solo_similar", "/api/price-monitor/snapshots?page_size=200&origin=web",
             "/api/price-monitor/snapshots?page_size=200&origin=api", "/api/price-monitor/snapshots?page_size=200&status=failed",
             "/api/price-monitor/snapshots?page_size=7&page=2", "/api/price-monitor/runs", "/api/price-monitor/summary",
             "/api/price-monitor/products/1/history", "/api/price-monitor/products/2/history"]


def _slug(query: str) -> str:
    return market_ml_web.slugify(query)


KINDS = ["api_ok", "api_ok", "api_ok", "api_fallback", "api_ambiguo", "api_sin_vendedores", "api_429", "api_503_respaldo",
         "web_ok", "web_ok", "web_respaldo", "web_packs", "web_impostores", "web_usd", "web_bloqueo", "nada", "nada",
         "sin_precio", "sin_foto", "deshabilitado", "excluido", "api_ok_barato"]


def build_world(w, seed: int, n: int = 40) -> None:
    rng = random.Random(seed)
    titles = rng.sample(REALISTAS, n)
    products = []
    for i, title in enumerate(titles, 1):
        kind = rng.choice(KINDS)
        pid = str(i)
        price = rng.choice([8_000, 10_000, 15_000, 20_000, 50_000])
        prod = _product(pid, title, price=None if kind == "sin_precio" else price, enabled=kind != "deshabilitado")
        if kind == "sin_foto":
            prod.image_urls, prod.featured_image_url = [], None
        products.append(prod)
        q = market_match.search_query(title)
        fb = market_match.fallback_query(title)
        a, b = f"MLA{i}01", f"MLA{i}02"
        cand = _candidate(a, title)
        imp = _candidate(b, "Zapatillas running hombre")
        sellers = [_listing(f"I{i}1", f"{i}01", rng.choice([150.0, 200.0, 260.0, 90.0])),
                   _listing(f"I{i}2", f"{i}02", rng.choice([180.0, 210.0, 400.0]))]
        w.ml.users.update({f"{i}01": rng.choice([5, 80, 1000]), f"{i}02": rng.choice([3, 900])})
        if kind in ("api_ok", "api_ok_barato", "excluido"):
            w.ml.search[q] = [cand, imp]
            w.ml.items[a] = sellers if kind != "api_ok_barato" else [_listing(f"I{i}1", f"{i}01", 40.0)]
            w.image_scores[ML_IMG.format(a)] = 0.9
            w.image_scores[ML_IMG.format(b)] = 0.1
        elif kind == "api_fallback" and fb:
            w.ml.search[q] = []
            w.ml.search[fb] = [cand]
            w.ml.items[a] = sellers
            w.image_scores[ML_IMG.format(a)] = 0.88
        elif kind == "api_ambiguo":
            w.ml.search[q] = [cand]
            w.ml.items[a] = sellers
            w.image_scores[ML_IMG.format(a)] = 0.5
        elif kind == "api_sin_vendedores":
            w.ml.search[q] = [cand]
            w.ml.items[a] = []
            w.image_scores[ML_IMG.format(a)] = 0.9
        elif kind == "api_429":
            w.ml.search[q] = 429
        elif kind == "api_503_respaldo" and fb:
            w.ml.search[q] = []
            w.ml.search[fb] = 503
        elif kind in ("web_ok", "web_respaldo", "web_packs", "web_impostores", "web_usd", "web_bloqueo"):
            ra, rb = f"MLA9{i}01", f"MLA9{i}02"
            target = _slug(fb) if kind == "web_respaldo" and fb else _slug(q)
            if kind == "web_ok":
                w.web.pages[target] = _web_page(_card(ra, title, rng.choice([120.0, 250.0, 600.0])),
                                                _card(rb, "Heladera no frost", 5000.0, seller="Dos"))
                _score(w, ra, 0.92)
                _score(w, rb, 0.2)
            elif kind == "web_respaldo":
                w.web.pages[target] = _web_page(_card(ra, title, 300.0))
                _score(w, ra, 0.9)
            elif kind == "web_packs":
                w.web.pages[target] = _web_page(_card(ra, f"Pack X3 {title}", 700.0), _card(rb, f"Set X2 {title}", 500.0, seller="Dos"))
                _score(w, ra, 0.93)
                _score(w, rb, 0.91)
            elif kind == "web_impostores":
                w.web.pages[target] = _web_page(_card(ra, "Zapatilla running", 990.0), _card(rb, "Heladera", 1500.0, seller="Dos"))
                _score(w, ra, 0.1)
                _score(w, rb, 0.1)
            elif kind == "web_usd":
                w.web.pages[target] = _web_page(_card(ra, title, 80.0, currency="USD"))
                _score(w, ra, 0.9)
            else:
                w.web.pages[target] = page(ANTIBOT_HTML, status=200)
        if kind == "excluido":
            with Session(engine) as s:
                s.add(MarketMatchFeedback(product_id=pid, ml_id=a, label=0))
                s.commit()
    FakeVendure.products = products


def _cols(model) -> list[str]:
    return [c.name for c in model.__table__.columns]


def _plain(value):
    import datetime as dt
    if isinstance(value, dt.datetime):
        return None
    if isinstance(value, str) and value[:1] in "[{":
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _norm(value, keep_ids: bool = False):
    if isinstance(value, dict):
        return {k: _norm(v, keep_ids or k == "product") for k, v in value.items()
                if (keep_ids or k not in ("id", "run_id", "snapshot_id")) and not (k.endswith("_at") or k in ("started", "finished", "next_run"))}
    if isinstance(value, list):
        return [_norm(v, keep_ids) for v in value]
    return value


def dump(w, client) -> dict:  # noqa: F811
    snaps = {}
    for pid, s in sorted(_snaps().items(), key=lambda kv: int(kv[0])):
        snaps[pid] = {c: _plain(getattr(s, c)) for c in _cols(MarketPriceSnapshot) if c not in SNAP_SKIP}
    run = _runs()[-1]
    api = {}
    for path in API_PATHS:
        r = client.get(path)
        api[path] = {"status": r.status_code, "body": _norm(r.json())}
    out = {"snaps": snaps, "run": {c: _plain(getattr(run, c)) for c in _cols(PriceMonitorRun) if c not in RUN_SKIP},
           "ml_calls": sorted(w.ml.calls), "web_calls": sorted(w.web.calls), "api": api}
    return json.loads(json.dumps(out, sort_keys=True, default=str))


@pytest.fixture
def qworld(webw, store_db, monkeypatch):  # noqa: F811
    store_catalog.seed_default_stores()           # las tiendas prendidas, con el índice vacío (como en prod al arrancar)

    async def clip(our, urls):                    # solo la ficha de la tienda con el id de ESTE producto se parece a su foto
        if not urls:
            return None
        return 0.95 if urls[0].rsplit("-", 1)[-1].split(".", 1)[0] == our.id else 0.20

    monkeypatch.setattr(market_match, "clip_score_urls", clip)
    runtime.set_value("pm_stores_affect_color", 1)
    runtime.set_value("pm_ml_concurrency", 1)
    runtime.set_value("pm_stores_topup_minutes", 0)
    yield webw
    runtime.invalidate()


def _apply(config: str) -> None:
    for key, value in CONFIGS[config].items():
        _set(key, value)
    try:
        _set("pm_ml_query_variants", 1)          # la rama: explícito. En origin/main el setting no existe.
    except (KeyError, ValueError):
        pass


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("config", list(CONFIGS))
async def test_origin_main_dump_or_compare(qworld, client, seed, config):  # noqa: F811
    w = qworld
    _apply(config)
    build_world(w, seed)
    if config == "tiendas_llenas":
        from tests.test_qa2_tiendas_regresion import fill_index
        fill_index()
    await price_monitor.run_price_monitor()
    got = dump(w, client)
    key = f"{config}/{seed}"
    if OUT:                                                      # modo dorado: solo en el worktree de origin/main
        path = Path(OUT)
        data = json.loads(path.read_text()) if path.exists() else {}
        data[key] = got
        path.write_text(json.dumps(data, sort_keys=True))
        return
    want = json.loads(gzip.decompress(GOLDEN.read_bytes()))[key]
    assert got["ml_calls"] == want["ml_calls"], f"{key}: requests a la API de ML"
    assert got["web_calls"] == want["web_calls"], f"{key}: búsquedas web"
    assert got["snaps"].keys() == want["snaps"].keys()
    diffs = {(pid, col): (v, got["snaps"][pid][col])
             for pid, row in want["snaps"].items() for col, v in row.items() if got["snaps"][pid][col] != v}
    assert diffs == {}, f"{key}: el snapshot cambió respecto de origin/main"
    extra = {k for row in got["snaps"].values() for k in row} - {k for row in want["snaps"].values() for k in row}
    assert extra == ADDED_BY_THIS_BRANCH & extra and extra <= ADDED_BY_THIS_BRANCH
    assert {r["ml_variant"] for r in got["snaps"].values()} <= {None, "titulo", "inicio"}
    assert {r["web_via"] for r in got["snaps"].values()} == {None}
    run_diffs = {c: (v, got["run"][c]) for c, v in want["run"].items() if got["run"][c] != v}
    assert run_diffs == {}, f"{key}: los contadores de la corrida cambiaron"
    assert got["run"]["oficina_fresh"] == 0 and got["run"]["n_oficina_ok"] == 0
    for path, old in want["api"].items():
        new = got["api"][path]
        assert new["status"] == old["status"] == 200, path
        _old_keys_equal(old["body"], new["body"], path)


def _old_keys_equal(old, new, path: str) -> None:
    """Toda clave que servía origin/main sigue ahí con el mismo valor; las únicas nuevas son las previstas."""
    if isinstance(old, dict):
        assert isinstance(new, dict), path
        missing = set(old) - set(new)
        assert not missing, f"{path}: la API dejó de servir {sorted(missing)}"
        extra = set(new) - set(old)
        assert extra <= NEW_ITEM_KEYS | NEW_RUN_KEYS | NEW_SUMMARY_KEYS, f"{path}: claves nuevas no previstas {sorted(extra)}"
        for k, v in old.items():
            _old_keys_equal(v, new[k], f"{path}.{k}")
    elif isinstance(old, list):
        assert isinstance(new, list) and len(new) == len(old), f"{path}: largo {len(old)} -> {len(new)}"
        for i, (a, b) in enumerate(zip(old, new)):
            _old_keys_equal(a, b, f"{path}[{i}]")
    else:
        assert new == old, f"{path}: {old!r} -> {new!r}"
