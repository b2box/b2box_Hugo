"""Tests del módulo de pricing.diff (comparación entre snapshots fuente)."""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")
os.environ.setdefault("VENDURE_BEARER", "test-token")

from app.pricing.diff import compare_source_snapshots  # noqa: E402


def test_first_observation_returns_first_observation():
    d = compare_source_snapshots(1680, "CNY", None, None)
    assert d.action == "first_observation"
    assert d.previous_price_cents is None
    assert d.drift_pct == 0.0


def test_no_drift_returns_ok():
    d = compare_source_snapshots(1680, "CNY", 1680, "CNY")
    assert d.action == "ok"
    assert d.drift_pct == 0.0


def test_small_drift_returns_ok():
    # ~3% (por debajo del 5% default)
    d = compare_source_snapshots(1730, "CNY", 1680, "CNY")
    assert d.action == "ok"


def test_moderate_drift_alerts():
    # +10% (entre 5% y 30%)
    d = compare_source_snapshots(1850, "CNY", 1680, "CNY")
    assert d.action == "alert"
    assert d.drift_pct > 0


def test_extreme_drift_alerts_critical():
    # +60% (por encima de 30%) → revisar si sigue siendo el mismo SKU
    d = compare_source_snapshots(2700, "CNY", 1680, "CNY")
    assert d.action == "alert_critical"


def test_drift_can_be_negative():
    # Proveedor BAJÓ el precio 10%
    d = compare_source_snapshots(1500, "CNY", 1680, "CNY")
    assert d.action == "alert"
    assert d.drift_pct < 0


def test_currency_mismatch_skips():
    # Hoy CNY, ayer USD: no podemos comparar
    d = compare_source_snapshots(1680, "CNY", 246, "USD")
    assert d.action == "skip_currency"
