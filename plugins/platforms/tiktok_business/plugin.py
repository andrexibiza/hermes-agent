"""TikTok Business Messaging plugin registration.

Per the design spec §8.1: registers the TikTok Business Messaging
platform plugin through ``ctx.register_platform()`` with the standard
callback set, mirroring the LINE plugin pattern.
"""

from __future__ import annotations

import logging
import os

from plugins.platforms.tiktok_business.adapter import (
    TikTokBusinessAdapter,
    TIKTOK_PLATFORM_HINT,
)

logger = logging.getLogger(__name__)


REQUIRED_ENV = [
    "TIKTOK_BUSINESS_ACCESS_TOKEN",
    "TIKTOK_BUSINESS_ACCOUNT_ID",
]


def check_requirements() -> bool:
    """Plugin gate: require credentials AND aiohttp at runtime."""
    # Allow configuration via environment OR via plugins.entries config
    has_token = (
        os.environ.get("TIKTOK_BUSINESS_ACCESS_TOKEN")
        or _has_configured_accounts()
    )
    if not has_token:
        return False
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return True


def _has_configured_accounts() -> bool:
    """Check if accounts are configured via plugins.entries."""
    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly() or {}
        plugins = config.get("plugins", {})
        entries = plugins.get("entries", {})
        tiktok_cfg = entries.get("tiktok-business", {})
        settings = tiktok_cfg.get("settings", {})
        accounts = settings.get("accounts", {})
        if isinstance(accounts, dict) and accounts:
            return True
    except Exception:
        pass
    return False


def validate_config(config: Any) -> bool:
    """Validate that the platform config has at least one account configured."""
    extra = getattr(config, "extra", {}) or {}
    accounts = extra.get("accounts", {})
    if isinstance(accounts, dict) and accounts:
        return True
    # Fallback: check env vars
    has_token = bool(os.environ.get("TIKTOK_BUSINESS_ACCESS_TOKEN"))
    has_account = bool(os.environ.get("TIKTOK_BUSINESS_ACCOUNT_ID"))
    return has_token and has_account


def is_connected(config: Any) -> bool:
    """Surface in ``hermes status`` even before the adapter is instantiated."""
    return validate_config(config)


def _env_enablement() -> dict:
    """Auto-seed PlatformConfig.extra from env-only setups."""
    if not (
        os.environ.get("TIKTOK_BUSINESS_ACCESS_TOKEN")
        and os.environ.get("TIKTOK_BUSINESS_ACCOUNT_ID")
    ):
        return None

    seeded = {}
    for env_key, extra_key in [
        ("TIKTOK_BUSINESS_PORT", "port"),
        ("TIKTOK_BUSINESS_HOST", "host"),
        ("TIKTOK_BUSINESS_PUBLIC_URL", "public_url"),
        ("TIKTOK_BUSINESS_API_VERSION", "api_version"),
        ("TIKTOK_BUSINESS_ALLOW_ALL_USERS", "allow_all_users"),
    ]:
        val = os.environ.get(env_key)
        if val:
            if extra_key == "port":
                try:
                    seeded[extra_key] = int(val)
                except ValueError:
                    pass
            elif extra_key == "allow_all_users":
                seeded[extra_key] = val.strip().lower() in ("1", "true", "yes", "on")
            else:
                seeded[extra_key] = val
    return seeded or {}


async def _standalone_send(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list] = None,
    force_document: bool = False,
) -> dict:
    """Out-of-process push delivery for cron jobs.

    Without this hook, cron delivery to TikTok fails when the gateway
    is not co-resident.  This creates an ephemeral client, sends the
    message, and closes.
    """
    extra = getattr(pconfig, "extra", {}) or {}
    accounts = extra.get("accounts", {})

    # Resolve chat_id to find the right account
    from plugins.platforms.tiktok_business.adapter import _parse_chat_id
    parsed = _parse_chat_id(chat_id)
    if parsed is None:
        return {"error": f"Invalid chat_id: {chat_id}"}

    _, account_alias, conversation_id = parsed
    account_cfg = accounts.get(account_alias)
    if not account_cfg:
        return {"error": f"Account not configured: {account_alias}"}

    from plugins.platforms.tiktok_business.models import AccountConfig, PROVIDER_TIKTOK_BUSINESS
    from plugins.platforms.tiktok_business.client import TikTokBusinessClient
    from plugins.bytedance.shared.tokens import TokenBroker

    account = AccountConfig(
        provider=PROVIDER_TIKTOK_BUSINESS,
        profile=os.environ.get("HERMES_PROFILE", "default"),
        account_alias=account_alias,
        provider_account_id=account_cfg.get("business_account_id", ""),
        access_token_secret=account_cfg.get(
            "access_token_secret",
            "tiktok_business/access_token",
        ),
        webhook_secret=None,
        route_id=account_cfg.get("route_id", ""),
        allowed_users=[],
        allow_all_users=False,
    )

    client = TikTokBusinessClient(account, token_broker=TokenBroker())
    try:
        # Check capability before sending
        cap = await client.get_conversation_capability(conversation_id)
        if not cap.can_send:
            return {
                "error": "Conversation is closed or does not allow sending",
                "reason": "capability_denied",
            }

        result = await client.send_message(conversation_id, message)
        data = result.get("data") or {}
        msg_id = data.get("message_id", "")
        return {"success": True, "message_id": msg_id}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        await client.close()


def interactive_setup() -> None:
    """Minimal stdin wizard for ``hermes setup tiktok-business``."""
    print()
    print("TikTok Business Messaging setup")
    print("--------------------------------")
    print("1. Go to https://business-api.tiktok.com/ and create a Business Account")
    print("2. Generate an access token with the Business Messaging scopes:")
    print("   - business_messaging_read")
    print("   - business_messaging_send")
    print("3. Configure webhook URL to point to this gateway")
    print()

    try:
        from hermes_cli.config import get_env_value as _get_env, save_env_value as _set_env
    except ImportError:
        print("hermes_cli.config not available; set TIKTOK_BUSINESS_* vars manually")
        return

    def _prompt(var: str, prompt_text: str, *, secret: bool = False) -> None:
        existing = _get_env(var) if callable(_get_env) else None
        suffix = " [keep current]" if existing else ""
        try:
            if secret:
                from hermes_cli.secret_prompt import masked_secret_prompt
                value = masked_secret_prompt(f"{prompt_text}{suffix}: ")
            else:
                value = input(f"{prompt_text}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if value:
            _set_env(var, value)

    _prompt("TIKTOK_BUSINESS_ACCESS_TOKEN", "Access token", secret=True)
    _prompt("TIKTOK_BUSINESS_ACCOUNT_ID", "Business Account ID")
    _prompt("TIKTOK_BUSINESS_PUBLIC_URL", "Public HTTPS base URL (e.g. https://my-gateway.example)")
    _prompt("TIKTOK_BUSINESS_WEBHOOK_SECRET", "Webhook secret", secret=True)
    print("Done. Configure the webhook URL in the TikTok Business Center panel.")


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="tiktok_business",
        label="TikTok Business Messaging",
        adapter_factory=lambda cfg: TikTokBusinessAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=REQUIRED_ENV,
        install_hint="pip install aiohttp",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="TIKTOK_BUSINESS_HOME_CONVERSATION",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="TIKTOK_BUSINESS_ALLOWED_USERS",
        allow_all_env="TIKTOK_BUSINESS_ALLOW_ALL_USERS",
        emoji="🎵",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=TIKTOK_PLATFORM_HINT,
    )


# Re-export for test compatibility
from typing import Optional  # noqa: E402
