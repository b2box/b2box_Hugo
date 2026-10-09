"""QA independiente de feat/semaforo-tiendas, criterio 6: rendimiento y memoria del matching contra tiendas.

Mide (con `pytest -s` se imprimen los números):

  * prefiltro por nombre con 22.000 títulos (el catálogo de Gadnic entero) y K = 6;
  * memoria del índice en RAM y lo que cuesta cargarlo desde la base;
  * el trabajo de CPU + base de datos del matching de un producto contra las dos tiendas, a escala de 1.800 productos
    con la concurrencia de la corrida (CLIP y el juez son dobles: su tiempo de red se estima aparte en el informe).

Los umbrales son holgados a propósito (no es un benchmark): detectan una regresión grande, no ruido de la máquina.
"""

from __future__ import annotations

import asyncio
import functools
import os
import random
import statistics
import time
import tracemalloc

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlalchemy import insert  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app import runtime  # noqa: E402
from app.clock import utcnow  # noqa: E402
from app.db.models import MarketStore, StoreCatalogItem, StoreMatch  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import market_match, price_monitor, store_catalog, store_match  # noqa: E402
from tests.store_fixtures import store_db  # noqa: E402,F401  (fixture)
from tests.test_price_monitor import _product  # noqa: E402

NOUNS = ("organizador", "cortina", "set", "taza", "botella", "lampara", "cable", "cargador", "funda", "soporte", "auricular",
         "teclado", "mouse", "parlante", "reloj", "mochila", "valija", "ventilador", "calefactor", "pava", "licuadora", "batidora",
         "tostadora", "cafetera", "sarten", "olla", "cuchillo", "tabla", "frasco", "caja", "estante", "silla", "mesa", "espejo",
         "alfombra", "almohada", "sabana", "toalla", "campera", "zapatilla", "remera", "gorra", "termo", "mate", "bombilla")
ADJS = ("plegable", "inalambrico", "recargable", "digital", "led", "portatil", "reforzado", "magnetico", "antideslizante", "termico",
        "acero", "silicona", "bambu", "vidrio", "plastico", "negro", "blanco", "rojo", "azul", "gris", "premium", "mini", "grande",
        "universal", "automatico", "bluetooth", "usb", "tipo", "c", "pro", "plus", "classic", "smart", "ultra", "slim")
BRANDS = ("gadnic", "philco", "bgh", "stanley", "samsung", "xiaomi", "")


def title(rng: random.Random) -> str:
    words = [rng.choice(NOUNS)] + [rng.choice(ADJS) for _ in range(rng.randint(2, 5))]
    if rng.random() < 0.3:
        words.append(f"{rng.choice((250, 500, 750, 1, 2, 3))} {rng.choice(('ml', 'l', 'cm', 'w'))}")
    if rng.random() < 0.4:
        words.append(rng.choice(BRANDS))
    return " ".join(w for w in words if w).capitalize()


def make_index(n: int, seed: int = 1, name: str = "Gadnic") -> store_match.StoreIndex:
    rng = random.Random(seed)
    info = store_catalog.StoreInfo(1, name, "https://www.gadnic.com.ar", "jsonld_sitemap", 11, 2000, "",
                                   ("gadnic.com.ar", "*.bidcom.com.ar"), "Gadnic")
    entries = [store_match.CatalogEntry(
        id=i + 1, url=f"https://www.gadnic.com.ar/categoria-{i % 90}/{'-'.join(title(rng).lower().split())}-{i}",
        title=(t := title(rng)), price_cents=rng.randint(150_000, 9_000_000), price_doubtful=False, price_note="",
        image_url=f"https://static.bidcom.com.ar/publicacionesML/productos/P{i}/1000x1000-P{i}.jpg", brand="", stock=3)
        for i in range(n)]
    return store_match.StoreIndex(info=info, entries=entries)


def test_prefilter_over_22000_titles_is_fast_and_the_index_fits_in_memory():
    tracemalloc.start()
    t0 = time.perf_counter()
    idx = make_index(22_000)
    build_s = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rng = random.Random(99)
    queries = [title(rng) for _ in range(300)]
    idx.prefilter(queries[0])                                           # calentamiento
    times = []
    for q in queries:
        t = time.perf_counter()
        got = idx.prefilter(q)
        times.append(time.perf_counter() - t)
        assert len(got) == 6
    times.sort()
    p50, p95 = statistics.median(times), times[int(len(times) * 0.95)]
    print(f"\nprefiltro 22.000 títulos: p50 {p50*1000:.1f} ms, p95 {p95*1000:.1f} ms, máx {times[-1]*1000:.1f} ms; "
          f"armar índice (normalizar) {build_s:.2f} s, memoria pico {peak/1e6:.1f} MB")
    assert p95 < 0.30 and peak < 150e6


async def test_prefilter_runs_off_the_event_loop_so_a_burst_of_products_does_not_freeze_it():
    """El prefiltro va a un thread: mientras 8 productos lo usan en paralelo, el loop sigue respondiendo (latidos < 200 ms)."""
    idx = make_index(22_000)
    rng = random.Random(5)
    queries = [title(rng) for _ in range(64)]
    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat():
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last - 0.01)
            last = now

    hb = asyncio.create_task(heartbeat())
    sem = asyncio.Semaphore(8)

    async def one(q):
        async with sem:
            await asyncio.to_thread(idx.prefilter, q, 6)

    t = time.perf_counter()
    await asyncio.gather(*(one(q) for q in queries))
    wall = time.perf_counter() - t
    stop.set()
    await hb
    print(f"\n64 prefiltros en paralelo (8 hilos): {wall:.2f} s ({wall/64*1000:.0f} ms c/u), peor demora del loop {max(gaps)*1000:.0f} ms")
    assert max(gaps) < 0.25


def test_loading_22000_indexed_products_from_the_database(store_db):
    store_catalog.seed_default_stores()
    rng = random.Random(3)
    with Session(engine) as s:
        sid = s.exec(MarketStore.__table__.select().where(MarketStore.name == "Gadnic")).first().id
        now = utcnow()
        s.execute(insert(StoreCatalogItem), [dict(
            store_id=sid, url=f"https://www.gadnic.com.ar/c/{i}-{'-'.join(title(rng).lower().split())}", title=title(rng),
            sku=f"S{i}", price_cents=rng.randint(150_000, 9_000_000), price_doubtful=False, price_note=None,
            image_url=f"https://static.bidcom.com.ar/publicacionesML/productos/P{i}/1000x1000-P{i}.jpg", brand=None, stock=3,
            first_seen_at=now, last_seen_at=now, last_checked_at=now, dead=False, fails=0, in_sitemap=True) for i in range(22_000)])
        s.commit()
    info = store_catalog.get_store(sid)
    tracemalloc.start()
    t = time.perf_counter()
    idx = store_match.load_index(info)
    secs = time.perf_counter() - t
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"\nload_index(22.000 filas, SQLite): {secs:.2f} s, memoria pico {peak/1e6:.1f} MB, {len(idx.entries)} cargados")
    assert len(idx.entries) == 22_000 and secs < 10 and peak < 400e6


async def test_cpu_and_db_cost_of_matching_a_product_against_both_stores_at_the_scale_of_the_nightly_run(store_db, monkeypatch):
    """400 productos × (Gadnic 22.000 + Casa Perfecta 150) con la concurrencia de la corrida (4). Mide el tiempo propio
    del matching (prefiltro + puntaje + reglas + guardado), sin red. Se extrapola a 1.800."""
    store_catalog.seed_default_stores()
    runtime.set_value("pm_stores_topup_minutes", 0)

    async def clip(our, urls):
        return 0.55 + (hash((our.id, urls[0])) % 40) / 100 if urls else None

    monkeypatch.setattr(market_match, "clip_score_urls", clip)
    gd, cp = make_index(22_000, 1), make_index(150, 2, "Casa Perfecta")
    cp.info = store_catalog.StoreInfo(2, "Casa Perfecta", "https://www.casaperfecta.com.ar", "tiendanube", 7, 1000, "",
                                      ("casaperfecta.com.ar", "acdn*.mitiendanube.com"), "")
    run = store_match.StoresRun(stores=[gd, cp], feedback={}, affect_color=False)
    ctx = price_monitor._context(1, None)
    rng = random.Random(11)
    products = [_product(str(i), title(rng)) for i in range(400)]

    async def judge(*a, **kw):
        return None

    sem = asyncio.Semaphore(4)

    async def one(p):
        async with sem:
            query = market_match.search_query(p.name)
            rows: list[StoreMatch] = []
            for st in run.stores:
                rows += await store_match._match_store(run, st, ctx, p, query, None, 10_000, judge, price_monitor._declared_brand)
            await asyncio.to_thread(store_match.save_matches, 1, p.id, rows)
            return len(rows)

    t = time.perf_counter()
    counts = await asyncio.gather(*(one(p) for p in products))
    wall = time.perf_counter() - t
    per_product = wall / len(products) * 4                               # segundos de slot ocupado por producto (concurrencia 4)
    print(f"\nmatching de 400 productos × 2 tiendas: {wall:.1f} s en total; {per_product*1000:.0f} ms de slot por producto; "
          f"1.800 productos ≈ {wall/400*1800/60:.1f} min de pared con concurrencia 4 (solo CPU + base, sin red ni CLIP)")
    assert set(counts) == {12}
    assert per_product < 1.0


# ─── calidad del prefiltro: ¿la ficha buena entra entre los 6 candidatos? ───


@functools.lru_cache(maxsize=1)
def _shared_index() -> store_match.StoreIndex:
    return make_index(22_000, 1)                                        # solo lectura: se arma una vez para los 6 casos


def _perturb(t: str, kind: str, rng: random.Random) -> str:
    w = t.split()
    if kind == "orden":
        rng.shuffle(w)
    elif kind == "extra":
        w += rng.sample(["envio", "gratis", "oferta", "nuevo", "original", "importado", "garantia"], 2)
    elif kind == "faltan" and len(w) > 4:
        for _ in range(2):
            w.pop(rng.randrange(1, len(w)))
    elif kind == "plural":
        w = [x + "s" if x.isalpha() and len(x) > 3 and not x.endswith("s") else x for x in w]
    elif kind == "marca":
        w = ["Gadnic"] + w
    elif kind == "sinonimo":
        w = [x.replace("inalambrico", "wireless").replace("recargable", "bateria").replace("grande", "xl") for x in w]
    return " ".join(w)


@pytest.mark.parametrize("kind", ["orden", "extra", "faltan", "plural", "marca", "sinonimo"])
def test_the_right_item_is_among_the_six_candidates_when_the_title_is_written_differently(kind):
    """Sobre 22.000 títulos parecidos entre sí (vocabulario chico a propósito: peor caso). El mismo producto escrito con otro
    orden, palabras de más o de menos, plurales, la marca adelante o un sinónimo tiene que quedar entre los 6 del prefiltro."""
    idx = _shared_index()
    rng = random.Random(42)
    n = hit = 0
    for _ in range(40):
        target = idx.entries[rng.randrange(len(idx.entries))]
        got = idx.prefilter(_perturb(target.title, kind, rng), 6)
        n += 1
        hit += any(e.id == target.id or e.title == target.title for e in got)
    print(f"\nprefiltro recall@6 [{kind}]: {hit}/{n}")
    assert hit / n >= 0.94, f"{hit}/{n}"
