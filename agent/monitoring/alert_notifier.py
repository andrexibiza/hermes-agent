"""Fail-isolated alert notifications for monitoring transitions.

The notifier is an optional Feishu webhook subscriber. It accepts only the
stable, typed monitoring event names owned by this module and never lets alert
formatting, redaction, executor submission, or lifecycle cleanup affect the
monitoring emitter or gateway.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_ALERT_RATE_LIMIT_SEC = 300.0
_WEBHOOK_TIMEOUT_SEC = 5.0
_MAX_SERVER_IDENTITY = 128
_MAX_REASON = 160
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_BREAKER_STATES = frozenset({"closed", "open", "half-open"})
_LIVENESS_STATES = frozenset({"connecting", "connected", "degraded", "parked"})
_SEVERITIES = frozenset({"info", "warning", "error"})

_state_lock = threading.RLock()
_last_sent: Dict[str, float] = {}
_executor: Optional[ThreadPoolExecutor] = None
_webhook_url = ""
_subscribed_emitter: Any = None


def _safe_debug(message: str, *args: Any, **kwargs: Any) -> None:
    """Best-effort diagnostics; a broken logging handler must not escape."""
    try:
        logger.debug(message, *args, **kwargs)
    except Exception:
        pass


def _now() -> float:
    return time.monotonic()


def _redacted_bounded(value: Any, *, limit: int) -> str:
    """Redact, remove controls, and bound structured alert text."""
    try:
        from agent.monitoring.redaction import redact_for_export
        text = redact_for_export(str(value)) or "[redacted]"
    except Exception:
        text = "[redaction-unavailable]"
    text = _CONTROL_RE.sub(" ", text)
    text = " ".join(text.split())
    if not text:
        return "[redacted]"
    if len(text) <= limit:
        return text
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
    return f"{text[: max(1, limit - 14)]}~{digest}"


def _server_identity(event: Dict[str, Any]) -> str:
    return _redacted_bounded(
        event.get("error_code") or event.get("platform") or "mcp",
        limit=_MAX_SERVER_IDENTITY,
    )


def _valid_transition(event: Dict[str, Any], allowed_states: frozenset[str]) -> bool:
    return (
        event.get("subsystem") == "mcp"
        and event.get("old_state") in allowed_states
        and event.get("new_state") in allowed_states
        and event.get("severity") in _SEVERITIES
    )


def _post_feishu(webhook_url: str, title: str, body: str) -> None:
    """POST one alert, swallowing network/serialization failures."""
    try:
        payload = json.dumps(
            {"msg_type": "text", "content": {"text": f"{title}\n{body}"}},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=_WEBHOOK_TIMEOUT_SEC) as response:
            response.read()
    except Exception as exc:
        _safe_debug("feishu alert post failed: %s", exc, exc_info=True)


def _dedup_send(dedup_key: str, title: str, body: str) -> bool:
    """Submit an alert and consume dedup state only after submit succeeds."""
    global _last_sent
    with _state_lock:
        executor = _executor
        webhook_url = _webhook_url
        if executor is None or not webhook_url:
            return False
        now = _now()
        if dedup_key in _last_sent and now - _last_sent[dedup_key] < _ALERT_RATE_LIMIT_SEC:
            return False
        try:
            executor.submit(_post_feishu, webhook_url, title, body)
        except Exception as exc:
            _safe_debug("feishu alert submit failed: %s", exc, exc_info=True)
            return False
        _last_sent[dedup_key] = now
        return True


def _format_breaker_alert(event: Dict[str, Any]) -> Optional[tuple[str, str, str]]:
    """Format only the exact MCP breaker transition event."""
    if event.get("event") != "gateway_diagnostic":
        return None
    if event.get("name") != "mcp_breaker_transition":
        return None
    if not _valid_transition(event, _BREAKER_STATES):
        return None
    if event.get("new_state") != "open":
        return None
    server = _server_identity(event)
    severity = event["severity"]
    icon = "🔴" if severity == "error" else "🟠"
    old_state = event["old_state"]
    title = f"{icon} [hermes] MCP breaker open: {server}"
    body = f"{server} {old_state}→open (subsystem=mcp)"
    return f"mcp_breaker_open:{server}", title, body


def _format_liveness_alert(event: Dict[str, Any]) -> Optional[tuple[str, str, str]]:
    """Format only a parked MCP liveness transition."""
    if event.get("event") != "gateway_diagnostic":
        return None
    if event.get("name") != "mcp_liveness_transition":
        return None
    if not _valid_transition(event, _LIVENESS_STATES):
        return None
    if event.get("new_state") != "parked":
        return None
    server = _server_identity(event)
    reason = _redacted_bounded(event.get("reason") or "unknown", limit=_MAX_REASON)
    severity = event["severity"]
    icon = "🔴" if severity == "error" else "🟠"
    old_state = event["old_state"]
    title = f"{icon} [hermes] MCP server parked: {server}"
    body = f"{server} {old_state}→parked (subsystem=mcp)\nreason={reason}"
    return f"mcp_liveness_parked:{server}", title, body


def _format_health_alert(event: Dict[str, Any]) -> Optional[tuple[str, str, str]]:
    """Format fatal gateway-health degradation, if present."""
    if event.get("event") != "gateway_health":
        return None
    new_state = event.get("new_state") or ""
    if new_state not in {"degraded", "fatal", "error", "failed"}:
        return None
    fatal = event.get("fatal_platform_count", 0)
    if fatal == 0 and new_state != "fatal":
        return None
    old_state = _redacted_bounded(event.get("old_state") or "?", limit=32)
    state = _redacted_bounded(new_state, limit=32)
    return (
        f"gateway_health:{state}",
        f"🔴 [hermes] gateway state degraded: {state}",
        f"gateway {old_state}→{state}\nfatal_platforms={fatal}",
    )


def _on_event(batch: list) -> None:
    """Emitter subscriber: filter cheaply, then submit fail-isolated alerts."""
    try:
        for event in batch:
            if not isinstance(event, dict):
                continue
            result = (
                _format_breaker_alert(event)
                or _format_liveness_alert(event)
                or _format_health_alert(event)
            )
            if result is not None:
                _dedup_send(*result)
    except Exception:
        _safe_debug("alert notifier event handling failed", exc_info=True)


def _reset_state() -> None:
    global _executor, _webhook_url, _subscribed_emitter
    with _state_lock:
        _executor = None
        _webhook_url = ""
        _subscribed_emitter = None
        _last_sent.clear()


def start_alert_notifier(config: Optional[Dict[str, Any]] = None) -> None:
    """Start one notifier subscription; repeated starts replace the old one."""
    global _executor, _webhook_url, _subscribed_emitter
    stop_alert_notifier()
    config = config if isinstance(config, dict) else {}
    executor: Optional[ThreadPoolExecutor] = None
    emitter = None
    try:
        monitoring = config.get("monitoring") or {}
        alert = monitoring.get("alert") if isinstance(monitoring, dict) else None
        alert = alert if isinstance(alert, dict) else {}
        url = str(
            alert.get("feishu_webhook_url")
            or os.environ.get("FEISHU_WEBHOOK_URL")
            or ""
        ).strip()
        if not url:
            return
        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hermes-alert")
        from agent.monitoring.emitter import get_emitter
        emitter = get_emitter()
        emitter.subscribe(_on_event)
        with _state_lock:
            _executor = executor
            _webhook_url = url
            _subscribed_emitter = emitter
    except Exception:
        _safe_debug("alert notifier start failed", exc_info=True)
        if emitter is not None:
            try:
                emitter.unsubscribe(_on_event)
            except Exception:
                _safe_debug("alert notifier failed-subscribe cleanup failed", exc_info=True)
        if executor is not None:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                _safe_debug("alert notifier executor cleanup failed", exc_info=True)
        _reset_state()


def stop_alert_notifier() -> None:
    """Stop the notifier and clear all state; safe before start and repeatedly."""
    global _executor, _webhook_url, _subscribed_emitter
    with _state_lock:
        executor = _executor
        emitter = _subscribed_emitter
        _executor = None
        _webhook_url = ""
        _subscribed_emitter = None
        _last_sent.clear()
    if emitter is not None:
        try:
            emitter.unsubscribe(_on_event)
        except Exception:
            _safe_debug("alert notifier unsubscribe failed", exc_info=True)
    if executor is not None:
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            _safe_debug("alert notifier executor shutdown failed", exc_info=True)


__all__ = [
    "start_alert_notifier",
    "stop_alert_notifier",
    "_format_breaker_alert",
    "_format_liveness_alert",
    "_format_health_alert",
    "_dedup_send",
]
