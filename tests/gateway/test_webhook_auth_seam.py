"""Seam tests for the webhook signature-validation mixin extraction."""

from gateway.config import PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.webhook import WebhookAdapter, _hmac_str_equal as legacy_equal
from gateway.platforms.webhook_auth import (
    WebhookAuthMixin,
    _hmac_str_equal as auth_equal,
)
from gateway.platforms.webhook_profile_admission import WebhookProfileAdmissionMixin


def _make_adapter() -> WebhookAdapter:
    return WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={"host": "127.0.0.1", "port": 0, "routes": {}},
        )
    )


def test_signature_methods_resolve_through_original_adapter_seam():
    assert WebhookAdapter.__mro__[:4] == (
        WebhookAdapter,
        WebhookAuthMixin,
        WebhookProfileAdmissionMixin,
        BasePlatformAdapter,
    )
    assert WebhookAdapter._validate_signature is WebhookAuthMixin._validate_signature
    assert (
        WebhookAdapter._validate_svix_signature
        is WebhookAuthMixin._validate_svix_signature
    )


def test_hmac_helper_is_reexported_from_both_namespaces():
    assert legacy_equal is auth_equal


def test_adapter_starts_with_empty_v1_signature_warning_set():
    adapter = _make_adapter()
    assert type(adapter._v1_signature_warned) is set
    assert adapter._v1_signature_warned == set()


def test_original_adapter_class_patch_affects_instances(monkeypatch):
    adapter = _make_adapter()

    def patched_validate_signature(self, request, body, secret):
        return self, request, body, secret

    monkeypatch.setattr(WebhookAdapter, "_validate_signature", patched_validate_signature)
    assert adapter._validate_signature("request", b"body", "secret") == (
        adapter,
        "request",
        b"body",
        "secret",
    )
