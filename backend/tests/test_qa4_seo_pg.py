"""QA independiente de HG1 sobre Postgres 16 (se salta sin HUGO_TEST_PG_URL; ver test_seo_text_audit_pg.py).

Lo que SQLite no muestra: el manejo de caracteres que Postgres rechaza (NUL) en los filtros, y la corrida de un
catálogo del tamaño real (1.077 productos, dos canales, tres idiomas) contra el límite de 5 minutos del criterio.
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import auth  # noqa: E402
from app import main as main_mod  # noqa: E402
from app.db import session  # noqa: E402
from app.db.models import TextAuditItem  # noqa: E402
from tests.seo_fixtures import FakeVendure, raw_product  # noqa: E402
from tests.test_qa_tiendas_pg import PG_URL  # noqa: E402
from tests.test_seo_text_audit import env, run  # noqa: E402,F401
from tests.test_seo_text_audit_pg import old_schema  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not PG_URL, reason="falta HUGO_TEST_PG_URL (Postgres descartable)")
BASE = "/api/seo/text-audit"


def _client() -> TestClient:
    c = TestClient(main_mod.app, raise_server_exceptions=False)
    c.cookies.set(auth.COOKIE_NAME, auth.issue_session_token("admin"))
    return c


def test_un_nul_en_la_busqueda_no_rompe_la_api_en_postgres(monkeypatch, old_schema):  # noqa: F811
    session.init_db()
    result = run(monkeypatch, FakeVendure({"ar": [raw_product(1)], None: [raw_product(1)]}))
    c = _client()
    for url in (f"{BASE}/items?run_id={result['id']}&q=%00", f"{BASE}/export.csv?run_id={result['id']}&q=a%00b"):
        assert c.get(url).status_code in (200, 422), url


def test_un_catalogo_del_tamano_real_termina_muy_por_debajo_de_los_5_minutos_en_postgres(monkeypatch, old_schema):  # noqa: F811
    session.init_db()
    titles = ["Organizador Plegable de Cocina", "Funda iPhone Transparente", "Lámpara LED Súper Mágica", "Taza  Térmica 350ml",
              "Mopa Giratoria con Balde Centrifugado", "Auto de Carrera Drift C64 Escala 176"]
    prods = [
        raw_product(i, business=f"Fábrica Número {i} Co., Ltd.", size_model=f"MOD-{i} / Negro",
                    link=f"https://detail.1688.com/offer/{600000000000 + i}.html",
                    translations=[("es_AR", f"{titles[i % 6]} {i}", f"producto-{i}", f"<p>Descripción número {i} 🙂 " + "texto " * 120 + "</p>"),
                                  ("es", f"{titles[i % 6]} {i}", f"producto-{i}", "Descripción en inglés " * 20),
                                  ("en", f"Product {i}", f"product-{i}", "")])
        for i in range(1, 1078)
    ]
    ar = prods[:1000]
    t0 = time.monotonic()
    result = run(monkeypatch, FakeVendure({"ar": ar, None: prods}))
    elapsed = time.monotonic() - t0
    assert result["status"] == "ok" and result["products_total"] == 1077 and result["rows_total"] == 3 * 1077
    assert elapsed < 60, f"{elapsed:.1f} s para 1.077 productos"
    with Session(old_schema) as s:
        assert len(s.exec(select(TextAuditItem)).all()) == 3 * 1077
