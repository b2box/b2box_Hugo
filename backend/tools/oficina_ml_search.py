#!/usr/bin/env python
"""Buscador de la oficina: busca en Mercado Libre desde la Mac de la oficina y se lo manda a Hugo.

Desde el servidor ML contesta con captcha a la IP del proxy residencial; desde la conexión de la oficina
responde la búsqueda normal. Esta Mac busca DE A POCO (una página cada 8-15 s, sin proxy, sin paralelismo) lo
que Hugo le pide, y Hugo hace el matching. Nada de esto esquiva un bloqueo: al PRIMER captcha o bloqueo se
frena la noche entera, se lo avisa a Hugo y no se reintenta ni se cambia nada.

Uso (con el venv del repo, desde cualquier carpeta):

    backend/.venv/bin/python backend/tools/oficina_ml_search.py --init            # una sola vez: genera la key
    backend/.venv/bin/python backend/tools/oficina_ml_search.py --check          # configuración y conexión, sin buscar
    backend/.venv/bin/python backend/tools/oficina_ml_search.py --dry-run --max 3  # busca pero no manda nada
    backend/.venv/bin/python backend/tools/oficina_ml_search.py --max 50         # el piloto
    backend/.venv/bin/python backend/tools/oficina_ml_search.py                  # la noche (hasta 250 productos)

Configuración: ~/.config/b2box-bench/.env (permisos 600) con OFICINA_SEARCH_KEY y HUGO_URL. La key NUNCA se
imprime ni se loguea. Ver el README ("Buscador de la oficina") para la instalación con launchd.

Una sola página de ML por producto por noche (la consulta que Hugo le da); una sola instancia a la vez (candado en
~/.config/b2box-bench/oficina-ml-search.lock).

Códigos de salida: 0 bien · 2 configuración o autenticación · 3 ML bloqueó (la noche se frenó) · 4 no se pudieron
enviar los resultados, o Hugo los rechazó · 5 el navegador no anda o demasiados errores seguidos · 6 ya hay otra
corrida en marcha.
"""

from __future__ import annotations

import argparse
import asyncio
import email.utils
import fcntl
import logging
import logging.handlers
import os
import random
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ENV = Path.home() / ".config" / "b2box-bench" / ".env"
DEFAULT_LOG = Path.home() / "Library" / "Logs" / "b2box-oficina-ml-search.log"
DEFAULT_LOCK = Path.home() / ".config" / "b2box-bench" / "oficina-ml-search.lock"
KEY_VAR, URL_VAR = "OFICINA_SEARCH_KEY", "HUGO_URL"

DEFAULT_MAX = 250
MAX_PRODUCTS = 500                 # lo máximo que Hugo entrega en la cola
BATCH_SIZE = 10
# Pausa entre dos búsquedas: al azar entre estos valores. El mínimo no se puede bajar por línea de comandos.
PAUSE_MIN_S, PAUSE_MAX_S = 8.0, 15.0
# Errores de lectura seguidos (página ilegible, navegador caído) que frenan la noche. No son bloqueos.
ERROR_STREAK = 5
# UNA página de ML por producto por noche: la primera consulta que da Hugo. Si vino vacía, Hugo da la siguiente la noche siguiente.
MAX_QUERIES_PER_PRODUCT = 1
SEND_ATTEMPTS = 3
SEND_BACKOFF_S = (5.0, 15.0, 45.0)
RETRY_AFTER_CAP_S = 120.0

EXIT_OK, EXIT_CONFIG, EXIT_BLOCKED, EXIT_SEND, EXIT_BROWSER, EXIT_BUSY = 0, 2, 3, 4, 5, 6

log = logging.getLogger("oficina_ml_search")

_PRODUCT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$", re.ASCII)
_MIN_KEY_LEN = 24


class ConfigError(Exception):
    """Algo de la configuración local está mal (el mensaje dice cómo arreglarlo)."""


class HugoError(Exception):
    """Hugo contestó algo que no se arregla reintentando (key mala, endpoint apagado…)."""


class SendFailed(Exception):
    """No se pudo hablar con Hugo después de los reintentos."""


class AlreadyRunning(Exception):
    """Hay otra corrida del buscador en marcha."""


# ─── Configuración local ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Config:
    key: str = field(repr=False)          # repr=False: la key no sale ni en un traceback
    hugo_url: str


def normalize_hugo_url(raw: str) -> str:
    """https://host (o http solo para localhost: la key viaja en un header)."""
    url = (raw or "").strip().rstrip("/")
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        _ = parts.port          # accederlo valida el puerto (ValueError si es raro)
    except ValueError:
        raise ConfigError(f"{URL_VAR} no es una URL válida") from None
    local = host in ("localhost", "127.0.0.1", "::1")
    if parts.scheme == "https" or (parts.scheme == "http" and local):
        pass
    else:
        raise ConfigError(f"{URL_VAR} tiene que ser https://… (http solo para localhost): la key viaja en cada pedido")
    if not host or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ConfigError(f"{URL_VAR} tiene que ser solo el dominio de Hugo, sin usuario, ruta ni parámetros")
    return f"{parts.scheme}://{parts.netloc}"


def _check_permissions(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigError(f"{path} tiene permisos {oct(mode)[2:]}: lo puede leer otra gente de la Mac. "
                          f"Corré: chmod 600 {path}")


def load_config(env_path: Path) -> Config:
    from dotenv import dotenv_values

    if not env_path.exists():
        raise ConfigError(f"No existe {env_path}. Corré --init para crear la key.")
    _check_permissions(env_path)
    if stat.S_IMODE(env_path.parent.stat().st_mode) & 0o077:
        log.warning("la carpeta %s la puede recorrer otra gente de la Mac (tiene otras keys adentro): chmod 700 %s",
                    env_path.parent, env_path.parent)
    values = dotenv_values(env_path)
    key = (values.get(KEY_VAR) or "").strip()
    if not key:
        raise ConfigError(f"Falta {KEY_VAR} en {env_path}. Corré --init para generarla.")
    if len(key) < _MIN_KEY_LEN:
        raise ConfigError(f"{KEY_VAR} es muy corta (mínimo {_MIN_KEY_LEN} caracteres)")
    if not (values.get(URL_VAR) or "").strip():
        raise ConfigError(f"Falta {URL_VAR} en {env_path} (por ejemplo {URL_VAR}=https://hugo.b2box.pro)")
    return Config(key=key, hugo_url=normalize_hugo_url(values[URL_VAR] or ""))


def init_key(env_path: Path) -> str:
    """Genera OFICINA_SEARCH_KEY y la guarda en el .env si no existe. Devuelve el texto para la persona; NO
    contiene la key."""
    from dotenv import dotenv_values

    existing = dotenv_values(env_path) if env_path.exists() else {}
    if (existing.get(KEY_VAR) or "").strip():
        if env_path.exists():
            os.chmod(env_path, 0o600)
        return (f"Ya hay una {KEY_VAR} en {env_path}: no toqué nada.\n"
                f"Si hay que rotarla, borrá esa línea del archivo y volvé a correr --init.")
    created_dir = not env_path.parent.exists()
    env_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if created_dir:
        os.chmod(env_path.parent, 0o700)          # (mkdir respeta el umask: se fuerza)
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    # Una línea vacía (OFICINA_SEARCH_KEY=) se reemplaza: no se deja un duplicado.
    lines = [ln for ln in lines if not re.match(rf"^\s*(export\s+)?{KEY_VAR}\s*=", ln)]
    lines.append(f"{KEY_VAR}={secrets.token_urlsafe(32)}")
    missing_url = not (existing.get(URL_VAR) or "").strip()
    if missing_url and not any(ln.lstrip().startswith(f"# {URL_VAR}") for ln in lines):
        lines.append(f"# {URL_VAR}=https://dominio-de-hugo  (sacá el # y poné el dominio)")
    fd, tmp = tempfile.mkstemp(dir=env_path.parent, prefix=".env-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, env_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    steps = [
        f"Listo: generé una {KEY_VAR} nueva y la guardé en {env_path} (permisos 600). No la imprimo a propósito.",
        "",
        "Siguiente:",
        f"  1. Abrí el archivo (en la Terminal: open -e {env_path}) y copiá el valor de {KEY_VAR}.",
        f"  2. En Coolify, aplicación Hugo → Environment Variables → agregá {KEY_VAR} con ese valor → Redeploy.",
    ]
    if missing_url:
        steps.append(f"  3. En el mismo archivo poné {URL_VAR}=https://<dominio de Hugo> (sacá el # de esa línea).")
    steps.append("Después: --check para probar la conexión.")
    return "\n".join(steps)


# ─── Una sola instancia ────────────────────────────────────────────


def acquire_lock(path: Path):
    """Candado de instancia única (flock): un piloto manual a la hora del launchd, o dos Macs con el mismo archivo de
    configuración, buscarían los MISMOS productos al doble de ritmo desde la misma IP. Devuelve el archivo abierto (el candado
    vive mientras el proceso; se suelta solo al terminar, también si muere)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(fd, "r+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        owner = handle.read().strip()
        handle.close()
        raise AlreadyRunning(f"Ya hay otra corrida del buscador de la oficina en marcha{f' (PID {owner})' if owner.isdigit() else ''}: "
                             f"esta no arranca para no duplicar las búsquedas desde la misma IP.") from None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


# ─── Hugo ──────────────────────────────────────────────────────────


class HugoClient:
    """Los dos endpoints de Hugo. Sin seguir redirects (la key no viaja a otro host) y sin loguear headers."""

    def __init__(self, config: Config, *, client: Any = None, sleep: Callable[[float], None] = time.sleep) -> None:
        import httpx

        self._httpx = httpx
        self._sleep = sleep
        self._client = client or httpx.Client(
            base_url=config.hugo_url, follow_redirects=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"x-oficina-key": config.key, "user-agent": "b2box-oficina-ml-search/1"})
        # Cuánto adelanta (+) o atrasa (-) el reloj de Hugo respecto del de esta Mac, según el header Date de sus respuestas.
        self.clock_offset = timedelta(0)

    def close(self) -> None:
        self._client.close()

    def now_iso(self) -> str:
        """La hora para `fetched_at`: la de Hugo. Con el reloj de la Mac corrido más de unos minutos Hugo rechazaría todo
        por "fecha en el futuro"."""
        return (datetime.now(timezone.utc) + self.clock_offset).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _learn_clock(self, resp) -> None:
        try:
            hugo = email.utils.parsedate_to_datetime(resp.headers.get("date", ""))
        except (TypeError, ValueError):
            return
        if hugo.tzinfo is None:
            hugo = hugo.replace(tzinfo=timezone.utc)
        offset = hugo - datetime.now(timezone.utc)
        if abs(offset) > timedelta(seconds=90) and abs(offset - self.clock_offset) > timedelta(seconds=90):
            log.warning("el reloj de esta Mac está %s minutos %s que el de Hugo: se usa la hora de Hugo para fetched_at",
                        round(abs(offset.total_seconds()) / 60, 1), "atrasado" if offset > timedelta(0) else "adelantado")
        self.clock_offset = offset

    def _call(self, method: str, path: str, **kw: Any):
        try:
            return self._client.request(method, path, **kw)
        except self._httpx.HTTPError as exc:
            raise SendFailed(f"no se pudo hablar con Hugo ({type(exc).__name__})") from None

    @staticmethod
    def _fatal(resp) -> None:
        reasons = {401: "Hugo rechazó la key (401): revisá que OFICINA_SEARCH_KEY sea la misma que en Coolify",
                   404: "Hugo no tiene el buscador de la oficina prendido (404): falta OFICINA_SEARCH_KEY en Coolify "
                        "o el deploy",
                   413: "Hugo rechazó el lote por grande (413)", 400: "Hugo no entendió el pedido (400)",
                   422: "Hugo rechazó los parámetros (422)"}
        raise HugoError(reasons.get(resp.status_code, f"Hugo contestó {resp.status_code}"))

    def _request(self, method: str, path: str, **kw: Any):
        """Un pedido con reintentos SOLO para lo transitorio (red caída, 5xx de un redeploy, 429 con su Retry-After):
        SEND_ATTEMPTS intentos con espera creciente. Lo demás (key mala, endpoint apagado) no se reintenta."""
        last = ""
        for attempt in range(SEND_ATTEMPTS):
            try:
                resp = self._call(method, path, **kw)
            except SendFailed as exc:
                last = str(exc)
            else:
                if resp.status_code == 200:
                    self._learn_clock(resp)
                    return resp
                if resp.status_code not in (429, 500, 502, 503, 504):
                    self._fatal(resp)
                last = f"Hugo contestó {resp.status_code}"
                retry_after = resp.headers.get("retry-after", "")
                if resp.status_code == 429 and retry_after.isdigit():
                    if attempt + 1 < SEND_ATTEMPTS:
                        self._sleep(min(float(retry_after), RETRY_AFTER_CAP_S))
                    continue
            if attempt + 1 < SEND_ATTEMPTS:
                self._sleep(SEND_BACKOFF_S[min(attempt, len(SEND_BACKOFF_S) - 1)])
        raise SendFailed(last or "no se pudo hablar con Hugo")

    @staticmethod
    def _json(resp) -> Any:
        try:
            return resp.json()
        except ValueError:
            raise HugoError("Hugo contestó algo que no es JSON") from None

    def queue(self, limit: int) -> dict[str, Any]:
        data = self._json(self._request("GET", "/api/oficina/ml-queue", params={"limit": limit}))
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise HugoError("Hugo contestó una cola con otra forma")
        return data

    def send(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        """Un lote (idempotente por producto y fetched_at, así que reintentarlo no duplica nada)."""
        data = self._json(self._request("POST", "/api/oficina/ml-results", json={"results": results}))
        return data if isinstance(data, dict) else {}


# ─── La búsqueda ───────────────────────────────────────────────────


@dataclass(slots=True)
class Stats:
    products: int = 0
    searches: int = 0
    ok: int = 0
    empty: int = 0
    blocked: int = 0
    error: int = 0
    candidates: int = 0
    sent: int = 0
    duplicates: int = 0
    rejected: int = 0
    lost: int = 0

    def line(self) -> str:
        return (f"{self.products} productos, {self.searches} búsquedas ({self.ok} con resultados, {self.empty} vacías, "
                f"{self.blocked} bloqueadas, {self.error} con error), {self.candidates} publicaciones; "
                f"Hugo: {self.sent} guardados, {self.duplicates} repetidos, {self.rejected} rechazados, {self.lost} sin enviar")


Fetcher = Callable[[str], Awaitable[Any]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clean_item(raw: Any) -> tuple[str, list[str]] | None:
    """Un item de la cola, validado: Hugo es de confianza pero la forma se chequea igual."""
    if not isinstance(raw, dict):
        return None
    pid, queries = raw.get("product_id"), raw.get("queries")
    if not isinstance(pid, str) or not _PRODUCT_ID.fullmatch(pid) or not isinstance(queries, list):
        return None
    clean = [q.strip()[:200] for q in queries if isinstance(q, str) and q.strip()]
    return (pid, clean[:MAX_QUERIES_PER_PRODUCT]) if clean else None


class Searcher:
    """Una noche de búsquedas. Las dependencias (navegador, Hugo, reloj) se inyectan para poder probarla
    sin navegador, sin red y sin esperar."""

    def __init__(self, *, fetch: Fetcher, hugo: HugoClient | None, max_results: int = 8, batch_size: int = BATCH_SIZE,
                 pause_min: float = PAUSE_MIN_S, pause_max: float = PAUSE_MAX_S, dry_run: bool = False,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, rng: random.Random | None = None,
                 now: Callable[[], str] | None = None) -> None:
        from app.ingest import browser_fetch
        from app.pricing import market_ml_web, oficina_ml

        self._bf, self._mlw, self._ofi = browser_fetch, market_ml_web, oficina_ml
        self._fetch, self._hugo = fetch, hugo
        self.max_results, self.batch_size, self.dry_run = max(1, int(max_results)), max(1, int(batch_size)), dry_run
        self.pause_min, self.pause_max = pause_min, pause_max
        self._sleep, self._rng = sleep, rng or random.Random()
        # fetched_at con la hora de HUGO (su header Date), no la de esta Mac: con el reloj corrido Hugo rechazaría todo.
        self._now = now or (hugo.now_iso if hugo is not None else _now_iso)
        self.stats = Stats()
        self.pending: list[dict[str, Any]] = []
        self.exit_code = EXIT_OK
        self._error_streak = 0
        self._stop = False

    # -- una página --

    async def _search_once(self, query: str) -> tuple[str, list[dict[str, Any]], str]:
        """(ok | empty | blocked | error, publicaciones, motivo). Nunca lanza."""
        url = self._mlw.search_url(query)
        if url is None:
            return "empty", [], "la consulta no tiene palabras para buscar"
        bf = self._bf
        try:
            page = await self._fetch(url)
        except bf.CircuitOpen:
            return "blocked", [], "el navegador se frenó por fallos seguidos"
        except bf.SsrfBlocked:
            return "error", [], "el navegador rechazó la URL"
        except Exception as exc:  # noqa: BLE001  (BrowserUnavailable, timeout, red)
            return "error", [], f"el navegador falló: {type(exc).__name__}: {str(exc)[:150]}"
        if not bf.url_host_allowed(page.final_url, self._mlw.ML_PAGE_HOSTS):
            return "blocked", [], "ML redirigió la búsqueda a otro sitio"
        parsed = self._mlw.parse_search(page.html, self.max_results)
        problem = self._mlw.page_problem(page, parsed)
        if problem is not None:
            return problem[0], [], problem[1]
        if not parsed.candidates:
            return "empty", [], "ML no tiene publicaciones para la consulta"
        return "ok", [self._ofi.candidate_to_wire(c) for c in parsed.candidates], ""

    async def _pace(self) -> None:
        if self.stats.searches > 0:
            await self._sleep(self._rng.uniform(self.pause_min, self.pause_max))

    # -- un producto --

    async def search_product(self, pid: str, queries: list[str]) -> dict[str, Any]:
        kind, cands, reason, used = "empty", [], "", queries[0]
        for query in queries:
            await self._pace()
            used = query
            kind, cands, reason = await self._search_once(query)
            self.stats.searches += 1
            if kind != "empty":
                break                      # ok, bloqueada o error: no se prueba otra consulta
        self.stats.products += 1
        setattr(self.stats, kind, getattr(self.stats, kind) + 1)
        self.stats.candidates += len(cands)
        if kind == "blocked":
            self._stop, self.exit_code = True, EXIT_BLOCKED
            log.error("ML bloqueó la búsqueda (producto %s): se frena la noche, sin reintentar. Motivo: %s", pid, reason)
        elif kind == "error":
            self._error_streak += 1
            log.warning("búsqueda con error (producto %s): %s", pid, reason)
            if self._error_streak >= ERROR_STREAK:
                self._stop, self.exit_code = True, EXIT_BROWSER
                log.error("%d errores seguidos: se frena la noche", self._error_streak)
        else:
            self._error_streak = 0
            log.info("producto %s: %s (%d publicaciones)", pid, kind, len(cands))
        return {"product_id": pid, "query": used, "fetched_at": self._now(), "status": kind, "reason": reason,
                "candidates": cands}

    # -- entrega --

    async def flush(self, *, force: bool = False) -> None:
        while self.pending and (force or len(self.pending) >= self.batch_size):
            batch, self.pending = self.pending[:self.batch_size], self.pending[self.batch_size:]
            if self.dry_run or self._hugo is None:
                log.info("dry-run: no se manda nada (%d resultados)", len(batch))
                continue
            try:
                report = await asyncio.to_thread(self._hugo.send, batch)
            except SendFailed as exc:
                self.stats.lost += len(batch)
                self.exit_code = self.exit_code or EXIT_SEND
                log.error("no se pudo enviar un lote de %d resultados: %s", len(batch), exc)
                continue
            except HugoError as exc:
                # Key mala, endpoint apagado, lote rechazado: reintentar no arregla nada. Se frena la noche.
                self.stats.lost += len(batch) + len(self.pending)
                self.pending = []
                self._stop, self.exit_code = True, EXIT_CONFIG
                log.error("Hugo rechazó el envío, se frena la noche: %s", exc)
                return
            self._account(batch, report)

    def _account(self, batch: list[dict[str, Any]], report: dict[str, Any]) -> None:
        """Anota lo que Hugo dijo de un lote. Que Hugo rechace TODO no es un "lote enviado": se avisa fuerte, la corrida
        termina con error y, si todos los rechazos tienen la misma causa (fecha en el futuro, key de otro entorno…), se frena
        la noche en vez de seguir gastando páginas de ML para tirarlas."""
        stored, duplicates = int(report.get("stored") or 0), int(report.get("duplicates") or 0)
        rejected = report.get("rejected") or []
        total_rejected = max(int(report.get("rejected_total") or 0), len(rejected))
        self.stats.sent += stored
        self.stats.duplicates += duplicates
        self.stats.rejected += total_rejected
        reasons = sorted({str(r.get("reason", ""))[:80] for r in rejected if isinstance(r, dict)})
        if total_rejected and stored + duplicates == 0:
            self.exit_code = self.exit_code or EXIT_SEND
            log.error("Hugo rechazó TODO el lote (%d resultados): %s", len(batch), "; ".join(reasons) or "sin motivo")
            if len(reasons) == 1 and len(batch) > 1:
                self._stop = True
                log.error("el motivo es el mismo para todos: se frena la noche")
        elif total_rejected:
            log.warning("lote enviado: %d guardados, %d repetidos, %d rechazados (%s)", stored, duplicates, total_rejected,
                        "; ".join(reasons[:3]) or "sin motivo")
        else:
            log.info("lote enviado: %d guardados, %d repetidos", stored, duplicates)

    async def run(self, items: list[Any], limit: int) -> int:
        todo = [c for c in (_clean_item(i) for i in items) if c][:limit]
        log.info("cola: %d productos para buscar", len(todo))
        try:
            for pid, queries in todo:
                if self._stop:
                    break
                self.pending.append(await self.search_product(pid, queries))
                await self.flush()
        finally:
            await self.flush(force=True)
        return self.exit_code


# ─── Entorno y navegador ───────────────────────────────────────────


def prepare_environment() -> None:
    """ANTES de importar nada de `app`: la Mac busca SIN proxy (forzado), sin base de datos y con el navegador
    prendido. Una variable de entorno le gana al .env del repo."""
    os.environ["BROWSER_PROXY"] = ""
    os.environ["BROWSER_FETCH_ENABLED"] = "true"
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))


def keep_awake() -> subprocess.Popen | None:
    """`caffeinate -i` atado a este proceso: la Mac no se duerme por inactividad mientras corre, y se suelta
    solo cuando termina (-w)."""
    exe = shutil.which("caffeinate")
    if sys.platform != "darwin" or not exe:
        return None
    return subprocess.Popen([exe, "-i", "-w", str(os.getpid())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class _PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """El log con permisos 600 (también el archivo nuevo después de rotar): no lo lee otra gente de la Mac."""

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(self.baseFilename, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding)


def setup_logging(log_file: Path | None, verbose: bool = False) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(_PrivateRotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers, force=True,
                        format="%(asctime)s %(levelname)s %(message)s")
    # httpx loguea cada pedido con la URL: sin la key, pero tampoco hace falta.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)


async def _run_real(searcher_kwargs: dict[str, Any], items: list[Any], limit: int) -> int:
    from app.ingest import browser_fetch

    if browser_fetch.proxy_configured():                       # defensa: nunca con proxy
        log.error("hay un proxy configurado y esta herramienta no lo usa: abortando")
        return EXIT_CONFIG
    if not browser_fetch.available():
        log.error("Camoufox no está instalado en este venv: uv sync --locked --extra dev --extra browser "
                  "(y python -m camoufox fetch)")
        return EXIT_BROWSER
    browser = browser_fetch.ListingBrowser(block_scripts=True)
    try:
        searcher = Searcher(fetch=browser.fetch, **searcher_kwargs)
        code = await searcher.run(items, limit)
    finally:
        await browser.close()
    log.info("fin: %s", searcher.stats.line())
    return code


# ─── Línea de comandos ─────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Busca en Mercado Libre desde la oficina y se lo manda a Hugo.")
    p.add_argument("--init", action="store_true", help="genera OFICINA_SEARCH_KEY en el .env local (si no existe)")
    p.add_argument("--check", action="store_true", help="prueba la configuración y la conexión con Hugo; no busca")
    p.add_argument("--dry-run", action="store_true", help="busca de verdad pero NO manda nada a Hugo")
    p.add_argument("--max", type=int, default=DEFAULT_MAX, metavar="N",
                   help=f"máximo de productos de esta corrida (default {DEFAULT_MAX}; el piloto usa 50)")
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE, metavar="N", help="productos por lote (default 10)")
    p.add_argument("--pause-min", type=float, default=PAUSE_MIN_S, help=f"pausa mínima en s (no baja de {PAUSE_MIN_S:g})")
    p.add_argument("--pause-max", type=float, default=PAUSE_MAX_S, help=f"pausa máxima en s (default {PAUSE_MAX_S:g})")
    p.add_argument("--env-file", type=Path, default=DEFAULT_ENV, help=f"default {DEFAULT_ENV}")
    p.add_argument("--log-file", type=Path, default=DEFAULT_LOG, help=f"default {DEFAULT_LOG}")
    p.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK, help=f"candado de instancia única (default {DEFAULT_LOCK})")
    p.add_argument("--no-caffeinate", action="store_true", help="no impedir que la Mac se duerma mientras corre")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.init:
        print(init_key(args.env_file))
        return EXIT_OK
    if not (1 <= args.max <= MAX_PRODUCTS):
        print(f"--max tiene que estar entre 1 y {MAX_PRODUCTS}", file=sys.stderr)
        return EXIT_CONFIG
    if args.pause_min < PAUSE_MIN_S or args.pause_max < args.pause_min:
        print(f"La pausa mínima no puede ser menor a {PAUSE_MIN_S:g} s ni mayor que la máxima", file=sys.stderr)
        return EXIT_CONFIG
    setup_logging(args.log_file, args.verbose)
    try:
        config = load_config(args.env_file)
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG
    lock = None
    if not args.check:                      # (--check no busca: puede correr con una corrida en marcha)
        try:
            lock = acquire_lock(args.lock_file)
        except AlreadyRunning as exc:
            log.error("%s", exc)
            return EXIT_BUSY
    prepare_environment()
    # launchd (o el apagado de la Mac) corta con SIGTERM: se trata como Ctrl+C, así lo que ya se buscó se entrega.
    previous_sigterm = signal.signal(signal.SIGTERM, signal.default_int_handler)
    hugo = HugoClient(config)
    try:
        try:
            queue = hugo.queue(1 if args.check else args.max)
        except (HugoError, SendFailed) as exc:
            log.error("%s", exc)
            return EXIT_CONFIG if isinstance(exc, HugoError) else EXIT_SEND
        if args.check:
            log.info("OK: configuración válida y Hugo acepta la key (%d producto(s) en la cola de prueba)",
                     len(queue["items"]))
            return EXIT_OK
        if not queue["items"]:
            log.info("la cola está vacía: no hay nada para buscar")
            return EXIT_OK
        awake = None if args.no_caffeinate else keep_awake()
        try:
            return asyncio.run(_run_real(
                dict(hugo=hugo, max_results=int(queue.get("max_results") or 8), batch_size=args.batch_size,
                     pause_min=args.pause_min, pause_max=args.pause_max, dry_run=args.dry_run),
                queue["items"], args.max))
        finally:
            if awake is not None:
                awake.terminate()
    except KeyboardInterrupt:
        log.warning("interrumpido (Ctrl+C o apagado): lo que ya estaba buscado se entregó")
        return EXIT_OK
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        hugo.close()
        if lock is not None:
            lock.close()                    # suelta el candado


if __name__ == "__main__":
    sys.exit(main())
