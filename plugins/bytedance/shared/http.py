"""Bounded async HTTP client with retry semantics.

Design: shared layer owns timeout/retry framework, endpoint-specific
concurrency, body-size caps, and structured error envelopes.  Provider
endpoint paths and versions live in the client layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

import aiohttp

from plugins.bytedance.shared.errors import ProviderError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Retry taxonomy
# ---------------------------------------------------------------------------

# HTTP classes that are always retryable when idempotency is provable.
_TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Default per-attempt timeouts (seconds).
DEFAULT_TOTAL_TIMEOUT = 30.0
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_SOA_TIMEOUT = 30.0
DEFAULT_READ_TIMEOUT = 30.0
DEFAULT_SOCK_READ_TIMEOUT = 30.0

# Response body byte cap — JSON object envelope must be bounded.
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MiB

# Maximum retries before giving up.
DEFAULT_MAX_RETRIES = 3


@dataclass
class EndpointConfig:
    """Per-endpoint timeout, concurrency, and retry policy.

    Stored keyed by endpoint name so the client can apply different
    limits to different provider operations.
    """

    timeout: aiohttp.ClientTimeout = field(
        default_factory=lambda: aiohttp.ClientTimeout(
            total=DEFAULT_TOTAL_TIMEOUT,
            connect=DEFAULT_CONNECT_TIMEOUT,
            sock_connect=DEFAULT_CONNECT_TIMEOUT,
            sock_read=DEFAULT_SOA_TIMEOUT,
        )
    )
    max_concurrency: int = 10
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_on: Tuple[int, ...] = (408, 425, 429, 500, 502, 503, 504)
    # Whether the operation is safe to retry automatically.  Non-idempotent
    # operations (POST with side effects) must set this False unless the
    # caller passes an explicit idempotency key.
    idempotent: bool = True


class BoundedApiClient:
    """Async HTTP client with retry, concurrency, and body-size bounds.

    Usage::

        client = BoundedApiClient(base_url="https://open-api.tiktok.com",
                                  default_headers={...})
        result = await client.request("GET", "/v1/conversation/list",
                                      params={...})
    """

    def __init__(
        self,
        base_url: str,
        *,
        default_headers: Optional[Dict[str, str]] = None,
        default_endpoint: str = "default",
        default_timeout: float = DEFAULT_TOTAL_TIMEOUT,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._default_headers = dict(default_headers or {})
        self._default_timeout = default_timeout
        self._max_response_bytes = max_response_bytes
        self._semaphores: Dict[str, asyncio.Semaphore] = {}
        self._endpoint_configs: Dict[str, EndpointConfig] = {}
        self._sessions: Dict[str, aiohttp.ClientSession] = {}
        self._default_endpoint = default_endpoint

    def register_endpoint(self, name: str, config: EndpointConfig) -> None:
        """Register an endpoint-specific config (timeouts, concurrency, retry)."""
        self._endpoint_configs[name] = config
        self._semaphores[name] = asyncio.Semaphore(config.max_concurrency)

    def get_semaphore(self, endpoint: str) -> asyncio.Semaphore:
        """Get (or lazily create) the concurrency semaphore for an endpoint."""
        if endpoint not in self._semaphores:
            cfg = self._endpoint_configs.get(endpoint)
            if cfg:
                self._semaphores[endpoint] = asyncio.Semaphore(cfg.max_concurrency)
            else:
                self._semaphores[endpoint] = asyncio.Semaphore(10)
        return self._semaphores[endpoint]

    @property
    def default_endpoint_config(self) -> EndpointConfig:
        return self._endpoint_configs.get(
            self._default_endpoint, EndpointConfig()
        )

    def _build_timeout(self, endpoint: str) -> aiohttp.ClientTimeout:
        cfg = self._endpoint_configs.get(endpoint)
        if cfg:
            return cfg.timeout
        return aiohttp.ClientTimeout(
            total=self._default_timeout,
            connect=DEFAULT_CONNECT_TIMEOUT,
            sock_connect=DEFAULT_CONNECT_TIMEOUT,
            sock_read=DEFAULT_SOA_TIMEOUT,
        )

    def _get_session(self, endpoint: str) -> aiohttp.ClientSession:
        """Return or create an aiohttp session for the endpoint."""
        if endpoint not in self._sessions:
            timeout = self._build_timeout(endpoint)
            self._sessions[endpoint] = aiohttp.ClientSession(
                timeout=timeout,
                headers=self._default_headers,
            )
        return self._sessions[endpoint]

    async def close(self) -> None:
        """Close all sessions."""
        for session in self._sessions.values():
            if not session.closed:
                await session.close()
        self._sessions.clear()

    async def __aenter__(self) -> "BoundedApiClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def request(
        self,
        method: str,
        url: str,
        *,
        endpoint: str = "",
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        raw_body: Optional[bytes] = None,
        idempotency_key: Optional[str] = None,
        max_retries: Optional[int] = None,
    ) -> Any:
        """Make a bounded HTTP request with retry.

        Args:
            method: HTTP method (GET, POST, etc.)
            url: Full URL or path (prefixed with base_url if relative)
            endpoint: Endpoint name for config lookup (defaults to endpoint
                name or "" for the default config)
            idempotency_key: When set for a non-idempotent operation, the
                request is retried only because the key proves safety
            max_retries: Override the endpoint's max_retries

        Returns:
            Parsed JSON (dict) or raw text if JSON parsing fails.

        Raises:
            ProviderError: After all retries exhausted.
        """
        ep_name = endpoint or (url if url.startswith("/") and "/" in url else "")
        # Use endpoint name for semaphore/config lookup; default if not registered
        effective_endpoint = ep_name if ep_name in self._endpoint_configs else ""
        sem = self.get_semaphore(effective_endpoint or "default")
        cfg = (
            self._endpoint_configs.get(effective_endpoint, None)
            or self.default_endpoint_config
        )
        if max_retries is None:
            max_retries = cfg.max_retries

        full_url = self._resolve_url(url)
        merged_headers = {**self._default_headers}
        if headers:
            merged_headers.update(headers)

        if idempotency_key:
            merged_headers["X-Idempotency-Key"] = idempotency_key

        body_bytes = None
        if json_body is not None:
            body_bytes = json.dumps(json_body).encode("utf-8")
            merged_headers.setdefault("Content-Type", "application/json")
        elif raw_body is not None:
            body_bytes = raw_body

        attempts = 0
        last_error: Optional[ProviderError] = None

        async with sem:
            for attempt in range(1, max_retries + 1):
                attempts = attempt
                try:
                    result, status, resp_headers = await self._do_request(
                        method,
                        full_url,
                        merged_headers,
                        params,
                        body_bytes,
                    )
                    # Success path — within 2xx
                    if 200 <= status < 300:
                        return result
                    # Error path
                    should_retry, err = self._classify_error(
                        status, result, resp_headers, attempt, cfg
                    )
                    if not should_retry:
                        raise err
                    last_error = err
                except aiohttp.ClientError as exc:
                    last_error = ProviderError(
                        f"Connection error: {exc}",
                        retryable=True,
                        context={"endpoint": effective_endpoint or "default"},
                        attempts=attempt,
                    )
                    logger.debug(
                        "HTTP retryable client error on attempt %d: %s",
                        attempt,
                        exc,
                    )

                if attempt < max_retries:
                    retry_after = self._parse_retry_after(
                        last_error, resp_headers if "resp_headers" in locals() else {}
                    )
                    await asyncio.sleep(retry_after)

        # All retries exhausted — raise the last error with attempt count
        if last_error:
            raise ProviderError(
                last_error.message,
                status=last_error.status,
                provider_code=last_error.provider_code,
                request_id=last_error.request_id,
                retryable=last_error.retryable,
                context=last_error.context,
                attempts=attempts,
            ) from last_error
        raise ProviderError(
            "Request failed after all retries", retryable=False, attempts=attempts
        )

    def _resolve_url(self, url: str) -> str:
        if url.startswith("http://") or url.startswith("https://"):
            return url
        return f"{self.base_url}{url}"

    async def _do_request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        params: Optional[Dict[str, Any]],
        body: Optional[bytes],
    ) -> Tuple[Any, int, Dict[str, str]]:
        session = self._get_session("default")
        try:
            async with session.request(
                method,
                url,
                headers=headers,
                params=params,
                data=body,
            ) as resp:
                # Check size before reading full body
                content_length = resp.content_length or 0
                if content_length > self._max_response_bytes:
                    raise ProviderError(
                        f"Response too large: {content_length} bytes "
                        f"(limit: {self._max_response_bytes})",
                        status=resp.status,
                        retryable=False,
                    )

                raw = await resp.read()
                if len(raw) > self._max_response_bytes:
                    raise ProviderError(
                        f"Response body exceeds {self._max_response_bytes} bytes",
                        status=resp.status,
                        retryable=False,
                    )

                # Validate JSON type: require top-level dict for object APIs
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    # Return raw text for non-JSON responses
                    return raw.decode("utf-8", errors="replace"), resp.status, {}

                if not isinstance(parsed, (dict, list)):
                    raise ProviderError(
                        "JSON response is not an object or array",
                        status=resp.status,
                        retryable=False,
                    )

                resp_headers = dict(resp.headers)
                return parsed, resp.status, resp_headers
        except aiohttp.ClientResponseError as exc:
            raise ProviderError(
                f"HTTP {exc.status}: {exc.message}",
                status=exc.status,
                retryable=exc.status in _TRANSIENT_HTTP_STATUSES,
            ) from exc

    def _classify_error(
        self,
        status: int,
        result: Any,
        resp_headers: Dict[str, str],
        attempt: int,
        cfg: EndpointConfig,
    ) -> Tuple[bool, ProviderError]:
        """Classify an HTTP error response.

        Returns (should_retry, ProviderError).

        Non-idempotent operations are not retried unless an idempotency
        key proves safety.
        """
        provider_code: Optional[str] = None
        request_id: Optional[str] = None
        message: str = f"HTTP {status}"

        if isinstance(result, dict):
            # TikTok common error envelope: {"message": ..., "error_code": ...}
            # or generic {"error": {"code": ..., "description": ...}}
            provider_code = result.get("error_code") or result.get("code")
            message = result.get("message") or result.get("error", {}).get(
                "description", message
            )
            request_id = result.get("request_id") or result.get("request_id")

        retryable_status = status in _TRANSIENT_HTTP_STATUSES
        if status in cfg.retry_on:
            retryable_status = True

        # Non-idempotent operations must not auto-retry
        should_retry = retryable_status and cfg.idempotent and attempt <= cfg.max_retries

        err = ProviderError(
            message,
            status=status,
            provider_code=str(provider_code) if provider_code else None,
            request_id=request_id,
            retryable=should_retry,
            context={"endpoint": cfg and "configured" or "default"},
            attempts=attempt,
        )
        return should_retry, err

    @staticmethod
    def _parse_retry_after(err: ProviderError, resp_headers: Dict[str, str]) -> float:
        """Parse Retry-After header, return backoff seconds with full jitter."""
        import random as _random

        retry_after = resp_headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except (ValueError, TypeError):
                pass

        # Exponential backoff with full jitter
        base = 0.5
        exp = min(err.attempts or 1, 10)
        cap = base * (2 ** exp)
        return _random.uniform(0, min(cap, 60.0))
