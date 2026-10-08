"""Mundo de la QA de cierre de feat/semaforo-tiendas (criterio 1: regresión de ML contra origin/main).

Este módulo SOLO usa lo que existe en origin/main (77645f9): se copia tal cual a un worktree de esa revisión para sacar el
dorado (`golden/semaforo_origin_main_77645f9_qa2.json`) y en esta rama se corre el mismo mundo. Si alguien lo
modifica hay que regenerar el dorado DESDE origin/main (ver test_qa2_tiendas_regresion.py), nunca desde esta rama.

El mundo es más ancho que el de la primera QA (test_qa_siempre_trae_algo._mixed_world): suma color rojo y verde por
precio, productos deshabilitados y sin foto, marca declarada por la web de ML (la que consulta el juez), lo marcado «No es
el mismo», el juez prendido con cupo de sobra, justo y corto, y volcado de TODAS las columnas del snapshot y de la corrida.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlmodel import Session

from app.db.models import MarketMatchFeedback, MarketPriceSnapshot, PriceMonitorRun
from app.db.session import engine
from app.pricing import daily_budget, market_judge
from app.pricing.semaforo import PricedVariant
from tests.ml_web_fixtures import ld_product
from tests.test_price_monitor import FakeVendure, _candidate, _listing, _product, _runs, _set, _snaps
from tests.test_semaforo_web import _card, _score, _web_page

ML_IMG = "https://http2.mlstatic.com/D_NQ_{}.jpg"
SNAP_SKIP = {"id", "run_id", "captured_at"}
RUN_SKIP = {"id", "started_at", "finished_at"}


def build_world(w, *, feedback: bool = False) -> None:
    """Un producto de cada situación que le importa a ML (API + web, sin tiendas de por medio)."""
    no_photo = _product("14", "Accesorio sin foto")
    no_photo.image_urls, no_photo.featured_image_url = [], None
    alfombra = _product("11", "Alfombra antideslizante")
    alfombra.priced_variants = [PricedVariant(id="v11", name="", sku="", price_with_tax_cents=10_000, currency="ARS",
                                              specs={"length": 40.0, "width": 30.0})]
    FakeVendure.products = [
        _product("1", "Organizador cocina"),                       # API: idéntico (+ ambiguo + impostor)
        _product("2", "Producto raro"),                            # web: idéntico + pack + impostor
        _product("3", "Lampara escritorio plegable"),                # web: solo packs
        _product("4", "Cable usb tipo c reforzado"),               # web: solo impostores
        _product("5", "Mate de calabaza"),                         # web: nada
        _product("6", "Botella termica acero"),                    # web: idéntico con pocas ventas + pack
        _product("7", "Cargador inalambrico rapido"),              # API 429
        _product("8", "Reloj digital"),                            # web: idéntico en USD
        _product("9", "Funda silicona celular"),                   # web: solo banda ambigua
        _product("10", "Soporte celular auto magnetico"),          # web: tres idénticos
        alfombra,                                                  # web: solo tamaños distintos (estimado)
        _product("12", "Termo acero inoxidable"),                  # web: idénticos, uno con marca conocida declarada
        _product("13", "Taza grande ceramica", price=20_000),      # API: idénticos MÁS BARATOS que nosotros (rojo)
        no_photo,                                                  # sin foto
        _product("15", "Deshabilitado viejo", enabled=False),     # deshabilitado
        _product("16", "Hervidor electrico", price=5_000),         # web: idénticos muy caros (verde)
    ]
    ml = w.ml
    ml.search.update({
        "Organizador cocina": [_candidate("MLA1", "Organizador de cocina"),
                               _candidate("MLA2", "Organizador de pared multiuso"),
                               _candidate("MLA3", "Zapatilla running")],
        "Cargador inalambrico rapido": 429,
        "Taza grande ceramica": [_candidate("MLA13", "Taza grande de ceramica")],
    })
    ml.items["MLA13"] = [_listing("I13", "113", 120.0), _listing("I14", "114", 130.0)]
    ml.users.update({"113": 900, "114": 900})
    w.image_scores[ML_IMG.format("MLA2")] = 0.50
    w.image_scores[ML_IMG.format("MLA3")] = 0.10
    w.image_scores[ML_IMG.format("MLA13")] = 0.9
    pages = w.web.pages
    pages["producto-raro"] = _web_page(
        _card("MLA901", "Producto Raro", 250.0), _card("MLA902", "Pack X6 Producto Raro", 900.0, seller="Dos"),
        _card("MLA903", "Heladera no frost", 5000.0, seller="Tres"))
    pages["lampara-escritorio-plegable"] = _web_page(
        _card("MLA911", "Pack X3 Lampara escritorio plegable", 300.0), _card("MLA912", "Set X2 Lampara escritorio plegable", 220.0, seller="Dos"))
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
    pages["alfombra-antideslizante"] = _web_page(
        _card("MLA971", "Alfombra antideslizante 60x80 cm", 250.0), _card("MLA972", "Alfombra antideslizante 70x90 cm", 300.0, seller="Dos"))
    ld = [ld_product("Termo acero inoxidable Stanley", "https://www.mercadolibre.com.ar/x/p/MLA29003349", 400, brand="Stanley"),
          ld_product("Termo acero inoxidable generico", "https://articulo.mercadolibre.com.ar/MLA-982982-x", 180, brand="Generica")]
    pages["termo-acero-inoxidable"] = _web_page(
        _card("MLA981", "Termo acero inoxidable Stanley", 400.0, catalog="MLA29003349", url="www.mercadolibre.com.ar/x/p/MLA29003349"),
        _card("MLA982982", "Termo acero inoxidable generico", 180.0, seller="Dos", url="articulo.mercadolibre.com.ar/MLA-982982-x"),
        ld=ld)
    pages["hervidor-electrico"] = _web_page(_card("MLA991", "Hervidor electrico", 900.0), _card("MLA992", "Hervidor electrico", 950.0, seller="Dos"))
    for ref, v in {"MLA901": 0.92, "MLA902": 0.95, "MLA903": 0.20, "MLA911": 0.93, "MLA912": 0.91,
                   "MLA921": 0.15, "MLA922": 0.12, "MLA931": 0.90, "MLA932": 0.94, "MLA941": 0.9,
                   "MLA951": 0.50, "MLA952": 0.55, "MLA961": 0.9, "MLA962": 0.88, "MLA963": 0.87,
                   "MLA971": 0.95, "MLA972": 0.93, "MLA981": 0.92, "MLA982982": 0.91, "MLA991": 0.9, "MLA992": 0.9}.items():
        _score(w, ref, v)
    if feedback:
        with Session(engine) as s:
            s.add(MarketMatchFeedback(product_id="10", ml_id="MLA963", label=0))      # No es el mismo
            s.add(MarketMatchFeedback(product_id="9", ml_id="MLA951", label=1))       # Es el mismo (promueve el ambiguo)
            s.commit()


def install_judge(monkeypatch, calls: list[tuple[str, str]]) -> None:
    """Un juez determinista de tres valores (por el último dígito del id de ML). Cuenta contra el contador que le pasen."""
    async def judge(our_name, our_images, candidates, *, max_calls, client=None, on_reserve=None,
                    counter_key=market_judge.LLM_COUNTER_KEY, **kw):
        if await daily_budget.reserve_async(counter_key, int(max_calls), None, on_reserve) is None:
            return None
        calls.append((our_name, counter_key))
        verdicts = {}
        for c in candidates:
            if (c.brand or "").lower() == "stanley":
                verdicts[c.ml_id] = market_judge.JudgeVerdict(c.ml_id, False, 0.9, "marca conocida", "similar", ("marca",))
                continue
            last = int(c.ml_id[-1]) if c.ml_id[-1].isdigit() else 0
            cat = (market_judge.CAT_IGUAL, market_judge.CAT_SIMILAR, market_judge.CAT_DIFERENTE)[last % 3]
            verdicts[c.ml_id] = market_judge.JudgeVerdict(c.ml_id, cat == market_judge.CAT_IGUAL, 0.9, f"juez {cat}", cat, ())
        return market_judge.JudgeResult(verdicts=verdicts)

    monkeypatch.setattr(market_judge, "enabled", lambda: True)
    monkeypatch.setattr(market_judge, "make_client", lambda: None)
    monkeypatch.setattr(market_judge, "judge", judge)


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


def dump_all(w, calls: list[tuple[str, str]] | None = None) -> dict:
    snaps = {}
    for pid, s in sorted(_snaps().items(), key=lambda kv: int(kv[0])):
        snaps[pid] = {c: _plain(getattr(s, c)) for c in _cols(MarketPriceSnapshot) if c not in SNAP_SKIP}
    run = _runs()[-1]
    out: dict[str, Any] = {
        "snaps": snaps, "run": {c: _plain(getattr(run, c)) for c in _cols(PriceMonitorRun) if c not in RUN_SKIP},
        "ml_calls": sorted(w.ml.calls), "web_calls": sorted(w.web.calls),
        "judge_used_ml": daily_budget.used_today(market_judge.LLM_COUNTER_KEY)}
    if calls is not None:
        out["judge_calls_ml"] = sorted(n for n, k in calls if k == market_judge.LLM_COUNTER_KEY)
    return json.loads(json.dumps(out, sort_keys=True, default=str))


VARIANTS: dict[str, dict] = {
    "base_spec1": {"pm_spec_check": 1},
    "base_spec0": {"pm_spec_check": 0},
    "include_disabled": {"pm_spec_check": 1, "pm_include_disabled": 1},
    "con_feedback": {"pm_spec_check": 1, "feedback": True},
    "juez_cupo_de_sobra": {"pm_spec_check": 1, "pm_vision_max_calls": 100, "judge": True, "pm_ml_concurrency": 1},
    "juez_cupo_justo": {"pm_spec_check": 1, "pm_vision_max_calls": "EXACT", "judge": True, "pm_ml_concurrency": 1},
    "juez_cupo_corto": {"pm_spec_check": 1, "pm_vision_max_calls": "SHORT", "judge": True, "pm_ml_concurrency": 1},
    "juez_sin_specs": {"pm_spec_check": 0, "pm_vision_max_calls": 100, "judge": True, "pm_ml_concurrency": 1},
    "presupuesto_ml_corto": {"pm_spec_check": 1, "pm_ml_daily_budget": 9, "pm_ml_concurrency": 1},
    "umbrales_otros": {"pm_spec_check": 1, "pm_green_min_pct": 80.0, "pm_yellow_min_pct": 40.0, "pm_ml_commission_pct": 20.0},
}


def apply_variant(name: str, *, judge_calls_needed: int | None = None) -> dict:
    """Aplica los settings de la variante y devuelve sus opciones (`judge`, `feedback`)."""
    opts = dict(VARIANTS[name])
    for key in [k for k in opts if k.startswith("pm_")]:
        value = opts[key]
        if value == "EXACT":
            value = judge_calls_needed
        elif value == "SHORT":
            value = max(1, (judge_calls_needed or 2) - 2)
        _set(key, value)
    return opts
