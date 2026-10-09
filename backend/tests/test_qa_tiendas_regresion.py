"""QA independiente de feat/semaforo-tiendas, criterio 1: con las tiendas apagadas (o sin índice, o prendidas
pero sin contar para el color) el semáforo da EXACTAMENTE lo mismo que feat/semaforo-ml-web (b052e4f).

El DORADO se sacó corriendo este mismo mundo (el mezclado de test_qa_siempre_trae_algo: API + web, idénticos,
packs, ambiguos, impostores, USD, 429, pocas ventas) sobre b052e4f. A diferencia del dorado de 9c6dc45, acá se
vuelcan TODAS las columnas del snapshot y de la corrida (incluidos otros listados, estado, color estimado) menos
las que son de la corrida (id, fechas) y las que agrega esta rama (price_basis, source_stats).

Regenerar el dorado (solo desde b052e4f, nunca desde esta rama):
    QA_WRITE_GOLDEN_B052E4F=1 pytest backend/tests/test_qa_tiendas_regresion.py -k write_golden

Después, lo que NO tiene que cambiar con las tiendas en cada situación; lo que SÍ cambia se ve en
test_qa_tiendas_color.py.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.db.models import MarketPriceSnapshot, PriceMonitorRun  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import price_monitor  # noqa: E402
from tests.test_price_monitor import FakeVendure, _product, _runs, _set, _snaps, world  # noqa: E402,F401
from tests.test_qa_siempre_trae_algo import _mixed_world as _mixed  # noqa: E402
from tests.test_semaforo_web import _card, _score, _web_page, webw  # noqa: E402,F401

GOLDEN = Path(__file__).parent / "golden" / "semaforo_b052e4f_full.json"
SNAP_SKIP = {"id", "run_id", "captured_at"}
RUN_SKIP = {"id", "started_at", "finished_at"}
ADDED_BY_THIS_BRANCH = {"price_basis", "source_stats", "ml_variant", "variant_stats", "web_via", "oficina_fresh",
                        "n_oficina_ok"}


def _mixed_world(w) -> None:
    """El mundo mezclado de «Siempre trae algo» + un producto con medidas cuyos únicos resultados son tamaños distintos
    (SIMILAR confirmado por medidas): ejercita el color ESTIMADO, que el mundo original no tiene."""
    from app.pricing.semaforo import PricedVariant

    _mixed(w)
    prod = _product("11", "Alfombra antideslizante")
    prod.priced_variants = [PricedVariant(id="v11", name="", sku="", price_with_tax_cents=10_000, currency="ARS",
                                          specs={"length": 40.0, "width": 30.0})]
    FakeVendure.products.append(prod)
    w.web.pages["alfombra-antideslizante"] = _web_page(
        _card("MLA971", "Alfombra antideslizante 60x80 cm", 250.0),
        _card("MLA972", "Alfombra antideslizante 70x90 cm", 300.0, seller="Dos"))
    _score(w, "MLA971", 0.95)
    _score(w, "MLA972", 0.93)


def _cols(model) -> list[str]:
    return [c.name for c in model.__table__.columns]


def _plain(value):
    if isinstance(value, dt.datetime):
        return None
    if isinstance(value, str) and value[:1] in "[{":
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def dump_all(w) -> dict:
    snaps = {}
    for pid, s in sorted(_snaps().items(), key=lambda kv: int(kv[0])):
        snaps[pid] = {c: _plain(getattr(s, c)) for c in _cols(MarketPriceSnapshot) if c not in SNAP_SKIP}
    run = _runs()[-1]
    return {"snaps": snaps, "run": {c: _plain(getattr(run, c)) for c in _cols(PriceMonitorRun) if c not in RUN_SKIP},
            "ml_calls": sorted(w.ml.calls), "web_calls": sorted(w.web.calls)}


def _roundtrip(d: dict) -> dict:
    return json.loads(json.dumps(d, sort_keys=True, default=str))


def _is_this_branch() -> bool:
    try:
        from app.db import models
        return hasattr(models, "MarketStore")
    except Exception:  # noqa: BLE001
        return False


# ─── solo en b052e4f: escribe el dorado ─────────────────────────────────────


@pytest.mark.skipif(not os.environ.get("QA_WRITE_GOLDEN_B052E4F"), reason="solo para regenerar el dorado desde b052e4f")
@pytest.mark.parametrize("spec_check", [1, 0])
async def test_write_golden_from_b052e4f(webw, spec_check):
    assert not _is_this_branch(), "el dorado se saca de b052e4f, no de la rama con tiendas"
    _set("pm_spec_check", spec_check)
    _mixed_world(webw)
    await price_monitor.run_price_monitor()
    data = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
    data[f"spec_check_{spec_check}"] = _roundtrip(dump_all(webw))
    GOLDEN.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


# ─── en esta rama: las tres situaciones donde todo tiene que ser igual ──────

if _is_this_branch():
    from sqlalchemy import update

    from app import runtime
    from app.clock import utcnow
    from app.db.models import MarketStore, StoreCatalogItem, StoreMatch
    from app.pricing import market_match, store_catalog
    from tests.store_fixtures import store_db  # noqa: F401  (fixture)

    def _fill_index(titles: list[str]) -> None:
        """Productos de las dos tiendas con el mismo nombre que los nuestros (los iguales más tentadores)."""
        with Session(engine) as s:
            ids = {r.name: int(r.id) for r in s.exec(select(MarketStore)).all()}
            for i, t in enumerate(titles):
                for store, base in (("Casa Perfecta", "https://www.casaperfecta.com.ar/productos"),
                                    ("Gadnic", "https://www.gadnic.com.ar")):
                    s.add(StoreCatalogItem(
                        store_id=ids[store], url=f"{base}/item-{i}/", title=t, sku=f"S{i}", price_cents=15_000 + i * 100,
                        image_url=("https://acdn-us.mitiendanube.com/stores/001/products/%d.webp" % i) if store == "Casa Perfecta"
                        else "https://static.bidcom.com.ar/publicacionesML/productos/%d.jpg" % i,
                        brand=None, stock=4, last_seen_at=utcnow(), last_checked_at=utcnow()))
            s.commit()

    TITLES = ["Organizador cocina", "Producto raro", "Lampara LED escritorio", "Cable usb tipo c reforzado", "Mate de calabaza",
              "Botella termica acero", "Cargador inalambrico rapido", "Reloj digital", "Funda silicona celular",
              "Soporte celular auto magnetico", "Pack x3 Mate de calabaza", "Heladera no frost", "Alfombra antideslizante"]

    @pytest.fixture
    def stores_ready(webw, store_db, monkeypatch):  # noqa: F811
        store_catalog.seed_default_stores()
        runtime.set_value("pm_stores_topup_minutes", 0)      # sin red
        runtime.set_value("pm_stores_affect_color", 0)

        async def clip(our, urls):                            # toda foto de tienda se parece (peor caso para el color)
            return 0.95 if urls else None

        monkeypatch.setattr(market_match, "clip_score_urls", clip)
        yield webw
        runtime.invalidate()

    def _same_as_b052e4f(got: dict, spec_check: int) -> None:
        want = json.loads(GOLDEN.read_text())[f"spec_check_{spec_check}"]
        assert got["ml_calls"] == want["ml_calls"], "requests a la API de ML cambiaron"
        assert got["web_calls"] == want["web_calls"], "búsquedas web cambiaron"
        assert got["snaps"].keys() == want["snaps"].keys()
        extra = {k for row in got["snaps"].values() for k in row} - {k for row in want["snaps"].values() for k in row}
        assert extra <= ADDED_BY_THIS_BRANCH, f"columnas nuevas inesperadas: {extra}"
        diffs = {(pid, col): (v, got["snaps"][pid][col])
                 for pid, row in want["snaps"].items() for col, v in row.items() if got["snaps"][pid][col] != v}
        assert diffs == {}, "el snapshot de ML cambió respecto de b052e4f"
        assert all(row["price_basis"] == "ml" for row in got["snaps"].values())
        run_diffs = {c: (v, got["run"][c]) for c, v in want["run"].items() if got["run"][c] != v}
        assert run_diffs == {}, "los contadores de la corrida cambiaron respecto de b052e4f"
        assert set(got["run"]) - set(want["run"]) <= ADDED_BY_THIS_BRANCH

    @pytest.mark.parametrize("spec_check", [1, 0])
    @pytest.mark.parametrize("situation", ["tiendas_apagadas", "prendidas_sin_indice", "prendidas_con_indice_sin_contar",
                                           "indice_con_precios_dudosos"])
    async def test_ml_is_exactly_as_in_b052e4f(stores_ready, situation, spec_check):
        _set("pm_spec_check", spec_check)
        if situation == "tiendas_apagadas":
            with Session(engine) as s:
                s.execute(update(MarketStore).values(enabled=False))
                s.commit()
        elif situation in ("prendidas_con_indice_sin_contar", "indice_con_precios_dudosos"):
            _fill_index(TITLES)
            if situation == "indice_con_precios_dudosos":
                with Session(engine) as s:
                    s.execute(update(StoreCatalogItem).values(price_cents=100, price_doubtful=True))
                    s.commit()
        _mixed_world(stores_ready)
        await price_monitor.run_price_monitor()
        _same_as_b052e4f(_roundtrip(dump_all(stores_ready)), spec_check)
        with Session(engine) as s:
            n = len(s.exec(select(StoreMatch)).all())
        if situation == "prendidas_con_indice_sin_contar":
            assert n > 0, "el mundo no ejercitó las tiendas: el test no probaría nada"
        elif situation != "indice_con_precios_dudosos":
            assert n == 0
