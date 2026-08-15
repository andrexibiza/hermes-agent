"""Douyin Open Platform platform adapter.

Per design spec §11: receives and answers Douyin private-message and
eligible group-message events using Douyin Open Platform webhooks and
IM OpenAPI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from gateway.config import PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

from plugins.bytedance.shared.errors import ProviderError
from plugins.bytedance.shared.observability import Metrics, hash_id
from plugins.bytedance.shared.state import StateStore, get_state_store
from plugins.bytedance.shared.webhook import (
    CompositeIdempotencyKey,
    NormalizedEvent,
    WebhookIngress,
)
from plugins.platforms.douyin.client import DouyinClient
from plugins.platforms.douyin.models import (
    DouyinAccountConfig,
    DouyinSendGrant,
    PROVIDER_DOUYIN,
    SCOPE_IM_DIRECT_MESSAGE,
)
from plugins.platforms.douyin.policy import DouyinPolicyEngine
from plugins.platforms.douyin.webhook import (
    DouyinWebhookParser,
    DouyinWebhookVerifier,
)

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8655

# Canonical chat ID: <provider>:<account_alias>:<provider_conversation_id>
def _build_chat_id(provider: str, account_alias: str, conversation_id: str) -> str:
    return f"{provider}:{account_alias}:{conversation_id}"


def _parse_chat_id(chat_id: str) -> Optional[tuple]:
    parts = chat_id.split(":", 2)
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


class DouyinAdapter(BasePlatformAdapter):
    """Douyin Open Platform IM gateway adapter.

    Runs an aiohttp webhook server, receives Douyin IM events, and
    routes them to the Hermes agent.  Direction resolution and
    scene-aware send grants are enforced before every outbound.
    """

    interactive_resume: bool = False  # webhook runs are event-triggered

    def __init__(self, config: PlatformConfig) -> None:
        from gateway.config import Platform
        super().__init__(config, Platform.DOUYIN)
        self._extra = getattr(config, "extra", {}) or {}
        self._accounts: Dict[str, DouyinAccountConfig] = {}
        self._clients: Dict[str, DouyinClient] = {}
        self._state = get_state_store()
        self._policy = DouyinPolicyEngine()
        self._ingress: Dict[str, WebhookIngress] = {}
        self._profile = self._resolve_profile()

        self._parse_accounts()

        self._host = self._extra.get("host") or os.environ.get("DOUYIN_HOST")
        self._port = int(
            self._extra.get("port")
            or os.environ.get("DOUYIN_PORT", DEFAULT_PORT)
        )
        self._public_url = self._extra.get("public_url") or os.environ.get(
            "DOUYIN_PUBLIC_URL"
        )

        self._runner = None
        self._app = None
        self._connected = False

    def _resolve_profile(self) -> str:
        try:
            import os
            return os.environ.get("HERMES_PROFILE", "default")
        except Exception:
            return "default"

    def _parse_accounts(self) -> None:
        accounts_cfg = self._extra.get("accounts", {}) or {}
        for alias, cfg in accounts_cfg.items():
            if not isinstance(cfg, dict):
                continue
            self._accounts[alias] = DouyinAccountConfig(
                provider=PROVIDER_DOUYIN,
                profile=self._profile,
                account_alias=alias,
                open_id=cfg.get("account_open_id_secret") or "",
                client_key=cfg.get("client_key", ""),
                client_secret=cfg.get("client_secret", ""),
                access_token_secret=(
                    cfg.get("access_token_secret") or "douyin/access_token"
                ),
                refresh_token_secret=(
                    cfg.get("refresh_token_secret") or "douyin/refresh_token"
                ),
                webhook_secret=(
                    cfg.get("webhook_secret") or "douyin/webhook_secret"
                ),
                route_id=cfg.get("route_id") or secrets.token_urlsafe(16),
                home_conversation=cfg.get("home_conversation"),
                allowed_users=cfg.get("allowed_users", []) or [],
                allow_all_users=cfg.get("allow_all_users", False),
                region=cfg.get("region"),
            )

    def _find_account_by_route(self, route_id: str) -> Optional[DouyinAccountConfig]:
        for account in self._accounts.values():
            if account.route_id == route_id:
                return account
        return None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to Douyin Open Platform."""
        if not self._accounts:
            logger.error("[douyin] No accounts configured")
            self._fatal_error_code = "no_accounts"
            self._fatal_error_message = "No Douyin accounts configured"
            return False

        for alias, account in self._accounts.items():
            try:
                client = DouyinClient(account)
                self._clients[alias] = client

                # Inspect scopes and check eligibility
                scopes = await client.inspect_scopes()
                self._scopes_cache = scopes

                if not client.check_account_eligibility():
                    logger.warning(
                        "[douyin] Account %s lacks im.direct_message scope — "
                        "IM features disabled",
                        alias,
                    )
                    continue

                # Set up ingress
                verifier = DouyinWebhookVerifier()
                parser = DouyinWebhookParser()
                self._ingress[account.route_id] = WebhookIngress(
                    provider=PROVIDER_DOUYIN,
                    verifier=verifier,
                    parser=parser,
                    state_store=self._state,
                )

            except Exception as e:
                logger.error("[douyin] Account %s connect failed: %s", alias, e)
                self._clients.pop(alias, None)
                continue

        if not self._clients:
            logger.error("[douyin] No accounts could connect")
            self._fatal_error_code = "auth_failure"
            return False

        # Start webhook server
        await self._start_webhook_server()

        # Restore unprocessed events
        await self._restore_unprocessed_events()

        self._connected = True
        self._mark_connected()
        logger.info(
            "[douyin] Connected %d account(s), webhook on %s:%d",
            len(self._clients),
            self._host or "*",
            self._port,
        )
        return True

    async def _start_webhook_server(self) -> None:
        try:
            from aiohttp import web
        except ImportError:
            self._fatal_error_code = "missing_aiohttp"
            self._fatal_error_message = "aiohttp is required"
            return

        app = web.Application()
        app.router.add_post(
            "/douyin/webhook/{route_id}", self._handle_webhook
        )
        app.router.add_get("/douyin/health", self._handle_health)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._host or None, self._port)
        await site.start()
        self._app = app
        self._runner = runner

    async def _handle_health(self, request: Any) -> Any:
        from aiohttp import web
        return web.json_response({"status": "ok", "platform": "douyin"})

    async def _handle_webhook(self, request: Any) -> Any:
        from aiohttp import web

        route_id = request.match_info.get("route_id", "")
        account = self._find_account_by_route(route_id)
        if not account:
            return web.json_response(
                {"error": f"Unknown route: {route_id}"}, status=404
            )

        ingress = self._ingress.get(route_id)
        if ingress is None:
            return web.json_response(
                {"error": "Ingress not configured"}, status=503
            )

        raw_body = await request.read()
        headers = dict(request.headers)

        # Verify + parse
        event, error = ingress.verify_and_parse(
            raw_body=raw_body,
            headers=headers,
            route_config={
                "webhook_secret": account.webhook_secret,
                "open_id": account.open_id,
            },
            profile=self._profile,
            account_alias=account.account_alias,
            route=route_id,
        )

        if error:
            if ingress.is_duplicate_sentinel(error):
                return web.json_response({"status": "ok"})
            return web.json_response({"error": "rejected"}, status=403)

        # Ack immediately
        ack = ingress.acknowledge({})

        if event is not None:
            asyncio.create_task(self._process_event(event, account))

        return web.json_response(ack)

    async def _process_event(self, event: NormalizedEvent, account: DouyinAccountConfig) -> None:
        """Process a verified Douyin webhook event."""
        try:
            self._state.update_webhook_state(
                self._profile,
                PROVIDER_DOUYIN,
                account.account_alias,
                event.event_id,
                "processing",
            )

            # Handle contract events
            if event.event_type == "contract_unauthorize":
                client = self._clients.get(account.account_alias)
                if client:
                    client.handle_unauthorize()
                    self._state.update_webhook_state(
                        self._profile, PROVIDER_DOUYIN,
                        account.account_alias, event.event_id, "completed",
                    )
                return

            if event.event_type == "contract_authorize":
                client = self._clients.get(account.account_alias)
                if client:
                    client.handle_authorize()
                self._state.update_webhook_state(
                    self._profile, PROVIDER_DOUYIN,
                    account.account_alias, event.event_id, "completed",
                )
                return

            # Direction resolution (§11.3)
            direction = event.payload.get("_direction", "inbound")
            sender_is_self = event.payload.get("_sender_is_self", False)

            if sender_is_self:
                # Echo — suppress
                Metrics.increment(
                    "bytedance_message_dispatch_total",
                    labels={
                        "provider": "douyin",
                        "type": "echo_suppressed",
                        "result": "suppressed",
                    },
                )
                self._state.update_webhook_state(
                    self._profile, PROVIDER_DOUYIN,
                    account.account_alias, event.event_id, "completed",
                )
                return

            # Handle enter_direct_msg — creates a short-lived send grant
            if event.event_type == "im_enter_direct_msg":
                conv_short = event.conversation_id or ""
                client = self._clients.get(account.account_alias)
                if client:
                    client.create_send_grant(
                        conv_short,
                        scene="im_enter_direct_msg",
                        expires_at=time.time() + 300,  # 5 minutes
                        remaining_count=1,
                        source_event_id=event.event_id,
                        eligible=True,
                    )
                self._state.update_webhook_state(
                    self._profile, PROVIDER_DOUYIN,
                    account.account_alias, event.event_id, "completed",
                )
                return

            # Handle recall — correlate by server_message_id
            if event.event_type == "im_recall_msg":
                server_msg_id = event.payload.get("_server_message_id")
                if server_msg_id:
                    # Update local metadata — don't rewrite agent history
                    logger.info(
                        "[douyin] Recall of message %s",
                        hash_id(server_msg_id),
                    )
                self._state.update_webhook_state(
                    self._profile, PROVIDER_DOUYIN,
                    account.account_alias, event.event_id, "completed",
                )
                return

            # Process as inbound message (im_send_msg / im_receive_msg)
            conversation_id = event.conversation_id or ""
            chat_id = _build_chat_id(
                PROVIDER_DOUYIN,
                account.account_alias,
                conversation_id,
            )

            text = ""
            media_urls: List[str] = []
            media_types: List[str] = []
            msg_type = MessageType.TEXT

            if event.message_type == "text":
                text = event.payload.get("content", event.payload.get("text", ""))
            elif event.message_type in ("image", "video", "audio", "file"):
                media_result = await self._fetch_inbound_media(event)
                if media_result:
                    media_urls.append(media_result.local_path)
                    media_types.append(media_result.mime_type)
                text = f"[{event.message_type}]"
                msg_type = {
                    "image": MessageType.PHOTO,
                    "video": MessageType.VIDEO,
                    "audio": MessageType.VOICE,
                    "file": MessageType.FILE,
                }.get(event.message_type, MessageType.TEXT)
            else:
                text = f"[{event.message_type}]"

            source_obj = self.build_source(
                chat_id=chat_id,
                chat_type="dm",
                user_id=event.sender_id or "",
                user_name=event.sender_id or "",
                chat_name=chat_id,
            )

            message_event = MessageEvent(
                text=text,
                message_type=msg_type,
                source=source_obj,
                raw_message=event.payload,
                message_id=event.message_id or "",
                media_urls=media_urls,
                media_types=media_types,
            )

            Metrics.increment(
                "bytedance_message_dispatch_total",
                labels={
                    "provider": "douyin",
                    "type": event.message_type or "unknown",
                    "result": "dispatched",
                },
            )

            await self.handle_message(message_event)

            self._state.update_webhook_state(
                self._profile,
                PROVIDER_DOUYIN,
                account.account_alias,
                event.event_id,
                "completed",
            )

        except Exception as e:
            logger.exception("[douyin] Event processing failed: %s", e)
            self._state.update_webhook_state(
                self._profile, PROVIDER_DOUYIN,
                event.message_id or "", "failed", error=str(e),
            )

    async def _fetch_inbound_media(self, event: NormalizedEvent) -> Optional[Any]:
        try:
            from plugins.bytedance.shared.media import MediaBroker
            broker = MediaBroker()
            media_url = event.payload.get("media_url") or event.payload.get("download_url")
            if not media_url:
                return None
            return await broker.download(media_url)
        except Exception as exc:
            logger.warning("[douyin] Failed to fetch inbound media: %s", exc)
            return None

    async def _restore_unprocessed_events(self) -> None:
        for alias, account in self._accounts.items():
            events = self._state.get_unprocessed_events(
                self._profile, PROVIDER_DOUYIN, alias, limit=100,
            )
            for event_id, raw_sha, route in events:
                logger.info(
                    "[douyin] Recovered unprocessed event %s for %s",
                    hash_id(event_id), alias,
                )

    # ------------------------------------------------------------------
    # Outbound send (§11.4 scene-aware)
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message to a Douyin conversation.

        Per §11.4: must select a valid send grant and atomically
        consume its allowance.  Provider rules are versioned in
        policy fixtures.
        """
        parsed = _parse_chat_id(chat_id)
        if parsed is None:
            return SendResult(success=False, error=f"Invalid chat_id: {chat_id}")

        provider, account_alias, conversation_id = parsed
        account = self._accounts.get(account_alias)
        if account is None:
            return SendResult(success=False, error=f"Unknown account: {account_alias}")

        client = self._clients.get(account_alias)
        if client is None:
            return SendResult(success=False, error=f"Client not connected for {account_alias}")

        # Check eligibility
        if not client.check_account_eligibility():
            return SendResult(
                success=False, error="Account lacks im.direct_message scope",
            )

        # Check send grant
        decision = self._policy.check_send(
            conversation_id,
            client=client,
            sender_id=metadata.get("sender_id") if metadata else None,
            scopes=client.scopes,
            allow_all_users=account.allow_all_users,
            allowed_users=set(account.allowed_users) if account.allowed_users else None,
        )

        if not decision.allowed:
            if decision.requires_new_grant:
                return SendResult(
                    success=False,
                    error="No valid send grant for this conversation",
                    error_code="no_send_grant",
                )
            return SendResult(
                success=False,
                error=f"Send denied: {decision.reason_code}",
                error_code=decision.reason_code,
            )

        # Consume the grant atomically
        if not client.consume_send_grant(conversation_id):
            return SendResult(
                success=False,
                error="Send grant could not be consumed (concurrent)",
                error_code="grant_concurrent",
            )

        # Perform the send
        try:
            result = await client.send_private_msg(
                conversation_id, content,
            )
            data = result.get("data") or {}
            msg_id = data.get("message_id", "") or data.get("server_message_id", "")

            Metrics.increment(
                "bytedance_message_send_total",
                labels={
                    "provider": "douyin",
                    "type": "text",
                    "result": "success",
                },
            )

            return SendResult(success=True, message_id=msg_id)
        except ProviderError as e:
            Metrics.increment(
                "bytedance_message_send_total",
                labels={
                    "provider": "douyin",
                    "type": "text",
                    "result": "failure",
                },
            )
            return SendResult(
                success=False,
                error=e.message,
                error_code=e.provider_code,
            )

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        parsed = _parse_chat_id(chat_id)
        if parsed is None:
            return {"name": chat_id, "type": "dm"}
        return {"name": chat_id, "type": "dm"}

    def format_message(self, content: str) -> str:
        return content

    async def disconnect(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
        self._connected = False
        self._mark_disconnected()

    def toolsets_for_source(self, source) -> Optional[List[str]]:
        return None


@staticmethod
def _tt(seconds: float):
    import aiohttp
    return aiohttp.ClientTimeout(total=seconds)


DOUYIN_PLATFORM_HINT = """
You are chatting via Douyin.  This is a private-message channel for
a Douyin account.  Sent messages count against the account's messaging
quota.  Keep responses concise.  Media sending may require media
upload through the Douyin media-resource API.
"""

import aiohttp  # noqa: E402
import time  # noqa: E402
