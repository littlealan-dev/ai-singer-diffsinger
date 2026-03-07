from dataclasses import dataclass
import asyncio

from src.backend.credit_retry import retry_credit_op


@dataclass(frozen=True)
class _Result:
    status: str


def test_retry_credit_op_returns_immediate_success(monkeypatch):
    calls = {"count": 0}
    sleeps: list[float] = []

    def op():
        calls["count"] += 1
        return _Result(status="settled")

    async def fake_sleep(delay: float):
        sleeps.append(delay)

    monkeypatch.setattr("src.backend.credit_retry.asyncio.sleep", fake_sleep)

    result = asyncio.run(
        retry_credit_op(op, max_attempts=3, base_delay=0.5)
    )

    assert result.status == "settled"
    assert calls["count"] == 1
    assert sleeps == []


def test_retry_credit_op_retries_then_succeeds(monkeypatch):
    statuses = iter(["infra_error", "infra_error", "settled"])
    sleeps: list[float] = []

    def op():
        return _Result(status=next(statuses))

    async def fake_sleep(delay: float):
        sleeps.append(delay)

    monkeypatch.setattr("src.backend.credit_retry.asyncio.sleep", fake_sleep)

    result = asyncio.run(
        retry_credit_op(op, max_attempts=3, base_delay=0.5)
    )

    assert result.status == "settled"
    assert sleeps == [0.5, 1.0]


def test_retry_credit_op_exhausts_retries(monkeypatch):
    calls = {"count": 0}
    sleeps: list[float] = []

    def op():
        calls["count"] += 1
        return _Result(status="infra_error")

    async def fake_sleep(delay: float):
        sleeps.append(delay)

    monkeypatch.setattr("src.backend.credit_retry.asyncio.sleep", fake_sleep)

    result = asyncio.run(
        retry_credit_op(op, max_attempts=3, base_delay=0.25)
    )

    assert result.status == "infra_error"
    assert calls["count"] == 3
    assert sleeps == [0.25, 0.5]


def test_retry_credit_op_bypasses_non_retryable_status(monkeypatch):
    calls = {"count": 0}
    sleeps: list[float] = []

    def op():
        calls["count"] += 1
        return _Result(status="already_settled")

    async def fake_sleep(delay: float):
        sleeps.append(delay)

    monkeypatch.setattr("src.backend.credit_retry.asyncio.sleep", fake_sleep)

    result = asyncio.run(
        retry_credit_op(op, max_attempts=3, base_delay=0.5)
    )

    assert result.status == "already_settled"
    assert calls["count"] == 1
    assert sleeps == []
