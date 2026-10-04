"""Hermes's own semantic rename of an auto-created thread keeps the thread's pinned prompt.

``chat_name`` is part of the pinned session-context prompt's key. The title lane renames a new
auto-thread once the LLM title arrives, normally before the user's second message, so turn 2 read
the new name and re-rendered the already-sent prompt: a prompt-cache miss on turn 2 of every
auto-threaded conversation on the default config. A rename by anyone else is a real metadata
change and still re-renders, including one that later restores Hermes's title: equal text is not
edit provenance.
"""

import asyncio
import sys
from datetime import datetime, timezone
from itertools import product
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import gateway.run as gateway_run
import plugins.platforms.discord.adapter as discord_platform
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import build_session_context
from plugins.platforms.discord.adapter import DiscordAdapter


class _Text:
    def __init__(self, channel_id, name="ops"):
        self.id, self.name, self.topic = channel_id, name, None
        self.guild = SimpleNamespace(id=1, name="Hermes Server")


class _Thread:
    def __init__(self, channel_id, parent, name):
        self.id, self.name, self.parent, self.parent_id = channel_id, name, parent, parent.id
        self.guild = parent.guild
        self.owner_id = 42
        self.archived = False

    def _update(self, data):
        self.name = data["name"]

    async def edit(self, *, name, reason=None):
        self.name = name


_USER = SimpleNamespace(id=42, display_name="Alice", name="alice")


def _message(channel, message_id):
    return SimpleNamespace(
        id=message_id, content="what broke?", mentions=[], attachments=[], reference=None,
        created_at=datetime.now(timezone.utc), channel=channel, author=_USER)


@pytest.fixture
def conversation(monkeypatch):
    monkeypatch.setattr(discord_platform.discord, "Thread", _Thread, raising=False)
    monkeypatch.setattr(discord_platform, "DISCORD_AVAILABLE", True)
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
    parent = _Text(700)
    thread = _Thread(800, parent, name="what broke?")
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=999), get_channel=lambda _id: thread)
    adapter._text_batch_delay_seconds = 0
    adapter._discord_history_backfill = lambda: False
    adapter._auto_create_thread = AsyncMock(return_value=thread)
    adapter.handle_message = AsyncMock()

    runner = object.__new__(gateway_run.GatewayRunner)
    config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="x")})

    async def turn(channel, message_id):
        await adapter._handle_message(_message(channel, message_id))
        source = adapter.handle_message.await_args.args[0].source
        assert source.chat_id == "800"
        return runner._pinned_session_context_prompt(build_session_context(source, config), False, "k")

    return adapter, parent, thread, turn


@pytest.fixture
def discord_sdk():
    # gateway/conftest.py supplies an SDK mock even when the extra is installed.
    # This contract needs the real parser and scheduling behavior, scoped here.
    with patch.dict(sys.modules):
        for name in tuple(sys.modules):
            if name == "discord" or name.startswith("discord."):
                del sys.modules[name]
        yield pytest.importorskip("discord", reason="requires the discord extra")


@pytest.mark.asyncio
@pytest.mark.parametrize("moderator_name", ["Renamed by a moderator", "what broke?"])
async def test_hermes_title_rename_keeps_the_pin_and_a_human_rename_does_not(conversation, moderator_name):
    adapter, parent, thread, turn = conversation
    first = await turn(parent, 100)
    # The title lane's call, as gateway/run_topics.py makes it for a native auto-thread.
    assert await adapter.rename_thread("800", "Database outage", only_if_current_name="what broke?")
    assert thread.name == "Database outage"
    # A message read through a cache that has not caught up with the edit still sees the old name.
    thread.name = "what broke?"
    assert await turn(thread, 104) == first
    thread.name = "Database outage"
    assert await turn(thread, 101) == first

    thread.name = moderator_name
    assert f"Hermes Server / #ops / {moderator_name}" in await turn(thread, 102)
    # The moderator then restores Hermes's title: it shows as itself, not as the opening name.
    thread.name = "Database outage"
    assert "Hermes Server / #ops / Database outage" in await turn(thread, 103)


async def _thread_update_retires_pin_without_a_title_turn(
    conversation, discord_sdk, during_edit, batch_updates, subscribed, moderator_name,
):
    """A native rename is authoritative even before a turn sees the title (#131614)."""
    from discord.state import ConnectionState

    adapter, parent, thread, turn = conversation
    adapter._platform_events_subscribed = lambda: subscribed
    adapter._platform_event_handler = AsyncMock()
    first = await turn(parent, 100)
    client = discord_sdk.Client(intents=discord_sdk.Intents.default())
    await client._async_setup_hook()

    @client.event
    async def on_raw_thread_update(payload):
        # Old heads have no raw callback. Native Discord dispatch ignores it there.
        handler = getattr(adapter, "_on_platform_raw_thread_update", None)
        if handler is not None:
            await handler(payload)

    @client.event
    async def on_thread_update(before, after):
        await adapter._on_platform_thread_update(before, after)

    guild = SimpleNamespace(get_thread=lambda _id: thread)
    state = SimpleNamespace(_get_guild=lambda _id: guild, dispatch=client.dispatch)

    async def update(name):
        # Real SDK parsing mutates the cached thread before scheduling callbacks.
        # Batched updates deliberately share ``after`` while raw data stays distinct.
        ConnectionState.parse_thread_update(state, {
            "id": str(thread.id), "guild_id": "1", "parent_id": str(parent.id),
            "type": 11, "name": name,
        })
        if not batch_updates:
            await asyncio.sleep(0)

    async def moderator_roundtrip():
        await update(moderator_name)
        await update("Database outage")
        await asyncio.sleep(0)

    async def edit(*, name, reason=None):
        # Discord delivers gateway updates independently of the REST response.
        await update(thread.name)  # unchanged-name metadata before the edit
        await update(name)
        if during_edit:
            await moderator_roundtrip()
        await asyncio.sleep(0)
        return _Thread(thread.id, parent, name)

    thread.edit = edit
    try:
        assert await adapter.rename_thread("800", "Database outage", only_if_current_name="what broke?")
        if not during_edit:
            # An unchanged-name metadata update must leave Hermes's alias intact.
            await update(thread.name)
            await asyncio.sleep(0)
            assert await turn(_Thread(thread.id, parent, "what broke?"), 101) == first
            await moderator_roundtrip()

        # No intervening message observed either name change. The moderator's restored
        # title must now render as itself; REST completion must not re-arm the alias.
        prompt = await turn(thread, 102)
        assert "Hermes Server / #ops / Database outage" in prompt
        assert prompt != first
        assert bool(adapter._platform_event_handler.await_count) == subscribed
    finally:
        await client.close()


async def _unfinished_rename_does_not_own_a_later_title(
    conversation, cancelled, newer_rename,
):
    """Failure/cancellation removes only the alias created by that REST attempt."""
    adapter, parent, thread, turn = conversation
    first = await turn(parent, 100)
    started, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def edit(*, name, reason=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await finish.wait()
            raise RuntimeError("REST edit failed")
        return _Thread(thread.id, parent, name)

    thread.edit = edit
    attempt = asyncio.create_task(adapter.rename_thread(
        "800", "Database outage", only_if_current_name="what broke?",
    ))
    await started.wait()
    # A later attempt for the same names has its own provenance.
    if newer_rename:
        assert await adapter.rename_thread("800", "Database outage", only_if_current_name="what broke?")
    if cancelled:
        attempt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attempt
    else:
        finish.set()
        assert not await attempt

    thread.name = "Database outage"
    prompt = await turn(thread, 101)
    if newer_rename:
        assert prompt == first
    else:
        assert "Hermes Server / #ops / Database outage" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    pytest.param(("update", *values), id="update-" + "-".join(map(str, values)))
    for values in product([False, True], [False, True], [False, True],
                          ["Renamed by a moderator", "what broke?"])
] + [
    pytest.param(("unfinished", *values), id="unfinished-" + "-".join(map(str, values)))
    for values in product([False, True], [False, True])
])
async def test_rename_attempt_owns_only_its_uninterrupted_title(conversation, request, case):
    """Moderator edits and failed attempts cannot restore an obsolete pin (#131614)."""
    kind, *values = case
    if kind == "update":
        await _thread_update_retires_pin_without_a_title_turn(
            conversation, request.getfixturevalue("discord_sdk"), *values,
        )
    else:
        await _unfinished_rename_does_not_own_a_later_title(conversation, *values)
