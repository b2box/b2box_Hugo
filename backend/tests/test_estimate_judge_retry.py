"""El juez del estimado reintenta una vez un 429 del proveedor."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.estimate import judge as est_judge


class _Http(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


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
def _no_delay(monkeypatch):
    monkeypatch.setattr(est_judge, "_RETRY_429_DELAY_S", 0.0)


async def test_a_429_is_retried_once_and_the_second_answer_counts():
    client = _Client(_Http(429), "ok")
    assert await est_judge._create_with_retry(client, {}) == "ok"
    assert client.calls == 2


async def test_two_429_in_a_row_give_up():
    client = _Client(_Http(429), _Http(429))
    with pytest.raises(_Http):
        await est_judge._create_with_retry(client, {})
    assert client.calls == 2


@pytest.mark.parametrize("error", [_Http(401), _Http(500), TimeoutError("30s")])
async def test_other_errors_are_not_retried(error):
    client = _Client(error, "ok")
    with pytest.raises(type(error)):
        await est_judge._create_with_retry(client, {})
    assert client.calls == 1
