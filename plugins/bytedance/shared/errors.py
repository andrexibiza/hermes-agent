"""Structured provider error model.

The shared HTTP client wraps every non-success into a ``ProviderError``
that carries structured context for the adapter to decide retryability
and for observability to log without leaking secrets.

Per the design spec §4.1 / §7.1: the shared layer owns the error
envelope, not provider return-code mapping. Provider codes are
carried verbatim in ``provider_code``.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, Optional


@dataclasses.dataclass(frozen=True)
class RequestFailureRecord:
    """Immutable record of why a request ultimately failed.

    Stored by the bounded HTTP client so callers can inspect retry
    history without re-walking the retry loop.  ``attempts`` counts
    every attempt including the final one.
    """

    attempts: int
    status: Optional[int]
    provider_code: Optional[str]
    request_id: Optional[str]
    last_error: Optional[str]
    retried: bool


class ProviderError(Exception):
    """Raised when a provider API call fails after retries are exhausted.

    Contains a structured envelope with HTTP status, provider code,
    request ID, retryability, and redacted context.  The raw response
    body is never attached — adapters normalize it themselves.
    """

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        provider_code: Optional[str] = None,
        request_id: Optional[str] = None,
        retryable: bool = False,
        context: Optional[Dict[str, Any]] = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.provider_code = provider_code
        self.request_id = request_id
        self.retryable = retryable
        self.context: Dict[str, Any] = dict(context or {})
        self.attempts = attempts

    @property
    def failure_record(self) -> RequestFailureRecord:
        return RequestFailureRecord(
            attempts=self.attempts,
            status=self.status,
            provider_code=self.provider_code,
            request_id=self.request_id,
            last_error=self.message,
            retried=self.retryable,
        )

    def __str__(self) -> str:
        parts = [self.message]
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.provider_code:
            parts.append(f"code={self.provider_code}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        if self.retryable:
            parts.append("retryable=True")
        return "ProviderError(" + ", ".join(parts) + ")"

    def to_redacted_dict(self) -> Dict[str, Any]:
        """Return a log-safe dict with secrets stripped from context."""
        safe_context = {
            k: ("***REDACTED***" if _is_secret_key(k) else v)
            for k, v in self.context.items()
        }
        return {
            "message": self.message,
            "status": self.status,
            "provider_code": self.provider_code,
            "request_id": self.request_id,
            "retryable": self.retryable,
            "attempts": self.attempts,
            "context": safe_context,
        }


# Keys whose values are treated as secrets and replaced with a
# placeholder in redacted logs.  Checked as a substring match
# (case-insensitive) against context dict keys.
_SECRET_KEY_PATTERNS = (
    "token",
    "secret",
    "password",
    "key",
    "credential",
    "auth",
    "signature",
    "cookie",
    "bearer",
)


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(p in lowered for p in _SECRET_KEY_PATTERNS)
