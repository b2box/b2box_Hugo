"""HG1, ronda de arreglos (seguridad y QA): logs, topes de CPU y memoria, motivos de fallo, quién hizo qué,
reintento del schema, listas por defecto, medidas en FAB y CSV para Excel en es-AR."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import threading
import time

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import auth  # noqa: E402
from app import main as main_mod  # noqa: E402
from app.api import routes as api_routes  # noqa: E402
from app.api import seo_routes  # noqa: E402
from app.db.models import AuditLog, TextAuditItem  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.seo import lists as seo_lists  # noqa: E402
from app.seo import text_audit  # noqa: E402
from app.seo import text_rules as r  # noqa: E402
from app.seo.lists import TextLists  # noqa: E402
from tests.seo_fixtures import FakeVendure, install, raw_product  # noqa: E402
from tests.test_seo_text_audit import env, items, rows_of, rules_in, run  # noqa: E402,F401  (fixture autouse)

BASE = "/api/seo/text-audit"
LISTS = TextLists()


def audit(name, slug="", description="", **kw):
    return r.audit_translation(name, slug, description, product_code=None, lists=kw.pop("lists", LISTS),
                               matcher=kw.pop("matcher", None))


@pytest.fixture
def client() -> TestClient:
    c = TestClient(main_mod.app, raise_server_exceptions=False)
    c.cookies.set(auth.COOKIE_NAME, auth.issue_session_token("pao@b2box.test"))
    return c


# ─── M1: ni gql, ni httpx ni httpcore hablan de más con LOG_LEVEL=DEBUG ──

@pytest.fixture
def restore_loggers():
    names = ("gql", "gql.transport.httpx", "httpx", "httpcore")
    saved = {n: logging.getLogger(n).level for n in names}
    yield
    for n, lvl in saved.items():
        logging.getLogger(n).setLevel(lvl)


def test_configure_logging_en_debug_deja_gql_httpx_y_httpcore_en_warning(monkeypatch, restore_loggers):
    for n in ("gql", "httpx", "httpcore"):
        logging.getLogger(n).setLevel(logging.NOTSET)
    st = type("S", (), {"log_level": "DEBUG"})()
    monkeypatch.setattr(main_mod, "get_settings", lambda: st)
    main_mod._configure_logging()
    for n in ("gql", "gql.transport.httpx", "httpx", "httpcore", "httpcore.http11"):
        assert logging.getLogger(n).getEffectiveLevel() == logging.WARNING, n


def test_con_log_debug_no_sale_ninguna_respuesta_de_vendure_en_los_logs(monkeypatch, caplog):
    cat = {"ar": [raw_product(1, business="Zhongshan Lanxing Lighting Co", size_model="QW-4410", link="https://x.1688.com/offer/512345678901.html")]}
    cat[None] = cat["ar"]
    with caplog.at_level(logging.DEBUG):
        run(monkeypatch, FakeVendure(cat))
    noisy = [rec for rec in caplog.records if rec.name.split(".")[0] in ("gql", "httpx", "httpcore")]
    assert noisy == []
    text = "\n".join(rec.getMessage() for rec in caplog.records).casefold()
    assert "lanxing" not in text and "qw-4410" not in text and "512345678901" not in text


# ─── B1: nombres hostiles ───────────────────────────────────────────

def test_un_nombre_gigante_con_asteriscos_no_es_cuadratico():
    t0 = time.monotonic()
    audit("*" * 2_000_000)
    audit("a" * 2_000_000)
    audit("a*" * 1_000_000)
    audit(("ab " * 700_000) + "*")
    assert time.monotonic() - t0 < 3


def test_el_asterisco_sigue_marcandose_y_el_detalle_es_corto():
    assert audit("Globo 98*56cm Torre")["COD"] == "98*56cm (asterisco)"
    detail = audit("x" * 500 + "*" + "y" * 500)["COD"]
    assert len(detail) < 80


def test_la_fila_guardada_trunca_nombre_y_slug(monkeypatch):
    big = raw_product(1, translations=[("es_AR", "N" * 50_000, "s" * 50_000, "descripción " * 10)])
    result = run(monkeypatch, FakeVendure({"ar": [big], None: [big]}))
    row = rows_of(result["id"])[("1", "es_AR")]
    assert len(row.name) == r.MAX_NAME_CHARS and len(row.slug) == r.MAX_SLUG_CHARS


# ─── B2: familias enormes de nombres casi iguales ───────────────────

def test_una_familia_de_miles_de_fundas_casi_iguales_termina_y_no_guarda_n_cuadrado():
    n = 3000
    entries = [(f"{i}|es_AR", str(i), f"Funda Silicona Para Celular Modelo {i}") for i in range(n)]
    t0 = time.monotonic()
    out = r.find_duplicates(entries)
    assert time.monotonic() - t0 < 15
    assert len(out) == n and all("DUP_CASI" in v for v in out.values())
    detail = out["10|es_AR"]["DUP_CASI"]
    assert detail.startswith("parecido al producto ") and "(+" in detail
    assert detail.count(",") <= r.DUP_MAX_LISTED - 1 + 1          # nombra a lo sumo 5 ids


def test_una_familia_de_nombres_identicos_tampoco_es_cuadratica():
    entries = [(f"{i}|es_AR", str(i), "Funda de silicona para celular modelo único") for i in range(6000)]
    t0 = time.monotonic()
    out = r.find_duplicates(entries)
    assert time.monotonic() - t0 < 5
    assert out["0|es_AR"]["DUP_EXACTO"] == "mismo nombre que el producto 1, 2, 3, 4, 5 (+5994)"
    assert out["5|es_AR"]["DUP_EXACTO"].startswith("mismo nombre que el producto 0, 1, 2, 3, 4 (+")


def test_el_plazo_corta_la_busqueda_de_duplicados_por_dentro():
    entries = [(f"{i}|es_AR", str(i), f"Funda Silicona Para Celular Modelo {i}") for i in range(4000)]
    with pytest.raises(r.AuditTimeout):
        r.find_duplicates(entries, deadline=time.monotonic() - 1)


def test_si_la_evaluacion_pasa_el_plazo_la_corrida_queda_failed_con_motivo_fijo(monkeypatch):
    monkeypatch.setattr(text_audit, "EVAL_TIMEOUT_S", 0.0)
    result = run(monkeypatch, FakeVendure({"ar": [raw_product(i) for i in range(1, 80)], None: [raw_product(i) for i in range(1, 80)]}))
    assert result["status"] == "failed"
    assert result["error"] == "AuditTimeout: la evaluación de las reglas pasó el plazo de 0 s"


def test_el_wait_for_es_el_respaldo_si_la_evaluacion_no_mira_el_reloj(monkeypatch):
    monkeypatch.setattr(text_audit, "EVAL_TIMEOUT_S", -29.8)          # + 30 s de gracia = 0,2 s
    monkeypatch.setattr(text_audit, "evaluate", lambda *a, **k: time.sleep(0.8) or [])
    result = run(monkeypatch, FakeVendure({"ar": [raw_product(1)], None: [raw_product(1)]}))
    assert result["status"] == "failed" and result["error"].startswith("TimeoutError")


# ─── B3: términos de lista ──────────────────────────────────────────

def test_un_termino_de_la_lista_tiene_como_mucho_seis_palabras():
    assert seo_lists.sanitize_items(["uno dos tres cuatro cinco seis"]) == ["uno dos tres cuatro cinco seis"]
    for bad in ("a b c d e f g", " ".join("x" * 1 for _ in range(30)), "a1b2c3d4e5f6g7", "a-b-c-d-e-f-g"):
        with pytest.raises(ValueError, match="más de 6 palabras"):
            seo_lists.sanitize_items([bad])


def test_un_termino_largo_guardado_a_mano_en_la_base_no_agranda_la_ventana():
    idx = r._term_index(("a b c d e f g h i j", "Hello Kitty"))
    assert idx.max_len == 2 and ("hello", "kitty") in idx.by_parts


def test_el_put_de_un_termino_largo_es_422(client):
    resp = client.put(f"{BASE}/lists/marcas", json={"items": [" ".join("w" * 1 for _ in range(30))]})
    assert resp.status_code == 422 and "más de 6 palabras" in resp.json()["detail"]


# ─── B4: GET /items no carga todo ni frena el event loop ────────────

def test_las_rutas_con_base_son_sincronas_y_corren_en_el_threadpool():
    for fn in (seo_routes.summary, seo_routes.list_items, seo_routes.export_csv, seo_routes.get_lists,
               seo_routes.put_list, seo_routes.reset_list):
        assert not inspect.iscoroutinefunction(fn), fn.__name__
    assert inspect.iscoroutinefunction(seo_routes.run_now)         # tiene que crear la task


def test_items_corre_fuera_del_hilo_del_event_loop(monkeypatch, client):
    result = run(monkeypatch, FakeVendure({"ar": [raw_product(1)], None: [raw_product(1)]}))
    seen: list[str] = []
    real = text_audit.query_items

    def spy(*a, **k):
        seen.append(threading.current_thread().name)
        return real(*a, **k)

    monkeypatch.setattr(text_audit, "query_items", spy)
    assert client.get(f"{BASE}/items?run_id={result['id']}&only_issues=false").status_code == 200
    assert seen and seen[0] != threading.main_thread().name and seen[0] != "MainThread"


def test_los_conteos_se_agregan_en_sql_sin_traer_las_filas(monkeypatch, client):
    prods = [raw_product(i, translations=[("es_AR", f"Funda iPhone {i}", f"funda-{i}", "descripción " * 8)]) for i in range(1, 60)]
    result = run(monkeypatch, FakeVendure({"ar": prods, None: prods}))
    statements: list[str] = []

    def spy(conn, cursor, statement, params, context, executemany):  # noqa: ARG001
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", spy)
    try:
        data = items(client, run_id=result["id"], rule="MAR", page_size=5)
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    assert data["total"] == 59 and len(data["items"]) == 5 and data["counts"]["MAR"] == 59
    selects = [s for s in statements if "FROM text_audit_item" in s]
    wide = [s for s in selects if "text_audit_item.details" in s or "text_audit_item.name" in s]
    assert wide and all("LIMIT" in s for s in wide), "solo la página trae columnas anchas"
    facet = [s for s in selects if "count(DISTINCT" in s.replace("COUNT(DISTINCT", "count(DISTINCT")]
    assert len(facet) == 1 and "text_audit_item.details" not in facet[0]


# ─── B5: datos raros no tiran la corrida ────────────────────────────

def test_traducciones_con_el_mismo_idioma_se_deduplican_y_queda_el_aviso(monkeypatch):
    weird = raw_product(7, translations=[("es_AR", "Taza uno", "taza-uno", "Taza larga y descriptiva para todos."),
                                         ("es-ar", "Taza dos", "taza-dos", "Otra taza larga y descriptiva."),
                                         ("es", "Taza tres", "taza-tres", "Más texto descriptivo de la taza.")])
    result = run(monkeypatch, FakeVendure({"ar": [weird], None: [weird]}))
    assert result["status"] == "ok" and result["rows_total"] == 2
    assert set(rows_of(result["id"])) == {("7", "es_AR"), ("7", "es")}
    assert rows_of(result["id"])[("7", "es_AR")].name == "Taza uno"
    assert "idioma repetido" in result["notes"] and "producto 7" in result["notes"]


def test_un_producto_que_rompe_una_regla_se_saltea_y_los_demas_se_guardan(monkeypatch):
    real = r.audit_translation

    def boom(name, *a, **k):
        if name == "Producto 2":
            raise ValueError("dato raro con supplierBusiness=Yongkang")
        return real(name, *a, **k)

    monkeypatch.setattr(r, "audit_translation", boom)
    prods = [raw_product(i) for i in (1, 2, 3)]
    result = run(monkeypatch, FakeVendure({"ar": prods, None: prods}))
    assert result["status"] == "ok" and result["products_total"] == 3 and result["rows_total"] == 2
    assert {k[0] for k in rows_of(result["id"])} == {"1", "3"}
    assert "producto 2: no se pudo evaluar (ValueError)" in result["notes"]
    assert "Yongkang" not in result["notes"]


def test_si_el_lote_de_filas_falla_se_reintenta_una_por_una_y_se_avisa(monkeypatch):
    real = text_audit.evaluate

    def with_clash(merged, lists, deadline=None, problems=None):
        rows = real(merged, lists, deadline, problems)
        clone = TextAuditItem(**{k: v for k, v in rows[0].__dict__.items() if not k.startswith("_") and k != "id"})
        return [*rows, clone]                                       # misma (producto, idioma): viola el índice único

    monkeypatch.setattr(text_audit, "evaluate", with_clash)
    prods = [raw_product(i) for i in (1, 2)]
    result = run(monkeypatch, FakeVendure({"ar": prods, None: prods}))
    assert result["status"] == "ok" and result["rows_total"] == 2
    assert "1 fila(s) no se pudieron guardar" in result["notes"]
    assert len(rows_of(result["id"])) == 2


# ─── B6: motivo fijo, detalle solo en el log ────────────────────────

def test_el_timeout_de_un_canal_dice_cuanto_espero_y_de_que_canal(monkeypatch):
    monkeypatch.setattr(text_audit, "READ_TIMEOUT_S", 0.2)

    async def reader(token):
        if token is None:
            await asyncio.Event().wait()
        return await text_audit.read_channel(token)

    install(monkeypatch, FakeVendure({"ar": [raw_product(1)], None: [raw_product(1)]}))
    result = asyncio.run(text_audit.run_text_audit(trigger="manual", reader=reader))
    assert result["channels_failed"] == {"default": "TimeoutError: Vendure no respondió en 0.2 s (canal default)"}


def test_el_texto_de_la_excepcion_no_llega_ni_a_la_base_ni_a_la_api(monkeypatch, client, caplog):
    secret = "supplierBusiness=Yongkang Hardware http://admin.interno/admin-api Authorization: Bearer abc123"

    async def reader(token):
        raise RuntimeError(secret)

    install(monkeypatch, FakeVendure({"ar": [], None: []}))
    with caplog.at_level(logging.ERROR):
        result = asyncio.run(text_audit.run_text_audit(trigger="manual", reader=reader))
    assert result["status"] == "failed"
    assert result["error"].startswith("AuditFailed") is False
    assert "RuntimeError: error inesperado (canal ar); el detalle está en el log del servidor" in result["error"]
    shown = result["error"] + client.get(f"{BASE}/summary").text
    for frag in ("Yongkang", "admin.interno", "abc123"):
        assert frag not in shown
    logs = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "RuntimeError" in logs and "abc123" not in logs            # en el log va el detalle, ya sin credenciales


def test_un_fallo_inesperado_del_run_guarda_solo_tipo_y_frase_fija(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("INSERT INTO text_audit_item ... secreto-del-servidor")

    monkeypatch.setattr(text_audit, "_save_results", boom)
    result = run(monkeypatch, FakeVendure({"ar": [raw_product(1)], None: [raw_product(1)]}))
    assert result["status"] == "failed"
    assert result["error"] == "RuntimeError: error inesperado; el detalle está en el log del servidor"


# ─── B7: quién hizo qué ─────────────────────────────────────────────

def _audit_rows(action=None):
    with Session(engine) as s:
        stmt = select(AuditLog).where(AuditLog.source == "seo")
        if action:
            stmt = stmt.where(AuditLog.action == action)
        return s.exec(stmt).all()


def test_el_cambio_de_una_lista_deja_actor_y_diferencia_en_el_audit_log(client):
    with Session(engine) as s:
        for row in s.exec(select(AuditLog)).all():
            s.delete(row)
        s.commit()
    r1 = client.put(f"{BASE}/lists/marcas", json={"items": ["Foobar", "Kuromi"]})
    assert r1.status_code == 200
    (row,) = _audit_rows("seo_list_changed")
    assert "pao@b2box.test" in row.detail and "«marcas»" in row.detail and "+1" in row.detail and "Foobar" in (row.after or "")
    assert row.note == "por pao@b2box.test"
    client.delete(f"{BASE}/lists/marcas")
    (reset,) = _audit_rows("seo_list_reset")
    assert "pao@b2box.test" in reset.detail and "restableció" in reset.detail


def test_un_put_rechazado_no_deja_rastro_de_cambio(client):
    before = len(_audit_rows())
    assert client.put(f"{BASE}/lists/marcas", json={"items": []}).status_code == 422
    assert client.put(f"{BASE}/lists/marcas", json={"items": ["a" * 61]}).status_code == 422
    assert len(_audit_rows()) == before


def test_disparar_a_mano_anota_quien_pero_los_rechazos_no(monkeypatch, client):
    from app.scheduler import jobs

    async def fake_job(trigger="cron"):
        return None

    monkeypatch.setattr(jobs, "seo_text_audit", fake_job)
    with Session(engine) as s:
        for row in s.exec(select(AuditLog)).all():
            s.delete(row)
        s.commit()
    assert client.post(f"{BASE}/run").status_code == 202
    (row,) = _audit_rows("seo_audit_requested")
    assert "pao@b2box.test" in row.detail and row.product_id == "-"
    from app.db.models import TextAuditRun
    with Session(engine) as s:
        s.add(TextAuditRun(status="running"))
        s.commit()
    assert client.post(f"{BASE}/run").status_code == 409
    assert len(_audit_rows("seo_audit_requested")) == 1


def test_el_audit_log_muestra_titulos_humanos_para_las_acciones_nuevas():
    for action in ("seo_audit_requested", "seo_list_changed", "seo_list_reset"):
        assert action in api_routes._ACTION_LABELS and api_routes._ACTION_LABELS[action]["title"] != action


# ─── B8: el schema se vuelve a probar, el CSV avisa ─────────────────

def test_si_el_schema_no_tenia_los_campos_la_proxima_corrida_los_vuelve_a_pedir(monkeypatch):
    prods = [raw_product(5, business="Yiwu Yongkang Hardware Co", translations=[("es_AR", "Pinza Yongkang Pro", "pinza-yongkang-pro", "Pinza de acero muy resistente.")])]
    fake = FakeVendure({"ar": prods, None: prods})
    fake.reject_supplier_fields = True
    first = run(monkeypatch, fake)
    assert "FAB solo compara el link" in first["notes"] and "FAB" not in rules_in(rows_of(first["id"])[("5", "es_AR")])
    fake.reject_supplier_fields = False                             # el schema se actualizó: sin reiniciar nada
    second = run(monkeypatch, fake)
    assert second["notes"] is None and "FAB" in rules_in(rows_of(second["id"])[("5", "es_AR")])


def test_el_csv_avisa_si_se_corto(monkeypatch, client):
    monkeypatch.setattr(text_audit, "EXPORT_MAX_ROWS", 5)
    prods = [raw_product(i, translations=[("es_AR", f"Funda iPhone {i}", f"funda-{i}", "")]) for i in range(1, 13)]
    result = run(monkeypatch, FakeVendure({"ar": prods, None: prods}))
    resp = client.get(f"{BASE}/export.csv?run_id={result['id']}&rule=MAR")
    assert resp.headers["x-export-truncated"] == "1" and resp.headers["x-export-total"] == "12"
    assert resp.headers["x-export-max-rows"] == "5"
    assert len(resp.content.decode("utf-8-sig").strip().splitlines()) == 1 + 5
    small = client.get(f"{BASE}/export.csv?run_id={result['id']}&rule=MAR&q=funda iphone 1")
    assert small.headers["x-export-truncated"] == "0"


# ─── CSV para Excel en es-AR ────────────────────────────────────────

def test_el_csv_va_con_punto_y_coma_y_bom_por_defecto_y_acepta_otros_separadores(monkeypatch, client):
    result = run(monkeypatch, FakeVendure({"ar": [raw_product(1, translations=[("es_AR", "Funda iPhone, rosa", "funda", "")])],
                                           None: [raw_product(1, translations=[("es_AR", "Funda iPhone, rosa", "funda", "")])]}))
    rid = result["id"]
    default = client.get(f"{BASE}/export.csv?run_id={rid}&only_issues=false")
    assert default.content.startswith(b"\xef\xbb\xbf")
    head = default.content.decode("utf-8-sig").splitlines()[0]
    assert head == ";".join(text_audit.CSV_COLUMNS)
    assert '"Funda iPhone, rosa"' not in default.text               # la coma ya no obliga a entrecomillar
    comma = client.get(f"{BASE}/export.csv?run_id={rid}&only_issues=false&sep=,")
    assert comma.content.decode("utf-8-sig").splitlines()[0] == ",".join(text_audit.CSV_COLUMNS)
    assert '"Funda iPhone, rosa"' in comma.text
    tab = client.get(f"{BASE}/export.csv?run_id={rid}&only_issues=false&sep=tab")
    assert tab.content.decode("utf-8-sig").splitlines()[0] == "\t".join(text_audit.CSV_COLUMNS)
    assert client.get(f"{BASE}/export.csv?run_id={rid}&sep=x").status_code == 422
    assert client.get(f"{BASE}/export.csv?run_id={rid}&sep=%3B").status_code == 200


# ─── Búsqueda con caracteres de control ─────────────────────────────

@pytest.mark.parametrize("q", ["%00", "a%00b", "%01%02", "%0a", "%7f"])
def test_un_caracter_de_control_en_la_busqueda_se_limpia_y_nunca_es_500(monkeypatch, client, q):
    result = run(monkeypatch, FakeVendure({"ar": [raw_product(1)], None: [raw_product(1)]}))
    for url in (f"{BASE}/items?run_id={result['id']}&only_issues=false&q={q}", f"{BASE}/export.csv?run_id={result['id']}&q={q}"):
        assert client.get(url).status_code == 200, url
    assert text_audit.clean_query("a\x00b\x1f c") == "ab c"


# ─── Listas por defecto ─────────────────────────────────────────────

@pytest.mark.parametrize("title,brand", [
    ("Conservadora al Vacío Inteligente everyU", "everyU"), ("Masajeador Íntimo Rosen placer", "Rosen"),
    ("Juguete Musical Otamatone Melodía", "Otamatone"), ("Funda iPhone15 Transparente", "iPhone"),
    ("Mangas Lets Slim Protección", "Let's Slim"), ("Mangas Let´s Slim Protección", "Let's Slim"),
    ("Mangas LET'S  SLIM", "Let's Slim"), ("Funda iPhone 15 Pro", "iPhone"),
])
def test_marcas_de_la_ronda(title, brand):
    assert brand in audit(title)["MAR"]


def test_one_piece_es_una_malla_no_una_marca_y_b2box_no_es_una_marca():
    assert "MAR" not in audit("Malla One Piece Enteriza para Mujer")
    assert "MAR" not in audit("Traje de Baño One Piece Deportivo")
    assert "MAR" not in audit("Estante B2BOX para Baño") and "MARCA_PROPIA" in audit("Estante B2BOX para Baño")


def test_siglas_tecnicas_nuevas_no_son_codigo():
    for sigla in ("IA", "AI", "VESA", "USBC", "USB-C", "SK5", "V8", "DPI", "PIR", "HSS", "SDS", "MDF", "BPA", "FDA", "BBQ",
                  "ISO", "TPR", "BMX", "PD", "QC", "RC", "SPA", "DC", "B22", "HB", "ZIP", "PS4", "PS5"):
        assert "COD" not in audit(f"Producto de prueba {sigla} para el hogar"), sigla


def test_relleno_ya_no_marca_ideal_para_ni_el_producto_y_marca_lo_nuevo():
    for ok in ("Mochila ideal para viajes", "Bolso con Compartimento para Traje Elegante", "Cubo Mágico 3x3 Velocidad",
               "Solución Salina para Lentes de Contacto", "Varita Mágica de Luces"):
        assert "RELLENO" not in audit(ok), ok
    for bad in ("Elegante Reloj Estilo Deportivo", "Mágico Vaso para Granizados", "Lámpara práctica de mesa",
                "Organizador Cómodo: Practicidad y Comodidad", "La Solución para tu cocina", "Cajón con Elegancia sin Igual",
                "Rizador Impactante Novedoso", "Mirada Cautivadora"):
        assert "RELLENO" in audit(bad), bad


# ─── FAB: las medidas no delatan al proveedor ───────────────────────

@pytest.mark.parametrize("size_model,title", [
    ("25x30cm", "Bolsa de Tela 25x30cm"), ("10 cm", "Cinta de 10 cm de Ancho"), ("500 ml", "Botella de 500 ml con Tapa"),
    ("5V2A", "Cargador Rápido 5V2A para Auto"), ("98*56cm", "Globo 98*56cm Torre"), ("100x130cm / Gris", "Manta 100x130cm Gris"),
    ("CM", "Regla de 30 CM Metálica"), ("30 / 40 / 50 cm", "Set de Cajas 30 40 50 cm"), ("1.5L", "Jarra de 1.5L Transparente"),
    ("16000PA", "Aspiradora 16000PA Potente"),
])
def test_una_medida_en_el_modelo_del_proveedor_no_marca_fab(size_model, title):
    m = r.SupplierMatcher(r.SupplierRefs(size_model=size_model), frozenset(t.upper() for t in LISTS.technical))
    assert r.check_proveedor(title, "", "", m) is None, size_model


def test_el_codigo_real_junto_a_una_medida_sigue_marcando_fab():
    m = r.SupplierMatcher(r.SupplierRefs(size_model="XK-900 / 25x30cm / Negro"), frozenset())
    got = r.check_proveedor("Bolsa XK-900 de Tela 25x30cm", "", "", m)
    assert got == "coincide con código de proveedor: sí (título)"
    assert r.check_proveedor("Bolsa de Tela 25x30cm", "", "", m) is None


def test_la_regex_de_medidas_no_explota_con_digitos_largos():
    t0 = time.monotonic()
    for piece in ("1" * 29 + "a", "1" * 29 + "x", ("1x" * 14) + "z"):
        r._MEASURE_RE.match(piece)
    assert time.monotonic() - t0 < 0.5
