"""Static composition gate for Task 13's extracted delivery seam."""

from gateway.platforms.webhook import WebhookAdapter
from gateway.platforms.webhook_delivery import WebhookDeliveryMixin


def test_adapter_keeps_the_extracted_delivery_mixin():
    assert WebhookDeliveryMixin in WebhookAdapter.__mro__
    assert hasattr(WebhookAdapter, "_deliver_targets")
    assert hasattr(WebhookAdapter, "_finalize_webhook_delivery")


def test_route_callback_cannot_override_private_address_block():
    import inspect

    source = inspect.getsource(WebhookAdapter._run_completion_callback)
    assert "allow_private=False" in source
    assert 'callback.get("allow_private"' not in source


def test_session_close_failure_does_not_skip_finalization():
    import inspect

    source = inspect.getsource(WebhookAdapter.on_processing_complete)
    assert "session cleanup must never suppress" in source.lower()
    assert source.index("_end_webhook_session") < source.index("_finalize_webhook_delivery")
