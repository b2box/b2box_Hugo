"""Cobertura del semáforo sobre el catálogo activo (pricing/coverage.py, GET /api/price-monitor/coverage) y las ventas que
muestra la polycard de ML."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.db.models import MarketPriceSnapshot
from app.pricing import coverage, market_ml_web

NOW = datetime(2026, 11, 8, 12, 0)


def _p(pid: str, enabled: bool = True):
    return SimpleNamespace(id=pid, enabled=enabled)


def test_reds_first_then_active_then_disabled_oldest_first():
    products = [_p("off", False), _p("a"), _p("r_new"), _p("b"), _p("r_old"), _p("never")]
    last = {"a": NOW - timedelta(days=1), "b": NOW - timedelta(days=3), "r_new": NOW, "r_old": NOW - timedelta(days=2),
            "off": NOW - timedelta(days=9)}
    colors = {"r_new": "rojo", "r_old": "rojo", "a": "verde", "b": "amarillo", "off": "rojo"}
    assert [p.id for p in coverage.order_for_coverage(products, last, colors)] == [
        "r_old", "r_new", "never", "b", "a", "off"]


def _snap(pid: str, color: str, *, status: str = "ok", days: float = 1, enabled: bool = True, flag: str | None = None,
          unmeasurable: str | None = None) -> MarketPriceSnapshot:
    return MarketPriceSnapshot(run_id=1, product_id=pid, color=color, ml_status=status,
                               captured_at=NOW - timedelta(days=days), product_enabled=enabled, review_flag=flag,
                               unmeasurable=unmeasurable)


def test_summary_counts_only_active_products_with_a_fresh_measure():
    snaps = [_snap("v", "verde"), _snap("a", "amarillo"), _snap("r", "rojo", flag="firme"), _snap("r2", "rojo"),
             _snap("s", "sin_dato", status="no_data"), _snap("old", "verde", days=10),
             _snap("skip", "sin_dato", status="skipped", unmeasurable="sin_precio"), _snap("off", "rojo", enabled=False)]
    active = {"v", "a", "r", "r2", "s", "old", "skip", "nuevo"}
    out = coverage.summarize(active, snaps, NOW)
    assert out["activos"] == 8 and out["medidos"] == 5 and out["cobertura_pct"] == 62.5
    assert (out["verdes"], out["amarillos"], out["rojos"], out["sin_dato"]) == (1, 1, 2, 1)
    assert out["rojos_firmes"] == 1 and out["no_medibles"] == 1 and out["medicion_vieja"] == 1
    assert out["sin_medir"] == 3 and out["sin_medir_ids"] == ["nuevo", "old", "skip"]
    fallback = coverage.summarize(None, snaps, NOW)
    assert fallback["fuente_activos"] == "snapshots" and "off" not in fallback["sin_medir_ids"]


def test_polycard_sales_from_text_when_tracks_has_none():
    pc = {"components": [{"id": "reviews", "reviews": {"text": "4.8 | +5mil vendidos"}}]}
    assert market_ml_web.sold_from_components(pc) == 5000
    assert market_ml_web.sold_from_components({"components": [{"id": "x", "x": {"text": "+1.234 vendidos"}}]}) == 1234
    assert market_ml_web.sold_from_components({"components": [{"id": "x", "x": {"text": "Envío gratis"}}]}) is None
    assert market_ml_web.sold_from_components({}) is None


def test_coverage_endpoint_needs_a_session_and_answers(monkeypatch):
    from app.vendure import catalog as vendure_catalog
    from tests import review_decide as kit
    from tests import review_world as w

    kit.setup(monkeypatch, mode=0)
    run = w.make_run()
    w.add_snap(run, "c1", color="verde", captured_at=datetime.utcnow())

    async def catalog():
        return [SimpleNamespace(id="c1", enabled=True), SimpleNamespace(id="c2", enabled=True),
                SimpleNamespace(id="c3", enabled=False)]
    monkeypatch.setattr(vendure_catalog, "get_catalog", catalog)
    assert kit.client(None).get("/api/price-monitor/coverage").status_code == 401
    out = kit.client().get("/api/price-monitor/coverage").json()
    assert out["fuente_activos"] == "vendure" and out["activos"] == 2 and out["verdes"] == 1
    assert out["sin_medir_ids"] == ["c2"] and out["rojos_primero"] is False and out["ultima_corrida"]["id"] == run


def test_llm_usage_endpoint_needs_a_session(monkeypatch):
    from tests import review_decide as kit

    kit.setup(monkeypatch, mode=0)
    assert kit.client(None).get("/api/llm-usage").status_code == 401
    out = kit.client().get("/api/llm-usage?days=7").json()
    assert out["days"] == 7 and "by_purpose" in out
