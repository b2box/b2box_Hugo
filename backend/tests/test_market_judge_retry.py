"""El juez del semáforo reintenta un 429 del proveedor (hasta 2 veces) y nada más."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx
import openai
import pytest
from sqlmodel import SQLModel

from app.config import Settings
from app.db.session import engine
from app.pricing import daily_budget, market_judge
from app.pricing.market_judge import JudgeCandidate

CANDS = [JudgeCandidate("MLA1", "Org A", "https://ml/a.jpg")]
GOOD = '{"results":[{"ml_id":"MLA1","verdict":"igual","confidence":0.9,"reason":"igual"}]}'


def _rate_limited(retry_after: str | None = None) -> openai.RateLimitError:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    request = httpx.Request("POST", "https://llm.invalid/v1/chat/completions")
    return openai.RateLimitError("rate limited", response=httpx.Response(429, headers=headers, request=request),
                                 body=None)


class _Http(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def _answer(text: str = GOOD):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
                           usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10))


class _Client:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **request):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    SQLModel.metadata.create_all(engine)  # el cupo diario vive en `settings`
    monkeypatch.setattr(market_judge, "_RETRY_429_DELAYS_S", (0.0, 0.0))
    monkeypatch.setattr(market_judge, "get_settings", lambda: Settings(
        vendure_api_url="https://example.invalid/admin-api", pm_llm_base_url="https://llm.invalid/v1",
        pm_llm_api_key="k", pm_llm_model="qwen3-vl-plus"))


async def _judge(client):
    outcomes: list[tuple[str, str]] = []
    result = await market_judge.judge("Organizador", ["https://cdn/1.jpg"], CANDS, max_calls=100_000,
                                      client=client, on_outcome=lambda k, d: outcomes.append((k, d)))
    return result, outcomes


async def test_a_429_then_ok_gives_the_verdict(caplog):
    client = _Client(_rate_limited(), _answer())
    with caplog.at_level(logging.INFO, logger=market_judge.__name__):
        result, outcomes = await _judge(client)
    assert set(result.verdicts) == {"MLA1"}
    assert client.calls == 2
    assert outcomes == [(market_judge.OUTCOME_OK, "")]
    assert "semáforo: el juez recibió 429, reintento en 0 s" in caplog.messages


async def test_429_every_time_fails_like_before_and_reserves_one_call():
    used = daily_budget.used_today(market_judge.LLM_COUNTER_KEY)
    client = _Client(_rate_limited(), _rate_limited(), _rate_limited())
    result, outcomes = await _judge(client)
    assert result is None
    assert client.calls == 3
    assert outcomes == [(market_judge.OUTCOME_ERROR, "HTTP 429, code=-")]
    # Un 429 no se factura: el reintento no reserva otro cupo.
    assert daily_budget.used_today(market_judge.LLM_COUNTER_KEY) == used + 1


@pytest.mark.parametrize("error", [_Http(500), _Http(401), TimeoutError("30s")])
async def test_other_errors_are_not_retried(error):
    client = _Client(error, _answer())
    result, outcomes = await _judge(client)
    assert result is None
    assert client.calls == 1
    assert outcomes[-1][0] == market_judge.OUTCOME_ERROR


async def test_retry_after_is_used_with_a_cap(monkeypatch):
    waits: list[float] = []

    async def _sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(market_judge, "asyncio", SimpleNamespace(sleep=_sleep))
    client = _Client(_rate_limited("60"), _rate_limited("3"), "ok")
    assert await market_judge.create_with_retry(client, {}, (2.0, 5.0), label="t",
                                                retry_after_max_s=10.0) == "ok"
    assert waits == [10.0, 3.0]


@pytest.mark.parametrize("header,cap,expected", [
    (None, 10.0, 2.0),                                # sin header: la espera por defecto
    ("Wed, 21 Oct 2026 07:28:00 GMT", 10.0, 2.0),     # fecha HTTP: no se interpreta
    ("-1", 10.0, 2.0),
    ("nan", 10.0, 2.0),
    ("0", 10.0, 0.0),
    ("7", None, 2.0),                                 # el estimado no mira Retry-After
])
def test_retry_wait(header, cap, expected):
    assert market_judge._retry_wait(_rate_limited(header), 2.0, cap) == expected
