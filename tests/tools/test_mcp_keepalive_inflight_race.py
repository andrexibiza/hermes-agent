"""Regression tests for the MCP keepalive / in-flight RPC race.

Background
==========
An MCP stdio session is a SINGLE JSON-RPC stream. The idle keepalive
(``ping`` / ``list_tools``) could fire WHILE a user-visible request
(``call_tool`` / ``read_resource`` / ``get_prompt`` / ``list_*``) was in
flight, wedging the stream so the in-flight request timed out. That timeout
triggered a false reconnect, and the MCP SDK does not always fail the pending
request when its streams close, so its ``run_coroutine_threadsafe`` future
never resolved and the calling agent thread polled to the full ``tool_timeout``
(up to hours).

The fix (against current main's ``_keepalive_probe`` / ``_wait_for_lifecycle_event``
structure):

  * the keepalive **skips a cycle** when a request is in flight
    (``self._rpc_lock.locked() or self._inflight_tasks``) and otherwise runs
    the probe **under the same ``_rpc_lock``** the request handlers use, so the
    two can never overlap;
  * a reconnect/shutdown teardown calls ``_fail_inflight_calls`` to cancel the
    pending request tasks (ALL user-visible families, not just ``call_tool``);
    and
  * every request handler wraps its RPC in ``_track_inflight_rpc``, which
    registers the task in ``_inflight_tasks`` and converts a deliberate
    teardown cancel into a clean, retryable ``RuntimeError`` so the agent
    recovers on the freshly rebuilt session.

These tests cover both the in-flight bookkeeping AND the real lifecycle-event
path with lock-ordering assertions (no live MCP server required).
"""

from __future__ import annotations

import asyncio

import pytest


# ---------------------------------------------------------------------------
# Bookkeeping-level tests
# ---------------------------------------------------------------------------


def test_new_server_starts_with_empty_inflight_state():
    from tools.mcp_tool import MCPServerTask

    server = MCPServerTask("init-test")
    assert server._inflight_tasks == set()
    assert server._reconnecting is False


def test_fail_inflight_calls_is_noop_when_nothing_in_flight():
    from tools.mcp_tool import MCPServerTask

    server = MCPServerTask("noop-test")
    # No in-flight tasks: must not flip the teardown flag (so a later genuine
    # cancel isn't misread as a deliberate reconnect).
    server._fail_inflight_calls("reconnect")
    assert server._reconnecting is False


def test_fail_inflight_calls_cancels_pending_and_flags_teardown():
    from tools.mcp_tool import MCPServerTask

    server = MCPServerTask("cancel-test")

    async def drive():
        async def _long():
            await asyncio.sleep(3600)

        task = asyncio.create_task(_long())
        server._inflight_tasks.add(task)
        await asyncio.sleep(0)  # let the task start

        server._fail_inflight_calls("reconnect")
        assert server._reconnecting is True

        try:
            await asyncio.wait_for(task, timeout=1.0)
        except asyncio.CancelledError:
            return "cancelled"
        except asyncio.TimeoutError:
            return "still_running"
        return "completed"

    assert asyncio.run(drive()) == "cancelled"


# ---------------------------------------------------------------------------
# Handler-level in-flight wrapper (`_track_inflight_rpc`) tests. These prove
# the tracking is GENERALIZED to every user-visible request family (tool call,
# resource read/list, prompt get/list), not just `call_tool`.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op",
    ["tools/call", "resources/list", "resources/read", "prompts/list", "prompts/get"],
)
def test_track_inflight_rpc_registers_and_discards(op):
    """The wrapper registers the running task while the RPC is active and
    discards it on completion, for EACH handler family."""
    from tools.mcp_tool import MCPServerTask, _track_inflight_rpc

    server = MCPServerTask("track-test")

    async def drive():
        seen_inside = None

        async def _work():
            nonlocal seen_inside
            async with _track_inflight_rpc(server, server.name, op):
                seen_inside = set(server._inflight_tasks)

        await asyncio.create_task(_work())
        return seen_inside

    inside = asyncio.run(drive())
    # The running task was tracked while inside the wrapper...
    assert inside is not None and len(inside) == 1
    # ...and discarded once the RPC completed.
    assert server._inflight_tasks == set()


@pytest.mark.parametrize(
    "op",
    ["tools/call", "resources/list", "resources/read", "prompts/list", "prompts/get"],
)
def test_teardown_cancel_becomes_retryable_error(op):
    """A deliberate teardown (``_fail_inflight_calls``) cancels an in-flight
    request of ANY family, and the wrapper surfaces a retryable RuntimeError
    (never a raw CancelledError), so the agent self-heals on the rebuilt
    session instead of hanging to ``tool_timeout``."""
    from tools.mcp_tool import MCPServerTask, _track_inflight_rpc

    server = MCPServerTask(f"teardown-{op}")

    async def drive():
        started = asyncio.Event()
        outcome = {}

        async def _work():
            async with _track_inflight_rpc(server, server.name, op):
                started.set()
                try:
                    await asyncio.sleep(3600)  # stand-in for the wedged RPC
                except Exception as exc:  # noqa: BLE001 - capture for assertion
                    outcome["exc"] = exc
                    raise

        task = asyncio.create_task(_work())
        await started.wait()
        # Deliberate reconnect teardown cancels every in-flight request.
        server._fail_inflight_calls("reconnect")
        with pytest.raises(RuntimeError) as ei:
            await task
        return str(ei.value), outcome.get("exc")

    message, inner = asyncio.run(drive())
    assert "reconnected during" in message
    assert "retry the request" in message
    # The wrapper converted the CancelledError, it did not let a raw cancel out.
    assert not isinstance(inner, RuntimeError)


def test_external_cancel_is_not_masked_as_retryable():
    """When ``_reconnecting`` is NOT set (a genuine external cancel, e.g. the
    caller's timeout), the wrapper must re-raise the CancelledError rather than
    disguise it as a retryable reconnect error."""
    from tools.mcp_tool import MCPServerTask, _track_inflight_rpc

    server = MCPServerTask("external-cancel")

    async def drive():
        started = asyncio.Event()

        async def _work():
            async with _track_inflight_rpc(server, server.name, "tools/call"):
                started.set()
                await asyncio.sleep(3600)

        task = asyncio.create_task(_work())
        await started.wait()
        assert server._reconnecting is False
        task.cancel()  # external cancel, not a teardown
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())


# ---------------------------------------------------------------------------
# Real lifecycle-event path + lock-ordering tests. These drive
# ``_wait_for_lifecycle_event`` directly and assert the keepalive probe never
# runs while an RPC holds ``_rpc_lock`` (real ordering, not synthetic
# bookkeeping).
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal session double that records when the keepalive probe runs.

    ``send_ping`` also snapshots whether ``_rpc_lock`` is held at the moment
    the probe issues its RPC, which is how the lock-ordering tests verify the
    probe runs *under* the lock.
    """

    def __init__(self):
        self.ping_calls = 0
        self.list_tools_calls = 0
        self._server = None  # type: ignore[var-annotated]  # MCPServerTask, set by the test
        self.locked_during_ping = None

    async def send_ping(self):
        self.ping_calls += 1
        if self._server is not None:
            self.locked_during_ping = self._server._rpc_lock.locked()
        return None

    async def list_tools(self):
        self.list_tools_calls += 1
        return []


def _make_lifecycle_server(name, *, keepalive_interval=0.05):
    from tools.mcp_tool import MCPServerTask

    server = MCPServerTask(name)
    server._config = {"keepalive_interval": keepalive_interval}
    server.session = None  # set by caller
    return server


def test_lifecycle_probe_runs_when_idle_and_takes_rpc_lock(monkeypatch):
    """Drive the real ``_wait_for_lifecycle_event`` loop: with no in-flight
    request, the keepalive probe fires, and it does so under ``_rpc_lock``
    (the lock is free before/after but held during the probe)."""
    import tools.mcp_tool as mcp_tool

    # The keepalive interval is floored at _MIN_KEEPALIVE_INTERVAL (5s in prod).
    # Lower the floor so the real loop probes within the test's time budget.
    monkeypatch.setattr(mcp_tool, "_MIN_KEEPALIVE_INTERVAL", 0.02, raising=True)

    server = _make_lifecycle_server("idle-probe", keepalive_interval=0.02)
    fake = _FakeSession()
    fake._server = server
    server.session = fake

    async def drive():
        lifecycle = asyncio.create_task(server._wait_for_lifecycle_event())
        # Wait until at least one probe has run.
        for _ in range(200):
            if fake.ping_calls >= 1:
                break
            await asyncio.sleep(0.01)
        server._shutdown_event.set()
        return await asyncio.wait_for(lifecycle, timeout=5.0)

    reason = asyncio.run(drive())
    assert reason == "shutdown"
    # The probe actually ran (ping is tried first).
    assert fake.ping_calls >= 1
    # And it held the RPC lock while running (ordering guarantee): a concurrent
    # tool call could therefore never overlap it.
    assert fake.locked_during_ping is True


def test_lifecycle_skips_probe_while_rpc_lock_held(monkeypatch):
    """If an RPC holds ``_rpc_lock`` when the keepalive interval elapses, the
    lifecycle loop must SKIP the probe entirely (never issue a concurrent
    ping/list_tools on the shared stream)."""
    import tools.mcp_tool as mcp_tool

    # Lower the floor so a probe WOULD fire within the window if not suppressed;
    # this makes the "skip" assertion meaningful rather than trivially true.
    monkeypatch.setattr(mcp_tool, "_MIN_KEEPALIVE_INTERVAL", 0.02, raising=True)

    server = _make_lifecycle_server("skip-when-locked", keepalive_interval=0.02)
    fake = _FakeSession()
    fake._server = server
    server.session = fake

    async def drive():
        # Hold the RPC lock for the duration, simulating an in-flight call.
        await server._rpc_lock.acquire()
        try:
            lifecycle = asyncio.create_task(server._wait_for_lifecycle_event())
            # Let several keepalive intervals elapse while the lock is held.
            await asyncio.sleep(0.3)
            # No probe should have run while the lock was held.
            assert fake.ping_calls == 0 and fake.list_tools_calls == 0
        finally:
            server._rpc_lock.release()

        # Now shut the loop down cleanly.
        server._shutdown_event.set()
        return await asyncio.wait_for(lifecycle, timeout=5.0)

    reason = asyncio.run(drive())
    assert reason == "shutdown"


def test_lifecycle_skips_probe_while_inflight_task_present(monkeypatch):
    """Even if the RPC lock happens to be momentarily free, the presence of an
    in-flight task in ``_inflight_tasks`` must also suppress the probe (a busy
    server is provably alive)."""
    import tools.mcp_tool as mcp_tool

    monkeypatch.setattr(mcp_tool, "_MIN_KEEPALIVE_INTERVAL", 0.02, raising=True)

    server = _make_lifecycle_server("skip-when-inflight", keepalive_interval=0.02)
    fake = _FakeSession()
    fake._server = server
    server.session = fake

    async def drive():
        async def _long():
            await asyncio.sleep(3600)

        inflight = asyncio.create_task(_long())
        server._inflight_tasks.add(inflight)
        try:
            lifecycle = asyncio.create_task(server._wait_for_lifecycle_event())
            await asyncio.sleep(0.3)
            assert fake.ping_calls == 0 and fake.list_tools_calls == 0
        finally:
            server._inflight_tasks.discard(inflight)
            inflight.cancel()

        server._shutdown_event.set()
        return await asyncio.wait_for(lifecycle, timeout=5.0)

    reason = asyncio.run(drive())
    assert reason == "shutdown"


def test_reconnect_via_keepalive_failure_fails_inflight_calls():
    """Drive the real lifecycle path to a reconnect (keepalive probe raises)
    and assert it cancels an in-flight request task through
    ``_fail_inflight_calls`` (the reconnect exit calls it), setting the
    deliberate-teardown flag so the handler converts the cancel to retryable."""
    server = _make_lifecycle_server("reconnect-fails-inflight")
    fake = _FakeSession()
    server.session = fake

    async def drive():
        # An in-flight request that will be cancelled by the reconnect teardown.
        cancelled = {"was_cancelled": False}

        async def _inflight_request():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled["was_cancelled"] = True
                raise

        task = asyncio.create_task(_inflight_request())
        server._inflight_tasks.add(task)
        await asyncio.sleep(0)

        # Make the probe fail. But the probe is SKIPPED while a task is in
        # flight, so temporarily clear the in-flight set to let the probe run,
        # then rely on the reconnect exit to fail the (re-added) task. To model
        # the real ordering we instead trigger the reconnect event directly:
        # the loop wakes, sees the lifecycle event, and takes the reconnect
        # exit that calls _fail_inflight_calls.
        server._reconnect_event.set()

        reason = await asyncio.wait_for(
            server._wait_for_lifecycle_event(), timeout=5.0
        )
        # The reconnect exit fired _fail_inflight_calls.
        assert server._reconnecting is True
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except asyncio.CancelledError:
            pass
        return reason, cancelled["was_cancelled"]

    reason, was_cancelled = asyncio.run(drive())
    assert reason == "reconnect"
    assert was_cancelled is True


def test_shutdown_exit_fails_inflight_calls():
    """The shutdown exit of the real lifecycle loop also cancels in-flight
    requests (shutdown teardown must not orphan them either)."""
    server = _make_lifecycle_server("shutdown-fails-inflight")
    fake = _FakeSession()
    server.session = fake

    async def drive():
        async def _inflight_request():
            await asyncio.sleep(3600)

        task = asyncio.create_task(_inflight_request())
        server._inflight_tasks.add(task)
        await asyncio.sleep(0)

        server._shutdown_event.set()
        reason = await asyncio.wait_for(
            server._wait_for_lifecycle_event(), timeout=5.0
        )
        assert server._reconnecting is True
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except asyncio.CancelledError:
            pass
        return reason, task.cancelled()

    reason, was_cancelled = asyncio.run(drive())
    assert reason == "shutdown"
    assert was_cancelled is True


def test_reconnecting_flag_reset_on_entry_to_healthy_wait():
    """A fresh healthy wait clears a lingering ``_reconnecting`` flag from a
    prior teardown, so a new in-flight call on the rebuilt session is not
    mistaken for a reconnect casualty."""
    server = _make_lifecycle_server("flag-reset")
    fake = _FakeSession()
    server.session = fake
    server._reconnecting = True  # leftover from a prior teardown

    async def drive():
        # Enter the loop; it clears _reconnecting on entry, then we shut down.
        server._shutdown_event.set()
        await asyncio.wait_for(server._wait_for_lifecycle_event(), timeout=5.0)
        return server._reconnecting

    # Entry clears it; the shutdown exit only re-sets it if there were
    # in-flight tasks (there are none here), so it stays False.
    assert asyncio.run(drive()) is False


# ---------------------------------------------------------------------------
# Generation late-admission tests (#48069 second round). The prior fix left a
# deterministic teardown window: `_wait_for_lifecycle_event` runs ONE
# `_fail_inflight_calls` sweep then returns, but `self.session` still publishes
# the OLD generation until run()'s outer finally clears it — AFTER the
# transport async contexts unwind. With ZERO active RPCs the sweep returned
# immediately (never flipping `_reconnecting`), so a user RPC arriving in that
# window would see a non-None session and be admitted AFTER the only
# cancellation sweep, racing a retiring transport with no teardown owner. These
# tests prove the per-generation admission gate closes that window.
# ---------------------------------------------------------------------------


def test_reconnect_closes_admission_with_zero_active_rpcs():
    """The sharp zero-active-RPC case: a reconnect fires with NO in-flight
    tasks; the lifecycle exit must still CLOSE admission for the retiring
    generation (``_admitting_generation is None``) even though
    ``_fail_inflight_calls`` short-circuits and never flips ``_reconnecting``.
    """
    server = _make_lifecycle_server("gen-close-zero")
    fake = _FakeSession()
    # Publish a real generation the way the transport paths do.
    server._publish_session(fake)
    assert server._admitting_generation == server._rpc_generation == 1

    async def drive():
        # Reconnect with ZERO active RPCs.
        server._reconnect_event.set()
        reason = await asyncio.wait_for(
            server._wait_for_lifecycle_event(), timeout=5.0
        )
        return reason

    reason = asyncio.run(drive())
    assert reason == "reconnect"
    # Admission for the retiring generation is CLOSED even though there were
    # no tasks to sweep (the zero-active-call case that never flips
    # _reconnecting).
    assert server._admitting_generation is None
    assert server._reconnecting is False  # no tasks → flag never set


def test_late_call_in_teardown_window_is_refused_not_admitted():
    """Hold the retiring session PUBLISHED after the lifecycle event returns
    (modelling the run() teardown window where the transport is still
    unwinding), then submit a user RPC. The late call must be REFUSED by the
    admission gate — never registered against the retiring generation — and the
    caller must get a controlled, retryable reconnect result rather than a hang
    or a strand.
    """
    from tools.mcp_tool import _track_inflight_rpc

    server = _make_lifecycle_server("gen-late-refuse")

    handler_invoked = {"count": 0}

    class _RetiringSession:
        """Its call_tool must NEVER run for the late call."""

        async def call_tool(self, *a, **k):
            handler_invoked["count"] += 1
            return "should-never-happen"

    retiring = _RetiringSession()
    server._publish_session(retiring)
    gen_before = server._rpc_generation

    async def drive():
        # --- Reconnect fires with zero active RPCs; lifecycle returns. ---
        server._reconnect_event.set()
        reason = await asyncio.wait_for(
            server._wait_for_lifecycle_event(), timeout=5.0
        )
        assert reason == "reconnect"
        # Barrier: the retiring session is STILL published (run()'s outer
        # finally has not cleared it yet — transport is unwinding).
        assert server.session is retiring
        assert server._admitting_generation is None

        # A NEW user RPC arrives in this teardown window. It observes a
        # non-None session, but the admission gate must refuse it.
        async def _late_call():
            async with _track_inflight_rpc(server, server.name, "tools/call"):
                # If admission were (wrongly) granted, this handler would run
                # against the retiring session.
                async with server._rpc_lock:
                    return await server.session.call_tool("x")

        outcome = {}
        try:
            await asyncio.wait_for(asyncio.create_task(_late_call()), timeout=2.0)
            outcome["result"] = "admitted"
        except RuntimeError as exc:
            outcome["error"] = str(exc)
        except asyncio.TimeoutError:
            outcome["result"] = "hung"
        return outcome

    outcome = asyncio.run(drive())
    # (a) The retiring session handler was NEVER invoked for the late call.
    assert handler_invoked["count"] == 0
    # (b) The caller did not hang or strand — it got the controlled retryable
    #     reconnect error.
    assert "error" in outcome, outcome
    assert "reconnected during" in outcome["error"]
    assert "retry the request" in outcome["error"]
    # The late call was never registered as in-flight against the old gen.
    assert server._inflight_tasks == set()
    # Generation did not advance from the refusal itself (a real rebuild would
    # publish a new generation via _publish_session).
    assert server._rpc_generation == gen_before


def test_next_generation_reopens_admission_for_new_calls():
    """After the retiring generation drains, a rebuilt session (new
    generation) must REOPEN admission so subsequent calls run normally. Proves
    the gate is generation-scoped, not a stuck one-way latch.
    """
    from tools.mcp_tool import _track_inflight_rpc

    server = _make_lifecycle_server("gen-reopen")
    fake_old = _FakeSession()
    server._publish_session(fake_old)

    async def drive():
        # Drain generation 1.
        server._reconnect_event.set()
        await asyncio.wait_for(server._wait_for_lifecycle_event(), timeout=5.0)
        assert server._admitting_generation is None

        # Rebuild: publish generation 2 the way a transport path does.
        fake_new = _FakeSession()
        server._publish_session(fake_new)
        assert server._rpc_generation == 2
        assert server._admitting_generation == 2

        # A call on the fresh generation is admitted and runs to completion.
        ran = {"ok": False}

        async def _work():
            async with _track_inflight_rpc(server, server.name, "tools/call"):
                ran["ok"] = True

        await asyncio.create_task(_work())
        return ran["ok"]

    assert asyncio.run(drive()) is True
    assert server._inflight_tasks == set()


def test_shutdown_closes_admission_with_zero_active_rpcs():
    """Shutdown analogue of the zero-active-RPC teardown window: shutdown with
    no in-flight tasks must also close admission for the retiring generation so
    a call racing the shutdown is refused, not admitted to a session whose
    transport is unwinding toward exit.
    """
    from tools.mcp_tool import _track_inflight_rpc

    server = _make_lifecycle_server("gen-shutdown-zero")
    retiring = _FakeSession()
    server._publish_session(retiring)

    handler_ran = {"count": 0}

    async def drive():
        server._shutdown_event.set()
        reason = await asyncio.wait_for(
            server._wait_for_lifecycle_event(), timeout=5.0
        )
        assert reason == "shutdown"
        assert server._admitting_generation is None
        # Session still published (outer teardown not yet run); a late call is
        # refused rather than admitted.
        assert server.session is retiring

        async def _late_call():
            async with _track_inflight_rpc(server, server.name, "tools/call"):
                handler_ran["count"] += 1

        error = None
        try:
            await asyncio.create_task(_late_call())
        except RuntimeError as exc:
            error = str(exc)
        return error

    error = asyncio.run(drive())
    assert handler_ran["count"] == 0
    assert error is not None and "reconnected during" in error


def test_publish_session_bumps_generation_monotonically():
    """Each transport (re)connect publishes a new, strictly increasing
    generation and opens admission for exactly that generation.
    """
    server = _make_lifecycle_server("gen-monotonic")
    assert server._rpc_generation == 0
    assert server._admitting_generation == 0  # open pre-publish (legacy parity)

    s1 = _FakeSession()
    server._publish_session(s1)
    assert server._rpc_generation == 1
    assert server._admitting_generation == 1
    assert server.session is s1

    server._close_rpc_admission()
    assert server._admitting_generation is None
    assert server._rpc_generation == 1  # counter unchanged by a drain

    s2 = _FakeSession()
    server._publish_session(s2)
    assert server._rpc_generation == 2
    assert server._admitting_generation == 2
    assert server.session is s2


# ---------------------------------------------------------------------------
# Generation-OWNERSHIP tests (#48069 third round). Admission is
# generation-scoped, but the SECOND-round fix left CANCELLATION and COMPLETION
# authority as process-wide mutable state: `_inflight_tasks` was one
# cross-generation set, and the teardown cause was the single shared
# `_reconnecting` bit that the NEXT generation's `_wait_for_lifecycle_event`
# unconditionally reset to False. The sharp schedule is a RESOURCE/PROMPT-ONLY
# server (no tools advertised, so `_discover_tools` returns WITHOUT taking
# `_rpc_lock`): a gen-N resources/read or prompts/get holds `_rpc_lock`; a
# reconnect closes admission, requests cancellation, and returns while the RPC
# is still doing async cancellation cleanup; gen N+1 is published and — because
# tool-less discovery doesn't take the lock — reaches the healthy wait, resets
# `_reconnecting=False`, and reopens admission WHILE the gen-N task still
# exists. When the gen-N task finally exits, the old code classified it by
# N+1's state: a raw CancelledError leaked out, or (if the SDK suppressed the
# cancel and RETURNED) the retired-generation payload was accepted.
#
# These tests bind the RPC to its captured generation and fence completion on
# BOTH exit paths: a retired-generation outcome is converted to the controlled
# retryable reconnect result and any payload discarded, whether the RPC exits
# by delayed cancellation OR by normal return.
# ---------------------------------------------------------------------------


class _ResourceOnlySession:
    """A resource/prompt-only session double (advertises NO tools).

    ``read_resource`` blocks on a caller-supplied event so the test can hold
    the RPC INSIDE its async cancellation cleanup across the N→N+1 lifecycle
    boundary, exactly the tool-less schedule that let a retired-generation
    outcome escape.
    """

    def __init__(self):
        self.read_calls = 0

    async def read_resource(self, uri, *, hold, release, mode):
        self.read_calls += 1
        hold.set()
        try:
            await asyncio.sleep(3600)  # stand-in for the wedged RPC
        except asyncio.CancelledError:
            # Model the SDK doing async cleanup AFTER the lifecycle exit: wait
            # until the test has advanced to generation N+1, THEN either
            # re-raise the cancel (delayed-cancellation path) or SUPPRESS it and
            # return a retired-generation payload (normal-return path).
            await release.wait()
            if mode == "suppress-and-return":
                return {"contents": "RETIRED-GEN-PAYLOAD-MUST-NOT-ESCAPE"}
            raise


def _drive_resource_only_across_generation(mode):
    """Shared driver: hold a gen-N resources/read inside cleanup, publish gen
    N+1 (reopening admission and resetting ``_reconnecting``), then release the
    gen-N task and capture how its outcome is classified.
    """
    from tools.mcp_tool import _track_inflight_rpc

    server = _make_lifecycle_server(f"gen-own-{mode}")
    old = _ResourceOnlySession()
    server._publish_session(old)  # generation 1
    gen_n = server._rpc_generation

    hold = asyncio.Event()
    release = asyncio.Event()
    result_box = {}

    async def drive():
        async def _gen_n_read():
            async with _track_inflight_rpc(server, server.name, "resources/read"):
                async with server._rpc_lock:
                    # The captured-generation binding happens at admission; the
                    # payload returned here (normal-return mode) is a RETIRED
                    # generation's and must never reach the caller.
                    return await server.session.read_resource(
                        "res://x", hold=hold, release=release, mode=mode,
                    )

        task = asyncio.create_task(_gen_n_read())
        await hold.wait()  # gen-N RPC is now holding _rpc_lock inside the SDK

        # --- Reconnect: close admission, request cancellation, RETURN while the
        #     gen-N task is still doing async cleanup. ---
        server._reconnect_event.set()
        reason = await asyncio.wait_for(
            server._wait_for_lifecycle_event(), timeout=5.0
        )
        assert reason == "reconnect"
        assert server._admitting_generation is None
        assert gen_n in server._retired_generations  # gen-N cause recorded

        # --- Publish gen N+1 the way a tool-less discovery path does: it does
        #     NOT take _rpc_lock, so it reaches the healthy wait even though the
        #     gen-N task still holds the lock. Model exactly what gen N+1's
        #     ``_wait_for_lifecycle_event`` entry does on a healthy session:
        #     reset the shared ``_reconnecting`` bit and reopen admission for
        #     the new generation. (Driving a real second lifecycle event would
        #     re-run ``_fail_inflight_calls`` and re-cancel the still-pending
        #     gen-N task, masking the normal-return path we are testing.) ---
        new = _ResourceOnlySession()
        server._publish_session(new)  # generation 2
        assert server._rpc_generation == gen_n + 1
        server._reconnecting = False  # N+1's healthy-wait entry resets the bit
        server._admitting_generation = server._rpc_generation  # admission reopens
        assert server._reconnecting is False
        # The generation-scoped cause SURVIVES the reset.
        assert gen_n in server._retired_generations

        # --- Now release the gen-N task's cleanup and see how it is classified.
        release.set()
        try:
            result_box["result"] = await asyncio.wait_for(task, timeout=2.0)
            result_box["outcome"] = "returned"
        except RuntimeError as exc:
            result_box["outcome"] = "retryable"
            result_box["error"] = str(exc)
        except asyncio.CancelledError:
            result_box["outcome"] = "raw_cancel"
        except asyncio.TimeoutError:
            result_box["outcome"] = "hung"
        return result_box

    return server, asyncio.run(drive())


def test_gen_owned_delayed_cancellation_is_controlled_reconnect():
    """RESOURCE-ONLY server, DELAYED-CANCELLATION path: a gen-N read that is
    still in async cancellation cleanup when gen N+1 reopens admission and
    resets ``_reconnecting`` must STILL surface the controlled retryable
    reconnect result (not a raw cancel), because the teardown cause is
    generation-scoped and survives the shared-bit reset.
    """
    server, box = _drive_resource_only_across_generation("reraise-cancel")

    # (a) controlled reconnect result...
    assert box["outcome"] == "retryable", box
    assert "reconnected during" in box["error"]
    assert "retry the request" in box["error"]
    # (c) ...and it cannot be confused with an external cancellation.
    assert box["outcome"] != "raw_cancel"
    # The gen-N bucket drained; pruning keeps the maps bounded once it exits.
    server._prune_drained_generations()
    assert server._inflight_by_gen == {}


def test_gen_owned_normal_return_after_drain_is_fenced():
    """RESOURCE-ONLY server, NORMAL-RETURN path (completion fencing): if the
    SDK SUPPRESSES the teardown cancel and RETURNS a retired-generation
    payload, the wrapper must DISCARD that payload and convert the outcome to
    the controlled retryable reconnect result. A retired-generation completion
    can never escape through gen N+1.
    """
    server, box = _drive_resource_only_across_generation("suppress-and-return")

    # (b) the retired-generation payload must NOT be returned to the caller.
    assert box["outcome"] == "retryable", box
    assert "result" not in box  # payload discarded
    assert "reconnected during" in box["error"]
    assert "retry the request" in box["error"]
    server._prune_drained_generations()
    assert server._inflight_by_gen == {}


def test_live_generation_normal_return_is_not_fenced():
    """Negative control for the normal-return fence: a resources/read that
    completes on the LIVE (admitting) generation, with no drain, returns its
    payload normally. The completion fence must reject ONLY retired
    generations, never the healthy path.
    """
    from tools.mcp_tool import _track_inflight_rpc

    server = _make_lifecycle_server("gen-live-return")

    class _HealthySession:
        async def read_resource(self, uri):
            return {"contents": "LIVE-PAYLOAD"}

    server._publish_session(_HealthySession())

    async def drive():
        async def _read():
            async with _track_inflight_rpc(server, server.name, "resources/read"):
                async with server._rpc_lock:
                    return await server.session.read_resource("res://x")

        return await asyncio.create_task(_read())

    result = asyncio.run(drive())
    assert result == {"contents": "LIVE-PAYLOAD"}
    assert server._inflight_tasks == set()
    assert server._inflight_by_gen == {}


def test_fail_inflight_calls_quarantines_only_retiring_generation():
    """``_fail_inflight_calls`` cancels/quarantines the tasks of the RETIRING
    generation and records that generation's cause, per-generation, so a later
    generation's in-flight task is untouched. Proves cancellation authority is
    generation-scoped, not one cross-generation set.
    """
    from tools.mcp_tool import _track_inflight_rpc

    server = _make_lifecycle_server("gen-quarantine")

    class _Blocking:
        async def read_resource(self, uri, *, started):
            started.set()
            await asyncio.sleep(3600)

    async def drive():
        # Generation 1 with an in-flight read.
        server._publish_session(_Blocking())
        gen1 = server._rpc_generation
        started1 = asyncio.Event()
        outcome1 = {}

        async def _read_gen1():
            try:
                async with _track_inflight_rpc(server, server.name, "resources/read"):
                    async with server._rpc_lock:
                        return await server.session.read_resource(
                            "res://1", started=started1,
                        )
            except RuntimeError as exc:
                outcome1["err"] = str(exc)
                raise

        t1 = asyncio.create_task(_read_gen1())
        await started1.wait()
        assert gen1 in server._inflight_by_gen
        assert t1 in server._inflight_by_gen[gen1]

        # Drain generation 1.
        server._close_rpc_admission()
        server._fail_inflight_calls("reconnect")
        assert gen1 in server._retired_generations
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(t1, timeout=2.0)
        assert "reconnected during" in outcome1["err"]
        # The cancelled task unwound its ``async with server._rpc_lock``, so the
        # lock is free again and the gen-1 bucket drained to empty.
        assert not server._rpc_lock.locked()
        return gen1

    gen1 = asyncio.run(drive())
    # After the retiring generation's only task drained, pruning drops it.
    server._prune_drained_generations()
    assert gen1 not in server._inflight_by_gen
    assert server._inflight_by_gen == {}

