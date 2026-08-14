"""Unit tests for the Freemaxxing proxy (bundled model provider).

Covers the v0.1 acceptance bar:
  - model-affinity routing (backend that advertises the model is preferred)
  - 429 failover + cooldown
  - 503 failover (seamless, no error to the caller)
  - model-not-found skip (no long cooldown)
  - non-retriable 4xx does NOT poison backend health
  - empty pool -> 503
  - catalog aggregation + provenance
  - round-robin among capable backends
  - cooldown expiry + recovered-backend reuse
  - malformed Retry-After
  - streaming pass-through + streaming 503 failover before commit
  - concurrent requests (ThreadingHTTPServer + pool lock)
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The proxy is a standalone module inside the bundled plugin directory. Resolve
# it relative to this test file regardless of the pytest working directory.
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
_PLUGIN_DIR = os.path.join(_REPO_ROOT, "plugins", "model-providers", "freemaxxing")
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from proxy import Backend, pool, spawn_proxy, stop_proxy  # noqa: E402


class MockBackend:
    """A mock OpenAI-compatible backend bound to an ephemeral port."""

    def __init__(self, *, models=None, status_code=200, body=None,
                 retry_after=None, stream_chunks=None):
        self.models = models or ["test-model"]
        self.status_code = status_code
        self.body = body or self._default_body()
        self.retry_after = retry_after
        self.stream_chunks = stream_chunks
        self.request_count = 0
        self.last_body = None

        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/models":
                    data = json.dumps(
                        {"object": "list",
                         "data": [{"id": m, "object": "model", "owned_by": "mock"} for m in outer.models]}
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                outer.request_count += 1
                length = int(self.headers.get("Content-Length", "0"))
                outer.last_body = json.loads(self.rfile.read(length))

                if outer.stream_chunks is not None:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for chunk in outer.stream_chunks:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    # Explicit end-of-body signal: tell the HTTP/1.1 handler to
                    # close the connection after this response, so a client
                    # waiting on read(8192) gets EOF instead of hanging.
                    self.close_connection = True
                    return

                if outer.status_code == 429 and outer.retry_after is not None:
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Retry-After", outer.retry_after)
                    data = b'{"error": {"message": "rate limited"}}'
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return

                data = json.dumps(outer.body).encode()
                self.send_response(outer.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _default_body(self):
        return {
            "id": "msg-1",
            "object": "chat.completion",
            "model": "test-model",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "hello from mock"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _add(backend, name="b", tier=0):
    b = Backend(name=name, base_url=backend.base_url(), api_key="", tier=tier)
    pool.add(b)
    return b


def _pool_backend(name):
    for b in pool.backends:
        if b.name == name:
            return b
    return None


def _post(proxy_port, model="test-model", stream=False, extra=None):
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    if stream:
        body["stream"] = True
    if extra:
        body.update(extra)
    req = urllib.request.Request(
        f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=15.0)


def _setup():
    pool.clear()
    proxy = spawn_proxy(port=0)
    return proxy, proxy.server_address[1]


def _teardown(proxy, backends):
    stop_proxy(proxy)
    for b in backends:
        b.stop()
    pool.clear()


def test_empty_pool_returns_503():
    proxy, port = _setup()
    try:
        try:
            _post(port)
            assert False, "expected 503"
        except urllib.error.HTTPError as e:
            assert e.code == 503
            assert "exhausted" in e.read().decode()
    finally:
        _teardown(proxy, [])


def test_failover_on_429():
    proxy, port = _setup()
    b1 = MockBackend(status_code=429, retry_after="1")
    b2 = MockBackend()
    _add(b1, "b1", 0)
    _add(b2, "b2", 0)
    try:
        with _post(port) as resp:
            data = json.loads(resp.read().decode())
            assert data["choices"][0]["message"]["content"] == "hello from mock"
        assert b1.request_count == 1
        assert b2.request_count == 1
        assert not _pool_backend("b1").is_available()
    finally:
        _teardown(proxy, [b1, b2])


def test_503_fails_over_seamlessly():
    proxy, port = _setup()
    b1 = MockBackend(status_code=503, body={"error": {"message": "overloaded"}})
    b2 = MockBackend()
    _add(b1, "b1", 0)
    _add(b2, "b2", 0)
    try:
        with _post(port) as resp:
            data = json.loads(resp.read().decode())
            assert data["choices"][0]["message"]["content"] == "hello from mock"
        assert b1.request_count == 1
        assert b2.request_count == 1
        assert not _pool_backend("b1").is_available()
    finally:
        _teardown(proxy, [b1, b2])


def test_all_backends_503_returns_503():
    proxy, port = _setup()
    b1 = MockBackend(status_code=503, body={"error": {"message": "overloaded"}})
    b2 = MockBackend(status_code=503, body={"error": {"message": "overloaded"}})
    _add(b1, "b1", 0)
    _add(b2, "b2", 0)
    try:
        try:
            _post(port)
            assert False, "expected 503"
        except urllib.error.HTTPError as e:
            assert e.code == 503
    finally:
        _teardown(proxy, [b1, b2])


def test_model_not_found_skips_without_cooldown():
    proxy, port = _setup()
    b1 = MockBackend(status_code=404, body={"error": {"message": "model not found"}})
    b2 = MockBackend()
    _add(b1, "b1", 0)
    _add(b2, "b2", 0)
    try:
        with _post(port) as resp:
            data = json.loads(resp.read().decode())
            assert data["choices"][0]["message"]["content"] == "hello from mock"
        assert _pool_backend("b1").is_available()
        assert b2.request_count == 1
    finally:
        _teardown(proxy, [b1, b2])


def test_401_does_not_poison_backend():
    proxy, port = _setup()
    b1 = MockBackend(status_code=401, body={"error": {"message": "bad key"}})
    b2 = MockBackend()
    _add(b1, "b1", 0)
    _add(b2, "b2", 0)
    try:
        with _post(port) as resp:
            data = json.loads(resp.read().decode())
            assert data["choices"][0]["message"]["content"] == "hello from mock"
        assert _pool_backend("b1").is_available()
    finally:
        _teardown(proxy, [b1, b2])


def test_400_fails_clearly_no_retry():
    proxy, port = _setup()
    b1 = MockBackend(status_code=400, body={"error": {"message": "bad max_tokens"}})
    b2 = MockBackend()
    _add(b1, "b1", 0)
    _add(b2, "b2", 0)
    try:
        try:
            _post(port)
            assert False, "expected 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
            assert b2.request_count == 0
    finally:
        _teardown(proxy, [b1, b2])


def test_401_triggers_refresh_and_retry():
    proxy, port = _setup()
    b1 = MockBackend(status_code=401, body={"error": {"message": "expired"}})
    state = {"refreshed": False}

    def _refresh():
        state["refreshed"] = True
        b1.status_code = 200
        b1.body = b1._default_body()
        return (b1.base_url(), "new-key")

    pool.add(Backend(name="b1", base_url=b1.base_url(), api_key="old-key",
                     tier=0, refresh=_refresh))
    try:
        with _post(port) as resp:
            data = json.loads(resp.read().decode())
            assert data["choices"][0]["message"]["content"] == "hello from mock"
        assert state["refreshed"] is True
        assert b1.request_count == 2
    finally:
        _teardown(proxy, [b1])


def test_model_affinity_prefers_advertising_backend():
    proxy, port = _setup()
    b1 = MockBackend(models=["other-model"])
    b2 = MockBackend(models=["test-model"])
    _add(b1, "b1", 0)
    _add(b2, "b2", 0)
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models").read()
        with _post(port, model="test-model") as resp:
            resp.read()
        assert b2.request_count == 1
        assert b1.request_count == 0
    finally:
        _teardown(proxy, [b1, b2])


def test_round_robin_among_capable_backends():
    proxy, port = _setup()
    b1 = MockBackend()
    b2 = MockBackend()
    _add(b1, "b1", 0)
    _add(b2, "b2", 0)
    try:
        with _post(port) as resp:
            resp.read()
        with _post(port) as resp:
            resp.read()
        assert b1.request_count == 1
        assert b2.request_count == 1
    finally:
        _teardown(proxy, [b1, b2])


def test_catalog_aggregation_and_provenance():
    proxy, port = _setup()
    b1 = MockBackend(models=["model-a"])
    b2 = MockBackend(models=["model-b"])
    _add(b1, "b1", 0)
    _add(b2, "b2", 0)
    try:
        data = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/v1/models", timeout=10.0
        ).read().decode())
        ids = {m["id"]: m["owned_by"] for m in data["data"]}
        assert ids["model-a"] == "b1"
        assert ids["model-b"] == "b2"
    finally:
        _teardown(proxy, [b1, b2])


def test_cooldown_expiry():
    proxy, port = _setup()
    b1 = MockBackend(status_code=429, retry_after="1")
    b2 = MockBackend()
    _add(b1, "b1", 0)
    _add(b2, "b2", 0)
    try:
        with _post(port) as resp:
            resp.read()
        assert not _pool_backend("b1").is_available()
        time.sleep(1.2)
        assert _pool_backend("b1").is_available()
    finally:
        _teardown(proxy, [b1, b2])


def test_malformed_retry_after():
    proxy, port = _setup()
    b1 = MockBackend(status_code=429, retry_after="not-a-number")
    b2 = MockBackend()
    _add(b1, "b1", 0)
    _add(b2, "b2", 0)
    try:
        with _post(port) as resp:
            resp.read()
        assert b2.request_count == 1
        assert not _pool_backend("b1").is_available()
    finally:
        _teardown(proxy, [b1, b2])


def test_streaming_passthrough():
    proxy, port = _setup()
    chunks = [
        b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    b1 = MockBackend(stream_chunks=chunks)
    _add(b1, "b1", 0)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=json.dumps({"model": "test-model", "messages": [{"role": "user", "content": "hi"}], "stream": True}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            raw = resp.read()
            assert b"hello" in raw
            assert b"[DONE]" in raw
    finally:
        _teardown(proxy, [b1])


def test_streaming_503_fails_over_before_commit():
    proxy, port = _setup()
    b1 = MockBackend(status_code=503, body={"error": {"message": "overloaded"}})
    chunks = [b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n', b'data: [DONE]\n\n']
    b2 = MockBackend(stream_chunks=chunks)
    _add(b1, "b1", 0)
    _add(b2, "b2", 0)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=json.dumps({"model": "test-model", "messages": [{"role": "user", "content": "hi"}], "stream": True}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            raw = resp.read()
            assert b"ok" in raw
        assert b1.request_count == 1
        assert b2.request_count == 1
    finally:
        _teardown(proxy, [b1, b2])


def test_full_request_passthrough():
    proxy, port = _setup()
    b1 = MockBackend()
    _add(b1, "b1", 0)
    try:
        extra = {"tools": [{"type": "function", "function": {"name": "f"}}],
                 "response_format": {"type": "json_object"},
                 "seed": 42, "stop": ["\n"], "top_p": 0.9}
        with _post(port, extra=extra) as resp:
            resp.read()
        sent = b1.last_body
        assert sent["tools"][0]["function"]["name"] == "f"
        assert sent["response_format"]["type"] == "json_object"
        assert sent["seed"] == 42
        assert sent["top_p"] == 0.9
    finally:
        _teardown(proxy, [b1])


def test_concurrent_requests_round_robin_without_corruption():
    proxy, port = _setup()
    b1 = MockBackend()
    b2 = MockBackend()
    _add(b1, "b1", 0)
    _add(b2, "b2", 0)
    results = []
    lock = threading.Lock()

    def worker(i):
        try:
            with _post(port) as resp:
                d = json.loads(resp.read().decode())
                with lock:
                    results.append(d["choices"][0]["message"]["content"])
        except Exception as e:
            with lock:
                results.append(f"ERR:{e}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert len(results) == 20
        assert all(r == "hello from mock" for r in results), results
        assert b1.request_count > 0
        assert b2.request_count > 0
    finally:
        _teardown(proxy, [b1, b2])


def test_recovered_backend_served_again_after_cooldown():
    proxy, port = _setup()
    b1 = MockBackend(status_code=503, body={"error": {"message": "overloaded"}})
    b2 = MockBackend()
    _add(b1, "b1", 0)
    _add(b2, "b2", 0)
    try:
        with _post(port) as resp:
            resp.read()
        assert not _pool_backend("b1").is_available()
        _pool_backend("b1").cooldown_until = 0.0
        b1.status_code = 200
        b1.body = b1._default_body()
        with _post(port) as resp:
            data = json.loads(resp.read().decode())
            assert data["choices"][0]["message"]["content"] == "hello from mock"
        assert b1.request_count >= 2
    finally:
        _teardown(proxy, [b1, b2])


def test_healthz_endpoint():
    proxy, port = _setup()
    b1 = MockBackend()
    _add(b1, "b1", 0)
    try:
        data = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/healthz", timeout=10.0
        ).read().decode())
        assert len(data["backends"]) == 1
        assert data["backends"][0]["name"] == "b1"
        assert data["backends"][0]["available"] is True
    finally:
        _teardown(proxy, [b1])
