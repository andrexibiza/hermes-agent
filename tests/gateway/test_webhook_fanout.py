"""Fan-out accounting contracts for Task 13."""

import pytest

from gateway.platforms.webhook_delivery_results import run_fanout


class Result:
    def __init__(self, success, error=None):
        self.success = success
        self.error = error


@pytest.mark.asyncio
async def test_one_failure_does_not_erase_another_target_success():
    async def ok():
        return Result(True)

    async def fail():
        raise RuntimeError("target unavailable")

    result = await run_fanout([("discord", ok), ("slack", fail)])
    assert result.any_success is True
    assert result.all_success is False
    assert result.failures[0].target == "slack"


@pytest.mark.asyncio
async def test_all_failures_are_retained():
    async def first():
        return Result(False, "one")

    async def second():
        return Result(False, "two")

    result = await run_fanout([("a", first), ("b", second)])
    assert [item.error for item in result.failures] == ["one", "two"]
