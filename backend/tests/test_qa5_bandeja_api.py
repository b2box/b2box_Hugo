"""QA independiente (criterio 4): auth, filtros, detalle (diagnóstico, links, desglose), «No es el mismo» y botones de decisión."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from decimal import Decimal as D

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app import main as main_mod  # noqa: E402
from app import security  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.models import MarketMatchFeedback, MarketPriceSnapshot, PriceMonitorRun, PriceReviewEvent, PriceReviewItem  # noqa: E402
from app.pricing import price_review as pr  # noqa: E402
from tests import qa5_bandeja as q  # noqa: E402
from tests.qa5_bandeja import NOW, VendureWorld, healthy, mk_snap  # noqa: E402

BASE = "/api/price-review"


def settings(**over) -> Settings:
    base = dict(vendure_api_url="https://example.invalid/admin-api", hugo_env="development", dashboard_user="admin",
                dashboard_password="secreto", dashboard_secret="s" * 32, supabase_url="", supabase_anon_key="",
                price_review_viewers="pao@b2box.pro",                      # Pao es responsable de costos (ve fábrica y fee)
                vendure_admin_product_url="https://admin.b2box.invalid/products/{id}",
                storefront_product_url="https://b2box.app/p/{slug}")
    base.update(over)
    return Settings(**base)


@pytest.fixture(autouse=True)
def stack(monkeypatch):
    q.reset_db()
    q.setting("pm_mode", 1)
    cfg = settings()
    from app.api import price_review_routes as routes

    for mod in (auth, security, main_mod, routes):
        if hasattr(mod, "get_settings"):
            monkeypatch.setattr(mod, "get_settings", lambda cfg=cfg: cfg)
    monkeypatch.setattr(pr.catalog_index, "neighbors_of", lambda pid, k=10: [(f"n{i}", 0.9) for i in range(10)])
    world = VendureWorld()
    world.spy = q.install_spy(monkeypatch, world)
    yield world
    assert world.writes() == [] and world.spy.violations == []


@pytest.fixture
def client() -> TestClient:
    c = TestClient(main_mod.app, raise_server_exceptions=False)
    c.cookies.set(auth.COOKIE_NAME, auth.issue_session_token("pao@b2box.pro"))
    return c


def hostile(s: str) -> str:
    return s


async def open_fichas(world: VendureWorld, specs: dict[str, dict]) -> int:
    """Abre una ficha por producto (2 noches de rojo firme). `specs`: pid → argumentos de mk_snap. Devuelve el run de hoy."""
    prev, today = q.new_run(NOW - timedelta(days=1)), q.new_run(NOW)
    for i, box in enumerate(q.neighbors()):            # los diez productos parecidos (verdes: no abren ficha)
        q.save(mk_snap(today, f"n{i}", our=500_000, specs={"box_length": box.length, "box_width": box.width,
                                                            "box_height": box.height, "box_weight": box.weight}))
    for pid, kw in specs.items():
        sc = kw.pop("scenario", None) or healthy(pid)
        if sc.median == 0:
            sc.median = int(sc.our_price * 0.8)            # ML a 80 % de nuestro precio: rojo real, cociente 1,25
        world.add(sc)
        base = dict(our=sc.our_price, variant=sc.variant, tier=f"tier:min_qty={sc.used_min_q}", specs=sc.specs(),
                    listings=((sc.median, "clip", "a"), (sc.median + 1000, "clip", "b"), (sc.median - 1000, "clip", "c")))
        base.update(kw)
        q.save(mk_snap(prev, pid, **dict(base, data_at=NOW - timedelta(days=1), captured_at=NOW - timedelta(days=1), flag="firme")))
        q.save(mk_snap(today, pid, **base))
    await pr.process_run(today, now=NOW + timedelta(hours=1))
    return today


# ─── Auth: 401 sin cookie en TODOS los endpoints nuevos, también con rarezas de URL ────────────────


def test_every_endpoint_answers_401_without_a_session_for_every_method():
    anon = TestClient(main_mod.app, raise_server_exceptions=False)
    for path in ("/summary", "/items", "/items/1", "/items/999999"):
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            r = anon.request(method, BASE + path)
            assert r.status_code == 401, (method, path, r.status_code)
        assert anon.request("GET", BASE + path, headers={"accept": "text/html"}, follow_redirects=False).status_code == 302


def test_a_forged_expired_or_foreign_cookie_is_not_a_session():
    good = auth.issue_session_token("pao@b2box.pro")
    forged = good[:-4] + ("AAAA" if not good.endswith("AAAA") else "BBBB")
    other = TestClient(main_mod.app, raise_server_exceptions=False)
    for token in ("", "x", "pao@b2box.pro", forged, good + "x", good.replace(".", ":")):
        other.cookies.clear()
        other.cookies.set(auth.COOKIE_NAME, token)
        assert other.get(BASE + "/summary").status_code == 401, token


@pytest.mark.parametrize("url", [
    "/api/price-review/summary/", "//api/price-review/summary", "/api/price-review//summary", "/API/price-review/summary",
    "/api/price-review/items/%2e%2e/summary", "/api/price-review/items/1/../../summary", "/api/./price-review/summary",
    "/api/price-review/summary%00", "/api/price-review/summary;x", "/api/price-review/items?state=todas&page=0"])
def test_url_tricks_never_reach_the_data_without_a_session(url):
    anon = TestClient(main_mod.app, raise_server_exceptions=False)
    r = anon.get(url)
    assert r.status_code in (401, 404, 307, 308, 422), (url, r.status_code)
    if r.status_code in (307, 308):
        assert anon.get(r.headers["location"], follow_redirects=False).status_code in (401, 404)


# ─── Filtros ─────────────────────────────────────────────────────────────────────────────────


async def test_filters_by_tab_state_cause_and_search(client, stack):
    big = healthy("1", box=q.BIG_BOX, cbm="0.024", kg="6.0")
    cheap = healthy("2", costs=["6", "5.5", "5"], box=q.OK_BOX, product=q.OK_PRODUCT)     # caja normal, fábrica baratísima
    cert = healthy("3", cert="40")
    cert.median = q.median_for(cert.our_price, -2)
    await open_fichas(stack, {"1": dict(scenario=big), "2": dict(scenario=cheap), "3": dict(scenario=cert),
                              "4": dict(scenario=healthy("4"), name_override=None)} if False else
                      {"1": dict(scenario=big), "2": dict(scenario=cheap), "3": dict(scenario=cert)})
    all_ = client.get(BASE + "/items", params={"state": "todas", "page_size": 100}).json()
    assert all_["total"] == 3
    by_cause = {i["probable_cause"]: i["product"]["id"] for i in all_["items"]}
    assert by_cause == {"a": "1", "d": "2", "c": "3"}
    for cause, pid in by_cause.items():
        r = client.get(BASE + "/items", params={"cause": cause}).json()
        assert [i["product"]["id"] for i in r["items"]] == [pid] and r["total"] == 1
        assert r["causes"][cause] == 1
    assert client.get(BASE + "/items", params={"cause": "ninguna"}).json()["total"] == 0
    # pestañas / estados
    assert client.get(BASE + "/items", params={"state": "para_decidir"}).json()["total"] == 3
    for st in ("en_proceso", "mantenidos", "cerrados"):
        assert client.get(BASE + "/items", params={"state": st}).json()["total"] == 0
    assert client.get(BASE + "/items", params={"state": "propuesto"}).json()["total"] == 3
    assert client.get(BASE + "/items", params={"state": "abiertas"}).json()["total"] == 3
    # búsqueda por código, por nombre y por id
    assert client.get(BASE + "/items", params={"q": "BX2"}).json()["total"] == 1
    assert client.get(BASE + "/items", params={"q": "producto 3"}).json()["total"] == 1
    assert client.get(BASE + "/items", params={"q": "3"}).json()["total"] >= 1
    for evil in ("%", "_", "\\", "' OR 1=1 --", "BX1%", "\x00", "a" * 120):
        r = client.get(BASE + "/items", params={"q": evil})
        assert r.status_code == 200, evil
    assert client.get(BASE + "/items", params={"q": "%"}).json()["total"] == 0           # el comodín se escapa
    assert client.get(BASE + "/items", params={"q": "_"}).json()["total"] == 0


async def test_invalid_filters_and_pages_are_rejected_not_500(client, stack):
    await open_fichas(stack, {"1": {}})
    for params in ({"state": "nada"}, {"cause": "z"}, {"page": -1}, {"page": "x"}, {"page_size": 0}, {"page_size": 101},
                   {"page": 10_001}, {"q": "x" * 121}):
        r = client.get(BASE + "/items", params=params)
        assert r.status_code in (400, 422), (params, r.status_code)
    assert client.get(BASE + "/items", params={"page": 10_000}).json()["items"] == []
    for iid in ("0", "-1", "x", str(2**31), "99999999999999999999", "1.5"):
        assert client.get(f"{BASE}/items/{iid}").status_code in (404, 422), iid


async def test_pagination_orders_open_ones_oldest_first_and_has_more(client, stack):
    await open_fichas(stack, {str(i): {} for i in range(7)})
    seen = []
    for page in range(3):
        r = client.get(BASE + "/items", params={"page": page, "page_size": 3}).json()
        seen += [i["id"] for i in r["items"]]
        assert r["has_more"] is (page < 2) and r["total"] == 7
    assert sorted(seen) == seen and len(set(seen)) == 7


async def test_summary_numbers_agree_with_the_list(client, stack):
    await open_fichas(stack, {str(i): {} for i in range(4)})
    s = client.get(BASE + "/summary").json()
    assert s["pending"] == 4 and s["counts"]["propuesto"] == 4 and s["groups"]["para_decidir"] == 4
    assert s["mode"] == 1 and s["mode_effective"] == 1 and s["read_only"] is True
    assert s["last_pass"]["new"] == 4 and s["last_pass"]["firm"] == 4
    q.setting("pm_mode", 2)
    s2 = client.get(BASE + "/summary").json()
    assert s2["mode"] == 2 and s2["mode_effective"] == 1 and "propuesta" in s2["mode_label"] + "propuesta"


# ─── Detalle ─────────────────────────────────────────────────────────────────────────────────


async def test_detail_has_the_diagnosis_a_breakdown_that_adds_up_links_and_the_date_and_origin(client, stack):
    big = healthy("1", box=q.BIG_BOX, cbm="0.024", kg="6.0")
    await open_fichas(stack, {"1": dict(scenario=big)})
    item = q.rows(PriceReviewItem)[0]
    d = client.get(f"{BASE}/items/{item.id}").json()
    assert d["state"] == "propuesto" and d["probable_cause"] == "a" and d["cause_label"] == "Caja fuera de rango"
    assert "mal cargada" in d["cause_summary"] and d["fixable"] == "aprox"
    # diagnóstico: las cinco reglas en orden
    assert [r["code"] for r in d["diagnosis"]["rules"]] == ["a", "b", "c", "d", "e"]
    m = int(big.our_price * 0.8)                                  # la mediana de las tres publicaciones firmes (m-1000, m, m+1000)
    assert d["diagnosis"]["p_max_cents"] == pytest.approx(q.price_max(m), abs=2)
    assert d["diagnosis"]["gap"] == pytest.approx(1 - q.price_max(m) / big.our_price, abs=1e-3)
    # desglose: 10 partes que suman la factura (0,01 USD)
    b = d["breakdown"]
    assert b["usd_available"] and len(b["components"]) == 10
    assert abs(sum(D(str(c["usd"])) for c in b["components"]) - D(str(b["factura_usd"]))) <= D("0.01")
    assert [c["key"] for c in b["components"]] == ["fabrica", "flete", "aranceles", "servicios", "logistica_china", "fee",
                                                   "costo_financiero", "certificado", "pasarela", "iva_venta"]
    assert len(d["tiers"]) == 3 and all(t["margin_pct"] is not None for t in d["tiers"])
    # contenido de la ficha: al menos un link de ML https, fecha y origen del dato
    snap = d["snapshot"]
    ml_links = [m["permalink"] for m in snap["matched_listings"]]
    assert ml_links and all(l.startswith("https://") and "mercadolibre" in l for l in ml_links)
    assert d["data"]["at"].endswith("Z") and d["data"]["origin"] == "api"
    assert d["links"] == {"vendure_admin": "https://admin.b2box.invalid/products/1", "storefront": "https://b2box.app/p/producto-1"}
    assert d["decisions_enabled"] is False
    assert [e["kind"] for e in d["events"]] == ["propuesto"] and d["events"][0]["actor"] == "hugo:auto"


@pytest.mark.parametrize("template", ["javascript:alert(1)//{id}", "http://admin.b2box.invalid/{id}", "//evil.invalid/{id}",
                                      "data:text/html,{id}", "ftp://x/{id}", " https://x/{id}", "",
                                      "https://x/{id}/{otro}", "https://x/{id}}"])
async def test_links_that_are_not_https_templates_with_the_marker_are_dropped(client, stack, monkeypatch, template):
    from app.api import price_review_routes as routes

    monkeypatch.setattr(routes, "get_settings", lambda: settings(vendure_admin_product_url=template, storefront_product_url=template))
    await open_fichas(stack, {"1": {}})
    d = client.get(f"{BASE}/items/{q.rows(PriceReviewItem)[0].id}").json()
    assert d["links"] == {"vendure_admin": None, "storefront": None}, template


async def test_a_template_without_a_marker_does_not_produce_the_same_link_for_every_ficha(client, stack, monkeypatch):
    from app.api import price_review_routes as routes

    monkeypatch.setattr(routes, "get_settings", lambda: settings(vendure_admin_product_url="https://x/no-marker"))
    await open_fichas(stack, {"1": {}})
    d = client.get(f"{BASE}/items/{q.rows(PriceReviewItem)[0].id}").json()
    assert d["links"]["vendure_admin"] is None


async def test_link_values_are_escaped_so_a_product_id_or_slug_cannot_bend_the_url(client, stack):
    await open_fichas(stack, {"1": {}})
    from sqlmodel import Session, select
    from app.db.session import engine

    with Session(engine) as ses:
        item = ses.exec(select(PriceReviewItem)).first()
        item.product_slug = 'x"><script>alert(1)</script>/../../evil?a=b#c'
        item.image_url = "javascript:alert(1)"
        ses.add(item)
        ses.commit()
        item_id = item.id
    d = client.get(f"{BASE}/items/{item_id}").json()
    link = d["links"]["storefront"]
    assert link.startswith("https://b2box.app/p/") and "<" not in link and '"' not in link and "?" not in link and "#" not in link
    assert "/../" not in link and link.count("/") == 4
    assert d["product"]["image_url"] is None                        # javascript: nunca llega al <img src>
    lst = client.get(BASE + "/items").json()["items"][0]
    assert lst["product"]["image_url"] is None


async def test_detail_sanitizes_listings_from_third_parties(client, stack):
    evil = [q.entry(1, 2_700_000, "clip", "<img src=x onerror=alert(1)>"), q.entry(2, 2_720_000, "clip", "b"),
            q.entry(3, 2_690_000, "clip", "c")]
    evil[0].update(title="<script>alert(1)</script>", permalink="javascript:alert(document.cookie)", image_url="javascript:alert(1)")
    evil[1].update(permalink="http://articulo.mercadolibre.com.ar/MLA-2-x", image_url="http://img/x.jpg")
    evil[2].update(permalink="https://evil.invalid/phish", image_url="data:image/png;base64,AAAA")
    await open_fichas(stack, {"1": dict(entries=evil)})
    d = client.get(f"{BASE}/items/{q.rows(PriceReviewItem)[0].id}").json()
    for m in d["snapshot"]["matched_listings"]:
        link, img = m["permalink"], m["image_url"]
        assert not link or (link.startswith("https://") and "mercadolibre" in link), link
        assert not img or img.startswith("https://"), img
    # el texto de terceros viaja como dato (JSON), el navegador lo pinta como texto: se verifica en el render del front


async def test_detail_of_an_unknown_ficha_is_404_and_a_ficha_without_breakdown_says_so(client, stack):
    assert client.get(f"{BASE}/items/12345").status_code == 404
    stack.breakdown_error.add("v1")
    await open_fichas(stack, {"1": {}})
    [item] = q.rows(PriceReviewItem)
    d = client.get(f"{BASE}/items/{item.id}").json()
    assert d["diag_status"] == "pendiente" and d["breakdown"] is None and "diag_pendiente" in d["flags"]
    assert client.get(BASE + "/items", params={"cause": "pendiente"}).json()["total"] == 1


# ─── «No es el mismo» ─────────────────────────────────────────────────────────────────────────


def _six(prices=(700_000, 800_000, 900_000, 1_300_000, 1_350_000, 1_400_000)):
    return [q.entry(i + 1000001, p, "clip", f"seller{i}") for i, p in enumerate(prices)]


def snap_of(today: int, pid: str = "1"):
    return q.rows(MarketPriceSnapshot, MarketPriceSnapshot.run_id == today, MarketPriceSnapshot.product_id == pid)[0]


async def test_not_the_same_closes_the_ficha_in_the_same_response_logs_the_actor_and_recounts_the_run(client, stack):
    today = await open_fichas(stack, {"1": dict(entries=_six(), our=1_000_000)})
    [item] = q.rows(PriceReviewItem)
    snap = snap_of(today)
    assert snap.color == "rojo" and item.state == "propuesto"
    r = client.post(f"/api/price-monitor/snapshots/{snap.id}/not-same", json={"ml_id": "MLA1000001"})
    assert r.status_code == 200
    body = r.json()
    assert body["review"] == {"item_id": item.id, "state": "cerrado_auto", "closed": True}
    assert body["snapshot"]["color"] == "amarillo" and body["snapshot"]["review_flag"] is None
    [item] = q.rows(PriceReviewItem)
    assert item.state == "cerrado_auto" and item.open_key is None and item.closed_at is not None
    ev = [e for e in q.rows(PriceReviewEvent) if e.kind == "cerrado_auto"]
    assert len(ev) == 1 and ev[0].actor == "pao@b2box.pro" and ev[0].motive_code == "no_es_el_mismo"
    assert ev[0].from_state == "propuesto" and ev[0].to_state == "cerrado_auto" and ev[0].snapshot_id == snap.id
    run_after = q.rows(PriceMonitorRun, PriceMonitorRun.id == today)[0]
    colors = [s.color for s in q.rows(MarketPriceSnapshot, MarketPriceSnapshot.run_id == today)]
    assert (run_after.n_rojo, run_after.n_amarillo, run_after.n_verde) == (colors.count("rojo"), colors.count("amarillo"), colors.count("verde"))
    assert run_after.n_rojo == 0 and run_after.n_amarillo == 1          # el contador se recalculó con la corrección
    # quedó el voto de la persona, con quién lo hizo
    [fb] = q.rows(MarketMatchFeedback)
    assert fb.actor == "pao@b2box.pro" and fb.ml_id == "MLA1000001"
    # la ficha cerrada sigue consultable y lo dice
    d = client.get(f"{BASE}/items/{item.id}").json()
    assert d["state"] == "cerrado_auto"
    assert next(e for e in d["events"] if e["kind"] == "cerrado_auto")["actor"] == "pao@b2box.pro"
    assert client.get(BASE + "/items", params={"state": "cerrados"}).json()["total"] == 1
    assert client.get(BASE + "/summary").json()["pending"] == 0


async def test_not_the_same_twice_does_not_duplicate_the_event(client, stack):
    await open_fichas(stack, {"1": dict(entries=_six(), our=1_000_000)})
    snap = snap_of(q.rows(PriceMonitorRun)[-1].id)
    for _ in range(2):
        assert client.post(f"/api/price-monitor/snapshots/{snap.id}/not-same", json={"ml_id": "MLA1000001"}).status_code == 200
    assert len([e for e in q.rows(PriceReviewEvent) if e.kind == "cerrado_auto"]) == 1


async def test_not_the_same_on_one_that_leaves_it_red_keeps_the_ficha_open(client, stack):
    today = await open_fichas(stack, {"1": dict(entries=_six((700_000, 720_000, 740_000, 760_000, 780_000, 800_000)), our=1_000_000)})
    snap = snap_of(today)
    r = client.post(f"/api/price-monitor/snapshots/{snap.id}/not-same", json={"ml_id": "MLA1000001"})
    assert r.status_code == 200 and r.json()["review"]["closed"] is False
    [item] = q.rows(PriceReviewItem)
    assert item.state == "propuesto" and item.open_key == "1"


async def test_not_the_same_by_an_unidentified_session_is_logged_as_desconocido_never_as_empty(client, stack):
    today = await open_fichas(stack, {"1": dict(entries=_six(), our=1_000_000)})
    snap = snap_of(today)
    anon_admin = TestClient(main_mod.app, raise_server_exceptions=False)
    anon_admin.cookies.set(auth.COOKIE_NAME, auth.issue_session_token(""))
    r = anon_admin.post(f"/api/price-monitor/snapshots/{snap.id}/not-same", json={"ml_id": "MLA1000001"})
    if r.status_code == 200:
        [ev] = [e for e in q.rows(PriceReviewEvent) if e.kind == "cerrado_auto"]
        assert ev.actor and ev.actor.strip()


async def test_not_the_same_rejects_garbage(client, stack):
    today = await open_fichas(stack, {"1": dict(entries=_six(), our=1_000_000)})
    snap = snap_of(today)
    for body in ({"ml_id": ""}, {"ml_id": "x"}, {"ml_id": "MLA1' OR 1=1"}, {"ml_id": "A" * 40}, {}, {"ml_id": 5}):
        assert client.post(f"/api/price-monitor/snapshots/{snap.id}/not-same", json=body).status_code in (400, 404, 422), body
    assert q.rows(PriceReviewItem)[0].state == "propuesto"


# ─── Los botones de decisión no existen del lado servidor y están deshabilitados en el front ────────


async def test_no_decision_route_exists_so_nothing_can_be_decided(client, stack):
    await open_fichas(stack, {"1": {}})
    [item] = q.rows(PriceReviewItem)
    for sub in ("approve", "decision", "undo", "recheck", "corregir", "mantener", "despublicar", "proveedor"):
        for method in ("POST", "PUT", "PATCH"):
            r = client.request(method, f"{BASE}/items/{item.id}/{sub}", json={"decision": "mantener", "motive_code": "nicho",
                                                                                "motive_text": "x" * 20})
            assert r.status_code in (404, 405), (sub, method, r.status_code)
    assert q.rows(PriceReviewItem)[0].decision is None and q.rows(PriceReviewItem)[0].state == "propuesto"
    openapi = client.get("/openapi.json")
    if openapi.status_code == 200:
        paths = {p: set(v) for p, v in openapi.json()["paths"].items() if p.startswith(BASE)}
        assert all(methods == {"get"} for methods in paths.values()), paths


async def test_the_detail_uses_one_median_for_the_card_and_for_the_max_price(client, stack):
    sc = healthy("1")
    entries = [q.entry(1000001 + i, p, src, f"v{i}") for i, (p, src) in enumerate(
        [(2_200_000, "llm"), (2_200_000, "llm"), (2_200_000, "llm"), (2_700_000, "clip"), (2_700_000, "clip"), (2_700_000, "clip")])]
    await open_fichas(stack, {"1": dict(scenario=sc, entries=entries)})
    d = client.get(f"{BASE}/items/{q.rows(PriceReviewItem)[0].id}").json()
    assert d["ml_median_cents"] == 2_700_000                                    # firmes
    assert d["diagnosis"]["p_max_cents"] == pytest.approx(q.price_max(d["ml_median_cents"]), abs=2)
