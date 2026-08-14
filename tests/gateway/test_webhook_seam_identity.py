"""MRO and public-seam identity tests for Webhook Revolution Task 5."""

from gateway.platforms.webhook import WebhookAdapter
from gateway.platforms.webhook_auth import WebhookAuthMixin
from gateway.platforms.webhook_delivery import WebhookDeliveryMixin
from gateway.platforms.webhook_ingress import WebhookIngressMixin
from gateway.platforms.webhook_rendering import WebhookRenderingMixin


def test_webhook_adapter_composes_all_four_responsibility_seams():
    mro = WebhookAdapter.__mro__
    for mixin in (
        WebhookAuthMixin,
        WebhookIngressMixin,
        WebhookRenderingMixin,
        WebhookDeliveryMixin,
    ):
        assert mixin in mro


def test_moved_ingress_method_identity_resolves_through_original_class():
    for name in (
        "_handle_webhook",
        "_record_delivery_id",
        "_record_rate_limit_hit",
        "_reload_dynamic_routes",
    ):
        assert getattr(WebhookAdapter, name) is getattr(WebhookIngressMixin, name)


def test_moved_rendering_method_identity_resolves_through_original_class():
    for name in ("_render_prompt", "_render_delivery_extra"):
        assert getattr(WebhookAdapter, name) is getattr(WebhookRenderingMixin, name)


def test_moved_delivery_method_identity_resolves_through_original_class():
    for name in ("_direct_deliver", "_deliver_github_comment", "_deliver_cross_platform"):
        assert getattr(WebhookAdapter, name) is getattr(WebhookDeliveryMixin, name)


def test_original_import_path_remains_public():
    assert WebhookAdapter.__module__ == "gateway.platforms.webhook"
