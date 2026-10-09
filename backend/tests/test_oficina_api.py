"""Buscador de la oficina, lado Hugo: auth, saneo hostil, idempotencia y cola priorizada.

La Mac es una máquina que no controlamos y lo que manda viene de la página de ML (texto de terceros):
Hugo vuelve a sanear TODO. Acá se le pega a los dos endpoints como lo haría un cliente hostil.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import auth, runtime  # noqa: E402
from app import main as main_mod  # noqa: E402
from app.api import oficina_routes  # noqa: E402
from app.clock import utcnow  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.models import MarketPriceSnapshot, MlWebResult, Setting  # noqa: E402
from app.db.session import engine, init_db  # noqa: E402
from app.pricing import market_query, oficina_ml  # noqa: E402
from tests.test_price_monitor_routes import _use_settings  # noqa: E402

KEY = "oficina-key-" + "0123456789abcdef" * 2
H = {"x-oficina-key": KEY}


def _iso(delta: timedelta = timedelta(0)) -> str:
    return (utcnow() + delta).replace(microsecond=0).isoformat() + "Z"


@pytest.fixture
def api(monkeypatch):
    init_db()
    with Session(engine) as s:
        for model in (MlWebResult, MarketPriceSnapshot):
            for row in s.exec(select(model)).all():
                s.delete(row)
        for key in ("pm_ml_query_variants", "pm_ml_web_max_results"):
            row = s.get(Setting, key)
            if row is not None:
                s.delete(row)
        s.commit()
    runtime.invalidate()
    _use_settings(monkeypatch)
    settings = Settings(vendure_api_url="https://example.invalid/admin-api", oficina_search_key=KEY)
    monkeypatch.setattr(oficina_ml, "get_settings", lambda: settings)
    oficina_routes.reset_limits()
    yield TestClient(main_mod.app)                      # sin cookies, sin lifespan
    oficina_routes.reset_limits()
    runtime.invalidate()


def _snap(pid: str, status: str = "no_data", *, name: str | None = "Organizador cocina", enabled: bool = True,
          origin: str | None = None, snap_id: int | None = None, run_id: int = 1) -> None:
    with Session(engine) as s:
        s.add(MarketPriceSnapshot(id=snap_id, run_id=run_id, product_id=pid, ml_status=status, product_name=name,
                                  product_enabled=enabled, match_origin=origin))
        s.commit()


def _result(pid: str, ago: timedelta = timedelta(hours=1), status: str = "ok", n: int = 1) -> None:
    cands = [oficina_ml.candidate_to_wire(oficina_ml.sanitize_candidate(_card(f"MLA{i + 1}"))) for i in range(n)]
    with Session(engine) as s:
        s.add(MlWebResult(product_id=pid, query="q", fetched_at=(utcnow() - ago).replace(microsecond=0),
                          candidates=json.dumps(cands), n_candidates=n, status=status))
        s.commit()


def _card(ref: str = "MLA123456", **over) -> dict:
    card = {"id": ref, "name": "Organizador de cocina", "image_urls": ["https://http2.mlstatic.com/D_NQ_NP_1-F.jpg"],
            "permalink": f"https://articulo.mercadolibre.com.ar/MLA-{ref[3:]}-organizador", "domain_id": "MLA-KITCHEN",
            "price_cents": 2_500_000, "currency": "ARS", "brand": "Marca", "seller": "Tienda Uno", "sold_quantity": 500,
            "catalog_id": ""}
    card.update(over)
    return card


def _post(api: TestClient, results: list, headers: dict | None = None):
    # A mano y con ensure_ascii: el cliente de test no sabe mandar un surrogate suelto ni un NaN, y un
    # atacante sí.
    body = json.dumps({"results": results}, ensure_ascii=True)
    return api.post("/api/oficina/ml-results", content=body,
                    headers={**(H if headers is None else headers), "content-type": "application/json"})


def _res(pid: str = "1", candidates: list | None = None, **over) -> dict:
    res = {"product_id": pid, "query": "organizador cocina", "fetched_at": _iso(-timedelta(minutes=5)),
           "status": "ok", "reason": "", "candidates": [_card()] if candidates is None else candidates}
    res.update(over)
    return res


def _rows() -> list[MlWebResult]:
    with Session(engine) as s:
        return list(s.exec(select(MlWebResult).order_by(MlWebResult.id)))


# ─── autenticación ──────────────────────────────────────────────────────────


def test_without_the_variable_the_endpoints_do_not_exist(api, monkeypatch):
    monkeypatch.setattr(oficina_ml, "get_settings",
                        lambda: Settings(vendure_api_url="https://example.invalid/x", oficina_search_key=""))
    for headers in ({}, H):
        assert api.get("/api/oficina/ml-queue", headers=headers).status_code == 404
        assert api.post("/api/oficina/ml-results", json={"results": []}, headers=headers).status_code == 404
    # ni siquiera una validación de parámetros delata que existen
    assert api.get("/api/oficina/ml-queue?limit=abc", headers=H).status_code == 404


@pytest.mark.parametrize("weak", ["corta", "replace-me", "x" * 23, "changeme", "a" * 40, "ab" * 30, "0123456789a" * 4,
                                  "abcdefghijk" * 4])             # el largo solo no alcanza: 11 caracteres distintos
def test_a_weak_key_counts_as_not_configured(api, monkeypatch, weak):
    monkeypatch.setattr(oficina_ml, "get_settings",
                        lambda: Settings(vendure_api_url="https://example.invalid/x", oficina_search_key=weak))
    assert api.get("/api/oficina/ml-queue", headers={"x-oficina-key": weak}).status_code == 404


def test_it_needs_the_exact_key_in_the_header(api):
    assert api.get("/api/oficina/ml-queue").status_code == 401
    assert api.get("/api/oficina/ml-queue", headers={"x-oficina-key": ""}).status_code == 401
    assert api.get("/api/oficina/ml-queue", headers={"x-oficina-key": KEY + "x"}).status_code == 401
    assert api.get("/api/oficina/ml-queue", headers={"x-oficina-key": KEY[:-1]}).status_code == 401
    assert api.get("/api/oficina/ml-queue", headers={"x-oficina-key": KEY.upper()}).status_code == 401
    assert api.post("/api/oficina/ml-results", json={"results": []}).status_code == 401
    assert api.get("/api/oficina/ml-queue", headers=H).status_code == 200
    # la key por query string o por otro header no vale
    assert api.get(f"/api/oficina/ml-queue?key={KEY}").status_code == 401
    assert api.get("/api/oficina/ml-queue", headers={"x-api-key": KEY}).status_code == 401


def test_the_dashboard_cookie_neither_opens_nor_is_needed(api):
    api.cookies.set(auth.COOKIE_NAME, auth.issue_session_token("admin"))
    assert api.get("/api/oficina/ml-queue").status_code == 401           # la sesión del dashboard no abre esto
    api.cookies.clear()
    assert api.get("/api/oficina/ml-queue", headers=H).status_code == 200   # y sin cookie, con la key, entra
    # el resto de /api sigue exigiendo la sesión
    assert api.get("/api/price-monitor/summary").status_code == 401


def test_the_key_is_compared_in_constant_time(api, monkeypatch):
    import hmac

    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(oficina_routes.hmac, "compare_digest", lambda a, b: calls.append((len(a), len(b))) or real(a, b))
    api.get("/api/oficina/ml-queue", headers={"x-oficina-key": "a"})
    api.get("/api/oficina/ml-queue", headers={"x-oficina-key": "a" * 5000})
    api.get("/api/oficina/ml-queue", headers=H)
    assert calls == [(32, 32)] * 3                  # siempre dos digests del mismo largo


def test_too_many_wrong_keys_lock_the_ip_but_never_the_right_key(api, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(oficina_routes, "_now", lambda: clock["t"])
    for _ in range(oficina_routes.FAIL_MAX):
        assert api.get("/api/oficina/ml-queue", headers={"x-oficina-key": "mala"}).status_code in (401,)
    r = api.get("/api/oficina/ml-queue", headers={"x-oficina-key": "mala"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    # el bloqueo frena fallos: detrás de un CDN esa IP puede ser un borde compartido, y la key buena tiene que seguir entrando
    assert api.get("/api/oficina/ml-queue", headers=H).status_code == 200
    clock["t"] += oficina_routes.LOCK_S + 1
    assert api.get("/api/oficina/ml-queue", headers={"x-oficina-key": "mala"}).status_code == 401


def test_cleaning_the_lock_table_never_overwrites_the_configured_key(api, monkeypatch):
    """Regresión (seguridad): la limpieza reusaba el nombre `key` y pisaba la key configurada con una IP vencida; con más de
    2.000 bloqueos, presentar esa IP como key daba 200."""
    clock = {"t": 10_000.0}
    monkeypatch.setattr(oficina_routes, "_now", lambda: clock["t"])
    for i in range(oficina_routes._MAX_TRACKED_IPS + 5):
        oficina_routes._locked_until[f"9.9.{i // 250}.{i % 250}"] = clock["t"] - 1          # todos vencidos
    assert api.get("/api/oficina/ml-queue", headers={"x-oficina-key": "9.9.8.4"}).status_code == 401
    assert api.get("/api/oficina/ml-queue", headers={"x-oficina-key": "9.9.8.4"}).status_code == 401
    assert api.get("/api/oficina/ml-queue", headers=H).status_code == 200
    assert len(oficina_routes._locked_until) <= 1                                           # y la limpieza sí limpió
    # lo mismo con la tabla de fallos y la de pedidos
    for i in range(oficina_routes._MAX_TRACKED_IPS + 5):
        oficina_routes._fails[f"8.8.{i // 250}.{i % 250}"].append(clock["t"] - 10_000)
        oficina_routes._hits[f"7.7.{i // 250}.{i % 250}"].append(clock["t"] - 10_000)
    assert api.get("/api/oficina/ml-queue", headers={"x-oficina-key": "8.8.8.4"}).status_code == 401
    assert api.get("/api/oficina/ml-queue", headers=H).status_code == 200


def test_every_route_of_the_router_is_behind_the_guard():
    """La auth es del router, no de cada ruta: una ruta nueva no se puede olvidar de protegerla."""
    assert any(d.dependency is oficina_routes.guard for d in oficina_routes.router.dependencies)
    assert {r.path for r in oficina_routes.router.routes} == {"/api/oficina/ml-queue", "/api/oficina/ml-results"}


def test_simple_rate_limit_per_ip(api, monkeypatch):
    clock = {"t": 50.0}
    monkeypatch.setattr(oficina_routes, "_now", lambda: clock["t"])
    for _ in range(oficina_routes.RATE_MAX):
        assert api.get("/api/oficina/ml-queue", headers=H).status_code == 200
    assert api.get("/api/oficina/ml-queue", headers=H).status_code == 429
    assert api.post("/api/oficina/ml-results", json={"results": []}, headers=H).status_code == 429
    clock["t"] += oficina_routes.RATE_WINDOW_S + 1
    assert api.get("/api/oficina/ml-queue", headers=H).status_code == 200


# ─── la cola ────────────────────────────────────────────────────────────────


def _queue(api, **params) -> dict:
    r = api.get("/api/oficina/ml-queue", params=params, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_queue_is_prioritized_and_leaves_out_what_does_not_need_a_search(api):
    _snap("1")                                                 # sin idéntico, nunca buscado
    _snap("2", enabled=False)                                  # ídem, deshabilitado: va después de los habilitados
    _snap("3", "failed")                                       # la API falló: también
    _snap("4", "ok", origin="api")                             # ya tiene idéntico con precio
    _snap("5", "skipped")                                      # no se pudo evaluar (sin foto/precio): buscar no ayuda
    _snap("6")
    _result("6", timedelta(hours=2))               # ya buscado hace poco
    _snap("7")
    _result("7", timedelta(days=9))                # resultado vencido
    _snap("8", "ok", origin="oficina")
    _result("8", timedelta(days=8))   # hoy tiene precio por la oficina y vence
    _snap("9")
    _result("9", timedelta(hours=2), status="empty")  # buscado y vacío: gastado
    _snap("10")
    _result("10", timedelta(days=30), status="blocked")  # solo un bloqueo: no cuenta como resultado
    _snap("11", name=None)                                     # sin nombre no hay qué buscar
    _snap("12")
    _result("12", timedelta(days=20))             # el más viejo de los vencidos
    ids = [i["product_id"] for i in _queue(api)["items"]]
    assert ids == ["1", "3", "10", "2", "12", "7", "8"]
    # los 4, 5, 6, 9 y 11 no están
    assert not {"4", "5", "6", "9", "11"} & set(ids)


def test_the_queue_asks_again_one_day_before_a_result_expires(api):
    _snap("1")
    _result("1", timedelta(days=5, hours=20))        # 5,8 días con TTL de 7: todavía no
    _snap("2")
    _result("2", timedelta(days=6, hours=2))         # 6,1 días: se renueva ya, para que la corrida de la noche lo encuentre fresco
    assert [i["product_id"] for i in _queue(api)["items"]] == ["2"]


def test_only_the_latest_snapshot_of_a_product_counts(api):
    _snap("1", "no_data", snap_id=1, run_id=1)
    _snap("1", "ok", origin="api", snap_id=2, run_id=2)        # lo resolvió la API después
    _snap("2", "ok", origin="api", snap_id=3, run_id=1)
    _snap("2", "no_data", snap_id=4, run_id=2)                 # al revés: ahora no tiene
    assert [i["product_id"] for i in _queue(api)["items"]] == ["2"]


def test_queue_items_carry_only_the_product_id_and_the_queries(api):
    _snap("7", name="Organizador Doble Ajustable 3 Niveles 40x30 Blanco")
    with Session(engine) as s:
        s.get(MarketPriceSnapshot, 1).our_price_cents = 12_345
        s.get(MarketPriceSnapshot, 1).product_code = "BX999"
        s.get(MarketPriceSnapshot, 1).ml_error = "secreto interno"
        s.commit()
    body = _queue(api)
    assert set(body) == {"items", "max_results", "ttl_days"}
    [item] = body["items"]
    assert set(item) == {"product_id", "queries"}
    assert item["queries"] == market_query.query_variants("Organizador Doble Ajustable 3 Niveles 40x30 Blanco", 3)
    text = json.dumps(body)
    assert "12345" not in text and "BX999" not in text and "secreto" not in text
    assert body["max_results"] == 8 and body["ttl_days"] == 7


def test_the_queries_follow_pm_ml_query_variants(api):
    _snap("7", name="Organizador Doble Ajustable 3 Niveles 40x30 Blanco")
    runtime.set_value("pm_ml_query_variants", 1)
    [item] = _queue(api)["items"]
    assert [q for q in item["queries"]] == market_query.query_variants("Organizador Doble Ajustable 3 Niveles 40x30 Blanco", 1)
    assert len(item["queries"]) == 2                           # el título y, de respaldo, las 4 primeras palabras
    runtime.set_value("pm_ml_query_variants", 2)
    assert len(_queue(api)["items"][0]["queries"]) == 2


def test_the_limit_is_respected_and_bounded(api):
    for i in range(1, 8):
        _snap(str(i))
    assert [i["product_id"] for i in _queue(api, limit=3)["items"]] == ["1", "2", "3"]
    assert len(_queue(api)["items"]) == 7
    for bad in (0, -1, oficina_ml.MAX_QUEUE + 1, "x"):
        assert api.get("/api/oficina/ml-queue", params={"limit": bad}, headers=H).status_code == 422


# ─── guardar resultados ─────────────────────────────────────────────────────


def test_a_good_result_is_stored_with_its_sanitized_candidates(api):
    _snap("1")
    r = _post(api, [_res("1", [_card("MLA111"), _card("MLA222", name="Otro organizador", price_cents=1_900_000)])])
    assert r.status_code == 200 and r.json() == {"received": 1, "stored": 1, "duplicates": 0, "rejected": [],
                                                 "rejected_total": 0}
    [row] = _rows()
    assert (row.product_id, row.status, row.origin, row.n_candidates, row.query) == ("1", "ok", "oficina", 2, "organizador cocina")
    stored = json.loads(row.candidates)
    assert [c["id"] for c in stored] == ["MLA111", "MLA222"] and stored[1]["price_cents"] == 1_900_000


def test_posting_the_same_batch_twice_changes_nothing(api):
    _snap("1")
    _snap("2")
    batch = [_res("1"), _res("2", [_card("MLA9")])]
    assert _post(api, batch).json()["stored"] == 2
    again = _post(api, batch).json()
    assert (again["stored"], again["duplicates"]) == (0, 2)
    assert len(_rows()) == 2
    # un resultado nuevo del mismo producto (otro fetched_at) sí suma
    assert _post(api, [_res("1", fetched_at=_iso(-timedelta(minutes=1)))]).json()["stored"] == 1
    assert len(_rows()) == 3
    # y repetido dentro del mismo lote: uno solo
    twin = _res("2", fetched_at=_iso(-timedelta(minutes=2)))
    assert _post(api, [twin, dict(twin)]).json()["stored"] == 1


def test_a_lost_race_on_the_unique_index_counts_as_a_duplicate_and_the_rest_is_stored(api):
    """Otro lote se coló entre el chequeo de repetidos y el guardado: lo repetido se cuenta, lo nuevo entra."""
    _snap("1")
    same = _iso(-timedelta(minutes=9))
    assert _post(api, [_res("1", fetched_at=same)]).json()["stored"] == 1
    now = utcnow()
    a = oficina_ml.clean_result(_res("1", fetched_at=same), now=now, max_candidates=8)
    b = oficina_ml.clean_result(_res("1", fetched_at=_iso(-timedelta(minutes=3))), now=now, max_candidates=8)
    report = oficina_ml.IngestReport()
    with Session(engine) as s:
        oficina_ml._insert(s, [a, b], report)
    assert (report.stored, report.duplicates) == (1, 1) and len(_rows()) == 2


def test_only_the_last_results_of_each_product_are_kept(api):
    _snap("1")
    for minutes in range(1, oficina_ml.KEEP_RESULTS_PER_PRODUCT + 4):
        _post(api, [_res("1", fetched_at=_iso(-timedelta(minutes=minutes)))])
    assert len(_rows()) == oficina_ml.KEEP_RESULTS_PER_PRODUCT


def test_candidates_are_capped_at_pm_ml_web_max_results_and_deduplicated(api):
    _snap("1")
    runtime.set_value("pm_ml_web_max_results", 3)
    many = [_card(f"MLA{100 + i}") for i in range(20)] + [_card("MLA100")]
    _post(api, [_res("1", many)])
    assert [c["id"] for c in json.loads(_rows()[0].candidates)] == ["MLA100", "MLA101", "MLA102"]
    runtime.set_value("pm_ml_web_max_results", 1)
    _post(api, [_res("1", [_card("MLA7"), _card("MLA7")], fetched_at=_iso(-timedelta(minutes=1)))])
    assert [c["id"] for c in json.loads(_rows()[-1].candidates)] == ["MLA7"]


@pytest.mark.parametrize("bad_id", [
    "MLA١٢٣", "MLA123/../admin", "mla123", "MLA", "MLA12 3", "MLA123\n", "MLA" + "9" * 40, "../../etc/passwd",
    "MLAU", "MLB123", 123, None, ["MLA1"], {"a": 1}, "", " MLA123",
])
def test_candidates_with_strange_ids_are_dropped(api, bad_id):
    _snap("1")
    r = _post(api, [_res("1", [_card("MLA1", id=bad_id), _card("MLA555")])])
    assert r.status_code == 200
    assert [c["id"] for c in json.loads(_rows()[0].candidates)] == ["MLA555"]


def test_ids_of_user_products_and_catalog_are_kept(api):
    _snap("1")
    _post(api, [_res("1", [_card("MLAU2999", catalog_id="MLA777"), _card("MLA8", catalog_id="MLA٣"),
                           _card("MLA9", catalog_id="evil")])])
    got = {c["id"]: c for c in json.loads(_rows()[0].candidates)}
    assert got["MLAU2999"]["catalog_id"] == "MLA777" and got["MLA8"]["catalog_id"] == "" and got["MLA9"]["catalog_id"] == ""


@pytest.mark.parametrize("link", [
    "https://evil.com/MLA-1", "http://articulo.mercadolibre.com.ar/MLA-1", "javascript:alert(1)",
    "https://mercadolibre.com.ar@evil.com/x", "https://evilmercadolibre.com.ar/x", "https://mercadolibre.com.ar.evil.com/x",
    "https://www.mercadolibre.com.ar\\@evil.com", "https://articulo.mercadolibre.com.ar:8443/x", "//evil.com/x",
    "https://click1.mercadolibre.com.ar/mclics/clicks/external/MLA/count?a=1", "data:text/html,x", 5, None, ["x"],
    "https://articulo.mercadolibre.com.ar/ MLA-1", "",
])
def test_permalinks_to_other_hosts_are_replaced_by_the_canonical_one(api, link):
    _snap("1")
    _post(api, [_res("1", [_card("MLA321", permalink=link)])])
    [c] = json.loads(_rows()[0].candidates)
    assert c["permalink"] == "https://articulo.mercadolibre.com.ar/MLA-321"


def test_a_good_permalink_of_ml_is_kept(api):
    _snap("1")
    link = "https://articulo.mercadolibre.com.ar/MLA-321-organizador-_JM"
    _post(api, [_res("1", [_card("MLA321", permalink=link)])])
    assert json.loads(_rows()[0].candidates)[0]["permalink"] == link


@pytest.mark.parametrize("image, expected", [
    ("https://http2.mlstatic.com/D_NQ_NP_1-F.jpg", ["https://http2.mlstatic.com/D_NQ_NP_1-F.jpg"]),
    ("http://http2.mlstatic.com/D_NQ_NP_1-F.jpg", ["https://http2.mlstatic.com/D_NQ_NP_1-F.jpg"]),
    ("https://evil.com/mlstatic.com/a.jpg", []), ("https://mlstatic.com.evil.com/a.jpg", []),
    ("file:///etc/passwd", []), ("https://169.254.169.254/latest", []), ("javascript:1", []), (7, []), (None, []),
])
def test_photos_only_from_mlstatic(api, image, expected):
    _snap("1")
    _post(api, [_res("1", [_card("MLA5", image_urls=[image])])])
    assert json.loads(_rows()[0].candidates)[0]["image_urls"] == expected


def test_image_urls_that_is_not_a_list_is_ignored(api):
    _snap("1")
    _post(api, [_res("1", [_card("MLA5", image_urls="https://http2.mlstatic.com/a.jpg"), _card("MLA6", image_urls={"a": 1})])])
    assert [c["image_urls"] for c in json.loads(_rows()[0].candidates)] == [[], []]


@pytest.mark.parametrize("price", [10**15, 10**11, 10**30, -5, 0, 0.5, float("nan"), float("inf"), True, "2500", None, [1], {"a": 1}])
def test_absurd_prices_are_dropped_but_the_listing_stays(api, price):
    _snap("1")
    r = _post(api, [_res("1", [_card("MLA5", price_cents=price)])])               # json.dumps manda NaN / Infinity tal cual
    assert r.status_code == 200
    [c] = json.loads(_rows()[0].candidates)
    assert c["price_cents"] is None and c["currency"] is None and c["id"] == "MLA5"


def test_a_sane_price_and_a_currency_that_does_not_parse(api):
    _snap("1")
    _post(api, [_res("1", [_card("MLA5", price_cents=10**11 - 1), _card("MLA6", currency="<script>"),
                           _card("MLA7", currency="usd"), _card("MLA8", price_cents=1234.0)])])
    got = {c["id"]: c for c in json.loads(_rows()[0].candidates)}
    assert got["MLA5"]["price_cents"] == 10**11 - 1
    assert got["MLA6"]["price_cents"] is None             # una moneda que no se entiende no puede contar como pesos
    assert (got["MLA7"]["currency"], got["MLA7"]["price_cents"]) == ("USD", 2_500_000)
    assert got["MLA8"]["price_cents"] == 1234


@pytest.mark.parametrize("raw, expected", [
    ("Organizador\ncocina\r\n\ttapa", "Organizador cocina tapa"),
    ("Hola\x00mundo\x1b[31m rojo\x07", "Holamundo[31m rojo"),
    ("Evil‮txt.exe‬ título", "Eviltxt.exe título"),
    ("zero​width﻿join", "zerowidthjoin"),
    ("línea otra más", "línea otra más"),
    ("{startHighlight}Taza{endHighlight} 500 ml", "Taza 500 ml"),
    ("Baño   con ñandú 🔥", "Baño con ñandú 🔥"),
    ("\ud800 suelto", "suelto"),
])
def test_titles_are_one_clean_line(api, raw, expected):
    _snap("1")
    _post(api, [_res("1", [_card("MLA5", name=raw)])])
    assert json.loads(_rows()[0].candidates)[0]["name"] == expected


def test_titles_brands_and_sellers_are_capped_and_empty_titles_dropped(api):
    _snap("1")
    _post(api, [_res("1", [_card("MLA5", name="x" * 10_000, brand="b" * 500, seller="s" * 500),
                           _card("MLA6", name=" \n\t "), _card("MLA7", name=None), _card("MLA8", name=123)])])
    [c] = json.loads(_rows()[0].candidates)
    assert (len(c["name"]), len(c["brand"]), len(c["seller"])) == (200, 40, 60)


@pytest.mark.parametrize("sold", [-1, 10**12, 1.5, True, "5", None])
def test_sold_quantity_that_makes_no_sense_is_dropped(api, sold):
    _snap("1")
    _post(api, [_res("1", [_card("MLA5", sold_quantity=sold)])])
    assert json.loads(_rows()[0].candidates)[0]["sold_quantity"] is None


def test_candidates_that_are_not_objects_or_not_a_list(api):
    _snap("1")
    _snap("2")
    _post(api, [_res("1", [1, "MLA1", None, [], _card("MLA3")]), _res("2", "MLA1")])
    rows = {r.product_id: r for r in _rows()}
    assert [c["id"] for c in json.loads(rows["1"].candidates)] == ["MLA3"]
    assert rows["2"].status == "empty" and rows["2"].n_candidates == 0


def test_an_ok_with_nothing_valid_is_stored_as_empty(api):
    _snap("1")
    _post(api, [_res("1", [_card("MLA١")])])
    [row] = _rows()
    assert row.status == "empty" and row.n_candidates == 0 and "saneo" in row.reason


@pytest.mark.parametrize("status", ["empty", "blocked", "error"])
def test_only_ok_keeps_candidates(api, status):
    _snap("1")
    _post(api, [_res("1", [_card("MLA5")], status=status, reason="ML pidió verificación")])
    [row] = _rows()
    assert row.status == status and row.candidates == "[]" and row.n_candidates == 0


@pytest.mark.parametrize("patch, why", [
    ({"status": "listo"}, "status"), ({"status": None}, "status"), ({"product_id": "../x"}, "product_id"),
    ({"product_id": "1;DROP"}, "product_id"), ({"product_id": ""}, "product_id"), ({"product_id": 1}, "product_id"),
    ({"product_id": "1" * 65}, "product_id"), ({"fetched_at": "mañana"}, "fetched_at"),
    ({"fetched_at": _iso(timedelta(hours=2))}, "futuro"), ({"fetched_at": None}, "fetched_at"),
    ({"fetched_at": "1999-01-01T00:00:00Z"}, "fetched_at"), ({"query": ""}, "consulta"), ({"query": " \n "}, "consulta"),
    ({"query": None}, "consulta"),
])
def test_a_bad_result_is_rejected_alone_and_the_good_one_still_goes_in(api, patch, why):
    _snap("1")
    _snap("2")
    r = _post(api, [_res("1", **patch), _res("2")])
    body = r.json()
    assert r.status_code == 200 and body["stored"] == 1 and len(body["rejected"]) == 1
    assert why in body["rejected"][0]["reason"]
    assert [row.product_id for row in _rows()] == ["2"]


def test_unknown_products_are_rejected(api):
    _snap("1")
    body = _post(api, [_res("999")]).json()
    assert body["stored"] == 0 and body["rejected"] == [{"product_id": "999", "reason": "producto desconocido"}]


def test_the_rejection_report_does_not_echo_raw_input(api):
    body = _post(api, [_res("<script>alert(1)</script>\n"), {"product_id": "x" * 500}, 5, None]).json()
    assert body["rejected_total"] == 4
    for item in body["rejected"]:
        assert len(item["product_id"]) <= 64 and "\n" not in item["product_id"]


def test_a_future_fetched_at_cannot_keep_a_result_fresh_forever(api):
    _snap("1")
    r = _post(api, [_res("1", fetched_at=_iso(timedelta(days=3650)))])
    assert r.json()["stored"] == 0 and _rows() == []
    # un desvío de reloj chico es normal
    assert _post(api, [_res("1", fetched_at=_iso(timedelta(minutes=2)))]).json()["stored"] == 1


def test_fetched_at_accepts_offsets_and_is_stored_in_utc(api):
    _snap("1")
    when = (utcnow() - timedelta(hours=3)).replace(microsecond=0)         # el instante, en UTC
    art = (when - timedelta(hours=3)).isoformat() + "-03:00"              # el mismo instante, escrito en hora argentina
    _post(api, [_res("1", fetched_at=art)])
    assert _rows()[0].fetched_at == when


# ─── el body ────────────────────────────────────────────────────────────────


def test_malformed_bodies_are_400_not_500(api):
    for content in (b"", b"no es json", b"[]", b'{"results": "x"}', b'{"results": {"a": 1}}', b"{", b"null", b'"x"'):
        r = api.post("/api/oficina/ml-results", content=content, headers={**H, "content-type": "application/json"})
        assert r.status_code == 400, content
    # anidado a propósito: no revienta el intérprete
    deep = b"[" * 200_000
    assert api.post("/api/oficina/ml-results", content=deep, headers=H).status_code == 400
    assert api.post("/api/oficina/ml-results", content=b'{"results": [' + b"1" * 6000 + b"]}", headers=H).status_code in (200, 400)


def test_a_huge_body_is_413_with_and_without_content_length(api):
    big = json.dumps({"results": [_res("1", [_card("MLA5", name="x" * 200)] * 3000)]}).encode()
    assert len(big) > oficina_ml.MAX_BODY_BYTES
    r = api.post("/api/oficina/ml-results", content=big, headers={**H, "content-type": "application/json"})
    assert r.status_code == 413

    def chunks():                                   # sin Content-Length (chunked): se corta igual
        for i in range(0, len(big), 4096):
            yield big[i:i + 4096]

    r = api.post("/api/oficina/ml-results", content=chunks(), headers={**H, "content-type": "application/json"})
    assert r.status_code == 413
    assert _rows() == []


def test_too_many_products_in_a_batch_is_413(api):
    _snap("1")
    too_many = [_res("1", fetched_at=_iso(-timedelta(minutes=i + 1))) for i in range(oficina_ml.MAX_PRODUCTS_PER_BATCH + 1)]
    assert _post(api, too_many).status_code == 413
    assert _post(api, too_many[:oficina_ml.MAX_PRODUCTS_PER_BATCH]).json()["stored"] == oficina_ml.MAX_PRODUCTS_PER_BATCH


def test_a_batch_just_under_the_cap_goes_through(api):
    _snap("1")
    cands = [_card(f"MLA{i}", name="y" * 200) for i in range(1, 9)]
    assert _post(api, [_res("1", cands)]).status_code == 200


# ─── lo que se guarda no abre nada hacia afuera ─────────────────────────────


def test_the_sanitizer_never_returns_a_field_outside_the_whitelists(api):
    import itertools

    hostile = ["https://evil.com/x", "javascript:alert(1)", "MLA١", "\x00", "<img src=x onerror=1>", 10**30, -1, None, [], {}]
    for link, image, ident in itertools.product(hostile, hostile, hostile):
        cand = oficina_ml.sanitize_candidate({"id": ident, "name": "t", "permalink": link, "image_urls": [image]})
        if cand is None:
            continue
        assert cand.permalink.startswith(("https://articulo.mercadolibre.com.ar/", "https://www.mercadolibre.com.ar/"))
        assert all(u.startswith("https://") and "mlstatic.com" in u.split("/")[2] for u in cand.image_urls)
        assert oficina_ml.valid_ref(cand.id) and cand.origin == "oficina"


def test_the_ip_tables_do_not_grow_without_bound(api, monkeypatch):
    clock = {"t": 10.0}
    monkeypatch.setattr(oficina_routes, "_now", lambda: clock["t"])
    for i in range(oficina_routes._MAX_TRACKED_IPS + 50):
        oficina_routes._fails[f"10.0.{i // 250}.{i % 250}"].append(clock["t"])
        oficina_routes._locked_until[f"10.1.{i // 250}.{i % 250}"] = clock["t"] + 1
    clock["t"] += oficina_routes.FAIL_WINDOW_S + oficina_routes.LOCK_S + 5
    api.get("/api/oficina/ml-queue", headers={"x-oficina-key": "mala"})            # una pasada con todo vencido
    assert len(oficina_routes._fails) <= 2 and len(oficina_routes._locked_until) <= 1


# ─── largo de las URLs y surrogates sueltos ─────────────────────────────────


def test_urls_have_a_length_cap_for_every_use_not_only_the_oficina():
    from app.pricing import market_ml

    ok = "https://articulo.mercadolibre.com.ar/MLA-1-" + "a" * 100
    assert market_ml.safe_permalink(ok) == ok
    edge = "https://articulo.mercadolibre.com.ar/" + "a" * (market_ml.MAX_URL_CHARS - len("https://articulo.mercadolibre.com.ar/"))
    assert len(edge) == market_ml.MAX_URL_CHARS and market_ml.safe_permalink(edge) == edge
    assert market_ml.safe_permalink(edge + "a") == ""
    assert market_ml.safe_permalink("https://articulo.mercadolibre.com.ar/" + "a" * 400_000) == ""
    img = "https://http2.mlstatic.com/D_NQ_NP_1-F.jpg"
    assert market_ml.safe_image_url(img) == img
    assert market_ml.safe_image_url("https://http2.mlstatic.com/" + "a" * market_ml.MAX_URL_CHARS) is None
    assert market_ml.safe_image_url("https://http2.mlstatic.com/" + "a" * 400_000) is None


def test_long_urls_from_the_mac_become_the_canonical_link_or_no_photo(api):
    _snap("1")
    _post(api, [_res("1", [_card("MLA777", permalink="https://articulo.mercadolibre.com.ar/" + "a" * 600,
                                 image_urls=["https://http2.mlstatic.com/" + "b" * 600, "https://http2.mlstatic.com/ok.jpg"])])])
    [cand] = json.loads(_rows()[0].candidates)
    assert cand["permalink"] == "https://articulo.mercadolibre.com.ar/MLA-777"
    assert cand["image_urls"] == ["https://http2.mlstatic.com/ok.jpg"]


@pytest.mark.parametrize("field", ["permalink", "image_urls"])
def test_a_lone_surrogate_in_a_url_drops_that_url_and_not_the_batch(api, field):
    """Antes: UnicodeEncodeError al guardar → 500 de todo el lote."""
    _snap("1")
    _snap("2")
    bad = "https://articulo.mercadolibre.com.ar/MLA-1-\ud800x" if field == "permalink" else ["https://http2.mlstatic.com/\ud800.jpg"]
    r = _post(api, [_res("1", [_card("MLA777", **{field: bad})]), _res("2", [_card("MLA888")])])
    assert r.status_code == 200 and r.json()["stored"] == 2
    rows = {row.product_id: row for row in _rows()}
    [cand] = json.loads(rows["1"].candidates)
    if field == "permalink":
        assert cand["permalink"] == "https://articulo.mercadolibre.com.ar/MLA-777"
    else:
        assert cand["image_urls"] == [] and cand["permalink"].startswith("https://articulo.mercadolibre.com.ar/MLA-777-")
    assert json.loads(rows["2"].candidates)[0]["id"] == "MLA888"


def test_an_unexpected_error_on_one_result_rejects_only_that_result(api, monkeypatch):
    _snap("1")
    _snap("2")
    real = oficina_ml.sanitize_candidates

    def boom(raw, limit):
        if raw and raw[0].get("id") == "MLA666":
            raise RuntimeError("bug")
        return real(raw, limit)

    monkeypatch.setattr(oficina_ml, "sanitize_candidates", boom)
    r = _post(api, [_res("1", [_card("MLA666")]), _res("2", [_card("MLA888")])])
    body = r.json()
    assert r.status_code == 200 and body["stored"] == 1 and body["rejected"] == [{"product_id": "1", "reason": "no se pudo procesar"}]


def test_a_key_needs_enough_distinct_characters_but_the_shared_check_is_unchanged(api, monkeypatch):
    good = "0123456789ab" * 3                                      # 12 distintos
    monkeypatch.setattr(oficina_ml, "get_settings",
                        lambda: Settings(vendure_api_url="https://example.invalid/x", oficina_search_key=good))
    assert api.get("/api/oficina/ml-queue", headers={"x-oficina-key": good}).status_code == 200
    from app import security
    assert security.weak_key_reason("a" * 40) is None                       # las API keys de producción de siempre no cambian
    assert "distintos" in security.weak_key_reason("a" * 40, min_distinct=12)
    assert security.weak_key_reason(KEY, min_distinct=oficina_ml.MIN_KEY_DISTINCT_CHARS) is None
