"""TUI gateway MCP OAuth loopback relay — RFC 9207 ``iss`` survival.

There is zero coverage of ``tui_gateway.mcp_oauth_sessions`` in the tree;
this file drives the real loopback listener with a real
``DashboardOAuthFlow`` and pins the RFC 9207 authorization-response issuer
surviving the relay (#88698 R4).
"""

import asyncio
import http.client

import pytest


def test_loopback_listener_forwards_iss():
    from tui_gateway.mcp_oauth_sessions import (
        _shutdown_listener,
        _start_loopback_listener,
    )
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow

    flow = DashboardOAuthFlow(
        flow_id="tui-iss",
        server_name="reports",
        profile=None,
        hermes_home="/tmp/hermes-test",
        redirect_uri="http://127.0.0.1:1/callback",
    )
    asyncio.run(flow.publish_authorization_url("https://idp.example/authorize?state=s1"))

    rec = {"httpd": None}
    try:
        server = _start_loopback_listener(flow)
        rec["httpd"] = server
        port = server.server_address[1]

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "GET",
            "/callback?code=tui-code&state=s1&iss=https%3A%2F%2Fidp.example",
        )
        resp = conn.getresponse()
        assert resp.status == 200
        resp.read()
        conn.close()

        # The 3-tuple (code, state, iss) with iss intact — the shape the
        # waiter's dashboard branch feeds into AuthorizationCodeResult.
        assert asyncio.run(flow.wait_for_callback()) == (
            "tui-code",
            "s1",
            "https://idp.example",
        )
    finally:
        _shutdown_listener(rec)


def test_loopback_listener_without_iss_records_none():
    from tui_gateway.mcp_oauth_sessions import (
        _shutdown_listener,
        _start_loopback_listener,
    )
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow

    flow = DashboardOAuthFlow(
        flow_id="tui-no-iss",
        server_name="reports",
        profile=None,
        hermes_home="/tmp/hermes-test",
        redirect_uri="http://127.0.0.1:1/callback",
    )
    asyncio.run(flow.publish_authorization_url("https://idp.example/authorize?state=s1"))

    rec = {"httpd": None}
    try:
        server = _start_loopback_listener(flow)
        rec["httpd"] = server
        port = server.server_address[1]

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/callback?code=tui-code&state=s1")
        resp = conn.getresponse()
        assert resp.status == 200
        resp.read()
        conn.close()

        assert asyncio.run(flow.wait_for_callback()) == ("tui-code", "s1", None)
    finally:
        _shutdown_listener(rec)


def test_loopback_listener_rejects_state_mismatch():
    from tui_gateway.mcp_oauth_sessions import (
        _shutdown_listener,
        _start_loopback_listener,
    )
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow

    flow = DashboardOAuthFlow(
        flow_id="tui-bad-state",
        server_name="reports",
        profile=None,
        hermes_home="/tmp/hermes-test",
        redirect_uri="http://127.0.0.1:1/callback",
    )
    asyncio.run(flow.publish_authorization_url("https://idp.example/authorize?state=s1"))

    rec = {"httpd": None}
    try:
        server = _start_loopback_listener(flow)
        rec["httpd"] = server
        port = server.server_address[1]

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/callback?code=tui-code&state=WRONG")
        resp = conn.getresponse()
        assert resp.status == 400
        resp.read()
        conn.close()
    finally:
        _shutdown_listener(rec)
