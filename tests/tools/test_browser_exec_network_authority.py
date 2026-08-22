"""Adversarial regressions for browser_exec network authority invariants."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

import tools.browser_exec_egress_guard as egress
import tools.browser_use_guard as guard


NONCE = "authority-test-nonce"


@pytest.fixture
def child_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    guard_dir = egress._egress_guard_dir()
    egress._verify_or_regenerate(guard_dir)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(guard_dir) + os.pathsep + env.get("PYTHONPATH", "")
    env["HERMES_BROWSER_EXEC_EGRESS_GUARD"] = "1"
    env["HERMES_BROWSER_EXEC_EGRESS_GUARD_NONCE"] = NONCE
    env["HERMES_BROWSER_EXEC_EGRESS_POLICY"] = json.dumps(
        egress._policy_snapshot(nonce=NONCE)
    )
    return env


def _run_child(body: str, env: dict):
    source = (
        "import browser_exec_egress_guard as g\n"
        "g.install()\n"
        + body
    )
    return subprocess.run(
        [sys.executable, "-S", "-c", source],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def test_execution_request_cannot_widen_configured_block(monkeypatch):
    import tools.url_safety as url_safety

    monkeypatch.setattr(url_safety, "_global_allow_private_urls", lambda: False)
    assert egress._policy_snapshot(allow_private=True)["allow_private"] is False

    monkeypatch.setattr(url_safety, "_global_allow_private_urls", lambda: True)
    assert egress._policy_snapshot(allow_private=True)["allow_private"] is True
    assert egress._policy_snapshot(allow_private=False)["allow_private"] is False


def test_ipv4_compatible_metadata_floor_survives_allow_private(child_env):
    policy = json.loads(child_env["HERMES_BROWSER_EXEC_EGRESS_POLICY"])
    policy["allow_private"] = True
    child_env["HERMES_BROWSER_EXEC_EGRESS_POLICY"] = json.dumps(policy)

    proc = _run_child(
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('::a9fe:a9fe', 80), timeout=0.1)\n"
        "    print('NOT-BLOCKED')\n"
        "except OSError as exc:\n"
        "    print('BLOCKED', 'egress guard blocked' in str(exc))\n",
        child_env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "BLOCKED True" in proc.stdout


def test_replacing_checker_does_not_replace_installed_authority(child_env):
    proc = _run_child(
        "import socket\n"
        "g.__dict__['_check_and_resolve'] = lambda _sock, address: address\n"
        "try:\n"
        "    socket.create_connection(('169.254.169.254', 80), timeout=0.1)\n"
        "    print('NOT-BLOCKED')\n"
        "except OSError as exc:\n"
        "    print('BLOCKED', 'egress guard blocked' in str(exc))\n",
        child_env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "BLOCKED True" in proc.stdout


def test_exit_receipt_cannot_self_certify_replaced_binding(child_env):
    proc = _run_child(
        "import socket\n"
        "replacement = lambda *args, **kwargs: None\n"
        "g.__dict__['_g_create_connection'] = replacement\n"
        "socket.create_connection = replacement\n",
        child_env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "__HERMES_EGRESS_GUARD__:tamper:binding" in proc.stderr


def test_datagram_dials_checked_sockaddr_not_original_hostname(child_env):
    proc = _run_child(
        "import socket\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "prims = g.__dict__['_PRIMS']\n"
        "resolved = []\n"
        "sent = []\n"
        "def fake_getaddrinfo(host, port, *args):\n"
        "    resolved.append((host, port))\n"
        "    return [(socket.AF_INET, socket.SOCK_DGRAM, 17, '', ('8.8.8.8', port))]\n"
        "def fake_sendto(self, data, *args):\n"
        "    sent.append(args[-1])\n"
        "    return len(data)\n"
        "prims.getaddrinfo = fake_getaddrinfo\n"
        "prims.sendto = fake_sendto\n"
        "sock.sendto(b'x', ('rebind.invalid', 53))\n"
        "print('RESOLVED', resolved)\n"
        "print('SENT', sent)\n",
        child_env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "RESOLVED [('rebind.invalid', 53)]" in proc.stdout
    assert "SENT [('8.8.8.8', 53)]" in proc.stdout


def test_proxy_http_dials_only_policy_approved_literal(monkeypatch):
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            observed.append((self.path, self.headers.get("Host")))
            payload = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    import tools.url_safety as url_safety

    monkeypatch.setattr(
        url_safety,
        "resolve_and_check_url",
        lambda url: SimpleNamespace(
            ok=True,
            reason="ok",
            resolved_ips=("127.0.0.1",),
        ),
    )
    original_getaddrinfo = socket.getaddrinfo

    def no_rebind_lookup(host, *args, **kwargs):
        if host == "rebind.invalid":
            raise AssertionError("authorized hostname was re-resolved")
        return original_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", no_rebind_lookup)
    try:
        status, _reason, _headers, payload = egress._request_with_checked_redirects(
            "GET",
            f"http://rebind.invalid:{port}/x",
            {},
            None,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert status == 200
    assert payload == b"ok"
    assert observed == [("/x", f"rebind.invalid:{port}")]


def test_proxy_reauthorizes_redirect_before_second_dial(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header(
                "Location",
                "http://169.254.169.254/latest/meta-data/",
            )
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    import tools.url_safety as url_safety

    checked = []

    def verdict(url):
        checked.append(url)
        if "169.254.169.254" in url:
            return SimpleNamespace(
                ok=False,
                reason="blocked:metadata-ip",
                resolved_ips=(),
            )
        return SimpleNamespace(
            ok=True,
            reason="ok",
            resolved_ips=("127.0.0.1",),
        )

    monkeypatch.setattr(url_safety, "resolve_and_check_url", verdict)
    try:
        with pytest.raises(egress._EgressPolicyBlocked):
            egress._request_with_checked_redirects(
                "GET",
                f"http://redirect.invalid:{port}/",
                {},
                None,
            )
    finally:
        server.shutdown()
        server.server_close()

    assert len(checked) == 2
    assert "169.254.169.254" in checked[1]


def test_connect_tunnel_dials_resolved_ip_object(monkeypatch):
    handler = object.__new__(egress._EgressFilterProxyHandler)
    handler.path = "rebind.invalid:443"
    handler.connection = object()
    handler.send_response = lambda *args, **kwargs: None
    handler.send_header = lambda *args, **kwargs: None
    handler.end_headers = lambda *args, **kwargs: None

    dialed = []
    upstream = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(
        egress,
        "_resolve_checked_target",
        lambda url: (urlsplit_for_test(url), ("8.8.8.8",)),
    )
    monkeypatch.setattr(
        egress,
        "_dial_checked_ips",
        lambda ips, port, timeout: dialed.append((tuple(ips), port)) or upstream,
    )
    monkeypatch.setattr(egress, "_pump", lambda client, peer: None)

    handler.do_CONNECT()
    assert dialed == [(('8.8.8.8',), 443)]


def urlsplit_for_test(url):
    from urllib.parse import urlsplit

    return urlsplit(url)


class _Sink:
    def write(self, _value):
        return None

    def close(self):
        return None


class _ExitedProcess:
    def __init__(self):
        self.stdin = _Sink()
        self.stdout = []
        self.stderr = []
        self.returncode = 0

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.returncode = -9


def test_late_remote_ip_marker_survives_child_exit(monkeypatch):
    monkeypatch.setattr(guard.subprocess, "Popen", lambda *args, **kwargs: _ExitedProcess())

    blocked = threading.Event()
    marker_lock = threading.Lock()
    markers = []
    ssrf_guard = {
        "blocked": blocked,
        "died": threading.Event(),
        "report_closed": threading.Event(),
        "marker_lock": marker_lock,
        "markers": markers,
    }
    ctx = {
        "config": {"grace_s": 0.25},
        "ssrf_guard": ssrf_guard,
    }

    def emit_after_exit():
        time.sleep(0.05)
        with marker_lock:
            markers.append(
                guard.SSRF_BLOCK_MARKER
                + "http://169.254.169.254/latest/meta-data/"
            )
        blocked.set()

    threading.Thread(target=emit_after_exit, daemon=True).start()
    started = time.monotonic()
    run = guard._run_guarded_cli(
        [sys.executable, "-"],
        dict(os.environ),
        "print('done')\n",
        {},
        1.0,
        ctx,
        preamble=False,
    )
    elapsed = time.monotonic() - started

    assert run["returncode"] == 0
    assert run["guard_blocked"] is True
    assert run["ssrf_markers"]
    assert elapsed >= 0.04


def test_durable_marker_withholds_even_if_boolean_snapshot_is_false():
    marker = guard.SSRF_BLOCK_MARKER + "http://10.0.0.1/private"
    ctx = {
        "config": {},
        "monitor": None,
        "ssrf_guard": {"markers": [marker]},
    }
    run = {
        "egress_reason": None,
        "guard_blocked": False,
        "guard_died": False,
        "markers": {"armed": None, "announce": None},
        "ssrf_markers": (),
    }
    verdict = guard._guard_endstate_verdict(ctx, None, run)
    assert verdict["verdict"] == "withhold"
    assert "10.0.0.1" in verdict["reason"]
