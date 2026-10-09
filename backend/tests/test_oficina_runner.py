"""El runner de la Mac (`backend/tools/oficina_ml_search.py`): configuración, `--init`, ritmo, frenado al primer
bloqueo, lotes, dry-run, y de punta a punta contra el Hugo de verdad (con ML y el navegador de mentira)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import httpx  # noqa: E402
import pytest  # noqa: E402
from dotenv import dotenv_values  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.db.models import MlWebResult  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.ingest import browser_fetch  # noqa: E402
from tests.ml_web_fixtures import ANTIBOT_HTML, listing_html, page, polycard  # noqa: E402
from tests.test_oficina_api import KEY, _snap, api  # noqa: E402,F401  (fixture `api`: Hugo de verdad, con la key)
from tools import oficina_ml_search as runner  # noqa: E402

TOOLS = Path(runner.__file__).parent
HugoClient = runner.HugoClient          # la de verdad: algunos tests reemplazan `runner.HugoClient`
SECRET = "S3cr3t-" + "k" * 40


def _cards(*refs: str):
    return [polycard(r, f"Organizador {r}", 250.0, picture=f"{r[3:]}-MLA1_012025") for r in refs]


def _ok(*refs: str):
    return page(listing_html(_cards(*refs or ("MLA901",))))


EMPTY = page(listing_html([]))


class FakeML:
    """El navegador de mentira: una página (o excepción) por slug, y lo que se le pidió."""

    def __init__(self, pages: dict | None = None, default=None):
        self.pages = pages or {}
        self.default = default if default is not None else _ok()
        self.urls: list[str] = []

    async def fetch(self, url: str):
        self.urls.append(url)
        item = self.pages.get(url.rsplit("/", 1)[-1], self.default)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeHugo:
    """Los dos endpoints, grabando lo que recibe."""

    def __init__(self, items=None, post_status=200):
        self.items = items or []
        self.post_status = post_status
        self.posts: list[list[dict]] = []
        self.queue_calls: list[str] = []
        self.headers: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(dict(request.headers))
        if request.method == "GET":
            self.queue_calls.append(str(request.url))
            return httpx.Response(200, json={"items": self.items, "max_results": 8, "ttl_days": 7})
        results = json.loads(request.content)["results"]
        if self.post_status != 200:
            return httpx.Response(self.post_status, json={"detail": "x"})
        self.posts.append(results)
        return httpx.Response(200, json={"received": len(results), "stored": len(results), "duplicates": 0,
                                         "rejected": [], "rejected_total": 0})

    def client(self) -> runner.HugoClient:
        sleeps: list[float] = []
        self.sleeps = sleeps
        http = httpx.Client(transport=httpx.MockTransport(self.handler), base_url="https://hugo.example",
                            headers={"x-oficina-key": SECRET})
        return HugoClient(runner.Config(key=SECRET, hugo_url="https://hugo.example"), client=http,
                          sleep=sleeps.append)


def _items(n: int, queries=("organizador", )):
    return [{"product_id": str(i), "queries": list(queries)} for i in range(1, n + 1)]


def _searcher(ml: FakeML, hugo: FakeHugo | None, **kw):
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    s = runner.Searcher(fetch=ml.fetch, hugo=hugo.client() if hugo else None, sleep=sleep,
                        rng=__import__("random").Random(7), now=lambda: "2026-10-10T04:00:00Z", **kw)
    s.sleeps = sleeps
    return s


async def _go(searcher: runner.Searcher, items, limit: int = 250) -> int:
    return await searcher.run(items, limit)


# ─── configuración y --init ────────────────────────────────────────────────


def test_init_creates_a_strong_key_without_printing_it(tmp_path, capsys):
    env = tmp_path / "cfg" / ".env"
    message = runner.init_key(env)
    key = dotenv_values(env)[runner.KEY_VAR]
    assert len(key) >= 40 and key not in message and key not in capsys.readouterr().out
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert "Coolify" in message and "OFICINA_SEARCH_KEY" in message and str(env) in message
    assert "HUGO_URL" in message and "# HUGO_URL=" in env.read_text()


def test_init_keeps_the_other_lines_and_does_not_rotate_an_existing_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# keys del bench\nANTHROPIC_API_KEY=sk-ant-xxx\nHUGO_URL=https://hugo.b2box.pro")   # sin salto final
    env.chmod(0o644)
    runner.init_key(env)
    first = env.read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-xxx" in first and "# keys del bench" in first and "HUGO_URL=https://hugo.b2box.pro" in first
    assert "# HUGO_URL" not in first                                        # ya estaba: no se agrega el recordatorio
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    key = dotenv_values(env)[runner.KEY_VAR]
    again = runner.init_key(env)
    assert env.read_text() == first and dotenv_values(env)[runner.KEY_VAR] == key
    assert "Ya hay" in again and key not in again


def test_init_replaces_an_empty_line_instead_of_duplicating_it(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OFICINA_SEARCH_KEY=\nOTRA=1\n")
    runner.init_key(env)
    assert env.read_text().count("OFICINA_SEARCH_KEY") == 1 and len(dotenv_values(env)[runner.KEY_VAR]) >= 40
    assert dotenv_values(env)["OTRA"] == "1"


def test_two_inits_generate_different_keys(tmp_path):
    keys = set()
    for i in range(3):
        env = tmp_path / f"{i}.env"
        runner.init_key(env)
        keys.add(dotenv_values(env)[runner.KEY_VAR])
    assert len(keys) == 3


def _write_env(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text)
    path.chmod(mode)
    return path


def test_load_config_reads_key_and_url(tmp_path):
    cfg = runner.load_config(_write_env(tmp_path / ".env", f"OFICINA_SEARCH_KEY={SECRET}\nHUGO_URL=https://hugo.b2box.pro/\n"))
    assert cfg.key == SECRET and cfg.hugo_url == "https://hugo.b2box.pro"
    assert SECRET not in repr(cfg) and SECRET not in str(cfg)


@pytest.mark.parametrize("text, mode, expect", [
    (f"HUGO_URL=https://h.example\n", 0o600, "Falta OFICINA_SEARCH_KEY"),
    (f"OFICINA_SEARCH_KEY={SECRET}\n", 0o600, "Falta HUGO_URL"),
    ("OFICINA_SEARCH_KEY=corta\nHUGO_URL=https://h.example\n", 0o600, "muy corta"),
    (f"OFICINA_SEARCH_KEY={SECRET}\nHUGO_URL=https://h.example\n", 0o644, "chmod 600"),
    (f"OFICINA_SEARCH_KEY={SECRET}\nHUGO_URL=https://h.example\n", 0o640, "chmod 600"),
    (f"OFICINA_SEARCH_KEY={SECRET}\nHUGO_URL=http://hugo.b2box.pro\n", 0o600, "https"),
    (f"OFICINA_SEARCH_KEY={SECRET}\nHUGO_URL=https://u:p@hugo.b2box.pro\n", 0o600, "sin usuario"),
    (f"OFICINA_SEARCH_KEY={SECRET}\nHUGO_URL=https://hugo.b2box.pro/api/x\n", 0o600, "sin usuario"),
])
def test_a_bad_configuration_is_refused_with_a_reason_that_never_contains_the_key(tmp_path, text, mode, expect):
    with pytest.raises(runner.ConfigError) as err:
        runner.load_config(_write_env(tmp_path / ".env", text, mode))
    assert expect in str(err.value) and SECRET not in str(err.value)


def test_missing_env_file_points_to_init(tmp_path):
    with pytest.raises(runner.ConfigError, match="--init"):
        runner.load_config(tmp_path / "no-existe.env")


@pytest.mark.parametrize("url, ok", [("https://hugo.b2box.pro", True), ("http://localhost:8000", True),
                                     ("http://127.0.0.1:8123/", True), ("http://hugo.b2box.pro", False),
                                     ("ftp://x", False), ("hugo.b2box.pro", False), ("https://", False),
                                     ("https://h.example?x=1", False), ("https://h.example#a", False)])
def test_hugo_url_rules(url, ok):
    if ok:
        assert runner.normalize_hugo_url(url).startswith(("http://", "https://"))
    else:
        with pytest.raises(runner.ConfigError):
            runner.normalize_hugo_url(url)


# ─── el ritmo y la entrega ─────────────────────────────────────────────────


async def test_it_searches_slowly_and_sends_small_batches():
    ml, hugo = FakeML(), FakeHugo()
    s = _searcher(ml, hugo, batch_size=10)
    code = await _go(s, _items(25))
    assert code == 0 and len(ml.urls) == 25
    assert [len(b) for b in hugo.posts] == [10, 10, 5]
    assert len(s.sleeps) == 24                                  # una pausa entre búsquedas, ninguna antes de la primera ni después de la última
    assert all(runner.PAUSE_MIN_S <= p <= runner.PAUSE_MAX_S for p in s.sleeps) and len(set(s.sleeps)) > 1
    first = hugo.posts[0][0]
    assert set(first) == {"product_id", "query", "fetched_at", "status", "reason", "candidates"}
    assert (first["product_id"], first["status"], first["fetched_at"]) == ("1", "ok", "2026-10-10T04:00:00Z")
    assert first["candidates"][0]["id"] == "MLA901" and first["candidates"][0]["price_cents"] == 25_000
    assert s.stats.products == 25 and s.stats.sent == 25


async def test_the_listing_urls_are_ml_search_pages():
    ml = FakeML()
    await _go(_searcher(ml, FakeHugo()), [{"product_id": "1", "queries": ["Organizador Doble Ajustable Niños"]}])
    assert ml.urls == ["https://listado.mercadolibre.com.ar/organizador-doble-ajustable-niños".replace("ñ", "%C3%B1")]


async def test_an_empty_first_query_goes_on_to_the_next_and_stops_at_the_first_with_results():
    ml = FakeML({"larga": EMPTY, "corta": _ok("MLA5"), "claves": _ok("MLA6")})
    hugo = FakeHugo()
    await _go(_searcher(ml, hugo), [{"product_id": "9", "queries": ["larga", "corta", "claves"]}])
    assert [u.rsplit("/", 1)[-1] for u in ml.urls] == ["larga", "corta"]          # la tercera no hace falta
    [res] = hugo.posts[0]
    assert (res["status"], res["query"]) == ("ok", "corta") and res["candidates"][0]["id"] == "MLA5"


async def test_every_query_empty_is_reported_empty_with_the_last_query():
    ml = FakeML(default=EMPTY)
    hugo = FakeHugo()
    await _go(_searcher(ml, hugo), [{"product_id": "9", "queries": ["a", "b"]}])
    [res] = hugo.posts[0]
    assert (res["status"], res["query"], res["candidates"]) == ("empty", "b", []) and len(ml.urls) == 2


async def test_max_limits_the_products_it_searches():
    ml = FakeML()
    s = _searcher(ml, FakeHugo())
    await _go(s, _items(30), limit=7)
    assert len(ml.urls) == 7 and s.stats.products == 7


async def test_hugo_items_with_a_wrong_shape_are_skipped():
    ml = FakeML()
    bad = [None, 5, {"product_id": "../x", "queries": ["a"]}, {"product_id": "1"}, {"product_id": "2", "queries": []},
           {"product_id": "3", "queries": [None, 5, "  "]}, {"product_id": "4", "queries": ["bien"] * 9}]
    await _go(_searcher(ml, FakeHugo()), bad)
    assert len(ml.urls) == 1                                    # solo el "4", y con una sola consulta (la primera con resultados corta)


# ─── el primer bloqueo frena la noche ──────────────────────────────────────

BLOCKS = {
    "captcha_en_la_pagina": page(ANTIBOT_HTML),
    "http_429": page(listing_html([]), status=429),
    "http_403": page("<html>no</html>", status=403),
    "verificacion_de_cuenta": page("<html></html>", final_url="https://www.mercadolibre.com.ar/account-verification/x"),
    "redirect_a_otro_sitio": page(listing_html(_cards("MLA1")), final_url="https://evil.example/listado"),
    "corta_circuito": browser_fetch.CircuitOpen("descanso"),
}


@pytest.mark.parametrize("name", list(BLOCKS))
async def test_the_first_block_stops_the_whole_night_and_is_reported_without_retrying(name):
    ml = FakeML({"p3": BLOCKS[name]})
    hugo = FakeHugo()
    s = _searcher(ml, hugo, batch_size=10)
    items = [{"product_id": str(i), "queries": [f"p{i}"]} for i in range(1, 8)]
    code = await _go(s, items)
    assert code == runner.EXIT_BLOCKED
    assert [u.rsplit("/", 1)[-1] for u in ml.urls] == ["p1", "p2", "p3"]       # el 4 al 7 ni se tocan; el 3 una sola vez
    sent = [r for batch in hugo.posts for r in batch]
    assert [(r["product_id"], r["status"]) for r in sent] == [("1", "ok"), ("2", "ok"), ("3", "blocked")]
    assert sent[2]["reason"] and sent[2]["candidates"] == []
    assert s.stats.blocked == 1 and len(s.sleeps) == 2                           # nada de esperar y volver a probar


async def test_a_block_on_a_second_query_does_not_try_the_third():
    ml = FakeML({"a": EMPTY, "b": page(ANTIBOT_HTML), "c": _ok()})
    hugo = FakeHugo()
    code = await _go(_searcher(ml, hugo), [{"product_id": "1", "queries": ["a", "b", "c"]}, {"product_id": "2", "queries": ["z"]}])
    assert code == runner.EXIT_BLOCKED and len(ml.urls) == 2
    assert [(r["status"], r["query"]) for r in hugo.posts[0]] == [("blocked", "b")]


async def test_what_was_pending_is_delivered_even_when_the_night_is_cut():
    ml = FakeML({"p5": page(ANTIBOT_HTML)})
    hugo = FakeHugo()
    items = [{"product_id": str(i), "queries": [f"p{i}"]} for i in range(1, 9)]
    await _go(_searcher(ml, hugo, batch_size=10), items)
    assert sum(len(b) for b in hugo.posts) == 5 and hugo.posts[-1][-1]["status"] == "blocked"


async def test_nothing_changes_to_get_around_a_block():
    """Un bloqueo no cambia el ritmo, ni reintenta, ni rota nada: las búsquedas siguientes simplemente no existen."""
    ml = FakeML({"p2": page(ANTIBOT_HTML)})
    s = _searcher(ml, FakeHugo())
    await _go(s, [{"product_id": str(i), "queries": [f"p{i}"]} for i in range(1, 6)])
    assert len(ml.urls) == len(set(ml.urls)) == 2
    assert s.sleeps and all(runner.PAUSE_MIN_S <= p <= runner.PAUSE_MAX_S for p in s.sleeps)


# ─── errores que no son bloqueos ───────────────────────────────────────────


async def test_unreadable_pages_are_errors_and_five_in_a_row_stop_the_night():
    unreadable = page("<html><body>nada</body></html>")
    ml = FakeML(default=unreadable)
    hugo = FakeHugo()
    s = _searcher(ml, hugo, batch_size=100)
    code = await _go(s, _items(20))
    assert code == runner.EXIT_BROWSER and len(ml.urls) == runner.ERROR_STREAK
    assert {r["status"] for b in hugo.posts for r in b} == {"error"} and s.stats.blocked == 0


async def test_an_isolated_error_does_not_stop_anything():
    ml = FakeML({"p2": RuntimeError("Firefox se cerró"), "p4": page("<html></html>")}, default=_ok())
    hugo = FakeHugo()
    items = [{"product_id": str(i), "queries": [f"p{i}"]} for i in range(1, 7)]
    code = await _go(_searcher(ml, hugo, batch_size=100), items)
    assert code == 0 and len(ml.urls) == 6
    statuses = {r["product_id"]: r["status"] for b in hugo.posts for r in b}
    assert statuses == {"1": "ok", "2": "error", "3": "ok", "4": "error", "5": "ok", "6": "ok"}


# ─── dry-run y entrega con problemas ───────────────────────────────────────


async def test_dry_run_searches_but_sends_nothing():
    ml, hugo = FakeML(), FakeHugo()
    s = _searcher(ml, hugo, dry_run=True)
    code = await _go(s, _items(12))
    assert code == 0 and len(ml.urls) == 12 and hugo.posts == []
    assert [h for h in hugo.headers if "x-oficina-key" in h] == []           # ni siquiera tocó a Hugo con la key


async def test_a_batch_that_cannot_be_sent_is_retried_then_counted_as_lost_and_the_night_goes_on():
    ml, hugo = FakeML(), FakeHugo(post_status=503)
    s = _searcher(ml, hugo, batch_size=5)
    code = await _go(s, _items(10))
    assert code == runner.EXIT_SEND and len(ml.urls) == 10 and s.stats.lost == 10
    assert len(hugo.headers) == 2 * runner.SEND_ATTEMPTS and hugo.sleeps == list(runner.SEND_BACKOFF_S[:2]) * 2


@pytest.mark.parametrize("status", [401, 404, 413])
async def test_a_hugo_that_rejects_the_key_or_is_off_stops_the_night_without_retrying(status):
    ml, hugo = FakeML(), FakeHugo(post_status=status)
    s = _searcher(ml, hugo, batch_size=3)
    code = await _go(s, _items(12))
    assert code == runner.EXIT_CONFIG and len(ml.urls) == 3 and len(hugo.headers) == 1
    assert hugo.sleeps == []


# ─── la key no se filtra ───────────────────────────────────────────────────


async def test_the_key_never_reaches_logs_or_output(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    ml = FakeML({"p2": page(ANTIBOT_HTML), "p1": RuntimeError("boom")})
    hugo = FakeHugo()
    await _go(_searcher(ml, hugo), [{"product_id": str(i), "queries": [f"p{i}"]} for i in range(1, 4)])
    hugo_bad = FakeHugo(post_status=401)
    await _go(_searcher(FakeML(), hugo_bad, batch_size=1), _items(2))
    out = capsys.readouterr()
    assert SECRET not in caplog.text and SECRET not in out.out and SECRET not in out.err


# ─── de punta a punta contra el Hugo de verdad ─────────────────────────────


def _bridge(api_client) -> httpx.Client:
    """Un httpx.Client cuyo transporte es la app de Hugo (FastAPI) de verdad."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        r = api_client.request(request.method, path, content=request.content,
                               headers={k: v for k, v in request.headers.items()
                                        if k.lower() in ("x-oficina-key", "content-type")})
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": "application/json"})
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://hugo.example",
                        headers={"x-oficina-key": KEY})


async def test_end_to_end_the_queue_comes_from_hugo_and_the_results_are_sanitized_and_stored(api):
    _snap("1", name="Organizador Doble Ajustable 3 Niveles 40x30 Blanco")
    _snap("2", name="Taza ceramica")
    _snap("3", "ok", origin="api")
    hugo = HugoClient(runner.Config(key=KEY, hugo_url="https://hugo.example"), client=_bridge(api))
    queue = hugo.queue(10)
    assert [i["product_id"] for i in queue["items"]] == ["1", "2"] and queue["max_results"] == 8

    hostile = polycard("MLA555", "Organizador\x00 raro\n{x}", 999.0, url="https://evil.example/x", picture="555-MLA1_012025")
    ml = FakeML({"organizador-doble-ajustable-3-niveles-40x30-blanco": page(listing_html([hostile, *_cards("MLA901")]))},
                default=EMPTY)
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)

    s = runner.Searcher(fetch=ml.fetch, hugo=hugo, max_results=queue["max_results"], sleep=sleep)
    code = await s.run(queue["items"], 10)
    assert code == 0 and s.stats.sent == 2 and s.stats.rejected == 0
    with Session(engine) as db:
        rows = {r.product_id: r for r in db.exec(select(MlWebResult))}
    assert rows["1"].status == "ok" and rows["2"].status == "empty"
    cands = {c["id"]: c for c in json.loads(rows["1"].candidates)}
    assert set(cands) == {"MLA555", "MLA901"}
    assert cands["MLA555"]["name"] == "Organizador raro"                                # saneado de punta a punta
    assert cands["MLA555"]["permalink"] == "https://articulo.mercadolibre.com.ar/MLA-555"
    # y lo que ya buscó la oficina deja de estar en la cola
    assert [i["product_id"] for i in hugo.queue(10)["items"]] == []


async def test_end_to_end_a_block_is_stored_as_blocked_and_the_product_stays_in_the_queue(api):
    _snap("1"); _snap("2"); _snap("3")
    hugo = HugoClient(runner.Config(key=KEY, hugo_url="https://hugo.example"), client=_bridge(api))
    ml = FakeML({"organizador-cocina": _ok("MLA901")}, default=page(ANTIBOT_HTML))
    items = [{"product_id": str(i), "queries": ["organizador cocina" if i == 1 else "otra cosa"]} for i in (1, 2, 3)]

    async def sleep(seconds):
        return None

    s = runner.Searcher(fetch=ml.fetch, hugo=hugo, sleep=sleep)
    assert await s.run(items, 10) == runner.EXIT_BLOCKED
    with Session(engine) as db:
        got = {r.product_id: r.status for r in db.exec(select(MlWebResult))}
    assert got == {"1": "ok", "2": "blocked"}
    assert [i["product_id"] for i in hugo.queue(10)["items"]] == ["2", "3"]      # el bloqueado y el que quedó sin buscar


# ─── línea de comandos y entorno ───────────────────────────────────────────


def test_cli_init_prints_instructions_and_never_the_key(tmp_path, capsys):
    env = tmp_path / ".env"
    assert runner.main(["--init", "--env-file", str(env)]) == 0
    out = capsys.readouterr().out
    assert dotenv_values(env)[runner.KEY_VAR] not in out and "Coolify" in out


@pytest.mark.parametrize("argv", [["--max", "0"], ["--max", "501"], ["--pause-min", "7.9"], ["--pause-min", "20", "--pause-max", "10"]])
def test_cli_refuses_dangerous_numbers(tmp_path, argv):
    assert runner.main([*argv, "--env-file", str(tmp_path / "x"), "--log-file", str(tmp_path / "log")]) == runner.EXIT_CONFIG


def test_cli_without_a_valid_config_exits_2_and_logs_no_key(tmp_path, capsys):
    env = _write_env(tmp_path / ".env", f"OFICINA_SEARCH_KEY={SECRET}\n", 0o644)
    assert runner.main(["--env-file", str(env), "--log-file", str(tmp_path / "run.log")]) == runner.EXIT_CONFIG
    assert SECRET not in capsys.readouterr().out and SECRET not in (tmp_path / "run.log").read_text()


def test_cli_check_talks_to_hugo_but_does_not_search(tmp_path, monkeypatch, capsys):
    env = _write_env(tmp_path / ".env", f"OFICINA_SEARCH_KEY={SECRET}\nHUGO_URL=https://hugo.example\n")
    hugo = FakeHugo(_items(1))
    monkeypatch.setattr(runner, "HugoClient", lambda cfg: hugo.client())
    monkeypatch.setattr(runner, "_run_real", lambda *a, **k: pytest.fail("--check no busca"))
    assert runner.main(["--check", "--env-file", str(env), "--log-file", str(tmp_path / "run.log")]) == 0
    assert hugo.queue_calls[0].endswith("limit=1") and "OK" in capsys.readouterr().out


def test_cli_maps_hugo_errors_to_exit_codes(tmp_path, monkeypatch):
    env = _write_env(tmp_path / ".env", f"OFICINA_SEARCH_KEY={SECRET}\nHUGO_URL=https://hugo.example\n")

    class Down:
        def queue(self, limit):
            raise runner.HugoError("Hugo rechazó la key (401)")

        def close(self):
            pass

    monkeypatch.setattr(runner, "HugoClient", lambda cfg: Down())
    assert runner.main(["--env-file", str(env), "--log-file", str(tmp_path / "l")]) == runner.EXIT_CONFIG


def test_cli_an_empty_queue_is_a_normal_night(tmp_path, monkeypatch):
    env = _write_env(tmp_path / ".env", f"OFICINA_SEARCH_KEY={SECRET}\nHUGO_URL=https://hugo.example\n")
    monkeypatch.setattr(runner, "HugoClient", lambda cfg: FakeHugo([]).client())
    monkeypatch.setattr(runner, "_run_real", lambda *a, **k: pytest.fail("no hay nada que buscar"))
    assert runner.main(["--env-file", str(env), "--log-file", str(tmp_path / "l")]) == 0


def test_the_proxy_is_forced_off_even_if_the_repo_env_has_one():
    code = ("import os, sys; sys.path.insert(0, %r); from tools import oficina_ml_search as r; r.prepare_environment();"
            "from app.ingest import browser_fetch as b; print(b.proxy_configured(), os.environ['DATABASE_URL'])"
            % str(TOOLS.parent))
    env = {**os.environ, "BROWSER_PROXY": "http://usuario:clave@proxy.example:8080", "DATABASE_URL": "sqlite:///./x.db"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=TOOLS.parent, env=env, timeout=120)
    assert out.stdout.split() == ["False", "sqlite:///:memory:"], out.stderr[-500:]


def test_keep_awake_ties_caffeinate_to_the_process(monkeypatch):
    seen = []
    monkeypatch.setattr(runner.sys, "platform", "darwin")
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/caffeinate")
    monkeypatch.setattr(runner.subprocess, "Popen", lambda cmd, **kw: seen.append(cmd) or object())
    assert runner.keep_awake() is not None
    assert seen == [["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())]]
    monkeypatch.setattr(runner.sys, "platform", "linux")
    assert runner.keep_awake() is None


def test_the_launchd_example_runs_at_one_am_and_holds_no_secret():
    raw = (TOOLS / "com.b2box.oficina-ml-search.plist").read_bytes()
    data = plistlib.loads(raw)
    assert data["Label"] == "com.b2box.oficina-ml-search"
    assert data["StartCalendarInterval"] == {"Hour": 1, "Minute": 0}
    assert data["ProgramArguments"][1].endswith("backend/tools/oficina_ml_search.py")
    assert data["KeepAlive"] is False and data["RunAtLoad"] is False
    assert not any("key" in str(a).lower() and "=" in str(a) for a in data["ProgramArguments"])
    assert "EnvironmentVariables" not in data                              # nada de secretos en el plist
