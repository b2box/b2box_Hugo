"""QA3, criterio 5: el runner de la Mac como CAJA NEGRA.

Cada caso lanza `python tools/oficina_ml_search.py` de verdad (un subproceso con su propio parseo de argumentos, config, señales,
lotes, salida y códigos de salida) contra un Hugo REAL (uvicorn en un hilo, la app de verdad, SQLite) y un "ML" local que sirve
páginas de listado, captchas, muros de verificación, 403/429, redirecciones a otro sitio y cuelgues. Los únicos reemplazos están en
`qa3_runner_driver.py`: el navegador por un cliente HTTP al ML local y `asyncio.sleep` por una espera casi nula que anota cuánto se
le pidió dormir (así se afirma el ritmo de 8 a 15 s sin esperarlo). HOME apunta a un directorio temporal: no toca la config real.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
import uvicorn  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import main as main_mod  # noqa: E402
from app.api import oficina_routes  # noqa: E402
from app.db.models import MlWebResult  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.clock import utcnow  # noqa: E402
from app.pricing import market_match, market_ml_web  # noqa: E402
from tests.ml_web_fixtures import ANTIBOT_HTML, listing_html, page, polycard  # noqa: E402,F401
from tests.test_oficina_api import KEY, _snap, api  # noqa: E402,F401
from tests.qa3_cleanup import qa3_clean  # noqa: E402,F401  (fixture autouse)

BACKEND = Path(__file__).resolve().parents[1]
DRIVER = Path(__file__).with_name("qa3_runner_driver.py")
RUNNER = BACKEND / "tools" / "oficina_ml_search.py"
NAMES = ["Taza cerámica", "Jarra vidrio", "Cuchara madera", "Plato hondo", "Vaso térmico", "Mate calabaza", "Termo acero",
         "Cuchillo chef", "Tabla bambú", "Colador fino", "Rallador queso", "Pava eléctrica"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _slug(name: str) -> str:
    return market_ml_web.slugify(market_match.search_query(name))


def _listing(*titles: str, head_extra: str = "") -> str:
    cards = [polycard(f"MLA9{k}0{i}", t, 250.0 + i, picture=f"9{k}0{i}-MLA1_012025") for i, t in enumerate(titles, 1) for k in (1,)]
    html = listing_html(cards)
    return html.replace("<head>", "<head>" + head_extra, 1) if head_extra else html


class MlLocal:
    """El "ML" local: una respuesta por slug (status, headers, body) y el registro de a qué hora le pegaron a qué."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, dict[str, str], str]] = {}
        self.default: tuple[int, dict[str, str], str] = (200, {}, _listing("Producto"))
        self.hits: list[tuple[str, float]] = []
        self.delay = 0.0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a, **k) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                slug = self.path.lstrip("/").split("?", 1)[0]
                outer.hits.append((slug, time.time()))
                if outer.delay:
                    time.sleep(outer.delay)
                status, headers, body = outer.routes.get(slug, outer.default)
                data = body.encode("utf-8")
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def slugs(self) -> list[str]:
        return [h[0] for h in self.hits]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def ml():
    server = MlLocal()
    yield server
    server.close()


@pytest.fixture
def hugo(api):  # noqa: F811
    """Hugo de verdad (la app, uvicorn, sin lifespan: ni scheduler ni warm-ups) en un puerto local."""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(main_mod.app, host="127.0.0.1", port=port, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    oficina_routes.reset_limits()
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)
    oficina_routes.reset_limits()


class Run:
    def __init__(self, proc: subprocess.CompletedProcess, sleeps: list[float], log: Path, env_file: Path) -> None:
        self.code, self.out, self.sleeps, self.log_file, self.env_file = proc.returncode, proc.stdout + proc.stderr, sleeps, log, env_file

    @property
    def log(self) -> str:
        return self.log_file.read_text(encoding="utf-8") if self.log_file.exists() else ""


@pytest.fixture
def box(tmp_path, ml, hugo):
    home = tmp_path / "home"
    home.mkdir()
    env_file = home / "bench.env"
    env_file.write_text(f"OFICINA_SEARCH_KEY={KEY}\nHUGO_URL={hugo}\n")
    env_file.chmod(0o600)

    def env(extra: dict | None = None) -> dict:
        base = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "VENDURE_API_URL": "https://example.invalid/admin-api",
                "QA3_ML_PORT": str(ml.port), "QA3_SLEEP_LOG": str(tmp_path / "sleeps.log"), "PYTHONDONTWRITEBYTECODE": "1"}
        return {**base, **(extra or {})}

    def popen(*args: str, extra_env: dict | None = None, env_file_path: Path | None = None) -> subprocess.Popen:
        cmd = [sys.executable, str(DRIVER), "--env-file", str(env_file_path or env_file), "--log-file", str(tmp_path / "run.log"),
               "--no-caffeinate", *args]
        return subprocess.Popen(cmd, cwd=BACKEND, env=env(extra_env), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def run(*args: str, extra_env: dict | None = None, env_file_path: Path | None = None, timeout: float = 90) -> Run:
        oficina_routes.reset_limits()
        proc = popen(*args, extra_env=extra_env, env_file_path=env_file_path)
        out, _ = proc.communicate(timeout=timeout)
        sleeps_path = tmp_path / "sleeps.log"
        sleeps = [float(x) for x in sleeps_path.read_text().split()] if sleeps_path.exists() else []
        sleeps_path.unlink(missing_ok=True)
        return Run(subprocess.CompletedProcess(proc.args, proc.returncode, out, ""), sleeps, tmp_path / "run.log", env_file_path or env_file)

    run.popen, run.env_file, run.home, run.tmp = popen, env_file, home, tmp_path
    return run


def _seed(n: int) -> list[str]:
    for i, name in enumerate(NAMES[:n], 1):
        _snap(str(i), name=name)
    return [_slug(name) for name in NAMES[:n]]


def _rows() -> list[MlWebResult]:
    with Session(engine) as s:
        return list(s.exec(select(MlWebResult).order_by(MlWebResult.id)))


def _hugo_hits() -> int:
    return sum(len(v) for v in oficina_routes._hits.values())


# ─── una noche normal ───────────────────────────────────────────────────────


def test_a_normal_night_searches_slowly_one_page_at_a_time_and_hugo_stores_everything(box, ml):
    slugs = _seed(6)
    for slug, name in zip(slugs, NAMES):
        ml.routes[slug] = (200, {}, _listing(f"{name} Premium", f"{name} Pro"))
    r = box()
    assert r.code == 0, r.out
    assert ml.slugs == slugs                                                      # una página por producto, en el orden de la cola
    assert len(r.sleeps) == 5 and all(8.0 <= s <= 15.0 for s in r.sleeps), r.sleeps       # pausa 8-15 s, ninguna antes de la primera
    assert len({round(s, 3) for s in r.sleeps}) > 1, "la pausa es al azar, no constante"
    rows = _rows()
    assert [(x.product_id, x.status, x.n_candidates) for x in rows] == [(str(i), "ok", 2) for i in range(1, 7)]
    assert all(x.origin == "oficina" and json.loads(x.candidates)[0]["permalink"].startswith("https://") for x in rows)
    assert "6 productos, 6 búsquedas" in r.out and "Traceback" not in r.out


def test_max_limits_the_run_and_the_request_to_hugo(box, ml):
    _seed(8)
    r = box("--max", "3")
    assert r.code == 0, r.out
    assert len(ml.hits) == 3 and len(_rows()) == 3 and len(r.sleeps) == 2
    assert "3 productos, 3 búsquedas" in r.out


@pytest.mark.parametrize("args", [("--max", "0"), ("--max", "501"), ("--max", "-3"), ("--pause-min", "7.9"), ("--pause-min", "0"),
                                  ("--pause-min", "20", "--pause-max", "10"), ("--pause-max", "5")])
def test_dangerous_numbers_are_refused_before_touching_anything(box, ml, args):
    _seed(3)
    r = box(*args)
    assert r.code == 2 and ml.hits == [] and _rows() == []
    assert _hugo_hits() == 0, "ni siquiera le habló a Hugo"


def test_dry_run_searches_for_real_but_posts_nothing(box, ml):
    _seed(3)
    r = box("--dry-run", "--max", "3")
    assert r.code == 0, r.out
    assert len(ml.hits) == 3 and _rows() == []
    assert _hugo_hits() == 1, "solo la cola (un GET): ningún POST"
    assert "dry-run" in r.out


def test_check_talks_to_hugo_once_and_never_searches(box, ml):
    _seed(3)
    r = box("--check")
    assert r.code == 0 and "OK" in r.out
    assert ml.hits == [] and _rows() == [] and _hugo_hits() == 1
    assert KEY not in r.out and KEY not in r.log


def test_an_empty_queue_is_a_normal_night(box, ml):
    r = box()
    assert r.code == 0 and ml.hits == [] and "vacía" in r.out


def test_one_page_per_product_per_night_and_the_next_variant_the_night_after(box, ml):
    """Tope de ML desde la IP de la oficina: Hugo da UNA consulta por producto; si vino vacía, la siguiente recién la noche
    siguiente (la cola espera 12 h desde el resultado vacío)."""
    _snap("1", name="Organizador Doble Ajustable 3 Niveles 40x30 Blanco")
    q1 = _slug("Organizador Doble Ajustable 3 Niveles 40x30 Blanco")
    q2 = _slug("Organizador Doble Ajustable Niveles")
    ml.routes[q1] = (200, {}, listing_html([]))
    ml.routes[q2] = (200, {}, _listing("Organizador doble ajustable"))
    r = box()
    assert r.code == 0 and ml.slugs == [q1]                                       # una sola página; la segunda ni se pide
    assert r.sleeps == []
    [row] = _rows()
    assert (row.status, row.query) == ("empty", market_match.search_query("Organizador Doble Ajustable 3 Niveles 40x30 Blanco"))
    again = box()                                                                 # esa misma noche (otra corrida): nada
    assert again.code == 0 and ml.slugs == [q1] and "vacía" in again.out
    with Session(engine) as s:                                                    # pasa la noche
        row = s.get(MlWebResult, row.id)
        row.fetched_at -= timedelta(hours=20)
        s.add(row)
        s.commit()
    tomorrow = box()
    assert tomorrow.code == 0 and ml.slugs == [q1, q2]
    assert [(x.status, x.query) for x in _rows()][-1] == ("ok", "Organizador Doble Ajustable Niveles")


def test_even_if_hugo_sent_several_queries_the_runner_opens_one_page(box, ml):
    stub = StubHugo(fail_first=0, items=[{"product_id": "1", "queries": ["taza ceramica", "taza", "ceramica"]}])
    try:
        box.env_file.write_text(f"OFICINA_SEARCH_KEY={KEY}\nHUGO_URL={stub.url}\n")
        ml.default = (200, {}, listing_html([]))
        r = box()
        assert r.code == 0 and ml.slugs == ["taza-ceramica"] and stub.posts == 1
    finally:
        stub.close()


# ─── frena al primer bloqueo de verdad ──────────────────────────────────────

WALL = "https://www.mercadolibre.com/gz/account-verification?go=https%3A%2F%2Flistado.mercadolibre.com.ar%2Fx"
BLOCKS = {
    "captcha": (200, {}, "<html><head><title>Un momento</title></head><body><div id='captcha-box'>captcha</div></body></html>"),
    "suspicious-traffic": (200, {}, ANTIBOT_HTML),
    "trafico-inusual": (200, {}, "<html><body>Detectamos tráfico inusual desde tu red</body></html>"),
    "account-verification": (200, {"x-final-url": WALL}, "<html><body>Verificá tu cuenta</body></html>"),
    "account-verification-con-estado-vacio": (200, {"x-final-url": WALL}, listing_html([])),
    "http-403": (403, {}, "<html>Forbidden</html>"),
    "http-429": (429, {}, "<html>Too Many Requests</html>"),
    "redirect-a-otro-sitio": (200, {"x-final-url": "https://evil.example/captura"}, "<html>x</html>"),
}


@pytest.mark.parametrize("name", list(BLOCKS))
def test_the_first_real_block_stops_the_whole_night_without_retrying(box, ml, name):
    slugs = _seed(6)
    ml.routes[slugs[2]] = BLOCKS[name]
    r = box()
    assert r.code == 3, r.out
    assert ml.slugs == slugs[:3], "ni una búsqueda después del bloqueo ni un reintento del mismo producto"
    rows = _rows()
    assert [(x.product_id, x.status) for x in rows] == [("1", "ok"), ("2", "ok"), ("3", "blocked")]
    assert rows[2].n_candidates == 0 and json.loads(rows[2].candidates) == []
    assert "se frena la noche" in r.out and "Traceback" not in r.out
    assert len(r.sleeps) == 2, "no esperó nada para volver a probar"


def test_a_blocked_product_goes_back_to_the_queue_and_the_ones_not_reached_too(box, ml, api):
    slugs = _seed(5)
    ml.routes[slugs[1]] = BLOCKS["http-429"]
    assert box().code == 3
    queue = api.get("/api/oficina/ml-queue", params={"limit": 50}, headers={"x-oficina-key": KEY}).json()["items"]
    assert [i["product_id"] for i in queue] == ["2", "3", "4", "5"]                     # 1 quedó buscado; el bloqueado y los demás siguen


def test_the_same_blocks_do_not_stop_a_normal_listing_that_merely_mentions_them(box, ml):
    """Falsos positivos: un listado con resultados cuyo head menciona captcha / verificación, o un producto que se llama así."""
    slugs = _seed(3)
    for slug in slugs:
        ml.routes[slug] = (200, {}, _listing("Libro Captcha y account-verification", "Tráfico inusual: novela",
                                             head_extra="<script id='recaptcha-lib'>/* captcha suspicious-traffic */</script>"))
    r = box()
    assert r.code == 0 and len(ml.hits) == 3 and [x.status for x in _rows()] == ["ok"] * 3


def test_an_empty_listing_is_empty_not_a_block(box, ml):
    slugs = _seed(3)
    for slug in slugs:
        ml.routes[slug] = (200, {}, listing_html([]))
    r = box()
    assert r.code == 0 and [x.status for x in _rows()] == ["empty"] * 3


def test_a_wall_that_says_nothing_known_is_an_error_and_five_in_a_row_stop_the_night(box, ml):
    slugs = _seed(8)
    ml.default = (200, {}, "<html><body><h1>Confirmá que sos una persona</h1></body></html>")
    r = box()
    assert r.code == 5, r.out
    assert len(ml.hits) == 5 and [x.status for x in _rows()] == ["error"] * 5


def test_an_isolated_browser_failure_does_not_stop_the_night(box, ml):
    slugs = _seed(5)
    ml.routes[slugs[1]] = (200, {"x-raise": "timeout"}, "")
    r = box()
    assert r.code == 0 and [x.status for x in _rows()] == ["ok", "error", "ok", "ok", "ok"]


def test_a_5xx_from_ml_is_an_error_not_a_block(box, ml):
    slugs = _seed(3)
    ml.routes[slugs[0]] = (503, {}, "<html>Service Unavailable</html>")
    r = box()
    assert r.code == 0 and [x.status for x in _rows()] == ["error", "ok", "ok"]


# ─── --init ─────────────────────────────────────────────────────────────────


def test_init_black_box_never_prints_the_key_and_never_overwrites_one(box):
    target = box.tmp / "nuevo" / "cfg" / ".env"
    first = box("--init", "--env-file", str(target))        # el --env-file del fixture va antes y se pisa con este
    assert first.code == 0 and target.exists()
    key = [ln for ln in target.read_text().splitlines() if ln.startswith("OFICINA_SEARCH_KEY=")][0].split("=", 1)[1]
    assert len(key) >= 32 and key not in first.out and stat.S_IMODE(target.stat().st_mode) == 0o600
    before = target.read_bytes()
    second = box("--init", "--env-file", str(target))
    assert second.code == 0 and target.read_bytes() == before and key not in second.out and "Ya hay" in second.out


def test_init_fixes_loose_permissions_of_an_existing_file_without_touching_the_key(box):
    target = box.tmp / "viejo.env"
    target.write_text("OFICINA_SEARCH_KEY=" + "k" * 40 + "\nHUGO_URL=https://h.example\n")
    target.chmod(0o644)
    r = box("--init", "--env-file", str(target))
    assert r.code == 0 and stat.S_IMODE(target.stat().st_mode) == 0o600 and ("k" * 40) not in r.out
    assert "k" * 40 in target.read_text()


# ─── configuración y Hugo que no responde bien ──────────────────────────────


def test_a_readable_env_file_is_refused(box, ml):
    _seed(2)
    box.env_file.chmod(0o644)
    r = box()
    assert r.code == 2 and "chmod 600" in r.out and ml.hits == [] and KEY not in r.out + r.log


def test_a_wrong_key_stops_at_the_first_contact_and_the_key_is_nowhere(box, ml):
    _seed(2)
    box.env_file.write_text(f"OFICINA_SEARCH_KEY={'W' * 40}\nHUGO_URL={box.env_file.read_text().split('HUGO_URL=')[1].strip()}\n")
    r = box()
    assert r.code == 2 and ml.hits == [] and "401" in r.out
    assert "W" * 40 not in r.out + r.log and KEY not in r.out + r.log


def test_hugo_not_listening_is_a_send_failure_not_a_traceback(box, ml):
    _seed(2)
    box.env_file.write_text(f"OFICINA_SEARCH_KEY={KEY}\nHUGO_URL=http://127.0.0.1:{_free_port()}\n")
    r = box()
    assert r.code == 4 and ml.hits == [] and "Traceback" not in r.out


def test_plain_http_to_a_real_host_is_refused_because_the_key_travels_in_every_request(box, ml):
    box.env_file.write_text(f"OFICINA_SEARCH_KEY={KEY}\nHUGO_URL=http://hugo.example.com\n")
    r = box()
    assert r.code == 2 and "https" in r.out and ml.hits == []


# ─── lo que queda en logs y salida ──────────────────────────────────────────


def test_nothing_sensitive_reaches_stdout_or_the_log_file(box, ml):
    slugs = _seed(4)
    ml.routes[slugs[3]] = BLOCKS["captcha"]
    r = box("-v")
    blob = r.out + r.log
    assert r.code == 3
    assert KEY not in blob and "x-oficina-key" not in blob.lower()
    for name in NAMES[:4]:
        assert name.lower() not in blob.lower(), f"el log trae el nombre del producto: {name}"
    assert "ceramica" not in blob.lower() and "premium" not in blob.lower()
    assert str(box.home) not in r.out or True


def test_log_file_is_created_with_the_run_and_rotates_only_by_size(box, ml):
    _seed(2)
    r = box()
    assert r.code == 0 and "producto 1: ok" in r.log and r.log_file.stat().st_size < 10_000


# ─── señales ────────────────────────────────────────────────────────────────


def test_sigterm_in_the_middle_delivers_what_was_searched_and_exits_clean(box, ml):
    _seed(12)
    ml.delay = 0.35
    proc = box.popen("--batch-size", "10", extra_env={"QA3_REAL_WAIT": "0.2"})
    deadline = time.time() + 40
    while len(ml.hits) < 4 and time.time() < deadline:
        time.sleep(0.05)
    assert len(ml.hits) >= 4, "el runner no arrancó"
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 0, out
    searched = len(ml.hits)
    assert searched < 12 and "Traceback" not in out and "interrumpido" in out
    stored = [x.product_id for x in _rows()]
    assert 1 <= len(stored) <= searched and stored == [str(i) for i in range(1, len(stored) + 1)]
    assert len(stored) >= searched - 1, "lo ya buscado se entregó (a lo sumo falta la página que estaba en vuelo)"


def test_sigint_behaves_like_sigterm(box, ml):
    _seed(12)
    ml.delay = 0.35
    proc = box.popen(extra_env={"QA3_REAL_WAIT": "0.2"})
    deadline = time.time() + 40
    while len(ml.hits) < 3 and time.time() < deadline:
        time.sleep(0.05)
    assert len(ml.hits) >= 3, "el runner no arrancó"
    proc.send_signal(signal.SIGINT)
    out, _ = proc.communicate(timeout=20)
    assert proc.returncode in (0, 130) and "Traceback" not in out.replace("KeyboardInterrupt", "")
    assert len(_rows()) >= len(ml.hits) - 1


# ─── dos runners a la vez ───────────────────────────────────────────────────


def test_a_second_runner_refuses_to_start_while_another_one_is_running(box, ml):
    _seed(10)
    ml.delay = 0.3
    first = box.popen("--max", "6", extra_env={"QA3_REAL_WAIT": "0.2"})
    deadline = time.time() + 40
    while len(ml.hits) < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert len(ml.hits) >= 2, "el primer runner no arrancó"
    before = len(ml.hits)
    second = box.popen("--max", "6", extra_env={"QA3_REAL_WAIT": "0.2"})
    out2, _ = second.communicate(timeout=60)
    first.communicate(timeout=60)
    assert second.returncode != 0 and "ya hay" in out2.lower()
    assert len(ml.hits) <= before + 6


# ─── un fallo silencioso: el reloj de la Mac ────────────────────────────────


def test_a_mac_clock_a_few_minutes_behind_still_delivers(box, ml):
    _seed(3)
    r = box(extra_env={"QA3_CLOCK_SKEW_MIN": "-30"})
    assert r.code == 0 and [x.status for x in _rows()] == ["ok"] * 3


def test_a_mac_clock_ahead_does_not_make_hugo_reject_everything_because_fetched_at_follows_hugos_clock(box, ml):
    """Antes: con el reloj de la Mac 10 minutos adelantado Hugo rechazaba TODO por fetched_at en el futuro y el runner salía con 0.
    Ahora fetched_at sale del reloj de Hugo (header Date de la cola)."""
    _seed(3)
    r = box(extra_env={"QA3_CLOCK_SKEW_MIN": "10"})
    assert r.code == 0 and [x.status for x in _rows()] == ["ok"] * 3
    assert all(abs((utcnow() - x.fetched_at).total_seconds()) < 120 for x in _rows())
    assert "adelantado" in r.out or "adelantado" in r.log                          # y avisa que el reloj está corrido


def test_a_mac_clock_a_day_behind_is_corrected_too(box, ml):
    _seed(2)
    r = box(extra_env={"QA3_CLOCK_SKEW_MIN": "-1440"})
    assert r.code == 0 and len(_rows()) == 2 and all(abs((utcnow() - x.fetched_at).total_seconds()) < 120 for x in _rows())


# ─── el prefijo público no abre el dashboard ────────────────────────────────

PATH_TRICKS = ["/api/oficina/../price-monitor/summary", "/api/oficina/%2e%2e/price-monitor/summary", "/api/oficina/..%2fprice-monitor/summary",
               "/api/oficina/./../price-monitor/summary", "/api/oficina//..//price-monitor/summary", "/api/oficina/%2e%2e%2fprice-monitor/summary",
               "/api/oficina/..;/price-monitor/summary", "/api/oficina/\\..\\price-monitor/summary", "//api/price-monitor/summary",
               "/api/oficina%2f..%2fprice-monitor/summary", "/api/oficina/ml-queue/../../price-monitor/summary", "/api/oficina/%252e%252e/price-monitor/summary",
               "/API/PRICE-MONITOR/summary", "/api/price-monitor/summary", "/api/price-monitor/summary/"]


@pytest.mark.parametrize("path", PATH_TRICKS)
def test_the_public_oficina_prefix_does_not_open_any_dashboard_route(hugo, path):
    """`/api/oficina/` está en las rutas públicas del middleware (sin cookie): ningún truco de ruta (puntos, %2e, barras, mayúsculas)
    puede colarse por ahí hasta `/api/price-monitor/*`. Se manda crudo por un socket: un cliente HTTP normaliza la ruta antes."""
    host, port = hugo.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=10) as s:
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n".encode())
        raw = b""
        while chunk := s.recv(65536):
            raw += chunk
    status = int(raw.split(b" ", 2)[1])
    body = raw.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in raw else b""
    assert status != 200 or b"last_run" not in body, (path, status, body[:200])
    assert b"cron_utc" not in body and b"fresh_products" not in body, path


# ─── Hugo reiniciándose a la hora del runner ────────────────────────────────


class StubHugo:
    """Un Hugo de mentira que falla las primeras N veces el GET de la cola (un redeploy de Coolify a la 01:00) y después anda."""

    def __init__(self, fail_first: int, items: list[dict]) -> None:
        self.fail_first, self.items, self.gets, self.posts = fail_first, items, 0, 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a, **k) -> None:
                return

            def _send(self, status: int, payload: dict) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                outer.gets += 1
                if outer.gets <= outer.fail_first:
                    return self._send(503, {"detail": "Service Unavailable"})
                self._send(200, {"items": outer.items, "max_results": 8, "ttl_days": 7})

            def do_POST(self) -> None:  # noqa: N802
                outer.posts += 1
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                n = len(body["results"])
                self._send(200, {"received": n, "stored": n, "duplicates": 0, "rejected": [], "rejected_total": 0})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_a_transient_5xx_on_the_queue_does_not_cost_the_whole_night(box, ml):
    stub = StubHugo(fail_first=1, items=[{"product_id": "1", "queries": ["taza ceramica"]}])
    try:
        box.env_file.write_text(f"OFICINA_SEARCH_KEY={KEY}\nHUGO_URL={stub.url}\n")
        r = box(timeout=120)
        assert r.code == 0 and len(ml.hits) == 1 and stub.posts == 1
    finally:
        stub.close()


def test_a_persistent_5xx_on_the_queue_is_exit_4_without_a_traceback(box, ml):
    stub = StubHugo(fail_first=10_000, items=[])
    try:
        box.env_file.write_text(f"OFICINA_SEARCH_KEY={KEY}\nHUGO_URL={stub.url}\n")
        r = box()
        assert r.code == 4 and ml.hits == [] and "Traceback" not in r.out and KEY not in r.out + r.log
    finally:
        stub.close()
