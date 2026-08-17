"""Freemaxxing forward proxy — stdlib-only, OpenAI-compatible, model-aware pool.

The provider plugin registers metadata at import time, but this module does not
bind a socket until ``spawn_proxy`` is called.

Endpoints:
  GET  /healthz                  -> per-backend health + pool state
  GET  /v1/healthz               -> compatibility health endpoint for base_url probes
  GET  /v1/models                -> one opaque Freemaxxing router model
  POST /v1/chat/completions      -> model-aware routing + failover
"""

from __future__ import annotations

import atexit
import hmac
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("freemaxxing.proxy")


def _open_credentialed(req, *, timeout: float):
    """Open a credentialed request without forwarding credentials across origins."""
    try:
        from hermes_cli.urllib_security import open_credentialed_url

        return open_credentialed_url(req, timeout=timeout)
    except ImportError:
        return urllib.request.urlopen(req, timeout=timeout)


class RateLimitError(Exception):
    def __init__(self, message: str, retry_after: float = 30.0):
        super().__init__(message)
        self.retry_after = retry_after


class TransientError(Exception):
    """5xx / timeout / connection reset — short cooldown, retry elsewhere."""


class ModelNotFoundError(Exception):
    """Backend does not know the requested model. Skip, do not cooldown."""


class AuthError(Exception):
    """401/403 — backend is not usable with this key. Do not cooldown."""


class ClientRequestError(Exception):
    """Other 4xx — malformed request. Fail clearly, do not retry elsewhere."""


class Backend:
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str = "",
        tier: int = 0,
        refresh=None,
        default_model: str = "",
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.tier = tier
        self.cooldown_until: float = 0.0
        self.last_success: float = 0.0
        self.cached_models: Optional[List[str]] = None
        self.cached_models_until: float = 0.0
        self.refresh_lock = threading.Lock()
        self.last_error_class: Optional[str] = None
        self.refresh = refresh
        self.default_model = default_model

    def is_available(self) -> bool:
        return time.time() >= self.cooldown_until

    def record_success(self) -> None:
        self.last_success = time.time()
        self.last_error_class = None
        self.cooldown_until = 0.0

    def record_failure(
        self, retry_after: float = 30.0, error_class: str = "transient"
    ) -> None:
        self.cooldown_until = max(self.cooldown_until, time.time() + retry_after)
        self.last_error_class = error_class

    def get_cached_models(self) -> List[str]:
        if self.cached_models is not None and time.time() < self.cached_models_until:
            return self.cached_models
        return []

    def set_cached_models(self, models: List[str], ttl: float = 60.0) -> None:
        self.cached_models = models
        self.cached_models_until = time.time() + ttl

    def supports_model(self, model: str) -> bool:
        if not model:
            return True
        cached = self.get_cached_models()
        if not cached:
            return True
        return model in cached

    def health(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "tier": self.tier,
            "available": self.is_available(),
            "cooldown_until": self.cooldown_until,
            "last_success": self.last_success,
            "last_error_class": self.last_error_class,
            "models_cached": self.cached_models is not None,
            "models_cached_until": self.cached_models_until,
        }


class BackendPool:
    def __init__(self):
        self.backends: List[Backend] = []
        self._index = 0
        self._lock = threading.Lock()
        self._catalog_refresh_lock = threading.Lock()
        self._catalog_refresh_until = 0.0

    def add(self, backend: Backend) -> None:
        with self._lock:
            self.backends.append(backend)

    def clear(self) -> None:
        with self._lock:
            self.backends.clear()
            self._index = 0
            self._catalog_refresh_until = 0.0

    def _pick_round_robin(self, candidates: List[Backend]) -> Optional[Backend]:
        if not candidates:
            return None
        ids = {id(b) for b in candidates}
        n = len(self.backends)
        for _ in range(n):
            b = self.backends[self._index]
            self._index = (self._index + 1) % n
            if id(b) in ids:
                return b
        return candidates[0]

    def next(
        self, requested_model: str = "", exclude: Optional[Set[str]] = None
    ) -> Optional[Backend]:
        """Choose an untried backend with strict tier precedence.

        Within the lowest eligible tier, selection round-robins. A higher tier
        is considered only when the lower tier has no eligible backend for the
        current request.
        """
        excluded = exclude or set()
        with self._lock:
            available = [
                b
                for b in self.backends
                if b.is_available() and b.name not in excluded
            ]
            if not available:
                return None

            if not _is_router_model(requested_model):
                supporters = [b for b in available if b.supports_model(requested_model)]
                candidates = supporters or available
            else:
                candidates = available

            min_tier = min(b.tier for b in candidates)
            tier_candidates = [b for b in candidates if b.tier == min_tier]
            return self._pick_round_robin(tier_candidates)

    def get_aggregated_models(self) -> List[Dict[str, str]]:
        """Expose exactly the Freemaxxing router alias.

        Backend catalogs are refreshed at most once per TTL. Refresh is
        single-flight and happens outside the pool lock.
        """
        now = time.time()
        if now >= self._catalog_refresh_until and self._catalog_refresh_lock.acquire(
            blocking=False
        ):
            try:
                with self._lock:
                    backends = list(self.backends)
                for b in backends:
                    try:
                        b.set_cached_models(self._fetch_models(b))
                    except Exception as exc:
                        logger.debug(
                            "freemaxxing: catalog refresh failed for %s: %s",
                            b.name,
                            exc,
                        )
                self._catalog_refresh_until = time.time() + 60.0
            finally:
                self._catalog_refresh_lock.release()

        return [
            {
                "id": "freemaxxing",
                "object": "model",
                "owned_by": "freemaxxing",
            }
        ]

    def _fetch_models(self, backend: Backend) -> List[str]:
        url = backend.base_url + "/models"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": _hermes_user_agent(),
            },
        )
        if backend.api_key:
            req.add_header("Authorization", f"Bearer {backend.api_key}")
        try:
            with _open_credentialed(req, timeout=8.0) as resp:
                data = json.loads(resp.read().decode())
                items = data if isinstance(data, list) else data.get("data", [])
                return [
                    m["id"]
                    for m in items
                    if isinstance(m, dict)
                    and "id" in m
                    and _accept_catalog_id(backend, str(m["id"]))
                ]
        except Exception as exc:
            logger.debug(
                "freemaxxing: model fetch failed for %s: %s", backend.name, exc
            )
            return []

    def exhaustion_detail(self) -> str:
        with self._lock:
            if not self.backends:
                return "no backends configured"
            cooling = [
                f"{b.name}({b.last_error_class or 'cooldown'})"
                for b in self.backends
                if not b.is_available()
            ]
            if len(cooling) == len(self.backends):
                return "all backends on cooldown: " + ", ".join(cooling)
            return "no eligible backend"

    def health(self) -> Dict[str, Any]:
        with self._lock:
            return {"backends": [b.health() for b in self.backends]}


pool = BackendPool()

_ROUTER_MODELS = {"freemaxxing", "fm", "freemaxxing-auto"}
_PREFERRED_AUTO_MODELS = (
    "deepseek/deepseek-v4-flash-0731",
    "deepseek-v4-flash-0731",
    "deepseek/deepseek-v4-flash",
    "deepseek-v4-flash",
)


def _is_router_model(model: str) -> bool:
    return (model or "").strip().lower() in _ROUTER_MODELS


def _backend_kind(backend: Backend) -> str:
    return (backend.name or "").strip().lower()


def _accept_catalog_id(backend: Backend, model_id: str) -> bool:
    if not model_id:
        return False
    mid = model_id.lower()
    if mid.endswith(":batch") or mid.startswith("~"):
        return False
    kind = _backend_kind(backend)
    if kind == "openrouter" or kind.startswith("openrouter"):
        return mid.endswith(":free")
    return True


def _resolve_auto_model(backend: Backend) -> str:
    cached = backend.get_cached_models()
    if not cached:
        try:
            cached = pool._fetch_models(backend)
            backend.set_cached_models(cached)
        except Exception as exc:
            logger.debug(
                "freemaxxing: auto-model fetch failed for %s: %s",
                backend.name,
                exc,
            )
            cached = []

    cached_by_lower = {m.lower(): m for m in cached}
    preferred: List[str] = []
    if backend.default_model:
        preferred.append(backend.default_model)
    preferred.extend(_PREFERRED_AUTO_MODELS)
    for candidate in preferred:
        hit = cached_by_lower.get(candidate.lower())
        if hit:
            return hit

    if cached:
        small = [
            m
            for m in cached
            if any(
                k in m.lower()
                for k in ("flash", "mini", "nano", "small", "lite")
            )
        ]
        for candidate in small + cached:
            low = candidate.lower()
            if (
                low in _ROUTER_MODELS
                or low.endswith(":batch")
                or low.startswith("~")
            ):
                continue
            return candidate

    if backend.default_model:
        return backend.default_model
    return ""


_MAX_BODY = 1_000_000


def _hermes_user_agent() -> str:
    try:
        from hermes_cli import __version__ as ver

        return f"hermes-cli/{ver}"
    except Exception:
        return "hermes-cli"


def _expected_token(handler: BaseHTTPRequestHandler) -> str:
    return str(getattr(handler.server, "auth_token", "") or "")


def _authorized(handler: BaseHTTPRequestHandler) -> bool:
    """Authenticate production listeners.

    A blank server token is supported only for standalone unit tests that call
    ``spawn_proxy(port=0)`` directly. The bundled provider always passes a
    cryptographically random non-empty token.
    """
    expected = _expected_token(handler)
    if not expected:
        return True
    supplied = handler.headers.get("Authorization", "")
    return hmac.compare_digest(supplied, f"Bearer {expected}")


class ChatCompletionsHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        logger.debug("%s - %s", self.client_address[0], format % args)

    def _send_json(self, code: int, body: Any) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error(
        self, code: int, message: str, error_type: str = "freemaxxing_error"
    ) -> None:
        self._send_json(
            code, {"error": {"message": message, "type": error_type}}
        )

    def _require_auth(self) -> bool:
        if _authorized(self):
            return True
        self._send_error(401, "Unauthorized", error_type="unauthorized")
        return False

    def do_GET(self) -> None:
        path = self.path.split("?")[0]

        # model_switch probes base_url + "/healthz", where base_url ends in /v1,
        # and historically does not attach provider credentials. Keep that
        # liveness probe intentionally non-sensitive: it exposes only a service
        # marker, never backend state or credentials.
        if path == "/v1/healthz":
            self._send_json(200, {"service": "freemaxxing", "status": "ok"})
            return

        # The detailed health surface is credentialed because backend names,
        # cooldowns, and failure classes are operationally sensitive.
        if path == "/healthz":
            if not self._require_auth():
                return
            self._send_json(
                200,
                {
                    "service": "freemaxxing",
                    "health": pool.health(),
                },
            )
            return

        if path == "/v1/models":
            if not self._require_auth():
                return
            self._send_json(
                200,
                {"object": "list", "data": pool.get_aggregated_models()},
            )
            return

        self._send_error(404, f"Unknown path: {path}")

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        if path != "/v1/chat/completions":
            self._send_error(404, f"Unknown path: {path}")
            return
        if not self._require_auth():
            return
        self._handle_chat_completions()

    def _read_body(self) -> Optional[Dict[str, Any]]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > _MAX_BODY:
                self._send_error(
                    413 if content_length > _MAX_BODY else 400,
                    "Invalid Content-Length",
                )
                return None
            parsed = json.loads(self.rfile.read(content_length))
            if not isinstance(parsed, dict):
                self._send_error(400, "JSON body must be an object")
                return None
            return parsed
        except (json.JSONDecodeError, ValueError):
            self._send_error(400, "Invalid JSON body")
            return None

    def _handle_chat_completions(self) -> None:
        body = self._read_body()
        if body is None:
            return
        model = body.get("model", "")
        stream = bool(body.get("stream", False))
        if stream:
            self._handle_streaming(body, model)
            return
        self._handle_nonstreaming(body, model)

    def _handle_nonstreaming(
        self, body: Dict[str, Any], model: str
    ) -> None:
        tried: Set[str] = set()
        last_error: Optional[str] = None

        while True:
            backend = pool.next(model, exclude=tried)
            if backend is None:
                break
            tried.add(backend.name)
            try:
                response = _forward(backend, body)
                self._send_json(200, response)
                backend.record_success()
                logger.info(
                    "freemaxxing: model=%s selected=%s tier=%d attempted=%d",
                    model,
                    backend.name,
                    backend.tier,
                    len(tried),
                )
                return
            except RateLimitError as exc:
                backend.record_failure(
                    retry_after=exc.retry_after, error_class="rate_limit"
                )
                last_error = (
                    f"rate limited on {backend.name} "
                    f"(retry after {exc.retry_after:.0f}s)"
                )
                logger.warning("freemaxxing: %s", last_error)
            except ModelNotFoundError as exc:
                last_error = str(exc)
                logger.info("freemaxxing: %s — skipping", last_error)
            except AuthError as exc:
                last_error = str(exc)
                logger.error(
                    "freemaxxing: %s — backend auth broken", last_error
                )
            except ClientRequestError as exc:
                self._send_error(
                    400, str(exc), error_type="invalid_request"
                )
                return
            except TransientError as exc:
                backend.record_failure(
                    retry_after=10.0, error_class="transient"
                )
                last_error = f"{backend.name} transient failure"
                logger.warning("freemaxxing: %s", last_error)
            except Exception:
                logger.exception(
                    "freemaxxing: internal failure while using backend %s",
                    backend.name,
                )
                self._send_error(
                    500,
                    "Freemaxxing internal proxy error",
                    error_type="internal_error",
                )
                return

        self._send_error(503, _exhausted_message(last_error))

    def _handle_streaming(
        self, body: Dict[str, Any], model: str
    ) -> None:
        tried: Set[str] = set()
        last_error: Optional[str] = None

        while True:
            backend = pool.next(model, exclude=tried)
            if backend is None:
                break
            tried.add(backend.name)
            try:
                resp = _open_stream(backend, body)
            except RateLimitError as exc:
                backend.record_failure(
                    retry_after=exc.retry_after, error_class="rate_limit"
                )
                last_error = f"rate limited on {backend.name}"
                logger.warning("freemaxxing: %s", last_error)
                continue
            except ModelNotFoundError as exc:
                last_error = str(exc)
                logger.info("freemaxxing: %s — skipping", last_error)
                continue
            except AuthError as exc:
                last_error = str(exc)
                logger.error("freemaxxing: %s", last_error)
                continue
            except ClientRequestError as exc:
                self._send_error(
                    400, str(exc), error_type="invalid_request"
                )
                return
            except TransientError:
                backend.record_failure(
                    retry_after=10.0, error_class="transient"
                )
                last_error = f"{backend.name} transient failure"
                logger.warning("freemaxxing: %s", last_error)
                continue
            except Exception:
                logger.exception(
                    "freemaxxing: internal stream-open failure on %s",
                    backend.name,
                )
                self._send_error(
                    500,
                    "Freemaxxing internal proxy error",
                    error_type="internal_error",
                )
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            completed = False
            interrupted = False
            try:
                # SSE is line-oriented. readline() lets small events reach the
                # client immediately rather than waiting for an 8 KiB buffer.
                while True:
                    chunk = resp.readline()
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    if b"data: [DONE]" in chunk:
                        completed = True
                if completed:
                    backend.record_success()
                else:
                    interrupted = True
            except Exception as exc:
                interrupted = True
                logger.warning(
                    "freemaxxing: stream from %s interrupted: %s",
                    backend.name,
                    exc,
                )
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

            if interrupted:
                backend.record_failure(
                    retry_after=10.0, error_class="stream_interrupted"
                )
            else:
                logger.info(
                    "freemaxxing: model=%s selected=%s tier=%d (streaming)",
                    model,
                    backend.name,
                    backend.tier,
                )
            return

        self._send_error(503, _exhausted_message(last_error))


def _exhausted_message(last_error: Optional[str]) -> str:
    detail = last_error or pool.exhaustion_detail()
    return f"All backends exhausted. Last error: {detail}"


def _forward(backend: Backend, body: Dict[str, Any]) -> Dict[str, Any]:
    resp = _open_response(backend, body)
    with resp:
        return json.loads(resp.read().decode())


def _open_stream(backend: Backend, body: Dict[str, Any]):
    return _open_response(backend, body)


def _open_response(backend: Backend, body: Dict[str, Any]):
    def _attempt(base_url: str, api_key: str):
        url = base_url.rstrip("/") + "/chat/completions"
        outgoing = body
        if _is_router_model(str(body.get("model", ""))):
            real_model = _resolve_auto_model(backend)
            if real_model:
                outgoing = dict(body)
                outgoing["model"] = real_model
            else:
                raise ModelNotFoundError(
                    f"backend {backend.name} has no free model in catalog"
                )

        req = urllib.request.Request(
            url,
            data=json.dumps(outgoing).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": _hermes_user_agent(),
            },
            method="POST",
        )
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")

        try:
            return _open_credentialed(req, timeout=120.0)
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code == 429:
                raise RateLimitError(
                    f"backend {backend.name} rate-limited",
                    _parse_retry_after(exc.headers),
                )
            if code in (401, 403):
                raise AuthError(
                    f"backend {backend.name} auth rejected (HTTP {code})"
                )
            if code == 404:
                raise ModelNotFoundError(
                    f"backend {backend.name} does not serve model (HTTP 404)"
                )
            if 400 <= code < 500:
                raise ClientRequestError(
                    f"backend {backend.name} rejected request (HTTP {code})"
                )
            raise TransientError(
                f"backend {backend.name} returned HTTP {code}"
            )
        except urllib.error.URLError as exc:
            raise TransientError(
                f"backend {backend.name} unreachable: {exc.reason}"
            )
        except TimeoutError:
            raise TransientError(f"backend {backend.name} timed out")

    try:
        if not backend.api_key and backend.refresh is not None:
            with backend.refresh_lock:
                if not backend.api_key:
                    try:
                        new_base, new_key = backend.refresh()
                        if new_key:
                            backend.base_url = new_base
                            backend.api_key = new_key
                    except Exception as exc:
                        logger.debug(
                            "freemaxxing: %s pre-request refresh failed: %s",
                            backend.name,
                            exc,
                        )
        return _attempt(backend.base_url, backend.api_key)
    except AuthError:
        if backend.refresh is None:
            raise
        before = backend.api_key
        with backend.refresh_lock:
            if backend.api_key != before:
                return _attempt(backend.base_url, backend.api_key)
            try:
                new_base, new_key = backend.refresh()
                if new_key and new_key != backend.api_key:
                    backend.base_url = new_base
                    backend.api_key = new_key
                    return _attempt(new_base, new_key)
            except Exception as exc:
                logger.warning(
                    "freemaxxing: %s credential refresh failed: %s",
                    backend.name,
                    exc,
                )
        raise


def _parse_retry_after(headers) -> float:
    raw = headers.get("Retry-After")
    if raw is None:
        return 30.0
    try:
        value = float(raw)
    except (ValueError, TypeError):
        return 30.0
    if not math.isfinite(value):
        return 30.0
    return min(max(value, 0.0), 300.0)


_proxy_server: Optional[ThreadingHTTPServer] = None
_proxy_lock = threading.Lock()


def spawn_proxy(
    *, port: int = 0, token: str = ""
) -> ThreadingHTTPServer:
    """Start the proxy exactly once.

    ``token`` is required by the bundled provider. A blank token remains
    available for the standalone unit-test harness.
    """
    global _proxy_server
    with _proxy_lock:
        if _proxy_server is not None:
            actual = int(_proxy_server.server_address[1])
            if port and port != actual:
                logger.warning(
                    "freemaxxing: existing proxy uses port %d; requested %d",
                    actual,
                    port,
                )
            existing_token = str(
                getattr(_proxy_server, "auth_token", "") or ""
            )
            if token and existing_token and not hmac.compare_digest(
                token, existing_token
            ):
                raise RuntimeError(
                    "freemaxxing proxy already running with a different "
                    "local authentication token"
                )
            return _proxy_server

        server = ThreadingHTTPServer(
            ("127.0.0.1", port), ChatCompletionsHandler
        )
        server.auth_token = token
        thread = threading.Thread(
            target=server.serve_forever,
            name="freemaxxing-proxy",
            daemon=True,
        )
        thread.start()
        _proxy_server = server
        logger.info(
            "freemaxxing: proxy listening on 127.0.0.1:%d",
            server.server_address[1],
        )
        return server


def stop_proxy(server: Optional[ThreadingHTTPServer] = None) -> None:
    global _proxy_server
    target = server or _proxy_server
    if target is None:
        return
    try:
        target.shutdown()
        target.server_close()
    except Exception as exc:
        logger.debug("freemaxxing: proxy shutdown error: %s", exc)
    if target is _proxy_server:
        _proxy_server = None


atexit.register(lambda: stop_proxy())
