"""QA independiente de HG1: la API del dashboard.

  * auth: todos los endpoints nuevos responden 401 (no 200/302) sin una cookie válida: sin cookie, cookie
    adulterada, vencida, firmada con otro secreto, con una API key de otro cliente o con rutas "ingeniosas";
  * "Auditar ahora": 409 si hay una corrida, 429 con Retry-After si arrancó hace < 60 s, sin filas nuevas;
  * filtros: cada combinación contra una verdad calculada aparte, parámetros inválidos = 422 (nunca 500),
    inyección SQL inerte, paginación sin solapes;
  * CSV: ninguna celda de texto puede empezar con = + - @ tab CR (inyección de fórmulas);
  * listas editables: validación, tope de tamaño, efecto de vaciar una lista y quién la cambió.
"""

from __future__ import annotations

import asyncio
import csv
import io
import itertools
import json
import os
import time
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import auth  # noqa: E402
from app import main as main_mod  # noqa: E402
from app.clock import utcnow  # noqa: E402
from app.db.models import AuditLog, Setting, TextAuditItem, TextAuditRun  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.scheduler import jobs  # noqa: E402
from app.seo import text_audit  # noqa: E402
from app.seo import text_rules as rules  # noqa: E402
from tests.seo_fixtures import FakeVendure, install, raw_product  # noqa: E402
from tests.test_seo_text_audit import client, env, items, make_catalog, rows_of, run  # noqa: E402,F401

BASE = "/api/seo/text-audit"
ENDPOINTS = (
    ("get", f"{BASE}/summary"), ("get", f"{BASE}/items"), ("get", f"{BASE}/items?rule=MAR&page=2"),
    ("get", f"{BASE}/export.csv"), ("post", f"{BASE}/run"), ("get", f"{BASE}/lists"),
    ("put", f"{BASE}/lists/marcas"), ("put", f"{BASE}/lists/relleno"), ("put", f"{BASE}/lists/tecnicos"),
    ("delete", f"{BASE}/lists/marcas"), ("delete", f"{BASE}/lists/relleno"), ("delete", f"{BASE}/lists/tecnicos"),
)


def _call(c: TestClient, method: str, url: str, **kw):
    if method == "put":
        kw.setdefault("json", {"items": ["Foobar"]})
    return getattr(c, method)(url, follow_redirects=False, **kw)


# ─── Auth ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,url", ENDPOINTS)
def test_sin_cookie_es_401_exacto(method, url):
    c = TestClient(main_mod.app)
    r = _call(c, method, url, headers={"accept": "application/json"})
    assert r.status_code == 401, (method, url, r.status_code)
    assert r.json() == {"detail": "No autenticado"}


@pytest.mark.parametrize("method,url", ENDPOINTS)
def test_con_cookie_adulterada_vencida_o_ajena_es_401(method, url):
    good = auth.issue_session_token("admin")
    payload, _, sig = good.partition(".")
    forged = {
        "firma_cambiada": f"{payload}.{sig[:-2]}AA",
        "payload_cambiado": f"{auth._b64url(json.dumps({'u': 'root', 'exp': 9999999999}).encode())}.{sig}",
        "sin_firma": payload + ".",
        "basura": "no-es-un-token",
        "vacia": "",
    }
    c = TestClient(main_mod.app)
    for label, token in forged.items():
        c.cookies.clear()
        c.cookies.set(auth.COOKIE_NAME, token)
        assert _call(c, method, url, headers={"accept": "application/json"}).status_code == 401, label
    # vencida: se firma con el secreto bueno pero con un exp en el pasado
    old = json.dumps({"u": "admin", "exp": int(time.time()) - 5}, separators=(",", ":")).encode()
    import hashlib
    import hmac
    p = auth._b64url(old)
    s = auth._b64url(hmac.new(auth._signing_secret(), p.encode("ascii"), hashlib.sha256).digest())
    c.cookies.clear()
    c.cookies.set(auth.COOKIE_NAME, f"{p}.{s}")
    assert _call(c, method, url, headers={"accept": "application/json"}).status_code == 401


def test_una_api_key_de_otro_cliente_no_abre_el_dashboard(monkeypatch):
    from app import security
    monkeypatch.setattr(security, "configured_api_keys", lambda: {"luis": "k-luis", "paco": "k-paco"})
    c = TestClient(main_mod.app)
    for method, url in ENDPOINTS:
        for headers in ({"x-api-key": "k-luis"}, {"x-api-key": "k-paco"}, {"x-oficina-key": "cualquiera"},
                        {"authorization": "Bearer k-luis"}):
            r = _call(c, method, url, headers={**headers, "accept": "application/json"})
            assert r.status_code == 401, (method, url, headers, r.status_code)


@pytest.mark.parametrize("path", [
    "//api/seo/text-audit/summary", "/api//seo/text-audit/summary", "/api/seo/text-audit/summary/",
    "/api/oficina/../seo/text-audit/summary", "/static/../api/seo/text-audit/summary",
    "/verify/../api/seo/text-audit/summary", "/app/../api/seo/text-audit/summary",
    "/api/seo/text-audit/summary%2f", "/api/login/../seo/text-audit/summary", "/login/../api/seo/text-audit/summary",
    "/health/../api/seo/text-audit/summary", "/favicon.ico/../api/seo/text-audit/summary",
])
def test_rutas_ingeniosas_no_saltean_la_auth(path):
    c = TestClient(main_mod.app)
    r = c.get(path, follow_redirects=False, headers={"accept": "application/json"})
    assert r.status_code in (401, 404, 307, 308), (path, r.status_code)
    assert "rules" not in r.text and "text_audit" not in r.text


def test_con_cookie_valida_todos_los_endpoints_de_lectura_responden_200(monkeypatch, client):
    run(monkeypatch, FakeVendure(make_catalog()))
    for url in (f"{BASE}/summary", f"{BASE}/items", f"{BASE}/export.csv", f"{BASE}/lists"):
        assert client.get(url).status_code == 200, url


def test_los_metodos_que_no_existen_dan_405_y_no_500(client):
    assert client.get(f"{BASE}/run").status_code == 405
    assert client.post(f"{BASE}/summary").status_code == 405
    assert client.delete(f"{BASE}/items").status_code == 405
    assert client.post(f"{BASE}/lists/marcas", json={"items": []}).status_code == 405


# ─── Auditar ahora: 409 y 429 ──────────────────────────────────────

def test_409_con_corrida_en_curso_y_no_se_crea_ninguna_fila_nueva(client):
    with Session(engine) as s:
        s.add(TextAuditRun(status="running"))
        s.commit()
    r = client.post(f"{BASE}/run")
    assert r.status_code == 409 and "en curso" in r.json()["detail"]
    with Session(engine) as s:
        assert len(s.exec(select(TextAuditRun)).all()) == 1


def test_429_trae_retry_after_numerico_y_no_crea_filas(monkeypatch, client):
    for age, expect in ((5, 429), (59, 429)):
        with Session(engine) as s:
            for row in s.exec(select(TextAuditRun)).all():
                s.delete(row)
            s.add(TextAuditRun(status="ok", started_at=utcnow() - timedelta(seconds=age), finished_at=utcnow()))
            s.commit()
        r = client.post(f"{BASE}/run")
        assert r.status_code == expect, age
        assert 1 <= int(r.headers["retry-after"]) <= 60
        with Session(engine) as s:
            assert len(s.exec(select(TextAuditRun)).all()) == 1


def test_pasado_el_minuto_se_puede_volver_a_auditar(monkeypatch, client):
    called = []

    async def fake_job(trigger="cron"):
        called.append(trigger)

    monkeypatch.setattr(jobs, "seo_text_audit", fake_job)
    with Session(engine) as s:
        s.add(TextAuditRun(status="ok", started_at=utcnow() - timedelta(seconds=75), finished_at=utcnow()))
        s.commit()
    assert client.post(f"{BASE}/run").status_code == 202
    assert called == ["manual"]


def test_una_corrida_fallida_reciente_tambien_cuenta_para_el_429(client):
    """Si la última corrida (aunque haya fallado) arrancó hace segundos, se frena el golpeteo sobre Vendure."""
    with Session(engine) as s:
        s.add(TextAuditRun(status="failed", started_at=utcnow() - timedelta(seconds=3), error="x"))
        s.commit()
    assert client.post(f"{BASE}/run").status_code == 429


def test_el_doble_clic_nunca_deja_dos_corridas_a_la_vez(monkeypatch, client):
    """Dos POST casi simultáneos: pueden dar 202+202 (la segunda tarea se descarta por el lock), pero jamás dos
    corridas ni dos filas `running`."""
    install(monkeypatch, FakeVendure({"ar": [raw_product(i) for i in range(1, 30)], None: [raw_product(i) for i in range(1, 30)]}))
    cookie = {auth.COOKIE_NAME: auth.issue_session_token("admin")}

    async def go():
        transport = httpx.ASGITransport(app=main_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", cookies=cookie) as c:
            return await asyncio.gather(*(c.post(f"{BASE}/run") for _ in range(3)))

    async def main():
        resp = await go()
        for _ in range(200):                         # esperar a que termine lo que haya arrancado
            await asyncio.sleep(0)
            if not text_audit.text_audit_lock.locked():
                await asyncio.sleep(0.05)
                if not text_audit.text_audit_lock.locked():
                    break
        return resp

    codes = sorted(r.status_code for r in asyncio.run(main()))
    assert set(codes) <= {202, 409, 429}, codes
    assert codes.count(202) >= 1
    with Session(engine) as s:
        runs = s.exec(select(TextAuditRun)).all()
    assert len(runs) <= 1, f"{len(runs)} corridas por un doble clic ({codes})"
    assert all(r.status != "running" for r in runs)


# ─── Filtros: contra una verdad calculada aparte ───────────────────

def _expected(rows: list[TextAuditItem], rule, enabled, lang, channel, only_issues):
    out = []
    for r in rows:
        ids = [x for x in r.issues.strip(",").split(",") if x]
        if only_issues and not ids:
            continue
        if rule and rule not in ids:
            continue
        if enabled == "enabled" and not r.enabled:
            continue
        if enabled == "disabled" and r.enabled:
            continue
        if lang is not None and r.language_code != ("" if lang == "-" else lang):
            continue
        if channel == "ar" and r.in_ar is not True:
            continue
        if channel == "solo_default" and not (r.in_default is True and r.in_ar is False):
            continue
        out.append((r.product_id, r.language_code))
    return sorted(out)


def _big_catalog():
    prods = list(make_catalog()[None])
    prods += [
        raw_product(30, code="PA000123", translations=[("es_AR", "PA000123", "pa000123", "")]),
        raw_product(31, translations=[("es_AR", "Cable A6S USB-C 1m", "cable-a6s", "Cable de carga rápida para todos los dispositivos.")]),
        raw_product(32, enabled=False, translations=[("es_AR", "Funda Barbie Rosa  ", "funda-rosa", "")]),
        raw_product(33, translations=[]),
        raw_product(34, translations=[("es_MX", "Taza Mexicana", "taza-mexicana", "Taza de barro para café de olla tradicional.")]),
    ]
    ar = [p for p in prods if p["id"] in {"1", "2", "3", "4", "5", "6", "8", "30", "31", "34"}]
    return {"ar": ar, None: prods}


def test_cada_combinacion_de_filtros_da_exactamente_lo_esperado(monkeypatch, client):
    result = run(monkeypatch, FakeVendure(_big_catalog()))
    rid = result["id"]
    all_rows = list(rows_of(rid).values())
    assert len(all_rows) >= 12
    combos = itertools.product([None, *rules.RULE_IDS], ["all", "enabled", "disabled"], [None, "es_AR", "es", "es_MX", "-"],
                               ["all", "ar", "solo_default"], [True, False])
    checked = 0
    for rule, enabled, lang, channel, only in combos:
        q = f"run_id={rid}&enabled={enabled}&channel={channel}&only_issues={'true' if only else 'false'}&page_size=200"
        if rule:
            q += f"&rule={rule}"
        if lang:
            q += f"&lang={lang}"
        data = client.get(f"{BASE}/items?{q}").json()
        got = sorted((i["product_id"], i["language"]) for i in data["items"])
        want = _expected(all_rows, rule, enabled, lang, channel, only)
        assert got == want, (rule, enabled, lang, channel, only, got, want)
        assert data["total"] == len(want)
        checked += 1
    assert checked == 15 * 3 * 5 * 3 * 2


def test_el_filtro_COD_no_trae_a_NOMBRE_ES_CODIGO_ni_DUP_CASI_a_DUP_EXACTO(monkeypatch, client):
    result = run(monkeypatch, FakeVendure(_big_catalog()))
    rid = result["id"]
    cod = items(client, run_id=rid, rule="COD", page_size=200)["items"]
    for it in cod:
        assert "COD" in [i["rule"] for i in it["issues"]], it
    only_name_is_code = [i for i in items(client, run_id=rid, rule="NOMBRE_ES_CODIGO", page_size=200)["items"]]
    assert only_name_is_code, "el catálogo de prueba tiene nombres que son códigos"
    ids_cod = {i["product_id"] for i in cod}
    for it in only_name_is_code:
        has_cod = "COD" in [i["rule"] for i in it["issues"]]
        assert (it["product_id"] in ids_cod) == has_cod


def test_los_conteos_por_regla_son_los_de_los_otros_filtros_sin_el_de_regla(monkeypatch, client):
    result = run(monkeypatch, FakeVendure(_big_catalog()))
    rid = result["id"]
    all_rows = list(rows_of(rid).values())
    for enabled, channel in itertools.product(["all", "enabled", "disabled"], ["all", "ar", "solo_default"]):
        data = items(client, run_id=rid, enabled=enabled, channel=channel, rule="MAR")
        for rule, n in data["counts"].items():
            want = {pid for pid, _ in _expected(all_rows, rule, enabled, None, channel, True)}
            assert n == len(want), (enabled, channel, rule)
        assert set(data["counts"]) == {r for r in rules.RULE_IDS if _expected(all_rows, r, enabled, None, channel, True)}


def test_paginacion_sin_solapes_ni_huecos_y_con_orden_estable(monkeypatch, client):
    big = [raw_product(i, translations=[("es_AR", f"Producto Súper Mágico Ideal {i}  ", f"producto-{i}", "")]) for i in range(1, 61)]
    result = run(monkeypatch, FakeVendure({"ar": big, None: big}))
    rid = result["id"]
    seen: list[tuple[str, str]] = []
    for page in range(0, 7):
        data = items(client, run_id=rid, page=page, page_size=10)
        seen += [(i["product_id"], i["language"]) for i in data["items"]]
        assert data["total"] == 60
    assert len(seen) == len(set(seen)) == 60
    again: list[tuple[str, str]] = []
    for page in range(0, 7):
        again += [(i["product_id"], i["language"]) for i in items(client, run_id=rid, page=page, page_size=10)["items"]]
    assert again == seen, "el orden entre pedidos tiene que ser estable"
    assert items(client, run_id=rid, page=999, page_size=10)["items"] == []
    n_issues = [i["n_issues"] for p in range(0, 6) for i in items(client, run_id=rid, page=p, page_size=10)["items"]]
    assert n_issues == sorted(n_issues, reverse=True)


@pytest.mark.parametrize("qs", [
    "rule=NOPE", "rule=", "rule=mar", "rule=MAR%27%20OR%201=1", "enabled=maybe", "enabled=", "channel=x", "channel=AR",
    "lang=es%20AR", "lang=es_AR;DROP", "lang=" + "a" * 17, "lang=%27", "q=" + "a" * 101,
    "page=-1", "page=x", "page=10001", "page=1.5", "page_size=0", "page_size=201", "page_size=-1", "page_size=x",
    "run_id=0", "run_id=-1", "run_id=x", "run_id=99999999999", "run_id=2147483648", "only_issues=maybe",
])
def test_parametros_invalidos_son_422_y_nunca_500(client, qs):
    for url in (f"{BASE}/items?{qs}", f"{BASE}/export.csv?{qs}"):
        r = client.get(url)
        assert r.status_code in (404, 422), (url, r.status_code)
        assert r.status_code != 500


@pytest.mark.parametrize("payload", [
    "' OR '1'='1", "'; DROP TABLE text_audit_item; --", "%' OR 1=1 --", "\" OR \"\"=\"", "\\", "%", "_", "%%%%",
    "' UNION SELECT name,sql,1,1,1,1,1,1,1,1,1,1 FROM sqlite_master --", "𝕏" * 50, "<script>alert(1)</script>",
])
def test_la_busqueda_es_inerte_ante_inyeccion_y_caracteres_raros(monkeypatch, client, payload):
    result = run(monkeypatch, FakeVendure(make_catalog()))
    rid = result["id"]
    from urllib.parse import quote
    r = client.get(f"{BASE}/items?run_id={rid}&only_issues=false&q={quote(payload)}")
    assert r.status_code in (200, 422), r.status_code
    if r.status_code == 200:
        assert r.json()["total"] == 0, "un texto de búsqueda raro no puede devolver 'todo'"
    with Session(engine) as s:
        assert s.exec(select(TextAuditItem)).first() is not None, "la tabla sigue ahí"
    # y la tabla de verdad sigue existiendo para el CSV
    assert client.get(f"{BASE}/export.csv?run_id={rid}&q={quote(payload)}").status_code in (200, 422)


def test_buscar_por_id_codigo_slug_y_nombre(monkeypatch, client):
    result = run(monkeypatch, FakeVendure(make_catalog()))
    rid = result["id"]

    def ids(q):
        return sorted({i["product_id"] for i in items(client, run_id=rid, only_issues="false", q=q)["items"]})

    assert ids("BX00717") == ["4"] and ids("bx00717-609") == ["4"] and ids("  bx00717  ") == ["4"]
    assert ids("kuromi") == ["9"] and ids("KUROMI") == ["9"]
    assert "9" in ids("9") and ids("producto-1") == ["1"]
    assert ids("no-existe-este-texto") == []


# ─── CSV: inyección de fórmulas ────────────────────────────────────

PAYLOADS = [
    "=1+1", "+1+1", "-1+1", "@SUM(1+1)", "=cmd|' /C calc'!A0", "=HYPERLINK(\"http://evil\",\"x\")",
    "\t=1+1", "\r=1+1", "-2+3+cmd|' /C notepad'!'A1'", "@A1", "+cmd|' /C calc'!A0", "=-1",
]


def _injection_catalog():
    prods = []
    for i, p in enumerate(PAYLOADS, start=100):
        prods.append(raw_product(
            i, code=p[:60],
            translations=[("es_AR", p + " Funda iPhone", p.strip() + "-slug", "Descripción de la funda para el celular, suficientemente larga.")],
        ))
    prods.append(raw_product(200, translations=[("=es", "Nombre =normal", "slug", "")]))
    return {"ar": prods, None: prods}


def test_ninguna_celda_de_texto_del_csv_empieza_con_un_prefijo_de_formula(monkeypatch, client):
    result = run(monkeypatch, FakeVendure(_injection_catalog()))
    rid = result["id"]
    raw = client.get(f"{BASE}/export.csv?run_id={rid}&only_issues=false").content.decode("utf-8-sig")
    table = list(csv.reader(io.StringIO(raw, newline="")))
    header, body = table[0], table[1:]
    assert len(body) == len(PAYLOADS) + 1
    bad = []
    for row in body:
        for col, cell in zip(header, row):
            if cell.startswith(("=", "+", "-", "@", "\t", "\r")):
                bad.append((col, cell))
    assert bad == [], bad
    # Y la información no se pierde: el texto original sigue estando (con el apóstrofo delante).
    names = {row[header.index("nombre")] for row in body}
    assert any(n.lstrip("'").startswith("=cmd|") for n in names)


def test_el_csv_con_filtros_tambien_neutraliza_y_no_tiene_saltos_que_rompan_filas(monkeypatch, client):
    prods = [raw_product(1, translations=[("es_AR", "Funda\niPhone\r\n=1+1 Rosa", "funda-iphone", "")])]
    result = run(monkeypatch, FakeVendure({"ar": prods, None: prods}))
    raw = client.get(f"{BASE}/export.csv?run_id={result['id']}&rule=MAR").content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(raw, newline="")))
    assert len(rows) == 2 and len(rows[1]) == len(rows[0]) == len(text_audit.CSV_COLUMNS)


def test_csv_safe_cubre_los_seis_prefijos_de_la_guia_owasp():
    for prefix in ("=", "+", "-", "@", "\t", "\r"):
        assert text_audit.csv_safe(prefix + "x").startswith("'"), repr(prefix)
    for harmless in ("hola", "1+1", "x=1", "'=1", "", "Ñandú", " =x"):
        assert text_audit.csv_safe(harmless) == harmless
    assert text_audit.csv_safe(None) == "" and text_audit.csv_safe(0) == "0"


def test_el_csv_tiene_bom_y_cabecera_estable_y_el_nombre_de_archivo_es_seguro(monkeypatch, client):
    result = run(monkeypatch, FakeVendure(make_catalog()))
    r = client.get(f"{BASE}/export.csv?run_id={result['id']}")
    assert r.content[:3] == b"\xef\xbb\xbf"
    assert r.headers["content-disposition"].count('"') == 2
    assert all(c not in r.headers["content-disposition"] for c in ("\n", "\r", "/", "\\"))
    assert r.content.decode("utf-8-sig").splitlines()[0] == ",".join(text_audit.CSV_COLUMNS)


# ─── Listas editables ──────────────────────────────────────────────

def _put(client, name, items):
    return client.put(f"{BASE}/lists/{name}", json={"items": items})


def test_limites_exactos_de_cantidad_y_de_largo(client):
    assert _put(client, "marcas", [f"m{i}" for i in range(500)]).status_code == 200
    assert _put(client, "marcas", [f"m{i}" for i in range(501)]).status_code == 422
    assert _put(client, "marcas", ["a" * 60]).status_code == 200
    assert _put(client, "marcas", ["a" * 61]).status_code == 422
    assert _put(client, "marcas", ["ñ" * 60]).status_code == 200
    # Los duplicados no cuentan para el tope de 500 (sí para el de 550 del cuerpo crudo).
    assert _put(client, "marcas", [f"m{i}" for i in range(500)] + [f"M{i}" for i in range(50)]).status_code == 200
    assert _put(client, "marcas", [f"m{i}" for i in range(500)] + [f"M{i}" for i in range(51)]).status_code == 422
    client.delete(f"{BASE}/lists/marcas")


@pytest.mark.parametrize("body", [
    "no es json", "[]", "null", '{"items": null}', '{"items": {"a": 1}}', '{"items": [["x"]]}', '{"items": [null]}',
    '{"items": [true]}', '{"items": [1.5]}', '{"items": ["a\\u0000b"]}', '{"items": ["a\\u0007b"]}', '{"items": ["\\u001bx"]}',
    '{"items": "marca"}', '{"otro": []}',
])
def test_cuerpos_invalidos_son_4xx_y_no_cambian_nada(client, body):
    _put(client, "marcas", ["Antes"])
    r = client.put(f"{BASE}/lists/marcas", content=body, headers={"content-type": "application/json"})
    assert 400 <= r.status_code < 500, (body, r.status_code, r.text)
    assert client.get(f"{BASE}/lists").json()["lists"]["marcas"]["items"] == ["Antes"], "un PUT inválido no puede pisar la lista"


@pytest.mark.parametrize("name", ["Marcas", "MARCAS", "marca", "..", "%2e%2e", "marcas%2f..%2frelleno", "a" * 21, "marcas%00", "marcas "])
def test_nombres_de_lista_invalidos_son_404_o_422(client, name):
    for method in ("put", "delete"):
        r = getattr(client, method)(f"{BASE}/lists/{name}", **({"json": {"items": ["x"]}} if method == "put" else {}))
        assert r.status_code in (404, 405, 422), (method, name, r.status_code)


def test_normaliza_espacios_dedup_sin_mayusculas_y_conserva_acentos(client):
    r = _put(client, "relleno", ["  Súper   práctico ", "súper práctico", "SÚPER PRÁCTICO", "", "   ", "Mágico"])
    assert r.status_code == 200 and r.json()["items"] == ["Súper práctico", "Mágico"]


def test_delete_es_idempotente_y_devuelve_la_lista_de_fabrica(client):
    first = client.delete(f"{BASE}/lists/tecnicos")
    second = client.delete(f"{BASE}/lists/tecnicos")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() and "LED" in first.json()["items"] and first.json()["modified"] is False


def test_cada_lista_es_independiente(client):
    _put(client, "marcas", ["SoloEsta"])
    got = client.get(f"{BASE}/lists").json()["lists"]
    assert got["marcas"]["modified"] and not got["relleno"]["modified"] and not got["tecnicos"]["modified"]
    client.delete(f"{BASE}/lists/marcas")


def test_vaciar_la_lista_de_marcas_apaga_la_regla_MAR_en_silencio(monkeypatch, client):
    """Comportamiento a conocer: PUT con `items: []` (o todo en blanco) se guarda y desactiva la regla; el dashboard
    no avisa. Es válido por diseño (la lista guardada reemplaza a la de fábrica) pero es fácil de hacer sin querer."""
    cat = {"ar": [], None: [raw_product(1, translations=[("es_AR", "Funda iPhone Rosa", "funda-iphone-rosa", "")])]}
    assert any("MAR" in rules_ for rules_ in [r.issues for r in rows_of(run(monkeypatch, FakeVendure(cat))["id"]).values()])
    r = _put(client, "marcas", ["   ", ""])
    assert r.status_code == 200 and r.json()["items"] == []
    after = run(monkeypatch, FakeVendure(cat))
    assert all("MAR" not in r.issues for r in rows_of(after["id"]).values())
    client.delete(f"{BASE}/lists/marcas")


def test_el_valor_guardado_en_la_base_es_json_chico_y_con_su_clave(client):
    _put(client, "marcas", [f"marca{i}" for i in range(500)])
    with Session(engine) as s:
        row = s.get(Setting, "seo:lista:marcas")
    assert row is not None and json.loads(row.value)[0] == "marca0" and len(row.value) < 20_000
    client.delete(f"{BASE}/lists/marcas")


@pytest.mark.xfail(strict=True, reason=(
    "BUG (medio): cambiar o restablecer una lista no deja ningún rastro de QUIÉN lo hizo (ni en `settings`, ni en "
    "`audit_log`, ni en el log, ni en la respuesta). El resto de las ediciones del dashboard (semáforo, tiendas) anota "
    "al actor con auth.session_username(). Las listas cambian el resultado de la auditoría y, más adelante, las reglas "
    "duras de HG4."))
def test_quien_cambio_la_lista_queda_anotado(client, caplog):
    import logging

    c = TestClient(main_mod.app)
    c.cookies.set(auth.COOKIE_NAME, auth.issue_session_token("qa-pao@b2box.test"))
    with caplog.at_level(logging.INFO):
        r1 = c.put(f"{BASE}/lists/marcas", json={"items": ["Foobar"]})
        r2 = c.delete(f"{BASE}/lists/relleno")
    with Session(engine) as s:
        persisted = " ".join(f"{x.key} {x.value}" for x in s.exec(select(Setting)).all())
        persisted += " ".join(f"{a.action} {a.detail} {a.before} {a.after}" for a in s.exec(select(AuditLog)).all())
    seen = persisted + r1.text + r2.text + c.get(f"{BASE}/lists").text + "\n".join(r.getMessage() for r in caplog.records)
    assert "qa-pao@b2box.test" in seen


@pytest.mark.xfail(strict=True, reason=(
    "BUG (bajo, UX): si un canal no contesta, el motivo que ve el usuario en el dashboard es solo «TimeoutError» "
    "(asyncio.TimeoutError no trae mensaje): no dice cuánto esperó ni de qué canal se trata el tiempo."))
def test_el_motivo_de_un_canal_que_no_contesta_dice_que_fue_por_tiempo(monkeypatch):
    monkeypatch.setattr(text_audit, "READ_TIMEOUT_S", 0.2)

    async def reader(token):
        if token is None:
            await asyncio.Event().wait()
        return await text_audit.read_channel(token)

    install(monkeypatch, FakeVendure({"ar": [raw_product(1)], None: [raw_product(1)]}))
    result = asyncio.run(text_audit.run_text_audit(trigger="manual", reader=reader))
    reason = result["channels_failed"]["default"].lower()
    assert any(w in reason for w in ("tiempo", "segundos", "timeout", "tardó")) and len(reason) > len("timeouterror")
