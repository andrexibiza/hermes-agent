"""Callback SSRF, DNS-pinning, and envelope contracts for Task 13."""

import asyncio

import pytest

from gateway.platforms.webhook_callback_transport import (
    CallbackSecurityError,
    PinnedResolver,
    callback_envelope,
    resolve_callback,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "::1", "169.254.169.254", "10.0.0.1", "192.168.1.1"],
)
async def test_private_loopback_and_metadata_destinations_are_rejected(
    monkeypatch, address
):
    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(*_args, **_kwargs):
        family = 10 if ":" in address else 2
        return [(family, 1, 6, "", (address, 443))]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(CallbackSecurityError, match="blocked"):
        await resolve_callback("https://callback.example/hook")


@pytest.mark.asyncio
async def test_pinned_resolver_refuses_hostname_change():
    resolver = PinnedResolver("callback.example", ["8.8.8.8"])
    first = await resolver.resolve("callback.example", 443)
    assert first[0]["host"] == "8.8.8.8"
    with pytest.raises(CallbackSecurityError, match="hostname changed"):
        await resolver.resolve("redirect.example", 443)


def test_callback_url_credentials_are_rejected():
    async def run():
        await resolve_callback("https://user:pass@example.com/hook")

    with pytest.raises(CallbackSecurityError, match="credentials"):
        asyncio.run(run())


def test_callback_envelope_is_versioned():
    envelope = callback_envelope(
        execution_id="exec",
        profile="default",
        route="route",
        state="completed",
    )
    assert envelope["schema_version"] == 1
    assert envelope["execution_id"] == "exec"
