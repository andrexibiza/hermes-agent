"""DNS-pinned, redirect-free, signed completion callback transport."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver


class CallbackSecurityError(ValueError):
    """The callback destination or authentication contract is unsafe."""


def _address_allowed(raw: str, *, allow_private: bool) -> bool:
    address = ipaddress.ip_address(raw)
    if allow_private:
        return not (address.is_unspecified or address.is_multicast)
    return address.is_global


class PinnedResolver(AbstractResolver):
    """Serve one prevalidated DNS answer set and never re-resolve on connect."""

    def __init__(self, hostname: str, addresses: list[str]):
        self.hostname = hostname.casefold()
        self.addresses = tuple(addresses)

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_UNSPEC,
    ):
        if host.casefold() != self.hostname:
            raise CallbackSecurityError("callback hostname changed after validation")
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": socket.AF_INET6 if ":" in address else socket.AF_INET,
                "proto": socket.IPPROTO_TCP,
                "flags": 0,
            }
            for address in self.addresses
        ]

    async def close(self):
        return None


def _parse_callback_url(url: str):
    parsed = urlsplit(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CallbackSecurityError("callback URL must be absolute http(s)")
    if parsed.username is not None or parsed.password is not None:
        raise CallbackSecurityError("callback URL credentials are forbidden")
    if parsed.fragment:
        raise CallbackSecurityError("callback URL fragments are forbidden")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise CallbackSecurityError("callback URL port is invalid") from exc
    return parsed, port


async def resolve_callback(
    url: str,
    *,
    allow_private: bool = False,
) -> tuple[str, list[str]]:
    parsed, port = _parse_callback_url(url)
    loop = asyncio.get_running_loop()
    try:
        records = await loop.getaddrinfo(
            parsed.hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise CallbackSecurityError("callback hostname could not be resolved") from exc
    addresses = sorted({str(record[4][0]) for record in records})
    if not addresses:
        raise CallbackSecurityError("callback hostname has no addresses")
    rejected = [
        item
        for item in addresses
        if not _address_allowed(item, allow_private=allow_private)
    ]
    if rejected:
        raise CallbackSecurityError(
            "callback resolves to blocked address(es): " + ", ".join(rejected)
        )
    return parsed.hostname, addresses


@dataclass(frozen=True)
class CallbackResult:
    status: int
    attempts: int


def callback_envelope(
    *,
    execution_id: str,
    profile: str,
    route: str,
    state: str,
    output: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "execution_id": execution_id,
        "profile": profile,
        "route": route,
        "state": state,
        "output": output,
        "error": error,
    }


async def deliver_signed_callback(
    *,
    url: str,
    envelope: dict[str, Any],
    secret: str,
    timeout: float = 10.0,
    max_attempts: int = 3,
    allow_private: bool = False,
) -> CallbackResult:
    """Deliver one signed envelope with pinned DNS and bounded retries."""
    if not isinstance(secret, str) or not secret:
        raise CallbackSecurityError("callback secret reference is unresolved")
    parsed, _ = _parse_callback_url(url)
    hostname, addresses = await resolve_callback(url, allow_private=allow_private)
    body = json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("ascii") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Hermes-Callback-Version": "1",
        "X-Hermes-Callback-Timestamp": timestamp,
        "X-Hermes-Callback-Signature": "sha256=" + signature,
        "X-Hermes-Callback-Delivery": str(envelope.get("execution_id") or ""),
    }
    connector = aiohttp.TCPConnector(
        resolver=PinnedResolver(hostname, addresses),
        use_dns_cache=False,
        force_close=True,
    )
    client_timeout = aiohttp.ClientTimeout(total=max(0.1, float(timeout)))
    attempts = max(1, min(int(max_attempts), 5))
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=client_timeout,
        trust_env=False,
    ) as session:
        for attempt in range(1, attempts + 1):
            try:
                async with session.post(
                    parsed.geturl(),
                    data=body,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    status = int(response.status)
                    await response.content.read(4096)
                    if 200 <= status < 300:
                        return CallbackResult(status, attempt)
                    if 300 <= status < 400:
                        raise CallbackSecurityError(
                            f"callback redirect refused (HTTP {status})"
                        )
                    retryable = status in {408, 425, 429} or status >= 500
                    if not retryable:
                        raise CallbackSecurityError(
                            f"callback refused non-retryable HTTP {status}"
                        )
                    if attempt == attempts:
                        return CallbackResult(status, attempt)
            except CallbackSecurityError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt == attempts:
                    raise
            await asyncio.sleep(min(2 ** (attempt - 1), 5))
    raise RuntimeError("callback delivery exhausted without a result")
