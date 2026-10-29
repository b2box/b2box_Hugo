"""QA independiente (criterio 1): CERO escrituras a Vendure y a Paco, a nivel HTTP, en los modos 0, 1 y 2.

Todo corre de punta a punta con el código real: `price_monitor.run_price_monitor` (la corrida nocturna, que dispara la
pasada de la bandeja), el job del resumen diario, y TODOS los endpoints nuevos con TODOS los métodos HTTP. Lo único falso
es lo de afuera: Vendure (`qa5_bandeja.VendureWorld`, que contesta 500 a cualquier mutation) y Mercado Libre. Un guardia
sobre `send` de httpx/httpx2 y sobre los sockets anota cada request; Paco (hosts `paco*.invalid`) y cualquier host
desconocido cuentan como violación.

«Escritura» = cualquier mutation GraphQL salvo `Login` (autenticarse no escribe datos), cualquier request que no sea un POST
de consulta, y cualquier request a Paco.
"""

from __future__ import annotations

import os
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app import main as main_mod  # noqa: E402
from app import security  # noqa: E402
from app.clock import utcnow  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.models import MarketPriceSnapshot, PriceMonitorRun, PriceReviewEvent, PriceReviewItem  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.ingest import meli  # noqa: E402
from app.integrations import paco  # noqa: E402
from app.notifier import email as email_chan  # noqa: E402
from app.pricing import market_match, price_monitor, price_review_digest  # noqa: E402
from app.pricing.market_ml import MlMarket  # noqa: E402
from app.scheduler import jobs  # noqa: E402
from app.vendure.client import VendureClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402
from tests import qa5_bandeja as q  # noqa: E402
from tests.qa5_bandeja import Scenario, Tier, VendureWorld, healthy, mk_snap  # noqa: E402

ML_IMG = "https://http2.mlstatic.com/D_NQ_{}.jpg"
READ_OPS = {"Products", "TierBreakdowns", "PricedVariant", "ProductCertification", "ActiveChannelPricing", "FiscalProfile"}
PRODUCTS = {  # nombre → (escenario, precio de ML en pesos o None si ML no lo tiene)
    "Organizador de cocina plegable": ("1", 26_000.0),
    "Taza de ceramica blanca": ("2", 27_000.0),
    "Lampara LED de escritorio": ("3", 30_000.0),
}


class FakeML:
    def __init__(self) -> None:
        self.search: dict[str, list[dict]] = {}
        self.items: dict[str, list[dict]] = {}
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if path == "/products/search":
            return httpx.Response(200, json={"results": self.search.get(request.url.params["q"], [])})
        if path.startswith("/products/") and path.endswith("/items"):
            return httpx.Response(200, json={"results": self.items.get(path.split("/")[2], [])})
        if path.startswith("/users/"):
            return httpx.Response(200, json={"seller_reputation": {"transactions": {"completed": 5_000}}})
        if path == "/sites/MLA/listing_prices":
            return httpx.Response(403, json={"message": "forbidden"})
        raise AssertionError(f"path de ML inesperado: {path}")


class Smtp:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, msg, **kw):
        self.sent.append(msg)


@pytest.fixture
def stack(monkeypatch):
    """Vendure + ML + SMTP falsos, base limpia, settings de prueba y el guardia de red puesto."""
    q.reset_db()
    q.setting("pm_include_disabled", 1)
    q.setting("pm_ml_query_variants", 1)
    cfg = Settings(vendure_api_url="https://example.invalid/admin-api", hugo_env="development", dashboard_user="admin",
                   dashboard_password="secreto", dashboard_secret="s" * 32, supabase_url="", supabase_anon_key="",
                   alert_smtp_user="alertas@b2box.pro", alert_smtp_pass="clave", alert_email_to="tech@b2box.pro",
                   pm_review_emails="pao@b2box.pro, gabriel@b2box.pro", paco_url="https://paco.invalid",
                   paco_pro_url="https://paco-pro.invalid", paco_pro_api_key="k", paco_api_key="k")
    for mod in (auth, security, main_mod, email_chan, price_review_digest, paco):
        if hasattr(mod, "get_settings"):
            monkeypatch.setattr(mod, "get_settings", lambda cfg=cfg: cfg)

    world = VendureWorld()
    spy = q.install_spy(monkeypatch, world)
    world.spy = spy
    # productos de Vendure: 3 con el desglose de la familia sana, ML por debajo -> rojos firmes; 1 deshabilitado
    for name, (pid, _price) in PRODUCTS.items():
        sc = healthy(pid, name=name)
        sc.median = 2_600_000
        world.add(sc)
    world.add(healthy("4", name="Producto deshabilitado"), enabled=False)

    ml = FakeML()
    for name, (pid, price) in PRODUCTS.items():
        mla = f"MLA10000{pid}"
        ml.search[name] = [{"id": mla, "name": name, "permalink": f"https://www.mercadolibre.com.ar/p/{mla}",
                            "pictures": [{"url": ML_IMG.format(mla)}]}]
        ml.items[mla] = [{"item_id": f"I{pid}{k}", "seller_id": f"90{pid}{k}", "price": price + 100.0 * k,
                          "currency_id": "ARS", "category_id": "MLA5725"} for k in range(3)]
    ml.search["Producto deshabilitado"] = ml.search["Taza de ceramica blanca"]

    async def _sleep(_s):
        return None

    async def _token():
        return "tok"

    def _market(budget, on_reserve=None):
        return MlMarket(budget=budget, client=httpx.AsyncClient(transport=httpx.MockTransport(ml.handler)), sleep=_sleep,
                        token_getter=_token, on_reserve=on_reserve)

    async def _scorer(our, urls):  # noqa: ARG001
        return 0.9

    async def _no_index():
        return None

    monkeypatch.setattr(price_monitor, "MlMarket", _market)
    monkeypatch.setattr(meli, "enabled", lambda: True)
    monkeypatch.setattr(price_monitor, "_ensure_clip_index", _no_index)
    monkeypatch.setattr(market_match, "clip_index_scorer", _scorer)
    monkeypatch.setattr(market_match, "indexed", lambda product: True)
    smtp = Smtp()
    monkeypatch.setattr(email_chan.aiosmtplib, "send", smtp.send)

    class Stack:
        pass

    st = Stack()
    st.world, st.spy, st.ml, st.smtp, st.cfg = world, spy, ml, smtp, cfg
    return st


def client_with_session() -> TestClient:
    c = TestClient(main_mod.app, raise_server_exceptions=False)
    c.cookies.set(auth.COOKIE_NAME, auth.issue_session_token("pao@b2box.pro"))
    return c


async def two_nights(st):
    """Corre la corrida real dos veces (la primera «ayer»). Devuelve (run1, run2)."""
    r1 = await jobs.price_monitor(trigger="manual")
    assert r1["status"] == "ok", r1
    yesterday = utcnow() - timedelta(days=1)
    with Session(engine) as s:
        run = s.get(PriceMonitorRun, r1["run_id"])
        run.started_at = yesterday
        run.finished_at = yesterday
        s.add(run)
        for snap in s.exec(select(MarketPriceSnapshot).where(MarketPriceSnapshot.run_id == r1["run_id"])).all():
            snap.captured_at = yesterday
            snap.data_at = yesterday
            s.add(snap)
        s.commit()
    r2 = await jobs.price_monitor(trigger="manual")
    assert r2["status"] == "ok", r2
    return r1, r2


ALL_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
PATHS = ("/summary", "/items", "/items/{id}", "/items/{id}/approve", "/items/{id}/decision", "/items/{id}/undo",
         "/items/{id}/recheck", "/items/{id}/inactivate", "/items/{id}/reactivate", "/items/{id}/send-to-paco",
         "/items/{id}/paco", "/decide", "/run", "/sync")


@pytest.mark.parametrize("mode", [0, 1, 2])
async def test_no_write_to_vendure_nor_to_paco_in_any_mode_through_the_whole_night_the_mail_and_every_endpoint(stack, mode):
    st = stack
    q.setting("pm_mode", mode)

    # 1. la corrida nocturna real, dos noches seguidas (la pasada de la bandeja corre dentro)
    r1, r2 = await two_nights(st)
    items = q.rows(PriceReviewItem)
    if mode == 0:
        assert items == [] and q.rows(PriceReviewEvent) == []
        assert set(st.world.ops()) == {"Products"}             # en sombra no se le pide NADA más a Vendure
    else:
        assert len(items) == 3 and all(i.state == "propuesto" for i in items)
        assert all(i.hugo_disabled is None and i.disabled_at is None and i.decision is None for i in items)
        assert {"TierBreakdowns", "ProductCertification"} <= set(st.world.ops())
    snaps = {s.product_id: s for s in q.rows(MarketPriceSnapshot, MarketPriceSnapshot.run_id == r2["run_id"])}
    assert snaps["1"].color == snaps["2"].color == snaps["3"].color == "rojo" and snaps["4"].product_enabled is False
    assert snaps["4"].review_flag == "dudoso:deshabilitado"

    # 2. el resumen diario
    sent = await jobs.price_review_digest()
    assert (sent["sent"] is True) is (mode >= 1)
    assert len(st.smtp.sent) == (1 if mode >= 1 else 0)

    # 3. todos los endpoints, con todos los métodos, con sesión y sin sesión
    c, anon = client_with_session(), TestClient(main_mod.app, raise_server_exceptions=False)
    item_id = items[0].id if items else 1
    seen = {}
    for path in PATHS:
        url = "/api/price-review" + path.replace("{id}", str(item_id))
        for method in ALL_METHODS:
            r = c.request(method, url)
            seen[(method, path)] = r.status_code
            assert r.status_code < 500, (method, url, r.status_code, r.text[:200])
            a = anon.request(method, url)
            assert a.status_code in (401, 404, 405), (method, url, a.status_code)
            assert a.status_code != 200
    # lo único que contesta 200 son las tres lecturas
    ok = {k for k, v in seen.items() if v == 200}
    assert ok == {("GET", "/summary"), ("GET", "/items")} | ({("GET", "/items/{id}")} if items else set())
    if items:
        assert c.get(f"/api/price-review/items/{item_id}").json()["state"] == "propuesto"

    # 4. «No es el mismo» / «Es el mismo» (corrigen el semáforo, nunca Vendure)
    first = q.rows(MarketPriceSnapshot, MarketPriceSnapshot.run_id == r2["run_id"], MarketPriceSnapshot.product_id == "1")[0]
    import json
    ml_id = json.loads(first.matched_listings)[0]["ml_id"]
    assert c.post(f"/api/price-monitor/snapshots/{first.id}/not-same", json={"ml_id": ml_id}).status_code == 200
    assert c.post(f"/api/price-monitor/snapshots/{first.id}/same", json={"ml_id": ml_id}).status_code in (200, 404, 409)
    assert c.get("/api/price-monitor/snapshots").status_code == 200

    # 5. el veredicto, a nivel de red
    world, spy = st.world, st.spy
    assert world.writes() == [], world.writes()
    assert {r["op"] for r in world.log} <= READ_OPS | {"Login"}, set(world.ops())
    assert "Login" not in world.ops()                          # el bearer estaba cacheado: ni siquiera se autenticó
    assert all(r["method"] == "POST" and r["kind"] == "query" for r in world.log)
    assert not any("mutation" in r["query"].lower() for r in world.log)
    assert spy.violations == [], spy.violations
    assert not any(r["host"] in spy.PACO_HOSTS for r in spy.requests)
    # nada de lo que se mandó a Vendure menciona updateProduct / enabled
    assert not any("updateProduct" in r["query"] or "enabled:" in r["query"].replace(" ", "") for r in world.log)


async def test_the_guard_itself_catches_a_write_and_a_call_to_paco(stack):
    """Sin esto, todo lo anterior podría pasar por no estar mirando. Se provoca a propósito una mutation y un POST a Paco."""
    st = stack
    c = VendureClient()
    with pytest.raises(Exception):
        await c.disable_product("1")                       # el cliente real SÍ puede escribir: updateProduct(enabled)
    assert {r["op"] for r in st.world.writes()} == {"SetEnabled"}          # (el cliente reintenta 3 veces)
    async with httpx.AsyncClient() as hc:
        await hc.post("https://paco-pro.invalid/api/tech/start", data={"x": "1"})
    assert len(st.spy.violations) == 1 and st.spy.violations[0]["host"] == "paco-pro.invalid"
    with pytest.raises(OSError):
        import socket
        socket.create_connection(("203.0.113.9", 80), timeout=0.2)
    assert st.spy.violations[-1]["method"] == "SOCKET"


def _names_used(tree) -> tuple[set[str], set[str]]:
    """(nombres y atributos usados, nombres importados) de un módulo. Los textos y docstrings no cuentan. El decorador
    `@router.post` de las rutas no es un POST a otro servicio: no cuenta como `post`."""
    import ast

    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
            and not (n.attr == "post" and isinstance(n.value, ast.Name) and n.value.id == "router")}
    used |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    imported = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    imported |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    return used, imported


def test_no_module_of_the_review_can_reach_a_write_method_statically():
    """K-2. Defensa en profundidad: la pasada, el diagnóstico, el desglose y las rutas no USAN (nombres, atributos o llamadas)
    los métodos que escriben en Vendure o hablan con Paco. Las rutas declaran solo los 3 POST de las decisiones, que delegan
    en `price_review_actions`: el ÚNICO módulo autorizado a escribir (ver el test siguiente)."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    banned = {"disable_product", "enable_product", "_set_enabled", "set_enabled", "submit_pro", "submit", "get_status",
              "post", "put", "patch", "delete", "aiosmtplib"}
    for rel in ("pricing/price_review.py", "pricing/diagnosis_source.py", "pricing/price_diagnosis.py", "pricing/price_breakdown.py",
                "api/price_review_routes.py"):
        used, imported = _names_used(ast.parse((root / rel).read_text()))
        assert not (banned & (used | imported)), (rel, banned & (used | imported))
        assert "paco" not in {m.split(".")[-1] for m in imported}, rel
    routes = (root / "api/price_review_routes.py").read_text()
    assert "@router.put" not in routes and "@router.delete" not in routes and "@router.patch" not in routes
    assert sorted(line.split('"')[1] for line in routes.splitlines() if line.startswith("@router.post")) == [
        "/items/{item_id}/approve", "/items/{item_id}/decision", "/items/{item_id}/undo"]


def test_the_actions_module_is_the_only_one_that_calls_set_enabled_and_it_never_talks_to_paco():
    """K-2. En todo `app/`, `set_enabled` (la escritura que verifica el valor devuelto) lo usa SOLO price_review_actions; los
    flujos viejos de duplicados siguen con `_set_enabled`. Y el módulo de acciones no usa los métodos viejos ni Paco."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    users = set()
    for path in root.rglob("*.py"):
        rel = str(path.relative_to(root))
        used, _ = _names_used(ast.parse(path.read_text()))
        if "set_enabled" in used and rel != "vendure/client.py":
            users.add(rel)
    assert users == {"pricing/price_review_actions.py"}, users
    used, imported = _names_used(ast.parse((root / "pricing/price_review_actions.py").read_text()))
    assert not ({"disable_product", "enable_product", "_set_enabled", "submit", "submit_pro", "get_status", "post", "put",
                 "patch", "aiosmtplib"} & (used | imported))
    assert not any("paco" in m for m in imported)


async def test_a_crash_of_the_review_pass_never_fails_the_nightly_run_and_the_next_pass_recovers(stack, monkeypatch):
    """La pasada de la bandeja corre DESPUÉS de cerrar la corrida: si revienta, la corrida queda `ok`, el error queda anotado y la
    pasada se puede repetir sobre la misma corrida (review_at sigue vacío). Nada de eso toca Vendure."""
    from app.pricing import price_review as pr_mod

    st = stack
    q.setting("pm_mode", 1)
    real = pr_mod.process_run

    async def boom(*a, **k):
        raise RuntimeError("bug en la bandeja")

    monkeypatch.setattr(pr_mod, "process_run", boom)
    r1 = await jobs.price_monitor(trigger="manual")
    assert r1["status"] == "ok"
    run = q.rows(PriceMonitorRun, PriceMonitorRun.id == r1["run_id"])[0]
    assert run.status == "ok" and run.review_at is None and "RuntimeError" in (run.review_summary or "")
    assert "bug en la bandeja" not in (run.review_summary or "")             # el tipo se anota, el mensaje va al log
    c = client_with_session()
    assert (c.get("/api/price-review/summary").json()["last_pass_error"] or "") == "RuntimeError"
    monkeypatch.setattr(pr_mod, "process_run", real)
    out = await real(r1["run_id"], now=utcnow())
    assert out is not None and q.rows(PriceMonitorRun, PriceMonitorRun.id == r1["run_id"])[0].review_at is not None
    assert st.world.writes() == [] and st.spy.violations == []
