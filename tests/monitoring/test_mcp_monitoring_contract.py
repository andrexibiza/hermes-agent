"""Focused contracts for MCP monitoring and alert delivery."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class _FakeEmitter:
    def __init__(self):
        self.subscribers = []

    def subscribe(self, callback):
        if callback not in self.subscribers:
            self.subscribers.append(callback)

    def unsubscribe(self, callback):
        if callback in self.subscribers:
            self.subscribers.remove(callback)


class _FakeExecutor:
    def __init__(self, *, fail=False, **_kwargs):
        self.fail = fail
        self.calls = []
        self.shutdown_calls = 0

    def submit(self, *args):
        if self.fail:
            raise RuntimeError("submit failed")
        self.calls.append(args)
        return object()

    def shutdown(self, **_kwargs):
        self.shutdown_calls += 1


def _event(name="mcp_liveness_transition", **overrides):
    event = {
        "event": "gateway_diagnostic",
        "name": name,
        "subsystem": "mcp",
        "old_state": "degraded",
        "new_state": "parked",
        "severity": "warning",
        "error_code": "server-\nBearer sk-1234567890 user@example.com",
        "platform": "ignored",
        "reason": "reconnect failed\nwith control",
    }
    event.update(overrides)
    return event


def test_alert_formatter_is_exact_and_redacts_identity():
    from agent.monitoring import alert_notifier as notifier

    result = notifier._format_liveness_alert(_event())
    assert result is not None
    key, title, body = result
    assert "mcp_liveness_parked:" in key
    assert "sk-1234567890" not in title + body + key
    assert "user@example.com" not in title + body + key
    assert "\n" not in title
    assert "reason=" in body

    unrelated = _event(name="some_other_breaker_event")
    assert notifier._format_liveness_alert(unrelated) is None
    assert notifier._format_breaker_alert(_event(name="mcp_liveness_transition")) is None


def test_alert_submit_failure_does_not_consume_dedup(monkeypatch):
    from agent.monitoring import alert_notifier as notifier

    executor = _FakeExecutor(fail=True)
    notifier.stop_alert_notifier()
    monkeypatch.setattr(notifier, "_executor", executor)
    monkeypatch.setattr(notifier, "_webhook_url", "https://example.invalid/hook")
    monkeypatch.setattr(notifier, "_now", lambda: 1000.0)

    assert notifier._dedup_send("key", "title", "body") is False
    assert "key" not in notifier._last_sent

    executor.fail = False
    assert notifier._dedup_send("key", "title", "body") is True
    assert notifier._dedup_send("key", "title", "body") is False
    notifier.stop_alert_notifier()


def test_notifier_lifecycle_is_idempotent_and_resets_state(monkeypatch):
    from agent.monitoring import alert_notifier as notifier

    emitter = _FakeEmitter()
    executor = _FakeExecutor()
    notifier.stop_alert_notifier()
    monkeypatch.setattr(notifier, "get_emitter", lambda: emitter, raising=False)
    monkeypatch.setattr("agent.monitoring.emitter.get_emitter", lambda: emitter)
    monkeypatch.setattr(notifier, "ThreadPoolExecutor", lambda **kwargs: executor)

    notifier.start_alert_notifier({})
    assert emitter.subscribers == []
    notifier.start_alert_notifier({"monitoring": {"alert": {"feishu_webhook_url": "https://example.invalid"}}})
    assert emitter.subscribers == [notifier._on_event]
    notifier._last_sent["x"] = 1.0
    notifier.start_alert_notifier({"monitoring": {"alert": {"feishu_webhook_url": "https://example.invalid/2"}}})
    assert emitter.subscribers == [notifier._on_event]
    assert notifier._last_sent == {}
    notifier.stop_alert_notifier()
    notifier.stop_alert_notifier()
    assert emitter.subscribers == []
    assert notifier._executor is None
    assert notifier._webhook_url == ""


def test_notifier_start_cleans_up_after_failed_subscribe(monkeypatch):
    from agent.monitoring import alert_notifier as notifier

    class _FailingEmitter(_FakeEmitter):
        def subscribe(self, callback):
            super().subscribe(callback)
            raise RuntimeError("subscribe failed")

    emitter = _FailingEmitter()
    executor = _FakeExecutor()
    notifier.stop_alert_notifier()
    monkeypatch.setattr("agent.monitoring.emitter.get_emitter", lambda: emitter)
    monkeypatch.setattr(notifier, "ThreadPoolExecutor", lambda **kwargs: executor)
    notifier.start_alert_notifier({"monitoring": {"alert": {"feishu_webhook_url": "https://example.invalid"}}})
    assert emitter.subscribers == []
    assert executor.shutdown_calls == 1
    assert notifier._executor is None


def test_breaker_config_rejects_invalid_values_atomically(monkeypatch):
    pytest.importorskip("mcp.client.auth.oauth2")
    from tools import mcp_tool

    invalid = [0, -1, float("nan"), float("inf"), 101]
    for value in invalid:
        mcp_tool._load_breaker_config({"_circuit_breaker": {"threshold": value}})
        assert mcp_tool._breaker_cfg == {}
    mcp_tool._load_breaker_config({"_circuit_breaker": {
        "threshold": 4,
        "cooldown_sec": 12.0,
        "connect_retry_base_sec": 2.0,
        "connect_retry_max_sec": 8.0,
    }})
    assert mcp_tool._breaker_threshold() == 4
    assert mcp_tool._breaker_cooldown_sec() == 12.0
    assert mcp_tool._connect_retry_base_sec() == 2.0
    assert mcp_tool._connect_retry_max_sec() == 8.0
    mcp_tool._load_breaker_config({})


def test_configured_connect_backoff_is_reachable(monkeypatch):
    pytest.importorskip("mcp.client.auth.oauth2")
    from tools import mcp_tool

    mcp_tool._load_breaker_config({"_circuit_breaker": {
        "connect_retry_base_sec": 2.0,
        "connect_retry_max_sec": 5.0,
    }})
    mcp_tool._server_connect_failures.clear()
    mcp_tool._server_connect_retry_after.clear()
    monkeypatch.setattr(mcp_tool.time, "monotonic", lambda: 100.0)
    mcp_tool._record_connect_failure("configured")
    assert mcp_tool._server_connect_retry_after["configured"] == 102.0
    mcp_tool._record_connect_failure("configured")
    assert mcp_tool._server_connect_retry_after["configured"] == 104.0
    mcp_tool._record_connect_failure("configured")
    assert mcp_tool._server_connect_retry_after["configured"] == 105.0
    mcp_tool._clear_connect_failure("configured")
    mcp_tool._load_breaker_config({})


def test_transition_emission_contains_bounded_reason_and_survives_logger_failure(monkeypatch):
    pytest.importorskip("mcp.client.auth.oauth2")
    from agent.monitoring import emitter
    from tools import mcp_tool

    captured = []
    fake = MagicMock()
    fake.emit.side_effect = captured.append
    monkeypatch.setattr(emitter, "get_emitter", lambda: fake)
    mcp_tool._emit_liveness_transition(
        "srv\nBearer sk-1234567890 user@example.com",
        "connected",
        "degraded",
        reason="x\n" + "r" * 500,
    )
    event = captured[0]
    data = event.to_dict()
    assert data["name"] == "mcp_liveness_transition"
    assert data["subsystem"] == "mcp"
    assert data["reason"] != data["source_logger"] if "source_logger" in data else True
    assert len(data["reason"]) <= 160
    assert "sk-1234567890" not in str(data)

    monkeypatch.setattr(mcp_tool.logger, "debug", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("log")))
    monkeypatch.setattr(emitter, "get_emitter", lambda: (_ for _ in ()).throw(RuntimeError("emit")))
    mcp_tool._emit_liveness_transition("srv", "connected", "degraded", reason="emit-failure")
