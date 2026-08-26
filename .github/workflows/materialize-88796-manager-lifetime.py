"""Materialize #88796 manager-lifetime provider quarantine on current main.

This script is intentionally exact-anchor and idempotent. It changes only
agent/memory_manager.py and tests/agent/test_memory_async_sync.py.
"""

from __future__ import annotations

from pathlib import Path


MANAGER_PATH = Path("agent/memory_manager.py")
TEST_PATH = Path("tests/agent/test_memory_async_sync.py")


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"{label} anchor drifted: expected 1, found {count}")
    return source.replace(old, new, 1)


def method_region(source: str, name: str) -> tuple[int, int, str]:
    marker = f"    def {name}("
    count = source.count(marker)
    if count != 1:
        raise SystemExit(f"{name} method anchor drifted: expected 1, found {count}")
    start = source.index(marker)
    next_method = source.find("\n    def ", start + len(marker))
    end = next_method + 1 if next_method != -1 else len(source)
    return start, end, source[start:end]


def replace_method(source: str, name: str, replacement: str) -> str:
    start, end, _ = method_region(source, name)
    if not replacement.endswith("\n"):
        replacement += "\n"
    return source[:start] + replacement + source[end:]


def insert_method_gate(
    source: str,
    name: str,
    *,
    loop_anchor: str,
    operation: str,
    indent: str,
) -> str:
    start, end, region = method_region(source, name)
    gate = (
        f'{indent}if not self._provider_call_allowed(provider, "{operation}"):\n'
        f"{indent}    continue\n"
    )
    if gate in region:
        return source
    count = region.count(loop_anchor)
    if count != 1:
        raise SystemExit(f"{name} loop anchor drifted: expected 1, found {count}")
    region = region.replace(loop_anchor, loop_anchor + gate, 1)
    return source[:start] + region + source[end:]


manager = MANAGER_PATH.read_text(encoding="utf-8")

if "class _ProviderGateState:" not in manager:
    manager = replace_once(
        manager,
        "from __future__ import annotations\n\nimport json\n",
        "from __future__ import annotations\n\nfrom dataclasses import dataclass, field\nimport json\n",
        "dataclass import",
    )

    gate_types = '''\n\n@dataclass\nclass _ProviderGateState:\n    """Manager-local admission state for one external provider instance."""\n\n    next_prefetch_generation: int = 0\n    active_prefetch_generation: Optional[int] = None\n    quarantined: bool = False\n    timeout_generation: Optional[int] = None\n\n\n@dataclass(frozen=True)\nclass _ExternalPrefetchLease:\n    """Owner token for one bounded external prefetch attempt."""\n\n    provider_key: int\n    provider_name: str\n    generation: int\n    completed: threading.Event = field(\n        default_factory=threading.Event, compare=False, repr=False\n    )\n'''
    manager = replace_once(
        manager,
        "\n\nclass MemoryManager:\n",
        gate_types + "\n\nclass MemoryManager:\n",
        "provider gate type insertion",
    )

old_state = '''        self._external_prefetch_threads: Dict[str, threading.Thread] = {}\n        self._external_prefetch_lock = threading.Lock()\n'''
new_state = '''        # Quarantine is scoped to this manager and keyed by provider identity.\n        # It is monotonic: no completion, re-initialization, or later hook can\n        # restore an external provider after an uncancellable prefetch timeout.\n        self._provider_gate_lock = threading.RLock()\n        self._provider_gate_states: Dict[int, _ProviderGateState] = {}\n'''
if new_state not in manager:
    manager = replace_once(manager, old_state, new_state, "manager gate state")

if "def _provider_call_allowed(" not in manager:
    _, insert_at, _ = method_region(manager, "get_provider")
    gate_methods = '''\n    def _provider_call_allowed(\n        self, provider: MemoryProvider, operation: str\n    ) -> bool:\n        """Return whether ``provider`` may perform a semantic operation.\n\n        The builtin provider is never quarantined. External-provider timeout\n        state belongs to this MemoryManager instance and is never cleared.\n        """\n        if provider.name == "builtin":\n            return True\n        with self._provider_gate_lock:\n            state = self._provider_gate_states.get(id(provider))\n            allowed = state is None or not state.quarantined\n        if not allowed:\n            logger.debug(\n                "Memory provider '%s' operation '%s' suppressed by permanent "\n                "manager-lifetime quarantine",\n                provider.name,\n                operation,\n            )\n        return allowed\n\n    def _begin_external_prefetch(\n        self, provider: MemoryProvider\n    ) -> tuple[Optional[_ExternalPrefetchLease], str]:\n        """Admit one bounded prefetch and return its owner finalization token."""\n        provider_key = id(provider)\n        with self._provider_gate_lock:\n            state = self._provider_gate_states.setdefault(\n                provider_key, _ProviderGateState()\n            )\n            if state.quarantined:\n                return None, "quarantined"\n            if state.active_prefetch_generation is not None:\n                return None, "running"\n            state.next_prefetch_generation += 1\n            generation = state.next_prefetch_generation\n            state.active_prefetch_generation = generation\n        return (\n            _ExternalPrefetchLease(\n                provider_key=provider_key,\n                provider_name=provider.name,\n                generation=generation,\n            ),\n            "",\n        )\n\n    def _finalize_external_prefetch(\n        self, lease: _ExternalPrefetchLease, *, timed_out: bool\n    ) -> None:\n        """Finalize the owner token; timeout wins monotonically over completion.\n\n        Only the waiting manager thread finalizes. The provider thread merely\n        sets ``lease.completed`` after publishing its result. Therefore a late\n        completion can never clear or race around a timeout decision.\n        """\n        quarantined_now = False\n        with self._provider_gate_lock:\n            state = self._provider_gate_states.setdefault(\n                lease.provider_key, _ProviderGateState()\n            )\n            if timed_out:\n                quarantined_now = not state.quarantined\n                state.quarantined = True\n                state.timeout_generation = lease.generation\n            if state.active_prefetch_generation == lease.generation:\n                state.active_prefetch_generation = None\n        if quarantined_now:\n            logger.warning(\n                "Memory provider '%s' permanently quarantined for this "\n                "MemoryManager lifetime after prefetch generation %d timed out",\n                lease.provider_name,\n                lease.generation,\n            )\n\n    def _provider_is_quarantined(self, provider: MemoryProvider) -> bool:\n        """Test/diagnostic snapshot of manager-local quarantine state."""\n        if provider.name == "builtin":\n            return False\n        with self._provider_gate_lock:\n            state = self._provider_gate_states.get(id(provider))\n            return bool(state and state.quarantined)\n'''
    manager = manager[:insert_at] + gate_methods + manager[insert_at:]

prefetch_method = '''    def _prefetch_provider(\n        self, provider: MemoryProvider, query: str, *, session_id: str = ""\n    ) -> str:\n        if provider.name == "builtin":\n            return provider.prefetch(query, session_id=session_id)\n\n        lease, refusal = self._begin_external_prefetch(provider)\n        if lease is None:\n            if refusal == "running":\n                logger.debug(\n                    "Memory provider '%s' prefetch is still running; skipping this turn",\n                    provider.name,\n                )\n            else:\n                logger.debug(\n                    "Memory provider '%s' prefetch suppressed by permanent "\n                    "manager-lifetime quarantine",\n                    provider.name,\n                )\n            return ""\n\n        result_box: Dict[str, str] = {}\n        error_box: Dict[str, Exception] = {}\n\n        def _run() -> None:\n            try:\n                result_box["value"] = provider.prefetch(\n                    query, session_id=session_id\n                ) or ""\n            except Exception as exc:  # pragma: no cover - re-raised by caller\n                error_box["value"] = exc\n            finally:\n                # Publish completion only. The owner thread is the sole gate\n                # finalizer, so this late edge can never restore provider trust.\n                lease.completed.set()\n\n        # Propagate the caller's contextvars (profile HERMES_HOME override)\n        # to the prefetch thread — see _submit_background.\n        import contextvars\n        from functools import partial\n\n        thread = threading.Thread(\n            target=partial(contextvars.copy_context().run, _run),\n            daemon=True,\n            name=f"memory-prefetch-{provider.name}-{lease.generation}",\n        )\n        try:\n            thread.start()\n        except Exception:\n            self._finalize_external_prefetch(lease, timed_out=False)\n            raise\n\n        completed = lease.completed.wait(self._external_prefetch_timeout)\n        self._finalize_external_prefetch(lease, timed_out=not completed)\n        if not completed:\n            logger.warning(\n                "Memory provider '%s' prefetch timed out after %.1fs; all "\n                "semantic calls are disabled for this MemoryManager lifetime",\n                provider.name,\n                self._external_prefetch_timeout,\n            )\n            return ""\n\n        if error_box:\n            raise error_box["value"]\n        return result_box.get("value", "")\n'''
manager = replace_method(manager, "_prefetch_provider", prefetch_method)

for method, loop_anchor, operation, indent in [
    ("build_system_prompt", "        for provider in self._providers:\n", "system_prompt", "            "),
    ("describe_recall", "        for provider in self._providers:\n", "recall_status", "            "),
    ("queue_prefetch_all", "            for provider in providers:\n", "queue_prefetch", "                "),
    ("sync_all", "            for provider in providers:\n", "sync_turn", "                "),
    ("get_all_tool_schemas", "        for provider in self._providers:\n", "tool_schema", "            "),
    ("on_turn_start", "        for provider in self._providers:\n", "turn_start", "            "),
    ("on_session_end", "        for provider in self._providers:\n", "session_end", "            "),
    ("on_session_switch", "        for provider in self._providers:\n", "session_switch", "            "),
    ("supports_pre_compress_checkpoint", "        for provider in self._providers:\n", "pre_compress_capability", "            "),
    ("on_pre_compress", "        for provider in self._providers:\n", "pre_compress", "            "),
    ("on_delegation", "        for provider in self._providers:\n", "delegation", "            "),
    ("initialize_all", "        for provider in self._providers:\n", "initialize", "            "),
]:
    manager = insert_method_gate(
        manager,
        method,
        loop_anchor=loop_anchor,
        operation=operation,
        indent=indent,
    )

start, end, region = method_region(manager, "on_memory_write")
memory_anchor = '''        for provider in self._providers:\n            if provider.name == "builtin":\n                continue\n'''
memory_gate = '''            if not self._provider_call_allowed(provider, "memory_write"):\n                continue\n'''
if memory_gate not in region:
    if region.count(memory_anchor) != 1:
        raise SystemExit("on_memory_write loop anchor drifted")
    region = region.replace(memory_anchor, memory_anchor + memory_gate, 1)
    manager = manager[:start] + region + manager[end:]

start, end, region = method_region(manager, "handle_tool_call")
handle_anchor = '''        provider = self._tool_to_provider.get(tool_name)\n        if provider is None:\n            return tool_error(f"No memory provider handles tool '{tool_name}'")\n'''
handle_gate = '''        if not self._provider_call_allowed(provider, f"tool:{tool_name}"):\n            return tool_error(\n                f"Memory provider '{provider.name}' is quarantined after an "\n                "uncancellable prefetch timeout"\n            )\n'''
if handle_gate not in region:
    if region.count(handle_anchor) != 1:
        raise SystemExit("handle_tool_call anchor drifted")
    region = region.replace(handle_anchor, handle_anchor + handle_gate, 1)
    manager = manager[:start] + region + manager[end:]

names_method = '''    def get_all_tool_names(self) -> set:\n        """Return callable tool names across non-quarantined providers."""\n        return {\n            name\n            for name, provider in self._tool_to_provider.items()\n            if self._provider_call_allowed(provider, "tool_schema")\n        }\n'''
manager = replace_method(manager, "get_all_tool_names", names_method)

has_tool_method = '''    def has_tool(self, tool_name: str) -> bool:\n        """Check if a currently admitted provider handles this tool."""\n        provider = self._tool_to_provider.get(tool_name)\n        return bool(\n            provider is not None\n            and self._provider_call_allowed(provider, "tool_schema")\n        )\n'''
manager = replace_method(manager, "has_tool", has_tool_method)

MANAGER_PATH.write_text(manager, encoding="utf-8")


tests = TEST_PATH.read_text(encoding="utf-8")
if "class _QuarantineProbeProvider" not in tests:
    regression_tests = r'''


class _QuarantineProbeProvider(_SlowProvider):
    """External provider with deterministic timeout and call tracing."""

    _name = "quarantine-probe"
    pre_compress_checkpoint_api_version = 2

    def __init__(self):
        super().__init__(delay=0)
        self.calls = []
        self.prefetch_started = threading.Event()
        self.prefetch_release = threading.Event()
        self.prefetch_finished = threading.Event()
        self.sync_started = threading.Event()
        self.sync_release = threading.Event()

    def initialize(self, session_id: str = "", **kwargs) -> None:
        self.calls.append(("initialize", session_id))

    def system_prompt_block(self) -> str:
        self.calls.append(("system_prompt",))
        return "provider prompt"

    def prefetch(self, query, *, session_id: str = "") -> str:
        self.calls.append(("prefetch", query))
        self.prefetch_started.set()
        self.prefetch_release.wait(timeout=2)
        self.calls.append(("prefetch_done", query))
        self.prefetch_finished.set()
        return "provider context"

    def queue_prefetch(self, query, *, session_id: str = "") -> None:
        self.calls.append(("queue_prefetch", query))

    def sync_turn(
        self,
        user_content,
        assistant_content,
        *,
        session_id: str = "",
        messages=None,
    ) -> None:
        self.calls.append(("sync", user_content))
        if user_content == "active":
            self.sync_started.set()
            self.sync_release.wait(timeout=2)

    def recall_status(self):
        self.calls.append(("recall_status",))
        return None

    def get_tool_schemas(self):
        self.calls.append(("schemas",))
        return [
            {
                "name": "quarantine_probe_tool",
                "description": "probe",
                "parameters": {"type": "object", "properties": {}},
            }
        ]

    def handle_tool_call(self, tool_name, args, **kwargs) -> str:
        self.calls.append(("tool", tool_name))
        return "handled"

    def on_turn_start(self, turn_number, message, **kwargs):
        self.calls.append(("turn_start", turn_number))

    def on_session_end(self, messages):
        self.calls.append(("session_end",))

    def on_session_switch(self, new_session_id, **kwargs):
        self.calls.append(("session_switch", new_session_id))

    def on_pre_compress(self, messages):
        self.calls.append(("pre_compress",))
        return "checkpoint"

    def on_memory_write(self, action, target, content, metadata=None):
        self.calls.append(("memory_write", action))

    def on_delegation(self, task, result, **kwargs):
        self.calls.append(("delegation", task))

    def shutdown(self):
        self.calls.append(("shutdown",))


def _timeout_external_prefetch(manager, provider):
    assert manager.prefetch_all("timeout", session_id="s1") == ""
    assert provider.prefetch_started.wait(timeout=1)
    assert manager._provider_is_quarantined(provider) is True


def test_prefetch_timeout_quarantines_all_semantic_paths_but_not_shutdown():
    manager = MemoryManager(external_prefetch_timeout=0.05)
    provider = _QuarantineProbeProvider()
    manager.add_provider(provider)

    _timeout_external_prefetch(manager, provider)
    provider.prefetch_release.set()
    assert provider.prefetch_finished.wait(timeout=1)
    baseline = list(provider.calls)

    assert manager.build_system_prompt() == ""
    assert manager.describe_recall() == ""
    assert manager.get_all_tool_schemas() == []
    assert manager.get_all_tool_names() == set()
    assert manager.has_tool("quarantine_probe_tool") is False
    assert "quarantined" in manager.handle_tool_call(
        "quarantine_probe_tool", {}
    ).lower()
    manager.on_turn_start(1, "hello")
    manager.on_session_end([])
    manager.on_session_switch("s2")
    assert manager.supports_pre_compress_checkpoint() is False
    assert manager.on_pre_compress([]) == ""
    manager.on_memory_write("add", "memory", "value")
    manager.on_delegation("task", "result")
    manager.initialize_all("s2", hermes_home="/tmp")
    manager.sync_all("queued-sync", "response")
    manager.queue_prefetch_all("queued-prefetch")
    assert manager.flush_pending(timeout=2) is True

    assert provider.calls == baseline
    manager.shutdown_all()
    assert provider.calls == baseline + [("shutdown",)]


def test_work_queued_before_timeout_rechecks_quarantine_at_execution():
    manager = MemoryManager(external_prefetch_timeout=0.05)
    provider = _QuarantineProbeProvider()
    manager.add_provider(provider)

    manager.sync_all("active", "response")
    assert provider.sync_started.wait(timeout=1)
    manager.sync_all("queued", "response")
    manager.queue_prefetch_all("queued")

    _timeout_external_prefetch(manager, provider)
    provider.sync_release.set()
    assert manager.flush_pending(timeout=2) is True

    assert ("sync", "active") in provider.calls
    assert ("sync", "queued") not in provider.calls
    assert ("queue_prefetch", "queued") not in provider.calls

    provider.prefetch_release.set()
    assert provider.prefetch_finished.wait(timeout=1)
    manager.shutdown_all()


def test_late_completion_cannot_restore_provider_trust():
    manager = MemoryManager(external_prefetch_timeout=0.05)
    provider = _QuarantineProbeProvider()
    manager.add_provider(provider)

    _timeout_external_prefetch(manager, provider)
    provider.prefetch_release.set()
    assert provider.prefetch_finished.wait(timeout=1)

    before = list(provider.calls)
    assert manager.prefetch_all("after-late-completion") == ""
    assert manager.build_system_prompt() == ""
    assert provider.calls == before
    assert manager._provider_is_quarantined(provider) is True
    manager.shutdown_all()


def test_quarantine_is_scoped_to_owning_manager_lifetime():
    provider = _QuarantineProbeProvider()
    first = MemoryManager(external_prefetch_timeout=0.05)
    first.add_provider(provider)
    _timeout_external_prefetch(first, provider)
    provider.prefetch_release.set()
    assert provider.prefetch_finished.wait(timeout=1)

    second = MemoryManager(external_prefetch_timeout=0.5)
    second.add_provider(provider)
    assert second.prefetch_all("fresh-manager") == "provider context"
    before = list(provider.calls)
    assert first.prefetch_all("still-quarantined") == ""
    assert provider.calls == before

    first.shutdown_all()
    second.shutdown_all()


def test_timeout_finalization_is_monotonic():
    manager = MemoryManager()
    provider = _QuarantineProbeProvider()
    manager.add_provider(provider)

    lease, refusal = manager._begin_external_prefetch(provider)
    assert refusal == ""
    assert lease is not None
    manager._finalize_external_prefetch(lease, timed_out=True)

    # Simulate a completion arriving after the timeout owner finalized, then a
    # stale success finalization. Neither edge may restore trust.
    lease.completed.set()
    manager._finalize_external_prefetch(lease, timed_out=False)

    assert manager._provider_is_quarantined(provider) is True
    assert manager._provider_call_allowed(provider, "late") is False
    manager.shutdown_all()
'''
    tests += regression_tests

TEST_PATH.write_text(tests, encoding="utf-8")
