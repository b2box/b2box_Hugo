"""Límite de TODO /app/lookup por visitante del storefront y cupo diario del
rerank de visión (Sonnet). Los dos fail-closed. El app sin `visitor` no cambia."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, delete, select

from app.api.app_routes import AppLookupRequest, AppLookupVisitor, _lookup, app_lookup
from app.db.models import EstimateUsage, Setting
from app.db.session import engine
from app.dedup import vision_rerank
from app.estimate import limits
from tests.test_estimate_lookup import (  # noqa: F401  (env es fixture)
    ML_URL,
    RUNTIME,
    VISITOR_IP,
    _request,
    env,
)


def _payload(ip=VISITOR_IP):
    return AppLookupRequest(url=ML_URL, visitor=AppLookupVisitor(ip=ip))


async def test_storefront_con_visitor_tiene_rafaga_por_minuto(env, monkeypatch):
    monkeypatch.setitem(RUNTIME, "lookup_visitor_per_minute", 3)
    for _ in range(3):
        await _lookup(_payload(), _request())
    with pytest.raises(HTTPException) as exc:
        await _lookup(_payload(), _request())
    assert exc.value.status_code == 429 and exc.value.headers["Retry-After"] == "60"
    # Otro visitante no se ve afectado.
    await _lookup(_payload("190.1.2.3"), _request())


async def test_storefront_con_visitor_tope_diario(env, monkeypatch):
    monkeypatch.setitem(RUNTIME, "lookup_visitor_daily_limit", 2)
    for _ in range(2):
        await _lookup(_payload(), _request())
    with pytest.raises(HTTPException) as exc:
        await _lookup(_payload(), _request())
    assert exc.value.status_code == 429 and int(exc.value.headers["Retry-After"]) > 60


async def test_ipv6_del_mismo_64_cuenta_como_un_visitante(env, monkeypatch):
    monkeypatch.setitem(RUNTIME, "lookup_visitor_daily_limit", 1)
    await _lookup(_payload("2800:810:1:2::5"), _request())
    with pytest.raises(HTTPException):
        await _lookup(_payload("2800:810:1:2:ffff::9"), _request())


async def test_limite_en_cero_o_base_caida_es_429(env, monkeypatch):
    monkeypatch.setitem(RUNTIME, "lookup_visitor_daily_limit", 0)
    with pytest.raises(HTTPException) as exc:
        await _lookup(_payload(), _request())
    assert exc.value.status_code == 429
    monkeypatch.setitem(RUNTIME, "lookup_visitor_daily_limit", 30)

    def broken(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(limits, "Session", broken)
    with pytest.raises(HTTPException) as exc:
        await _lookup(_payload("190.7.7.7"), _request())
    assert exc.value.status_code == 429


async def test_el_app_sin_visitor_no_tiene_limite_y_no_cambia(env, monkeypatch):
    monkeypatch.setitem(RUNTIME, "lookup_visitor_daily_limit", 0)
    resp = await app_lookup(AppLookupRequest(url=ML_URL))
    data = json.loads(resp.model_dump_json())
    assert resp.status == "not_found" and "estimate" not in data
    # Un cliente que no es de confianza tampoco pasa por el límite (ni con visitor).
    await _lookup(_payload(), _request(client="b2box-app"))
    with Session(engine) as s:
        assert not [u for u in s.exec(select(EstimateUsage)).all() if u.scope.startswith("lk:")]


# ─── Cupo del rerank de visión ─────────────────────────────────────


@pytest.fixture
def rerank(monkeypatch):
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.execute(delete(Setting).where(Setting.key == vision_rerank.RERANK_COUNTER_KEY))
        s.commit()
    vision_rerank._CACHE.clear()
    calls = []

    async def prepare(queries, candidates, title):
        return SimpleNamespace(candidates=candidates)

    async def ask(opts, payload):
        calls.append(1)
        return vision_rerank.Verdict(None, 0.9, "ninguno", opts.provider)
    monkeypatch.setattr(vision_rerank, "available", lambda provider=None: True)
    monkeypatch.setattr(vision_rerank, "_shortlist", lambda c, k: list(c))
    monkeypatch.setattr(vision_rerank, "_prepare", prepare)
    monkeypatch.setattr(vision_rerank, "_ask", ask)
    yield calls
    vision_rerank._CACHE.clear()


def _cands(i):
    return [SimpleNamespace(id=f"p{i}")]


async def test_rerank_con_cupo_agotado_no_llama(rerank, monkeypatch):
    from app import runtime

    budget = {"vision_rerank_daily_budget": 2}
    monkeypatch.setattr(runtime, "get", lambda k: budget[k])
    assert await vision_rerank.pick_match(["https://a/1.jpg"], _cands(1)) is not None
    assert await vision_rerank.pick_match(["https://a/2.jpg"], _cands(2)) is not None
    assert await vision_rerank.pick_match(["https://a/3.jpg"], _cands(3)) is None
    assert len(rerank) == 2
    budget["vision_rerank_daily_budget"] = 0
    vision_rerank._CACHE.clear()
    assert await vision_rerank.pick_match(["https://a/1.jpg"], _cands(1)) is None
    assert len(rerank) == 2


async def test_rerank_con_base_caida_no_llama(rerank, monkeypatch):
    from app import runtime
    from app.pricing import daily_budget

    monkeypatch.setattr(runtime, "get", lambda k: 300)

    async def down(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(daily_budget, "reserve_async", down)
    assert await vision_rerank.pick_match(["https://a/1.jpg"], _cands(1)) is None
    assert rerank == []


async def test_storefront_sin_visitor_usa_el_bucket_compartido_con_topes_propios(env,
                                                                              monkeypatch):
    # El tope por visitante no aplica al bucket compartido del socket.
    monkeypatch.setitem(RUNTIME, "lookup_visitor_daily_limit", 1)
    monkeypatch.setitem(RUNTIME, "lookup_shared_daily_limit", 2)
    await _lookup(AppLookupRequest(url=ML_URL), _request())                 # sin visitor
    await _lookup(_payload("10.0.0.7"), _request())                          # privada
    with pytest.raises(HTTPException) as exc:
        await _lookup(AppLookupRequest(url=ML_URL), _request())
    assert exc.value.status_code == 429
    # Un visitante con IP real tiene su propio cupo (el de por visitante).
    await _lookup(_payload("190.4.4.4"), _request())
    with pytest.raises(HTTPException):
        await _lookup(_payload("190.4.4.4"), _request())


async def test_bucket_compartido_rafaga_propia(env, monkeypatch):
    monkeypatch.setitem(RUNTIME, "lookup_visitor_per_minute", 1)
    monkeypatch.setitem(RUNTIME, "lookup_shared_per_minute", 3)
    for _ in range(3):
        await _lookup(AppLookupRequest(url=ML_URL), _request())
    with pytest.raises(HTTPException) as exc:
        await _lookup(AppLookupRequest(url=ML_URL), _request())
    assert exc.value.headers["Retry-After"] == "60"


async def test_storefront_sin_ip_de_visitante_valida_no_estima(env):
    from tests.test_estimate_lookup import _ask

    for ip in (None, "10.0.0.8", "no-es-ip"):
        resp = await _ask(ip=ip)
        assert resp.estimate_status == "rate_limited" and resp.estimate is None
    assert env.searches == []
