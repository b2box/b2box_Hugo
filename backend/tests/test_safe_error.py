"""Los errores que la bandeja guarda y muestra llevan el tipo y un código, nunca el mensaje (que puede traer montos,
URLs o pedazos de la respuesta de Vendure); el detalle va al log."""

from __future__ import annotations

import logging
import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from gql.transport.exceptions import TransportQueryError, TransportServerError  # noqa: E402

from app.pricing.price_breakdown import BreakdownError  # noqa: E402
from app.pricing.safe_error import error_code  # noqa: E402

SECRET = "USD 4321.99 en https://admin.interno/x?token=abc"


def test_the_code_is_the_type_plus_a_short_code_never_the_message():
    assert error_code(RuntimeError(SECRET)) == "RuntimeError"
    assert error_code(BreakdownError(f"las 10 partes suman {SECRET}")) == "BreakdownError"
    assert error_code(TransportServerError(SECRET, code=502)) == "TransportServerError 502"
    gql_error = TransportQueryError(SECRET, errors=[{"message": SECRET, "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}}])
    assert error_code(gql_error) == "TransportQueryError GRAPHQL_VALIDATION_FAILED"
    for exc in (RuntimeError(SECRET), BreakdownError(SECRET), TransportServerError(SECRET, code=500), gql_error):
        assert "4321" not in error_code(exc) and "token" not in error_code(exc)


@pytest.mark.parametrize("bad", ["a b", "x" * 80, "<script>", "", True, None, 1.5])
def test_a_weird_code_is_dropped(bad):
    exc = RuntimeError("x")
    exc.code = bad            # type: ignore[attr-defined]
    assert error_code(exc) == "RuntimeError"


async def test_a_failing_diagnosis_stores_only_the_type_and_logs_the_detail(caplog):
    from app.db.models import MarketPriceSnapshot
    from app.pricing import diagnosis_source as ds
    from tests.review_fixtures import FakeReviewVendure  # noqa: F401

    class Boom:
        async def get_tier_breakdowns(self, v):
            raise BreakdownError(f"las 10 partes suman {SECRET}")

        async def get_priced_variant(self, v):
            raise TransportServerError(SECRET, code=503)

        async def get_certification(self, p, c):
            raise RuntimeError(SECRET)

        async def get_channel_pricing(self):
            return None

    snap = MarketPriceSnapshot(run_id=1, product_id="9", variant_id="v9", ml_status="ok", color="rojo", our_price_cents=1_000_000,
                               ml_median_cents=900_000, commission_pct=13.0, shipping_cents=0, tier_used="tier:min_qty=1")
    from app.vendure.client import ChannelPricing

    with caplog.at_level(logging.WARNING, logger="app.pricing.diagnosis_source"):
        out = await ds.diagnose_product(snap, Boom(), yellow_min_pct=10.0, channel=ChannelPricing("2", 500, 20.0, 30.0))
    assert out.errors == ["tramos de la variante: TransportServerError 503", "desglose de precio (no cierra): BreakdownError",
                          "certificado: RuntimeError"]
    assert "4321" not in " ".join(out.errors)
    assert "4321.99" in caplog.text                          # el detalle sí está en el log del servidor
