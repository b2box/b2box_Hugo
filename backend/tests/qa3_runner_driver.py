"""Arranca el runner REAL de la Mac (`tools/oficina_ml_search.py main()`, con su propio parseo de argumentos, config, señales,
lotes y salida) con DOS reemplazos, nada más:

  * el navegador (Camoufox) por un cliente HTTP a un "ML" local (`QA3_ML_PORT`): el servidor local decide qué página, qué
    status y qué URL final (`x-final-url`) devuelve, o si el navegador "falla" (`x-raise`);
  * `asyncio.sleep` por una espera casi nula que ANOTA cuánto se le pidió dormir (`QA3_SLEEP_LOG`), para no esperar 8-15 s
    por búsqueda y poder afirmar que el ritmo es el de verdad.

Solo se usa desde los tests de QA (test_qa3_oficina_runner.py); no es código de producción.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

SLEEP_LOG = os.environ.get("QA3_SLEEP_LOG", "")
HIT_SLEEP_S = float(os.environ.get("QA3_REAL_WAIT", "0"))
_real_sleep = asyncio.sleep


async def _recording_sleep(delay, result=None):
    if delay and delay >= 1.0 and SLEEP_LOG:
        with open(SLEEP_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{delay}\n")
        return await _real_sleep(HIT_SLEEP_S, result)
    return await _real_sleep(delay, result)


asyncio.sleep = _recording_sleep          # ANTES de importar el runner: lo toma como valor por defecto del Searcher

from tools import oficina_ml_search as runner  # noqa: E402

_original_prepare = runner.prepare_environment


class LocalBrowser:
    def __init__(self, **_kw) -> None:
        pass

    async def fetch(self, url: str):
        import httpx

        from app.ingest.browser_fetch import ListingPage

        slug = url.rsplit("/", 1)[-1]
        async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
            resp = await client.get(f"http://127.0.0.1:{os.environ['QA3_ML_PORT']}/{slug}")
        if resp.headers.get("x-raise") == "timeout":
            raise TimeoutError("el navegador no cargó la página")
        final = resp.headers.get("x-final-url") or f"https://listado.mercadolibre.com.ar/{slug}"
        return ListingPage(html=resp.text, final_url=final, status=resp.status_code, bytes=len(resp.content), elapsed_s=0.01)

    async def close(self) -> None:
        pass


SKEW_MIN = float(os.environ.get("QA3_CLOCK_SKEW_MIN", "0"))
if SKEW_MIN:
    import datetime as _dt

    class _SkewedDatetime(_dt.datetime):
        """El reloj de la Mac adelantado (o atrasado) SKEW_MIN minutos respecto del de Hugo."""

        @classmethod
        def now(cls, tz=None):
            return super().now(tz) + _dt.timedelta(minutes=SKEW_MIN)

    runner.datetime = _SkewedDatetime


def _prepare() -> None:
    _original_prepare()
    from app.ingest import browser_fetch as bf

    bf.ListingBrowser = LocalBrowser
    bf.available = lambda: True


runner.prepare_environment = _prepare
runner.SEND_BACKOFF_S = (0.05, 0.05, 0.05)          # los reintentos contra Hugo, sin esperar 5 y 15 s de verdad

if __name__ == "__main__":
    sys.exit(runner.main(sys.argv[1:]))
