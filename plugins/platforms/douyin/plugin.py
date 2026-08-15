"""Douyin Open Platform platform plugin registration."""

from __future__ import annotations

import logging
import os
from typing import Any

from plugins.platforms.douyin.adapter import (
    DouyinAdapter,
    DOUYIN_PLATFORM_HINT,
)

logger = logging.getLogger(__name__)

REQUIRED_ENV = [
    "DOUYIN_CLIENT_KEY",
    "DOUYIN_CLIENT_SECRET",
    "DOUYIN_OPEN_ID",
    "DOUYIN_ACCESS_TOKEN_SECRET_REF",
    "DOUYIN_REFRESH_TOKEN_SECRET_REF",
    "DOUYIN_WEBHOOK_SECRET",
]


def check_requirements() -> bool:
    """Plugin gate: require credentials AND aiohttp."""
    has_creds = _has_configured_accounts() or (
        os.environ.get("DOUYIN_CLIENT_KEY")
        and os.environ.get("DOUYIN_OPEN_ID")
    )
    if not has_creds:
        return False
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return True


def _has_configured_accounts() -> bool:
    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly() or {}
        plugins = config.get("plugins", {})
        entries = plugins.get("entries", {})
        dy_cfg = entries.get("douyin", {})
        settings = dy_cfg.get("settings", {})
        accounts = settings.get("accounts", {})
        if isinstance(accounts, dict) and accounts:
            return True
    except Exception:
        pass
    return False


def validate_config(config: Any) -> bool:
    extra = getattr(config, "extra", {}) or {}
    accounts = extra.get("accounts", {})
    if isinstance(accounts, dict) and accounts:
        return True
    has_client = bool(os.environ.get("DOUYIN_CLIENT_KEY"))
    has_openid = bool(os.environ.get("DOUYIN_OPEN_ID"))
    return has_client and has_openid


def is_connected(config: Any) -> bool:
    return validate_config(config)


def _env_enablement() -> dict:
    if not (os.environ.get("DOUYIN_CLIENT_KEY") and os.environ.get("DOUYIN_OPEN_ID")):
        return None
    seeded = {}
    for env_key, extra_key in [
        ("DOUYIN_PORT", "port"),
        ("DOUYIN_HOST", "host"),
        ("DOUYIN_PUBLIC_URL", "public_url"),
        ("DOUYIN_ALLOW_ALL_USERS", "allow_all_users"),
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
    """Out-of-process send for cron jobs."""
    extra = getattr(pconfig, "extra", {}) or {}
    accounts = extra.get("accounts", {})

    from plugins.platforms.douyin.adapter import _parse_chat_id
    parsed = _parse_chat_id(chat_id)
    if parsed is None:
        return {"error": f"Invalid chat_id: {chat_id}"}

    _, account_alias, conversation_short_id = parsed
    account_cfg = accounts.get(account_alias)
    if not account_cfg:
        return {"error": f"Account not configured: {account_alias}"}

    from plugins.platforms.douyin.models import DouyinAccountConfig, PROVIDER_DOUYIN
    from plugins.platforms.douyin.client import DouyinClient
    from plugins.bytedance.shared.tokens import TokenBroker

    account = DouyinAccountConfig(
        provider=PROVIDER_DOUYIN,
        profile=os.environ.get("HERMES_PROFILE", "default"),
        account_alias=account_alias,
        open_id=account_cfg.get("account_open_id", ""),
        client_key=account_cfg.get("client_key", ""),
        client_secret=account_cfg.get("client_secret", ""),
        access_token_secret=account_cfg.get(
            "access_token_secret", "douyin/access_token"
        ),
        refresh_token_secret=account_cfg.get(
            "refresh_token_secret", "douyin/refresh_token"
        ),
        webhook_secret=account_cfg.get("webhook_secret", ""),
        route_id=account_cfg.get("route_id", ""),
    )

    # Standalone cron send must fail closed without a valid grant (§11.4)
    client = DouyinClient(account, token_broker=TokenBroker())
    try:
        grant = client.get_send_grant(conversation_short_id)
        if not grant or not grant.eligible:
            return {
                "error": "No valid send grant — cron standalone send denied",
                "reason_code": "no_send_grant",
            }
        if not client.consume_send_grant(conversation_short_id):
            return {"error": "Could not consume send grant", "reason_code": "grant_concurrent"}

        result = await client.send_private_msg(conversation_short_id, message)
        data = result.get("data") or {}
        msg_id = data.get("message_id", "")
        return {"success": True, "message_id": msg_id}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        await client.close()


def interactive_setup() -> None:
    """Minimal stdin wizard for ``hermes setup douyin``."""
    print()
    print("Douyin Open Platform setup")
    print("---------------------------")
    print("1. Go to https://open.douyin.com and create an app")
    print("2. Apply for im.direct_message scope")
    print("3. Configure webhook URL to point to this gateway")
    print("4. Ensure the app is approved for IM operations")
    print()

    try:
        from hermes_cli.config import get_env_value as _get_env, save_env_value as _set_env
    except ImportError:
        print("hermes_cli.config not available; set DOUYIN_* vars manually")
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

    _prompt("DOUYIN_CLIENT_KEY", "Client key")
    _prompt("DOUYIN_CLIENT_SECRET", "Client secret", secret=True)
    _prompt("DOUYIN_OPEN_ID", "Open ID")
    _prompt("DOUYIN_ACCESS_TOKEN_SECRET_REF", "Access token secret ref")
    _prompt("DOUYIN_REFRESH_TOKEN_SECRET_REF", "Refresh token secret ref")
    _prompt("DOUYIN_WEBHOOK_SECRET", "Webhook secret", secret=True)
    _prompt("DOUYIN_PUBLIC_URL", "Public HTTPS base URL (e.g. https://cn-gateway.example)")
    _prompt("DOUYIN_ALLOWED_USERS", "Allowed user IDs (comma-separated)")
    print("Done. Configure the webhook URL in the Douyin Open Platform console.")


def register(ctx) -> None:
    """Plugin entry point — registers the Douyin IM platform."""
    ctx.register_platform(
        name="douyin",
        label="Douyin",
        adapter_factory=lambda cfg: DouyinAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=REQUIRED_ENV,
        install_hint="pip install aiohttp",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="DOUYIN_HOME_CONVERSATION",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="DOUYIN_ALLOWED_USERS",
        allow_all_env="DOUYIN_ALLOW_ALL_USERS",
        emoji="🎶",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=DOUYIN_PLATFORM_HINT,
    )


from typing import Optional  # noqa: E402
