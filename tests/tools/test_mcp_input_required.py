"""Bounded ``input_required`` (MRTR) continuation + supervised subscriptions/listen.

Acceptance tests for #88698 R2 Slices A/B/C — Hermes-owned SEP-2322
multi-round-trip continuation (``_call_with_input_required``), connection
generation binding, the supervised modern-era ``subscriptions/listen``
stream with visible recovery, and the liveness tie-in. All duck-typed —
no real MCP servers or subprocesses.
"""

import asyncio
import contextvars
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tools.mcp_tool as mcp_tool
from tools.mcp_tool import (
    MCPServerTask,
    _LISTEN_RETRY_BACKOFFS,
    _call_with_input_required,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _task(**cfg):
    t = MCPServerTask("mrtr-test")
    t._config = cfg
    return t


def _ir(request_state="rs-1", key="elicit"):
    """Build a real InputRequiredResult carrying one ElicitRequest."""
    from mcp.types import ElicitRequest, InputRequiredResult
    from mcp_types import ElicitRequestFormParams

    req = ElicitRequest(
        params=ElicitRequestFormParams(
            message="Approve?", requestedSchema={"type": "object"}
        )
    )
    return InputRequiredResult(input_requests={key: req}, request_state=request_state)


class _FakeSession:
    """Scripted ClientSession stand-in: records kwargs, pops scripted results."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []  # list of (args, kwargs)
        self.dispatch_calls = []
        self.dispatch_outcome = None  # None = default InputResponse, or ErrorData
        self.replay_var = contextvars.ContextVar("mrtr-replay", default="unset")
        self.replay_seen = None

    async def call_tool(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.results.pop(0)

    async def dispatch_input_request(self, ctx, request):
        self.dispatch_calls.append((ctx, request))
        self.replay_seen = self.replay_var.get()
        return self.dispatch_outcome


# ---------------------------------------------------------------------------
# Slice A — bounded MRTR continuation (T1–T4)
# ---------------------------------------------------------------------------

class TestInputRequiredContinuation:
    def test_opt_in_and_retry_carries_request_state_verbatim(self):
        session = _FakeSession(
            _ir(request_state="rs-original"),
            SimpleNamespace(result="terminal"),
        )
        session.dispatch_outcome = SimpleNamespace(input_response=True)
        server = _task()
        server.session = session

        async def drive():
            # Snapshot a context whose contextvar carries the gateway session;
            # the helper replays it into the dispatch path (T1).
            token = session.replay_var.set("gateway-session-7")
            server._pending_call_context = contextvars.copy_context()
            session.replay_var.reset(token)
            return await _call_with_input_required(
                server, session.call_tool, "pay", arguments={"amt": 1},
                timeout=30,
            )

        result = _run(drive())
        assert result.result == "terminal"

        # First call carried the allow_input_required opt-in.
        first_args, first_kwargs = session.calls[0]
        assert first_kwargs.get("allow_input_required") is True
        assert "input_responses" not in first_kwargs
        assert "request_state" not in first_kwargs

        # Retry carried (input_responses, request_state) with request_state
        # string-identical to what the server sent.
        retry_args, retry_kwargs = session.calls[1]
        assert retry_kwargs.get("request_state") == "rs-original"
        responses = retry_kwargs.get("input_responses")
        assert responses is not None
        assert "elicit" in responses
        assert retry_kwargs.get("allow_input_required") is True

        # The embedded request was dispatched through dispatch_input_request
        # with the replayed context (T1 contextvars replay).
        assert len(session.dispatch_calls) == 1
        ctx, request = session.dispatch_calls[0]
        assert ctx.request_id == "elicit"
        assert session.replay_seen == "gateway-session-7"

    def test_bounded_rounds_default_cap(self):
        server = _task()
        session = _FakeSession(*[_ir(request_state=f"rs-{i}") for i in range(20)])
        server.session = session

        with pytest.raises(RuntimeError, match="more than 10 rounds"):
            _run(
                _call_with_input_required(
                    server, session.call_tool, "pay", arguments={}, timeout=30,
                )
            )
        # SDK semantics: initial call + 10 retry rounds = 11 InputRequiredResult
        # legs max; the 12th call is never issued.
        assert len(session.calls) == 11

    def test_bounded_rounds_configurable(self):
        server = _task(input_required_max_rounds=3)
        session = _FakeSession(*[_ir(request_state=f"rs-{i}") for i in range(10)])
        server.session = session

        with pytest.raises(RuntimeError, match="more than 3 rounds"):
            _run(
                _call_with_input_required(
                    server, session.call_tool, "pay", arguments={}, timeout=30,
                )
            )
        assert len(session.calls) == 4

    def test_fail_closed_refusal_aborts_no_retry(self):
        from mcp.types import ErrorData

        server = _task()
        session = _FakeSession(_ir(request_state="rs-1"))
        session.dispatch_outcome = ErrorData(code=-32000, message="declined")
        server.session = session

        with pytest.raises(RuntimeError, match="refused input request"):
            _run(
                _call_with_input_required(
                    server, session.call_tool, "pay", arguments={}, timeout=30,
                )
            )
        # The refused input was never retried: exactly one call, no retry.
        assert len(session.calls) == 1

    def test_generation_change_aborts_mid_loop(self):
        server = _task()
        session = _FakeSession(_ir(request_state="rs-1"))
        server.session = session
        original_generation = server._connection_generation

        async def drive():
            # Bump the generation between the first call and the dispatch.
            class _BumpingSession(_FakeSession):
                def __init__(self):
                    super().__init__(_ir(request_state="rs-1"))
                    self.bumped = False

                async def dispatch_input_request(self, ctx, request):
                    if not self.bumped:
                        self.bumped = True
                        server._connection_generation += 1
                    return SimpleNamespace()

            s = _BumpingSession()
            server.session = s
            with pytest.raises(RuntimeError, match="connection generation changed"):
                await _call_with_input_required(
                    server, s.call_tool, "pay", arguments={}, timeout=30,
                )

        _run(drive())
        assert server._connection_generation == original_generation + 1

    def test_terminal_result_passthrough_no_opt_in_flag_on_plain_result(self):
        session = _FakeSession(SimpleNamespace(result="plain"))
        server = _task()
        server.session = session

        out = _run(
            _call_with_input_required(
                server, session.call_tool, "echo", arguments={}, timeout=30,
            )
        )
        assert out.result == "plain"
        assert session.calls[0][1].get("allow_input_required") is True


# ---------------------------------------------------------------------------
# Slice B — supervised subscriptions/listen (T5–T7)
# ---------------------------------------------------------------------------

def _modern_session():
    return SimpleNamespace(protocol_version="2026-07-28")


def _handshake_session():
    return SimpleNamespace(protocol_version="2025-11-25")


class _FakeSubscription:
    """Async iterator of listen events (duck-typed Subscription)."""

    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


class _FakeListenStream:
    """Async context manager yielding a _FakeSubscription (duck-typed listen())."""

    def __init__(self, *events):
        self._events = list(events)
        self.enters = 0

    async def __aenter__(self):
        self.enters += 1
        return _FakeSubscription(self._events)

    async def __aexit__(self, *exc):
        return False


class TestListenSupervision:
    def test_refetches_tools_on_list_changed(self, monkeypatch):
        server = _task()
        server.session = _modern_session()
        # MCPServerTask is slotted: patch the method at class level.
        refresh_mock = AsyncMock()
        monkeypatch.setattr(mcp_tool.MCPServerTask, "_refresh_tools", refresh_mock)
        changed = SimpleNamespace(type="tools/list_changed")
        monkeypatch.setattr(
            mcp_tool, "_open_listen_stream",
            lambda session: _FakeListenStream(changed),
        )
        monkeypatch.setattr(mcp_tool, "_LISTEN_RETRY_BACKOFFS", (0.0, 0.0, 0.0))

        _run(server._supervise_listen_stream())
        refresh_mock.assert_awaited_once()
        assert server.subscription_state == "closed"

    def test_subscription_lost_recovers_with_relisten_and_refetch(self, monkeypatch):
        server = _task()
        server.session = _modern_session()
        refresh_mock = AsyncMock()
        monkeypatch.setattr(mcp_tool.MCPServerTask, "_refresh_tools", refresh_mock)
        from mcp.client.subscriptions import SubscriptionLost

        calls = {"n": 0}

        def _flaky_listen(session):
            calls["n"] += 1
            if calls["n"] == 1:
                raise SubscriptionLost("stream dropped")
            return _FakeListenStream(SimpleNamespace(type="tools/list_changed"))

        monkeypatch.setattr(mcp_tool, "_open_listen_stream", _flaky_listen)
        monkeypatch.setattr(mcp_tool, "_LISTEN_RETRY_BACKOFFS", (0.0, 0.0, 0.0))

        _run(server._supervise_listen_stream())
        assert calls["n"] == 2
        assert server._listen_recovery_count >= 1
        refresh_mock.assert_awaited_once()
        assert server.subscription_state == "closed"

    def test_listen_guard_handshake_era_no_stream(self):
        server = _task()
        server.session = _handshake_session()

        server._start_listen_supervision()
        assert server.subscription_state == "none"
        assert server._listen_task is None

    def test_listen_loss_exhausted_budget_triggers_reconnect(self, monkeypatch):
        from mcp.client.subscriptions import SubscriptionLost

        server = _task()
        server.session = _modern_session()
        refresh_mock = AsyncMock()
        monkeypatch.setattr(mcp_tool.MCPServerTask, "_refresh_tools", refresh_mock)

        def _always_lost(session):
            raise SubscriptionLost("dead stream")

        monkeypatch.setattr(mcp_tool, "_open_listen_stream", _always_lost)
        monkeypatch.setattr(mcp_tool, "_LISTEN_RETRY_BACKOFFS", (0.0, 0.0, 0.0))

        _run(server._supervise_listen_stream())
        assert server.subscription_state == "lost"
        assert server._reconnect_event.is_set()
        assert server._listen_recovery_count == 3


# ---------------------------------------------------------------------------
# Slice C — era-aware liveness (T8 second clause)
# ---------------------------------------------------------------------------

class TestKeepaliveEraSemantics:
    def test_modern_era_keepalive_logs_request_level_semantics(self, caplog):
        server = _task()
        server.session = MagicMock()
        server.negotiated_era = "stateless"
        server._ping_unsupported = False
        server.session.send_ping = AsyncMock()

        with caplog.at_level(logging.DEBUG, logger="tools.mcp_tool"):
            _run(server._keepalive_probe())

        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "request-level health only" in joined
        assert server.liveness_strategy == "ping"

    def test_ping_unsupported_latches_list_tools_strategy(self, monkeypatch):
        from mcp.shared.exceptions import MCPError
        from mcp.types import ErrorData

        server = _task()
        server.session = MagicMock()
        server.negotiated_era = "legacy"
        server.session.send_ping = AsyncMock(
            side_effect=MCPError.from_error_data(
                ErrorData(code=-32601, message="Method not found: ping")
            )
        )
        server.session.list_tools = AsyncMock()
        monkeypatch.setattr(
            mcp_tool.MCPServerTask, "_advertises_tools", lambda self: True
        )

        _run(server._keepalive_probe())
        assert server._ping_unsupported is True
        assert server.liveness_strategy == "list_tools"
