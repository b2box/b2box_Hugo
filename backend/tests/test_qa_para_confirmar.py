"""QA de «Para confirmar» (match=para_confirmar y el contador `para_confirmar` de GET /api/price-monitor/snapshots).

La regla: sin dato medible (unmeasurable NULL), sin idéntico, con similares, y (nombre ≥ 0,8 y foto ≥ 0,5) o un
similar que el juez no miró (source "ambiguo"). Las listas se guardan con el `_store_listings` de la corrida (el JSON
real, con ensure_ascii=False), no a mano. `seed` y `check_*` los usa también test_qa_para_confirmar_pg.py.
"""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

from datetime import timedelta  # noqa: E402

from sqlmodel import Session  # noqa: E402

from app.clock import utcnow  # noqa: E402
from app.db.models import MarketPriceSnapshot, PriceMonitorRun  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import market_match, price_monitor  # noqa: E402
from tests.test_price_monitor_api_web import _clean_feedback  # noqa: E402,F401
from tests.test_price_monitor_routes import _env, client  # noqa: E402,F401

IN = {"A", "B", "C", "D", "M"}


def _pub(ml_id, category, price=None, *, source="llm", **kw):
    e = {"ml_id": ml_id, "title": f"Publicación {ml_id}", "permalink": f"https://www.mercadolibre.com.ar/p/{ml_id}",
         "origin": "web", "category": category, "source": source, "image_score": 0.6, "name_score": 0.6,
         "confidence": None, "reason": "La marca es diferente.", "differences": [], "notes": [], "brand": None,
         "image_url": None, "seller": "T", "sold_quantity": 10, "price_cents": price, "est_ok": False}
    e.update(kw)
    return e


def _sim(ml_id, **kw):
    return _pub(ml_id, "similar", 20_000, **kw)


def _amb(ml_id):
    return _sim(ml_id, source=market_match.SOURCE_UNCONFIRMED, reason="parecido en foto y nombre, sin confirmar")


def seed(eng) -> tuple[int, int]:
    """Una corrida vieja (con uno que entraría) y la última con la matriz de casos. Devuelve (última, vieja)."""
    with Session(eng) as s:
        old = PriceMonitorRun(status="ok", started_at=utcnow() - timedelta(days=1), total_products=1)
        run = PriceMonitorRun(status="ok", total_products=16)
        s.add(old)
        s.add(run)
        s.commit()
        s.refresh(old)
        s.refresh(run)

        def product(pid, run_id=run.id, *, name=None, image=None, igual=(), unpriced=(), similar=(), other=(),
                    ok=False, **kw):
            snap = MarketPriceSnapshot(run_id=run_id, product_id=pid, product_name=f"P{pid}", our_price_cents=10_000,
                                       commission_pct=13.0, shipping_cents=0, ml_status="ok" if ok else "no_data",
                                       color="verde" if ok else "sin_dato", name_score_max=name, image_score_max=image,
                                       **kw)
            if ok:
                snap.ml_median_cents, snap.ml_min_cents, snap.ml_listing_count, snap.ml_seller_count = 25_000, 25_000, 1, 1
                snap.est_margin_pct = 117.5
            price_monitor._store_listings(snap, list(igual), list(unpriced), list(similar), list(other),
                                          keep=8, green_min=30.0, yellow_min=10.0)
            s.add(snap)

        # entran
        product("A", name=0.85, image=0.55, similar=[_sim("MLA1100")], match_origin="web")
        product("B", name=0.8, image=0.5, similar=[_sim("MLA1200")], review_flag="firme")   # justo en el umbral
        product("C", name=0.3, image=0.3, similar=[_amb("MLA1300")])                        # el juez no lo miró
        product("D", similar=[_sim("MLA1400"), _amb("MLA1410")])                              # sin scores, uno sin mirar
        product("M", name=0.9, image=0.9, similar=[_sim("MLA1500")], product_enabled=False)
        # no entran
        product("E", name=0.79, image=0.9, similar=[_sim("MLA1600")])
        product("F", name=0.95, image=0.49, similar=[_sim("MLA1700")])
        product("G", name=0.9, similar=[_sim("MLA1800")])                                   # sin score de foto
        product("H", name=0.9, image=0.9, ok=True, igual=[_pub("MLA1900", "igual", 25_000, source="clip", listings=1,
                                                                min_cents=25_000, median_cents=25_000,
                                                                prices_cents=[25_000], sellers=["T"])],
                similar=[_amb("MLA1910")])
        product("I", name=0.9, image=0.9, unpriced=[_pub("MLA1920", "igual", None, source="clip", listings=0)],
                similar=[_amb("MLA1930")])                                                  # idéntico sin precio
        product("J", name=0.9, image=0.9, similar=[_amb("MLA1940")], unmeasurable="sin_foto")
        product("K", name=0.9, image=0.9, other=[_pub("MLA1950", "diferente", 30_000)])     # solo diferentes
        product("L", name=0.3, image=0.3, similar=[_sim("MLA1960", title="Combo ambiguo", reason="es ambiguo",
                                                         differences=["ambiguo en la medida"])])
        # fila vieja incoherente: dice que tiene similares pero no guardó la lista
        s.add(MarketPriceSnapshot(run_id=run.id, product_id="N", product_name="PN", color="sin_dato",
                                  ml_status="no_data", similar_count=2, our_price_cents=10_000))
        # otra corrida: no cuenta para la última
        product("Z", old.id, name=0.9, image=0.9, similar=[_amb("MLA1990")])
        s.commit()
        return run.id, old.id


def _ids(body):
    return {i["product"]["id"] for i in body["items"]}


def check_rule(get) -> None:
    body = get(match="para_confirmar", page_size=100)
    assert _ids(body) == IN
    assert body["total"] == body["para_confirmar"] == len(IN)
    # con el filtro puesto, los chips de color solo ven sin dato
    assert body["colors"] == {"verde": 0, "amarillo": 0, "rojo": 0, "sin_dato": len(IN), "no_medible": 0}
    assert _ids(get(match="para_confirmar", enabled="disabled")) == {"M"}
    assert _ids(get(match="para_confirmar", color="verde")) == set()
    assert _ids(get(match="para_confirmar", color="sin_dato", page_size=100)) == IN


def check_counter(get, old_run_id: int | None = None) -> None:
    full = len(IN)
    for params in ({}, {"color": "verde"}, {"color": "sin_dato"}, {"color": "no_medible"}, {"estimated": "any"},
                   {"estimated": "verde"}, {"match": "igual"}, {"match": "solo_similar"}, {"status": "failed"},
                   {"q": "no existe"}):
        assert get(**params)["para_confirmar"] == full, params
    assert get(enabled="enabled")["para_confirmar"] == full - 1          # M está deshabilitado
    assert get(enabled="disabled")["para_confirmar"] == 1
    assert get(origin="web")["para_confirmar"] == 1                      # solo A
    assert get(origin="api")["para_confirmar"] == 0
    assert get(review="firme")["para_confirmar"] == 1                    # solo B
    if old_run_id is not None:
        assert _ids(get(run_id=old_run_id, match="para_confirmar")) == {"Z"}


def check_pao_flow(get, post) -> None:
    """«Es el mismo» y «No es el mismo» sacan al producto de la lista y bajan el contador."""
    sid = {i["product"]["id"]: i["id"] for i in get(match="para_confirmar", page_size=100)["items"]}
    r = post(sid["A"], "same", "MLA1100")
    assert r.status_code == 200 and r.json()["snapshot"]["ml_status"] == "ok"
    r = post(sid["C"], "not-same", "MLA1300")                              # era el único similar (y el sin mirar)
    assert r.status_code == 200
    r = post(sid["D"], "not-same", "MLA1410")                              # se va el sin mirar, queda uno juzgado
    assert r.status_code == 200
    body = get(match="para_confirmar", page_size=100)
    assert _ids(body) == IN - {"A", "C", "D"}
    assert body["para_confirmar"] == len(IN) - 3


def _get(client):
    def get(**params):
        r = client.get("/api/price-monitor/snapshots", params=params)
        assert r.status_code == 200, r.text
        return r.json()
    return get


def _post(client):
    return lambda sid, path, ml_id: client.post(f"/api/price-monitor/snapshots/{sid}/{path}", json={"ml_id": ml_id})


def test_rule_includes_exactly_the_cases_worth_a_look(client):
    seed(engine)
    check_rule(_get(client))


def test_counter_ignores_color_and_estimated_but_follows_enabled_origin_review(client):
    _, old_id = seed(engine)
    check_counter(_get(client), old_id)


def test_same_and_not_same_take_it_out_of_the_list(client):
    seed(engine)
    check_pao_flow(_get(client), _post(client))


def test_bad_match_value_is_rejected_and_the_empty_db_answers_zero(client):
    assert client.get("/api/price-monitor/snapshots").json()["para_confirmar"] == 0
    assert client.get("/api/price-monitor/snapshots", params={"match": "para_confirmar_ya"}).status_code == 422
    assert client.get("/api/price-monitor/snapshots", params={"match": "confirmar"}).status_code == 400
