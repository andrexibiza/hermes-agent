"""Freemaxxing forward proxy — stdlib-only, OpenAI-compatible, model-aware pool.

Spawned as a background thread by __init__.py (module-level side effect).
Endpoints:
  GET  /healthz                  → per-backend health + pool state
  GET  /v1/models                → aggregated catalog with provenance
  POST /v1/chat/completions      → model-aware routing + failover; streaming
                                   is passed through (Hermes streams by default)
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("freemaxxing.proxy")


def _open_credentialed(req, *, timeout: float):
    """Open a credentialed request without forwarding credentials across origins.

    Preferred path is ``hermes_cli.urllib_security.open_credentialed_url`` (the
    redirect-safe opener that strips auth on cross-origin 30x), matching how
    ``providers/base.py`` fetches credentialed URLs. Falls back to plain
    ``urlopen`` only when that helper is unavailable (standalone proxy/test use
    outside a Hermes install). The request carries Authorization, so never use a
    raw redirect-following opener when the security module is importable.
    """
    try:
        from hermes_cli.urllib_security import open_credentialed_url

        return open_credentialed_url(req, timeout=timeout)
    except ImportError:
        return urllib.request.urlopen(req, timeout=timeout)


# ── Errors ───────────────────────────────────────────────────────────────────
# Only these are cooldown-worthy (transient / recoverable):
#   RateLimitError  -> 429, honor Retry-After
#   TransientError  -> 5xx, connection reset, DNS, timeout
# Non-retriable failures (auth, model mismatch, malformed request) surface
# clearly and do NOT poison backend health.


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


# ── Backend ──────────────────────────────────────────────────────────────────

class Backend:
    def __init__(self, name: str, base_url: str, api_key: str = "", tier: int = 0,
                 refresh=None, default_model: str = ""):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.tier = tier
        self.cooldown_until: float = 0.0
        self.last_success: float = 0.0
        self.cached_models: Optional[List[str]] = None
        self.cached_models_until: float = 0.0
        # Serializes the refresh-and-assign of (base_url, api_key) so concurrent
        # handler threads cannot interleave a new key with an old base URL.
        self.refresh_lock = threading.Lock()
        self.last_error_class: Optional[str] = None
        # Optional credential refresher: callable() -> (base_url, api_key).
        # Set for backends whose auth rotates (Nous inference JWT). Invoked on
        # 401/403 so a long-lived gateway re-resolves instead of wedging on the
        # first expired token.
        self.refresh = refresh
        # The real model to use when the request asks for the freemaxxing
        # router alias (see _ROUTER_MODELS) — i.e. "auto, best available".
        self.default_model = default_model

    def is_available(self) -> bool:
        return time.time() >= self.cooldown_until

    def record_success(self) -> None:
        self.last_success = time.time()
        self.last_error_class = None
        self.cooldown_until = 0.0  # a live success clears any lingering cooldown

    def record_failure(self, retry_after: float = 30.0, error_class: str = "transient") -> None:
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
            return True  # no preference
        cached = self.get_cached_models()
        if not cached:
            return True  # unknown — allow trial
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


# ── Pool ─────────────────────────────────────────────────────────────────────

class BackendPool:
    def __init__(self):
        self.backends: List[Backend] = []
        self._index = 0
        self._lock = threading.Lock()
        self._global_models_cache: Optional[List[Dict[str, str]]] = None
        self._global_models_until: float = 0.0

    def add(self, backend: Backend) -> None:
        with self._lock:
            self.backends.append(backend)
            self._global_models_cache = None

    def clear(self) -> None:
        with self._lock:
            self.backends.clear()
            self._global_models_cache = None
            self._index = 0

    def next(self, requested_model: str = "") -> Optional[Backend]:
        """Prefer backends that advertise the model; otherwise round-robin among available.

        The freemaxxing router alias matches no backend catalog, so it bypasses
        model-affinity and round-robins all available backends directly.
        """
        with self._lock:
            if not self.backends:
                return None
            n = len(self.backends)
            if _is_router_model(requested_model):
                # Router model: any available backend, round-robin.
                start = self._index
                for _ in range(n):
                    b = self.backends[self._index]
                    self._index = (self._index + 1) % n
                    if b.is_available():
                        return b
                    if self._index == start:
                        break
                return None
            # First pass: model supporters that are available
            for _ in range(n):
                b = self.backends[self._index]
                self._index = (self._index + 1) % n
                if b.is_available() and b.supports_model(requested_model):
                    return b
            # Second pass: any available backend (model support unknown or none matched)
            start = self._index
            for _ in range(n):
                b = self.backends[self._index]
                self._index = (self._index + 1) % n
                if b.is_available():
                    return b
                if self._index == start:
                    break
            return None

    def get_aggregated_models(self) -> List[Dict[str, str]]:
        """Expose exactly the freemaxxing router alias to pickers.

        The external /v1/models surface advertises a single model so a picker
        shows exactly one entry, never the hundreds of backend models. The
        internal (free-only) routing catalog is refreshed here so the alias can
        resolve a concrete free model at forward time.

        Backend list is snapshotted under the lock; remote catalog fetches run
        OUTSIDE the lock so a slow/absent catalog (8s timeout per backend) can
        never freeze chat selection, health, or add/clear for other threads.
        """
        with self._lock:
            backends = list(self.backends)

        # Refresh each backend's free-only catalog for routing. Fetches run
        # outside the lock; results are published back per-backend.
        for b in backends:
            try:
                b.set_cached_models(self._fetch_models(b))
            except Exception as e:
                logger.debug("freemaxxing: catalog refresh failed for %s: %s", b.name, e)

        return [{
            "id": "freemaxxing",
            "object": "model",
            "owned_by": "freemaxxing",
        }]

    def _fetch_models(self, backend: Backend) -> List[str]:
        url = backend.base_url + "/models"
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": _hermes_user_agent()},
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
        except Exception as e:
            logger.debug("freemaxxing: model fetch failed for %s: %s", backend.name, e)
            return []

    def exhaustion_detail(self) -> str:
        """Human reason for a 503 when no backend can be selected."""
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

# ── Router model alias ───────────────────────────────────────────────────────
# The freemaxxing "model" is a virtual router id — asking for it means "pick the
# best available backend and use its default model". These ids skip
# model-affinity routing (no backend advertises "freemaxxing" in its catalog)
# and get substituted with the selected backend's default_model at forward time.
_ROUTER_MODELS = {"freemaxxing", "fm", "freemaxxing-auto"}

# Preferred Nous/auto picks. OpenRouter's ":free" suffix is NOT a Nous
# convention — preferring it sent traffic to hanging poolside/tencent free
# IDs while deepseek-v4-flash-0731 sat unused in the same catalog.
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
    """Decide whether a catalog id is eligible for routing on this backend.

    OpenRouter bills anything without ``:free``. Nous Portal and HuggingFace
    already entitlement-filter their /models list for the caller's account,
    so applying the OpenRouter suffix there empties (or poisons) the catalog.
    Mock/unknown backends keep their full list so unit tests stay honest.
    """
    if not model_id:
        return False
    mid = model_id.lower()
    if mid.endswith(":batch") or mid.startswith("~"):
        return False
    kind = _backend_kind(backend)
    if kind == "openrouter" or kind.startswith("openrouter"):
        return ":free" in mid
    return True


def _resolve_auto_model(backend: Backend) -> str:
    """Pick the best concrete model for the freemaxxing router alias.

    Order: backend.default_model if present in catalog, then the known-good
    flash IDs, then remaining catalog entries (flash/mini before the rest).
    Empty catalog falls back to backend.default_model so Nous still works
    when /models is slow or filtered.
    """
    cached = backend.get_cached_models()
    if not cached:
        try:
            cached = pool._fetch_models(backend)
            backend.set_cached_models(cached)
        except Exception as e:
            logger.debug("freemaxxing: auto-model fetch failed for %s: %s", backend.name, e)
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
        _small = [
            m
            for m in cached
            if any(k in m.lower() for k in ("flash", "mini", "nano", "small", "lite"))
        ]
        for candidate in _small + cached:
            low = candidate.lower()
            if low in _ROUTER_MODELS or low.endswith(":batch") or low.startswith("~"):
                continue
            return candidate

    if backend.default_model:
        return backend.default_model
    return ""

# ── Handler ──────────────────────────────────────────────────────────────────

_MAX_BODY = 1_000_000


def _hermes_user_agent() -> str:
    """Return a ``hermes-cli/<version>`` UA string for outbound requests.

    The Nous inference endpoint sits behind Cloudflare's browser-integrity
    check, which returns ``error code: 1010`` for the default
    ``Python-urllib/*`` UA. Mirror the UA that ``ProviderProfile.fetch_models``
    uses (providers/base.py) so the proxy is not 403'd at the edge.
    """
    try:
        from hermes_cli import __version__ as _ver
        return f"hermes-cli/{_ver}"
    except Exception:
        return "hermes-cli"


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

    def _send_error(self, code: int, message: str, error_type: str = "freemaxxing_error") -> None:
        self._send_json(code, {"error": {"message": message, "type": error_type}})

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/v1/models":
            self._send_json(200, {"object": "list", "data": pool.get_aggregated_models()})
            return
        if path == "/healthz":
            self._send_json(200, pool.health())
            return
        self._send_error(404, f"Unknown path: {path}")

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        if path != "/v1/chat/completions":
            self._send_error(404, f"Unknown path: {path}")
            return
        self._handle_chat_completions()

    def _read_body(self) -> Optional[Dict[str, Any]]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > _MAX_BODY:
                self._send_error(413 if content_length > _MAX_BODY else 400, "Invalid Content-Length")
                return None
            return json.loads(self.rfile.read(content_length))
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

    def _handle_nonstreaming(self, body: Dict[str, Any], model: str) -> None:
        tried: Set[str] = set()
        last_error: Optional[str] = None
        max_attempts = max(len(pool.backends) * 2, 3)

        for _ in range(max_attempts):
            backend = pool.next(model)
            if backend is None:
                break
            if backend.name in tried:
                continue
            tried.add(backend.name)
            try:
                response = _forward(backend, body)
                self._send_json(200, response)
                backend.record_success()
                logger.info(
                    "freemaxxing: model=%s selected=%s tier=%d attempted=%d/%d",
                    model, backend.name, backend.tier, len(tried), max_attempts,
                )
                return
            except RateLimitError as e:
                backend.record_failure(retry_after=e.retry_after, error_class="rate_limit")
                last_error = f"rate limited on {backend.name} (retry after {e.retry_after:.0f}s)"
                logger.warning("freemaxxing: %s", last_error)
            except ModelNotFoundError as e:
                last_error = str(e)
                logger.info("freemaxxing: %s — skipping", last_error)
            except AuthError as e:
                last_error = str(e)
                logger.error("freemaxxing: %s — backend auth broken", last_error)
            except ClientRequestError as e:
                self._send_error(400, str(e), error_type="invalid_request")
                return
            except TransientError as e:
                backend.record_failure(retry_after=10.0, error_class="transient")
                last_error = f"{backend.name} transient failure"
                logger.warning("freemaxxing: %s", last_error)
            except Exception as e:
                backend.record_failure(retry_after=10.0, error_class="transient")
                last_error = f"{backend.name} failed"
                logger.warning("freemaxxing: %s", last_error)

        self._send_error(503, _exhausted_message(last_error))

    def _handle_streaming(self, body: Dict[str, Any], model: str) -> None:
        tried: Set[str] = set()
        last_error: Optional[str] = None
        max_attempts = max(len(pool.backends) * 2, 3)

        for _ in range(max_attempts):
            backend = pool.next(model)
            if backend is None:
                break
            if backend.name in tried:
                continue
            tried.add(backend.name)
            try:
                resp = _open_stream(backend, body)
            except RateLimitError as e:
                backend.record_failure(retry_after=e.retry_after, error_class="rate_limit")
                last_error = f"rate limited on {backend.name}"
                logger.warning("freemaxxing: %s", last_error)
                continue
            except ModelNotFoundError as e:
                last_error = str(e)
                logger.info("freemaxxing: %s — skipping", last_error)
                continue
            except AuthError as e:
                last_error = str(e)
                logger.error("freemaxxing: %s", last_error)
                continue
            except ClientRequestError as e:
                self._send_error(400, str(e), error_type="invalid_request")
                return
            except (TransientError, Exception) as e:
                backend.record_failure(retry_after=10.0, error_class="transient")
                last_error = f"{backend.name} transient failure"
                logger.warning("freemaxxing: %s", last_error)
                continue

            # Stream opened successfully — commit to the client and forward bytes.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            backend.record_success()
            logger.info("freemaxxing: model=%s selected=%s tier=%d (streaming)",
                        model, backend.name, backend.tier)
            try:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception as e:
                logger.warning("freemaxxing: stream from %s interrupted: %s", backend.name, e)
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            return

        self._send_error(503, _exhausted_message(last_error))


def _exhausted_message(last_error: Optional[str]) -> str:
    detail = last_error or pool.exhaustion_detail()
    return f"All backends exhausted. Last error: {detail}"


def _forward(backend: Backend, body: Dict[str, Any]) -> Dict[str, Any]:
    """Forward a non-streaming request, preserving the full body."""
    resp = _open_response(backend, body)
    with resp:
        return json.loads(resp.read().decode())


def _open_stream(backend: Backend, body: Dict[str, Any]):
    """Open an upstream stream. Raises typed errors on failure; on success the
    caller owns the response object (must close it)."""
    return _open_response(backend, body)


def _open_response(backend: Backend, body: Dict[str, Any]):
    """Open an upstream request, mapping failures to typed exceptions.

    The full request body is forwarded unchanged (model, messages, tools,
    response_format, seed, stop, top_p, extra_body, etc.), so downstream
    capabilities are preserved where the selected backend supports them.

    401/403 on a backend with a credential refresher (Nous JWT) triggers one
    re-resolve + retry so a rotating token never wedges the pool.
    """

    def _attempt(base_url: str, api_key: str):
        url = base_url.rstrip("/") + "/chat/completions"
        # Router alias ("freemaxxing") → substitute the best available model
        # from the selected backend's live catalog, with the backend's static
        # default as a last resort. This is what "freemaxxing = auto" means:
        # never a hardcoded provider model, always the best one on hand.
        _out = body
        if _is_router_model(str(body.get("model", ""))):
            _real = _resolve_auto_model(backend)
            if _real:
                _out = dict(body)
                _out["model"] = _real
            else:
                # No free model resolved on this backend. NEVER forward the raw
                # "freemaxxing" alias upstream: the upstream's own default may be
                # a paid model, which would violate the free-only invariant.
                # Skip this backend so the request fails over to a cheaper tier
                # (or 503s when every tier is exhausted).
                raise ModelNotFoundError(
                    f"backend {backend.name} has no free model in catalog"
                )
        req = urllib.request.Request(
            url,
            data=json.dumps(_out).encode("utf-8"),
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
        except urllib.error.HTTPError as e:
            # Do NOT read/log the body — it may contain prompt fragments or secrets.
            code = e.code
            if code == 429:
                retry_after = _parse_retry_after(e.headers)
                raise RateLimitError(f"backend {backend.name} rate-limited", retry_after)
            if code in (401, 403):
                raise AuthError(f"backend {backend.name} auth rejected (HTTP {code})")
            if code == 404:
                # 404 on a model id = "model not found" on OpenAI-compatible
                # endpoints. Skip this backend, do not cooldown.
                raise ModelNotFoundError(
                    f"backend {backend.name} does not serve model (HTTP 404)"
                )
            if 400 <= code < 500:
                # Genuine malformed request (bad max_tokens, unsupported param).
                # Fail clearly to the caller — do not retry elsewhere.
                raise ClientRequestError(
                    f"backend {backend.name} rejected request (HTTP {code})"
                )
            # 5xx → transient, cooldown-worthy
            raise TransientError(f"backend {backend.name} returned HTTP {code}")
        except urllib.error.URLError as e:
            raise TransientError(f"backend {backend.name} unreachable: {e.reason}")
        except TimeoutError:
            raise TransientError(f"backend {backend.name} timed out")

    try:
        # If the backend has a refresher but no key yet (JWT deferred from
        # discovery, when auth was mid-import), resolve it before the first
        # attempt so the request doesn't burn a guaranteed 401 round-trip.
        # The per-backend lock serializes refresh-and-assign so concurrent
        # threads cannot interleave a new key with an old base URL; re-check the
        # key after acquiring so only the first thread performs the refresh.
        if not backend.api_key and backend.refresh is not None:
            with backend.refresh_lock:
                if not backend.api_key:
                    try:
                        new_base, new_key = backend.refresh()
                        if new_key:
                            backend.base_url = new_base
                            backend.api_key = new_key
                    except Exception as e:
                        logger.debug("freemaxxing: %s pre-request refresh failed: %s", backend.name, e)
        return _attempt(backend.base_url, backend.api_key)
    except AuthError:
        if backend.refresh is None:
            raise
        # Rotating credential (Nous JWT): re-resolve once and retry before
        # declaring the backend auth-broken. Serialized per-backend so the key
        # and base URL are assigned atomically under concurrency. Only skip the
        # refresh when another thread actually produced a new key (the pre-lock
        # value changed); a pre-existing stale/placeholder key still triggers a
        # refresh here.
        before = backend.api_key
        with backend.refresh_lock:
            if backend.api_key != before:
                # Another thread refreshed while we waited; retry with the pair.
                return _attempt(backend.base_url, backend.api_key)
            try:
                new_base, new_key = backend.refresh()
                if new_key and new_key != backend.api_key:
                    backend.base_url = new_base
                    backend.api_key = new_key
                    return _attempt(new_base, new_key)
            except Exception as e:
                logger.warning(
                    "freemaxxing: %s credential refresh failed: %s", backend.name, e
                )
        raise


def _parse_retry_after(headers) -> float:
    raw = headers.get("Retry-After")
    if raw is None:
        return 30.0
    try:
        return float(raw)
    except (ValueError, TypeError):
        # Retry-After may be an HTTP-date; fall back to a safe default.
        return 30.0


# ── Lifecycle ────────────────────────────────────────────────────────────────

_proxy_server: Optional[ThreadingHTTPServer] = None
_proxy_lock = threading.Lock()


def spawn_proxy(*, port: int = 0) -> ThreadingHTTPServer:
    """Start the proxy exactly once (singleton). Returns the live server."""
    global _proxy_server
    with _proxy_lock:
        if _proxy_server is not None:
            return _proxy_server
        server = ThreadingHTTPServer(("127.0.0.1", port), ChatCompletionsHandler)
        thread = threading.Thread(target=server.serve_forever, name="freemaxxing-proxy", daemon=True)
        thread.start()
        _proxy_server = server
        logger.info("freemaxxing: proxy listening on 127.0.0.1:%d", server.server_address[1])
        return server


def stop_proxy(server: Optional[ThreadingHTTPServer] = None) -> None:
    global _proxy_server
    target = server or _proxy_server
    if target is None:
        return
    try:
        target.shutdown()
        target.server_close()
    except Exception as e:
        logger.debug("freemaxxing: proxy shutdown error: %s", e)
    _proxy_server = None


atexit.register(lambda: stop_proxy())
