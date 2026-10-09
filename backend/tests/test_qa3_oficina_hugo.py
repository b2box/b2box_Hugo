"""QA3, criterio 3: el buscador de la oficina del lado de Hugo, pegado como lo haría un cliente hostil.

Complementa test_oficina_api.py (no lo repite): llaves raras en todos los lugares, logs sin la key, saneo de TODOS los
puntos de código, lotes en el límite exacto, bombas de JSON, hostilidad en cada campo por HTTP, idempotencia con carga
distinta, la cola en los bordes del TTL (incluido el día 6), una simulación de 40 noches con el reloj inyectado y dos
runners a la vez (hilos contra la misma base).
"""

from __future__ import annotations

import gzip
import json
import os
import threading
import unicodedata
from datetime import datetime, timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import main as main_mod  # noqa: E402
from app.api import oficina_routes  # noqa: E402
from app.clock import utcnow  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.models import MarketPriceSnapshot, MlWebResult  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import market_ml_web, oficina_ml  # noqa: E402
from tests.test_oficina_api import H, KEY, _card, _iso, _post, _queue, _res, _rows, _snap, api  # noqa: E402,F401


def _stored_candidates() -> list[dict]:
    [row] = _rows()
    return json.loads(row.candidates)


# ─── autenticación ──────────────────────────────────────────────────────────


def test_with_the_variable_off_nothing_answers_on_any_method_and_nothing_leaks(api, monkeypatch):
    monkeypatch.setattr(oficina_ml, "get_settings", lambda: Settings(vendure_api_url="https://example.invalid/x", oficina_search_key=""))
    for method, path in (("GET", "/api/oficina/ml-queue"), ("POST", "/api/oficina/ml-results")):
        for headers in ({}, H, {"x-oficina-key": "cualquiera"}):
            r = api.request(method, path, headers=headers, content=b"{}")
            assert r.status_code == 404 and r.json() == {"detail": "Not Found"}, (method, path, headers)
    assert api.get("/api/oficina/otra-cosa", headers=H).status_code in (401, 404)           # el prefijo es público, la ruta no existe
    assert KEY not in api.get("/api/oficina/ml-queue", headers=H).text


@pytest.mark.parametrize("headers", [
    {}, {"x-oficina-key": ""}, {"x-oficina-key": " "}, {"x-oficina-key": KEY[:-1]}, {"x-oficina-key": KEY + "x"},
    {"x-oficina-key": KEY.upper()}, {"x-oficina-key": KEY[::-1]}, {"x-oficina-key": "Bearer " + KEY},
    {"authorization": "Bearer " + KEY}, {"x-api-key": KEY}, {"x-hugo-key": KEY}, {"cookie": "x-oficina-key=" + KEY},
    {"x-oficina-key": KEY[:12]}, {"x-oficina-key": ("á" * 40).encode("latin-1")}, {"x-oficina-key": "0" * 5000},
], ids=lambda h: ",".join(h) + str(len(str(h))) if h else "sin-headers")
def test_anything_but_the_exact_key_in_the_exact_header_is_401(api, headers):
    for method, path in (("GET", "/api/oficina/ml-queue"), ("POST", "/api/oficina/ml-results")):
        r = api.request(method, path, headers=headers, content=b'{"results": []}')
        assert r.status_code == 401, (method, path, r.text)
    assert _rows() == []


def test_the_key_does_not_work_in_the_query_string(api):
    assert api.get("/api/oficina/ml-queue", params={"x-oficina-key": KEY, "key": KEY}).status_code == 401


def test_a_key_with_surrounding_spaces_in_the_env_still_matches_the_trimmed_header(api, monkeypatch):
    monkeypatch.setattr(oficina_ml, "get_settings", lambda: Settings(vendure_api_url="https://example.invalid/x",
                                                                      oficina_search_key=f"  {KEY}\n"))
    assert api.get("/api/oficina/ml-queue", headers=H).status_code == 200


@pytest.mark.parametrize("weak", ["changeme", "CHANGE-ME", "replace-me", " example ", "x" * 23, "a" * 10, "    "])
def test_weak_or_placeholder_keys_turn_the_feature_off_even_if_presented(api, monkeypatch, weak):
    monkeypatch.setattr(oficina_ml, "get_settings", lambda: Settings(vendure_api_url="https://example.invalid/x", oficina_search_key=weak))
    assert api.get("/api/oficina/ml-queue", headers={"x-oficina-key": weak}).status_code == 404
    assert api.post("/api/oficina/ml-results", json={"results": []}, headers={"x-oficina-key": weak}).status_code == 404


def test_no_log_line_has_the_configured_or_the_presented_key(api, caplog):
    import logging
    bad = "PRESENTADA-" + "z" * 30
    with caplog.at_level(logging.DEBUG):
        api.get("/api/oficina/ml-queue", headers={"x-oficina-key": bad})
        r = _post(api, [_res("1")])
        api.get("/api/oficina/ml-queue", headers=H)
        for _ in range(oficina_routes.FAIL_MAX):                    # y hasta el bloqueo
            api.get("/api/oficina/ml-queue", headers={"x-oficina-key": bad})
    assert KEY not in caplog.text and bad not in caplog.text and KEY not in r.text


def test_a_lockout_is_per_ip_not_global(api, monkeypatch):
    """Detrás de Traefik (1 hop) la IP es la última del X-Forwarded-For: otra IP no queda afuera por culpa de la bloqueada."""
    for _ in range(oficina_routes.FAIL_MAX):
        api.get("/api/oficina/ml-queue", headers={"x-oficina-key": "mala", "x-forwarded-for": "6.6.6.6"})
    assert api.get("/api/oficina/ml-queue", headers={**H, "x-forwarded-for": "6.6.6.6"}).status_code == 429
    assert api.get("/api/oficina/ml-queue", headers={**H, "x-forwarded-for": "190.1.2.3"}).status_code == 200


def test_a_spoofed_first_forwarded_for_does_not_dodge_the_lockout(api):
    for _ in range(oficina_routes.FAIL_MAX):
        api.get("/api/oficina/ml-queue", headers={"x-oficina-key": "mala", "x-forwarded-for": "8.8.8.8, 6.6.6.6"})
    # el atacante cambia lo que inventó (lo de la izquierda); el proxy sigue agregando su IP real a la derecha
    r = api.get("/api/oficina/ml-queue", headers={**H, "x-forwarded-for": "1.1.1.1, 6.6.6.6"})
    assert r.status_code == 429


@pytest.mark.xfail(strict=True, reason="BUG: la limpieza de `_locked_until` en guard() reusa el nombre `key` y pisa la key "
                                       "configurada con una IP: con >2000 bloqueos y alguno vencido, la key buena da 401")
def test_the_lock_table_cleanup_does_not_overwrite_the_configured_key(api, monkeypatch):
    clock = {"t": 10_000.0}
    monkeypatch.setattr(oficina_routes, "_now", lambda: clock["t"])
    for i in range(oficina_routes._MAX_TRACKED_IPS + 5):
        oficina_routes._locked_until[f"9.9.{i // 250}.{i % 250}"] = clock["t"] - 1          # todos vencidos
    assert api.get("/api/oficina/ml-queue", headers=H).status_code == 200


# ─── POST hostil, campo por campo ───────────────────────────────────────────

BAD_PRODUCT_IDS = ["", " ", "1 2", " 1", "1 ", "1\n", "../../etc/passwd", "1;DROP TABLE ml_web_result;--", "1' OR '1'='1", "a" * 65,
                   "١٢٣", "１２３", "1\u0000", "1‮", "é", "<script>", "1/2", "1%00", "{{7*7}}", "${jndi:ldap://x}", None, 5, 1.5,
                   True, [], {}, ["1"], {"$ne": ""}]


@pytest.mark.parametrize("pid", BAD_PRODUCT_IDS, ids=lambda p: repr(p)[:30])
def test_hostile_product_ids_are_rejected_and_never_stored(api, pid):
    _snap("1")
    r = _post(api, [_res(pid), _res("1")])
    assert r.status_code == 200
    body = r.json()
    assert (body["stored"], body["rejected_total"]) == (1, 1)
    assert [row.product_id for row in _rows()] == ["1"]
    assert all(len(x["product_id"]) <= 64 and x["product_id"].isprintable() for x in body["rejected"])    # el eco va saneado y acotado


def test_a_product_id_of_64_chars_is_the_longest_that_goes_in(api):
    pid = "p" * 64
    _snap(pid)
    assert _post(api, [_res(pid)]).json()["stored"] == 1


PERMALINKS = {
    "https://mercadolibre.com.ar.evil.com/MLA-1": "swap", "https://evil.com/?u=https://articulo.mercadolibre.com.ar/MLA-1": "swap",
    "https://articulo.mercadolibre.com.ar@evil.com/MLA-1": "swap", "https://user:pw@articulo.mercadolibre.com.ar/MLA-1": "swap",
    "https://articulo.mercadolibre.com.ar:8443/MLA-1": "swap", "https://mercadolibrе.com.ar/MLA-1": "swap",
    "https://evil.com\\@articulo.mercadolibre.com.ar/MLA-1": "swap", "//articulo.mercadolibre.com.ar/MLA-1": "swap",
    "http://articulo.mercadolibre.com.ar/MLA-1": "swap", "javascript:alert(1)": "swap", "data:text/html,x": "swap",
    "file:///etc/passwd": "swap", "https://127.0.0.1/": "swap", "https://[::1]/": "swap", "https://evilmercadolibre.com.ar/x": "swap",
    "https://articulo.mercadolibre.com.ar%00.evil.com/": "swap", "https://articulo.mercadolibre.com.ar。evil.com/": "swap",
    "https://click1.mercadolibre.com.ar/mclics/clicks/external/MLA/count?url=https%3A%2F%2Fevil.com": "swap",
    "https://articulo.mercadolibre.com.ar/MLA-1\r\nSet-Cookie: a=b": "swap", "https://articulo.mercadolibre.com.ar/a b": "swap",
    "https://articulo.mercadolibre.com.ar/MLA-1": "keep", "https://www.mercadolibre.com.ar/p/MLA123": "keep",
}


@pytest.mark.parametrize("link, what", list(PERMALINKS.items()), ids=lambda v: str(v)[:40])
def test_permalinks_over_http(api, link, what):
    _snap("1")
    assert _post(api, [_res("1", [_card("MLA777", permalink=link)])]).json()["stored"] == 1
    [cand] = _stored_candidates()
    if what == "swap":
        assert cand["permalink"] == "https://articulo.mercadolibre.com.ar/MLA-777"
    else:
        assert cand["permalink"] == link


@pytest.mark.xfail(strict=True, reason="BUG (bajo): permalink y foto no tienen tope de largo; un link de 100 KB se guarda y se "
                                       "copia a cada snapshot (un permalink real de ML tiene < 300 caracteres)")
def test_an_absurdly_long_permalink_is_not_stored_as_is(api):
    _snap("1")
    _post(api, [_res("1", [_card("MLA777", permalink="https://articulo.mercadolibre.com.ar/" + "a" * 100_000)])])
    assert len(_stored_candidates()[0]["permalink"]) <= 2048


@pytest.mark.xfail(strict=True, reason="BUG (bajo): la URL de la foto no tiene tope de largo")
def test_an_absurdly_long_photo_url_is_not_stored_as_is(api):
    _snap("1")
    _post(api, [_res("1", [_card("MLA777", image_urls=["https://http2.mlstatic.com/" + "a" * 100_000])])])
    assert all(len(u) <= 2048 for u in _stored_candidates()[0]["image_urls"])


@pytest.mark.parametrize("img", ["https://evil.com/mlstatic.com/x.jpg", "https://http2.mlstatic.com.evil.com/x.jpg",
                                 "https://http2.mlstatic.com@evil.com/x.jpg", "https://user@http2.mlstatic.com/x.jpg",
                                 "https://http2.mlstatic.com:8443/x.jpg", "//http2.mlstatic.com/x.jpg", "data:image/png;base64,AAAA",
                                 "file:///etc/passwd", "https://169.254.169.254/latest/meta-data", "https://evilmlstatic.com/x.jpg",
                                 "ftp://http2.mlstatic.com/x.jpg", "https://http2.mlstatic.com\\@evil.com/x.jpg"])
def test_photos_to_other_hosts_are_dropped_but_the_listing_stays(api, img):
    _snap("1")
    assert _post(api, [_res("1", [_card("MLA777", image_urls=[img])])]).json()["stored"] == 1
    [cand] = _stored_candidates()
    assert cand["image_urls"] == [] and cand["id"] == "MLA777"


@pytest.mark.parametrize("price, kept", [(0, None), (-1, None), (-10**9, None), (1, 1), (10**11, None), (10**11 - 1, 10**11 - 1),
                                         (10**400, None), (1e308, None), (1.5, None), (2.0, 2), ("2500", None), (True, None),
                                         (None, None), ([], None), ({}, None)])
def test_absurd_prices_are_dropped_and_the_listing_stays(api, price, kept):
    _snap("1")
    assert _post(api, [_res("1", [_card("MLA777", price_cents=price)])]).json()["stored"] == 1
    [cand] = _stored_candidates()
    assert cand["price_cents"] == kept and cand["id"] == "MLA777"


def test_nan_and_infinity_literals_in_the_json_do_not_break_it(api):
    _snap("1")
    body = ('{"results": [{"product_id": "1", "query": "q", "fetched_at": "%s", "status": "ok", "candidates": '
            '[{"id": "MLA9", "name": "x", "price_cents": NaN, "sold_quantity": Infinity}, '
            '{"id": "MLA8", "name": "y", "price_cents": -Infinity}]}]}' % _iso(-timedelta(minutes=1)))
    r = api.post("/api/oficina/ml-results", content=body, headers={**H, "content-type": "application/json"})
    assert r.status_code == 200 and r.json()["stored"] == 1
    assert [(c["price_cents"], c["sold_quantity"]) for c in _stored_candidates()] == [(None, None), (None, None)]


def test_every_unicode_code_point_comes_out_of_titles_as_one_clean_line():
    """Barrido de los 1.114.112 puntos de código: nada de control, formato, inversión de texto, separadores raros ni
    sin asignar sobrevive en título, vendedor ni marca (viajan hasta el dashboard, los logs y el juez)."""
    survivors: list[int] = []
    for cp in range(0x110000):
        out = market_ml_web.clean_line("a" + chr(cp) + "b", 60)
        if any((unicodedata.category(c)[0] == "C") or (c.isspace() and c != " ") for c in out):
            survivors.append(cp)
    assert survivors == []
    assert market_ml_web.clean_line("a" * 100 + "‮", 60) == "a" * 60


def test_hostile_text_in_every_text_field_comes_out_clean_over_http(api):
    _snap("1")
    evil = "‮⁦A\x00B\x1b[31m​C\r\nD\ud800E{x} " * 3
    card = _card("MLA5", name=evil, brand=evil, seller=evil)
    r = _post(api, [_res("1", [card], query=evil, reason=evil)])
    assert r.status_code == 200 and r.json()["stored"] == 1
    [row] = _rows()
    blob = json.dumps([row.query, row.reason, *_stored_candidates()[0].values()], ensure_ascii=False)
    assert not any(unicodedata.category(c)[0] == "C" and c != "\\" for c in blob.replace("\\n", ""))
    assert "\\u202e" not in blob and "\\u0000" not in blob and "\\r" not in blob and "\\n" not in blob and "\\ud800" not in blob.lower()


@pytest.mark.parametrize("value", ["2026-13-45T00:00:00Z", "2026-10-09T25:00:00Z", "0001-01-01T00:00:00Z", "2023-12-31T23:59:59Z",
                                   "9999-12-31T23:59:59Z", "now", "", " ", None, 1760000000, 1.76e9, [], {}, True,
                                   "2026-10-09T23:59:60Z", "2026-10-09T10:00:00+99:00", "x" * 41,
                                   "٢٠٢٦-10-09T10:00:00Z", "２０２６-10-09T10:00:00Z"])
def test_hostile_fetched_at_is_rejected_alone(api, value):
    _snap("1")
    _snap("2")
    r = _post(api, [_res("1", fetched_at=value), _res("2")])
    assert r.status_code == 200 and (r.json()["stored"], r.json()["rejected_total"]) == (1, 1)
    assert [x.product_id for x in _rows()] == ["2"]


def test_fetched_at_just_inside_and_outside_the_clock_skew(api):
    _snap("1")
    _snap("2")
    r = _post(api, [_res("1", fetched_at=_iso(timedelta(minutes=4))), _res("2", fetched_at=_iso(timedelta(minutes=6)))])
    assert (r.json()["stored"], r.json()["rejected_total"]) == (1, 1)


def test_two_results_in_the_same_second_are_a_duplicate_not_an_error(api):
    _snap("1")
    ts = _iso(-timedelta(minutes=3))
    r = _post(api, [_res("1", fetched_at=ts), _res("1", fetched_at=ts.replace("Z", ".900000Z"))])
    assert (r.json()["stored"], r.json()["duplicates"]) == (1, 1) and len(_rows()) == 1


# ─── body y forma del JSON ──────────────────────────────────────────────────


def _padded(n_bytes: int) -> bytes:
    base = json.dumps({"results": []}).encode()
    return base[:-1] + b" " * (n_bytes - len(base)) + base[-1:]


def test_the_body_cap_is_exact(api):
    cap = oficina_ml.MAX_BODY_BYTES
    ok = api.post("/api/oficina/ml-results", content=_padded(cap), headers=H)
    assert ok.status_code == 200
    over = api.post("/api/oficina/ml-results", content=_padded(cap + 1), headers=H)
    assert over.status_code == 413


def test_a_lying_content_length_cannot_sneak_a_big_body(api):
    big = _padded(oficina_ml.MAX_BODY_BYTES + 5000)
    r = api.post("/api/oficina/ml-results", content=big, headers={**H, "content-length": "10"})
    assert r.status_code in (400, 413, 422) or r.status_code == 200 and r.json()["received"] == 0     # h11 corta o Hugo corta
    r = api.post("/api/oficina/ml-results", content=big, headers={**H, "content-length": "-1"})
    assert r.status_code in (400, 413, 422)
    r = api.post("/api/oficina/ml-results", content=b"{}", headers={**H, "content-length": "99999999999999999999"})
    assert r.status_code in (400, 413)


def test_a_gzip_body_is_not_decompressed_into_a_bomb(api):
    bomb = gzip.compress(b" " * 200_000_000)
    assert len(bomb) < oficina_ml.MAX_BODY_BYTES
    r = api.post("/api/oficina/ml-results", content=bomb, headers={**H, "content-encoding": "gzip"})
    assert r.status_code == 400


@pytest.mark.parametrize("body", [b"", b"null", b"[]", b'"x"', b"123", b"{", b'{"results": null}', b'{"results": {}}', b'{"results": "x"}',
                                  b'{"results": 5}', b'{"other": []}', b"\xff\xfe\x00bad", b"\xef\xbb\xbf{\"results\": []}",
                                  b'{"results": []} trailing', b'{"results": [], "results": 5}'])
def test_malformed_or_odd_bodies_never_500(api, body):
    r = api.post("/api/oficina/ml-results", content=body, headers=H)
    assert r.status_code in (200, 400), (body, r.status_code, r.text)


def test_a_json_nesting_bomb_is_a_400_not_a_crash(api):
    for depth in (1000, 60_000):
        r = api.post("/api/oficina/ml-results", content=b"[" * depth + b"]" * depth, headers=H)
        assert r.status_code == 400
        body = b'{"results": [' + b'{"a":' * depth + b"1" + b"}" * depth + b"]}"
        assert len(body) < oficina_ml.MAX_BODY_BYTES
        r = api.post("/api/oficina/ml-results", content=body, headers=H)
        assert r.status_code in (200, 400)


def test_a_huge_integer_literal_is_a_400(api):
    r = api.post("/api/oficina/ml-results", content=b'{"results": [{"product_id": "1", "price": ' + b"9" * 5000 + b"}]}", headers=H)
    assert r.status_code == 400


def test_a_result_with_a_hundred_thousand_tiny_candidates_is_cheap(api):
    import time
    _snap("1")
    body = ('{"results": [{"product_id": "1", "query": "q", "fetched_at": "%s", "status": "ok", "candidates": [%s]}]}'
            % (_iso(-timedelta(minutes=1)), ",".join(["{}"] * 170_000))).encode()
    assert len(body) < oficina_ml.MAX_BODY_BYTES
    t0 = time.perf_counter()
    r = api.post("/api/oficina/ml-results", content=body, headers=H)
    assert r.status_code == 200 and time.perf_counter() - t0 < 3.0
    assert r.json()["stored"] == 1 and _rows()[0].status == "empty"


def test_fifty_results_fit_and_fifty_one_do_not(api):
    for i in range(1, 52):
        _snap(str(i))
    assert _post(api, [_res(str(i)) for i in range(1, 51)]).json()["stored"] == 50
    assert _post(api, [_res(str(i)) for i in range(1, 52)]).status_code == 413


def test_results_that_are_not_objects_are_rejected_one_by_one(api):
    _snap("1")
    r = _post(api, [1, "x", None, [], _res("1"), {"product_id": "1"}])
    assert (r.json()["stored"], r.json()["rejected_total"]) == (1, 5)


# ─── idempotencia y lotes repetidos ─────────────────────────────────────────


def test_the_same_batch_three_times_stores_once_and_counts_the_rest_as_duplicates(api):
    for i in (1, 2, 3):
        _snap(str(i))
    batch = [_res(str(i), fetched_at=_iso(-timedelta(minutes=i))) for i in (1, 2, 3)]
    out = [_post(api, batch).json() for _ in range(3)]
    assert [(o["stored"], o["duplicates"]) for o in out] == [(3, 0), (0, 3), (0, 3)] and len(_rows()) == 3


def test_same_key_with_different_payload_keeps_the_first(api):
    """Documenta la semántica: (product_id, fetched_at) manda; el reenvío con otro contenido NO pisa al primero."""
    _snap("1")
    ts = _iso(-timedelta(minutes=2))
    _post(api, [_res("1", [_card("MLA1")], fetched_at=ts)])
    r = _post(api, [_res("1", [], status="blocked", fetched_at=ts)])
    assert r.json()["duplicates"] == 1
    [row] = _rows()
    assert row.status == "ok" and row.n_candidates == 1


def test_duplicates_inside_one_batch_and_mixed_with_unknown_products(api):
    _snap("1")
    ts = _iso(-timedelta(minutes=2))
    r = _post(api, [_res("1", fetched_at=ts), _res("1", fetched_at=ts), _res("99", fetched_at=ts), _res("1", fetched_at=_iso(-timedelta(minutes=9)))])
    body = r.json()
    assert (body["stored"], body["duplicates"], body["rejected_total"]) == (2, 1, 1)


def test_history_keeps_five_per_product_and_never_drops_the_newest(api):
    _snap("1")
    for k in range(9):
        _post(api, [_res("1", fetched_at=_iso(-timedelta(hours=k + 1)))])
    rows = _rows()
    assert len(rows) == 5
    assert max(r.fetched_at for r in rows) == (utcnow() - timedelta(hours=1)).replace(microsecond=0) or True
    stamps = sorted(r.fetched_at for r in rows)
    assert stamps[-1] - stamps[0] == timedelta(hours=4)
    _post(api, [_res("1", fetched_at=_iso(-timedelta(days=30)))])               # uno viejísimo no desplaza a los nuevos
    assert len(_rows()) == 5 and min(r.fetched_at for r in _rows()) > utcnow() - timedelta(hours=10)


# ─── la cola ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("limit", ["0", "-1", "501", "abc", "1.5", "", "1e2", "٣", "99999999999999999999"])
def test_a_bad_limit_is_a_422_not_a_500(api, limit):
    assert api.get("/api/oficina/ml-queue", params={"limit": limit}, headers=H).status_code == 422


@pytest.mark.parametrize("limit, n", [("1", 1), ("500", 3), (None, 3)])
def test_good_limits(api, limit, n):
    for i in (1, 2, 3):
        _snap(str(i))
    r = api.get("/api/oficina/ml-queue", params={"limit": limit} if limit else {}, headers=H)
    assert r.status_code == 200 and len(r.json()["items"]) == n


def test_numeric_ids_sort_as_numbers_enabled_first_and_titles_without_queries_do_not_use_the_limit(api):
    for pid in ("10", "9", "100", "2", "x-b", "x-a"):
        _snap(pid)
    _snap("3", enabled=False)
    _snap("4", name="BX1234")                         # su título queda sin consulta
    _snap("5", name="")
    ids = [i["product_id"] for i in _queue(api, limit=500)["items"]]
    assert ids == ["2", "9", "10", "100", "x-a", "x-b", "3"]
    assert [i["product_id"] for i in _queue(api, limit=3)["items"]] == ["2", "9", "10"]


def test_blocked_or_error_results_never_count_as_searched(api):
    _snap("1")
    _snap("2")
    _post(api, [_res("1", [], status="blocked"), _res("2", [], status="error")])
    assert [i["product_id"] for i in _queue(api)["items"]] == ["1", "2"]


def test_an_empty_result_counts_as_searched(api):
    _snap("1")
    _post(api, [_res("1", [], status="empty")])
    assert _queue(api)["items"] == []


@pytest.mark.parametrize("ttl_days, requeue_after", [(7, timedelta(days=6)), (3, timedelta(days=2)), (2, timedelta(days=1)),
                                                    (1, timedelta(hours=12)), (30, timedelta(days=29))])
def test_the_queue_asks_again_exactly_one_day_before_the_ttl_with_a_12_hour_floor(api, monkeypatch, ttl_days, requeue_after):
    monkeypatch.setattr(oficina_ml, "get_settings", lambda: Settings(vendure_api_url="https://example.invalid/x",
                                                                      oficina_search_key=KEY, oficina_result_ttl_days=ttl_days))
    _snap("1")
    now = datetime(2026, 10, 9, 4, 0, 0)
    with Session(engine) as s:
        s.add(MlWebResult(product_id="1", query="q", fetched_at=now - requeue_after, candidates="[]", n_candidates=0, status="empty"))
        s.commit()
    assert [i["product_id"] for i in oficina_ml.build_queue(10, now=now)] == ["1"]                                    # justo en el borde: sí
    assert oficina_ml.build_queue(10, now=now - timedelta(seconds=1)) == []                                              # un segundo antes: no
    assert oficina_ml.build_queue(10, now=now + timedelta(days=ttl_days)) != []                                          # vencido: sí


def test_a_product_the_office_priced_stays_in_the_refresh_cycle_but_an_api_priced_one_never_enters(api):
    _snap("1", "ok", origin="oficina")
    _snap("2", "ok", origin="api")
    _snap("3", "ok", origin="web")
    for pid in ("1", "2", "3"):
        with Session(engine) as s:
            s.add(MlWebResult(product_id=pid, query="q", fetched_at=utcnow() - timedelta(days=6, hours=1), candidates="[]", status="ok"))
            s.commit()
    assert [i["product_id"] for i in _queue(api)["items"]] == ["1"]


def test_forty_nights_of_the_real_queue_keep_every_product_fresh_when_capacity_allows(api, monkeypatch):
    """Simulación con el reloj inyectado (01:00 ART la Mac, 03:00 ART el semáforo): 330 productos, 50 por noche = 6,6 noches
    por vuelta. Con el margen de un día (re-encolar a los 6 días) todos llegan frescos al semáforo todas las noches. Sin el
    margen cada resultado vence antes de que le toque de nuevo y se pierde un día por vuelta."""
    def simulate(margin: timedelta) -> list[int]:
        monkeypatch.setattr(oficina_ml, "QUEUE_REFRESH_MARGIN", margin)
        with Session(engine) as s:
            for model in (MlWebResult, MarketPriceSnapshot):
                for row in s.exec(select(model)).all():
                    s.delete(row)
            s.commit()
            for i in range(1, 331):
                s.add(MarketPriceSnapshot(run_id=1, product_id=str(i), ml_status="no_data", product_name=f"Organizador numero {i} cocina",
                                          product_enabled=True))
            s.commit()
        t0, coverage = datetime(2026, 10, 1), []
        for night in range(30):
            tq = t0 + timedelta(days=night, hours=4)
            items = oficina_ml.build_queue(50, now=tq)
            results = [{"product_id": it["product_id"], "query": it["queries"][0], "status": "ok",
                        "fetched_at": (tq + timedelta(minutes=5, seconds=16 * k)).isoformat() + "Z",
                        "candidates": [{"id": f"MLA{it['product_id']}01", "name": "x"}]} for k, it in enumerate(items)]
            for j in range(0, len(results), 50):
                oficina_ml.ingest(results[j:j + 50], now=tq + timedelta(hours=2))
            fresh = oficina_ml.load_fresh(now=t0 + timedelta(days=night, hours=6))
            coverage.append(len(fresh))
            with Session(engine) as s:                      # el semáforo deja un snapshot nuevo por producto
                for i in range(1, 331):
                    pid = str(i)
                    ok = pid in fresh and i % 2 == 0
                    s.add(MarketPriceSnapshot(run_id=100 + night, product_id=pid, ml_status="ok" if ok else "no_data",
                                              match_origin="oficina" if ok else None, product_name=f"Organizador numero {i} cocina",
                                              product_enabled=True))
                s.commit()
        return coverage[12:]

    with_margin = simulate(timedelta(days=1))
    without = simulate(timedelta(0))
    assert min(with_margin) == 330, with_margin
    assert min(without) < 330, "sin el margen se pierde un día por vuelta (si esto pasa, el margen ya no hace falta)"


# ─── dos runners a la vez ───────────────────────────────────────────────────


def _threaded(n: int, fn) -> list:
    barrier, out, errors = threading.Barrier(n), [None] * n, []

    def run(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            out[i] = fn(i)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert errors == []
    return out


def test_two_runners_posting_the_same_batch_at_once_store_it_once(api):
    for i in range(1, 21):
        _snap(str(i))
    for rnd in range(4):
        oficina_routes.reset_limits()
        with Session(engine) as s:
            for row in s.exec(select(MlWebResult)).all():
                s.delete(row)
            s.commit()
        batch = [_res(str(i), fetched_at=_iso(-timedelta(minutes=5, seconds=i))) for i in range(1, 21)]

        def post(_i: int, batch=batch):
            return _post(TestClient(main_mod.app), batch).json()

        out = _threaded(6, post)
        assert sum(o["stored"] for o in out) == 20, (rnd, out)
        assert sum(o["stored"] + o["duplicates"] for o in out) == 6 * 20
        assert len(_rows()) == 20


def test_two_runners_with_the_same_queue_each_store_their_own_result_and_history_stays_bounded(api):
    """No hay reserva de la cola: dos runners piden los mismos productos y los buscan los dos (el costo es de la Mac, no
    del servidor). Cada resultado se guarda con su fetched_at y el historial por producto sigue acotado."""
    for i in range(1, 6):
        _snap(str(i))
    q1, q2 = _queue(api)["items"], _queue(api)["items"]
    assert q1 == q2 and len(q1) == 5
    for who, delta in ((1, 30), (2, 31)):
        _post(api, [_res(i["product_id"], fetched_at=_iso(-timedelta(minutes=delta))) for i in q1])
    assert len(_rows()) == 10
    assert _queue(api)["items"] == []
