"""Hallazgos de la revisión de seguridad del estimado: reembolso de cupo solo si
no se gastó nada (M1), registro aunque el lookup explote (B1), HMAC (B2) y
techo de la respuesta con want_estimate (extracción y envío a Cloud)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import app_routes
from app.api.app_routes import AppLookupRequest, AppLookupVisitor, _lookup
from app.estimate import identity, limits, otapi_item
from app.estimate import service as est_service
from tests.test_estimate_lookup import (  # noqa: F401  (env es fixture)
    ML_URL,
    OFFER_URL,
    VISITOR_IP,
    _ask,
    _request,
    _rows,
    env,
)


def _visitor_used():
    return limits.used_today(limits.visitor_scope(_rows()[-1].ip_hash))


async def test_m1_link_de_1688_leido_y_encontrado_no_devuelve_cupo(env, monkeypatch):
    env.otapi = otapi_item.Offer1688(offer_id="666233922092", title="杯", price_cny=30.0,
                                     ladder=[(1, 30.0)], pictures=[
                                         "https://cbu01.alicdn.com/img/main.jpg"])
    product = SimpleNamespace(id="42", name="Vaso", slug="vaso", description="", enabled=True,
                              source_url=OFFER_URL, image_urls=[], product_code="BX-1",
                              featured_image_url=None, first_variant_price_cents=1000)

    async def catalog(force=False):  # noqa: ARG001
        return [product]

    class FullClient:
        async def get_product_full(self, pid):  # noqa: ARG002
            return None
    monkeypatch.setattr(app_routes.vendure_catalog, "get_catalog", catalog)
    monkeypatch.setattr(app_routes, "url_similarity", lambda a, b: 1.0)
    monkeypatch.setattr(app_routes, "VendureClient", FullClient)
    monkeypatch.setattr(app_routes, "get_settings",
                        lambda: SimpleNamespace(storefront_url="", storefront_product_path=""))
    resp = await _ask(OFFER_URL)
    assert resp.status == "found" and resp.estimate_status == "skipped_found"
    # OTAPI se pagó: el cupo del visitante NO vuelve (sin gasto sí vuelve:
    # test_er06_si_lo_tenemos_no_hay_estimado).
    assert _visitor_used() == 1


async def test_b1_excepcion_despues_de_abrir_el_estimado_deja_fila_y_devuelve_cupo(env,
                                                                                 monkeypatch):
    async def boom(force=False):  # noqa: ARG001
        raise RuntimeError("vendure caído")
    monkeypatch.setattr(app_routes.vendure_catalog, "get_catalog", boom)
    with pytest.raises(HTTPException) as exc:
        await _lookup(AppLookupRequest(url=ML_URL, want_estimate=True,
                                       visitor=AppLookupVisitor(ip=VISITOR_IP)), _request())
    assert exc.value.status_code == 502
    await est_service.wait_background()
    rows = _rows()
    assert len(rows) == 1 and rows[0].estimate_status == "error"
    assert "excepción" in rows[0].reason
    assert _visitor_used() == 0


def test_b2_hash_es_hmac_con_la_sal(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "estimate_hash_salt", "s" * 40, raising=False)
    expected = hmac.new(b"s" * 40, b"ip:190.1.2.3", hashlib.sha256).hexdigest()[:32]
    assert identity.ip_hash("190.1.2.3") == expected


async def test_extraccion_lenta_con_estimado_tiene_techo(env, monkeypatch):
    monkeypatch.setattr(app_routes, "_ESTIMATE_EXTRACT_BUDGET_SECONDS", 0.2)

    async def slow(url):  # noqa: ARG001
        await asyncio.sleep(30)
    monkeypatch.setattr(app_routes.image_from_url, "extract", slow)
    t = time.monotonic()
    resp = await _ask()
    assert time.monotonic() - t < 1.0
    assert resp.status == "no_image" and resp.estimate_status == "error"


async def test_extraccion_sin_estimado_no_tiene_techo_nuevo(env, monkeypatch):
    monkeypatch.setattr(app_routes, "_ESTIMATE_EXTRACT_BUDGET_SECONDS", 0.01)
    resp = await app_routes.app_lookup(AppLookupRequest(url=ML_URL))
    assert resp.status == "not_found"


async def test_envio_a_cloud_con_estimado_tiene_techo(env, monkeypatch):
    monkeypatch.setattr(app_routes, "_ESTIMATE_RESPONSE_CAP_SECONDS", 0.0)
    monkeypatch.setattr(app_routes, "_ESTIMATE_CLOUD_MIN_SECONDS", 0.2)
    monkeypatch.setattr(app_routes.cloud_integration, "enabled", lambda: True)
    monkeypatch.setattr(app_routes.cloud_integration, "missing_client_fields", lambda c: [])

    async def hang(payload):  # noqa: ARG001
        await asyncio.sleep(30)
    monkeypatch.setattr(app_routes.cloud_integration, "submit_request", hang)
    t = time.monotonic()
    resp = await _ask()
    assert time.monotonic() - t < 1.5
    assert resp.estimate_status == "ok" and resp.action == "show_estimate"
    assert resp.cloud_request.sent is False and "no contestó" in resp.cloud_request.error


# ─── Carrera abandon / skip_found contra la búsqueda en segundo plano ──


async def _open_and_start(monkeypatch, photo_gate: asyncio.Event):
    from app.estimate import images

    async def slow_fetch(url, *, interactive=False):  # noqa: ARG001
        await photo_gate.wait()
        return b"bytes-of:" + url.encode()
    monkeypatch.setattr(images, "_fetch", slow_fetch)
    est = await est_service.open_session(source="test", api_client="storefront",
                                         declared_ip=VISITOR_IP)
    est.start(["https://http2.mlstatic.com/D_NQ_NP_x.jpg"])
    await asyncio.sleep(0.01)          # _prepare quedó bajando la foto
    assert est.searching
    return est


@pytest.mark.parametrize("how", ["abandon", "skip_found"])
async def test_cortar_mientras_baja_la_foto_no_lanza_apify_y_devuelve_cupo(env, monkeypatch,
                                                                         how):
    gate = asyncio.Event()
    est = await _open_and_start(monkeypatch, gate)
    if how == "abandon":
        waiter = asyncio.create_task(est.abandon("corte"))
    else:
        await est.skip_found()
    await asyncio.sleep(0.01)
    gate.set()                          # la foto llega DESPUÉS del corte
    if how == "abandon":
        await waiter
    await est_service.wait_background()
    assert env.searches == [] and not est.apify_launched
    assert limits.used_today(limits.visitor_scope(est.ip_hash)) == 0
    assert _rows()[-1].estimate_status in ("error", "skipped_found")


async def test_cortar_con_apify_ya_lanzado_no_devuelve_cupo(env, monkeypatch):
    from app.ingest import apify_1688

    launched = asyncio.Event()

    async def slow_search(*a, **k):
        launched.set()
        await asyncio.sleep(0.2)
        return apify_1688.Search1688Result(status="no_candidates", run_ids=["R"],
                                           usd_imputado=0.02)
    monkeypatch.setattr(apify_1688, "search_by_image", slow_search)
    gate = asyncio.Event()
    est = await _open_and_start(monkeypatch, gate)
    gate.set()
    await launched.wait()
    await est.abandon("corte")
    assert est.apify_launched
    assert limits.used_today(limits.visitor_scope(est.ip_hash)) == 1


async def test_lookup_que_abandona_con_busqueda_en_curso_no_espera(env, monkeypatch):
    gate = asyncio.Event()
    est = await _open_and_start(monkeypatch, gate)
    base = app_routes.AppLookupResponse(status="indexing")
    t = time.monotonic()
    await app_routes._abandon_estimate(est, base, "índice armándose")
    assert time.monotonic() - t < 0.1 and base.estimate_status == "error"
    gate.set()
    await est_service.wait_background()
    assert env.searches == []
    assert limits.used_today(limits.visitor_scope(est.ip_hash)) == 0
