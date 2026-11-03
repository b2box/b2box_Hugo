"""QA de Google Lens en el semáforo (feat/semaforo-google-lens): bordes que test_ml_lens.py no cubre.

Sin red: el mismo mundo falso de test_ml_lens.py (MockTransport de httpx para Apify y www.google.com/goto). Los
defectos que encontró QA (D1, D2) ya están arreglados y sus tests corren como tests normales.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from app import auth, runtime, security
from app import main as main_mod
from app.clock import utcnow
from app.config import Settings
from app.db.models import AuditLog, MlWebResult, Setting
from app.db.session import engine
from app.pricing import apify_ml_search as ams
from app.pricing import daily_budget, market_query, ml_lens, oficina_ml
from tests.test_apify_ml_search import TOKEN, env  # noqa: F401
from tests.test_ml_lens import (  # noqa: F401
    LONG, World, _eligible, _lens_item, _lens_row, _lens_rows, _ml_match, _snap, _text, lens,  # lens: fixture
)

GOTO = "https://www.google.com/goto?url=CAESxyz"


class PathWorld(World):
    """World que además anota método y path de cada request (para ver que ni se lee el actor de Lens)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.paths: list[tuple[str, str]] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append((request.method, request.url.path))
        return await super().handler(request)


def _next_day() -> None:
    """La noche siguiente: los contadores del día vuelven a cero (el formato es "AAAA-MM-DD:N")."""
    with Session(engine) as s:
        s.execute(delete(Setting).where(Setting.key.in_([ml_lens.COUNTER_KEY, ams.COUNTER_KEY])))  # type: ignore[attr-defined]
        s.commit()
    runtime.invalidate()


# ─── 1. Apagado ────────────────────────────────────────────────────


@pytest.mark.usefixtures("lens")
async def test_qa_apagado_ni_lee_el_actor_de_lens_ni_toca_su_cupo_y_ml_busca_texto(monkeypatch):
    _eligible(2)
    _lens_row("200", ["consulta pendiente vieja"], ago=timedelta(hours=30))
    _snap("400", "Mochila urbana impermeable")                                # nunca buscado: ML lo busca igual
    runtime.set_value("pm_lens_enabled", 0)
    world = PathWorld().install(monkeypatch)
    report = await ams.run_search()
    assert not any("google-lens" in path for _, path in world.paths), "con Lens apagado no se le pide nada al actor"
    assert world.google == [] and set(world.hosts) == {"api.apify.com"}
    assert daily_budget.used_today(ml_lens.COUNTER_KEY) == 0
    assert world.ml_queries == [market_query.query_variants("Mochila urbana impermeable", 3)[0]]
    assert report.lens_searches == 0 and report.lens["status"] == "disabled"
    with Session(engine) as s:
        assert s.exec(select(MlWebResult).where(MlWebResult.origin == "lens")).all() == []


# ─── 2. Tope diario ────────────────────────────────────────────────


@pytest.mark.usefixtures("lens")
async def test_qa_dos_pasos_lens_a_la_vez_no_pasan_el_tope(monkeypatch):
    """La reserva es por compare-and-swap: dos pasos en paralelo (dos workers) que ven "quedan 3" no lensean 6."""
    _eligible(6)
    runtime.set_value("pm_lens_daily_max", 3)
    world = World().install(monkeypatch)
    http = ams._new_client(10)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    a, b = await asyncio.gather(ml_lens.run_step(http, headers), ml_lens.run_step(http, headers))
    await http.aclose()
    assert sum(len(body["image_urls"]) for _, body in world.lens_starts) == 3
    assert daily_budget.used_today(ml_lens.COUNTER_KEY) == 3
    assert a.products + b.products == 3 and "over_budget" in (a.status, b.status)


@pytest.mark.usefixtures("lens")
async def test_qa_tres_corridas_el_mismo_dia_no_pasan_el_tope(monkeypatch):
    _eligible(6)
    runtime.set_value("pm_lens_daily_max", 4)
    world = World().install(monkeypatch)
    reports = [await ams.run_search() for _ in range(3)]
    assert sum(len(body["image_urls"]) for _, body in world.lens_starts) == 4
    assert [r.lens["status"] for r in reports] == ["ok", "over_budget", "over_budget"]


@pytest.mark.usefixtures("lens")
async def test_qa_si_falla_solo_la_reserva_de_lens_no_se_lensea_y_ml_sigue(monkeypatch):
    _eligible(2)
    _snap("400", "Mochila urbana impermeable")
    real = daily_budget._reserve_cas

    def flaky(key, *a, **k):
        if key == ml_lens.COUNTER_KEY:
            return None                                                        # lo que devuelve con la DB caída
        return real(key, *a, **k)

    monkeypatch.setattr(daily_budget, "_reserve_cas", flaky)
    world = World().install(monkeypatch)
    report = await ams.run_search()
    assert world.lens_starts == [] and world.google == [] and _lens_rows() == {}
    assert report.lens["status"] == "over_budget" and report.lens["products"] == 0
    assert world.ml_queries == [market_query.query_variants("Mochila urbana impermeable", 3)[0]]


# ─── 3. Resolver los goto ──────────────────────────────────────────


def _resolver(handler) -> tuple[ml_lens._Resolver, list[httpx.Request], httpx.AsyncClient]:
    seen: list[httpx.Request] = []

    async def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return await handler(request) if asyncio.iscoroutinefunction(handler) else handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return ml_lens._Resolver(http), seen, http


@pytest.mark.parametrize("location, accepted", [
    ("https://www.mercadolibre.com.ar/organizador-bambu/p/MLA1", True),
    ("https://articulo.mercadolibre.com.mx/MLM-1-lampara-led-_JM", True),
    ("https://www.mercadolibre.cl/lampara-led/p/MLC1", True),
    ("https://WWW.MERCADOLIBRE.COM.AR/organizador-bambu", True),
    ("http://www.mercadolibre.com.ar/organizador-bambu/p/MLA1", False),
    ("https://www.mercadolibre.com.ar.evil.com/organizador-bambu/p/MLA1", False),
    ("https://evilmercadolibre.com.ar/organizador-bambu/p/MLA1", False),
    ("https://www.mercadolibre.com.ar@evil.com/organizador-bambu", False),
    ("https://evil.com@www.mercadolibre.com.ar/organizador-bambu", False),
    ("https://www.mercadolibre.com.ar:8443/organizador-bambu", False),
    ("https://www.mercadolibre.com.ar./organizador-bambu", False),
    ("https://www.mercadolibre.com.ar\\@evil.com/organizador-bambu", False),
    ("https://www.mercadolivre.com.br/organizador-bambu/p/MLB1", False),
    ("https://www.mercadolibre.com/organizador-bambu", False),
    ("https://www.mercadolibre.com.br/organizador-bambu", False),
    ("https://click1.mercadoclicks.com/organizador-bambu", False),
    ("javascript:alert(1)", "error"),          # httpx no puede ni armar el request siguiente: error, se reintenta
    ("", False),
])
async def test_qa_el_location_solo_vale_si_es_https_a_un_ml_hispano_y_no_se_sigue(location, accepted):
    resolver, seen, http = _resolver(lambda r: httpx.Response(302, headers={"location": location} if location else {}))
    got = await resolver.resolve(GOTO)
    await http.aclose()
    if accepted == "error":
        assert got is ml_lens._FAILED
    else:
        assert isinstance(got, str) is accepted and got is not ml_lens._FAILED
    assert len(seen) == 1 and seen[0].url.host == "www.google.com", "un solo GET, sin seguir la redirección"
    assert not resolver.blocked and resolver.streak == 0


async def test_qa_un_200_con_location_no_es_un_link():
    """D2: lo que no es una redirección es una falla de Google (error, se reintenta, suma a la racha), no «no lleva a ML»."""
    resolver, _, http = _resolver(lambda r: httpx.Response(200, headers={"location": "https://www.mercadolibre.com.ar/a-b"}))
    assert await resolver.resolve(GOTO) is ml_lens._FAILED and resolver.streak == 1
    await http.aclose()


async def test_qa_los_frenos_tienen_que_ser_seguidos_y_despues_no_sale_ningun_request():
    plan = iter([httpx.Response(429), httpx.Response(302, headers={"location": "/sorry/index?continue=x"}),
                 httpx.Response(302, headers={"location": "https://www.mercadolibre.com.ar/a-b"}),
                 httpx.Response(429), httpx.Response(302, headers={"location": "https://consent.google.com/ml"}),
                 httpx.Response(429)])
    resolver, seen, http = _resolver(lambda r: next(plan))
    got = [await resolver.resolve(GOTO) for _ in range(5)]
    assert not resolver.blocked and got[2] == "https://www.mercadolibre.com.ar/a-b", "un 302 bueno corta la racha"
    assert all(g is ml_lens._FAILED for i, g in enumerate(got) if i != 2)
    assert await resolver.resolve(GOTO) is ml_lens._FAILED and resolver.blocked
    assert await resolver.resolve(GOTO) is ml_lens._FAILED and len(seen) == 6, "bloqueado: no se le pega más a Google"
    await http.aclose()


async def test_qa_un_goto_que_no_contesta_corta_a_los_5_s_y_no_cuenta_como_freno(monkeypatch):
    monkeypatch.setattr(ml_lens, "_RESOLVE_TIMEOUT_S", 0.05)

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(302, headers={"location": "https://www.mercadolibre.com.ar/a-b"})

    resolver, _, http = _resolver(slow)
    assert await resolver.resolve(GOTO) is ml_lens._FAILED and resolver.streak == 0
    await http.aclose()
    assert ml_lens._RESOLVE_TIMEOUT_S == 0.05 and ml_lens._RESOLVE_PARALLEL == 4 and ml_lens._BLOCK_STREAK == 3


@pytest.mark.usefixtures("lens")
async def test_qa_el_freno_corta_rapido_deja_error_reintenta_a_las_20_h_y_ml_sigue(monkeypatch):
    _eligible(3)
    _snap("400", "Mochila urbana impermeable")
    items = lambda urls: [_lens_item(i, u, [_ml_match(f"AR{k}") for k in range(5)]) for i, u in enumerate(urls)]  # noqa: E731
    world = World(lens_items=items, goto=lambda token: httpx.Response(429)).install(monkeypatch)
    report = await ams.run_search()
    assert report.lens["resolve_blocked"] is True and report.lens["errors"] == 3
    assert len(world.google) <= ml_lens._BLOCK_STREAK + ml_lens._RESOLVE_PARALLEL - 1, len(world.google)
    assert world.ml_queries == [market_query.query_variants("Mochila urbana impermeable", 3)[0]]
    now = utcnow()
    assert {p for p, _, _ in ml_lens.eligible(10, now=now + timedelta(hours=19))} == {"400"}   # recién buscado por texto
    assert {p for p, _, _ in ml_lens.eligible(10, now=now + timedelta(hours=21))} == {"200", "201", "202", "400"}


@pytest.mark.parametrize("status", [503, 403])
@pytest.mark.usefixtures("lens")
async def test_qa_un_error_de_google_que_no_es_429_deja_el_producto_en_error(monkeypatch, status):
    _eligible(1)
    World(goto=lambda token: httpx.Response(status)).install(monkeypatch)
    await ams.run_search()
    assert _lens_rows()["200"].status == "error"


# ─── 4. Slug → consulta ────────────────────────────────────────────


def _slug(n: int) -> str:
    """Un slug de exactamente n caracteres que termina en la palabra "zzzz"."""
    head = "organizador-de-cocina-bambu-tres-niveles-para-alacena-"
    body = (head * 3)[: n - 5].rstrip("-")
    body += "-" * (n - 5 - len(body))
    return body + "-zzzz"


@pytest.mark.parametrize("n, keeps_last", [(55, True), (56, False), (60, False)])
def test_qa_el_corte_de_la_ultima_palabra_arranca_en_56(n, keeps_last):
    slug = _slug(n)
    assert len(slug) == n
    q = ml_lens.query_from_link(f"https://www.mercadolibre.com.ar/{slug}/p/MLA1")["q"]
    assert q.endswith("zzzz") is keeps_last
    assert "  " not in q and "-" not in q


@pytest.mark.parametrize("url, expected", [
    ("https://www.mercadolibre.com.ar/ba%C3%B1o-organizador-ESQUINERO/p/MLA1", "baño organizador esquinero"),
    ("https://www.mercadolibre.com.ar/hola--mundo---cruel/p/MLA1", "hola mundo cruel"),
    ("https://www.mercadolibre.com.ar/a%3Cscript%3Eb-c%2F%2Fd%0A%00e/p/MLA1", "a script b c d e"),
    ("https://www.mercadolibre.com.ar/a-b%2F..%2Fc/p/MLA1", None),                      # "." no entra en el slug
    ("https://www.mercadolibre.com.ar/organizador-bambu/p/MLA1?tracking_id=abc#x", "organizador bambu"),
    ("https://articulo.mercadolibre.com.ar/MLA-99-" + "organizador-de-cocina-de-bambu-con-tres-niveles-ajustable-blan"
     + "-_JM", "organizador de cocina de bambu con tres niveles ajustable"),
    ("https://www.mercadolibre.com.ar/organizador/p/MLA1", None),                       # una sola palabra
    ("https://www.mercadolibre.com.ar/organizador_bambu/p/MLA1", None),                 # fuera del alfabeto del slug
    ("https://www.mercadolibre.com.ar/tienda/organizador-bambu", None),
    ("https://www.mercadolibre.com.ar/MLA-1234567", None),
])
def test_qa_slug_raro_queda_en_letras_numeros_y_espacios(url, expected):
    found = ml_lens.query_from_link(url)
    assert (found["q"] if found else None) == expected


def test_qa_el_largo_maximo_de_la_consulta():
    slug = "-".join(["palabra"] * 28)                                          # 223 caracteres: el _SLUG tope es 200
    assert ml_lens.query_from_link(f"https://www.mercadolibre.com.ar/{slug}/p/MLA1") is None
    slug = "-".join(["abcdefghijklmnopqrstuvwxy"] * 7)                        # 181 caracteres
    q = ml_lens.query_from_link(f"https://www.mercadolibre.com.ar/{slug}/p/MLA1")["q"]
    assert len(q) <= 120 and set(q) <= set("abcdefghijklmnopqrstuvwxy ")


@pytest.mark.parametrize("variants", [1, 3])
@pytest.mark.usefixtures("lens")
def test_qa_no_repite_ninguna_variante_de_texto_aunque_el_plan_de_la_cola_sea_mas_corto(variants):
    """Se deduplica contra las 3 variantes aunque `pm_ml_query_variants` sea 1: la consulta Lens nunca es una de texto."""
    runtime.set_value("pm_ml_query_variants", variants)
    plan = market_query.query_variants(LONG, 3)
    links = [(i + 1, "https://www.mercadolibre.com.ar/" + q.lower().replace(" ", "-") + "/p/MLA" + str(i))
             for i, q in enumerate(plan)]
    assert ml_lens.pick_queries(links, market_query.query_variants(LONG, ml_lens._TEXT_PLAN_VARIANTS)) == []


# ─── 5. Lo ya procesado no se vuelve a lensear; con IGUAL nunca ────


@pytest.mark.usefixtures("lens")
async def test_qa_la_noche_siguiente_no_se_vuelve_a_lensear_lo_ya_lenseado(monkeypatch):
    _eligible(3)
    _snap("210", "Lámpara de pie", status="ok", state="igual", origin="oficina")
    _text("210")
    world = World().install(monkeypatch)
    await ams.run_search()
    assert sorted(_lens_rows()) == ["200", "201", "202"]
    _next_day()
    second = await ams.run_search()
    assert len(world.lens_starts) == 1 and second.lens["products"] == 0
    assert second.lens["reason"] == "no hay productos para lensear"
    assert "210" not in _lens_rows()


@pytest.mark.usefixtures("lens")
def test_qa_con_igual_nunca_hay_item_lens_aunque_tenga_una_consulta_pendiente():
    _snap("1", LONG, status="ok", state="igual", origin="oficina")
    _text("1", "la que gano", ago=timedelta(hours=13))
    _lens_row("1", ["q uno"])
    assert all(not i.get("lens") for i in oficina_ml.build_queue(50, with_lens=True))
    assert ml_lens.eligible(50) == []


# ─── 6. Medición ───────────────────────────────────────────────────


def test_qa_n_lens_ok_de_una_corrida_vieja_o_rota():
    from app.db.models import PriceMonitorRun
    from app.pricing import price_monitor

    for raw, expected in ((None, 0), ("", 0), ("{}", 0), ('{"lens": 4, "corto": 2}', 4), ('{"lens": "x"}', 0)):
        d = price_monitor.run_to_dict(PriceMonitorRun(status="ok", variant_stats=raw))
        assert d["oficina"]["n_lens_ok"] == expected and "lens" not in d["variants"]


@pytest.mark.usefixtures("lens")
async def test_qa_el_refresco_de_lo_ganado_por_lens_sigue_contando_como_lens(monkeypatch):
    _snap("300", "Organizador cocina", status="ok", state="igual", origin="oficina")
    _text("300", "organizador lens modelo 300", ago=timedelta(days=8), origin="lens")
    World().install(monkeypatch)
    await ams.run_search()
    with Session(engine) as s:
        newest = s.exec(select(MlWebResult).where(MlWebResult.product_id == "300")
                        .order_by(MlWebResult.fetched_at.desc())).first()  # type: ignore[attr-defined]
    assert newest.query == "organizador lens modelo 300"
    assert newest.origin == "lens"


# ─── I-4: admin y auditados ────────────────────────────────────────


def _client(monkeypatch, user: str) -> TestClient:
    s = Settings(vendure_api_url="https://example.invalid/admin-api", dashboard_password="", supabase_url="",
                 supabase_anon_key="", dashboard_admins="nico@b2box.pro", dashboard_secret="k" * 40)
    for mod in (auth, security, main_mod):
        monkeypatch.setattr(mod, "get_settings", lambda s=s: s)
    monkeypatch.setattr(auth, "session_username", lambda token: user)
    return TestClient(main_mod.app)


def _wipe_settings() -> None:
    with Session(engine) as s:
        s.execute(delete(Setting).where(Setting.key.in_(["pm_lens_enabled", "pm_lens_daily_max"])))  # type: ignore[attr-defined]
        s.execute(delete(AuditLog).where(AuditLog.action == "setting_changed"))
        s.commit()
    runtime.invalidate()


def test_qa_los_settings_de_lens_solo_los_cambia_un_admin_y_quedan_auditados(monkeypatch):
    from app.db.session import init_db

    init_db()
    _wipe_settings()
    try:
        pao = _client(monkeypatch, "pao@b2box.pro")
        assert pao.put("/api/settings/pm_lens_enabled", json={"value": 1}).status_code == 403
        assert pao.put("/api/settings/pm_lens_daily_max", json={"value": 100}).status_code == 403
        assert pao.delete("/api/settings/pm_lens_daily_max").status_code == 403
        runtime.invalidate()
        assert (runtime.get("pm_lens_enabled"), runtime.get("pm_lens_daily_max")) == (0, 30)
        nico = _client(monkeypatch, "nico@b2box.pro")
        assert nico.put("/api/settings/pm_lens_enabled", json={"value": 2}).status_code == 400
        assert nico.put("/api/settings/pm_lens_daily_max", json={"value": 101}).status_code == 400
        assert nico.put("/api/settings/pm_lens_enabled", json={"value": 1}).status_code == 200
        assert nico.put("/api/settings/pm_lens_daily_max", json={"value": 60}).status_code == 200
        with Session(engine) as s:
            rows = s.exec(select(AuditLog).where(AuditLog.action == "setting_changed").order_by(AuditLog.id)).all()
        assert [json.loads(r.after) for r in rows] == [{"pm_lens_enabled": 1}, {"pm_lens_daily_max": 60}]
        assert all(r.detail.startswith("nico@b2box.pro cambió pm_lens_") for r in rows)
    finally:
        _wipe_settings()
