"""Exactly-once completion coordination for Task 13."""

import asyncio

import pytest

from gateway.platforms.webhook_delivery_results import CompletionOnce


@pytest.mark.asyncio
async def test_completion_action_runs_once_under_race():
    once = CompletionOnce()
    calls = 0

    async def action():
        nonlocal calls
        calls += 1

    results = await asyncio.gather(
        once.run("execution:script", action),
        once.run("execution:script", action),
        once.run("execution:script", action),
    )
    assert results.count(True) == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_completion_dedup_ledger_is_ttl_and_size_bounded(monkeypatch):
    import itertools

    import gateway.platforms.webhook_delivery_results as results_module

    # Infinite monotonic clock: patching the shared time.monotonic means asyncio
    # internals also consume values, so a fixed-length generator exhausts.
    clock = itertools.count()
    monkeypatch.setattr(results_module.time, "monotonic", lambda: next(clock))
    once = CompletionOnce(ttl_seconds=5, max_entries=1)

    async def action():
        return None

    assert await once.run("first", action)
    assert await once.run("second", action)
    assert len(once._completed) == 1
    assert await once.run("first", action)
