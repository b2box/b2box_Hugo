"""HG1: la corrida completa (Vendure falso por HTTP → reglas → tablas), los
endpoints del dashboard y el CSV.

Garantías que se prueban acá:
  * no hay ningún pedido a Vendure que no sea una `query` (nada de mutations);
  * una lectura por página de 100 productos, por canal, con el token del canal;
  * los campos de proveedor no aparecen en la base, en la API ni en el CSV;
  * un canal caído no tira la auditoría del otro.
"""

from __future__ import annotations

import csv
import io
import json
import os
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import auth, security  # noqa: E402
from app import main as main_mod  # noqa: E402
from app.api import seo_routes  # noqa: E402
from app.clock import utcnow  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.models import Setting, TextAuditItem, TextAuditRun  # noqa: E402
from app.db.session import engine, init_db  # noqa: E402
from app.scheduler import jobs  # noqa: E402
from app.seo import text_audit  # noqa: E402
from app.vendure import client as vendure_client  # noqa: E402
from tests.seo_fixtures import FakeVendure, install, raw_product  # noqa: E402

SECRETS = {
    "business": "Yiwu Yongkang Hardware Co., Ltd.",
    "size_model": "ZK-7731 / Negro / 30cm",
    "link": "https://detail.1688.com/offer/698765432109.html?spm=a2615",
}
SECRET_FRAGMENTS = ("yongkang", "zk-7731", "zk7731", "698765432109", "detail.1688.com", "spm=a2615")


def settings(**over) -> Settings:
    base = dict(vendure_api_url="https://example.invalid/admin-api", hugo_env="development",
                dashboard_user="admin", dashboard_password="secreto", dashboard_secret="s" * 32,
                supabase_url="", supabase_anon_key="", vendure_channel_token="ar")
    base.update(over)
    return Settings(**base)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    init_db()
    with Session(engine) as s:
        for model in (TextAuditItem, TextAuditRun):
            for row in s.exec(select(model)).all():
                s.delete(row)
        for row in s.exec(select(Setting).where(Setting.key.like("seo:lista:%"))).all():  # type: ignore[attr-defined]
            s.delete(row)
        s.commit()
    st = settings()
    for mod in (auth, security, main_mod, text_audit, vendure_client):
        monkeypatch.setattr(mod, "get_settings", lambda st=st: st)
    yield


@pytest.fixture
def client() -> TestClient:
    c = TestClient(main_mod.app)
    c.cookies.set(auth.COOKIE_NAME, auth.issue_session_token("admin"))
    return c


def make_catalog():
    """Un catálogo chico con un caso de cada cosa. Canal AR = ids 1..6 y 8;
    el por defecto tiene además 7 (solo ahí) y 9 (deshabilitado)."""
    products = [
        raw_product(1),                                                           # limpio
        raw_product(2, translations=[("es_AR", "Funda iPhone Efecto Líquido", "funda-iphone-efecto-liquido",
                                      "<p>Funda transparente 😍 para el celular de todos los días</p>"),
                                     ("es", "Funda iPhone Efecto Líquido", "funda-iphone-efecto-liquido", "Funda")]),
        raw_product(3, translations=[("es", "Taza Blanca", "taza-blanca", "Taza de cerámica blanca de 300 ml.")]),   # sin es_AR
        raw_product(4, code="BX00717", translations=[("es_AR", "BX00717", "bx00717-609", "B2BOX")]),
        raw_product(5, business=SECRETS["business"], size_model=SECRETS["size_model"], link=SECRETS["link"],
                    translations=[("es_AR", "Pinza Yongkang ZK-7731 Profesional", "pinza-yongkang-profesional",
                                   "Pinza de acero. Ref 698765432109 en el catálogo del proveedor.")]),
        raw_product(6, translations=[("es_AR", "Organizador de Huevos Automático Transparente para Heladera",
                                      "organizador-de-huevos", "Organizador de huevos para la heladera.")]),
        raw_product(7, translations=[("es_AR", "Organizador de Huevos Automático para Heladera",
                                      "organizador-de-huevos-2", "Otro organizador de huevos para la heladera.")]),
        raw_product(8, translations=[("es_AR", "Organizador de Huevos Automático Transparente para Heladera",
                                      "organizador-huevos", "Organizador de huevos para la heladera, idéntico.")]),
        raw_product(9, enabled=False, translations=[("es_AR", "Taza  Kuromi Rosa", "taza-kuromi-rosa",
                                                     "Taza de cerámica de personaje, cómoda para el día.")]),
    ]
    by_id = {p["id"]: p for p in products}
    ar = [by_id[i] for i in ("1", "2", "3", "4", "5", "6", "8")]
    return {"ar": ar, None: products}


def run(monkeypatch, fake, trigger="manual"):
    install(monkeypatch, fake)
    import asyncio

    return asyncio.run(text_audit.run_text_audit(trigger=trigger))


def rows_of(run_id: int) -> dict[tuple[str, str], TextAuditItem]:
    with Session(engine) as s:
        return {(i.product_id, i.language_code): i
                for i in s.exec(select(TextAuditItem).where(TextAuditItem.run_id == run_id))}


def rules_in(item: TextAuditItem) -> set[str]:
    return {r for r in item.issues.strip(",").split(",") if r}


# ─── Corrida ───────────────────────────────────────────────────────

def test_la_corrida_recorre_los_dos_canales_y_guarda_una_fila_por_producto_e_idioma(monkeypatch):
    fake = FakeVendure(make_catalog())
    result = run(monkeypatch, fake)

    assert result["status"] == "ok"
    assert result["channels_ok"] == ["ar", "default"] and result["channels_failed"] == {}
    assert result["products_total"] == 9 and result["products_enabled"] == 8
    assert result["rows_total"] == 10                       # el 2 tiene dos idiomas
    rows = rows_of(result["id"])
    assert set(rows) == {("1", "es_AR"), ("2", "es_AR"), ("2", "es"), ("3", "es"), ("4", "es_AR"),
                         ("5", "es_AR"), ("6", "es_AR"), ("7", "es_AR"), ("8", "es_AR"), ("9", "es_AR")}
    # canal: el 7 y el 9 no están en AR; el 1 sí
    assert rows[("1", "es_AR")].in_ar is True and rows[("1", "es_AR")].in_default is True
    assert rows[("7", "es_AR")].in_ar is False and rows[("7", "es_AR")].in_default is True
    assert rows[("9", "es_AR")].enabled is False


def test_las_reglas_saltan_donde_corresponde(monkeypatch):
    result = run(monkeypatch, FakeVendure(make_catalog()))
    rows = rows_of(result["id"])
    assert rules_in(rows[("1", "es_AR")]) == set()
    assert {"MAR", "DESC_CON_HTML_EN_META"} <= rules_in(rows[("2", "es_AR")])
    assert "SIN_ES_AR" in rules_in(rows[("3", "es")])
    assert "SIN_ES_AR" not in rules_in(rows[("2", "es")]) and "SIN_ES_AR" not in rules_in(rows[("2", "es_AR")])
    assert {"NOMBRE_ES_CODIGO", "SIN_DESCRIPCION"} <= rules_in(rows[("4", "es_AR")])
    assert "FAB" in rules_in(rows[("5", "es_AR")]) and "COD" in rules_in(rows[("5", "es_AR")])
    assert {"MAR", "ESPACIOS"} <= rules_in(rows[("9", "es_AR")])
    details = json.loads(rows[("3", "es")].details)
    assert details["SIN_ES_AR"] == "sin traducción es_AR (tiene: es)"


def test_casi_duplicados_y_duplicado_exacto_solo_entre_habilitados(monkeypatch):
    cat = make_catalog()
    # El 9 (deshabilitado) tiene el mismo nombre que el 1: no debe contar como duplicado.
    cat[None][8]["translations"][0]["name"] = "Producto 1"
    result = run(monkeypatch, FakeVendure(cat))
    rows = rows_of(result["id"])
    assert "DUP_EXACTO" in rules_in(rows[("6", "es_AR")]) and "DUP_EXACTO" in rules_in(rows[("8", "es_AR")])
    assert "DUP_CASI" in rules_in(rows[("7", "es_AR")])
    assert "DUP_EXACTO" not in rules_in(rows[("1", "es_AR")]) and "DUP_EXACTO" not in rules_in(rows[("9", "es_AR")])


def test_el_conteo_es_por_producto_distinto(monkeypatch):
    result = run(monkeypatch, FakeVendure(make_catalog()))
    assert result["counts"]["SIN_ES_AR"] == 1
    assert result["counts"]["MAR"] == 2                      # el 2 (en dos idiomas) cuenta una vez, y el 9
    assert result["products_with_issues"] == len({k[0] for k, v in rows_of(result["id"]).items() if v.n_issues})
    assert list(result["counts"]) == sorted(result["counts"], key=text_audit.rules.RULE_IDS.index)


# ─── Solo lectura y paginado, a nivel HTTP ─────────────────────────

def test_nunca_hay_un_pedido_que_no_sea_una_query(monkeypatch):
    fake = FakeVendure(make_catalog())
    run(monkeypatch, fake)
    assert fake.requests, "tiene que haber leído algo"
    assert fake.writes() == []
    for req in fake.requests:
        assert req["method"] == "POST"
        assert req["query"].lstrip().startswith("query ")
        assert "mutation" not in req["query"].lower()
        assert "updateProduct" not in req["query"] and "variantList" not in req["query"]


def test_una_lectura_por_pagina_de_100_y_el_token_de_cada_canal(monkeypatch):
    n = 250
    products = [raw_product(i) for i in range(1, n + 1)]
    fake = FakeVendure({"ar": products[:200], None: products})
    result = run(monkeypatch, fake)

    assert result["products_total"] == n
    ar, default = fake.queries("ar"), fake.queries(None)
    assert [r["variables"] for r in ar] == [{"skip": 0, "take": 100}, {"skip": 100, "take": 100}]
    assert [r["variables"] for r in default] == [
        {"skip": 0, "take": 100}, {"skip": 100, "take": 100}, {"skip": 200, "take": 100}]
    assert len(fake.requests) == len(ar) + len(default)       # ni una lectura de más (no hay N+1 de productos)
    assert all(r["token"] in ("ar", None) for r in fake.requests)


def test_la_query_pide_todas_las_traducciones_y_los_campos_de_proveedor(monkeypatch):
    fake = FakeVendure(make_catalog())
    run(monkeypatch, fake)
    q = " ".join(fake.requests[0]["query"].split())     # gql reimprime el documento: se compara sin saltos
    assert "translations { languageCode name slug description }" in q
    assert "supplierBusiness supplierSizeModel supplierLink b2boxProductCode" in q
    assert "sort: { id: ASC }" in q


def test_si_el_schema_no_tiene_los_campos_de_proveedor_se_lee_sin_ellos(monkeypatch):
    fake = FakeVendure(make_catalog())
    fake.reject_supplier_fields = True
    result = run(monkeypatch, fake)
    assert result["status"] == "ok"
    assert "FAB solo compara el link" in result["notes"]
    assert any("supplierBusiness" not in r["query"] for r in fake.requests)
    # sin supplierBusiness ni supplierSizeModel la regla sigue con el link del proveedor
    # (que el fake devuelve igual): el id de la oferta está en la descripción del producto 5.
    assert "FAB" in rules_in(rows_of(result["id"])[("5", "es_AR")])


# ─── Canales caídos ────────────────────────────────────────────────

def test_un_canal_caido_no_tira_al_otro(monkeypatch):
    fake = FakeVendure(make_catalog())
    fake.fail_tokens = {"ar"}
    result = run(monkeypatch, fake)
    assert result["status"] == "degraded"
    assert result["channels_ok"] == ["default"] and "ar" in result["channels_failed"]
    rows = rows_of(result["id"])
    assert len(rows) == 10
    assert all(r.in_ar is None for r in rows.values())       # sin dato, no «no»
    assert all(r.in_default is True for r in rows.values())


def test_todos_los_canales_caidos_es_failed_con_motivo(monkeypatch):
    fake = FakeVendure(make_catalog())
    fake.fail_tokens = {"ar", None}
    result = run(monkeypatch, fake)
    assert result["status"] == "failed" and "no se pudo leer ningún canal" in result["error"]
    assert rows_of(result["id"]) == {}


def test_sin_token_de_canal_solo_se_lee_el_por_defecto(monkeypatch):
    st = settings(vendure_channel_token=None)
    for mod in (text_audit, vendure_client):
        monkeypatch.setattr(mod, "get_settings", lambda: st)
    fake = FakeVendure({None: [raw_product(1)]})
    result = run(monkeypatch, fake)
    assert result["channels_ok"] == ["default"]
    assert "VENDURE_CHANNEL_TOKEN vacío" in result["notes"]
    assert rows_of(result["id"])[("1", "es_AR")].in_ar is None


def test_el_cliente_por_defecto_sigue_usando_el_canal_de_la_configuracion(monkeypatch):
    assert vendure_client.VendureClient()._channel_token == "ar"
    assert vendure_client.VendureClient(channel_token=None)._channel_token is None
    assert vendure_client.VendureClient(channel_token="mx")._channel_token == "mx"


def test_dos_corridas_a_la_vez_la_segunda_se_ignora(monkeypatch):
    import asyncio

    install(monkeypatch, FakeVendure(make_catalog()))

    async def both():
        return await asyncio.gather(text_audit.run_text_audit("manual"), text_audit.run_text_audit("manual"))

    a, b = asyncio.run(both())
    assert (a is None) != (b is None)


# ─── Lo del proveedor no sale de la corrida ────────────────────────

def _everything_visible(client, run_id) -> str:
    """Todo lo que cualquiera puede ver: la base entera de la auditoría, la API y el CSV."""
    parts = []
    with engine.connect() as conn:
        for table in ("text_audit_item", "text_audit_run"):
            parts.append(json.dumps([list(map(str, r)) for r in conn.execute(text(f"SELECT * FROM {table}"))]))
    parts.append(client.get("/api/seo/text-audit/summary").text)
    parts.append(client.get(f"/api/seo/text-audit/items?run_id={run_id}&only_issues=false&page_size=200").text)
    parts.append(client.get(f"/api/seo/text-audit/export.csv?run_id={run_id}&only_issues=false").content.decode("utf-8-sig"))
    return "\n".join(parts).casefold()


QUIET = {
    "business": "Zhongshan Lanxing Lighting Co., Ltd.",
    "size_model": "QW-4410 / Blanco / 20cm",
    "link": "https://lanxing-factory.1688.com/offer/512345678901.html?spm=b7788",
}
QUIET_FRAGMENTS = ("zhongshan", "lanxing", "qw-4410", "qw4410", "512345678901", "lanxing-factory", "spm=b7788")


def test_los_campos_de_proveedor_no_se_guardan_ni_se_muestran(monkeypatch, client):
    """El producto 10 tiene los tres campos cargados y ninguno aparece en sus textos: nada de lo que
    se guarda, se lista o se exporta puede contenerlos (ni en la base, ni en la API, ni en el CSV)."""
    cat = make_catalog()
    quiet = raw_product(10, business=QUIET["business"], size_model=QUIET["size_model"], link=QUIET["link"])
    cat[None].append(quiet)
    cat["ar"].append(quiet)
    result = run(monkeypatch, FakeVendure(cat))
    visible = _everything_visible(client, result["id"])
    for fragment in QUIET_FRAGMENTS:
        assert fragment not in visible, f"se filtró «{fragment}»"
    quiet_rows = [i for i in items(client, run_id=result["id"], only_issues="false", page_size=200)["items"]
                  if i["product_id"] == "10"]
    assert quiet_rows and quiet_rows[0]["issues"] == []


def test_la_regla_fab_dice_que_hubo_coincidencia_pero_no_con_que(monkeypatch, client):
    result = run(monkeypatch, FakeVendure(make_catalog()))
    item = items(client, rule="FAB", run_id=result["id"])["items"][0]
    assert item["product_id"] == "5"
    detail = {i["rule"]: i["detail"] for i in item["issues"]}["FAB"]
    assert "coincide con nombre de fábrica: sí" in detail
    assert "coincide con código de proveedor: sí" in detail
    assert "coincide con link de proveedor: sí" in detail
    for fragment in SECRET_FRAGMENTS:
        assert fragment not in detail.casefold()
    # COD muestra el código que está en el título (público), no el campo del proveedor.
    assert {i["rule"]: i["detail"] for i in item["issues"]}["COD"] == "ZK-7731"


def test_el_dataclass_de_producto_no_muestra_los_campos_de_proveedor_en_repr():
    p = vendure_client.ProductTexts(
        id="1", enabled=True, product_code=None, updated_at=None, translations=[],
        supplier_business="Yongkang Secreta", supplier_size_model="ZK-7731", supplier_link="https://x/1234567890")
    assert "Yongkang" not in repr(p) and "ZK-7731" not in repr(p) and "1234567890" not in repr(p)


# ─── API ───────────────────────────────────────────────────────────

def items(client, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    r = client.get("/api/seo/text-audit/items?" + qs)
    assert r.status_code == 200, r.text
    return r.json()


def test_los_endpoints_piden_sesion():
    c = TestClient(main_mod.app)
    for method, url in (("get", "/api/seo/text-audit/summary"), ("get", "/api/seo/text-audit/items"),
                        ("get", "/api/seo/text-audit/export.csv"), ("post", "/api/seo/text-audit/run"),
                        ("get", "/api/seo/text-audit/lists"), ("put", "/api/seo/text-audit/lists/marcas"),
                        ("delete", "/api/seo/text-audit/lists/marcas")):
        assert getattr(c, method)(url, follow_redirects=False).status_code in (401, 302, 303, 307), url


def test_las_fechas_salen_en_utc_con_z(monkeypatch, client):
    run(monkeypatch, FakeVendure({"ar": [], None: [raw_product(1)]}))
    data = client.get("/api/seo/text-audit/summary").json()["run"]
    assert data["started_at"].endswith("Z") and data["finished_at"].endswith("Z")
    from datetime import datetime, timezone

    aware = datetime(2026, 10, 9, 15, 30, tzinfo=timezone.utc)
    assert text_audit._utc_iso(aware) == "2026-10-09T15:30:00Z"
    assert text_audit._utc_iso(None) is None


def test_summary_sin_corridas(client):
    data = client.get("/api/seo/text-audit/summary").json()
    assert data["run"] is None and data["running"] is False
    assert {r["id"] for r in data["rules"]} >= {"COD", "MAR", "FAB", "LARGO", "RELLENO", "SLUG_NO_COINCIDE",
                                                "DUP_EXACTO", "NOMBRE_ES_CODIGO", "ESPACIOS", "SIN_DESCRIPCION",
                                                "DESC_CON_HTML_EN_META", "SIN_ES_AR", "DUP_CASI"}
    assert items(client)["items"] == []
    assert client.get("/api/seo/text-audit/export.csv").status_code == 404


def test_items_filtros_y_conteos(monkeypatch, client):
    result = run(monkeypatch, FakeVendure(make_catalog()))
    rid = result["id"]
    allrows = items(client, run_id=rid, only_issues="false", page_size=200)
    assert allrows["total"] == 10
    con_problemas = items(client, run_id=rid)
    assert con_problemas["total"] == sum(1 for r in allrows["items"] if r["n_issues"])
    assert con_problemas["items"][0]["n_issues"] >= con_problemas["items"][-1]["n_issues"]

    mar = items(client, run_id=rid, rule="MAR")
    assert {i["product_id"] for i in mar["items"]} == {"2", "9"}
    assert mar["counts"]["MAR"] == 2                        # los conteos ignoran el filtro de regla
    assert items(client, run_id=rid, rule="MAR", enabled="disabled")["total"] == 1
    assert items(client, run_id=rid, rule="MAR", enabled="enabled")["counts"].get("ESPACIOS") is None
    assert {i["product_id"] for i in items(client, run_id=rid, lang="es")["items"]} == {"2", "3"}
    assert items(client, run_id=rid, channel="solo_default", only_issues="false")["total"] == 2
    assert {i["product_id"] for i in items(client, run_id=rid, channel="ar", only_issues="false", page_size=200)["items"]} \
        == {"1", "2", "3", "4", "5", "6", "8"}
    assert [i["product_id"] for i in items(client, run_id=rid, q="kuromi")["items"]] == ["9"]
    assert [i["product_id"] for i in items(client, run_id=rid, q="BX00717")["items"]] == ["4"]
    assert [i["product_id"] for i in items(client, run_id=rid, q="7", only_issues="false")["items"]
            if i["product_id"] == "7"] == ["7"]
    assert sorted(allrows["languages"]) == ["es", "es_AR"]


def test_items_pagina(monkeypatch, client):
    result = run(monkeypatch, FakeVendure({"ar": [], None: [raw_product(i, translations=[("es", f"Pack {i}", "", "")]) for i in range(1, 31)]}))
    p0 = items(client, run_id=result["id"], page=0, page_size=10)
    p2 = items(client, run_id=result["id"], page=2, page_size=10)
    assert p0["total"] == 30 and len(p0["items"]) == 10 and len(p2["items"]) == 10
    assert {i["product_id"] for i in p0["items"]}.isdisjoint({i["product_id"] for i in p2["items"]})


@pytest.mark.parametrize("params", [
    "rule=NOPE", "enabled=maybe", "channel=mx", "page=-1", "page_size=0", "page_size=500",
    "lang=es%20AR", "run_id=0", "run_id=99999999999", "q=" + "a" * 101,
])
def test_items_valida_los_parametros(client, params):
    assert client.get("/api/seo/text-audit/items?" + params).status_code in (404, 422)


def test_items_run_inexistente_es_404(client):
    assert client.get("/api/seo/text-audit/items?run_id=424242").status_code == 404


def test_la_busqueda_escapa_los_comodines(monkeypatch, client):
    result = run(monkeypatch, FakeVendure({"ar": [], None: [
        raw_product(1, translations=[("es_AR", "Taza 100% algodón", "taza-100", "")]),
        raw_product(2, translations=[("es_AR", "Taza 1000 algodón", "taza-1000", "")])]}))
    ids = [i["product_id"] for i in items(client, run_id=result["id"], q="100%25", only_issues="false")["items"]]
    assert ids == ["1"]


def test_export_csv(monkeypatch, client):
    cat = make_catalog()
    cat[None][0]["translations"][0]["name"] = "=HYPERLINK(\"http://x\")"          # inyección de fórmula
    result = run(monkeypatch, FakeVendure(cat))
    resp = client.get(f"/api/seo/text-audit/export.csv?run_id={result['id']}&rule=MAR")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"].startswith('attachment; filename="auditoria-textos-')
    assert resp.content.startswith(b"\xef\xbb\xbf")                                # BOM para Excel
    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    assert rows[0] == list(text_audit.CSV_COLUMNS)
    body = rows[1:]
    assert {r[0] for r in body} == {"2", "9"}
    assert all("MAR" in r[10].split() for r in body)
    assert all("MAR: " in r[11] for r in body)
    everything = client.get(f"/api/seo/text-audit/export.csv?run_id={result['id']}&only_issues=false").content.decode("utf-8-sig")
    assert "'=HYPERLINK" in everything and ",=HYPERLINK" not in everything and "\n=HYPERLINK" not in everything


# ─── Disparo manual ────────────────────────────────────────────────

def test_run_dispara_en_background_y_responde_202(monkeypatch, client):
    called = []

    async def fake_job(trigger="cron"):
        called.append(trigger)

    monkeypatch.setattr(jobs, "seo_text_audit", fake_job)
    r = client.post("/api/seo/text-audit/run")
    assert r.status_code == 202 and r.json() == {"status": "scheduled", "read_only": True}
    assert called == ["manual"]


def test_run_responde_409_si_hay_una_en_curso(monkeypatch, client):
    with Session(engine) as s:
        s.add(TextAuditRun(status="running"))
        s.commit()
    assert client.post("/api/seo/text-audit/run").status_code == 409
    assert client.get("/api/seo/text-audit/summary").json()["running"] is True


def test_run_responde_429_si_la_ultima_arranco_hace_segundos(client):
    with Session(engine) as s:
        s.add(TextAuditRun(status="ok", finished_at=utcnow()))
        s.commit()
    r = client.post("/api/seo/text-audit/run")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


def test_una_corrida_huerfana_no_bloquea_para_siempre(monkeypatch):
    with Session(engine) as s:
        s.add(TextAuditRun(status="running", started_at=utcnow() - timedelta(hours=3)))
        s.commit()
    result = run(monkeypatch, FakeVendure({"ar": [], None: [raw_product(1)]}))
    assert result["status"] == "ok"
    with Session(engine) as s:
        orphan = s.exec(select(TextAuditRun).where(TextAuditRun.status == "failed")).one()
    assert "interrumpida" in orphan.error


def test_se_conservan_solo_las_ultimas_corridas(monkeypatch):
    fake = FakeVendure({"ar": [], None: [raw_product(1)]})
    for _ in range(text_audit.KEEP_RUNS + 3):
        run(monkeypatch, fake)
    with Session(engine) as s:
        runs = s.exec(select(TextAuditRun)).all()
        run_ids = {r.id for r in runs}
        item_run_ids = {i.run_id for i in s.exec(select(TextAuditItem))}
    assert len(runs) == text_audit.KEEP_RUNS
    assert item_run_ids <= run_ids


def test_el_job_mueve_el_marcador_solo_si_la_corrida_termino(monkeypatch):
    import asyncio

    async def fake_run(trigger="cron"):
        return {"status": fake_run.status}

    monkeypatch.setattr(text_audit, "run_text_audit", fake_run)
    jobs._set_meta(f"{jobs._LAST_RUN_PREFIX}{jobs.SEO_TEXT_AUDIT_JOB_ID}", "")
    fake_run.status = "failed"
    asyncio.run(jobs.seo_text_audit())
    assert jobs._last_job_run(jobs.SEO_TEXT_AUDIT_JOB_ID) is None
    fake_run.status = "degraded"
    asyncio.run(jobs.seo_text_audit())
    assert jobs._last_job_run(jobs.SEO_TEXT_AUDIT_JOB_ID) is not None


# ─── Programación ──────────────────────────────────────────────────

def _stub_settings(cron):
    return type("S", (), {"audit_interval_hours": 336, "verify_catalog_ttl_seconds": 300,
                          "seo_text_audit_cron_utc": cron})()


@pytest.fixture
def scheduler(monkeypatch):
    """Un scheduler propio (sin arrancar): no deja jobs colgados para otras pruebas."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    sch = AsyncIOScheduler()
    monkeypatch.setattr(jobs, "scheduler", sch)
    return sch


def test_el_job_semanal_se_registra_los_lunes_y_el_cron_vacio_lo_apaga(monkeypatch, scheduler):
    from datetime import datetime, timezone

    monkeypatch.setattr(jobs, "get_settings", lambda: _stub_settings("30 7 * * mon"))
    jobs.register_jobs()
    job = scheduler.get_job(jobs.SEO_TEXT_AUDIT_JOB_ID)
    assert job is not None
    nxt = job.trigger.get_next_fire_time(None, datetime.now(timezone.utc))
    assert nxt.weekday() == 0 and (nxt.hour, nxt.minute) == (7, 30)
    assert getattr(job, "next_run_time", None) is None          # sin marcador no se recupera nada

    sch2 = type(scheduler)()
    monkeypatch.setattr(jobs, "scheduler", sch2)
    monkeypatch.setattr(jobs, "get_settings", lambda: _stub_settings(""))
    jobs.register_jobs()
    assert sch2.get_job(jobs.SEO_TEXT_AUDIT_JOB_ID) is None


def test_un_cron_invalido_cae_al_default_en_vez_de_romper_el_arranque(monkeypatch, scheduler):
    monkeypatch.setattr(jobs, "get_settings", lambda: _stub_settings("no es un cron"))
    jobs.register_jobs()
    assert scheduler.get_job(jobs.SEO_TEXT_AUDIT_JOB_ID) is not None


def test_la_corrida_perdida_se_recupera_al_arrancar(monkeypatch, scheduler):
    from datetime import datetime, timezone

    jobs._set_meta(f"{jobs._LAST_RUN_PREFIX}{jobs.SEO_TEXT_AUDIT_JOB_ID}",
                   (datetime.now(timezone.utc) - timedelta(days=9)).isoformat())
    monkeypatch.setattr(jobs, "get_settings", lambda: _stub_settings("30 7 * * mon"))
    jobs.register_jobs()
    delay = (scheduler.get_job(jobs.SEO_TEXT_AUDIT_JOB_ID).next_run_time - datetime.now(timezone.utc)).total_seconds()
    assert 0 < delay <= jobs._STARTUP_GRACE.total_seconds() + 5


# ─── Listas editables ──────────────────────────────────────────────

def test_listas_get_put_delete(client):
    got = client.get("/api/seo/text-audit/lists").json()["lists"]
    assert set(got) == {"marcas", "relleno", "tecnicos"}
    assert "Kuromi" in got["marcas"]["items"] and got["marcas"]["modified"] is False

    r = client.put("/api/seo/text-audit/lists/marcas", json={"items": ["  Foobar ", "foobar", "Bazqux"]})
    assert r.status_code == 200 and r.json()["items"] == ["Foobar", "Bazqux"]
    got = client.get("/api/seo/text-audit/lists").json()["lists"]["marcas"]
    assert got == {"items": ["Foobar", "Bazqux"], "modified": True}

    r = client.delete("/api/seo/text-audit/lists/marcas")
    assert r.status_code == 200 and "Kuromi" in r.json()["items"] and r.json()["modified"] is False
    assert client.get("/api/seo/text-audit/lists").json()["lists"]["marcas"]["modified"] is False


@pytest.mark.parametrize("name,body,status", [
    ("marcas", {"items": ["a" * 61]}, 422), ("marcas", {"items": "x"}, 422), ("marcas", {}, 422),
    ("marcas", {"items": [1, 2]}, 422), ("nope", {"items": ["a"]}, 404),
    ("marcas", {"items": [f"m{i}" for i in range(600)]}, 422),
])
def test_listas_validan(client, name, body, status):
    assert client.put(f"/api/seo/text-audit/lists/{name}", json=body).status_code == status


def test_la_lista_guardada_manda_en_la_proxima_corrida(monkeypatch, client):
    cat = {"ar": [], None: [raw_product(1, translations=[("es_AR", "Taza Foobar Rosa", "taza-foobar-rosa", "Una taza de cerámica común y corriente.")]),
                            raw_product(2, translations=[("es_AR", "Funda iPhone Rosa", "funda-iphone-rosa", "Una funda de silicona común y corriente.")])]}
    first = run(monkeypatch, FakeVendure(cat))
    assert {k[0] for k, v in rows_of(first["id"]).items() if "MAR" in rules_in(v)} == {"2"}
    client.put("/api/seo/text-audit/lists/marcas", json={"items": ["Foobar"]})
    second = run(monkeypatch, FakeVendure(cat))
    assert {k[0] for k, v in rows_of(second["id"]).items() if "MAR" in rules_in(v)} == {"1"}


def test_una_lista_corrupta_en_la_base_cae_a_la_de_fabrica(monkeypatch):
    with Session(engine) as s:
        s.add(Setting(key="seo:lista:marcas", value="{no es json"))
        s.commit()
    result = run(monkeypatch, FakeVendure({"ar": [], None: [raw_product(2, translations=[("es_AR", "Funda iPhone", "funda-iphone", "")])]}))
    assert "MAR" in rules_in(rows_of(result["id"])[("2", "es_AR")])


def test_las_rutas_no_usan_ninguna_funcion_de_escritura_de_vendure():
    """Defensa estática: el módulo de rutas y el de la auditoría no importan ni llaman a nada que escriba en Vendure."""
    import inspect

    from app.seo import text_audit as ta

    for mod in (seo_routes, ta):
        src = inspect.getsource(mod)
        for forbidden in ("disable_product", "enable_product", "_set_enabled", "updateProduct", "mutation"):
            assert forbidden not in src, f"{mod.__name__} menciona {forbidden}"
