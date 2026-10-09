"""QA independiente de HG1, criterio FAB: los valores de supplierBusiness / supplierSizeModel / supplierLink
no se persisten, no se loguean, no salen por la API ni por el CSV y el dashboard no los conoce.

Los fragmentos de estos tests son propios (distintos de los de test_seo_text_audit.py) y los productos "mudos"
no los traen en ningún texto público: si aparecen en cualquier superficie, se filtraron.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db.session import engine  # noqa: E402
from tests.seo_fixtures import FakeVendure, raw_product  # noqa: E402
from tests.test_seo_text_audit import client, env, items, run  # noqa: E402,F401

SUP = {
    "business": "Wenzhou Qiaoxin Metalware Co., Ltd.",
    "size_model": "QX-9012 / Plata / 15cm",
    "link": "https://qiaoxin-metal.1688.com/offer/771122334455.html?spm=zz991",
}
FRAGMENTS = ("wenzhou", "qiaoxin", "metalware", "qx-9012", "qx9012", "771122334455", "qiaoxin-metal", "spm=zz991")


def _catalog():
    quiet = raw_product(
        21, business=SUP["business"], size_model=SUP["size_model"], link=SUP["link"],
        translations=[("es_AR", "Pinza de Acero Inoxidable para Cocina", "pinza-de-acero-para-cocina",
                       "Pinza de acero inoxidable ideal para la cocina de todos los días.")],
    )
    # El proveedor aparece SOLO en la descripción (que no se guarda): la regla FAB tiene que saltar sin copiar el valor.
    loud = raw_product(
        22, business=SUP["business"], size_model=SUP["size_model"], link=SUP["link"],
        translations=[("es_AR", "Cuchara de Acero Inoxidable", "cuchara-de-acero",
                       f"Cuchara de acero. Fabricada por {SUP['business']}, modelo QX-9012, ref 771122334455.")],
    )
    other = raw_product(23)
    return {"ar": [quiet, loud, other], None: [quiet, loud, other]}


def _sqlite_bytes() -> bytes:
    path = engine.url.database
    with engine.begin() as c:
        c.execute(text("PRAGMA wal_checkpoint(FULL)"))
    blob = Path(path).read_bytes()
    for suffix in ("-wal", "-journal"):
        extra = Path(path + suffix)
        if extra.exists():
            blob += extra.read_bytes()
    return blob


def _surfaces(client, run_id: int) -> dict[str, str]:
    out = {
        "summary": client.get("/api/seo/text-audit/summary").text,
        "items": client.get(f"/api/seo/text-audit/items?run_id={run_id}&only_issues=false&page_size=200").text,
        "items_fab": client.get(f"/api/seo/text-audit/items?run_id={run_id}&rule=FAB").text,
        "csv": client.get(f"/api/seo/text-audit/export.csv?run_id={run_id}&only_issues=false").content.decode("utf-8-sig"),
        "csv_fab": client.get(f"/api/seo/text-audit/export.csv?run_id={run_id}&rule=FAB").content.decode("utf-8-sig"),
        "lists": client.get("/api/seo/text-audit/lists").text,
    }
    for q in ("qiaoxin", "wenzhou", "771122334455"):          # buscar por el valor secreto no puede "adivinarlo"
        out[f"search_{q}"] = client.get(f"/api/seo/text-audit/items?run_id={run_id}&only_issues=false&q={q}").text
    return out


def test_ninguna_superficie_trae_los_valores_del_proveedor_pero_FAB_si_salta(monkeypatch, client, caplog):
    with caplog.at_level(logging.INFO):
        result = run(monkeypatch, FakeVendure(_catalog()))
    assert result["status"] == "ok"
    rid = result["id"]

    fab = [i for i in items(client, run_id=rid, rule="FAB")["items"]]
    assert [i["product_id"] for i in fab] == ["22"], "FAB tiene que saltar en el producto que nombra al proveedor en la descripción"
    detail = {x["rule"]: x["detail"] for x in fab[0]["issues"]}["FAB"]
    assert "descripción" in detail and "sí" in detail

    for name, body in _surfaces(client, rid).items():
        low = body.casefold()
        for frag in FRAGMENTS:
            assert frag not in low, f"«{frag}» salió por {name}"
    # El campo de búsqueda no es un oráculo: buscar por el nombre de la fábrica no encuentra el producto.
    for q in ("qiaoxin", "wenzhou", "771122334455"):
        assert items(client, run_id=rid, only_issues="false", q=q)["items"] == []

    logs = "\n".join(r.getMessage() for r in caplog.records).casefold()
    for frag in FRAGMENTS:
        assert frag not in logs, f"«{frag}» salió por el log (INFO)"

    blob = _sqlite_bytes().lower()
    assert b"pinza de acero inoxidable para cocina" in blob, "sanidad: el escaneo tiene que estar leyendo la base real"
    for frag in FRAGMENTS:
        assert frag.encode() not in blob, f"«{frag}» quedó guardado en la base"


def test_la_base_no_guarda_la_descripcion_ni_columnas_de_proveedor(monkeypatch):
    run(monkeypatch, FakeVendure(_catalog()))
    with engine.connect() as c:
        for table in ("text_audit_item", "text_audit_run"):
            cols = [r[1] for r in c.execute(text(f"PRAGMA table_info({table})"))]
            assert not [x for x in cols if re.search(r"supplier|provee|fabric|descr(?!_chars)|html", x, re.I)], (table, cols)


def test_con_log_debug_tampoco_se_loguean_los_valores_del_proveedor(monkeypatch, caplog):
    with caplog.at_level(logging.DEBUG):
        run(monkeypatch, FakeVendure(_catalog()))
    logs = "\n".join(r.getMessage() for r in caplog.records).casefold()
    assert not [f for f in FRAGMENTS if f in logs]


def test_el_front_no_conoce_los_campos_del_proveedor():
    src = Path(__file__).resolve().parents[2] / "frontend" / "src"
    offenders = [str(p.relative_to(src)) for p in src.rglob("*") if p.suffix in {".ts", ".tsx"}
                 and re.search(r"supplier(Business|SizeModel|Link)", p.read_text(encoding="utf-8"))]
    assert offenders == []
    types_ts = (src / "types.ts").read_text(encoding="utf-8")
    assert "supplier" not in types_ts.casefold()


def test_la_query_pide_los_campos_pero_ninguna_ruta_de_la_api_los_devuelve():
    import inspect

    from app.api import seo_routes

    src = inspect.getsource(seo_routes)
    assert not re.search(r"supplier", src.replace("supplierBusiness / supplierSizeModel / supplierLink", ""), re.I)
