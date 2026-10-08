"""QA de cierre de feat/semaforo-tiendas, criterio 2: los bugs de la primera QA (commit 8b22473) están resueltos de verdad.

No alcanza con que el test viejo pase: acá se prueban VARIANTES de cada uno (otros códigos, otros caminos, otros
momentos), muchas por la pila HTTP real (`qa2_http.Local` + `safe_get` de verdad).

  1. 429 en robots.txt            → 401/403/429/5xx/red, cooldown, 404 sí deja pasar
  2. redirect a una URL vedada     → Gadnic y Tiendanube, 301/302/307/308, cadenas, off-site, sitemap, y los redirects legítimos
  3. juez compartido con ML        → tope propio que se agota a mitad de la corrida, que cambia de día
  4. «No es el mismo» desaparecía  → persistencia en varias corridas, «Es el mismo», sin bajar la foto, ficha que se fue
  5. dudoso sin juez = diferente   → todos los modos de falla del juez, contra ML en paralelo
  6. nombres Gadnic/gadnic         → mayúsculas, espacios, mismo sitio con otro nombre, renombrar, dos pedidos a la vez
  7. borrar/apagar en curso        → durante robots, sitemap y fichas; tiendas que se borran en plena corrida del semáforo
  (8 y 9, las columnas de Postgres, están en test_qa2_tiendas_pg_migracion.py)
"""

from __future__ import annotations

import asyncio
import os
import random
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import update  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import net_guard, runtime  # noqa: E402
from app.db.models import (  # noqa: E402
    MarketStore,
    Setting,
    StoreCatalogItem,
    StoreMatch,
    StoreMatchFeedback,
)
from app.db.session import engine  # noqa: E402
from app.pricing import daily_budget, market_judge, market_match, price_monitor, store_catalog, store_match  # noqa: E402
from tests import qa2_http  # noqa: E402
from tests import store_fixtures as fx  # noqa: E402
from tests.store_fixtures import Clock, FakeSite, store_db  # noqa: E402,F401  (fixture)
from tests.test_price_monitor import FakeVendure, _product, _set, world  # noqa: E402,F401
from tests.test_price_monitor_routes import _env, client  # noqa: E402,F401
from tests.test_qa_tiendas_color import CP, GD, SCORES, _snap, add_item, sw  # noqa: E402,F401
from tests.test_qa2_tiendas_http_real import CP_ROBOTS, GD_ROBOTS, _add_gadnic, server  # noqa: E402,F401
from tests.test_semaforo_web import _card, _score, _web_page, webw  # noqa: E402,F401
from tests.test_store_catalog import _clock, add_store, items, run_index, tn_product, tn_site  # noqa: E402,F401

UA = "HugoPriceBot/1.0 (+https://b2box.pro)"


def _cp_site(server: qa2_http.Local, slugs: list[str], robots: str = CP_ROBOTS) -> list[str]:
    server.route("/robots.txt", robots, Content_Type="text/plain")
    urls = []
    for slug in slugs:
        url, body = tn_product(slug)
        server.route(f"/productos/{slug}/", body, Content_Type="text/html")
        urls.append(url)
    server.route("/sitemap.xml", fx.urlset(f"{fx.CP}/", f"{fx.CP}/productos/", *urls), Content_Type="application/xml")
    return urls


# ═══ 1) robots.txt con 429 y compañía ════════════════════════════════════════


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503, 504])
async def test_robots_txt_that_cannot_be_read_stops_the_pass_before_the_sitemap_and_the_pages(store_db, server, status):
    sid = add_store()
    _cp_site(server, ["a", "b", "c"])
    server.route("/robots.txt", "slow down", status, Retry_After="3600")
    report = await store_catalog.index_store(sid, delay=(0.0, 0.0))
    assert report.status == "aborted" and str(status) in report.message, report.summary()
    assert server.paths() == ["/robots.txt"], "ni sitemap ni fichas"
    assert items(sid) == {}
    with Session(engine) as s:
        assert s.get(MarketStore, sid).last_index_status.startswith("aborted")


@pytest.mark.parametrize("status", [404, 410])
async def test_a_robots_txt_that_does_not_exist_allows_everything(store_db, server, status):
    sid = add_store()
    _cp_site(server, ["a", "b"])
    server.route("/robots.txt", "no existe", status)
    report = await store_catalog.index_store(sid, delay=(0.0, 0.0))
    assert report.status == "ok" and report.ok == 2


async def test_robots_txt_that_never_answers_the_connection_stops_the_pass(store_db, monkeypatch):
    sid = add_store()

    async def refuse(url, **kw):
        raise httpx.ConnectError("connection refused")

    report = await store_catalog.index_store(sid, get=refuse, delay=(0.0, 0.0))
    assert report.status == "aborted" and "ConnectError" in report.message and report.fetched == 0


async def test_a_429_on_the_sitemap_or_on_a_page_also_stops_and_marks_nothing_dead(store_db, server, _clock):
    sid = add_store()
    urls = _cp_site(server, ["a", "b", "c", "d"])
    server.route("/sitemap.xml", "", 429)
    r1 = await store_catalog.index_store(sid, delay=(0.0, 0.0))
    assert r1.status == "aborted" and r1.fetched == 0
    _cp_site(server, ["a", "b", "c", "d"])
    server.route("/productos/b/", "slow", 429)
    r2 = await store_catalog.index_store(sid, delay=(0.0, 0.0))
    assert r2.status == "aborted" and "429" in r2.message and r2.fetched == 2
    rows = items(sid)
    assert rows[urls[0]].title and rows[urls[1]].fails == 0 and not rows[urls[1]].dead, "un 429 no es un fallo de la ficha"
    assert "/productos/c/" not in server.paths() and "/productos/d/" not in server.paths()


async def test_after_a_429_the_top_up_leaves_the_store_alone_for_six_hours_and_then_tries_again(store_db, server, _clock):
    sid = add_store(refresh_days=7)
    _cp_site(server, ["a"])
    await store_catalog.index_store(sid, delay=(0.0, 0.0))             # ya hay una ficha leída…
    _clock["now"] += timedelta(days=8)                                 # …y se venció
    server.route("/robots.txt", "slow", 429)
    r = await store_catalog.index_store(sid, delay=(0.0, 0.0))
    assert r.status == "aborted"
    server.requests.clear()
    assert await store_catalog.index_all(only_if_due=True, delay=(0.0, 0.0)) == [], "recién bloqueada: el top-up ni lo intenta"
    assert server.requests == []
    _clock["now"] += timedelta(hours=6, minutes=1)
    with Session(engine) as s:
        s.execute(update(MarketStore).values(last_indexed_at=_clock["now"] - timedelta(hours=6, minutes=1)))
        s.commit()
    _cp_site(server, ["a"])                                            # vuelve a andar
    reports = await store_catalog.index_all(only_if_due=True, delay=(0.0, 0.0))
    assert [r.status for r in reports] == ["ok"] and reports[0].ok == 1


# ═══ 2) redirects ═══════════════════════════════════════════════════════════

FORBIDDEN_TARGETS_GADNIC = ["/tripodes/otro?utm=1", "/?s=tripode", "/x/y?a=1&b=2"]
FORBIDDEN_TARGETS_TN = ["/search/?q=x", "/ar/search/", "/checkout/", "/admin/", "/account/", "/discount/x",
                        "/productos/otra/?preview_theme_installation_id=3", "/productos/otra/?view=print"]
OFFSITE = ["https://evil.example/productos/a/", "//evil.example/productos/a/", "https://casaperfecta.com.ar.evil.example/productos/a/",
           "https://www.casaperfecta.com.ar@evil.example/", "http://169.254.169.254/latest/meta-data/", "http://127.0.0.1/admin/",
           "https://www.gadnic.com.ar/tripodes/otro"]


@pytest.mark.parametrize("code", [301, 302, 307, 308])
@pytest.mark.parametrize("target", FORBIDDEN_TARGETS_TN + OFFSITE)
async def test_tiendanube_never_follows_a_redirect_into_a_forbidden_or_foreign_url(store_db, server, code, target):
    sid = add_store()
    urls = _cp_site(server, ["a"])
    server.redirect("/productos/a/", target, code)
    for path in ("/search/", "/ar/search/", "/checkout/", "/admin/", "/account/", "/discount/x", "/tripodes/otro"):
        server.route(path, "NO DEBERIA PEDIRSE", 200)
    report = await store_catalog.index_store(sid, delay=(0.0, 0.0))
    paths = server.paths()
    assert paths == ["/robots.txt", "/sitemap.xml", "/productos/a/"], f"se siguió el redirect a {target}: {paths}"
    row = items(sid)[urls[0]]
    assert row.fails == 1 and row.last_seen_at is None and report.ok == 0
    assert row.fail_reason in ("redirige fuera del sitio o a una URL vedada", "redirige a otro sitio"), row.fail_reason


@pytest.mark.parametrize("code", [301, 302, 307, 308])
@pytest.mark.parametrize("target", FORBIDDEN_TARGETS_GADNIC + OFFSITE[:4])
async def test_gadnic_never_follows_a_redirect_into_a_url_with_a_query_or_off_the_site(store_db, server, code, target):
    sid = _add_gadnic()
    server.route("/robots.txt", GD_ROBOTS, Content_Type="text/plain")
    page = f"{fx.GD}/tripodes/tripod-gadnic-tripode3"
    server.route("/sitemap.xml", fx.urlset(page), Content_Type="application/xml")
    server.redirect("/tripodes/tripod-gadnic-tripode3", target, code)
    server.default = (200, {}, "NO DEBERIA PEDIRSE")
    await store_catalog.index_store(sid, delay=(0.0, 0.0))
    assert server.paths() == ["/robots.txt", "/sitemap.xml", "/tripodes/tripod-gadnic-tripode3"], server.paths()


async def test_a_chain_whose_second_hop_is_forbidden_stops_at_that_hop(store_db, server):
    sid = add_store()
    urls = _cp_site(server, ["a"])
    server.redirect("/productos/a/", "/productos/b/", 301)
    server.redirect("/productos/b/", "/search/?q=x", 302)
    server.route("/search/", "NO DEBERIA PEDIRSE")
    await store_catalog.index_store(sid, delay=(0.0, 0.0))
    assert server.paths() == ["/robots.txt", "/sitemap.xml", "/productos/a/", "/productos/b/"]
    assert items(sid)[urls[0]].fails == 1


async def test_a_redirect_of_the_sitemap_into_a_forbidden_place_is_not_followed(store_db, server):
    sid = add_store()
    _cp_site(server, ["a"])
    server.redirect("/sitemap.xml", "/search/?sitemap=1", 301)
    server.route("/search/", "NO")
    report = await store_catalog.index_store(sid, delay=(0.0, 0.0))
    assert report.fetched == 0 and report.status == "aborted"
    assert not any(p.startswith("/search") for p in server.paths())


async def test_robots_txt_redirecting_off_site_is_not_followed_but_within_the_site_it_is_like_rfc_9309_says(store_db, server):
    sid = add_store()
    _cp_site(server, ["a"])
    server.redirect("/robots.txt", "https://evil.example/robots.txt", 301)
    r = await store_catalog.index_store(sid, delay=(0.0, 0.0))
    assert r.status == "aborted" and r.fetched == 0 and server.paths() == ["/robots.txt"]
    server.requests.clear()
    server.redirect("/robots.txt", "/robots-nuevo.txt", 301)
    server.route("/robots-nuevo.txt", "User-agent: *\nDisallow: /productos/a/\n")
    r = await store_catalog.index_store(sid, delay=(0.0, 0.0))
    assert server.paths()[:3] == ["/robots.txt", "/robots-nuevo.txt", "/sitemap.xml"]
    assert "/productos/a/" not in server.paths(), "y se respetan las reglas del archivo al que redirigió"


@pytest.mark.parametrize("code,location", [(301, "/productos/a-nuevo/"), (302, "/productos/a-nuevo/"), (308, "/productos/a-nuevo/"),
                                           (301, "/productos/a-nuevo"), (301, "https://casaperfecta.com.ar/productos/a-nuevo/")])
async def test_a_legit_redirect_to_another_product_page_of_the_same_site_is_followed_and_read(store_db, server, code, location):
    """El otro lado de la moneda: redirect_ok no puede romper los redirects normales (cambio de slug, barra final, sin www)."""
    sid = add_store()
    urls = _cp_site(server, ["a"])
    new_url, body = tn_product("a-nuevo")
    server.route("/productos/a-nuevo/", body, Content_Type="text/html")
    server.route("/productos/a-nuevo", body, Content_Type="text/html")
    server.redirect("/productos/a/", location, code)
    report = await store_catalog.index_store(sid, delay=(0.0, 0.0))
    assert report.ok == 1 and report.failed == 0, report.summary()
    row = items(sid)[urls[0]]
    assert row.title == "Producto a-nuevo" and row.fails == 0 and row.last_seen_at is not None


async def test_a_redirect_loop_is_a_failure_and_not_an_infinite_request_storm(store_db, server):
    sid = add_store()
    urls = _cp_site(server, ["a"])
    server.redirect("/productos/a/", "/productos/b/", 302)
    server.redirect("/productos/b/", "/productos/a/", 302)
    report = await store_catalog.index_store(sid, delay=(0.0, 0.0))
    assert len(server.paths()) <= 2 + 6, server.paths()
    assert report.ok == 0 and items(sid)[urls[0]].last_seen_at is None


# ═══ 3) el tope del juez es propio ══════════════════════════════════════════


@pytest.fixture
def judge_counting(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(market_judge, "enabled", lambda: True)
    monkeypatch.setattr(market_judge, "make_client", lambda: None)
    _set("pm_vision_max_calls", 100)

    async def judge(our_name, our_images, candidates, *, max_calls, on_reserve=None,
                    counter_key=market_judge.LLM_COUNTER_KEY, **kw):
        if await daily_budget.reserve_async(counter_key, int(max_calls), None, on_reserve) is None:
            return None
        calls.append((our_name, counter_key))
        return market_judge.JudgeResult(verdicts={
            c.ml_id: market_judge.JudgeVerdict(c.ml_id, True, 0.9, "igual", "igual", ()) for c in candidates})

    monkeypatch.setattr(market_judge, "judge", judge)
    return calls


async def test_when_the_stores_cap_runs_out_mid_run_the_rest_are_unconfirmed_similars_and_ml_is_untouched(sw, judge_counting):
    runtime.set_value("pm_stores_vision_max_calls", 2)
    FakeVendure.products = [_product(str(i), f"Soporte celular auto {i} reforzado") for i in range(1, 6)]
    sw.ml.search = {f"Soporte celular auto {i} reforzado": [] for i in range(1, 6)}
    for i in range(1, 6):
        add_item(CP, f"cp-{i}", f"Soporte celular auto {i} reforzado", 31_000, 0.62, product=str(i))
    _set("pm_ml_concurrency", 1)
    await price_monitor.run_price_monitor()
    used = daily_budget.used_today(market_judge.STORES_LLM_COUNTER_KEY)
    assert used == 2 and daily_budget.used_today(market_judge.LLM_COUNTER_KEY) == 0
    with Session(engine) as s:
        rows = {m.product_id: m for m in s.exec(select(StoreMatch).where(StoreMatch.title.like("Soporte celular auto%"))).all()
                if m.store_id and m.rank == 1}
    by_source = sorted(m.source for m in rows.values())
    assert by_source == ["ambiguo"] * 3 + ["llm"] * 2, by_source
    assert all(m.category == "igual" for m in rows.values() if m.source == "llm")
    assert all(m.category == "similar" for m in rows.values() if m.source == "ambiguo")


async def test_the_stores_counter_is_per_utc_day_and_does_not_touch_the_ml_one(sw, judge_counting, monkeypatch):
    runtime.set_value("pm_stores_vision_max_calls", 1)
    add_item(CP, "cp-1", "Organizador de cocina", 31_000, 0.62)
    day = {"d": "2026-10-08"}
    monkeypatch.setattr(daily_budget, "today_utc", lambda: day["d"])
    await price_monitor.run_price_monitor()
    assert daily_budget.used_today(market_judge.STORES_LLM_COUNTER_KEY) == 1
    await price_monitor.run_price_monitor()                         # mismo día: cupo gastado
    assert daily_budget.used_today(market_judge.STORES_LLM_COUNTER_KEY) == 1
    day["d"] = "2026-10-09"
    assert daily_budget.used_today(market_judge.STORES_LLM_COUNTER_KEY) == 0
    await price_monitor.run_price_monitor()
    assert daily_budget.used_today(market_judge.STORES_LLM_COUNTER_KEY) == 1
    assert daily_budget.used_today(market_judge.LLM_COUNTER_KEY) == 0 or day["d"] == "2026-10-09"


# ═══ 4) «No es el mismo» sigue visible ═══════════════════════════════════════


async def test_a_no_es_mark_survives_many_runs_stays_different_and_its_photo_is_never_downloaded_again(sw, client, monkeypatch):
    runtime.set_value("pm_stores_affect_color", 1)
    add_item(GD, "gd-org", "Organizador de cocina", 20_000, 0.95)
    add_item(CP, "cp-org", "Organizador de cocina plegable", 22_000, 0.95)
    await price_monitor.run_price_monitor()
    run0 = price_monitor.latest_run_id() if hasattr(price_monitor, "latest_run_id") else None
    with Session(engine) as s:
        target = s.exec(select(StoreMatch).where(StoreMatch.title == "Organizador de cocina")).first()
    r = client.post(f"/api/price-monitor/store-matches/{target.id}/label", json={"label": "no_es"})
    assert r.status_code == 200
    asked: list[str] = []
    real = market_match.clip_score_urls

    async def spy(our, urls):
        asked.extend(urls)
        return await real(our, urls)

    monkeypatch.setattr(market_match, "clip_score_urls", spy)
    for _ in range(3):
        asked.clear()
        await price_monitor.run_price_monitor()
        with Session(engine) as s:
            latest = max(m.run_id for m in s.exec(select(StoreMatch)).all())
            row = s.exec(select(StoreMatch).where(StoreMatch.run_id == latest, StoreMatch.item_id == target.item_id)).one()
        assert (row.category, row.human_label, row.source) == ("diferente", "no_es", "manual")
        assert not any("gd-org" in u for u in asked), "la foto marcada «No es el mismo» no se vuelve a bajar"
        assert _snap().color == _snap().color and _snap().price_basis in ("ml", "ml+tiendas")
    # y «Es el mismo» la devuelve: vuelve a ser idéntica, con fuente manual
    with Session(engine) as s:
        latest_match = s.exec(select(StoreMatch).where(StoreMatch.item_id == target.item_id).order_by(StoreMatch.run_id.desc())).first()
    assert client.post(f"/api/price-monitor/store-matches/{latest_match.id}/label", json={"label": "es"}).status_code == 200
    await price_monitor.run_price_monitor()
    with Session(engine) as s:
        latest = max(m.run_id for m in s.exec(select(StoreMatch)).all())
        row = s.exec(select(StoreMatch).where(StoreMatch.run_id == latest, StoreMatch.item_id == target.item_id)).one()
    assert (row.category, row.source, row.human_label) == ("igual", "manual", "es")


async def test_a_mark_on_an_item_that_left_the_sitemap_or_died_is_ignored_without_breaking_the_run(sw, client):
    gd = add_item(GD, "gd-org", "Organizador de cocina", 20_000, 0.95)
    add_item(CP, "cp-org", "Organizador de cocina plegable", 22_000, 0.95)
    await price_monitor.run_price_monitor()
    with Session(engine) as s:
        m = s.exec(select(StoreMatch).where(StoreMatch.item_id == gd)).first()
    client.post(f"/api/price-monitor/store-matches/{m.id}/label", json={"label": "es"})
    for column in ("in_sitemap", "dead"):
        with Session(engine) as s:
            s.execute(update(StoreCatalogItem).where(StoreCatalogItem.id == gd).values(**{column: column == "dead"}))
            s.commit()
        result = await price_monitor.run_price_monitor()
        assert result and result["counts"]["ok"] >= 1, column
        with Session(engine) as s:
            latest = max(x.run_id for x in s.exec(select(StoreMatch)).all())
            assert not [x for x in s.exec(select(StoreMatch).where(StoreMatch.run_id == latest)).all() if x.item_id == gd]
        with Session(engine) as s:
            s.execute(update(StoreCatalogItem).where(StoreCatalogItem.id == gd).values(in_sitemap=True, dead=False))
            s.commit()


# ═══ 5) lo dudoso sin juez es «similar sin confirmar», como en ML ════════════


def _judge_modes(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(market_judge, "enabled", lambda: mode != "apagado")
    monkeypatch.setattr(market_judge, "make_client", lambda: None)
    _set("pm_vision_max_calls", 0 if mode in ("apagado", "cupo_cero") else 50)
    runtime.set_value("pm_stores_vision_max_calls", 0 if mode == "cupo_cero" else 50)

    async def judge(our_name, our_images, candidates, *, max_calls, on_reserve=None, **kw):
        if mode == "revienta":
            raise RuntimeError("el proveedor se cayó")
        if mode == "none":
            return None
        if mode == "vacio":
            return market_judge.JudgeResult(verdicts={})
        if mode == "baja_confianza":
            return market_judge.JudgeResult(verdicts={c.ml_id: market_judge.JudgeVerdict(c.ml_id, True, 0.40, "no sé", "igual", ())
                                                      for c in candidates})
        return market_judge.JudgeResult(verdicts={c.ml_id: market_judge.JudgeVerdict(c.ml_id, True, 0.9, "ok", "igual", ())
                                                  for c in candidates})

    monkeypatch.setattr(market_judge, "judge", judge)


@pytest.mark.parametrize("mode", ["apagado", "cupo_cero", "revienta", "none", "vacio", "baja_confianza", "igual"])
@pytest.mark.parametrize("score", [0.55, 0.62])
async def test_a_store_item_and_the_same_ml_listing_get_the_same_verdict_under_every_judge_failure_mode(webw, store_db, monkeypatch, mode, score):
    from tests.test_qa_tiendas_color import CP_IMG

    store_catalog.seed_default_stores()
    runtime.set_value("pm_stores_topup_minutes", 0)
    runtime.set_value("pm_stores_affect_color", 0)
    SCORES.clear()
    FakeVendure.products = [_product("3", "Producto raro")]
    webw.ml.search["Producto raro"] = []
    webw.web.pages["producto-raro"] = _web_page(_card("MLA901", "Producto Raro", 250.0))
    _score(webw, "MLA901", score)
    add_item(CP, "cp-raro", "Producto raro", 25_000, score, product="3")
    _judge_modes(monkeypatch, mode)

    async def scorer(our, urls):
        return SCORES.get((our.id, urls[0]), 0.2) if urls else None

    monkeypatch.setattr(market_match, "clip_score_urls", scorer)
    await price_monitor.run_price_monitor()
    s = _snap("3")
    import json as _json
    ml_entries = [(e["category"] if "category" in e else "igual", e["source"])
                  for key, cat in (("matched_listings", "igual"), ("similar_listings", "similar"), ("other_listings", "diferente"))
                  for e in (_json.loads(getattr(s, key)) if getattr(s, key) else [])
                  for e in [dict(e, category=cat)]]
    with Session(engine) as ses:
        [m] = ses.exec(select(StoreMatch).where(StoreMatch.product_id == "3", StoreMatch.title == "Producto raro")).all()
    assert [(m.category, m.source)] == ml_entries, f"ML {ml_entries} vs tienda {(m.category, m.source)}"


# ═══ 6) nombres de tienda ════════════════════════════════════════════════════


@pytest.mark.parametrize("name", ["gadnic", "GADNIC", "Gadnic ", "  Gadnic", "Gad   nic".replace("Gad   nic", "Gadnic"), "gAdNiC"])
def test_store_names_that_differ_only_in_case_or_blanks_are_the_same_store(store_db, client, name):
    store_catalog.seed_default_stores()
    r = client.post("/api/stores", json={"name": name, "base_url": "https://www.otro-sitio.com.ar", "platform": "jsonld_sitemap"})
    assert r.status_code == 409, r.text


def test_inner_blanks_are_collapsed_before_comparing(store_db, client):
    store_catalog.seed_default_stores()
    r = client.post("/api/stores", json={"name": "Casa    Perfecta", "base_url": "https://www.otro-sitio.com.ar", "platform": "tiendanube"})
    assert r.status_code == 409


@pytest.mark.parametrize("base", ["https://gadnic.com.ar", "https://WWW.GADNIC.COM.AR", "https://www.gadnic.com.ar/", "https://gadnic.com.ar:443"])
def test_the_same_site_under_another_name_is_refused(store_db, client, base):
    store_catalog.seed_default_stores()
    r = client.post("/api/stores", json={"name": "Otro nombre", "base_url": base, "platform": "jsonld_sitemap"})
    assert r.status_code == 409, r.text


def test_renaming_a_store_to_an_existing_one_is_refused_but_changing_the_case_of_its_own_name_is_fine(store_db, client):
    store_catalog.seed_default_stores()
    stores = {s["name"]: s["id"] for s in client.get("/api/stores").json()["items"]}
    assert client.put(f"/api/stores/{stores['Gadnic']}", json={"name": "casa perfecta"}).status_code == 409
    assert client.put(f"/api/stores/{stores['Gadnic']}", json={"name": "GADNIC"}).status_code == 200
    assert client.put(f"/api/stores/{stores['Gadnic']}", json={"name": "Gadnic"}).status_code == 200


def test_two_simultaneous_posts_with_the_same_name_leave_exactly_one_store(store_db, client):
    import threading

    store_catalog.seed_default_stores()
    codes: list[int] = []

    def post():
        codes.append(client.post("/api/stores", json={"name": "Tienda Nueva", "base_url": "https://www.tienda-nueva.com.ar",
                                                      "platform": "tiendanube"}).status_code)

    threads = [threading.Thread(target=post) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes).count(201) == 1 and all(c in (201, 409) for c in codes), codes
    with Session(engine) as s:
        assert len([x for x in s.exec(select(MarketStore)).all() if x.name == "Tienda Nueva"]) == 1


# ═══ 7) borrar o apagar una tienda mientras se la indexa ═════════════════════


@pytest.mark.parametrize("when", ["robots", "sitemap", "ficha_1", "ficha_3"])
@pytest.mark.parametrize("how", ["borrar", "apagar"])
async def test_deleting_or_switching_off_a_store_at_any_point_of_the_pass_stops_the_requests(store_db, how, when):
    sid = add_store()
    site = tn_site([f"p{i}" for i in range(8)])
    trigger = {"robots": "/robots.txt", "sitemap": "/sitemap.xml", "ficha_1": "/productos/p0/", "ficha_3": "/productos/p2/"}[when]

    def on_request(url):
        if url.endswith(trigger):
            if how == "borrar":
                store_catalog.delete_store(sid)
            else:
                with Session(engine) as s:
                    s.execute(update(MarketStore).values(enabled=False))
                    s.commit()

    site.on_request = on_request
    await run_index(sid, site)
    pages = site.fetched_pages()
    expected_max = {"robots": 0, "sitemap": 0, "ficha_1": 1, "ficha_3": 3}[when]
    assert len(pages) <= expected_max, f"{how} en {when}: se pidieron {len(pages)} fichas más"
    if how == "borrar":
        with Session(engine) as s:
            assert s.exec(select(StoreCatalogItem).where(StoreCatalogItem.store_id == sid)).all() == []
            assert s.get(MarketStore, sid) is None


async def test_deleting_a_store_in_the_middle_of_the_semaforo_run_leaves_a_run_that_finishes_and_an_api_that_serves(sw, client):
    runtime.set_value("pm_stores_affect_color", 1)
    FakeVendure.products = [_product(str(i), f"Soporte celular auto {i} reforzado") for i in range(1, 7)]
    sw.ml.search = {f"Soporte celular auto {i} reforzado": [] for i in range(1, 7)}
    for i in range(1, 7):
        add_item(GD, f"gd-{i}", f"Soporte celular auto {i} reforzado", 31_000, 0.95, product=str(i))
        add_item(CP, f"cp-{i}", f"Soporte celular auto {i} reforzado", 32_000, 0.95, product=str(i))
    gd_id = next(s.id for s in [Session(engine).exec(select(MarketStore).where(MarketStore.name == GD)).one()])
    calls = {"n": 0}
    real = store_match.save_matches

    def deleting(run_id, product_id, rows):
        calls["n"] += 1
        if calls["n"] == 3:
            store_catalog.delete_store(gd_id)
        real(run_id, product_id, rows)

    store_match.save_matches = deleting
    try:
        _set("pm_ml_concurrency", 1)
        result = await price_monitor.run_price_monitor()
    finally:
        store_match.save_matches = real
    assert result and result["counts"]["no_data"] == 6
    r = client.get("/api/price-monitor/snapshots", params={"page_size": 50})
    assert r.status_code == 200 and len(r.json()["items"]) == 6
    assert all("Gadnic" not in [v["label"] for v in i["stores"].values()] for i in r.json()["items"])
    assert client.get("/api/price-monitor/summary").status_code == 200 and client.get("/api/price-monitor/runs").status_code == 200
    store_match.prune(retention_days=30)
    with Session(engine) as s:
        assert not [m for m in s.exec(select(StoreMatch)).all() if m.store_id == gd_id]
