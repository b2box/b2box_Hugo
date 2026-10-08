"""QA independiente de la rama feat/semaforo-ml-web (criterios 5, 6 y 7).

  * «No es el mismo»: sesión, idempotencia (también con carrera), validación de
    ids, que solo toque el producto del snapshot, y el recálculo hecho a mano.
  * Chequeo de medidas: tabla de títulos reales de catálogo, unidades, packs,
    capacidad, bordes de la tolerancia y valores absurdos.
  * Parser del listado: precio solo en pesos, links y fotos saneados, páginas
    enormes, ilegibles o de verificación.

Los casos marcados xfail(strict=True) son BUGS REPRODUCIDOS que quedaron
reportados al developer: cuando se arreglen el test pasa a XPASS y falla, así
que hay que sacarle el xfail.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import BigInteger  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import auth, runtime  # noqa: E402
from app import main as main_mod  # noqa: E402
from app.db.models import (  # noqa: E402
    MarketMatchFeedback,
    MarketPriceSnapshot,
    PriceMonitorRun,
    Setting,
)
from app.db.session import engine  # noqa: E402
from app.ingest.browser_fetch import ListingPage  # noqa: E402
from app.pricing import market_ml_web as web  # noqa: E402
from app.pricing import match_feedback  # noqa: E402
from app.pricing.market_specs import (  # noqa: E402
    OurSpecs,
    differences,
    extract,
    our_specs_from_custom_fields,
)
from tests.ml_web_fixtures import ANTIBOT_HTML, listing_html, polycard  # noqa: E402
from tests.test_price_monitor_routes import _env, client  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _clean(_env):
    with Session(engine) as s:
        for model in (MarketMatchFeedback, Setting):
            for row in s.exec(select(model)).all():
                s.delete(row)
        s.commit()
    runtime.invalidate()
    yield
    runtime.invalidate()


# ─── «No es el mismo» (criterio 5) ─────────────────────────────────────────


def _entry(ml_id, price, *, listings=None, sellers=("s",), category="igual", origin="api", **kw):
    prices = listings if listings is not None else [price]
    return {"ml_id": ml_id, "title": f"Producto {ml_id}", "permalink": f"https://www.mercadolibre.com.ar/p/{ml_id}",
            "origin": origin, "category": category, "source": "clip", "image_score": 0.9, "name_score": 0.8,
            "confidence": None, "reason": "", "differences": [], "brand": None, "image_url": None,
            "listings": len(prices), "min_cents": min(prices), "median_cents": sorted(prices)[len(prices) // 2],
            "prices_cents": prices, "sellers": list(sellers), **kw}


def _put(product_id, matched, similar=(), *, our=10_000, **kw) -> int:
    with Session(engine) as s:
        run = s.exec(select(PriceMonitorRun)).first()
        if run is None:
            run = PriceMonitorRun(status="ok", total_products=2)
            s.add(run)
            s.commit()
            s.refresh(run)
        prices = [p for m in matched
                  for p in (m.get("prices_cents") or [m["median_cents"]] * int(m.get("listings") or 1))]
        snap = MarketPriceSnapshot(
            run_id=run.id, product_id=product_id, product_name=f"Producto {product_id}", ml_status="ok",
            our_price_cents=our, commission_pct=13.0, shipping_cents=0, color="verde", est_margin_pct=0.0,
            ml_median_cents=sorted(prices)[len(prices) // 2] if prices else None,
            ml_min_cents=min(prices) if prices else None, ml_listing_count=len(prices),
            ml_seller_count=1, match_origin="api", matched_listings=json.dumps(matched),
            similar_count=len(similar), similar_listings=json.dumps(list(similar)) if similar else None, **kw)
        s.add(snap)
        s.commit()
        s.refresh(snap)
        return snap.id


def _post(client, sid, ml_id, **extra):
    return client.post(f"/api/price-monitor/snapshots/{sid}/not-same", json={"ml_id": ml_id, **extra})


def _feedback():
    with Session(engine) as s:
        return [(f.product_id, f.ml_id) for f in s.exec(select(MarketFeedbackAlias)).all()]


MarketFeedbackAlias = MarketMatchFeedback


@pytest.mark.parametrize("prices, drop, median, minimum, margin, color", [
    # nuestro precio 10.000, comisión 13 %, sin envío: margen = (mediana·0,87 − 10.000) / 10.000
    ((9_000, 15_000, 16_000), 1, 12_500, 9_000, 8.75, "rojo"),        # sacar la del medio: verde → rojo
    ((9_000, 15_000, 16_000), 0, 15_500, 15_000, 34.85, "verde"),     # sacar la más barata: sube el mínimo
    ((12_000, 14_000, 30_000), 2, 13_000, 12_000, 13.1, "amarillo"),  # sacar la más cara: verde-ish → amarillo
])
def test_recalculation_matches_the_formula_computed_by_hand(client, prices, drop, median, minimum, margin, color):
    ids = ["MLA1001", "MLA1002", "MLA1003"]
    sid = _put("2", [_entry(ml, p, sellers=(f"v{i}",)) for i, (ml, p) in enumerate(zip(ids, prices))])
    snap = _post(client, sid, ids[drop]).json()["snapshot"]
    assert (snap["ml_median_cents"], snap["ml_min_cents"], snap["ml_listing_count"]) == (median, minimum, 2)
    assert snap["est_margin_pct"] == pytest.approx(margin)
    assert snap["color"] == color
    assert snap["ml_seller_count"] == 2 and snap["ml_status"] == "ok"
    assert snap["our_price_cents"] == 10_000          # nuestro precio no se toca
    assert ids[drop] not in [m["ml_id"] for m in snap["matched_listings"]]
    with Session(engine) as s:                        # y quedó guardado, no solo devuelto
        saved = s.get(MarketPriceSnapshot, sid)
        assert (saved.ml_median_cents, saved.color) == (median, color)


def test_dropping_the_last_igual_leaves_the_similars_and_no_color(client):
    sid = _put("2", [_entry("MLA1001", 9_000)], [_entry("MLA5005", 100, category="similar")])
    snap = _post(client, sid, "MLA1001").json()["snapshot"]
    assert (snap["ml_status"], snap["color"], snap["ml_median_cents"], snap["ml_min_cents"],
            snap["est_margin_pct"], snap["ml_listing_count"], snap["match_origin"]) == (
        "no_data", "sin_dato", None, None, None, 0, None)
    assert snap["similar_count"] == 1 and [x["ml_id"] for x in snap["similar_listings"]] == ["MLA5005"]


def test_it_never_touches_another_product_or_its_snapshot(client):
    a = _put("2", [_entry("MLA901", 9_000), _entry("MLA902", 15_000)])
    b = _put("7", [_entry("MLA901", 9_000), _entry("MLA902", 15_000)])
    # intentos de apuntar a otro producto desde el cuerpo: se ignoran
    r = _post(client, a, "MLA901", product_id="7", snapshot_id=b)
    assert r.status_code == 200
    assert _feedback() == [("2", "MLA901")]
    with Session(engine) as s:
        other = s.get(MarketPriceSnapshot, b)
        assert [m["ml_id"] for m in json.loads(other.matched_listings)] == ["MLA901", "MLA902"]
        assert other.color == "verde" and other.ml_median_cents == 15_000
    assert match_feedback.load_excluded() == {"2": frozenset({"MLA901"})}


def test_an_id_that_is_not_in_this_snapshot_is_a_404_and_writes_nothing(client):
    a = _put("2", [_entry("MLA1001", 9_000)])
    _put("7", [_entry("MLA777", 9_000)])
    assert _post(client, a, "MLA777").status_code == 404           # existe, pero en OTRO producto
    assert _feedback() == []


@pytest.mark.parametrize("body", [
    {"ml_id": 123}, {"ml_id": ["MLA1"]}, {"ml_id": None}, {"ml_id": "MLA1" + "0" * 40}, {"ml_id": ""},
    {"ml_id": "MLA1; DROP TABLE settings"}, {"ml_id": "MLA1'--"}, {"ml_id": "mla1"}, {"ml_id": "MLA-1234"},
    {"ml_id": "MLA1\n"}, {"ml_id": " MLA1234"}, [], "MLA1234",
])
def test_the_ml_id_is_validated_before_anything_runs(client, body):
    sid = _put("2", [_entry("MLA1234", 9_000)])
    r = client.post(f"/api/price-monitor/snapshots/{sid}/not-same", json=body)
    assert r.status_code in (400, 422), (body, r.status_code)
    assert _feedback() == []
    with Session(engine) as s:
        assert s.get(MarketPriceSnapshot, sid).ml_median_cents == 9_000


@pytest.mark.parametrize("snapshot_id", ["0", "-1", "abc", str(2**40), "1.5"])
def test_the_snapshot_id_is_validated(client, snapshot_id):
    r = client.post(f"/api/price-monitor/snapshots/{snapshot_id}/not-same", json={"ml_id": "MLA1234"})
    assert r.status_code in (404, 422)


@pytest.mark.parametrize("ml_id", ["MLA1%2F..%2F..", "..", "MLA1;x", "%00", "MLA<script>", "x" * 300])
def test_the_undo_path_is_validated_too(client, ml_id):
    r = client.delete(f"/api/price-monitor/products/2/not-same/{ml_id}")
    assert r.status_code in (400, 404, 422)


def test_undo_only_removes_that_product_and_that_id(client):
    for pid in ("2", "7"):
        for ml in ("MLA1234", "MLA5678"):
            match_feedback.add_feedback(product_id=pid, ml_id=ml, entry=None, snapshot_id=None, product_name=None)
    assert client.delete("/api/price-monitor/products/2/not-same/MLA1234").status_code == 200
    assert sorted(_feedback()) == [("2", "MLA5678"), ("7", "MLA1234"), ("7", "MLA5678")]


def test_the_session_is_required_for_real_not_just_present():
    sid = _put("2", [_entry("MLA1234", 9_000)])
    for cookie in ("", "garbage", "a.b.c", auth.issue_session_token("admin")[:-3] + "xxx"):
        anon = TestClient(main_mod.app)
        if cookie:
            anon.cookies.set(auth.COOKIE_NAME, cookie)
        assert anon.post(f"/api/price-monitor/snapshots/{sid}/not-same", json={"ml_id": "MLA1234"}).status_code == 401
        assert anon.delete("/api/price-monitor/products/2/not-same/MLA1234").status_code == 401
    # con Accept: text/html el middleware redirige al login, y tampoco ejecuta nada
    html = TestClient(main_mod.app, follow_redirects=False)
    r = html.post(f"/api/price-monitor/snapshots/{sid}/not-same", json={"ml_id": "MLA1234"},
                  headers={"Accept": "text/html"})
    assert r.status_code in (302, 401)
    assert _feedback() == []
    with Session(engine) as s:
        assert s.get(MarketPriceSnapshot, sid).ml_median_cents == 9_000


def test_pressing_the_button_from_several_tabs_at_once_is_idempotent(client):
    sid = _put("2", [_entry("MLA1234", 9_000), _entry("MLA5678", 15_000)])
    cookie = client.cookies.get(auth.COOKIE_NAME)

    def press(_):
        c = TestClient(main_mod.app)
        c.cookies.set(auth.COOKIE_NAME, cookie)
        return c.post(f"/api/price-monitor/snapshots/{sid}/not-same", json={"ml_id": "MLA1234"})

    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = [r.status_code for r in pool.map(press, range(8))]
    assert set(codes) == {200}, codes
    assert _feedback() == [("2", "MLA1234")]
    with Session(engine) as s:
        snap = s.get(MarketPriceSnapshot, sid)
        assert [m["ml_id"] for m in json.loads(snap.matched_listings)] == ["MLA5678"]
        assert snap.ml_median_cents == 15_000 and snap.ml_listing_count == 1


def test_the_exclusion_survives_a_snapshot_that_no_longer_lists_the_id(client):
    sid = _put("2", [_entry("MLA1234", 9_000), _entry("MLA5678", 15_000)])
    assert _post(client, sid, "MLA1234").json()["already"] is False
    again = _post(client, sid, "MLA1234")
    assert again.status_code == 200 and again.json()["already"] is True


@pytest.mark.xfail(strict=True, reason="BUG-L1: en una fila vieja (sin prices_cents, la que dejó main) el mínimo "
                                       "recalculado sale de la mediana repetida y pierde min_cents")
def test_old_rows_keep_their_real_minimum_after_not_the_same(client):
    old_a = {"ml_id": "MLA1001", "title": "A", "permalink": "https://www.mercadolibre.com.ar/p/MLA1", "listings": 2,
             "min_cents": 20_000, "median_cents": 21_000, "source": "clip", "image_score": 0.9, "name_score": 1.0,
             "confidence": None}
    old_b = {**old_a, "ml_id": "MLA1002", "listings": 1, "min_cents": 30_000, "median_cents": 30_000}
    sid = _put("2", [old_a, old_b])
    snap = _post(client, sid, "MLA1002").json()["snapshot"]
    assert snap["ml_min_cents"] == 20_000


@pytest.mark.xfail(strict=True, reason="BUG-L2: tras «No es el mismo» los contadores por color de la CORRIDA "
                                       "(n_verde, n_rojo…) quedan viejos; los chips del snapshot sí se recalculan")
def test_run_color_counters_follow_the_recalculated_snapshot(client):
    sid = _put("2", [_entry("MLA1001", 9_000), _entry("MLA1002", 15_000, sellers=("b",)),
                     _entry("MLA1003", 16_000, sellers=("c",))])
    with Session(engine) as s:
        run = s.exec(select(PriceMonitorRun)).first()
        run.n_verde, run.n_ok, run.processed = 1, 1, 1
        s.add(run)
        s.commit()
    snap = _post(client, sid, "MLA1002").json()["snapshot"]
    assert snap["color"] == "rojo"
    colors = client.get("/api/price-monitor/runs").json()["items"][0]["colors"]
    assert colors["rojo"] == 1 and colors["verde"] == 0


# ─── chequeo de medidas (criterio 6) ───────────────────────────────────────


@pytest.mark.parametrize("ours, theirs, want", [
    # capacidad: ml / l / cc / litro(s)
    ("Botella 500 ml", "Botella 0.5 L", []),
    ("Botella 500 ml", "Botella 1 litro", ["capacidad"]),
    ("Botella 1,5 L", "Botella 1500ml", []),
    ("Botella 1.5 L", "Botella 1500 cc", []),
    ("Termo 1 litro", "Termo 1lt", []),
    ("Vaso 350ml", "Vaso 355 ml", []),           # ±3 % de capacidad
    ("Vaso 350ml", "Vaso 450 ml", ["capacidad"]),
    # packs: x2, x 2, pack de 3, set, docena, "2 en 1" no es pack
    ("Organizador", "Organizador x2", ["cantidad"]),
    ("Organizador x2", "Organizador pack x 2", []),
    ("Organizador x2", "Organizador x3", ["cantidad"]),
    ("Organizador", "Organizador pack de 3 unidades", ["cantidad"]),
    ("Set de 6 vasos", "Vasos x6", []),
    ("Set de 6 vasos", "Set 12 vasos", ["cantidad"]),
    ("Organizador", "Organizador 2 en 1", []),
    ("Botella 500ml", "Botella 500 ml 1 unidad", []),
    ("Botella 500ml", "Botella 500 ml docena", ["cantidad"]),
    ("Pila 1.5V AAA x4", "Pila AAA 1.5V pack x 4", []),
    ("Pila 1.5V AAA x4", "Pila AAA 1.5V pack x 8", ["cantidad"]),
    ("Pack x2 Botella 500ml", "Botella 500ml x 2", []),
    ("Bolsa 50 x 70", "Bolsa 50x70 x 100 unidades", ["cantidad"]),   # 50 x 70 es medida, x 100 es pack
    # medidas: cm / mm / m, cualquier orden, con o sin unidad
    ("Alfombra 40x60", "Alfombra 40 x 60 cm", []),
    ("Alfombra 40x60", "Alfombra 400x600 mm", []),
    ("Alfombra 40x60", "Alfombra 0,4 x 0,6 m", []),
    ("Alfombra 40x60", "Alfombra 0.4x0.6m", []),
    ("Alfombra 40x60 cm", "Alfombra 60x40 cm", []),
    ("Alfombra 40x60", "Alfombra 50x80", ["medida"]),
    ("Alfombra 40x60 cm", "Alfombra 120x180 cm", ["medida"]),
    ("Estante 30x20x10 cm", "Estante 30x20 cm", []),
    ("Estante 30 x 20 x 10", "Estante 31x21x11 cm", []),
    ("Cable usb 1m", "Cable usb 100cm", []),
    ("Cable usb 1m", "Cable usb 3m", ["medida"]),
    ("Cable 0,5m", "Cable 50 cm", []),
    ("Cinta 3m x 12mm", "Cinta 3 m x 12 mm", []),
    ("Cinta 3m x 12mm", "Cinta 5 m x 12 mm", ["medida"]),
    # peso: kg / g
    ("Balanza 5kg", "Balanza 5000 g", []),
    ("Balanza 5kg", "Balanza 10 kg", ["peso"]),
    # lo que NO se mide no genera diferencias inventadas
    ("Pendrive 64GB", "Pendrive 128GB", []),
    ("Cargador 20W", "Cargador 65W", []),
    ("Camara 1080p", "Camara 720p", []),
    ("Cuchillo 20 cm", "Cuchillo 8 pulgadas", []),
    ("Zapatilla 42", "Zapatilla 43", []),
    ("Remera talle M", "Remera talle XL", []),
    ("Producto", "Producto sin ninguna medida", []),
])
def test_title_pairs(ours, theirs, want):
    assert differences(ours, None, theirs) == want


@pytest.mark.xfail(strict=True, reason="BUG-L3: «x 3» después de un token que termina en dígito (E27, 42) se toma "
                                       "por una medida «N x M» y no cuenta como pack")
@pytest.mark.parametrize("ours, theirs", [
    ("Foco LED 9W E27", "Foco LED 9W E27 x 3"),
    ("Zapatilla talle 42", "Zapatilla talle 42 x 2"),
])
def test_a_pack_after_a_model_code_is_a_different_quantity(ours, theirs):
    assert differences(ours, None, theirs) == ["cantidad"]


@pytest.mark.parametrize("ours_value, theirs_cm, diverges", [
    (100.0, 110.0, False),            # justo el 10 % (relativo a lo NUESTRO)
    (100.0, 111.0, True),
    (100.0, 90.0, False),
    (100.0, 89.0, True),
])
def test_the_dimension_tolerance_boundary(ours_value, theirs_cm, diverges):
    specs = OurSpecs(length=ours_value, width=50.0)
    got = differences("Producto", specs, f"Producto {theirs_cm:g}x50 cm")
    assert (got == ["medida"]) is diverges


def test_the_tolerances_are_configurable_and_zero_is_exact():
    specs = OurSpecs(length=100.0, width=50.0)
    assert differences("P", specs, "P 105x50 cm", dim_tol_pct=0) == ["medida"]
    assert differences("P", specs, "P 105x50 cm", dim_tol_pct=5) == []
    assert differences("P", specs, "P 105x50 cm", dim_tol_pct=-5) == ["medida"]       # negativo = exacto
    heavy = OurSpecs(weight=1.0)
    assert differences("P", heavy, "P 1,2 kg", weight_tol_pct=15) == ["peso"]
    assert differences("P", heavy, "P 1,12 kg", weight_tol_pct=15) == []


@pytest.mark.parametrize("raw", [{}, None, "x", [], {"length": 0, "width": -3, "weight": "1"},
                                 {"length": True, "weight": None}, {"length": float("nan")}])
def test_garbage_custom_fields_give_no_specs(raw):
    got = our_specs_from_custom_fields(raw)
    assert got is None or (got.dims_cm == () and got.weight is None)


def test_a_publication_with_no_measures_never_clashes_with_ours():
    ours = OurSpecs(length=40, width=30, height=5, weight=0.5)
    assert differences("Producto", ours, "Producto premium con tapa") == []
    assert differences("Producto", OurSpecs(), "Producto 30x40 cm 500 g") == []        # sin medidas nuestras


def test_absurd_numbers_in_titles_are_ignored_not_crashes():
    for title in ["x5000", "123456789", "1.000.000 ml", "999999999 litros", "0 ml", "x0", "10000 kg",
                  "Pack x1000", "-5 cm", "5x", "x x x", "1e5 ml", "30x40x50x60 cm", "99999x99999 cm", "x" * 5000,
                  "9" * 5000, "ml " * 3000]:
        extract(title)
        differences("Producto 500 ml x2", OurSpecs(length=40, width=30), title)


def test_extraction_is_fast_on_pathological_titles():
    start = time.perf_counter()
    for title in ("1 " * 2000, "x " * 2000, "30x" * 1500, "1,5 " * 1500, ("a" * 50 + " 1 ") * 200):
        extract(title)
    assert time.perf_counter() - start < 2.0


# ─── parser del listado (criterio 7) ───────────────────────────────────────


def _html(*cards, ld=None):
    return listing_html(list(cards), ld=ld)


def test_prices_are_only_taken_when_they_are_a_sane_number():
    cards = [polycard("MLA100001", "Producto A", 100.0), polycard("MLA100002", "Producto B", 0.0),
             polycard("MLA100003", "Producto C", -5.0), polycard("MLA100004", "Producto D", 1e13),
             polycard("MLA100005", "Producto E", "100"), polycard("MLA100006", "Producto F", True)]
    got = {c.id: c.price_cents for c in web.parse_search(_html(*cards), 20).candidates}
    assert got == {"MLA100001": 10_000, "MLA100002": None, "MLA100003": None, "MLA100004": None,
                   "MLA100005": None, "MLA100006": None}


def test_the_currency_travels_with_the_candidate_and_pesos_only_count():
    cards = [polycard("MLA100001", "Producto A", 100.0), polycard("MLA100002", "Producto B", 100.0, currency="USD"),
             polycard("MLA100003", "Producto C", 100.0, currency="usd")]
    got = {c.id: c.currency for c in web.parse_search(_html(*cards), 20).candidates}
    assert got == {"MLA100001": "ARS", "MLA100002": "USD", "MLA100003": "USD"}


@pytest.mark.parametrize("url", [
    "http://www.mercadolibre.com.ar/x", "javascript:alert(1)", "https://evil.com/x",
    "https://www.mercadolibre.com.ar.evil.com/x", "https://www.mercadolibre.com.ar@evil.com/x",
    "evil.com/www.mercadolibre.com.ar", "//evil.com/x", "https://user:pw@www.mercadolibre.com.ar/x",
    "https://www.mercadolibre.com.ar:8443/x", "data:text/html,<script>", "https://www.mercadolibre.com.ar/x y",
    "www.mercadolibre.com.ar\\@evil.com",
])
def test_hostile_permalinks_become_empty_and_the_candidate_survives(url):
    [cand] = web.parse_search(_html(polycard("MLA100001", "Producto A", 100.0, url=url)), 20).candidates
    assert cand.permalink == "" and cand.id == "MLA100001" and cand.price_cents == 10_000


def test_good_permalinks_are_https_and_mercadolibre_only():
    urls = {"www.mercadolibre.com.ar/x/p/MLA1": "https://www.mercadolibre.com.ar/x/p/MLA1",
            "articulo.mercadolibre.com.ar/MLA-1-x#polycard_client=search": "https://articulo.mercadolibre.com.ar/MLA-1-x"}
    for raw, want in urls.items():
        [cand] = web.parse_search(_html(polycard("MLA100001", "Producto A", 100.0, url=raw)), 5).candidates
        assert cand.permalink == want


@pytest.mark.xfail(strict=True, reason="BUG-L4: un resultado PATROCINADO trae como url el click-tracker sin los "
                                       "parámetros (url_params va aparte) y el permalink guardado es un link muerto")
def test_a_sponsored_result_does_not_keep_a_dead_click_tracker_as_its_link():
    [cand] = web.parse_search(_html(polycard(
        "MLA1154769187", "Fuente para impresora", 4830.0,
        url="click1.mercadolibre.com.ar/mclics/clicks/external/MLA/count")), 5).candidates
    assert "click1." not in cand.permalink


@pytest.mark.xfail(strict=True, reason="BUG-L5: \\d de Python acepta dígitos Unicode: «MLA١٢٣» pasa como id de ML "
                                       "(en el parser y en valid_ml_id). Usar re.ASCII")
def test_ml_ids_are_ascii_digits_only():
    assert web.parse_search(_html(polycard("MLA١٢٣٤٥٦", "Producto A", 100.0)), 5).candidates == []
    assert not match_feedback.valid_ml_id("MLA١٢٣٤")


def test_bad_ids_and_titles_are_dropped_not_fatal():
    cards = [polycard("MLA100001", "Bueno", 1.0), polycard("MLB100002", "Otro sitio", 1.0),
             polycard("mla100003", "minúscula", 1.0), polycard("MLA 100004", "espacio", 1.0),
             polycard("MLA100005", "", 1.0), polycard("MLA100006", "{icon_cockade}", 1.0),
             {"id": "POLYCARD", "polycard": "no es un dict"}, {"id": "X"}, None, 5]
    got = [c.id for c in web.parse_search(_html(*[c for c in cards if isinstance(c, dict)]), 20).candidates]
    assert got == ["MLA100001"]


def test_a_huge_page_parses_fast_and_in_bounded_memory():
    cards = [polycard(f"MLA{200000 + i}", f"Organizador número {i}", 100.0 + i) for i in range(48)]
    html = _html(*cards).replace("</head>", "<script>/*" + "x" * 3_000_000 + "*/</script></head>")
    assert len(html) > 3_000_000
    start = time.perf_counter()
    parsed = web.parse_search(html, 8)
    assert time.perf_counter() - start < 1.5
    assert len(parsed.candidates) == 8 and parsed.total == 48 and parsed.source == "state"


@pytest.mark.parametrize("html", [
    "", "<html>", "<script id=\"__NORDIC_RENDERING_CTX__\">_n.ctx.r={\"appProps\":", "{" * 5000,
    "<script type=\"application/ld+json\">{</script>", "\x00\x01\x02", "<script id=\"__NORDIC_RENDERING_CTX__\">_n.ctx.r=[]</script>",
    "<script id=\"__NORDIC_RENDERING_CTX__\">_n.ctx.r={\"appProps\":{\"pageProps\":{\"initialState\":{\"results\":\"x\"}}}}</script>",
])
def test_unreadable_pages_never_raise(html):
    parsed = web.parse_search(html, 8)
    assert parsed.candidates == []


def test_a_verification_page_is_a_block_even_with_a_200_and_junk_in_it():
    pg = ListingPage(html="<html><title>Verificá tu cuenta</title></html>", status=200, bytes=5000,
                     final_url="https://www.mercadolibre.com/jms/mla/lgz/account-verification?go=x")
    assert web.page_problem(pg, web.parse_search(pg.html, 8))[0] == "blocked"
    pg2 = ListingPage(html=ANTIBOT_HTML, status=200, bytes=5000, final_url="https://listado.mercadolibre.com.ar/x")
    assert web.page_problem(pg2, web.parse_search(pg2.html, 8))[0] == "blocked"
    pg3 = ListingPage(html="<html>login</html>", status=200, bytes=5000, final_url="https://evil.example/login")
    assert web.page_problem(pg3, web.parse_search(pg3.html, 8))[0] == "blocked"


@pytest.mark.xfail(strict=True, reason="BUG-L6 (endurecimiento): una página de verificación que lleve el estado de "
                                       "ML con results=[] se clasifica 'empty' (válida, corta la racha) y no "
                                       "'blocked'; page_problem sale temprano si hubo estado")
def test_a_verification_redirect_with_an_empty_state_is_still_a_block():
    pg = ListingPage(html=listing_html([]), status=200, bytes=5000,
                     final_url="https://www.mercadolibre.com/jms/mla/lgz/account-verification?go=x")
    assert web.page_problem(pg, web.parse_search(pg.html, 8)) is not None


# ─── columnas nuevas en Postgres (criterio 8) ──────────────────────────────


@pytest.mark.xfail(strict=True, reason="BUG-L7: price_monitor_run.web_bytes es INTEGER de 32 bits (2,1 GB): con "
                                       "pm_ml_web_block_scripts=0 (3,4 GB por noche según el README) el UPDATE "
                                       "revienta en Postgres con NumericValueOutOfRange. Debe ser BigInteger")
def test_the_run_byte_counter_does_not_overflow_in_postgres():
    col = PriceMonitorRun.__table__.c.web_bytes
    assert isinstance(col.type, BigInteger)


@pytest.mark.xfail(strict=True, reason="BUG-L8: de una ficha de la API se comparan también PACKAGE_LENGTH/WIDTH/HEIGHT/"
                                       "WEIGHT (la CAJA de envío) contra las medidas del PRODUCTO, y se mira un solo "
                                       "grupo (el primero): da 'medida'/'peso' falsos y depende del orden del dict")
def test_package_attributes_of_a_catalog_card_do_not_decide():
    ours = OurSpecs(length=20, width=15, height=5, weight=0.4)
    attrs = {"PACKAGE_LENGTH": "30 cm", "PACKAGE_WEIGHT": "600 g", "LENGTH": "20 cm", "WEIGHT": "400 g"}
    assert differences("Producto", ours, "Producto", attrs) == []
    assert differences("Producto", ours, "Producto", dict(reversed(attrs.items()))) == []
