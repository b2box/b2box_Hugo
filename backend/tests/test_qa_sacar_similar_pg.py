"""«No tiene nada que ver» sobre un Postgres descartable: el motivo y la nota por la API, las lecciones (orden por fecha),
deshacer y «Es el mismo», con el SQL de Postgres. Se salta sola sin HUGO_TEST_PG_URL:

    HUGO_TEST_PG_URL=postgresql+psycopg://postgres:pw@localhost:55541/hugo_test \\
        VENDURE_API_URL=https://example.invalid/admin-api pytest backend/tests/test_qa_sacar_similar_pg.py
"""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.db.models import MarketMatchFeedback, MarketPriceSnapshot, PriceMonitorRun  # noqa: E402
from app.pricing import match_feedback, price_monitor  # noqa: E402
from app.pricing.market_judge import JudgeLesson  # noqa: E402
from tests.test_qa_siempre_trae_algo_pg import PG_URL, api, pg  # noqa: E402,F401
from tests.test_semaforo_sacar_similar import _sim  # noqa: E402

pytestmark = pytest.mark.skipif(not PG_URL, reason="falta HUGO_TEST_PG_URL (Postgres descartable)")


def _seed(pg, *similars) -> int:
    with Session(pg) as s:
        run = PriceMonitorRun(status="ok", total_products=1)
        s.add(run)
        s.commit()
        s.refresh(run)
        snap = MarketPriceSnapshot(run_id=run.id, product_id="7", product_name="Soporte celular",
                                   our_price_cents=10_000, commission_pct=13.0, shipping_cents=0,
                                   ml_status="no_data", color="sin_dato")
        price_monitor._store_listings(snap, [], [], list(similars), [], keep=8, green_min=30.0, yellow_min=10.0)
        s.add(snap)
        price_monitor.recount_run(s, run.id)
        s.commit()
        s.refresh(snap)
        return snap.id


def test_drop_with_motive_lessons_undo_and_same_on_postgres(api, pg):
    with pg.connect() as c:
        types = dict(c.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'market_match_feedback' AND column_name IN ('motive', 'note')")).all())
    assert types == {"motive": "character varying", "note": "character varying"}

    sid = _seed(pg, _sim("MLA111", 12_000), _sim("MLA112", 40_000), _sim("MLA113", 50_000))

    def drop(ml_id, **b):
        return api.post(f"/api/price-monitor/snapshots/{sid}/not-same", json={"ml_id": ml_id, **b})

    assert drop("MLA111", motive="otro").status_code == 400
    r = drop("MLA111", motive="marca", note="es\nStanley ñandú")
    assert r.status_code == 200, r.text
    assert drop("MLA112", motive="cantidad").status_code == 200
    assert drop("MLA112", motive="cantidad").json()["already"] is True                 # idempotente
    snap = drop("MLA113", motive="otro", note="x" * 200).json()["snapshot"]
    assert snap["similar_count"] == 0 and snap["estimated_color"] is None
    with Session(pg) as s:
        assert len(s.exec(select(MarketMatchFeedback)).all()) == 3
        run = s.get(PriceMonitorRun, snap["run_id"])
        assert (run.n_con_similares, run.n_est_verde, run.n_solo_diferentes) == (0, 0, 1)
    # la más nueva primero
    assert [x.ml_id for x in match_feedback.load_lessons()["7"]] == ["MLA113", "MLA112", "MLA111"]
    assert match_feedback.load_lessons()["7"][2] == JudgeLesson("MLA111", "Publicación MLA111", "Otra marca",
                                                                "es Stanley ñandú")

    # «Es el mismo» da vuelta la marca y deja de ser lección; deshacer la vuelve
    r = api.post(f"/api/price-monitor/snapshots/{sid}/same", json={"ml_id": "MLA112"})
    assert r.status_code == 200, r.text
    assert r.json()["snapshot"]["color"] == "verde"
    assert [x.ml_id for x in match_feedback.load_lessons()["7"]] == ["MLA113", "MLA111"]
    assert api.delete("/api/price-monitor/products/7/feedback/MLA112").json() == {"removed": False, "restored": 0}
    assert [x.ml_id for x in match_feedback.load_lessons()["7"]] == ["MLA113", "MLA112", "MLA111"]
    # deshacer una sin cambio de opinión la borra
    assert api.delete("/api/price-monitor/products/7/feedback/MLA111").json() == {"removed": True}
    assert [x.ml_id for x in match_feedback.load_lessons()["7"]] == ["MLA113", "MLA112"]
