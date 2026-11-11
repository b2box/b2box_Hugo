"""Registro común de tokens y costo de las llamadas a modelos (app/llm_usage.py)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import delete
from sqlmodel import Session, select

from app import llm_usage
from app.clock import utcnow
from app.db.models import LlmUsage
from app.db.session import engine, init_db


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    with Session(engine) as s:
        s.execute(delete(LlmUsage))
        s.commit()
    yield
    with Session(engine) as s:
        s.execute(delete(LlmUsage))
        s.commit()


def test_price_table_picks_the_longest_prefix():
    assert llm_usage.price_for("claude-opus-5-5") == (4.0, 20.0)
    assert llm_usage.price_for("claude-opus-5") == (5.0, 25.0)
    assert llm_usage.price_for("claude-haiku-4-5") == (1.0, 5.0)
    assert llm_usage.price_for("qwen3-vl-plus") is None
    assert llm_usage.estimate_cost("claude-sonnet-5-5", 1_000_000, 100_000) == pytest.approx(3.0)


def test_row_keeps_the_callers_cost_or_estimates_it():
    given = llm_usage.build_row(provider="qwen", model="qwen3-vl-plus", purpose="semaforo_juez",
                                input_tokens=1000, output_tokens=50, cost_usd=0.0003)
    assert given.cost_usd == 0.0003 and given.cost_estimated is False
    est = llm_usage.build_row(provider="anthropic", model="claude-opus-5", purpose="app_rerank",
                              input_tokens=2000, output_tokens=100)
    assert est.cost_usd == pytest.approx((2000 * 5 + 100 * 25) / 1e6) and est.cost_estimated is True
    unknown = llm_usage.build_row(provider="x", model="raro", purpose="p" * 99, input_tokens=-5)
    assert unknown.cost_usd == 0.0 and unknown.input_tokens == 0 and len(unknown.purpose) == llm_usage.PURPOSE_MAX


async def test_record_and_summary():
    await llm_usage.record_async(provider="qwen", model="qwen3-vl-plus", purpose="semaforo_juez", input_tokens=1000,
                                 output_tokens=100, cost_usd=0.001)
    llm_usage.record(provider="anthropic", model="claude-opus-5", purpose="app_rerank", input_tokens=1000,
                     output_tokens=0)
    llm_usage.record(provider="x", model="sin-precio", purpose="otro", input_tokens=10, output_tokens=10)
    with Session(engine) as s:
        assert len(s.exec(select(LlmUsage)).all()) == 3
    out = llm_usage.summary(days=7)
    assert out["total"]["calls"] == 3 and out["total"]["cost_usd"] == pytest.approx(0.006)
    assert out["by_purpose"]["semaforo_juez"]["input_tokens"] == 1000
    assert "anthropic/claude-opus-5" in out["by_model"] and out["sin_precio"] == 1
    assert list(out["by_day"]) == [utcnow().strftime("%Y-%m-%d")]
    assert llm_usage.summary(days=1, now=utcnow() + timedelta(days=3))["total"]["calls"] == 0


def test_record_never_raises(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("base caída")
    monkeypatch.setattr(llm_usage, "Session", boom)
    llm_usage.record(provider="x", model="y", purpose="z")      # no lanza


def test_provider_names():
    assert llm_usage.provider_of_base_url("api.xiaomimimo.com") == "mimo"
    assert llm_usage.provider_of_base_url("dashscope-intl.aliyuncs.com") == "qwen"
    assert llm_usage.provider_of_base_url("") == "desconocido"


async def test_the_semaforo_judge_records_its_tokens(monkeypatch):
    from types import SimpleNamespace

    from app.pricing import market_judge

    class FakeCompletions:
        async def create(self, **_kw):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="[]"))],
                                   usage=SimpleNamespace(prompt_tokens=1234, completion_tokens=56))

    class FakeClient:
        chat = SimpleNamespace(completions=FakeCompletions())

        async def close(self):
            return None

    monkeypatch.setattr(market_judge, "enabled", lambda: True)
    monkeypatch.setattr(market_judge, "image_mode", lambda: market_judge.IMAGE_MODE_URL)

    async def free_cupo(*_a, **_k):
        return 1
    monkeypatch.setattr(market_judge.daily_budget, "reserve_async", free_cupo)
    cand = market_judge.JudgeCandidate(ml_id="MLA1", title="x", image_url="https://http2.mlstatic.com/D_1.jpg")
    await market_judge.judge("nuestro", ["https://cdn.b2box/x.jpg"], [cand], max_calls=5, client=FakeClient())
    with Session(engine) as s:
        [row] = s.exec(select(LlmUsage)).all()
    assert row.purpose == "semaforo_juez" and row.input_tokens == 1234 and row.output_tokens == 56


def test_apify_spend_suma_por_etiqueta_en_la_ventana():
    from sqlalchemy import delete
    from sqlmodel import Session

    from app.db.models import ApifySpend
    from app.db.session import engine

    labels = ("t_app_meli_link", "t_radar")
    with Session(engine) as s:
        s.execute(delete(ApifySpend).where(ApifySpend.label.in_(labels)))  # type: ignore[attr-defined]
        s.add(ApifySpend(label="t_app_meli_link", terms=1, usd=0.005, estimated=True))
        s.add(ApifySpend(label="t_app_meli_link", terms=1, usd=0.005, estimated=True))
        s.add(ApifySpend(label="t_radar", run_id="r1", terms=4, usd=0.01, estimated=False))
        s.add(ApifySpend(label="t_radar", terms=1, usd=9.0, created_at=utcnow() - timedelta(days=40)))
        s.commit()
    try:
        out = llm_usage.apify_spend(days=7)
        assert out["by_label"]["t_app_meli_link"] == {"runs": 2, "usd": 0.01, "estimated_runs": 2}
        assert out["by_label"]["t_radar"] == {"runs": 1, "usd": 0.01, "estimated_runs": 0}
        assert out["total_usd"] >= 0.02
    finally:
        with Session(engine) as s:
            s.execute(delete(ApifySpend).where(ApifySpend.label.in_(labels)))  # type: ignore[attr-defined]
            s.commit()
