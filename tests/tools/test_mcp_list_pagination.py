"""Tests for MCP list_* pagination (nextCursor draining).

The MCP spec allows servers to paginate ``tools/list``, ``resources/list``,
and ``prompts/list`` via an opaque ``nextCursor`` token. The Python SDK
fetches one page per call, so hermes must follow the cursor to see items
past page 1. Port of the invariant behind anomalyco/opencode#35439/#35500.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from tools.mcp_tool import _MCP_LIST_MAX_PAGES, _paginate_full_list


def _tool(name):
    t = MagicMock()
    t.name = name
    return t


class TestPaginateFullList:
    def test_single_page_no_cursor(self):
        """A result without nextCursor returns just that page."""
        list_method = AsyncMock(
            return_value=SimpleNamespace(tools=[_tool("a"), _tool("b")])
        )
        items = asyncio.run(_paginate_full_list(list_method, "tools", "srv"))
        assert [t.name for t in items] == ["a", "b"]
        list_method.assert_called_once_with()


    def test_runaway_cursor_capped(self):
        """A server that returns a cursor forever is bounded by the page cap."""
        calls = {"n": 0}

        async def evil_list(cursor=None):
            calls["n"] += 1
            return SimpleNamespace(
                tools=[_tool(f"t{calls['n']}")], nextCursor=f"c{calls['n']}"
            )

        items = asyncio.run(_paginate_full_list(evil_list, "tools", "srv"))
        assert calls["n"] == _MCP_LIST_MAX_PAGES
        assert len(items) == _MCP_LIST_MAX_PAGES


class TestCacheMetaAggregation:
    """#88698 R3: SEP-2549 hints aggregate conservatively across ALL pages."""

    def _page(self, ttl=None, scope=None, cursor=None):
        return SimpleNamespace(
            tools=[_tool("a")],
            ttl_ms=ttl,
            cache_scope=scope,
            nextCursor=cursor,
        )

    def test_ttl_aggregates_min_across_pages(self):
        meta = {}
        pages = {
            None: self._page(ttl=60_000, cursor="p2"),
            "p2": self._page(ttl=5_000),
        }

        async def list_method(cursor=None):
            return pages[cursor]

        asyncio.run(
            _paginate_full_list(list_method, "tools", "srv", cache_meta_out=meta)
        )
        assert meta["ttl_ms"] == 5_000

    def test_zero_ttl_wins(self):
        meta = {}
        pages = {
            None: self._page(ttl=60_000, cursor="p2"),
            "p2": self._page(ttl=0),
        }

        async def list_method(cursor=None):
            return pages[cursor]

        asyncio.run(
            _paginate_full_list(list_method, "tools", "srv", cache_meta_out=meta)
        )
        assert meta["ttl_ms"] == 0

    def test_page1_ttl_still_captured(self):
        # Regression for the old ``not items`` path: a single page's hint
        # must still be recorded.
        meta = {}

        async def list_method(cursor=None):
            return self._page(ttl=60_000)

        asyncio.run(
            _paginate_full_list(list_method, "tools", "srv", cache_meta_out=meta)
        )
        assert meta["ttl_ms"] == 60_000

    def test_numeric_string_ttl_coerced(self):
        meta = {}

        async def list_method(cursor=None):
            return self._page(ttl="5000")

        asyncio.run(
            _paginate_full_list(list_method, "tools", "srv", cache_meta_out=meta)
        )
        assert meta["ttl_ms"] == 5000.0

        meta2 = {}

        async def list_method2(cursor=None):
            return self._page(ttl="soon")

        asyncio.run(
            _paginate_full_list(list_method2, "tools", "srv", cache_meta_out=meta2)
        )
        assert "ttl_ms" not in meta2

    def test_scope_conflict_fails_closed(self):
        meta = {}
        pages = {
            None: self._page(ttl=60_000, scope="public", cursor="p2"),
            "p2": self._page(ttl=60_000, scope="private"),
        }

        async def list_method(cursor=None):
            return pages[cursor]

        asyncio.run(
            _paginate_full_list(list_method, "tools", "srv", cache_meta_out=meta)
        )
        assert meta["cache_scope"] == "private"

        meta2 = {}

        async def list_method2(cursor=None):
            return self._page(ttl=60_000, scope="public")

        asyncio.run(
            _paginate_full_list(list_method2, "tools", "srv", cache_meta_out=meta2)
        )
        assert meta2["cache_scope"] == "public"

    def test_scope_captured_from_any_page(self):
        # Fixes the page-1-missing case: the hint may arrive on page 2.
        meta = {}
        pages = {
            None: self._page(ttl=60_000, cursor="p2"),
            "p2": self._page(ttl=60_000, scope="public"),
        }

        async def list_method(cursor=None):
            return pages[cursor]

        asyncio.run(
            _paginate_full_list(list_method, "tools", "srv", cache_meta_out=meta)
        )
        assert meta["cache_scope"] == "public"

    def test_no_cache_meta_out_is_noop(self):
        pages = {
            None: self._page(ttl=60_000, cursor="p2"),
            "p2": self._page(ttl=5_000),
        }

        async def list_method(cursor=None):
            return pages[cursor]

        items = asyncio.run(_paginate_full_list(list_method, "tools", "srv"))
        assert len(items) == 2


class TestDiscoveryUsesPagination:
    def test_discover_tools_drains_all_pages(self):
        """MCPServerTask._discover_tools registers tools from every page."""
        from tools.mcp_tool import MCPServerTask

        server = MCPServerTask("pag_srv")
        server._config = {"command": "test"}
        pages = {
            None: SimpleNamespace(tools=[_tool("first")], nextCursor="page-2"),
            "page-2": SimpleNamespace(tools=[_tool("second")]),
        }

        async def fake_list(cursor=None):
            return pages[cursor]

        server.session = MagicMock()
        server.session.list_tools = fake_list
        # capability gate: _advertises_tools() returns True when no
        # capability info was captured (legacy fallback), so no override
        # is needed here.

        asyncio.run(server._discover_tools())
        assert [t.name for t in server._tools] == ["first", "second"]
