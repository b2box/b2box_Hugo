"""QA de cierre de feat/semaforo-tiendas, criterio 5: `safe_get` sigue igual para los otros usos de Hugo
(verify, /app/lookup, source_check, competitor_check, judge_images).

Comparación DIFERENCIAL con sockets reales: la versión de origin/main (77645f9, guardada en
`golden/net_guard_origin_main_77645f9.py.txt`) y la de esta rama se llaman con los mismos pedidos contra un servidor HTTP
local (`qa2_http.Local`) y tienen que devolver exactamente lo mismo — estado, cuerpo, URL final, headers que viajan, la
excepción — cuando NO se pasa `max_bytes` (que es como lo llaman todos los usos viejos). Lo nuevo (`max_bytes`,
`redirect_ok`) solo se activa si el llamador lo pide.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import httpx  # noqa: E402
import pytest  # noqa: E402

from app import net_guard as new_guard  # noqa: E402
from tests import qa2_http  # noqa: E402

OLD_SRC = (Path(__file__).parent / "golden" / "net_guard_origin_main_77645f9.py.txt").read_text()
T = httpx.Timeout(10.0, connect=5.0)
HEADERS = {"User-Agent": "HugoQA/1.0", "Accept-Language": "es-AR", "X-Prueba": "1"}


def _load_old():
    spec = importlib.util.spec_from_loader("net_guard_origin_main", loader=None)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(OLD_SRC, "net_guard_origin_main_77645f9.py", "exec"), mod.__dict__)
    return mod


@pytest.fixture
def old_guard():
    return _load_old()


@pytest.fixture
def server(monkeypatch, old_guard):
    s = qa2_http.Local().start()
    ok = "<html>hola</html>"
    s.route("/ok", ok, Content_Type="text/html; charset=utf-8", ETag='"abc"')
    s.route("/gz", qa2_http.gz(ok * 100), Content_Type="text/html", Content_Encoding="gzip")
    s.route("/deflate", qa2_http.deflate(ok * 100), Content_Type="text/html", Content_Encoding="deflate")
    s.route("/chunked", ok * 5000, Content_Type="text/html", X_Chunked="1")
    s.redirect("/r1", "/r2", 301)
    s.redirect("/r2", "/ok", 302)
    s.redirect("/abs", f"{s.base}/ok", 301)
    s.redirect("/307", "/ok", 307)
    s.redirect("/308", "/ok", 308)
    s.redirect("/loop", "/loop", 302)
    s.redirect("/private", "http://169.254.169.254/latest/meta-data/", 302)
    s.redirect("/file", "file:///etc/passwd", 302)
    s.redirect("/other-scheme", "ftp://example.com/x", 302)
    s.routes["/no-location"] = (302, {}, "")
    s.route("/404", "no", 404)
    s.route("/500", "boom", 500)
    s.route("/429", "slow", 429, Retry_After="30")
    s.route("/204", "", 204)
    s.route("/empty", "", 200)
    s.route("/big", b"\0" * (5 * 1024 * 1024), Content_Type="application/octet-stream")
    s.routes["/hdrs"] = lambda seen: (200, {"Content-Type": "application/json"}, json.dumps(seen.headers, sort_keys=True))
    for mod in (new_guard, old_guard):
        real = mod._ip_is_public
        monkeypatch.setattr(mod, "_ip_is_public", lambda ip, real=real: ip == "127.0.0.1" or real(ip))
    yield s
    s.stop()


async def outcome(guard, url, **kw):
    try:
        r = await guard.safe_get(url, timeout=T, headers=dict(HEADERS), **kw)
    except Exception as exc:  # noqa: BLE001
        return ("exc", type(exc).__name__)
    return ("ok", r.status_code, str(r.url).replace(url.split("/", 3)[0] + "//" + url.split("/", 3)[2], "BASE"), r.content,
            r.headers.get("content-type"), r.headers.get("etag"), r.http_version)


PATHS = ["/ok", "/gz", "/deflate", "/chunked", "/r1", "/abs", "/307", "/308", "/loop", "/private", "/file", "/other-scheme",
         "/no-location", "/404", "/500", "/429", "/204", "/empty", "/big", "/hdrs", "/missing"]


@pytest.mark.parametrize("path", PATHS)
async def test_without_max_bytes_the_old_and_the_new_safe_get_answer_the_same(server, old_guard, path):
    url = f"{server.base}{path}"
    before = len(server.requests)
    old = await outcome(old_guard, url)
    n_old = len(server.requests) - before
    before = len(server.requests)
    new = await outcome(new_guard, url)
    n_new = len(server.requests) - before
    assert new == old, f"{path}: distinto resultado"
    assert n_new == n_old, f"{path}: distinta cantidad de pedidos al servidor ({n_old} vs {n_new})"


async def test_the_headers_that_travel_are_exactly_the_ones_the_caller_gave_plus_httpxs_defaults(server, old_guard):
    old = await outcome(old_guard, f"{server.base}/hdrs")
    new = await outcome(new_guard, f"{server.base}/hdrs")
    assert new == old
    sent = json.loads(new[3])
    assert sent["user-agent"] == "HugoQA/1.0" and sent["x-prueba"] == "1"
    assert sent["accept-encoding"].startswith("gzip"), "sin max_bytes manda lo de siempre de httpx, no solo gzip"
    assert "cookie" not in sent and "authorization" not in sent


@pytest.mark.parametrize("hops,expect", [(0, "exc"), (1, "exc"), (2, "ok"), (5, "ok")])
async def test_max_redirects_counts_the_same_in_both(server, old_guard, hops, expect):
    url = f"{server.base}/r1"                        # /r1 → /r2 → /ok: dos saltos
    old = await outcome(old_guard, url, max_redirects=hops)
    new = await outcome(new_guard, url, max_redirects=hops)
    assert new == old and new[0] == expect


async def test_a_redirect_to_a_private_address_is_blocked_by_both_without_asking_it(server, old_guard):
    for guard in (old_guard, new_guard):
        with pytest.raises(guard.SsrfBlocked):
            await guard.safe_get(f"{server.base}/private", timeout=T, headers=dict(HEADERS))


async def test_the_old_callers_keep_working_through_their_real_modules(server, monkeypatch):
    """Los módulos que llaman a safe_get sin max_bytes (source_check, competitor_check, image_from_url) siguen igual."""
    from app.pricing import source_check

    class F(source_check.SourceFetcher):
        name = "qa"
        domain_pattern = __import__("re").compile("127")

        async def fetch_price(self, url):
            return None

    html = await F()._get(f"{server.base}/ok")
    assert html == "<html>hola</html>"
    html = await F()._get(f"{server.base}/r1")                 # con redirects
    assert html == "<html>hola</html>"
    with pytest.raises(httpx.HTTPStatusError):
        await F()._get(f"{server.base}/500")

    from app.ingest import image_from_url
    png = __import__("base64").b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    server.route("/foto.png", png, Content_Type="image/png")
    monkeypatch.setattr(image_from_url, "safe_get", new_guard.safe_get)
    product = await image_from_url.extract(f"{server.base}/foto.png")
    assert product.kind == "image" and product.image_urls == [f"{server.base}/foto.png"]
