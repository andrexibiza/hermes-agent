from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "agent" / "auxiliary_client.py"
OWNER = ROOT / "agent" / "auxiliary_context_ceiling.py"
TEST = ROOT / "tests" / "agent" / "test_auxiliary_context_ceiling_shard.py"

OWNER_CONTENT = '"""Auxiliary-provider context-ceiling ownership.\n\nThis module owns the policy that binds one auxiliary invocation to its\nresolved effective context ceiling, enforces that ceiling at the Relay and\nprovider boundaries, and rebinds it for each physical fallback destination.\nThe adapter router keeps only compatibility imports and lifecycle wiring.\n"""\n\nfrom __future__ import annotations\n\nimport asyncio\nimport contextlib\nfrom collections.abc import Callable, MutableMapping\nfrom typing import Any, Protocol\n\nfrom agent import model_metadata as _model_metadata\n\n\nContextCeilingExceeded = _model_metadata.ContextCeilingExceeded\n\n\nclass _FallbackDestinationLike(Protocol):\n    provider: str\n    base_url: str\n    model: str | None\n\n\n_CeilingSetter = Callable[[int], Any]\n\n\ndef set_aux_ceiling(value: int) -> Any:\n    """Publish an auxiliary ceiling through the canonical ContextVar owner."""\n    return _model_metadata.set_aux_ceiling(value)\n\n\ndef get_aux_ceiling() -> int | None:\n    """Read the auxiliary ceiling from the canonical ContextVar owner."""\n    return _model_metadata.get_aux_ceiling()\n\n\ndef reset_aux_ceiling(token: Any) -> None:\n    """Restore the auxiliary ceiling represented by *token*."""\n    _model_metadata.reset_aux_ceiling(token)\n\n\ndef _aux_relay_gate(\n    kwargs: dict[str, Any],\n    task: str = "auxiliary",\n    *,\n    provider: str | None = None,\n    model: str | None = None,\n) -> None:\n    """Enforce the invocation ceiling on one provider-bound payload.\n\n    The Relay helpers are the shared physical I/O owner for auxiliary sync,\n    async, streaming, retry, and fallback dispatch. The invocation owner\n    publishes the resolved effective ceiling before entering those helpers;\n    this gate reads it and applies Hermes\' canonical final-payload budget.\n    """\n    ceiling = get_aux_ceiling()\n    if not (\n        isinstance(ceiling, int)\n        and not isinstance(ceiling, bool)\n        and ceiling > 0\n    ):\n        return\n    budget = _model_metadata.build_final_context_budget(\n        kwargs,\n        provider=provider,\n        model=model,\n    )\n    _model_metadata.enforce_final_context_budget(\n        budget,\n        ceiling=ceiling,\n        reason=task,\n    )\n\n\ndef _aux_provider_callback(\n    callback: Callable[[dict[str, Any]], Any],\n    *,\n    gate: Callable[..., None] = _aux_relay_gate,\n    task: str = "auxiliary",\n    provider: str | None = None,\n    model: str | None = None,\n) -> Callable[[dict[str, Any]], Any]:\n    """Wrap the true provider callback with final-payload enforcement.\n\n    Relay or middleware may enlarge the caller-built request after the Relay\n    entry gate. This wrapper repeats enforcement at the exact provider seam,\n    before the physical callback can run. ``gate`` is injectable so the legacy\n    ``agent.auxiliary_client._aux_relay_gate`` monkeypatch seam remains live.\n    """\n\n    def _gated(request: dict[str, Any]) -> Any:\n        gate(request, task=task, provider=provider, model=model)\n        return callback(request)\n\n    _gated.__name__ = getattr(callback, "__name__", "provider_callback")\n    _gated.__doc__ = callback.__doc__\n    return _gated\n\n\ndef _publish_aux_ceiling(\n    scope: MutableMapping[str, Any] | None,\n    *,\n    model: str,\n    base_url: str,\n    provider: str,\n    api_key: str = "",\n    set_ceiling: _CeilingSetter = set_aux_ceiling,\n) -> int | None:\n    """Resolve and publish one invocation-specific effective ceiling.\n\n    The returned value is also used by the one streaming bypass that does not\n    enter the Relay helpers. The ContextVar token is retained in ``scope`` so\n    the invocation owner can reset it in its existing ``finally`` block.\n    """\n    try:\n        ceiling = _model_metadata.effective_context_length(\n            model=model,\n            base_url=base_url,\n            api_key=api_key,\n            provider=provider,\n        )\n    except Exception:\n        ceiling = None\n    if ceiling is None:\n        return None\n    token = set_ceiling(ceiling)\n    if scope is not None:\n        scope["token"] = token\n    return ceiling\n\n\ndef _enforce_aux_ceiling_request(\n    kwargs: dict[str, Any],\n    *,\n    ceiling: int | None,\n    reason: str,\n    provider: str | None = None,\n    model: str | None = None,\n) -> None:\n    """Enforce an already-resolved ceiling for a physical dispatch bypass."""\n    if ceiling is None:\n        return\n    budget = _model_metadata.build_final_context_budget(\n        kwargs,\n        provider=provider,\n        model=model,\n    )\n    _model_metadata.enforce_final_context_budget(\n        budget,\n        ceiling=ceiling,\n        reason=reason,\n    )\n\n\ndef _rebind_aux_ceiling_for_fallback(\n    destination: _FallbackDestinationLike,\n    *,\n    api_key: str = "",\n):\n    """Scope the ambient ceiling to one physical fallback destination.\n\n    Each fallback has its own provider, model, base URL, and potentially its\n    own credential-aware capability. The prior invocation ceiling is restored\n    after the attempt, including refusal and failure paths.\n    """\n    model = destination.model or ""\n    base_url = destination.base_url or ""\n    try:\n        ceiling = _model_metadata.effective_context_length(\n            model=model,\n            base_url=base_url,\n            api_key=api_key or "",\n            provider=destination.provider or "",\n        )\n    except Exception:\n        ceiling = None\n    if ceiling is None:\n        return contextlib.nullcontext()\n    token = set_aux_ceiling(ceiling)\n\n    @contextlib.contextmanager\n    def _scoped():\n        try:\n            yield ceiling\n        finally:\n            reset_aux_ceiling(token)\n\n    return _scoped()\n\n\ndef _rebind_aux_ceiling_for_fallback_async(\n    destination: _FallbackDestinationLike,\n    *,\n    api_key: str = "",\n):\n    """Async fallback rebinding with capability resolution off the event loop."""\n    model = destination.model or ""\n    base_url = destination.base_url or ""\n\n    @contextlib.asynccontextmanager\n    async def _scoped():\n        try:\n            ceiling = await asyncio.to_thread(\n                _model_metadata.effective_context_length,\n                model=model,\n                base_url=base_url,\n                api_key=api_key or "",\n                provider=destination.provider or "",\n            )\n        except Exception:\n            ceiling = None\n        if not (\n            isinstance(ceiling, int)\n            and not isinstance(ceiling, bool)\n            and ceiling > 0\n        ):\n            yield None\n            return\n        token = set_aux_ceiling(ceiling)\n        try:\n            yield ceiling\n        finally:\n            reset_aux_ceiling(token)\n\n    return _scoped()\n\n\n__all__ = [\n    "ContextCeilingExceeded",\n    "_aux_provider_callback",\n    "_aux_relay_gate",\n    "_enforce_aux_ceiling_request",\n    "_publish_aux_ceiling",\n    "_rebind_aux_ceiling_for_fallback",\n    "_rebind_aux_ceiling_for_fallback_async",\n    "get_aux_ceiling",\n    "reset_aux_ceiling",\n    "set_aux_ceiling",\n]\n'
TEST_CONTENT = '"""Architecture and compatibility receipts for the auxiliary ceiling shard."""\n\nfrom __future__ import annotations\n\nimport ast\nfrom pathlib import Path\nfrom typing import Any\n\nfrom agent import auxiliary_client as _aux\nfrom agent import auxiliary_context_ceiling as _ceiling\n\n\n_REPO_ROOT = Path(__file__).resolve().parents[2]\n_AUXILIARY_CLIENT = _REPO_ROOT / "agent" / "auxiliary_client.py"\n_CEILING_OWNER = _REPO_ROOT / "agent" / "auxiliary_context_ceiling.py"\n\n\ndef _top_level_definitions(path: Path) -> set[str]:\n    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))\n    return {\n        node.name\n        for node in tree.body\n        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))\n    }\n\n\ndef test_ceiling_policy_definitions_live_in_extracted_owner() -> None:\n    router_definitions = _top_level_definitions(_AUXILIARY_CLIENT)\n    owner_definitions = _top_level_definitions(_CEILING_OWNER)\n\n    assert "_aux_relay_gate" not in router_definitions\n    assert "_rebind_aux_ceiling_for_fallback" not in router_definitions\n    assert "_rebind_aux_ceiling_for_fallback_async" not in router_definitions\n    assert "_aux_relay_gate" in owner_definitions\n    assert "_rebind_aux_ceiling_for_fallback" in owner_definitions\n    assert "_rebind_aux_ceiling_for_fallback_async" in owner_definitions\n    assert len(_CEILING_OWNER.read_text(encoding="utf-8").splitlines()) < 2_000\n\n\ndef test_legacy_auxiliary_client_symbols_reexport_extracted_owner() -> None:\n    assert _aux._aux_relay_gate is _ceiling._aux_relay_gate\n    assert (\n        _aux._rebind_aux_ceiling_for_fallback\n        is _ceiling._rebind_aux_ceiling_for_fallback\n    )\n    assert (\n        _aux._rebind_aux_ceiling_for_fallback_async\n        is _ceiling._rebind_aux_ceiling_for_fallback_async\n    )\n    assert _aux.ContextCeilingExceeded is _ceiling.ContextCeilingExceeded\n\n\ndef test_provider_callback_preserves_legacy_gate_monkeypatch_seam(\n    monkeypatch,\n) -> None:\n    events: list[tuple[str, Any]] = []\n\n    def gate(\n        request: dict[str, Any],\n        task: str = "auxiliary",\n        *,\n        provider: str | None = None,\n        model: str | None = None,\n    ) -> None:\n        events.append(("gate", (request, task, provider, model)))\n\n    def callback(request: dict[str, Any]) -> str:\n        events.append(("callback", request))\n        return "ok"\n\n    monkeypatch.setattr(_aux, "_aux_relay_gate", gate)\n    wrapped = _aux._aux_provider_callback(\n        callback,\n        task="compression",\n        provider="openai",\n        model="test-model",\n    )\n    request = {"messages": [{"role": "user", "content": "hello"}]}\n\n    assert wrapped(request) == "ok"\n    assert events == [\n        ("gate", (request, "compression", "openai", "test-model")),\n        ("callback", request),\n    ]\n\n\ndef test_publication_uses_live_model_metadata_resolver(monkeypatch) -> None:\n    calls: list[dict[str, str]] = []\n    token = object()\n    scope: dict[str, Any] = {"token": None}\n\n    def effective_context_length(**kwargs: str) -> int:\n        calls.append(kwargs)\n        return 123_456\n\n    monkeypatch.setattr(\n        _ceiling._model_metadata,\n        "effective_context_length",\n        effective_context_length,\n    )\n\n    value = _ceiling._publish_aux_ceiling(\n        scope,\n        model="test-model",\n        base_url="https://api.test/v1",\n        provider="openai",\n        api_key="sk-test",\n        set_ceiling=lambda ceiling: token if ceiling == 123_456 else None,\n    )\n\n    assert value == 123_456\n    assert scope["token"] is token\n    assert calls == [\n        {\n            "model": "test-model",\n            "base_url": "https://api.test/v1",\n            "api_key": "sk-test",\n            "provider": "openai",\n        }\n    ]\n'



def _require_once(text: str, marker: str) -> int:
    count = text.count(marker)
    if count != 1:
        raise RuntimeError(f"expected exactly one marker {marker!r}, found {count}")
    return text.index(marker)


def _replace_exact(text: str, old: str, new: str) -> str:
    _require_once(text, old)
    return text.replace(old, new, 1)


def _replace_between(text: str, start: str, end: str, replacement: str) -> str:
    start_index = _require_once(text, start)
    end_index = _require_once(text, end)
    if end_index <= start_index:
        raise RuntimeError(f"marker order is invalid: {start!r} then {end!r}")
    return text[:start_index] + replacement + text[end_index:]


def _replace_inclusive(text: str, start: str, end: str, replacement: str) -> str:
    start_index = _require_once(text, start)
    end_index = _require_once(text, end)
    if end_index <= start_index:
        raise RuntimeError(f"marker order is invalid: {start!r} then {end!r}")
    end_index += len(end)
    return text[:start_index] + replacement + text[end_index:]


before = ROUTER.read_text(encoding="utf-8")
text = before

old_imports = '''from agent.credential_pool import load_pool
from agent.model_metadata import (
    MINIMUM_CONTEXT_LENGTH,
    ContextCeilingExceeded,
    get_model_context_length,
    strip_codex_context_variant_suffix as _strip_codex_ctx_variant,
    # Ceiling contextvar API — re-exported so tests / callers can publish the
    # auxiliary effective ceiling and the relay-helper gates read it.
    set_aux_ceiling,
    get_aux_ceiling,
    reset_aux_ceiling,
)
'''
new_imports = '''from agent.auxiliary_context_ceiling import (
    ContextCeilingExceeded,
    _aux_provider_callback as _make_aux_provider_callback,
    _aux_relay_gate,
    _enforce_aux_ceiling_request,
    _publish_aux_ceiling,
    _rebind_aux_ceiling_for_fallback,
    _rebind_aux_ceiling_for_fallback_async,
    get_aux_ceiling,
    reset_aux_ceiling,
    set_aux_ceiling,
)
from agent.credential_pool import load_pool
from agent.model_metadata import (
    MINIMUM_CONTEXT_LENGTH,
    get_model_context_length,
    strip_codex_context_variant_suffix as _strip_codex_ctx_variant,
)
'''
text = _replace_exact(text, old_imports, new_imports)

relay_wrapper = '''def _aux_provider_callback(
    callback: Callable[[dict[str, Any]], Any],
    *,
    task: str = "auxiliary",
    provider: str | None = None,
    model: str | None = None,
) -> Callable[[dict[str, Any]], Any]:
    "Preserve the legacy adapter seam while delegating ceiling ownership."
    return _make_aux_provider_callback(
        callback,
        gate=_aux_relay_gate,
        task=task,
        provider=provider,
        model=model,
    )


'''
text = _replace_between(
    text,
    "def _aux_relay_gate(\n",
    "def _relay_sync_completion(\n",
    relay_wrapper,
)

text = _replace_between(
    text,
    "def _rebind_aux_ceiling_for_fallback(\n",
    "def _fallback_destination(\n",
    "",
)

sync_publication = '''    _aux_limit = _publish_aux_ceiling(
        ceiling_scope,
        model=str(final_model or ""),
        base_url=str(resolved_base_url or _base_info or ""),
        provider=str(request_provider or ""),
    )

'''
text = _replace_between(
    text,
    "    # ── Hard-ceiling gate for ALL auxiliary dispatch ──────────────────\n",
    "    # Streaming path: return the raw SDK Stream iterator directly. This is used by\n",
    sync_publication,
)

stream_bypass_end = "            return client.chat.completions.create(**kwargs)\n"
stream_bypass = '''            _enforce_aux_ceiling_request(
                kwargs,
                ceiling=_aux_limit,
                reason=f"auxiliary {task or 'call'}",
                provider=str(request_provider or "") or None,
                model=str(final_model or "") or None,
            )
            return client.chat.completions.create(**kwargs)
'''
text = _replace_inclusive(
    text,
    "            # ── Ceiling gate (aggregator streaming bypass) ─────────────\n",
    stream_bypass_end,
    stream_bypass,
)

async_publication = '''    _aux_limit_async = _publish_aux_ceiling(
        ceiling_scope,
        model=str(final_model or ""),
        base_url=str(
            resolved_base_url or getattr(client, "base_url", "") or ""
        ),
        provider=str(request_provider or ""),
    )

'''
text = _replace_between(
    text,
    "    # ── Auxiliary effective-ceiling publication (mirrors the sync owner) ─────\n",
    "    # Pass the client's actual base_url (not just resolved_base_url) so\n",
    async_publication,
)

OWNER.parent.mkdir(parents=True, exist_ok=True)
TEST.parent.mkdir(parents=True, exist_ok=True)
OWNER.write_text(OWNER_CONTENT, encoding="utf-8")
TEST.write_text(TEST_CONTENT, encoding="utf-8")
ROUTER.write_text(text, encoding="utf-8")

router_tree = ast.parse(text, filename=str(ROUTER))
owner_tree = ast.parse(OWNER_CONTENT, filename=str(OWNER))
ast.parse(TEST_CONTENT, filename=str(TEST))

router_defs = {
    node.name
    for node in router_tree.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
}
owner_defs = {
    node.name
    for node in owner_tree.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
}

for moved in (
    "_aux_relay_gate",
    "_rebind_aux_ceiling_for_fallback",
    "_rebind_aux_ceiling_for_fallback_async",
):
    if moved in router_defs:
        raise RuntimeError(f"{moved} still has a definition in the adapter router")
    if moved not in owner_defs:
        raise RuntimeError(f"{moved} is missing from the extracted owner")

if "_aux_provider_callback" not in router_defs:
    raise RuntimeError("legacy provider-callback compatibility wrapper is missing")
if "from agent.auxiliary_context_ceiling import (" not in text:
    raise RuntimeError("router does not import the extracted ceiling owner")
if len(OWNER_CONTENT.splitlines()) >= 2_000:
    raise RuntimeError("extracted owner exceeds the repository's 2K godfile ceiling")
removed_lines = len(before.splitlines()) - len(text.splitlines())
if removed_lines < 200:
    raise RuntimeError(f"shard removed only {removed_lines} router lines")

print(
    f"sharded auxiliary context-ceiling policy: "
    f"router -{removed_lines} lines; owner {len(OWNER_CONTENT.splitlines())} lines"
)
